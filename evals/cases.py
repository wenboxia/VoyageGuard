"""
evals/cases.py — L1 标注用例集（第二轮：只用官方判据后重标）

与上一版的差别：
  · 阈值全部换成有官方出处的（大风预警四色 / 海浪预警四色 / 两条禁航线 / LVTO）
  · 删掉了无来源的「浪高 1.5m 中风险」—— 多条海事用例因此从 MEDIUM 变 LOW
  · 修正了错误的大船 17.2 → 13.9（这条错在危险方向）
  · 船型从 coastal/ropax 改为 small/large/unknown，新增「不确定按小船判」用例
  · 新增阵风触发用例 —— 大风预警本就同时看平均风和阵风，之前完全没测
  · 新增两条 PROD 规则的用例（小船遇蓝色海浪预警、航空 15 m/s）
"""

import datetime

from evidence import EvidenceBundle, Missing, ms_to_beaufort

TODAY = datetime.date.today().isoformat()


def loc(wind, vis, desc, wave=None, gust=None):
    return {"wind": wind, "vis": vis, "desc": desc, "wave": wave, "gust": gust}


SAFE = loc(3.0, 10.0, "晴", wave=0.4, gust=5.0)
SAFE_AIR = loc(3.0, 10.0, "晴", gust=5.0)


def make_bundle(transport, vessel_type, locations, target_date=TODAY, drop=()) -> EvidenceBundle:
    b = EvidenceBundle(target_date=target_date, transport=transport, vessel_type=vessel_type)
    roles = ("origin", "destination")
    dropped = dict(drop)

    for role, (name, spec) in zip(roles, locations.items()):
        entry = {"name": name, "role": role, "errors": [],
                 "resolved": {"lat": 0.0, "lon": 0.0, "source": "whitelist", "matched_name": name}}
        drop_field = dropped.get(role)
        wind, vis = spec["wind"], spec["vis"]

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
            "date": target_date, "max_wind_speed_ms": wind,
            "max_wind_beaufort": ms_to_beaufort(wind) if wind is not None else None,
            "max_gust_ms": spec.get("gust"), "min_visibility_km": vis,
            "max_temp_c": "25", "min_temp_c": "18", "description": spec["desc"],
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
                entry["marine"] = {"date": target_date, "max_wave_height_m": wave,
                                   "mean_wave_height_m": wave, "max_wave_period_s": 6.0,
                                   "max_swell_height_m": wave, "source": "mock",
                                   "fetched_at": target_date + "T00:00:00+00:00"}
        b.locations[name] = entry
    return b


NO_WARNING = '{"message": "当前无相关气象预警信息"}'
HAS_WARNING = ('{"results": [{"title": "大风蓝色预警", '
               '"summary": "预计未来24小时平均风力达6级以上", "url": "http://example.com"}]}')


