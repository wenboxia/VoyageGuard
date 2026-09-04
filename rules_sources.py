"""
rules_sources.py — 阈值与出处的单一事实来源

本项目的一条硬规矩：**知识库里只放有官方出处的阈值**。
每一条判据必须标注来源类型，允许的措辞由类型决定：

    REG   法规里的禁止性条款      → 可以说"依规不得开航 / 不得上客"
    WARN  官方预警的发布标准      → 只能说"已达 X 色预警发布标准"
    PROD  我们自己的产品决策      → 必须说"本工具判定"，并写明理由

为什么这么严：交通运输部对人大建议的答复函明确指出，船舶抗风能力按【风压】计算，
与风级【不是对应关系】，官方因此故意不在船舶证书上标注抗风等级，理由是
"容易引起社会误解"。真实链条是：每条船有自己的稳性限制 → 船公司判断 →
海事部门可下令停航 → 地方另有一刀切规定。

所以本工具能说的只有一句话：**今天有没有越过某条官方发布预警或禁航的线**。
至于这条船会不会停航，那是船公司和海事部门的决定，我们无权预测。

上一版的错误正是在这里：把"越线"这个可验证的事实，说成了"停航预警"这个预测。
并且其中一条（大船 8 级）没有任何来源，而且比真实标准宽松，属于"该警告时不警告"。
"""

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 蒲福风级 → 平均风速下限（m/s），中国 17 级风级表
# ---------------------------------------------------------------------------
BEAUFORT_MIN_MS = {
    0: 0.0, 1: 0.3, 2: 1.6, 3: 3.4, 4: 5.5, 5: 8.0, 6: 10.8, 7: 13.9,
    8: 17.2, 9: 20.8, 10: 24.5, 11: 28.5, 12: 32.7, 13: 37.0,
}


def beaufort(ms: float | None) -> int | None:
    """风速换算风级。返回该风速所属的级数。"""
    if ms is None:
        return None
    level = 0
    for lv, lo in sorted(BEAUFORT_MIN_MS.items()):
        if ms >= lo:
            level = lv
    return level


# ---------------------------------------------------------------------------
# 来源登记
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Source:
    key: str
    name_zh: str
    name_en: str
    url: str
    caveat_zh: str = ""     # 已知局限，会出现在 README 里


