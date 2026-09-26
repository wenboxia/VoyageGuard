"""
rules.py — 规则引擎 / 确定性 verifier

站在模型循环之外的一层。四件事，优先级从高到低：

  1. Abstention     证据不足 → UNKNOWN，绝不下结论
  2. Override UP    越过官方判据但 LLM 判低了 → 强制升级
  3. Override DOWN  LLM 判 HIGH 但官方判据不支持 → 降级（只对 HIGH 生效）
  4. 产出触发项列表  每条结论都能追到是哪条官方线、出处是什么

【本轮最重要的改动】输出从「断言」改为「陈述」：
    旧：高风险（停航预警）           ← 预测某条船会停航，我们无权这么说
    新：已达大风黄色预警发布标准     ← 可验证的事实，能点名出处

所有阈值和出处集中在 rules_sources.py，本文件只负责判定逻辑。
"""

from dataclasses import dataclass, field

import rules_sources as S
from evidence import EvidenceBundle

LEVELS = ("LOW", "MEDIUM", "HIGH")
LEVEL_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
UNKNOWN = "UNKNOWN"
VALID_LEVELS = set(LEVELS) | {UNKNOWN}


@dataclass
class Trigger:
    """一条被触发的官方判据。cls 决定了允许用什么措辞。"""
    code: str
    cls: str            # REG | WARN | PROD
    level: str          # HIGH | MEDIUM
    location: str
    text_zh: str
    text_en: str
    source_key: str
    value: float | None = None
    unit: str = ""

    def as_dict(self, lang: str = "zh") -> dict:
        src = S.SOURCES.get(self.source_key)
        return {
            "code": self.code, "cls": self.cls, "level": self.level,
            "location": self.location, "value": self.value, "unit": self.unit,
            "text": self.text_zh if lang != "en" else self.text_en,
            "source": (src.name_zh if lang != "en" else src.name_en) if src else "",
            "source_url": src.url if src else "",
        }


# ---------------------------------------------------------------------------
# 判据检测
# ---------------------------------------------------------------------------
def _warning_level(value: float | None, table: dict) -> str | None:
    """给定数值落在四色预警的哪一档。返回 None 表示未达最低档。"""
    if value is None:
        return None
    hit = None
    for lv in S.WARNING_ORDER:
        thr = table[lv]
        if isinstance(thr, dict):
            continue
        if value >= thr:
            hit = lv
    return hit


def _gale_level(mean_ms: float | None, gust_ms: float | None) -> str | None:
    """大风预警信号：平均风力与阵风取「或」，任一达标即发布。"""
    hit = None
    for lv in S.WARNING_ORDER:
        thr = S.GALE_WARNING[lv]
        if (mean_ms is not None and mean_ms >= thr["mean_ms"]) or \
           (gust_ms is not None and gust_ms >= thr["gust_ms"]):
            hit = lv
    return hit


