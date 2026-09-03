"""
L2 · 集成层 —— 真实工具调用、真实网络，测端到端结论是否成立且自洽。

断言全部是【不变量】，不是固定风险等级。真实天气每天在变，把等级钉死必然天天红。
遵守 Anthropic 评测指南："grade what the agent produced, not the path it took"——
这里不检查工具调用顺序，只检查产出物与关键能力。

这一层如果早就存在，第 4 组断言会在第一次运行时抓到"船舶浪高恒为 None"。

运行：python -m evals.l2_integration
"""

import datetime
import importlib
import json
import os
import sys
import time

from fastapi.testclient import TestClient

import evidence
import rules

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

VALID_LEVELS = {"LOW", "MEDIUM", "HIGH", "UNKNOWN"}
REQUIRED_FIELDS = ("risk_level", "risk_label", "is_go_recommended", "core_reason",
                   "alternative_advice", "weather_summary", "rule_override",
                   "decision_source", "evidence", "trace", "triggers")
# Vercel 函数硬上限 60s。模型调用有 32s 超时并降级到 rule_only，
# 所以端到端最坏 ≈ 预取(~2s) + 超时(32s) + 余量。50s 仍能抓住"顶穿上限"的回归。
LATENCY_BUDGET_S = 50.0


def _today(offset=0):
    return (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()


class Report:
    def __init__(self):
        self.passed, self.failed = 0, []

    def check(self, ok, label, detail=""):
        if ok:
            self.passed += 1
            print(f"    {GREEN}✓{RESET} {label} {DIM}{detail}{RESET}")
        else:
            self.failed.append(label)
            print(f"    {RED}✗{RESET} {label} {DIM}{detail}{RESET}")
        return ok


def call(client, **kw):
    body = {"lang": "zh", **kw}
    t0 = time.time()
    resp = client.post("/api/assess", json=body)
    return resp, time.time() - t0


def check_schema(rep, data, label):
    missing = [f for f in REQUIRED_FIELDS if f not in data]
    rep.check(not missing, f"{label} · 响应字段齐全", f"缺 {missing}" if missing else "")
    rep.check(data.get("risk_level") in VALID_LEVELS,
              f"{label} · risk_level 合法", f"got={data.get('risk_level')}")


def check_provenance(rep, data, label):
    """每一个被采信的数值都要能追到来源和取数时间（decision-to-provenance link）。"""
    bad = []
    for name, entry in data.get("evidence", {}).get("locations", {}).items():
        for block in ("atmos", "marine"):
            b = entry.get(block)
            if b and not (b.get("source") and b.get("fetched_at")):
                bad.append(f"{name}.{block}")
    rep.check(not bad, f"{label} · 证据带 source/fetched_at", f"缺失: {bad}" if bad else "")


def check_trigger_provenance(rep, data, label):
    """每条触发项都要能追到出处，且来源类型必须是三类之一。"""
    trig = data.get("triggers") or []
    bad_cls = [t["code"] for t in trig if t.get("cls") not in ("REG", "WARN", "PROD")]
    rep.check(not bad_cls, f"{label} · 触发项来源类型合法", f"异常: {bad_cls}" if bad_cls else "")
    no_src = [t["code"] for t in trig if not t.get("source")]
    rep.check(not no_src, f"{label} · 每条触发项都带出处", f"缺出处: {no_src}" if no_src else "")
    if data.get("risk_level") not in ("LOW", "UNKNOWN"):
        rep.check(bool(trig), f"{label} · 非 LOW 结论必须有支撑它的官方判据",
                  f"level={data.get('risk_level')} 但 triggers 为空")
        body = data.get("core_reason") or ""
        rep.check("以官方通知为准" in body or "official notices" in body,
                  f"{label} · 正文含「以官方通知为准」", body[-40:])


def check_no_assertive_wording(rep, data, label):
    """全响应不得出现替监管部门下结论的措辞。"""
    blob = json.dumps(data, ensure_ascii=False)
    banned = [w for w in ("停航预警", "无法起降", "航班将取消", "强制停航") if w in blob]
    rep.check(not banned, f"{label} · 无断言式措辞", f"命中: {banned}" if banned else "")


def check_self_consistency(rep, data, label):
    """拿返回的 evidence 重跑规则引擎，结论必须和返回的 risk_level 一致。"""
    bundle = evidence.EvidenceBundle.from_dict(data.get("evidence", {}))
    if not bundle.ok:
        rep.check(data.get("risk_level") == "UNKNOWN",
                  f"{label} · 证据不足时必须是 UNKNOWN", f"got={data.get('risk_level')}")
        return
    req, _ = rules.required_level(bundle)
    final = data.get("risk_level")
    if final == "UNKNOWN":
        rep.check(data.get("decision_source") == "llm_abstain",
                  f"{label} · 证据充分却 UNKNOWN，只能是模型主动弃权",
                  f"decision_source={data.get('decision_source')}")
        return
    ok = rules.LEVEL_ORDER[final] >= rules.LEVEL_ORDER[req]
    rep.check(ok, f"{label} · 最终等级不低于规则引擎要求", f"规则要求 {req}，最终 {final}")


def scenario_marine_route(rep, client):
    print(f"\n  【1】真实海运航线 上海→舟山（近海游船）")
    resp, dt = call(client, origin="上海", destination="舟山", date=_today(),
                    transport="ship", vessel_type="small")
    if not rep.check(resp.status_code == 200, "HTTP 200", f"status={resp.status_code}"):
        return
    data = resp.json()
    check_schema(rep, data, "海运")
    check_provenance(rep, data, "海运")
    check_trigger_provenance(rep, data, "海运")
    check_no_assertive_wording(rep, data, "海运")
    check_self_consistency(rep, data, "海运")
    rep.check(dt < LATENCY_BUDGET_S, "端到端时延在预算内", f"{dt:.1f}s / {LATENCY_BUDGET_S}s")

    waves = [(e.get("marine") or {}).get("max_wave_height_m")
             for e in data["evidence"]["locations"].values()]
    rep.check(all(w is not None for w in waves) or data["risk_level"] == "UNKNOWN",
              "海运两端浪高非空，或明确弃权 ← 这条早存在就会抓到浪高 bug",
              f"waves={waves} level={data['risk_level']}")

    kinds = {s.get("kind") for s in data.get("trace", [])}
    rep.check("prefetch" in kinds, "trace 含确定性预取步骤", f"kinds={sorted(kinds)}")
    rep.check(any(s.get("name") == "get_marine_forecast" for s in data["trace"]),
              "trace 里能看到海洋数据的实际调用")


def scenario_inland_as_ship(rep, client):
    print(f"\n  【2】内陆当船走 北京→西安（应当弃权）")
    resp, _ = call(client, origin="北京", destination="西安", date=_today(),
                   transport="ship", vessel_type="small")
    if not rep.check(resp.status_code == 200, "HTTP 200", f"status={resp.status_code}"):
        return
    data = resp.json()
    check_schema(rep, data, "内陆海运")
    rep.check(data["risk_level"] == "UNKNOWN", "判定为 UNKNOWN",
              f"got={data['risk_level']}")
    rep.check(data.get("is_go_recommended") is None,
              "is_go_recommended 为 null（不能落成'不建议出行'）",
              f"got={data.get('is_go_recommended')}")
    codes = {m["code"] for m in data["evidence"]["sufficiency"]["missing"]}
    rep.check(bool(codes & {"wave_height_missing", "location_unresolved", "not_coastal"}),
              "缺失原因指向浪高/坐标/不临海", f"codes={sorted(codes)}")
    rep.check(bool(data.get("missing_evidence")), "missing_evidence 有人话说明",
              str(data.get("missing_evidence"))[:70])


def scenario_out_of_horizon(rep, client):
    print(f"\n  【3】出行日期 +7 天（超出预报范围）")
    resp, _ = call(client, origin="杭州", destination="南京", date=_today(7),
                   transport="plane")
    if not rep.check(resp.status_code == 200, "HTTP 200", f"status={resp.status_code}"):
        return
    data = resp.json()
    rep.check(data["risk_level"] == "UNKNOWN", "判定为 UNKNOWN", f"got={data['risk_level']}")
    codes = {m["code"] for m in data["evidence"]["sufficiency"]["missing"]}
    # 航空走官方机场预报（TAF，约 30 小时），海事走 wttr.in（3 天），
    # 两条路径超范围的缺失码不同，都要接受
    rep.check(bool(codes & {"date_out_of_range", "beyond_taf_horizon"}),
              "缺失原因为超出官方预报范围", f"codes={sorted(codes)}")


def scenario_air_route(rep, client):
    print(f"\n  【4】真实航空航线 杭州→南京")
    resp, dt = call(client, origin="杭州", destination="南京", date=_today(), transport="plane")
    if not rep.check(resp.status_code == 200, "HTTP 200", f"status={resp.status_code}"):
        return
    data = resp.json()
    check_schema(rep, data, "航空")
    check_provenance(rep, data, "航空")
    check_trigger_provenance(rep, data, "航空")
    check_no_assertive_wording(rep, data, "航空")
    check_self_consistency(rep, data, "航空")
    rep.check(dt < LATENCY_BUDGET_S, "端到端时延在预算内", f"{dt:.1f}s")
    rep.check(data.get("decision_source") in
              {"llm", "rule_override_up", "rule_override_down", "rule_only",
               "llm_abstain", "abstain_insufficient_evidence"},
              "decision_source 可解释", f"got={data.get('decision_source')}")


def scenario_vessel_contrast(rep, client):
    """同一条航线、同一天，小船与大船的判定不应更宽松；不确定必须等同于小船。"""
    print(f"\n  【7】船型对照：同航线不同船型")
    out = {}
    for v in ("small", "large", "unknown"):
        resp, _ = call(client, origin="烟台", destination="大连", date=_today(),
                       transport="ship", vessel_type=v)
        if resp.status_code != 200:
            rep.check(False, f"船型 {v} HTTP 200", f"status={resp.status_code}")
            return
        out[v] = resp.json()
    order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "UNKNOWN": 0}
    rep.check(order[out["small"]["risk_level"]] >= order[out["large"]["risk_level"]],
              "小船的判定不低于大船（禁航线 6 级 vs 7 级）",
              f"small={out['small']['risk_level']} large={out['large']['risk_level']}")
    rep.check(out["unknown"]["risk_level"] == out["small"]["risk_level"],
              "不确定 == 按小船的严标准判",
              f"unknown={out['unknown']['risk_level']} small={out['small']['risk_level']}")
    rep.check(out["unknown"]["evidence"]["vessel_type"] == "unknown",
              "evidence 里如实记录用户选的是「不确定」")


