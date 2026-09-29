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

    # 覆盖率本身就是产品质量：TAF 覆盖不到时必须回落而不是弃权。
    # 只断言"回落且标了质量"，不断言具体是哪一档 —— 那取决于 TAF 签发时刻。
    beyond = (datetime.date.today() + datetime.timedelta(days=2)).isoformat()
    a, err = evidence.fetch_aviation_wx("ZSPD", "上海", beyond)
    rep.check(a is not None and a.get("quality") in ("full", "mixed", "city_surface"),
              "航空：TAF 覆盖不到时回落而不是弃权，且质量标记合法",
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


def check_marine_boundary(rep: Report) -> None:
    """
    海域判据：判的应该是"这个城市能不能通海"，不是"市中心那个像素是不是海"。

    半径 50 km 是实测定的：可通海城市（杭州/嘉兴/台州）最近海域都在 50 km 内，
    内陆城市（南京/镇江/武汉/郑州，含长江沿线）都在 100 km 外，余量很大。
    """
    print("\n【8】海域判据的边界城市")
    for name in ("杭州", "嘉兴", "台州"):
        r, m = evidence.resolve_location(name, "marine")
        rep.check(r is not None, f"{name}（湾顶/河口）判为可通海",
                  f"采样点偏移 {r.get('offset_km')} km" if r else (m.detail if m else ""))
    for name in ("南京", "镇江", "武汉", "郑州"):
        r, m = evidence.resolve_location(name, "marine")
        rep.check(r is None and m and m.code == "not_coastal",
                  f"{name}（内陆/长江沿线）仍判为不可通海",
                  f"意外放行到 ({r['lat']},{r['lon']})" if r else "")


def check_geocode_name_guard(rep: Report) -> None:
    """
    Geocoding 是模糊匹配，返回的可能跟用户问的完全不是一个城市。

    实测：搜 "Xian" 返回 Xián(西班牙)/Xianning/咸阳/湘潭市/**珠海市**，里面没有西安。
    原实现取人口最多的 → 珠海市 → 临海 → 回验通过 → "Xian 坐船"拿珠海的海况
    答了"建议出行"，而正确答案是"西安不临海，弃权"。
    「取人口最多」只该用于在同名候选里挑一个，不能用于在毫不相干的候选里挑一个。

    这组断言钉死两件事：模糊匹配不许放行，且加了校验之后合法覆盖不许缩水。
    """
    print("\n【10】地名解析：模糊匹配不许冒充")
    for name in ("Xian", "Xi'an", "西安", "Beijing", "南京", "Wuhan"):
        r, m = evidence.resolve_location(name, "marine")
        rep.check(r is None and m and m.code == "not_coastal",
                  f"{name}（内陆）不被模糊匹配放行",
                  f"意外解析成 {r['matched_name']} ({r['lat']},{r['lon']})" if r else "")

    # 加校验不能把合法的罗马化输入也挡掉：英文名必须和中文名解析到同一坐标
    for en, zh in (("Hangzhou", "杭州"), ("Zhoushan", "舟山"), ("Shantou", "汕头")):
        a, _ = evidence.resolve_location(en, "marine")
        b, _ = evidence.resolve_location(zh, "marine")
        same = a and b and (a["lat"], a["lon"]) == (b["lat"], b["lon"])
        rep.check(bool(same), f"{en} 与 {zh} 解析到同一坐标（覆盖不因校验缩水）",
                  f"{a and (a['lat'], a['lon'])} vs {b and (b['lat'], b['lon'])}")

    # 拼音输入必须先查英文库：中文库里对得上拼音的多是罗马化的小地方
    # （实测 "Beijing" → 山西同名村，"Jiaxing" → 台湾，"Wuhan" → 杭州湾边的 "Wuhang"）
    for en, zh in (("Beijing", "北京"), ("Jiaxing", "嘉兴")):
        a, b = evidence._geocode(en), evidence._geocode(zh)
        near = a and b and evidence.great_circle_km(a[0], a[1], b[0], b[1]) < 5
        rep.check(bool(near), f"拼音 {en} 与 {zh} 落在同一城市（不被同名小地方冒充）",
                  f"{a and a[:2]} vs {b and b[:2]}")


def check_taf_coverage(rep: Report) -> None:
    """
    TAF 通常只覆盖约 30 小时，查"明天"时往往只覆盖 20/24 小时。
    拿这 20 小时的极值当整天结论 = 用局部数据冒充完整结论，
    而漏掉晚间大风是"该警告时不警告"方向的错。所以未覆盖时段必须补齐并标注。
    """
    print("\n【9】航空 TAF 覆盖与补齐")
    today = _dates()[0]
    icao = evidence.AIRPORTS_WX["上海"][0]

    a, _ = evidence.fetch_airport_wx(icao, today)
    rep.check(a is not None and a.get("taf_cov_hours") is not None,
              "官方数据带出 TAF 对该日的实际覆盖小时数",
              f"cov={a.get('taf_cov_hours') if a else None}h")

    b, _ = evidence.fetch_aviation_wx(icao, "上海", today)
    if b and (a or {}).get("taf_cov_hours", 24) < 23.5:
        rep.check(b.get("quality") == "mixed",
                  "TAF 未完整覆盖 → 用城市天气补齐并标为 mixed", f"quality={b.get('quality')}")
        rep.check(b.get("taf_cov_to") is not None,
                  "补齐后仍透出官方预报覆盖到几点（用户要对自己的航班时间）")
    else:
        rep.check(b is not None, "官方数据完整覆盖该日", f"quality={b.get('quality') if b else None}")

    # 【不能写死"后天一定是 city_surface"】—— TAF 有效期约 30 小时，
    # 签发时刻决定它能盖到第几天：签发晚的 TAF 尾巴会伸进"后天"的头几个小时。
    # 真正的不变量是【覆盖时长与质量标记必须一致】，与当前时刻无关。
    for offset in (0, 1, 2):
        d = (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()
        raw, _ = evidence.fetch_airport_wx(icao, d)
        cov = (raw or {}).get("taf_cov_hours") or 0
        got, _ = evidence.fetch_aviation_wx(icao, "上海", d)
        expect = "full" if cov >= 23.5 else ("mixed" if cov > 0 else "city_surface")
        rep.check(got is not None and got.get("quality") == expect,
                  f"+{offset} 天：TAF 覆盖 {cov}h → 质量标记应为 {expect}",
                  f"实际 {got.get('quality') if got else None}")
        rep.check(got is not None, f"+{offset} 天：无论覆盖多少都不弃权（回落而非拒答）")


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
    check_marine_boundary(rep)
    check_geocode_name_guard(rep)
    check_taf_coverage(rep)

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
