"""
rules.py — 规则引擎 / 确定性 verifier

站在模型循环之外的一层。三件事，优先级从高到低：

  1. Abstention  证据不足 → UNKNOWN，绝不下结论
  2. Override UP 气象数据越过硬红线但 LLM 判低了 → 强制升级
  3. Override DOWN  LLM 判了 HIGH 但结构化指标不支持 → 降级

设计要点：判定权在这里，不在 LLM。system prompt 里也允许模型自己输出 UNKNOWN，
但那只是第二道保险（Anthropic 评测指南："给模型一个出口，信息不足时返回 Unknown"）。
"""

from evidence import EvidenceBundle

LEVELS = ("LOW", "MEDIUM", "HIGH")
LEVEL_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
UNKNOWN = "UNKNOWN"
VALID_LEVELS = set(LEVELS) | {UNKNOWN}

# ---------------------------------------------------------------------------
# 阈值表
# ---------------------------------------------------------------------------
# 航空：侧风红线用持续风速近似（没有跑道方位就算不出真侧风，这是已知简化）
AVIATION = {
    "wind_high_ms": 15.0,       # >= 触发 HIGH
    "visibility_high_km": 0.4,  # <  触发 HIGH
}

# 海事：风速红线按船型分档；浪高红线两种船型共用
# —— 手上的规则来源只给了分船型的风速红线，没给分船型的浪高红线，不编造数字。
MARITIME = {
    "coastal": {"wind_high_ms": 10.8, "wind_medium_ms": 8.0},   # 近海游船
    "ropax":   {"wind_high_ms": 17.2, "wind_medium_ms": 10.8},  # 跨海客滚船
}
WAVE_HIGH_M = 2.5     # >  触发 HIGH
WAVE_MEDIUM_M = 1.5   # >= 触发 MEDIUM

THUNDER_KEYWORDS = ("雷暴", "雷阵雨", "thunder")


def _raise_to(current: str, target: str, reason_zh: str, reason_en: str, state: dict) -> None:
    if LEVEL_ORDER[target] > LEVEL_ORDER[current]:
        state["level"] = target
        state["reason_zh"] = reason_zh
        state["reason_en"] = reason_en


def required_level(bundle: EvidenceBundle) -> tuple[str, str, str]:
    """
    只看结构化证据，推导规则引擎要求的最低风险等级。
    返回 (level, 中文触发原因, 英文触发原因)。
    """
    state = {"level": "LOW", "reason_zh": "", "reason_en": ""}
    transport = bundle.transport
    limits = MARITIME.get(bundle.vessel_type or "coastal", MARITIME["coastal"])

    for name, entry in bundle.locations.items():
        atmos = entry.get("atmos") or {}
        marine = entry.get("marine") or {}
        wind = atmos.get("max_wind_speed_ms")
        vis = atmos.get("min_visibility_km")
        desc = (atmos.get("description") or "").lower()
        wave = marine.get("max_wave_height_m")

        if transport == "plane":
            if wind is not None and wind >= AVIATION["wind_high_ms"]:
                _raise_to(state["level"], "HIGH",
                          f"{name} 风速 {wind} m/s 达到航空红线（≥{AVIATION['wind_high_ms']} m/s）",
                          f"{name} wind {wind} m/s reaches aviation red line (≥{AVIATION['wind_high_ms']} m/s)",
                          state)
            if vis is not None and vis < AVIATION["visibility_high_km"]:
                _raise_to(state["level"], "HIGH",
                          f"{name} 能见度 {vis} km 低于航空红线 {AVIATION['visibility_high_km']} km",
                          f"{name} visibility {vis} km below aviation red line of {AVIATION['visibility_high_km']} km",
                          state)
            if any(k in desc for k in THUNDER_KEYWORDS):
                _raise_to(state["level"], "MEDIUM",
                          f"{name} 存在雷暴，至少中风险",
                          f"Thunderstorm at {name} — at least medium risk", state)

        elif transport == "ship":
            if wind is not None and wind >= limits["wind_high_ms"]:
                _raise_to(state["level"], "HIGH",
                          f"{name} 风速 {wind} m/s 达到停航红线（≥{limits['wind_high_ms']} m/s）",
                          f"{name} wind {wind} m/s reaches suspension red line (≥{limits['wind_high_ms']} m/s)",
                          state)
            if wave is not None and wave > WAVE_HIGH_M:
                _raise_to(state["level"], "HIGH",
                          f"{name} 有效浪高 {wave} m 超过红线 {WAVE_HIGH_M} m",
                          f"{name} significant wave height {wave} m exceeds red line of {WAVE_HIGH_M} m",
                          state)
            if wave is not None and wave >= WAVE_MEDIUM_M:
                _raise_to(state["level"], "MEDIUM",
                          f"{name} 有效浪高 {wave} m 处于中风险区间（{WAVE_MEDIUM_M}-{WAVE_HIGH_M} m）",
                          f"{name} wave height {wave} m in medium-risk range ({WAVE_MEDIUM_M}-{WAVE_HIGH_M} m)",
                          state)
            if wind is not None and wind >= limits["wind_medium_ms"]:
                _raise_to(state["level"], "MEDIUM",
                          f"{name} 风速 {wind} m/s 处于中风险区间",
                          f"{name} wind {wind} m/s in medium-risk range", state)

    return state["level"], state["reason_zh"], state["reason_en"]