SOURCES = {
    "gale_warning": Source(
        "gale_warning", "中国气象局《大风预警信号》",
        "China Meteorological Administration — Gale Warning Signals",
        "https://www.cma.gov.cn/2011xzt/2022zt/20220330/2022033011/202204/t20220412_4750933.html",
        "全国统一标准。平均风力与阵风取「或」关系，任一达标即发布。",
    ),
    "wave_warning": Source(
        "wave_warning", "《海洋灾害应急预案》海浪警报发布标准（广州市规划和自然资源局）",
        "Marine Disaster Emergency Plan — Sea Wave Warning issuance criteria "
        "(Guangzhou Bureau of Planning and Natural Resources)",
        "https://ghzyj.gz.gov.cn/hdjl/ywzsk/zygl/content/post_8340306.html",
        "政府门户转载的具名发布文件（2020-08 印发），四色阈值原文为"
        "蓝 2.5–3.5（不含）／黄 3.5–4.5（不含）／橙 4.5–6.0（不含）／红 ≥6.0 米有效波高。"
        "该页措辞针对珠江口海域，是全国分级的地方落地版本。"
        "近岸与近海阈值不同，本工具取港口坐标，适用【近岸】档。另有硬定义：超过 4 米为灾害性海浪。",
    ),
    "small_craft_ban": Source(
        "small_craft_ban", "沿海防大风管理规定（小型船艇禁止出海）· 转述来源",
        "Coastal gale management rules — small craft prohibited from sailing "
        "(secondary source)",
        "https://www.chinanews.com.cn/sh/2026/08-31/10687414.shtml",
        "【转述来源，未取得发布文件原文】。搜遍海事局(msa.gov.cn)与司法部法规库(moj.gov.cn) "
        "未找到写明「6 级禁止出海」的规章条文，这里引的是新闻转述："
        "「风力预计达到 6 至 8 级时，乡镇船舶、海钓船、休闲船艇、游艇等禁止出海」。"
        "已核实的旁证：日照海事局 2026-08-07 在预报「海上 6 级、阵风 7~8 级」时发布大风蓝色预警，"
        "并点名通知「抗风等级低、危险程度高的小型船舶」"
        "(https://www.sd.msa.gov.cn/art/2026/8/7/art_5305_1830755.html) —— "
        "佐证了阈值方向，但不是规则本身的出处。"
        "阈值予以保留：方向偏严（安全的一侧），且无任何证据说它是错的。"
        "属地方/专项管理规定，非全国统一法规，不同海事辖区可能不同。",
    ),
    "passenger_boarding_ban": Source(
        "passenger_boarding_ban", "山东省规定（7 级以上不得允许旅客、车辆上船）",
        "Shandong provincial rule — no passenger/vehicle boarding at Force 7+",
        "http://whhly.shandong.gov.cn/art/2018/10/26/art_70739_6714054.html",
        "地方规定。渤海海峡客滚船 2018-10-26、2026-02-05 的实际停航与之吻合。"
        "其他海区未必执行同一数字。",
    ),
    "yangtze_ban": Source(
        "yangtze_ban", "《长江干线恶劣天气等条件下船舶禁限航管理规定》",
        "Yangtze Trunk Line adverse-weather navigation restrictions",
        "https://xxgk.mot.gov.cn/jigou/aqyzljlglj/202006/t20200623_3316290.html",
        "内河规定，本工具面向沿海，仅作为「船越小限制越严」这一分档方向的旁证，不直接采用其数值。",
    ),
    "lvto": Source(
        "lvto", "民航局《航空器机场运行最低标准的制定与实施规定》",
        "CAAC — Aerodrome Operating Minima",
        "http://www.caac.gov.cn/XXGK/XXGK/MHGZ/201511/P020151103350133392047.pdf",
        "RVR 低于 400 米称为「低能见度起飞(LVTO)」，需机场 LVP、机组资质、航空器设备三方齐备。"
        "这是运行门槛，不是禁飞令。着陆最低标准另计（一类约 550m，二类 300m，三类更低）。",
    ),
    "crosswind": Source(
        "crosswind", "常见窄体机厂商公布的干跑道侧风限制（二手转述）",
        "Published crosswind limits for common narrow-body types (secondary source)",
        "",
        "【二手转述，未取得手册原件】。这条规则本身标的就是 PROD（本工具的产品决策），"
        "从未声称是法规；15 m/s 的真实来源是波音/空客的机型手册值。"
        "原来挂的科普网站链接反而像在假装有出处，已去掉——宁可没有链接，不要假装有出处。"
        "B737 干跑道约 15 m/s（30 节）、A320 约 29 节，全机型区间 25–33 节。"
        "【湿跑道 B737 降至 12 m/s，刹车效应中等以下降至 7 m/s】。分机型、分道面，不是通用红线。",
    ),
    "sigmet": Source(
        "sigmet", "ICAO 重要气象情报（SIGMET）",
        "ICAO Significant Meteorological Information (SIGMET)",
        "https://aviationweather.gov/gfa/#sigmet",
        "由各飞行情报区的气象监视台发布，是航路危险天气（雷暴、积冰、颠簸、火山灰、"
        "热带气旋）的官方产品。【有效期只有 4-6 小时】，所以只能用于当天查询，"
        "而且我们不知道用户具体几点的航班——文案必须带上有效期让用户自己判断。",
    ),
    "mot_reply_6750": Source(
        "mot_reply_6750", "交通运输部关于十三届全国人大一次会议第 6750 号建议的答复函",
        "MOT reply to NPC proposal No. 6750",
        "https://xxgk.mot.gov.cn/jigou/haishi/202006/t20200630_3319352.html",
        "官方明确：船舶稳性按风压计算，与蒲福风级不对应，故不在船证上标注抗风等级，"
        "以免引起社会误解。这是本项目「不预测停航、只陈述越线」的直接依据。",
    ),
}


# ---------------------------------------------------------------------------
# 阈值
# ---------------------------------------------------------------------------
# 轴 A —— 禁限航（REG）：官方明文"禁止/不得"，这才配触发 HIGH
BAN_WIND_MS = {
    "small": BEAUFORT_MIN_MS[6],   # 10.8 —— 小型船艇 6 级禁止出海
    "large": BEAUFORT_MIN_MS[7],   # 13.9 —— 海上客运 7 级不得允许旅客、车辆上船
}
# 航空没有等价的通用禁飞线。不假装有。
AVIATION_HAS_BAN_LINE = False

# 轴 B —— 官方预警（WARN）
# 大风预警信号：平均风力 与 阵风 取「或」
GALE_WARNING = {
    "blue":   {"mean_ms": BEAUFORT_MIN_MS[6],  "gust_ms": BEAUFORT_MIN_MS[7]},
    "yellow": {"mean_ms": BEAUFORT_MIN_MS[8],  "gust_ms": BEAUFORT_MIN_MS[9]},
    "orange": {"mean_ms": BEAUFORT_MIN_MS[10], "gust_ms": BEAUFORT_MIN_MS[11]},
    "red":    {"mean_ms": BEAUFORT_MIN_MS[12], "gust_ms": BEAUFORT_MIN_MS[13]},
}
# 海浪预警（近岸海域有效波高，米）
WAVE_WARNING = {"blue": 2.5, "yellow": 3.5, "orange": 4.5, "red": 6.0}
DISASTROUS_WAVE_M = 4.0        # 官方硬定义：超过 4 米为灾害性海浪

WARNING_ORDER = ["blue", "yellow", "orange", "red"]
WARNING_LABEL = {
    "blue":   ("蓝色", "Blue"), "yellow": ("黄色", "Yellow"),
    "orange": ("橙色", "Orange"), "red": ("红色", "Red"),
}

# 航空
LVTO_RVR_KM = 0.4              # REG：低于此为低能见度起飞，需特殊资质

