"""
L3 · 数据充分性层 —— 确定性断言，不调 LLM，不需要任何 API Key。

测的是【该有的数据到底拿到没有】，不是【按什么顺序调工具】。
Anthropic 评测指南警告过：去评具体的工具调用顺序太僵硬、测试会很脆弱，
agent 常会找到设计者没预料到的有效路径。所以这里只断言产出物与关键能力。

这一层如果早就存在，就会在第一次运行时抓到"船舶浪高恒为 None"。

运行：python -m evals.l3_sufficiency
"""

import concurrent.futures as cf
import datetime
import sys

import evidence

# 航空侧抽样（wttr.in 按地名查即可，不需要在港口白名单里）
AIRPORT_CITIES = ["上海", "北京", "广州", "成都", "乌鲁木齐", "拉萨", "哈尔滨", "昆明"]

# 内陆对照点：必须取不到浪高，否则"是不是海域"这个判别器就没有判别力
INLAND_CONTROLS = ["北京", "西安", "乌鲁木齐", "成都"]

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  {GREEN}✓{RESET} {label} {DIM}{detail}{RESET}")
        else:
            self.failed.append(label)
            print(f"  {RED}✗{RESET} {label} {DIM}{detail}{RESET}")
        return ok


def _dates() -> list[str]:
    today = datetime.date.today()
    return [(today + datetime.timedelta(days=i)).isoformat() for i in range(evidence.FORECAST_HORIZON_DAYS)]


def check_ports(rep: Report) -> None:
    """白名单里每一个港口，都必须能在预报范围内取到有效浪高。"""
    print(f"\n【1】港口白名单浪高可得性 · {len(evidence.PORTS)} 个港口")
    today = _dates()[0]

    def probe(item):
        name, (lat, lon) = item
        marine, err = evidence.fetch_marine(lat, lon, today)
        return name, marine, err

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for name, marine, err in ex.map(probe, evidence.PORTS.items()):
            rep.check(marine is not None, f"{name} 可取到有效浪高",
                      f"max_wave={marine['max_wave_height_m']} m" if marine else (err or ""))


def check_inland_controls(rep: Report) -> None:
    """内陆对照点必须取不到浪高——确认海域判别器真的有判别力。"""
    print("\n【2】内陆对照点（应当取不到浪高）")
    for name in INLAND_CONTROLS:
        resolved, miss = evidence.resolve_location(name, require_marine=True)
        rep.check(resolved is None and miss is not None,
                  f"{name} 被正确判定为非海域",
                  miss.detail if miss else "意外解析成功——判别器失效")


def check_atmos(rep: Report) -> None:
    """航空侧：风速与能见度必须可得，且能按目标日期精确对齐。"""
    print(f"\n【3】大气数据可得性与日期对齐 · {len(AIRPORT_CITIES)} 个城市 × {len(_dates())} 天")
    dates = _dates()

    def probe(city):
        out = []
        for d in dates:
            atmos, err = evidence.fetch_atmos(city, d)
            out.append((d, atmos, err))
        return city, out

    with cf.ThreadPoolExecutor(max_workers=5) as ex:
        for city, rows in ex.map(probe, AIRPORT_CITIES):
            for d, atmos, err in rows:
                if atmos is None:
                    rep.check(False, f"{city} {d} 取到大气数据", err or "")
                    continue
                ok = atmos["max_wind_speed_ms"] is not None and atmos["min_visibility_km"] is not None
                rep.check(ok, f"{city} {d} 风速+能见度齐备",
                          f"wind={atmos['max_wind_speed_ms']} vis={atmos['min_visibility_km']}")
                rep.check(atmos["date"] == d, f"{city} {d} 日期对齐", f"返回 {atmos['date']}")


def check_out_of_range(rep: Report) -> None:
    """超出预报范围的日期必须被识别，而不是静默回落到别的天。"""
    print("\n【4】预报范围边界")
    beyond = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
    atmos, err = evidence.fetch_atmos("上海", beyond)
    rep.check(atmos is None and (err or "").startswith("date_out_of_range"),
              f"{beyond}（+7天）被识别为超出预报范围", (err or "")[:70])


def check_sufficiency_gate(rep: Report) -> None:
    """端到端充分性判定：白名单航线 ok=True，内陆当船走 ok=False。"""
    print("\n【5】充分性判定门")
    today = _dates()[0]

    b = evidence.build_evidence("上海", "舟山", today, "ship", "coastal")
    rep.check(b.ok, "白名单海运航线 上海→舟山 证据充分",
              f"missing={[m.code for m in b.missing]}")
    waves = [(e.get("marine") or {}).get("max_wave_height_m") for e in b.locations.values()]
    rep.check(all(w is not None for w in waves),
              "海运航线两端浪高均非空 ← 这条早存在就会抓到浪高 bug", f"waves={waves}")

    b2 = evidence.build_evidence("北京", "西安", today, "ship", "coastal")
    rep.check(not b2.ok, "内陆航线当船走 → 证据不足",
              f"missing={[m.code for m in b2.missing]}")

    b3 = evidence.build_evidence("杭州", "南京", today, "plane", None)
    rep.check(b3.ok, "航空航线 杭州→南京 证据充分",
              f"missing={[m.code for m in b3.missing]}")


def main() -> int:
    print("=" * 72)
    print("  L3 · 数据充分性层（确定性断言，零 token）")
    print("=" * 72)
    rep = Report()
    check_ports(rep)
    check_inland_controls(rep)
    check_atmos(rep)
    check_out_of_range(rep)
    check_sufficiency_gate(rep)

    total = rep.passed + len(rep.failed)
    print("\n" + "=" * 72)
    if rep.failed:
        print(f"  {RED}FAILED{RESET}  {rep.passed}/{total} 通过，{len(rep.failed)} 条失败：")
        for f in rep.failed:
            print(f"    · {f}")
    else:
        print(f"  {GREEN}PASSED{RESET}  {rep.passed}/{total} 全部通过")
    print("=" * 72)
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
