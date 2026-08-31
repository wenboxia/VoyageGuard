"""
L1 · 推理层 —— 工具全 mock，隔离网络与数据源，只测 LLM 在给定数据下的判断质量。

这一层的目的是【隔离】：不让数据源波动污染对模型推理能力的测量。
代价必须写清楚：隔离得越干净，越容易漏掉集成层的问题——本项目的浪高 bug
就是被这层的干净隔离藏了半年。所以它必须和 L2 / L3 一起用，不能单独存在。

两个准确率列：
  LLM      模型自己的判断（UNKNOWN 用例里 = 模型是否自己提出弃权）
  安全网后  经过 rules.evaluate() 之后的最终结论

运行：python -m evals.l1_reasoning [provider ...]
"""

import sys
import time

import agent
import providers
import rules
from evals.cases import CASES, NO_WARNING, bundle_for


def make_mock_executor(case):
    """补充证据工具的 mock。必需证据已经在 bundle 里，不经过工具。"""
    warning = case.get("warning", NO_WARNING)

    def executor(tool_name, args):
        if tool_name == "search_weather_warning":
            return warning
        if tool_name == "get_weather_forecast":
            # 第三地补查：L1 不提供，返回明确的"无数据"而不是假数据
            return '{"error": "L1 mock: 第三地数据不可用"}'
        return '{"error": "unknown tool"}'

    return executor


def eval_one(case, provider_key):
    bundle = bundle_for(case)
    msg = agent.build_user_message(
        case["origin"], case["destination"], bundle.target_date,
        case["transport"], case.get("vessel_type"), bundle, "zh")

    t0 = time.time()
    llm_output, trace, meta = agent.run_agent(
        msg, lang="zh", provider_key=provider_key,
        tool_executor=make_mock_executor(case))
    elapsed = time.time() - t0

    json_ok = llm_output is not None
    llm_level = (llm_output or {}).get("risk_level", "PARSE_FAIL")
    final = rules.evaluate(llm_output, bundle, lang="zh")
    return {
        "json_ok": json_ok,
        "llm_level": llm_level,
        "final_level": final.get("risk_level", ""),
        "decision_source": final.get("decision_source", ""),
        "elapsed": elapsed,
        "tool_calls": meta.get("tool_calls", 0),
    }


def run_for_provider(provider_key):
    print(f"\n{'─' * 96}")
    print(f"  模型: {providers.PROVIDERS[provider_key].label}  ({providers.model_id(provider_key)})")
    print(f"{'─' * 96}")
    print(f"  {'ID':>3}  {'期望':^8}  {'LLM':^9}  {'安全网后':^9}  {'JSON':^4}  {'工具':^4}  {'耗时':>6}  {'类型':^4}  描述")

    rows = []
    for case in CASES:
        r = eval_one(case, provider_key)
        acceptable = case["acceptable"]
        r["llm_ok"] = r["llm_level"] in acceptable
        r["final_ok"] = r["final_level"] in acceptable
        rows.append((case, r))

        kind = "弃权" if case["expected"] == "UNKNOWN" else ("灰色" if len(acceptable) > 1 else "硬判")
        override = " ←校正" if r["final_level"] != r["llm_level"] else ""
        print(f"  {case['id']:>3}  {case['expected']:^8}  "
              f"{'✓' if r['llm_ok'] else '✗'}{r['llm_level']:<8} "
              f"{'✓' if r['final_ok'] else '✗'}{r['final_level']:<8} "
              f"{'✓' if r['json_ok'] else '✗':^4}  {r['tool_calls']:^4}  "
              f"{r['elapsed']:>5.1f}s  {kind:^4}  {case['desc'][:38]}{override}")

    n = len(rows)
    unknown_rows = [(c, r) for c, r in rows if c["expected"] == "UNKNOWN"]
    return {
        "provider": provider_key,
        "llm_acc": sum(r["llm_ok"] for _, r in rows) / n * 100,
        "final_acc": sum(r["final_ok"] for _, r in rows) / n * 100,
        "json_rate": sum(r["json_ok"] for _, r in rows) / n * 100,
        "avg_time": sum(r["elapsed"] for _, r in rows) / n,
        "abstain_rate": (sum(r["llm_ok"] for _, r in unknown_rows) / len(unknown_rows) * 100
                         if unknown_rows else 0.0),
    }


def main(argv):
    wanted = [a for a in argv if a in providers.PROVIDERS] or providers.available_providers()
    active = [p for p in wanted if providers.has_key(p)]

    print("=" * 96)
    print(f"  L1 · 推理层  |  用例数: {len(CASES)}  |  工具全 mock，隔离网络")
    print("=" * 96)
    for p in providers.PROVIDERS:
        if p not in active:
            print(f"  ⚠  跳过 {p}（{providers.PROVIDERS[p].env_var} 未配置）")
    if not active:
        print("\n  没有可用的 provider，退出。请配置至少一个 API Key。")
        return 1

    summary = [run_for_provider(p) for p in active]

    print(f"\n{'=' * 96}")
    print("  汇总")
    print(f"{'=' * 96}")
    print(f"  {'模型':<22}  {'LLM准确率':^11}  {'安全网后':^10}  {'提升':^8}  "
          f"{'主动弃权率':^11}  {'JSON合规':^9}  {'平均耗时':^9}")
    for s in summary:
        d = s["final_acc"] - s["llm_acc"]
        print(f"  {providers.PROVIDERS[s['provider']].label:<22}  {s['llm_acc']:>8.1f} %  "
              f"{s['final_acc']:>7.1f} %  {('+' if d >= 0 else '') + f'{d:.1f}%':>8}  "
              f"{s['abstain_rate']:>8.1f} %  {s['json_rate']:>6.1f} %  {s['avg_time']:>6.1f} s")
    print("=" * 96)
    print("  主动弃权率 = 证据有洞的 6 条用例里，模型自己提出 UNKNOWN 的比例。")
    print("  安全网后那一列不受它影响 —— 弃权判定由规则引擎的确定性门保证。")
    print("=" * 96 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
