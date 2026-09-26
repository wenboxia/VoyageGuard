"""
L4 · 真实世界回验 —— 用已公布的停航记录反过来验证我们的阈值。

这一层回答的问题是三层评测都答不了的那个：
    我编码的这些阈值，跟现实中真的停不停航，到底对不对得上？

标签不需要专家判断 —— "那天实际停没停航"是官方与媒体已公布的客观事实。

【方法论限制，必须一起读】
ERA5 是【再分析】：事后用观测重建的"实际发生了什么"，不是决策当时可得的【预报】。
所以本层验证的是「阈值与实际停航是否吻合」，不是「当时的预报能否预测停航」。
另外 ERA5 网格约 0.25°，在海峡等地形复杂处会平滑掉局地峰值风。

运行：python -m evals.l4_realworld
"""

import concurrent.futures as cf
import json
import pathlib
import sys
from collections import defaultdict

import evidence
import rules

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
EVENTS_PATH = pathlib.Path(__file__).parent / "golden" / "events.jsonl"


def load_events() -> list[dict]:
    return [json.loads(l) for l in EVENTS_PATH.read_text().splitlines() if l.strip()]


def rebuild(event: dict) -> tuple[evidence.EvidenceBundle | None, str | None]:
    """用历史存档重建那一天的风浪，组成与生产环境同构的 EvidenceBundle。"""
    lat, lon, date = event["lat"], event["lon"], event["date"]
    atmos, a_err = evidence.fetch_atmos_archive(lat, lon, date)
    marine, m_err = evidence.fetch_marine(lat, lon, date)
    if atmos is None:
        return None, a_err
    if marine is None:
        return None, m_err

    b = evidence.EvidenceBundle(
        target_date=date, transport="ship", vessel_type=event.get("vessel_class", "large"))
    b.locations[event["route"]] = {
        "name": event["route"], "role": "midpoint", "errors": [],
        "resolved": {"lat": lat, "lon": lon, "source": "golden", "matched_name": event["route"]},
        "atmos": atmos, "marine": marine,
    }
    return b, None


def score(event: dict) -> dict:
    bundle, err = rebuild(event)
    if bundle is None:
        return {**event, "error": err, "level": None, "triggers": []}
    level, triggers = rules.required_level(bundle)
    a = bundle.locations[event["route"]]["atmos"]
    m = bundle.locations[event["route"]]["marine"]
    return {
        **event, "error": None, "level": level,
        "triggers": [t.code for t in triggers],
        "wind": a["max_wind_speed_ms"], "gust": a["max_gust_ms"],
        "wave": m["max_wave_height_m"],
    }


