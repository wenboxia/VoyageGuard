"""
evals/cases.py — L1 标注用例集

与旧版最重要的差别：浪高现在是结构化字段 marine.max_wave_height_m，
不再是塞在天气描述文本里的中文串。旧版 mock 把 "有效浪高 3.0 米" 写进 description，
恰好喂给了那个从文本抠数值的正则，于是 20 条涉浪用例全过——而生产环境
根本拿不到浪高。schema 一改，那条作弊路径就不存在了。

新增：
  · vessel_type（coastal / ropax）分档，解决旧用例 13 的期望与规则冲突
  · UNKNOWN 用例：证据有洞时，规则门该弃权、模型也该弃权
"""

import datetime

from evidence import EvidenceBundle, Missing, ms_to_beaufort

TODAY = datetime.date.today().isoformat()


def loc(wind, vis, desc, wave=None, gust=None):
    """一个地点的证据。wave 为 None 表示没有海洋数据（航空场景正常，海事场景=证据缺失）。"""
    return {"wind": wind, "vis": vis, "desc": desc, "wave": wave, "gust": gust}


SAFE = loc(3.0, 10.0, "晴", wave=0.4)
SAFE_AIR = loc(3.0, 10.0, "晴")


def make_bundle(transport, vessel_type, locations, target_date=TODAY,
                drop=()) -> EvidenceBundle:
    """
    把用例里的地点字典变成 EvidenceBundle。
    drop 用来人为制造证据缺口，形如 (("destination", "wave"), ("origin", "vis"))。
    """
    b = EvidenceBundle(target_date=target_date, transport=transport, vessel_type=vessel_type)
    roles = ("origin", "destination")
    dropped = dict(drop)

    for role, (name, spec) in zip(roles, locations.items()):
        entry = {"name": name, "role": role, "errors": []}
        entry["resolved"] = {"lat": 0.0, "lon": 0.0, "source": "whitelist", "matched_name": name}

        drop_field = dropped.get(role)
        wind = spec["wind"]
        vis = spec["vis"]

        if drop_field == "atmos":
            b.missing.append(Missing("atmos_unavailable", role, name, "mock: 数据源不可用"))
            b.locations[name] = entry
            continue
        if drop_field == "date":
            b.missing.append(Missing("date_out_of_range", role, name, "mock: 超出预报范围"))
            b.locations[name] = entry
            continue
        if drop_field == "vis":
            vis = None
        if drop_field == "wind":
            wind = None

        entry["atmos"] = {
            "date": target_date,
            "max_wind_speed_ms": wind,
            "max_wind_beaufort": ms_to_beaufort(wind) if wind is not None else None,
            "max_gust_ms": spec.get("gust"),
            "min_visibility_km": vis,
            "max_temp_c": "25", "min_temp_c": "18",
            "description": spec["desc"],
            "quality": "full", "source": "mock", "fetched_at": target_date + "T00:00:00+00:00",
        }
        if wind is None:
            b.missing.append(Missing("wind_missing", role, name, "mock: 未取到风速"))
        if transport == "plane" and vis is None:
            b.missing.append(Missing("visibility_missing", role, name, "mock: 未取到能见度"))

        if transport == "ship":
            wave = None if drop_field == "wave" else spec.get("wave")
            if wave is None:
                b.missing.append(Missing("wave_height_missing", role, name, "mock: 未取到浪高"))
            else:
                entry["marine"] = {
                    "date": target_date, "max_wave_height_m": wave,
                    "mean_wave_height_m": wave, "max_wave_period_s": 6.0,
                    "max_swell_height_m": wave, "source": "mock",
                    "fetched_at": target_date + "T00:00:00+00:00",
                }
        b.locations[name] = entry

    return b


NO_WARNING = '{"message": "当前无相关气象预警信息"}'
HAS_WARNING = ('{"results": [{"title": "大风蓝色预警", '
               '"summary": "预计未来24小时平均风力达6级以上", "url": "http://example.com"}]}')