def detect_triggers(bundle: EvidenceBundle) -> list[Trigger]:
    """只看结构化证据，列出所有被触发的官方判据。不做等级归并。"""
    out: list[Trigger] = []
    transport = bundle.transport
    vessel = S.effective_vessel(bundle.vessel_type)

    for name, entry in bundle.locations.items():
        atmos = entry.get("atmos") or {}
        marine = entry.get("marine") or {}
        mean = atmos.get("max_wind_speed_ms")
        gust = atmos.get("max_gust_ms")
        vis = atmos.get("min_visibility_km")
        desc = (atmos.get("description") or "").lower()
        wave = marine.get("max_wave_height_m")

        # ── 轴 B：大风预警（气象标准，与交通方式无关）──────────────────
        gale = _gale_level(mean, gust)
        if gale:
            zh_lv, en_lv = S.WARNING_LABEL[gale]
            thr = S.GALE_WARNING[gale]
            # 大风预警是「平均风 或 阵风」达标即发布，文案要点明是哪一项触发的，
            # 否则会出现"平均风力 5 级 …… 已达蓝色预警"这种看起来自相矛盾的句子。
            by_mean = mean is not None and mean >= thr["mean_ms"]
            by_gust = gust is not None and gust >= thr["gust_ms"]
            parts_zh, parts_en = [], []
            if by_mean:
                parts_zh.append(f"平均风力达 {S.beaufort(mean)} 级（{mean} m/s）")
                parts_en.append(f"mean wind reaches Force {S.beaufort(mean)} ({mean} m/s)")
            if by_gust:
                parts_zh.append(f"阵风达 {S.beaufort(gust)} 级（{gust} m/s）")
                parts_en.append(f"gusts reach Force {S.beaufort(gust)} ({gust} m/s)")
            ctx_zh, ctx_en = [], []
            if not by_mean and mean is not None:
                ctx_zh.append(f"平均风 {mean} m/s")
                ctx_en.append(f"mean wind {mean} m/s")
            if not by_gust and gust is not None:
                ctx_zh.append(f"阵风 {gust} m/s")
                ctx_en.append(f"gusts {gust} m/s")
            detail_zh = "、".join(parts_zh) + (f"（{('，'.join(ctx_zh))}）" if ctx_zh else "")
            detail_en = ", ".join(parts_en) + (f" ({'; '.join(ctx_en)})" if ctx_en else "")
            out.append(Trigger(
                f"gale_warning_{gale}", "WARN",
                "HIGH" if gale != "blue" else "MEDIUM", name,
                f"{detail_zh}，已达【大风{zh_lv}预警】发布标准",
                f"{detail_en} — meets the {en_lv} Gale Warning issuance criteria",
                "gale_warning", mean, "m/s"))

        if transport == "ship":
            # ── 轴 A：禁限航（REG）—— 唯一配得上"依规不得开航"的措辞 ──────
            ban = S.BAN_WIND_MS[vessel]
            if mean is not None and mean >= ban:
                if vessel == "small":
                    out.append(Trigger(
                        "ban_small_craft", "REG", "HIGH", name,
                        f"海面风力已达 {S.beaufort(mean)} 级（{mean} m/s）；"
                        f"按规定，风力 6 级以上时休闲船艇、游艇、游览船等禁止出海",
                        f"Sea wind reaches Force {S.beaufort(mean)} ({mean} m/s); "
                        f"small craft are prohibited from sailing at Force 6 and above",
                        "small_craft_ban", mean, "m/s"))
                else:
                    out.append(Trigger(
                        "ban_passenger_boarding", "REG", "HIGH", name,
                        f"海面风力已达 {S.beaufort(mean)} 级（{mean} m/s）；"
                        f"参照山东省规定，海面风力 7 级以上不得允许旅客、车辆上船"
                        f"（地方规定，其他海区未必相同）",
                        f"Sea wind reaches Force {S.beaufort(mean)} ({mean} m/s); "
                        f"under Shandong provincial rules, passengers and vehicles may not "
                        f"board at Force 7 and above (a local rule — other sea areas may differ)",
                        "passenger_boarding_ban", mean, "m/s"))

            # ── 轴 B：海浪预警 ────────────────────────────────────────────
            wave_lv = _warning_level(wave, S.WAVE_WARNING)
            if wave_lv:
                zh_lv, en_lv = S.WARNING_LABEL[wave_lv]
                if wave_lv != "blue":
                    out.append(Trigger(
                        f"wave_warning_{wave_lv}", "WARN", "HIGH", name,
                        f"有效波高 {wave} m，已达【近岸海浪{zh_lv}预警】发布标准",
                        f"Significant wave height {wave} m — meets the {en_lv} nearshore "
                        f"Sea Wave Warning issuance criteria",
                        "wave_warning", wave, "m"))
                elif vessel == "small":
                    # PROD：官方没给分船型的浪高线，这一条是我们的判断
                    out.append(Trigger(
                        "prod_small_craft_wave", "PROD", "HIGH", name,
                        f"有效波高 {wave} m，已达【近岸海浪蓝色预警】发布标准；"
                        f"该海况下小型船艇通常无法出海（本工具判定，非法规条款）",
                        f"Significant wave height {wave} m meets the Blue nearshore Sea Wave "
                        f"Warning criteria; small craft normally cannot operate in such seas "
                        f"(this tool's judgement, not a regulation)",
                        "wave_warning", wave, "m"))
                else:
                    out.append(Trigger(
                        "wave_warning_blue", "WARN", "MEDIUM", name,
                        f"有效波高 {wave} m，已达【近岸海浪蓝色预警】发布标准",
                        f"Significant wave height {wave} m — meets the Blue nearshore "
                        f"Sea Wave Warning issuance criteria",
                        "wave_warning", wave, "m"))

            if wave is not None and wave > S.DISASTROUS_WAVE_M:
                out.append(Trigger(
                    "disastrous_wave", "WARN", "HIGH", name,
                    f"有效波高 {wave} m，已超过【灾害性海浪】的官方定义（4 米）",
                    f"Significant wave height {wave} m exceeds the official definition of "
                    f"disastrous sea waves (4 m)",
                    "wave_warning", wave, "m"))

        elif transport == "plane":
            # REG：低能见度起飞门槛
            if vis is not None and vis < S.LVTO_RVR_KM:
                out.append(Trigger(
                    "lvto_threshold", "REG", "HIGH", name,
                    f"能见度 {vis} km，低于低能见度起飞（LVTO）门槛 "
                    f"{S.LVTO_RVR_KM} km；需机场低能见度程序、机组资质与航空器设备三方齐备，"
                    f"多数情况下航班将延误或备降",
                    f"Visibility {vis} km is below the Low Visibility Take-Off threshold of "
                    f"{S.LVTO_RVR_KM} km; airport LVP, crew qualification and aircraft equipment "
                    f"must all be in place, otherwise delays or diversions are likely",
                    "lvto", vis, "km"))

            # PROD：侧风近似
            cw = S.PROD_RULES["aviation_wind"]["value"]
            if mean is not None and mean >= cw:
                out.append(Trigger(
                    "prod_aviation_wind", "PROD", "HIGH", name,
                    f"风速 {mean} m/s，接近常见窄体机干跑道侧风限制（B737 约 {cw} m/s）；"
                    f"本工具用总风速近似侧风分量，湿滑跑道下该限制会更低（本工具判定，非法规条款）",
                    f"Wind {mean} m/s approaches the dry-runway crosswind limit of common "
                    f"narrow-bodies (B737 ~{cw} m/s); we approximate the crosswind component "
                    f"with total wind speed, and wet runways lower this limit "
                    f"(this tool's judgement, not a regulation)",
                    "crosswind", mean, "m/s"))

            # PROD：雷暴，无法规阈值
            if any(k in desc for k in S.THUNDER_KEYWORDS):
                out.append(Trigger(
                    "prod_thunderstorm", "PROD", "MEDIUM", name,
                    "起降地存在雷暴。无对应法规阈值，依据是运营经验："
                    "强对流下延误与取消风险显著上升（本工具判定）",
                    "Thunderstorm at the airport. No regulatory threshold exists; based on "
                    "operational experience, convective weather sharply raises delay and "
                    "cancellation risk (this tool's judgement)",
                    "", None, ""))   # 雷暴没有任何出处，source_key 必须留空（曾误写成 crosswind）

    # ── 航路危险天气：SIGMET（仅航空、仅当天，evidence 层已限定）─────────
    # 文案顺序统一为【事实 → 出处 → 免责】，跟其余触发项一致
    # （「厦门 — 海面风力已达 6 级（13.1 m/s）；……」）。
    # 前端会自己拼 "{location} — "，而这里 location 就是「航路」，
    # 所以正文不能再以「航路」开头，否则读出来是「航路 — 航路穿越……」。
    for sg in bundle.sigmets:
        hz = S.SIGMET_HAZARD_ZH.get(sg.get("hazard") or "", sg.get("hazard") or "危险天气")
        ql = S.SIGMET_QUALIFIER_ZH.get(sg.get("qualifier") or "", "")
        hz_en = S.SIGMET_HAZARD_EN.get(sg.get("hazard") or "",
                                       (sg.get("hazard") or "hazardous weather").lower())
        ql_en = S.SIGMET_QUALIFIER_EN.get(sg.get("qualifier") or "", "")
        fir_raw = (sg.get("firName") or "").split(" ", 1)[-1]
        # 名单里有就直接接中文，没有就前后留空格，避免「这是SHANGHAI飞行情报区」挤在一起
        fir_zh = S.SIGMET_FIR_ZH.get(fir_raw.upper()) or f" {fir_raw} "
        valid_zh = valid_en = ""
        if sg.get("validTimeTo"):
            import datetime as _dt
            end = _dt.datetime.fromtimestamp(sg["validTimeTo"], _dt.timezone.utc) \
                + _dt.timedelta(hours=8)
            valid_zh = f"，有效期至北京时间 {end:%H:%M}"
            valid_en = f", valid until {end:%H:%M} Beijing time"
        band_zh = band_en = ""
        if sg.get("base") is not None and sg.get("top") is not None:
            lo, hi = sg["base"] // 100, sg["top"] // 100
            band_zh = f"，影响高度 FL{lo:03d}–{hi:03d}"
            band_en = f", affecting FL{lo:03d}–{hi:03d}"
        out.append(Trigger(
            f"sigmet_{(sg.get('hazard') or 'wx').lower()}", "WARN", S.SIGMET_LEVEL, "航路",
            f"{ql}{hz}{valid_zh}{band_zh}。"
            f"这是{fir_zh}飞行情报区正在生效的官方航路危险天气通报（SIGMET）；"
            f"航路危险天气通常由绕飞处置，是否影响航班以航司通知为准",
            f"{ql_en} {hz_en}".strip().capitalize()
            + f"{valid_en}{band_en}. "
            f"This is an official en-route hazard notice (SIGMET) active in the {fir_raw} FIR; "
            f"en-route hazards are normally handled by rerouting — follow the airline's "
            f"notice for actual impact",
            "sigmet"))

    return out