def scenario_llm_timeout(rep):
    """模型太慢时必须降级到规则引擎，而不是让请求顶穿 Vercel 的 60s 上限吃 504。"""
    print(f"\n  【9】故障注入：LLM 超时")
    saved = os.environ.get("VOYAGEGUARD_LLM_TIMEOUT")
    os.environ["VOYAGEGUARD_LLM_TIMEOUT"] = "2"
    try:
        import providers, agent as agent_mod
        importlib.reload(providers); importlib.reload(agent_mod)
        import app as app_module
        importlib.reload(app_module)
        with TestClient(app_module.app) as c:
            resp, dt = call(c, origin="上海", destination="舟山", date=_today(),
                            transport="ship", vessel_type="small")
        rep.check(resp.status_code == 200, "模型超时不返回 5xx", f"status={resp.status_code}")
        if resp.status_code == 200:
            d = resp.json()
            rep.check(d.get("decision_source") == "rule_only",
                      "超时后规则引擎接管", f"src={d.get('decision_source')}")
            rep.check(d["risk_level"] in VALID_LEVELS, "仍给出合法风险等级",
                      f"got={d['risk_level']}")
            rep.check(any(not s.get("ok") for s in d.get("trace", [])),
                      "超时如实进 trace，没有被吞掉")
            rep.check(dt < 15, "超时后快速返回而不是继续等", f"{dt:.1f}s")
    finally:
        if saved is None:
            os.environ.pop("VOYAGEGUARD_LLM_TIMEOUT", None)
        else:
            os.environ["VOYAGEGUARD_LLM_TIMEOUT"] = saved
        import providers, agent as agent_mod
        importlib.reload(providers); importlib.reload(agent_mod)