# ---------------------------------------------------------------------------
# 用例集
# ---------------------------------------------------------------------------
CASES = [
    # ══ 航空 · 大风预警四色 ═══════════════════════════════════════════════
    dict(id=1, desc="[航空] 平均风 10.7（未达6级）→ 无预警",
         origin="杭州", destination="南京", transport="plane",
         expected="LOW", acceptable=["LOW"],
         locations={"杭州": SAFE_AIR, "南京": loc(10.7, 8.0, "多云", gust=12.0)}),
    dict(id=2, desc="[航空-边界] 平均风 10.8（6级）→ 大风蓝色预警",
         origin="杭州", destination="南京", transport="plane",
         expected="MEDIUM", acceptable=["MEDIUM"],
         locations={"杭州": SAFE_AIR, "南京": loc(10.8, 8.0, "大风", gust=12.0)}),
    dict(id=3, desc="[航空-边界] 平均风 14.9（未达PROD侧风线15.0）",
         origin="兰州", destination="成都", transport="plane",
         expected="MEDIUM", acceptable=["MEDIUM"], warning=HAS_WARNING,
         locations={"兰州": loc(14.9, 8.0, "大风", gust=18.0), "成都": SAFE_AIR}),
    dict(id=4, desc="[航空-边界-PROD] 平均风 15.0 → 接近窄体机侧风限制",
         origin="兰州", destination="乌鲁木齐", transport="plane",
         expected="HIGH", acceptable=["HIGH"], warning=HAS_WARNING,
         locations={"兰州": SAFE_AIR, "乌鲁木齐": loc(15.0, 8.0, "大风", gust=18.0)}),
    dict(id=5, desc="[航空] 平均风 17.2（8级）→ 大风黄色预警",
         origin="上海", destination="北京", transport="plane",
         expected="HIGH", acceptable=["HIGH"], warning=HAS_WARNING,
         locations={"上海": loc(17.2, 8.0, "大风", gust=22.0), "北京": SAFE_AIR}),
    dict(id=6, desc="[航空-阵风] 平均风仅 9.0 但阵风 13.9 → 大风蓝色预警",
         origin="青岛", destination="济南", transport="plane",
         expected="MEDIUM", acceptable=["MEDIUM"],
         locations={"青岛": loc(9.0, 8.0, "多云", gust=13.9), "济南": SAFE_AIR}),
    dict(id=7, desc="[航空-阵风] 平均风 9.0 但阵风 20.8（9级）→ 大风黄色预警",
         origin="青岛", destination="济南", transport="plane",
         expected="HIGH", acceptable=["HIGH"],
         locations={"青岛": loc(9.0, 8.0, "大风", gust=20.8), "济南": SAFE_AIR}),

    # ══ 航空 · 能见度（LVTO 门槛，REG）═══════════════════════════════════
    dict(id=8, desc="[航空-边界] 能见度 0.39 km → 低于 LVTO 门槛",
         origin="南京", destination="合肥", transport="plane",
         expected="HIGH", acceptable=["HIGH"],
         locations={"南京": SAFE_AIR, "合肥": loc(4.0, 0.39, "浓雾", gust=6.0)}),
    dict(id=9, desc="[航空-边界] 能见度 0.41 km → 未低于 LVTO 门槛",
         origin="南京", destination="合肥", transport="plane",
         expected="LOW", acceptable=["LOW"],
         locations={"南京": SAFE_AIR, "合肥": loc(4.0, 0.41, "薄雾", gust=6.0)}),
    dict(id=10, desc="[航空] 能见度 0.2 km 浓雾",
         origin="上海", destination="重庆", transport="plane",
         expected="HIGH", acceptable=["HIGH"],
         locations={"上海": SAFE_AIR, "重庆": loc(4.0, 0.2, "浓雾", gust=6.0)}),

    # ══ 航空 · 雷暴（PROD，无法规阈值）═══════════════════════════════════
    dict(id=11, desc="[航空-PROD] 目的地雷暴，风与能见度均正常",
         origin="广州", destination="成都", transport="plane",
         expected="MEDIUM", acceptable=["MEDIUM"],
         locations={"广州": SAFE_AIR, "成都": loc(5.0, 8.0, "雷暴", gust=8.0)}),
    dict(id=12, desc="[航空] 雷暴 + 平均风 15.0（PROD侧风线）",
         origin="郑州", destination="长沙", transport="plane",
         expected="HIGH", acceptable=["HIGH"], warning=HAS_WARNING,
         locations={"郑州": loc(15.0, 5.0, "雷暴", gust=19.0), "长沙": SAFE_AIR}),
    dict(id=13, desc="[航空] 两地均雷暴，其余指标正常",
         origin="武汉", destination="长沙", transport="plane",
         expected="MEDIUM", acceptable=["MEDIUM"], warning=HAS_WARNING,
         locations={"武汉": loc(6.0, 6.0, "雷暴", gust=9.0), "长沙": loc(5.0, 7.0, "雷暴", gust=8.0)}),
    dict(id=14, desc="[航空] 小雨多云，风 8.0，能见度 5 km → 一条判据都没触发",
         origin="深圳", destination="厦门", transport="plane",
         expected="LOW", acceptable=["LOW"],
         locations={"深圳": loc(8.0, 5.0, "多云转小雨", gust=11.0),
                    "厦门": loc(6.0, 6.0, "阴天", gust=9.0)}),

    # ══ 海事 · 禁航线（REG）══════════════════════════════════════════════
    dict(id=15, desc="[海事-边界-REG] 小船 平均风 10.7（未达6级禁航线）",
         origin="青岛", destination="长岛", transport="ship", vessel_type="small",
         expected="LOW", acceptable=["LOW"],
         locations={"青岛": SAFE, "长岛": loc(10.7, 8.0, "大风", wave=1.0, gust=12.0)}),
    dict(id=16, desc="[海事-边界-REG] 小船 平均风 10.8 → 6级禁止出海",
         origin="青岛", destination="长岛", transport="ship", vessel_type="small",
         expected="HIGH", acceptable=["HIGH"], warning=HAS_WARNING,
         locations={"青岛": SAFE, "长岛": loc(10.8, 8.0, "大风", wave=1.0, gust=12.0)}),
    dict(id=17, desc="[海事-边界-REG] 大船 平均风 13.8（未达7级）",
         origin="烟台", destination="大连", transport="ship", vessel_type="large",
         expected="MEDIUM", acceptable=["MEDIUM"],
         locations={"烟台": SAFE, "大连": loc(13.8, 8.0, "大风", wave=1.0, gust=16.0)}),
    dict(id=18, desc="[海事-边界-REG] 大船 平均风 13.9 → 7级不得允许旅客上船",
         origin="烟台", destination="大连", transport="ship", vessel_type="large",
         expected="HIGH", acceptable=["HIGH"], warning=HAS_WARNING,
         locations={"烟台": SAFE, "大连": loc(13.9, 8.0, "大风", wave=1.0, gust=17.0)}),
    dict(id=19, desc="[海事] 大船 平均风 22.0（超7级禁航+大风黄色）",
         origin="大连", destination="烟台", transport="ship", vessel_type="large",
         expected="HIGH", acceptable=["HIGH"], warning=HAS_WARNING,
         locations={"大连": SAFE, "烟台": loc(22.0, 4.0, "狂风", wave=4.5, gust=28.0)}),

    # ══ 海事 · 船型对照（同样数据，不同船型不同结论）════════════════════
    dict(id=20, desc="[船型对照] 平均风 12.0 + 小船 → 越过 6 级禁航线",
         origin="青岛", destination="长岛", transport="ship", vessel_type="small",
         expected="HIGH", acceptable=["HIGH"],
         locations={"青岛": SAFE, "长岛": loc(12.0, 7.0, "大风", wave=1.2, gust=15.0)}),
    dict(id=21, desc="[船型对照] 同样 12.0 + 大船 → 未达 7 级，仅大风蓝色",
         origin="青岛", destination="长岛", transport="ship", vessel_type="large",
         expected="MEDIUM", acceptable=["MEDIUM"],
         locations={"青岛": SAFE, "长岛": loc(12.0, 7.0, "大风", wave=1.2, gust=15.0)}),
    dict(id=22, desc="[船型对照] 同样 12.0 + 不确定 → 按小船的严标准判",
         origin="青岛", destination="长岛", transport="ship", vessel_type="unknown",
         expected="HIGH", acceptable=["HIGH"],
         locations={"青岛": SAFE, "长岛": loc(12.0, 7.0, "大风", wave=1.2, gust=15.0)}),
    dict(id=23, desc="[船型对照] 不确定 + 未传船型字段 → 同样按小船判",
         origin="青岛", destination="长岛", transport="ship", vessel_type=None,
         expected="HIGH", acceptable=["HIGH"],
         locations={"青岛": SAFE, "长岛": loc(12.0, 7.0, "大风", wave=1.2, gust=15.0)}),

    # ══ 海事 · 海浪预警四色 ══════════════════════════════════════════════
    dict(id=24, desc="[海事-边界] 大船 波高 2.4（未达蓝色预警）",
         origin="宁波", destination="舟山", transport="ship", vessel_type="large",
         expected="LOW", acceptable=["LOW"],
         locations={"宁波": SAFE, "舟山": loc(6.0, 8.0, "中浪", wave=2.4, gust=9.0)}),
    dict(id=25, desc="[海事-边界] 大船 波高 2.5 → 近岸海浪蓝色预警",
         origin="宁波", destination="舟山", transport="ship", vessel_type="large",
         expected="MEDIUM", acceptable=["MEDIUM"], warning=HAS_WARNING,
         locations={"宁波": SAFE, "舟山": loc(6.0, 8.0, "中浪", wave=2.5, gust=9.0)}),
    dict(id=26, desc="[海事-PROD] 小船 波高 2.5 + 风很小（涌浪场景）",
         origin="珠海", destination="外伶仃岛", transport="ship", vessel_type="small",
         expected="HIGH", acceptable=["HIGH"],
         locations={"珠海": SAFE, "外伶仃岛": loc(4.0, 9.0, "涌浪", wave=2.5, gust=6.0)}),
    dict(id=27, desc="[海事-边界] 大船 波高 3.5 → 海浪黄色预警",
         origin="温州", destination="洞头", transport="ship", vessel_type="large",
         expected="HIGH", acceptable=["HIGH"], warning=HAS_WARNING,
         locations={"温州": SAFE, "洞头": loc(6.0, 5.0, "大浪", wave=3.5, gust=9.0)}),
    dict(id=28, desc="[海事] 大船 波高 4.2 → 超过灾害性海浪定义（4m）",
         origin="广州", destination="三亚", transport="ship", vessel_type="large",
         expected="HIGH", acceptable=["HIGH"], warning=HAS_WARNING,
         locations={"广州": SAFE, "三亚": loc(8.0, 6.0, "巨浪", wave=4.2, gust=11.0)}),

    # ══ 海事 · 阵风 ══════════════════════════════════════════════════════
    dict(id=29, desc="[海事-阵风] 大船 平均风 9.0 但阵风 13.9 → 大风蓝色预警",
         origin="厦门", destination="金门", transport="ship", vessel_type="large",
         expected="MEDIUM", acceptable=["MEDIUM"],
         locations={"厦门": SAFE, "金门": loc(9.0, 10.0, "多云", wave=1.0, gust=13.9)}),
    dict(id=30, desc="[海事-阵风] 大船 平均风 9.0 但阵风 20.8 → 大风黄色预警",
         origin="厦门", destination="金门", transport="ship", vessel_type="large",
         expected="HIGH", acceptable=["HIGH"],
         locations={"厦门": SAFE, "金门": loc(9.0, 10.0, "大风", wave=1.0, gust=20.8)}),

    # ══ 海事 · LOW（一条判据都没触发）═══════════════════════════════════
    dict(id=31, desc="[海事] 小船 风 3.0 波高 0.5 晴好",
         origin="厦门", destination="金门", transport="ship", vessel_type="small",
         expected="LOW", acceptable=["LOW"],
         locations={"厦门": SAFE, "金门": loc(3.0, 12.0, "晴，海面平静", wave=0.5, gust=5.0)}),
    dict(id=32, desc="[海事] 小船 风 7.0 波高 1.4（旧版会误判中风险，无官方依据）",
         origin="威海", destination="刘公岛", transport="ship", vessel_type="small",
         expected="LOW", acceptable=["LOW"],
         locations={"威海": SAFE, "刘公岛": loc(7.0, 9.0, "微浪", wave=1.4, gust=10.0)}),
    dict(id=33, desc="[海事] 小船 风 9.0 波高 2.0（同上，1.5m 那条线已删除）",
         origin="舟山", destination="嵊泗", transport="ship", vessel_type="small",
         expected="LOW", acceptable=["LOW"],
         locations={"舟山": SAFE, "嵊泗": loc(9.0, 7.0, "中浪", wave=2.0, gust=12.0)}),

    # ══ 弃权：证据不足时不下结论 ═════════════════════════════════════════
    dict(id=34, desc="[弃权] 海运但目的地拿不到浪高",
         origin="上海", destination="舟山", transport="ship", vessel_type="small",
         expected="UNKNOWN", acceptable=["UNKNOWN"], drop=(("destination", "wave"),),
         locations={"上海": SAFE, "舟山": loc(6.0, 8.0, "多云", wave=1.0, gust=9.0)}),
    dict(id=35, desc="[弃权] 海运两端都拿不到浪高（旧版生产环境的真实状态）",
         origin="厦门", destination="金门", transport="ship", vessel_type="small",
         expected="UNKNOWN", acceptable=["UNKNOWN"],
         drop=(("origin", "wave"), ("destination", "wave")),
         locations={"厦门": SAFE, "金门": loc(3.0, 12.0, "晴", wave=0.5, gust=5.0)}),
    dict(id=36, desc="[弃权] 航空但目的地拿不到能见度（LVTO 判据缺失）",
         origin="上海", destination="重庆", transport="plane",
         expected="UNKNOWN", acceptable=["UNKNOWN"], drop=(("destination", "vis"),),
         locations={"上海": SAFE_AIR, "重庆": loc(4.0, 8.0, "多云", gust=6.0)}),
    dict(id=37, desc="[弃权] 出行日期超出预报范围",
         origin="杭州", destination="南京", transport="plane",
         expected="UNKNOWN", acceptable=["UNKNOWN"], drop=(("destination", "date"),),
         locations={"杭州": SAFE_AIR, "南京": SAFE_AIR}),
    dict(id=38, desc="[弃权] 出发地气象数据源整体不可用",
         origin="大连", destination="烟台", transport="ship", vessel_type="large",
         expected="UNKNOWN", acceptable=["UNKNOWN"], drop=(("origin", "atmos"),),
         locations={"大连": SAFE, "烟台": loc(8.0, 8.0, "多云", wave=1.0, gust=11.0)}),
    dict(id=39, desc="[弃权] 目的地风速缺失",
         origin="青岛", destination="长岛", transport="ship", vessel_type="small",
         expected="UNKNOWN", acceptable=["UNKNOWN"], drop=(("destination", "wind"),),
         locations={"青岛": SAFE, "长岛": loc(9.0, 8.0, "多云", wave=1.0, gust=12.0)}),
]


def bundle_for(case) -> EvidenceBundle:
    return make_bundle(case["transport"], case.get("vessel_type"),
                       case["locations"], drop=case.get("drop", ()))