def required_level(bundle: EvidenceBundle) -> tuple[str, list[Trigger]]:
    """规则引擎要求的最低风险等级 + 支撑它的触发项列表。"""
    triggers = detect_triggers(bundle)
    level = "LOW"
    for t in triggers:
        if LEVEL_ORDER[t.level] > LEVEL_ORDER[level]:
            level = t.level
    # 展示顺序：先 HIGH 后 MEDIUM；同级里 REG > PROD > WARN（法规条款最该先看到）
    cls_rank = {"REG": 0, "PROD": 1, "WARN": 2}
    triggers.sort(key=lambda t: (-LEVEL_ORDER[t.level], cls_rank.get(t.cls, 9)))
    return level, triggers


# ---------------------------------------------------------------------------
# Abstention
# ---------------------------------------------------------------------------
MISSING_TEXT = {
    "date_out_of_range": ("出行日期超出可用预报范围（仅支持今天起 3 天内）",
                          "Travel date is beyond the available forecast horizon (today + 2 days only)"),
    "atmos_unavailable": ("气象数据源不可用", "Weather data source unavailable"),
    "wind_missing": ("未能取得风速数据", "Wind speed data unavailable"),
    "visibility_missing": ("未能取得能见度数据（低能见度起飞门槛的判据，缺失则无法排除高风险）",
                           "Visibility unavailable (needed for the low-visibility take-off threshold; "
                           "high risk cannot be ruled out without it)"),
    "wave_height_missing": ("未能取得有效浪高数据（海浪预警的判据）",
                            "Significant wave height unavailable (needed for sea wave warning criteria)"),
    "location_unresolved": ("地名无法解析为坐标", "Place name could not be resolved to coordinates"),
    # 这两条是【范围边界】不是【系统失败】，措辞必须让用户能分清。
    # 前端用 escHtml 渲染，不要写 markdown 标记，会原样显示出来。
    "not_coastal": (
        "该地点不临海，无法按海事出行评估"
        "（内河与湖泊航线同样不在覆盖范围内——内河另有一套官方标准，"
        "判据是风力分档、不含浪高，与海事判据不通用）",
        "This location is not on the coast, so no maritime assessment is given "
        "(inland waterway and lake routes are likewise out of scope — inland navigation follows a "
        "separate official standard that is wind-tiered and does not use wave height at all)"),
    "beyond_taf_horizon": (
        "出行日期超出该机场官方预报（TAF）的覆盖范围。TAF 通常只覆盖约 30 小时，"
        "更远的日期没有官方机场预报可依据，本工具不用其他数据源顶替",
        "The travel date is beyond the airport's official TAF forecast horizon. TAF typically "
        "covers about 30 hours; beyond that there is no official airport forecast to rely on, and "
        "this tool will not substitute another data source"),
    "no_airport": (
        "该地点不在本工具收录的民航机场清单内，因此不按航空出行评估",
        "This location is not in the tool's civil-airport list, so no aviation assessment is given"),
}