# ---------------------------------------------------------------------------
# 用例集
# ---------------------------------------------------------------------------
# expected / acceptable_levels：灰色地带用例给多个可接受值，避免把 prompt 的
# 固有模糊性误计为模型错误。
CASES = [
    # ── 航空 HIGH ─────────────────────────────────────────────────────────
    dict(id=1, desc="[航空-HIGH] 出发地风速 20 m/s（超 15 m/s 红线）",
         origin="上海", destination="北京", transport="plane",
         expected="HIGH", acceptable=["HIGH"], warning=HAS_WARNING,
         locations={"上海": loc(20.0, 8.0, "大风"), "北京": SAFE_AIR}),
    dict(id=2, desc="[航空-HIGH] 目的地能见度 0.2 km（低于 0.4 km 红线）",
         origin="上海", destination="重庆", transport="plane",
         expected="HIGH", acceptable=["HIGH"],
         locations={"上海": SAFE_AIR, "重庆": loc(4.0, 0.2, "浓雾")}),
    dict(id=3, desc="[航空-HIGH] 目的地风速 16 m/s（超红线）",
         origin="北京", destination="乌鲁木齐", transport="plane",
         expected="HIGH", acceptable=["HIGH"], warning=HAS_WARNING,
         locations={"北京": SAFE_AIR, "乌鲁木齐": loc(16.0, 6.0, "大风")}),
    # ── 航空 MEDIUM ───────────────────────────────────────────────────────
    dict(id=4, desc="[航空-MEDIUM] 目的地雷暴（风速能见度均达标）",
         origin="广州", destination="成都", transport="plane",
         expected="MEDIUM", acceptable=["MEDIUM"],
         locations={"广州": SAFE_AIR, "成都": loc(5.0, 8.0, "雷暴")}),
    dict(id=5, desc="[航空-灰色] 出发地雷暴 + 风速 12 m/s（接近红线未超）",
         origin="武汉", destination="西安", transport="plane",
         expected="MEDIUM", acceptable=["MEDIUM", "HIGH"], warning=HAS_WARNING,
         locations={"武汉": loc(12.0, 5.0, "雷暴"), "西安": SAFE_AIR}),
    # ── 航空 LOW ──────────────────────────────────────────────────────────
    dict(id=6, desc="[航空-LOW] 晴天，风速 3 m/s，能见度 10 km",
         origin="杭州", destination="南京", transport="plane",
         expected="LOW", acceptable=["LOW"],
         locations={"杭州": SAFE_AIR, "南京": SAFE_AIR}),
    dict(id=7, desc="[航空-LOW] 多云小雨，风速 8 m/s，能见度 5 km",
         origin="深圳", destination="厦门", transport="plane",
         expected="LOW", acceptable=["LOW"],
         locations={"深圳": loc(8.0, 5.0, "多云转小雨"), "厦门": loc(6.0, 6.0, "阴天")}),
    # ── 航空 边界 ─────────────────────────────────────────────────────────
    dict(id=8, desc="[航空-边界-HIGH] 风速恰好 15.0 m/s（≥ 即触发）",
         origin="兰州", destination="拉萨", transport="plane",
         expected="HIGH", acceptable=["HIGH"], warning=HAS_WARNING,
         locations={"兰州": SAFE_AIR, "拉萨": loc(15.0, 8.0, "大风")}),
    dict(id=9, desc="[航空-灰色] 风速 13 m/s（接近未超）+ 雷暴",
         origin="上海", destination="海口", transport="plane",
         expected="MEDIUM", acceptable=["MEDIUM", "HIGH"], warning=HAS_WARNING,
         locations={"上海": SAFE_AIR, "海口": loc(13.0, 4.0, "雷暴")}),

    # ── 海事 HIGH ─────────────────────────────────────────────────────────
    dict(id=10, desc="[海事-HIGH] 近海游船 风速 14 m/s（超 10.8 红线）",
         origin="上海", destination="舟山", transport="ship", vessel_type="coastal",
         expected="HIGH", acceptable=["HIGH"], warning=HAS_WARNING,
         locations={"上海": SAFE, "舟山": loc(14.0, 6.0, "大风", wave=2.2)}),
    dict(id=11, desc="[海事-HIGH] 客滚船 风速 22 m/s（超 17.2 红线）",
         origin="大连", destination="烟台", transport="ship", vessel_type="ropax",
         expected="HIGH", acceptable=["HIGH"], warning=HAS_WARNING,
         locations={"大连": SAFE, "烟台": loc(22.0, 4.0, "狂风", wave=4.5)}),
    dict(id=12, desc="[海事-HIGH] 客滚船 浪高 3.0 m（风速未触红线，纯浪高判定）",
         origin="广州", destination="三亚", transport="ship", vessel_type="ropax",
         expected="HIGH", acceptable=["HIGH"], warning=HAS_WARNING,
         locations={"广州": SAFE, "三亚": loc(12.0, 6.0, "大浪，涌浪发展", wave=3.0)}),
    # ── 海事 MEDIUM ───────────────────────────────────────────────────────
    dict(id=13, desc="[海事-MEDIUM] 客滚船 风速 11 m/s（≥10.8 但 <17.2）",
         origin="福州", destination="平潭", transport="ship", vessel_type="ropax",
         expected="MEDIUM", acceptable=["MEDIUM"],
         locations={"福州": SAFE, "平潭": loc(11.0, 8.0, "中浪", wave=2.0)}),
    dict(id=14, desc="[海事-MEDIUM] 近海游船 浪高 2.0 m（1.5-2.5 中风险区间）",
         origin="海口", destination="三亚", transport="ship", vessel_type="coastal",
         expected="MEDIUM", acceptable=["MEDIUM"],
         locations={"海口": SAFE, "三亚": loc(9.0, 8.0, "轻到中浪", wave=2.0)}),
    # ── 海事 LOW ──────────────────────────────────────────────────────────
    dict(id=15, desc="[海事-LOW] 风速 3 m/s，浪高 0.5 m，晴好",
         origin="厦门", destination="金门", transport="ship", vessel_type="coastal",
         expected="LOW", acceptable=["LOW"],
         locations={"厦门": SAFE, "金门": loc(3.0, 12.0, "晴，海面平静", wave=0.5)}),

    # ── 航空 临界三连 ─────────────────────────────────────────────────────
    dict(id=16, desc="[航空-边界-LOW] 风速 14.9 m/s（刚低于红线），无雷暴",
         origin="西宁", destination="成都", transport="plane",
         expected="LOW", acceptable=["LOW"], warning=HAS_WARNING,
         locations={"西宁": loc(14.9, 8.0, "大风"), "成都": SAFE_AIR}),
    dict(id=17, desc="[航空-边界-HIGH] 风速 15.1 m/s（刚超红线）",
         origin="呼和浩特", destination="北京", transport="plane",
         expected="HIGH", acceptable=["HIGH"], warning=HAS_WARNING,
         locations={"呼和浩特": loc(15.1, 8.0, "大风"), "北京": SAFE_AIR}),
    dict(id=18, desc="[航空-边界-HIGH] 能见度 0.39 km（刚低于 0.4 红线）",
         origin="南京", destination="合肥", transport="plane",
         expected="HIGH", acceptable=["HIGH"],
         locations={"南京": SAFE_AIR, "合肥": loc(4.0, 0.39, "浓雾")}),
    dict(id=19, desc="[航空-边界-LOW] 能见度 0.41 km（刚高于 0.4 红线）",
         origin="南京", destination="合肥", transport="plane",
         expected="LOW", acceptable=["LOW"],
         locations={"南京": SAFE_AIR, "合肥": loc(4.0, 0.41, "薄雾")}),
    dict(id=20, desc="[航空-HIGH] 雷暴 + 风速恰好 15 m/s",
         origin="郑州", destination="长沙", transport="plane",
         expected="HIGH", acceptable=["HIGH"], warning=HAS_WARNING,
         locations={"郑州": loc(15.0, 5.0, "雷暴"), "长沙": SAFE_AIR}),
    dict(id=21, desc="[航空-MEDIUM] 两地均雷暴，风速能见度均正常",
         origin="武汉", destination="南昌", transport="plane",
         expected="MEDIUM", acceptable=["MEDIUM"], warning=HAS_WARNING,
         locations={"武汉": loc(6.0, 6.0, "雷暴"), "南昌": loc(5.0, 7.0, "雷暴")}),
    dict(id=22, desc="[航空-LOW] 小雨多云，风速 10 m/s，能见度 3 km，无雷暴",
         origin="青岛", destination="济南", transport="plane",
         expected="LOW", acceptable=["LOW"],
         locations={"青岛": loc(10.0, 3.0, "小雨"), "济南": loc(7.0, 4.0, "多云")}),

    # ── 海事 浪高边界 ─────────────────────────────────────────────────────
    dict(id=23, desc="[海事-边界-MEDIUM] 浪高恰好 2.5 m（>2.5 才 HIGH）",
         origin="宁波", destination="舟山", transport="ship", vessel_type="coastal",
         expected="MEDIUM", acceptable=["MEDIUM"], warning=HAS_WARNING,
         locations={"宁波": SAFE, "舟山": loc(10.0, 6.0, "中浪", wave=2.5)}),
    dict(id=24, desc="[海事-边界-HIGH] 客滚船 浪高 2.6 m（刚超 2.5 红线）",
         origin="温州", destination="洞头", transport="ship", vessel_type="ropax",
         expected="HIGH", acceptable=["HIGH"], warning=HAS_WARNING,
         locations={"温州": SAFE, "洞头": loc(11.0, 5.0, "大浪", wave=2.6)}),
    dict(id=25, desc="[海事-边界-MEDIUM] 浪高 1.5 m（中风险下边界，含）",
         origin="烟台", destination="蓬莱", transport="ship", vessel_type="coastal",
         expected="MEDIUM", acceptable=["MEDIUM"],
         locations={"烟台": SAFE, "蓬莱": loc(7.0, 8.0, "轻浪", wave=1.5)}),
    dict(id=26, desc="[海事-边界-LOW] 浪高 1.4 m（低于 1.5 起点）",
         origin="威海", destination="刘公岛", transport="ship", vessel_type="coastal",
         expected="LOW", acceptable=["LOW"],
         locations={"威海": SAFE, "刘公岛": loc(6.0, 9.0, "微浪", wave=1.4)}),
    # ── 海事 风速边界 ─────────────────────────────────────────────────────
    dict(id=27, desc="[海事-边界-HIGH] 近海游船 风速恰好 10.8 m/s（≥ 即触发）",
         origin="青岛", destination="长岛", transport="ship", vessel_type="coastal",
         expected="HIGH", acceptable=["HIGH"], warning=HAS_WARNING,
         locations={"青岛": SAFE, "长岛": loc(10.8, 7.0, "大风，6级", wave=1.0)}),
    dict(id=28, desc="[海事-边界-MEDIUM] 近海游船 风速 10.5 m/s（刚低于 10.8）",
         origin="连云港", destination="朝连岛", transport="ship", vessel_type="coastal",
         expected="MEDIUM", acceptable=["MEDIUM"],
         locations={"连云港": SAFE, "朝连岛": loc(10.5, 7.0, "较大风浪，5级风", wave=1.2)}),
    dict(id=29, desc="[海事-MEDIUM] 风速 9 m/s + 浪高 2.0 m（双中风险叠加）",
         origin="舟山", destination="嵊泗", transport="ship", vessel_type="coastal",
         expected="MEDIUM", acceptable=["MEDIUM"],
         locations={"舟山": SAFE, "嵊泗": loc(9.0, 7.0, "中浪，风力 5 级", wave=2.0)}),
    dict(id=30, desc="[海事-LOW] 风速 7 m/s，浪高 1.2 m，晴好",
         origin="珠海", destination="外伶仃岛", transport="ship", vessel_type="coastal",
         expected="LOW", acceptable=["LOW"],
         locations={"珠海": SAFE, "外伶仃岛": loc(7.0, 12.0, "晴，微浪", wave=1.2)}),

    # ── 船型对照：同样的气象数据，不同船型应得出不同结论 ──────────────────
    dict(id=31, desc="[船型对照-HIGH] 风速 12 m/s + 近海游船 → 超 10.8 红线",
         origin="青岛", destination="长岛", transport="ship", vessel_type="coastal",
         expected="HIGH", acceptable=["HIGH"],
         locations={"青岛": SAFE, "长岛": loc(12.0, 7.0, "大风", wave=1.2)}),
    dict(id=32, desc="[船型对照-MEDIUM] 同样 12 m/s + 跨海客滚船 → 未超 17.2",
         origin="青岛", destination="长岛", transport="ship", vessel_type="ropax",
         expected="MEDIUM", acceptable=["MEDIUM"],
         locations={"青岛": SAFE, "长岛": loc(12.0, 7.0, "大风", wave=1.2)}),

    # ── UNKNOWN：证据不足时必须弃权，而不是静默按安全处理 ──────────────────
    dict(id=33, desc="[弃权] 海运但目的地拿不到浪高",
         origin="上海", destination="舟山", transport="ship", vessel_type="coastal",
         expected="UNKNOWN", acceptable=["UNKNOWN"], drop=(("destination", "wave"),),
         locations={"上海": SAFE, "舟山": loc(6.0, 8.0, "多云", wave=1.0)}),
    dict(id=34, desc="[弃权] 海运但两端都拿不到浪高（旧版生产环境的真实状态）",
         origin="厦门", destination="金门", transport="ship", vessel_type="coastal",
         expected="UNKNOWN", acceptable=["UNKNOWN"],
         drop=(("origin", "wave"), ("destination", "wave")),
         locations={"厦门": SAFE, "金门": loc(3.0, 12.0, "晴", wave=0.5)}),
    dict(id=35, desc="[弃权] 航空但目的地拿不到能见度（航空硬红线，不能默认安全）",
         origin="上海", destination="重庆", transport="plane",
         expected="UNKNOWN", acceptable=["UNKNOWN"], drop=(("destination", "vis"),),
         locations={"上海": SAFE_AIR, "重庆": loc(4.0, 8.0, "多云")}),
    dict(id=36, desc="[弃权] 出行日期超出预报范围",
         origin="杭州", destination="南京", transport="plane",
         expected="UNKNOWN", acceptable=["UNKNOWN"], drop=(("destination", "date"),),
         locations={"杭州": SAFE_AIR, "南京": SAFE_AIR}),
    dict(id=37, desc="[弃权] 出发地气象数据源整体不可用",
         origin="大连", destination="烟台", transport="ship", vessel_type="ropax",
         expected="UNKNOWN", acceptable=["UNKNOWN"], drop=(("origin", "atmos"),),
         locations={"大连": SAFE, "烟台": loc(8.0, 8.0, "多云", wave=1.0)}),
    dict(id=38, desc="[弃权] 目的地风速缺失",
         origin="青岛", destination="长岛", transport="ship", vessel_type="coastal",
         expected="UNKNOWN", acceptable=["UNKNOWN"], drop=(("destination", "wind"),),
         locations={"青岛": SAFE, "长岛": loc(9.0, 8.0, "多云", wave=1.0)}),
]


def bundle_for(case) -> EvidenceBundle:
    return make_bundle(
        case["transport"], case.get("vessel_type"),
        case["locations"], drop=case.get("drop", ()))