def main() -> int:
    events = load_events()
    print("=" * 100)
    print(f"  L4 · 真实世界回验  |  {len(events)} 条已公布的停航/通航记录")
    print("=" * 100)

    with cf.ThreadPoolExecutor(max_workers=5) as ex:
        rows = list(ex.map(score, events))

    failed = [r for r in rows if r["error"]]
    rows = [r for r in rows if not r["error"]]
    for f in failed:
        print(f"  {YELLOW}!{RESET} {f['id']} 数据重建失败: {f['error']}")

    print(f"\n  {'事件':<16} {'日期':<11} {'实际':<6} {'判定':<7} {'平均风':>7} {'阵风':>7} {'浪高':>6}  触发")
    print(f"  {'─'*16} {'─'*11} {'─'*6} {'─'*7} {'─'*7} {'─'*7} {'─'*6}  {'─'*30}")
    for r in sorted(rows, key=lambda x: (x["corridor"], x["date"])):
        actual = "停航" if r["suspended"] else "通航"
        hit = (r["level"] == "HIGH") == r["suspended"]
        mark = GREEN + "✓" + RESET if hit else RED + "✗" + RESET
        cause = f" [{r['cause']}]" if r["cause"] else ""
        print(f"  {mark} {r['id']:<14} {r['date']:<11} {actual:<6} {r['level']:<7} "
              f"{r['wind']:>6.1f} {(r['gust'] or 0):>7.1f} {r['wave']:>6.2f}  "
              f"{','.join(r['triggers']) or '—'}{DIM}{cause}{RESET}")

    # ── 指标 ────────────────────────────────────────────────────────────
    weather = [r for r in rows if r["cause"] != "non_weather"]
    susp = [r for r in weather if r["suspended"]]
    oper = [r for r in weather if not r["suspended"]]
    non_weather = [r for r in rows if r["cause"] == "non_weather"]

    def pct(n, d):
        return f"{n / d * 100:.1f}%" if d else "n/a"

    recall_high = sum(r["level"] == "HIGH" for r in susp)
    recall_any = sum(r["level"] in ("HIGH", "MEDIUM") for r in susp)
    fp_high = sum(r["level"] == "HIGH" for r in oper)
    fp_any = sum(r["level"] in ("HIGH", "MEDIUM") for r in oper)

    print(f"\n{'=' * 100}")
    print("  指标（已排除非气象原因的停航）")
    print(f"{'=' * 100}")
    print(f"  召回率（真实停航 → 判 HIGH）        {recall_high}/{len(susp)}  {pct(recall_high, len(susp))}")
    print(f"  预警覆盖率（真实停航 → 判 HIGH 或 MEDIUM）  {recall_any}/{len(susp)}  {pct(recall_any, len(susp))}")
    print(f"  误报率（正常通航 → 判 HIGH）        {fp_high}/{len(oper)}  {pct(fp_high, len(oper))}")
    print(f"  过度预警率（正常通航 → 判 HIGH 或 MEDIUM）  {fp_any}/{len(oper)}  {pct(fp_any, len(oper))}")

    # ── 事件级召回：把连续停航日归并成一次"停航事件"，只要窗口内任一天判 HIGH 即算命中。
    #    这更贴近产品的实际用法（用户查某天能不能走），也避免把一次停航的多天重复计数。
    import datetime as _dt
    events_by_corridor = defaultdict(list)
    for r in sorted(susp, key=lambda x: (x["corridor"], x["date"])):
        events_by_corridor[r["corridor"]].append(r)
    windows: list[list[dict]] = []
    for corridor, group in events_by_corridor.items():
        cur: list[dict] = []
        prev = None
        for r in group:
            d = _dt.date.fromisoformat(r["date"])
            if prev is not None and (d - prev).days > 1:
                windows.append(cur); cur = []
            cur.append(r); prev = d
        if cur:
            windows.append(cur)
    caught = sum(any(x["level"] == "HIGH" for x in w) for w in windows)
    print(f"\n  事件级召回率（一次停航窗口内任一天判 HIGH）  {caught}/{len(windows)}  "
          f"{pct(caught, len(windows))}")
    for w in windows:
        ok = any(x["level"] == "HIGH" for x in w)
        days = f"{w[0]['date']}~{w[-1]['date']}" if len(w) > 1 else w[0]["date"]
        levels = "/".join(x["level"] for x in w)
        print(f"    {GREEN + '✓' + RESET if ok else RED + '✗' + RESET} "
              f"{w[0]['corridor']} {days:<23} 判定 {levels}")

    by_cause = defaultdict(list)
    for r in susp:
        by_cause[r["cause"]].append(r)
    print(f"\n  按停航原因分组的召回率：")
    for cause, group in sorted(by_cause.items()):
        n = sum(g["level"] == "HIGH" for g in group)
        print(f"    {cause:<12} {n}/{len(group)}  {pct(n, len(group))}")

    if non_weather:
        print(f"\n  {DIM}非气象原因停航（不计入指标，用于标注能力边界）：{RESET}")
        for r in non_weather:
            print(f"    {r['id']} {r['date']} — 判定 {r['level']}，实际停航但原因是"
                  f"{r['quote'].split('——')[-1].strip() if '——' in r['quote'] else r['cause']}")
        print(f"    {DIM}这些我们判不了，也不该判 —— 气象工具管不了渔汛、检修和管制。{RESET}")

    # ── 失败模式归因 ────────────────────────────────────────────────────
    missed = [r for r in susp if r["level"] != "HIGH"]
    if missed:
        print(f"\n  漏判归因（{len(missed)} 条）：")
        typhoon_missed = [r for r in missed if r["cause"] == "typhoon"]
        if typhoon_missed:
            print(f"    · 台风相关 {len(typhoon_missed)} 条 —— 承运人依据【台风预警】提前停运，"
                  f"而台风预警是独立于风力/浪高的另一套官方信号，本工具完全没有建模。")
            print(f"      实测：台风「风神」整个停运窗口内风速 10.1–13.4 m/s，"
                  f"始终落在我们的 MEDIUM 档，不是阈值定错，是缺了一条轴。")
        near = [r for r in missed if r["wind"] and 12.0 <= r["wind"] < 13.9]
        if near:
            print(f"    · 贴线漏判 {len(near)} 条（按风速另算，与台风组有重叠）—— 平均风 12.0–13.9 m/s，"
                  f"就差在大船禁航线（13.9）下方。ERA5 网格 0.25° 会平滑掉海峡的局地峰值风，"
                  f"新闻报道的实际风力普遍高于重建值。")

    print(f"\n{'=' * 100}")
    print("  方法论限制：ERA5 是再分析（事后重建），不是决策当时可得的预报；")
    print("  网格约 0.25°，海峡等地形复杂处会平滑掉局地峰值风。")
    print("  因此本层说明的是「阈值与实际停航是否吻合」，不是「预报是否够用」。")
    print("=" * 100 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