ABSTAIN_ADVICE_ZH = (
    "请直接查询官方渠道确认：航空可查航司官网 / 机场航班动态；"
    "海运可查当地海事局公告、港航企业公众号或拨打 12395 海上搜救电话。"
    "本工具在证据不足时不会给出风险等级。"
)
ABSTAIN_ADVICE_EN = (
    "Please confirm through official channels: airline or airport status pages for flights; "
    "the local maritime safety administration bulletin or port operator announcements for sailings. "
    "This tool does not issue a risk level when the evidence is insufficient."
)


def _abstain(bundle: EvidenceBundle, lang: str) -> dict:
    zh = lang != "en"
    items, seen = [], set()
    for m in bundle.missing:
        text = MISSING_TEXT.get(m.code, (m.code, m.code))[0 if zh else 1]
        line = f"{m.location}：{text}" if zh else f"{m.location}: {text}"
        if line not in seen:
            seen.add(line)
            items.append(line)

    reason = (
        "证据不足，无法给出风险判断。缺少以下关键数据：\n" + "\n".join(f"· {i}" for i in items)
        if zh else
        "Insufficient evidence to assess risk. The following required data is missing:\n"
        + "\n".join(f"- {i}" for i in items)
    )
    return {
        "risk_level": UNKNOWN,
        "risk_label": "证据不足" if zh else "Insufficient Evidence",
        "is_go_recommended": None,
        "core_reason": reason,
        "alternative_advice": ABSTAIN_ADVICE_ZH if zh else ABSTAIN_ADVICE_EN,
        "weather_summary": ("未能取得足够的结构化气象数据，因此不输出气象摘要。" if zh else
                            "Not enough structured weather data was retrieved, so no summary is given."),
        "missing_evidence": items,
        "triggers": [],
        "rule_override": False,
        "decision_source": "abstain_insufficient_evidence",
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
RISK_LABEL = {
    "zh": {"LOW": "低风险", "MEDIUM": "中风险", "HIGH": "高风险"},
    "en": {"LOW": "Low Risk", "MEDIUM": "Medium Risk", "HIGH": "High Risk"},
}


def evaluate(llm_output: dict | None, bundle: EvidenceBundle, lang: str = "zh") -> dict:
    """对 LLM 输出做二次校验，产出最终结论。触发项列表始终由规则引擎产出，模型不参与。"""
    zh = lang != "en"

    # ① Abstention 优先于一切
    if not bundle.ok:
        return _abstain(bundle, lang)

    req, triggers = required_level(bundle)
    trigger_dicts = [t.as_dict(lang) for t in triggers]
    summary_line = " / ".join(t.text_zh if zh else t.text_en for t in triggers[:2])

    def finish(result: dict, level: str, source: str) -> dict:
        result["risk_level"] = level
        result["is_go_recommended"] = None if level == UNKNOWN else level != "HIGH"
        result["decision_source"] = source
        result["triggers"] = trigger_dicts
        result.setdefault("missing_evidence", [])
        if level != "LOW":
            note = S.AUTHORITY_NOTE_ZH if zh else S.AUTHORITY_NOTE_EN
            body = result.get("core_reason") or ""
            if note not in body:
                result["core_reason"] = f"{body}\n\n{note}".strip()
        return result

    if llm_output is None:
        return finish({
            "risk_label": RISK_LABEL["zh" if zh else "en"][req],
            "core_reason": ((f"模型未能给出有效结论，以下结果完全由规则引擎依据结构化气象数据得出。"
                             f"{summary_line}") if zh else
                            (f"The model produced no usable output; this result comes solely from "
                             f"the rule engine. {summary_line}")),
            "alternative_advice": ABSTAIN_ADVICE_ZH if zh else ABSTAIN_ADVICE_EN,
            "weather_summary": _summarize(bundle, lang),
            "rule_override": True,
            "original_risk_level": None,
        }, req, "rule_only")

    result = dict(llm_output)
    llm_level = result.get("risk_level", "LOW")
    if llm_level not in VALID_LEVELS:
        llm_level = "LOW"

    def mark(new_level: str, source: str) -> dict:
        result["rule_override"] = True
        result["original_risk_level"] = llm_level
        prefix = (f"⚠️ 规则引擎校正（{llm_level}→{new_level}）\n\n" if zh
                  else f"⚠️ Rule engine override ({llm_level}→{new_level})\n\n")
        result["core_reason"] = prefix + (result.get("core_reason") or "")
        return finish(result, new_level, source)

    # ② 模型主动弃权且规则无触发 → 尊重它的弃权
    if llm_level == UNKNOWN and req == "LOW":
        result["is_go_recommended"] = None
        result["rule_override"] = False
        result["decision_source"] = "llm_abstain"
        result["triggers"] = trigger_dicts
        result.setdefault("missing_evidence", [])
        result.setdefault("risk_label", "证据不足" if zh else "Insufficient Evidence")
        return result

    # ③ Override UP
    if llm_level == UNKNOWN or LEVEL_ORDER[req] > LEVEL_ORDER[llm_level]:
        return mark(req, "rule_override_up")

    # ④ Override DOWN：只对 HIGH 生效。MEDIUM 代表模型对多因素边缘组合的综合判断，
    #    官方判据捕获不了，予以保留。
    if llm_level == "HIGH" and LEVEL_ORDER[req] < LEVEL_ORDER["HIGH"]:
        return mark(req, "rule_override_down")

    result["rule_override"] = False
    return finish(result, llm_level, "llm")


def _summarize(bundle: EvidenceBundle, lang: str) -> str:
    zh = lang != "en"
    parts = []
    for name, entry in bundle.locations.items():
        a = entry.get("atmos") or {}
        m = entry.get("marine") or {}
        bits = []
        if a.get("max_wind_speed_ms") is not None:
            bft = S.beaufort(a["max_wind_speed_ms"])
            bits.append(f"风速 {a['max_wind_speed_ms']} m/s（{bft} 级）" if zh
                        else f"wind {a['max_wind_speed_ms']} m/s (Force {bft})")
        if a.get("max_gust_ms") is not None:
            bits.append(f"阵风 {a['max_gust_ms']} m/s" if zh else f"gusts {a['max_gust_ms']} m/s")
        if a.get("min_visibility_km") is not None:
            bits.append(f"能见度 {a['min_visibility_km']} km" if zh
                        else f"visibility {a['min_visibility_km']} km")
        if m.get("max_wave_height_m") is not None:
            bits.append(f"有效波高 {m['max_wave_height_m']} m" if zh
                        else f"wave height {m['max_wave_height_m']} m")
        if a.get("description"):
            bits.append(a["description"])
        if bits:
            parts.append(f"{name}：{'，'.join(bits)}" if zh else f"{name}: {', '.join(bits)}")
    return "；".join(parts) if zh else "; ".join(parts)