def scenario_reachability(rep, client):
    """前提不成立的查询必须弃权，不能给出"建议出行"。"""
    print(f"\n  【8】可达性：前提不成立的查询")

    resp, _ = call(c := client, origin="上海", destination="南极", date=_today(), transport="plane")
    if rep.check(resp.status_code == 200, "HTTP 200", f"status={resp.status_code}"):
        d = resp.json()
        rep.check(d["risk_level"] == "UNKNOWN", "上海→南极（飞机）弃权而非给出低风险",
                  f"got={d['risk_level']}")
        rep.check(d.get("is_go_recommended") is None, "不出现「建议出行」",
                  f"go={d.get('is_go_recommended')}")
        codes = {m["code"] for m in d["evidence"]["sufficiency"]["missing"]}
        rep.check("no_airport" in codes, "缺失原因为「不在机场清单内」", f"codes={sorted(codes)}")

    resp, _ = call(client, origin="舟山", destination="嵊泗", date=_today(), transport="plane")
    if resp.status_code == 200:
        d = resp.json()
        rep.check(d["risk_level"] == "UNKNOWN", "舟山→嵊泗（飞机，嵊泗无机场）弃权",
                  f"got={d['risk_level']}")

    resp, _ = call(client, origin="杭州", destination="南京", date=_today(),
                   transport="ship", vessel_type="unknown")
    if resp.status_code == 200:
        d = resp.json()
        codes = {m["code"] for m in d["evidence"]["sufficiency"]["missing"]}
        rep.check("not_coastal" in codes, "杭州→南京（船）判为「不临海」而非解析失败",
                  f"codes={sorted(codes)}")
        txt = " ".join(d.get("missing_evidence") or [])
        rep.check("内河" in txt and "**" not in txt, "文案明确告知内河航线不在覆盖范围内且无 markdown 残留", txt[:70])