# ---------------------------------------------------------------------------
# Abstention 文案
# ---------------------------------------------------------------------------
MISSING_TEXT = {
    "date_out_of_range": ("出行日期超出可用预报范围（仅支持今天起 3 天内）",
                          "Travel date is beyond the available forecast horizon (today + 2 days only)"),
    "atmos_unavailable": ("气象数据源不可用", "Weather data source unavailable"),
    "wind_missing": ("未能取得风速数据", "Wind speed data unavailable"),
    "visibility_missing": ("未能取得能见度数据（航空硬红线，缺失则无法排除高风险）",
                           "Visibility data unavailable (an aviation red line — high risk cannot be ruled out without it)"),
    "wave_height_missing": ("未能取得有效浪高数据（海事硬红线）",
                            "Significant wave height unavailable (a maritime red line)"),
    "location_unresolved": ("地点无法解析为可用的海域坐标",
                            "Location could not be resolved to a usable marine coordinate"),
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
        "weather_summary": (
            "未能取得足够的结构化气象数据，因此不输出气象摘要。" if zh else
            "Not enough structured weather data was retrieved, so no summary is given."
        ),
        "missing_evidence": items,
        "rule_override": False,
        "decision_source": "abstain_insufficient_evidence",
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def evaluate(llm_output: dict | None, bundle: EvidenceBundle, lang: str = "zh") -> dict:
    """
    对 LLM 输出做二次校验，产出最终结论。
    llm_output 为 None 表示模型没能给出结果 —— 此时若证据充分，仍由规则引擎独立出结论。
    """
    zh = lang != "en"

    # ① Abstention 优先于一切：没有证据就不下结论
    if not bundle.ok:
        return _abstain(bundle, lang)

    req, reason_zh, reason_en = required_level(bundle)
    reason = reason_zh if zh else reason_en

    if llm_output is None:
        return {
            "risk_level": req,
            "risk_label": {"LOW": "低风险", "MEDIUM": "中风险", "HIGH": "高风险"}[req] if zh
                          else {"LOW": "Low Risk", "MEDIUM": "Medium Risk", "HIGH": "High Risk"}[req],
            "is_go_recommended": req != "HIGH",
            "core_reason": (f"模型未能给出有效结论，以下结果完全由规则引擎依据结构化气象数据得出。{reason}" if zh
                            else f"The model produced no usable output; this result comes solely from the rule engine. {reason}"),
            "alternative_advice": ABSTAIN_ADVICE_ZH if zh else ABSTAIN_ADVICE_EN,
            "weather_summary": _summarize(bundle, lang),
            "missing_evidence": [],
            "rule_override": True,
            "original_risk_level": None,
            "decision_source": "rule_only",
        }

    result = dict(llm_output)
    result.setdefault("missing_evidence", [])
    llm_level = result.get("risk_level", "LOW")
    if llm_level not in VALID_LEVELS:
        llm_level = "LOW"
        result["risk_level"] = "LOW"

    def mark(new_level: str, source: str, note_zh: str, note_en: str) -> dict:
        result["risk_level"] = new_level
        result["is_go_recommended"] = new_level != "HIGH"
        result["rule_override"] = True
        result["original_risk_level"] = llm_level
        result["decision_source"] = source
        prefix = (f"⚠️ 规则引擎校正（{llm_level}→{new_level}）：{note_zh}\n\n" if zh
                  else f"⚠️ Rule engine override ({llm_level}→{new_level}): {note_en}\n\n")
        result["core_reason"] = prefix + (result.get("core_reason") or "")
        return result

    # ② 模型主动弃权：证据虽然齐备，但模型可能看到了规则捕获不了的东西
    #    （比如搜索到的预警彼此矛盾）。红线没被越过时尊重它的弃权，不硬造结论。
    if llm_level == UNKNOWN and req == "LOW":
        result["is_go_recommended"] = None
        result["rule_override"] = False
        result["decision_source"] = "llm_abstain"
        result.setdefault("risk_label", "证据不足" if zh else "Insufficient Evidence")
        return result

    # ③ Override UP：红线被越过，无论模型说什么都要升上去（模型弃权也一样）
    if llm_level == UNKNOWN or LEVEL_ORDER[req] > LEVEL_ORDER[llm_level]:
        return mark(req, "rule_override_up", reason, reason)

    # ④ Override DOWN：只对 HIGH 生效，不干预 MEDIUM
    # MEDIUM 代表 LLM 对多因素边缘组合的综合判断，硬阈值捕获不了，应予保留。
    # 旧实现里那个 has_wave_data 守卫已经不需要了 —— 证据不足在 ① 就被拦下了。
    if llm_level == "HIGH" and LEVEL_ORDER[req] < LEVEL_ORDER["HIGH"]:
        note_zh = f"结构化气象指标未达到 HIGH 的触发条件（规则引擎推导为 {req}）"
        note_en = f"Structured indicators do not meet the trigger conditions for HIGH (rule engine derives {req})"
        return mark(req, "rule_override_down", note_zh, note_en)

    result["rule_override"] = False
    result["decision_source"] = "llm"
    result["is_go_recommended"] = result.get("risk_level") != "HIGH"
    return result


def _summarize(bundle: EvidenceBundle, lang: str) -> str:
    zh = lang != "en"
    parts = []
    for name, entry in bundle.locations.items():
        a = entry.get("atmos") or {}
        m = entry.get("marine") or {}
        bits = []
        if a.get("max_wind_speed_ms") is not None:
            bits.append(f"风速 {a['max_wind_speed_ms']} m/s" if zh else f"wind {a['max_wind_speed_ms']} m/s")
        if a.get("min_visibility_km") is not None:
            bits.append(f"能见度 {a['min_visibility_km']} km" if zh else f"visibility {a['min_visibility_km']} km")
        if m.get("max_wave_height_m") is not None:
            bits.append(f"浪高 {m['max_wave_height_m']} m" if zh else f"wave {m['max_wave_height_m']} m")
        if a.get("description"):
            bits.append(a["description"])
        if bits:
            parts.append(f"{name}：{'，'.join(bits)}" if zh else f"{name}: {', '.join(bits)}")
    return "；".join(parts) if zh else "; ".join(parts)