# ---------------------------------------------------------------------------
# PROD —— 我们自己的产品决策，非法规引用。刻意只有两条。
# ---------------------------------------------------------------------------
PROD_RULES = {
    "small_craft_wave": {
        "value": WAVE_WARNING["blue"],
        "reason_zh": (
            "官方对小船的风速禁航线本就比大船严（6 级 vs 7 级），我们按同样的保守比例"
            "响应海浪预警；且涌浪场景（台风远在千里、本地风不大但涌很大）下风速判据会漏判，"
            "而那正是小型船艇最容易出事的情况。"
        ),
        "reason_en": (
            "Official wind bans are already stricter for small craft (Force 6 vs 7), so we mirror "
            "that conservatism for wave warnings. Swell-dominated conditions — distant typhoon, "
            "light local wind, large swell — slip past the wind criterion entirely, and that is "
            "precisely when small craft get into trouble."
        ),
    },
    "aviation_wind": {
        "value": 15.0,
        "reason_zh": (
            "接近常见窄体机干跑道最大侧风分量（B737 约 15 m/s、A320 约 29 节），"
            "且我们用总风速近似侧风（没有跑道方位算不出真侧风）。"
            "注意：湿滑跑道下该限制会明显更低。"
        ),
        "reason_en": (
            "Close to the published dry-runway crosswind limit of common narrow-bodies "
            "(B737 ~15 m/s, A320 ~29 kt). We approximate the crosswind component with total "
            "wind speed because runway heading is unavailable. Wet runways lower this markedly."
        ),
    },
}

# 航路危险天气：SIGMET 是官方产品，但有效期只有几小时，且处置以绕飞为主。
# 查证依据：航路遇雷雨的处置顺序是 绕飞 → 返航/备降 → 只有大范围绕不过才取消；
# 民航局统计里空管/流量原因仅占 0.02%。所以定 MEDIUM 而不是 HIGH。
SIGMET_LEVEL = "MEDIUM"
SIGMET_HAZARD_ZH = {
    "TS": "雷暴", "TSGR": "雷暴伴冰雹", "TURB": "颠簸", "ICE": "积冰",
    "MTW": "山地波", "DS": "沙暴", "SS": "尘暴", "VA": "火山灰", "TC": "热带气旋",
    "RDOACT CLD": "放射性云",
}
SIGMET_QUALIFIER_ZH = {"SEV": "严重", "MOD": "中度", "EMBD": "嵌入性",
                       "OBSC": "隐蔽性", "FRQ": "频繁", "SQL": "飑线", "ISOL": "孤立"}
SIGMET_HAZARD_EN = {
    "TS": "thunderstorms", "TSGR": "thunderstorms with hail", "TURB": "turbulence",
    "ICE": "icing", "MTW": "mountain wave", "DS": "duststorm", "SS": "sandstorm",
    "VA": "volcanic ash", "TC": "tropical cyclone", "RDOACT CLD": "radioactive cloud",
}
SIGMET_QUALIFIER_EN = {"SEV": "severe", "MOD": "moderate", "EMBD": "embedded",
                       "OBSC": "obscured", "FRQ": "frequent", "SQL": "squall line",
                       "ISOL": "isolated"}
# 飞行情报区名。API 返回的是英文（"SHANGHAI"），夹在中文句子里很硌眼。
# 查不到就原样保留 —— 名单不全不影响正确性，只影响这一个词好不好读。
SIGMET_FIR_ZH = {
    "SHANGHAI": "上海", "BEIJING": "北京", "GUANGZHOU": "广州", "KUNMING": "昆明",
    "WUHAN": "武汉", "SANYA": "三亚", "SHENYANG": "沈阳", "LANZHOU": "兰州",
    "URUMQI": "乌鲁木齐", "HONG KONG": "香港", "HONGKONG": "香港", "TAIBEI": "台北",
}

# 雷暴：无官方阈值，纯运营经验，单列
THUNDER_KEYWORDS = ("雷暴", "雷阵雨", "thunder")
THUNDER_NOTE_ZH = "无对应法规阈值。依据是运营经验：雷雨季民航局的正常率目标为 70%+，强对流下取消与延误风险显著上升。"
THUNDER_NOTE_EN = "No regulatory threshold exists. Based on operational experience: CAAC targets 70%+ on-time rate during the thunderstorm season, and convective weather sharply raises delay/cancellation risk."

# 每个非 LOW 结论都要带的免责句 —— 从页脚小字移进正文
AUTHORITY_NOTE_ZH = "最终是否停航/取消由承运人与主管部门决定，请以官方通知为准。"
AUTHORITY_NOTE_EN = "Whether service is actually suspended is decided by the carrier and the authorities — always follow official notices."

VESSEL_LABEL = {
    "small":   ("小船 / 渡轮", "small boat / ferry"),
    "large":   ("大船 / 客轮", "large ship / ro-pax"),
    "unknown": ("未指定船型", "vessel type not specified"),
}


def effective_vessel(vessel_type: str | None) -> str:
    """unknown 一律按 small 判定 —— 用户答不上来时往安全方向错。"""
    return "small" if vessel_type in (None, "unknown", "small") else "large"