def scenario_fault_injection(rep):
    """把海洋 API 指向不可达主机：必须弃权，不能崩、更不能静默给 LOW。"""
    print(f"\n  【5】故障注入：海洋数据源不可达")
    saved = os.environ.get("VOYAGEGUARD_MARINE_BASE")
    os.environ["VOYAGEGUARD_MARINE_BASE"] = "https://127.0.0.1:9/marine"
    os.environ["VOYAGEGUARD_HTTP_TIMEOUT"] = "2"
    os.environ["VOYAGEGUARD_HTTP_RETRIES"] = "0"
    try:
        importlib.reload(evidence)
        import app as app_module
        importlib.reload(app_module)
        with TestClient(app_module.app) as c:
            resp, _ = call(c, origin="上海", destination="舟山", date=_today(),
                           transport="ship", vessel_type="small")
        rep.check(resp.status_code == 200, "数据源挂掉时不返回 5xx", f"status={resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            rep.check(data["risk_level"] == "UNKNOWN",
                      "判定为 UNKNOWN 而不是静默给出低风险", f"got={data['risk_level']}")
            codes = {m["code"] for m in data["evidence"]["sufficiency"]["missing"]}
            rep.check("wave_height_missing" in codes, "缺失原因为浪高不可得",
                      f"codes={sorted(codes)}")
    finally:
        if saved is None:
            os.environ.pop("VOYAGEGUARD_MARINE_BASE", None)
        else:
            os.environ["VOYAGEGUARD_MARINE_BASE"] = saved
        os.environ.pop("VOYAGEGUARD_HTTP_TIMEOUT", None)
        os.environ.pop("VOYAGEGUARD_HTTP_RETRIES", None)
        importlib.reload(evidence)


def scenario_llm_unavailable(rep):
    """模型完全不可用时：规则引擎独立出结论，不崩、不静默给低风险、失败如实进 trace。"""
    print(f"\n  【6】故障注入：LLM 不可用")
    import providers
    saved = {p: os.environ.get(providers.PROVIDERS[p].env_var) for p in providers.PROVIDERS}
    for p in providers.PROVIDERS:
        os.environ[providers.PROVIDERS[p].env_var] = "invalid-key-for-test"
    try:
        importlib.reload(providers)
        import agent, rules as rules_mod, app as app_module
        importlib.reload(agent); importlib.reload(rules_mod); importlib.reload(app_module)
        with TestClient(app_module.app) as c:
            resp, _ = call(c, origin="青岛", destination="长岛", date=_today(),
                           transport="ship", vessel_type="small")
        rep.check(resp.status_code == 200, "模型挂掉时不返回 5xx", f"status={resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            rep.check(data.get("decision_source") == "rule_only",
                      "规则引擎接管并独立出结论", f"src={data.get('decision_source')}")
            rep.check(data.get("risk_level") in VALID_LEVELS,
                      "仍给出合法风险等级", f"got={data.get('risk_level')}")
            failed = [s for s in data.get("trace", []) if not s.get("ok")]
            rep.check(bool(failed), "模型调用失败如实进 trace，没有被吞掉",
                      f"{[s['name'] for s in failed]}")
    finally:
        for p, v in saved.items():
            if v is None:
                os.environ.pop(providers.PROVIDERS[p].env_var, None)
            else:
                os.environ[providers.PROVIDERS[p].env_var] = v
        importlib.reload(providers)


def main():
    import providers
    print("=" * 88)
    print("  L2 · 集成层（真实网络 + 真实模型调用）")
    print("=" * 88)
    if not providers.available_providers():
        print(f"  {YELLOW}!{RESET} 没有配置任何 LLM API Key —— 需要 LLM 的用例会走 rule_only 路径。")

    rep = Report()
    import app as app_module
    with TestClient(app_module.app) as client:
        scenario_marine_route(rep, client)
        scenario_inland_as_ship(rep, client)
        scenario_out_of_horizon(rep, client)
        scenario_air_route(rep, client)
        scenario_vessel_contrast(rep, client)
        scenario_reachability(rep, client)
    scenario_fault_injection(rep)
    scenario_llm_unavailable(rep)
    scenario_llm_timeout(rep)

    total = rep.passed + len(rep.failed)
    print("\n" + "=" * 88)
    if rep.failed:
        print(f"  {RED}FAILED{RESET}  {rep.passed}/{total} 通过，{len(rep.failed)} 条失败：")
        for f in rep.failed:
            print(f"    · {f}")
    else:
        print(f"  {GREEN}PASSED{RESET}  {rep.passed}/{total} 全部通过")
    print("=" * 88)
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
