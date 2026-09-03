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

# 航空侧抽样：清单内的机场（全量 38 个跑网络太慢，抽 8 个）
AIRPORT_CITIES = ["上海", "北京", "广州", "成都", "乌鲁木齐", "西安", "哈尔滨", "昆明"]

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
        resolved, miss = evidence.resolve_location(name, "marine")
        rep.check(resolved is None and miss is not None and miss.code == "not_coastal",
                  f"{name} 被正确判定为非海域",
                  miss.detail if miss else "意外解析成功——判别器失效")


def check_atmos(rep: Report) -> None:
    """航空侧：风速与能见度必须可得，且能按目标日期精确对齐。"""
    print(f"\n【3】官方机场气象可得性 · {len(AIRPORT_CITIES)} 个机场 × 2 天（TAF 覆盖范围内）")
    dates = _dates()

    def probe(city):
        icao = evidence.AIRPORTS_WX[city][0]
        out = []
        for d in dates[:2]:      # TAF 只覆盖约 30 小时，第 3 天本就该弃权
            atmos, err = evidence.fetch_airport_wx(icao, d)
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
                rep.check(atmos["source"].startswith("aviationweather"),
                          f"{city} {d} 用的是官方机场气象（METAR/TAF）",
                          f"source={atmos['source']}")
                rep.check(atmos["date"] == d, f"{city} {d} 日期对齐", f"返回 {atmos['date']}")


def check_out_of_range(rep: Report) -> None:
    """超出预报范围的日期必须被识别，而不是静默回落到别的天。"""
    print("\n【4】预报范围边界")
    beyond = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
    atmos, err = evidence.fetch_atmos("上海", beyond)
    rep.check(atmos is None and (err or "").startswith("date_out_of_range"),
              f"{beyond}（+7天）被识别为超出预报范围", (err or "")[:70])


def check_reachability_gates(rep: Report) -> None:
    """
    可达性契约：两条路径都必须能拦住"前提不成立"的查询。

    航空侧在加机场白名单之前是零校验的 —— 任何地名都能查到风速和能见度，
    所以"上海 → 南极 飞机"会返回"低风险，建议出行"。这一组断言就是钉死那个漏洞。
    """
    print("\n【6】可达性契约")
    today = _dates()[0]

    for name in ("南极", "珠穆朗玛峰", "嵊泗", "刘公岛", "撒哈拉"):
        resolved, miss = evidence.resolve_location(name, "aviation")
        rep.check(resolved is None and miss is not None and miss.code == "no_airport",
                  f"航空：{name} 不在机场清单内，被拦截",
                  miss.detail if miss else "意外放行 —— 会对不存在的航线给出结论")

    for name in ("上海", "北京", "西安", "乌鲁木齐"):
        resolved, _ = evidence.resolve_location(name, "aviation")
        rep.check(resolved is not None, f"航空：{name} 在机场清单内，正常放行")

    # 两份清单各管一件事：可达性（宽，149 个）决定"能不能评估"，
    # 官方气象清单（窄，38 个）决定"用哪个数据源"。
    # 拉萨有机场但不发布 METAR/TAF —— 应当放行并回落到城市地面天气，而不是弃权。
    resolved, _ = evidence.resolve_location("拉萨", "aviation")
    rep.check(resolved is not None and resolved.get("source") == "airport_city",
              "航空：有机场但无官方气象的城市 → 放行并回落，不弃权",
              f"source={resolved.get('source') if resolved else None}")
    resolved, _ = evidence.resolve_location("上海", "aviation")
    rep.check(resolved is not None and resolved.get("icao") == "ZSPD",
              "航空：有官方气象的机场 → 带上 ICAO 走 METAR/TAF")

    # 覆盖率本身就是产品质量：超出 TAF 范围要回落而不是弃权
    beyond = (datetime.date.today() + datetime.timedelta(days=2)).isoformat()
    a, err = evidence.fetch_aviation_wx("ZSPD", "上海", beyond)
    rep.check(a is not None and a.get("quality") == "city_surface",
              "航空：超出 TAF 范围时回落到城市地面天气并标明质量",
              f"quality={a.get('quality') if a else err}")

    b = evidence.build_evidence("上海", "南极", today, "plane", None)
    rep.check(not b.ok and any(m.code == "no_airport" for m in b.missing),
              "端到端：上海→南极（飞机）必须弃权而不是给出低风险",
              f"missing={[m.code for m in b.missing]}")

    b = evidence.build_evidence("杭州", "南京", today, "ship", "unknown")
    rep.check(not b.ok and any(m.code == "not_coastal" for m in b.missing),
              "端到端：杭州→南京（船）判为「不临海」而非「解析失败」",
              f"missing={[m.code for m in b.missing]}")


def check_route_sampling(rep: Report) -> None:
    """
    航路采样：点数随距离增长，且采样点必须真的落在海上。

    几何限制（实测）：中国海岸线是弯的，远距离两港之间的大圆直线会切进内陆——
    538 km 以内采样点全在海上，845 km 以上全部落到陆地。所以采样点要自过滤，
    并把覆盖情况如实透出，而不是拿陆地上的地面天气冒充航路气象。
    """
    print("\n【7】航路采样")
    today = _dates()[0]

    for a, b, min_pts in (("厦门", "金门", 1), ("烟台", "大连", 1), ("青岛", "上海", 2)):
        bd = evidence.build_evidence(a, b, today, "ship", "small")
        r = bd.route
        rep.check(r.get("sampled", 0) >= min_pts,
                  f"{a}→{b} 采样点数随距离增长", f"{r.get('distance_km')} km → {r.get('sampled')} 点")
        rep.check(r.get("on_water") == r.get("sampled"),
                  f"{a}→{b} 采样点全部落在海上",
                  f"on_water={r.get('on_water')}/{r.get('sampled')}")
        rep.check(any(e.get("role") == "midpoint" for e in bd.locations.values()),
                  f"{a}→{b} 航路采样点进入证据")

    bd = evidence.build_evidence("上海", "厦门", today, "ship", "small")
    r = bd.route
    rep.check(r.get("on_water") == 0 and "note" in r,
              "长航线大圆穿越陆地时，如实标注未覆盖航程中段（不假装采到了）",
              (r.get("note") or "")[:56])
    rep.check(bd.quality == "partial", "该情况下证据质量降级为 partial", f"quality={bd.quality}")
    rep.check(bd.ok, "但两端证据仍充分，不因此弃权", f"missing={[m.code for m in bd.missing]}")


def check_sufficiency_gate(rep: Report) -> None:
    """端到端充分性判定：白名单航线 ok=True，内陆当船走 ok=False。"""
    print("\n【5】充分性判定门")
    today = _dates()[0]

    b = evidence.build_evidence("上海", "舟山", today, "ship", "small")
    rep.check(b.ok, "白名单海运航线 上海→舟山 证据充分",
              f"missing={[m.code for m in b.missing]}")
    waves = [(e.get("marine") or {}).get("max_wave_height_m") for e in b.locations.values()]
    rep.check(all(w is not None for w in waves),
              "海运航线两端浪高均非空 ← 这条早存在就会抓到浪高 bug", f"waves={waves}")

    b2 = evidence.build_evidence("北京", "西安", today, "ship", "small")
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
    check_reachability_gates(rep)
    check_route_sampling(rep)

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
