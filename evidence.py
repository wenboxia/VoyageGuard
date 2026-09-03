"""
evidence.py — 确定性证据管线

核心设计：把证据分成两类，边界写死在代码里。

  必需证据（本模块）—— 确定性预取，不经模型自由裁量。
      出发地/目的地大气数据；transport=ship 时再加两端浪高。
      规则引擎与 abstention 门【只看】这里产出的 evidence bundle。

  补充证据（agent.py 的工具表）—— 模型自由裁量，失败不影响结论正确性。

为什么必需证据不能交给模型去调工具：AgentAbstain (arXiv 2607.10059) 实测
最强模型在成对弃权任务上只有 59.5% 准确率，且弃权能力与通用任务能力基本无关。
安全关键证据的获取不能取决于模型这一次想不想调工具。

数据源：
  - wttr.in                    大气（风速、阵风、能见度、天气描述）
  - Open-Meteo Marine API      有效浪高、波周期、涌浪
  - Open-Meteo Geocoding API   白名单未命中时的兜底坐标解析（必须经海洋 API 回验）
"""

import concurrent.futures as cf
import datetime
import json
import os
import time
import urllib.parse
from dataclasses import dataclass, field

import requests

# base url 走环境变量，L2 集成测试可以指向不可达主机做故障注入
WTTR_BASE = os.getenv("VOYAGEGUARD_WTTR_BASE", "https://wttr.in")
MARINE_BASE = os.getenv("VOYAGEGUARD_MARINE_BASE", "https://marine-api.open-meteo.com/v1/marine")
GEOCODE_BASE = os.getenv("VOYAGEGUARD_GEOCODE_BASE", "https://geocoding-api.open-meteo.com/v1/search")

HTTP_TIMEOUT = float(os.getenv("VOYAGEGUARD_HTTP_TIMEOUT", "15"))

# wttr.in 只提供 today / +1 / +2，与前端三个日期按钮一致
FORECAST_HORIZON_DAYS = 3


HTTP_RETRIES = int(os.getenv("VOYAGEGUARD_HTTP_RETRIES", "2"))


def _get_json(url: str) -> dict:
    """带退避重试的 GET。免费气象 API 在并发下会瞬时限流，重试比放大并发划算。"""
    last: Exception | None = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:      # noqa: BLE001 — 上层把失败转成 abstention，不是崩溃
            last = e
            if attempt < HTTP_RETRIES:
                time.sleep(0.6 * (attempt + 1))
    raise last if last else RuntimeError("unknown http error")


# ---------------------------------------------------------------------------
# 港口白名单 —— 契约层
# ---------------------------------------------------------------------------
# 坐标全部经 evals/l3_sufficiency.py 实测确认能从 Open-Meteo Marine 取到浪高。
# 内河上游港口（广州、南通）已下移到河口/外港，否则海洋 API 返回全 null。
PORTS: dict[str, tuple[float, float]] = {
    "上海": (31.36, 121.68),
    "舟山": (30.00, 122.11),
    "嵊泗": (30.73, 122.45),
    "普陀山": (30.01, 122.39),
    "宁波": (29.87, 121.98),
    "三亚": (18.23, 109.51),
    "海口": (20.03, 110.29),
    "徐闻": (20.23, 110.17),
    "厦门": (24.45, 118.09),
    "金门": (24.43, 118.32),
    "泉州": (24.81, 118.68),
    "大连": (38.92, 121.65),
    "长海": (39.27, 122.59),
    "烟台": (37.55, 121.39),
    "蓬莱": (37.83, 120.76),
    "长岛": (37.92, 120.73),
    "威海": (37.51, 122.12),
    "刘公岛": (37.51, 122.18),
    "青岛": (36.06, 120.32),
    "朝连岛": (36.10, 120.85),
    "日照": (35.38, 119.55),
    "连云港": (34.75, 119.45),
    "南通": (32.13, 121.62),      # 吕四港
    "福州": (26.02, 119.62),
    "平潭": (25.50, 119.79),
    "温州": (27.94, 120.83),
    "洞头": (27.83, 121.15),
    "汕头": (23.34, 116.75),
    "深圳": (22.49, 113.90),
    "蛇口": (22.47, 113.90),
    "广州": (22.72, 113.62),      # 南沙港
    "珠海": (22.21, 113.56),
    "外伶仃岛": (22.10, 114.03),
    "香港": (22.29, 114.17),
    "澳门": (22.15, 113.55),
    "湛江": (21.18, 110.40),
    "北海": (21.46, 109.10),
    "涠洲岛": (21.03, 109.12),
    "天津": (38.98, 117.79),
    "秦皇岛": (39.91, 119.62),
}

# 常见别名 / 英文名 → 白名单主键
PORT_ALIASES: dict[str, str] = {
    "shanghai": "上海", "zhoushan": "舟山", "shengsi": "嵊泗", "ningbo": "宁波",
    "sanya": "三亚", "haikou": "海口", "xiamen": "厦门", "kinmen": "金门",
    "quanzhou": "泉州", "dalian": "大连", "yantai": "烟台", "penglai": "蓬莱",
    "weihai": "威海", "qingdao": "青岛", "rizhao": "日照", "lianyungang": "连云港",
    "nantong": "南通", "fuzhou": "福州", "pingtan": "平潭", "wenzhou": "温州",
    "shantou": "汕头", "shenzhen": "深圳", "guangzhou": "广州", "zhuhai": "珠海",
    "hongkong": "香港", "hong kong": "香港", "macau": "澳门", "macao": "澳门",
    "zhanjiang": "湛江", "beihai": "北海", "tianjin": "天津",
    "qinhuangdao": "秦皇岛", "xuwen": "徐闻",
    "上海港": "上海", "洋山港": "上海", "吴淞口": "上海", "南沙港": "广州",
    "吕四港": "南通", "定海": "舟山", "沈家门": "舟山",
}


# ---------------------------------------------------------------------------
# 民航机场白名单 —— 航空侧的可达性契约
# ---------------------------------------------------------------------------
# 为什么需要：航空判据只要风速和能见度，而【任何地名都能查到风速和能见度】。
# 所以在加这个清单之前，"上海 → 南极 飞机" 会返回"低风险，建议出行"，
# "北京 → 珠穆朗玛峰" 也能正常出结论。船舶侧因为需要浪高，顺带获得了一个
# "这里是不是海"的校验；航空侧没有等价的天然校验，只能靠白名单。
#
# 这里只存名字不存坐标：120 个机场的精确坐标我无法逐一核实，而城市名查 wttr.in
# 本来就能用。代价是拿到的是城市气象而非机场跑道气象（机场通常离市区 20-40km）,
# 这个简化写在 README 的已知取舍里。
#
# 清单不完整是刻意的：漏收一个支线机场会导致弃权（安全方向），
# 而错误地放行一个不存在的航点会导致"建议出行"（危险方向）。
AIRPORTS: set[str] = {
    # 直辖市与主要枢纽
    "北京", "上海", "天津", "重庆", "广州", "深圳", "成都", "杭州", "西安", "昆明",
    "南京", "郑州", "武汉", "长沙", "青岛", "厦门", "大连", "沈阳", "哈尔滨", "济南",
    # 省会 / 首府
    "石家庄", "太原", "呼和浩特", "长春", "合肥", "福州", "南昌", "南宁", "海口",
    "贵阳", "拉萨", "兰州", "西宁", "银川", "乌鲁木齐",
    # 华北 / 东北
    "唐山", "秦皇岛", "邯郸", "张家口", "大同", "运城", "包头", "鄂尔多斯", "赤峰",
    "呼伦贝尔", "鞍山", "丹东", "锦州", "延吉", "齐齐哈尔", "牡丹江", "佳木斯", "大庆",
    # 华东
    "徐州", "连云港", "常州", "南通", "盐城", "扬州", "无锡", "温州", "台州", "舟山",
    "义乌", "黄山", "阜阳", "烟台", "威海", "济宁", "临沂", "潍坊", "日照", "泉州",
    "武夷山", "赣州", "景德镇", "九江", "宁波",
    # 华中
    "洛阳", "南阳", "宜昌", "襄阳", "恩施", "张家界", "常德", "怀化",
    # 华南
    "珠海", "汕头", "湛江", "梅州", "揭阳", "桂林", "柳州", "北海", "三亚", "金门",
    # 西南
    "绵阳", "宜宾", "泸州", "南充", "西昌", "九寨沟", "丽江", "大理", "西双版纳",
    "芒市", "腾冲", "遵义", "兴义", "林芝", "日喀则",
    # 西北
    "榆林", "汉中", "敦煌", "嘉峪关", "格尔木", "中卫", "喀什", "库尔勒", "伊宁",
    "阿勒泰", "和田", "克拉玛依",
    # 港澳台
    "香港", "澳门", "台北", "高雄", "台中",
    # 主要国际枢纽
    "东京", "大阪", "首尔", "新加坡", "曼谷", "吉隆坡", "悉尼", "墨尔本",
    "伦敦", "巴黎", "法兰克福", "阿姆斯特丹", "莫斯科", "迪拜", "多哈",
    "纽约", "洛杉矶", "旧金山", "西雅图", "温哥华", "多伦多",
}

AIRPORT_ALIASES: dict[str, str] = {
    "beijing": "北京", "shanghai": "上海", "guangzhou": "广州", "shenzhen": "深圳",
    "chengdu": "成都", "hangzhou": "杭州", "xian": "西安", "kunming": "昆明",
    "nanjing": "南京", "wuhan": "武汉", "qingdao": "青岛", "xiamen": "厦门",
    "dalian": "大连", "shenyang": "沈阳", "harbin": "哈尔滨", "chongqing": "重庆",
    "tianjin": "天津", "lhasa": "拉萨", "urumqi": "乌鲁木齐", "sanya": "三亚",
    "haikou": "海口", "hongkong": "香港", "hong kong": "香港", "macau": "澳门",
    "tokyo": "东京", "osaka": "大阪", "seoul": "首尔", "singapore": "新加坡",
    "bangkok": "曼谷", "london": "伦敦", "paris": "巴黎", "dubai": "迪拜",
    "new york": "纽约", "los angeles": "洛杉矶", "san francisco": "旧金山",
}


def _lookup_airport(name: str) -> str | None:
    """机场白名单查找。返回规范名，未命中返回 None。"""
    raw = (name or "").strip()
    if raw in AIRPORTS:
        return raw
    norm = _normalize(raw)
    for alias, canonical in AIRPORT_ALIASES.items():
        if _normalize(alias) == norm:
            return canonical
    for canonical in AIRPORTS:
        if _normalize(canonical) == norm:
            return canonical
    return None


def _normalize(name: str) -> str:
    return (name or "").strip().lower().replace(" ", "").replace("市", "").replace("港", "")


def _lookup_port(name: str) -> tuple[str, float, float] | None:
    """白名单查找。返回 (规范名, lat, lon)，未命中返回 None。"""
    raw = (name or "").strip()
    if raw in PORTS:
        return raw, *PORTS[raw]
    norm = _normalize(raw)
    for alias, canonical in PORT_ALIASES.items():
        if _normalize(alias) == norm:
            return canonical, *PORTS[canonical]
    for canonical in PORTS:
        if _normalize(canonical) == norm:
            return canonical, *PORTS[canonical]
    return None


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class Missing:
    """一条缺失证据。code 供程序判断，location/role 供人阅读。"""
    code: str          # date_out_of_range | atmos_unavailable | wind_missing
                       # | visibility_missing | wave_height_missing | location_unresolved
    role: str          # origin | destination
    location: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {"code": self.code, "role": self.role, "location": self.location, "detail": self.detail}


@dataclass
class TraceStep:
    """确定性预取的一步，进入最终 trace（kind="prefetch"）。"""
    name: str
    args: dict
    ok: bool
    latency_ms: int
    summary: str = ""
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "kind": "prefetch", "name": self.name, "args": self.args, "ok": self.ok,
            "latency_ms": self.latency_ms, "summary": self.summary, "error": self.error,
        }


@dataclass
class EvidenceBundle:
    target_date: str
    transport: str
    vessel_type: str | None
    locations: dict = field(default_factory=dict)
    missing: list[Missing] = field(default_factory=list)
    trace: list[TraceStep] = field(default_factory=list)
    quality: str = "full"          # full | partial

    @property
    def ok(self) -> bool:
        return not self.missing

    def as_dict(self) -> dict:
        return {
            "target_date": self.target_date,
            "transport": self.transport,
            "vessel_type": self.vessel_type,
            "locations": self.locations,
            "sufficiency": {
                "ok": self.ok,
                "quality": self.quality,
                "missing": [m.as_dict() for m in self.missing],
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EvidenceBundle":
        """从 API 响应里的 evidence 字段还原，供 L2 做自洽性复核。"""
        suff = data.get("sufficiency", {})
        b = cls(
            target_date=data.get("target_date", ""),
            transport=data.get("transport", ""),
            vessel_type=data.get("vessel_type"),
            locations=data.get("locations", {}),
            quality=suff.get("quality", "full"),
        )
        b.missing = [Missing(m.get("code", ""), m.get("role", ""), m.get("location", ""),
                             m.get("detail", "")) for m in suff.get("missing", [])]
        return b

    def for_model(self) -> str:
        """喂给 LLM 的结构化证据（不含 trace，避免污染上下文）。"""
        return json.dumps(
            {"target_date": self.target_date, "locations": self.locations},
            ensure_ascii=False, indent=2,
        )


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 坐标解析：白名单 → Geocoding（必须经海洋 API 回验）→ 失败
# ---------------------------------------------------------------------------
def _geocode(name: str) -> tuple[float, float, str] | None:
    url = f"{GEOCODE_BASE}?{urllib.parse.urlencode({'name': name, 'count': 5, 'language': 'zh'})}"
    try:
        results = _get_json(url).get("results") or []
    except Exception:
        return None
    if not results:
        return None
    # 同名地点很常见（实测搜"三亚"返回海南/广西/玉林三个），取人口最多的那个，
    # 但这只是猜测——真正的保险是下面的海洋 API 回验。
    best = max(results, key=lambda r: r.get("population", 0) or 0)
    return float(best["latitude"]), float(best["longitude"]), best.get("name", name)


def resolve_location(name: str, mode: str) -> tuple[dict | None, Missing | None]:
    """
    mode="marine"   ：必须解析出一个真实海域坐标（港口白名单 → Geocoding + 海洋 API 回验）
    mode="aviation" ：必须命中民航机场白名单

    两条路径都要有可达性契约。加机场白名单之前航空侧是零校验的——
    任何地名都能查到风速和能见度，所以"上海 → 南极 飞机"会返回"低风险，建议出行"。
    """
    if mode == "aviation":
        hit = _lookup_airport(name)
        if hit:
            return {"lat": None, "lon": None, "source": "airport_whitelist",
                    "matched_name": hit}, None
        return None, Missing("no_airport", "", name,
                             "该地点不在已收录的民航机场清单内")

    hit = _lookup_port(name)
    if hit:
        canonical, lat, lon = hit
        return {"lat": lat, "lon": lon, "source": "whitelist", "matched_name": canonical}, None

    geo = _geocode(name)
    if not geo:
        return None, Missing("location_unresolved", "", name, "地名无法解析为坐标")

    lat, lon, matched = geo
    # 回验：海洋 API 对内陆点返回全 null，用这个行为确认解析结果确实落在海域。
    # 兜底可以不准，但不能静默地不准。
    probe, _ = fetch_marine(lat, lon, datetime.date.today().isoformat())
    if probe is None:
        # 能解析出坐标但不是海域 —— 这是【范围边界】不是【系统失败】，措辞要分清楚
        return None, Missing("not_coastal", "", name,
                             f"该地点（{lat:.2f}, {lon:.2f}）不临海")
    return {"lat": lat, "lon": lon, "source": "geocoding", "matched_name": matched}, None


# ---------------------------------------------------------------------------
# 单位换算
# ---------------------------------------------------------------------------
def kmh_to_ms(kmh: float) -> float:
    return kmh / 3.6


def ms_to_beaufort(ms: float) -> int:
    thresholds = [0.3, 1.6, 3.4, 5.5, 8.0, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7]
    for i, t in enumerate(thresholds):
        if ms < t:
            return i
    return 12


# ---------------------------------------------------------------------------
# 大气数据（wttr.in），按目标日期精确对齐
# ---------------------------------------------------------------------------
def fetch_atmos(query: str, target_date: str) -> tuple[dict | None, str | None]:
    """
    query 可以是地名，也可以是 "lat,lon"（白名单命中时用坐标，保证与海洋数据同点位）。
    返回 (atmos_dict, error)。atmos_dict 只包含 target_date 当天的数据。
    """
    url = f"{WTTR_BASE}/{urllib.parse.quote(query)}?format=j1"
    try:
        data = _get_json(url)
    except Exception as e:
        return None, f"wttr.in 请求失败: {e}"

    days = data.get("weather", [])
    available = [d.get("date", "") for d in days]
    day = next((d for d in days if d.get("date") == target_date), None)
    if day is None:
        return None, f"date_out_of_range:{target_date} 不在预报范围 {available}"

    hourly = day.get("hourly", [])

    winds = [float(h.get("windspeedKmph", 0) or 0) for h in hourly]
    gusts = [float(h.get("WindGustKmph", 0) or 0) for h in hourly]
    max_wind_ms = round(kmh_to_ms(max(winds)), 1) if winds else None
    max_gust_ms = round(kmh_to_ms(max(gusts)), 1) if any(gusts) else None

    # wttr.in 用 visibility == 0 表示缺失，不是字面的 0 km
    vis_valid = [float(h.get("visibility", 0) or 0) for h in hourly]
    vis_valid = [v for v in vis_valid if v > 0]
    min_vis_km = min(vis_valid) if vis_valid else None
    quality = "full"

    if min_vis_km is None and target_date == datetime.date.today().isoformat():
        # 目标日就是今天时，可以退回实况观测值，但要标记为 partial
        cur_vis = float(data.get("current_condition", [{}])[0].get("visibility", 0) or 0)
        if cur_vis > 0:
            min_vis_km = cur_vis
            quality = "partial"

    descs = []
    for h in hourly:
        dl = h.get("lang_zh") or h.get("weatherDesc") or []
        if dl:
            v = dl[0].get("value", "")
            if v:
                descs.append(v)
    description = "、".join(dict.fromkeys(descs)) or "N/A"

    return {
        "date": target_date,
        "max_wind_speed_ms": max_wind_ms,
        "max_wind_beaufort": ms_to_beaufort(max_wind_ms) if max_wind_ms is not None else None,
        "max_gust_ms": max_gust_ms,
        "min_visibility_km": min_vis_km,
        "max_temp_c": day.get("maxtempC"),
        "min_temp_c": day.get("mintempC"),
        "description": description,
        "quality": quality,
        "source": "wttr.in",
        "fetched_at": _now_iso(),
    }, None


ARCHIVE_BASE = os.getenv("VOYAGEGUARD_ARCHIVE_BASE",
                        "https://archive-api.open-meteo.com/v1/archive")


def fetch_atmos_archive(lat: float, lon: float, target_date: str) -> tuple[dict | None, str | None]:
    """
    历史大气数据（ERA5 再分析），供 L4 黄金数据集重建过去某天的风况。
    wttr.in 只有未来 3 天，做历史回验必须换源。

    【重要】ERA5 是【再分析】——事后用观测重建的"实际发生了什么"，
    不是决策当时可得的【预报】。所以基于它的评测说明的是"阈值与实际停航是否吻合"，
    而不是"当时的预报能否预测停航"。这两件事不能混为一谈。
    另外 ERA5 网格约 0.25°，在海峡等地形复杂处会平滑掉局地峰值风。
    """
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": target_date, "end_date": target_date,
        "hourly": "wind_speed_10m,wind_gusts_10m,weather_code",
        "wind_speed_unit": "ms", "timezone": "auto",
    }
    url = f"{ARCHIVE_BASE}?{urllib.parse.urlencode(params)}"
    try:
        data = _get_json(url)
    except Exception as e:
        return None, f"Open-Meteo Archive 请求失败: {e}"
    if "error" in data:
        return None, f"Open-Meteo Archive 返回错误: {data.get('reason')}"

    hourly = data.get("hourly", {})
    winds = [v for v in (hourly.get("wind_speed_10m") or []) if v is not None]
    gusts = [v for v in (hourly.get("wind_gusts_10m") or []) if v is not None]
    codes = [v for v in (hourly.get("weather_code") or []) if v is not None]
    if not winds:
        return None, "该日期/坐标无历史风速数据"

    # WMO weather code 95-99 = 雷暴
    desc = "雷暴" if any(95 <= c <= 99 for c in codes) else ""
    return {
        "date": target_date,
        "max_wind_speed_ms": round(max(winds), 1),
        "max_wind_beaufort": ms_to_beaufort(max(winds)),
        "max_gust_ms": round(max(gusts), 1) if gusts else None,
        "min_visibility_km": None,          # ERA5 的 visibility 在多数海域为空
        "max_temp_c": None, "min_temp_c": None,
        "description": desc,
        "quality": "archive",
        "source": "open-meteo-era5-archive",
        "fetched_at": _now_iso(),
    }, None


# ---------------------------------------------------------------------------
# 海洋数据（Open-Meteo Marine）
# ---------------------------------------------------------------------------
def fetch_marine(lat: float, lon: float, target_date: str) -> tuple[dict | None, str | None]:
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "wave_height,wave_period,swell_wave_height",
        "start_date": target_date, "end_date": target_date,
        "timezone": "auto",
    }
    url = f"{MARINE_BASE}?{urllib.parse.urlencode(params)}"
    try:
        data = _get_json(url)
    except Exception as e:
        return None, f"Open-Meteo Marine 请求失败: {e}"

    if "error" in data:
        return None, f"Open-Meteo Marine 返回错误: {data.get('reason')}"

    hourly = data.get("hourly", {})
    waves = [v for v in (hourly.get("wave_height") or []) if v is not None]
    if not waves:
        # 内陆点会走到这里 —— 全 null 是 Open-Meteo 对非海域的正常行为
        return None, "该坐标无浪高数据（非海域或超出模式覆盖范围）"

    periods = [v for v in (hourly.get("wave_period") or []) if v is not None]
    swells = [v for v in (hourly.get("swell_wave_height") or []) if v is not None]

    return {
        "date": target_date,
        "max_wave_height_m": round(max(waves), 2),
        "mean_wave_height_m": round(sum(waves) / len(waves), 2),
        "max_wave_period_s": round(max(periods), 1) if periods else None,
        "max_swell_height_m": round(max(swells), 2) if swells else None,
        "source": "open-meteo-marine",
        "fetched_at": _now_iso(),
    }, None


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------
def _timed(fn, *args):
    t0 = time.perf_counter()
    result, err = fn(*args)
    return result, err, int((time.perf_counter() - t0) * 1000)


def _timed_parallel(jobs: list[tuple]) -> list[tuple]:
    """
    并发执行若干 (fn, *args)，返回 [(result, err, ms), ...]，顺序与输入一致。

    一条船舶航线要取 3 个点 × (大气 + 海洋) = 6 次网络调用，彼此完全独立。
    串行约 4.2s，并发后约 1s。
    """
    if not jobs:
        return []
    with cf.ThreadPoolExecutor(max_workers=min(6, len(jobs))) as ex:
        return list(ex.map(lambda j: _timed(j[0], *j[1:]), jobs))


def build_evidence(
    origin: str,
    destination: str,
    target_date: str,
    transport: str,
    vessel_type: str | None = None,
) -> EvidenceBundle:
    """确定性预取两端（船舶再加航线中点）的必需证据，并判定充分性。不调用 LLM。"""
    bundle = EvidenceBundle(target_date=target_date, transport=transport, vessel_type=vessel_type)
    need_marine = transport == "ship"
    mode = "marine" if need_marine else "aviation"

    # ── 阶段一：解析坐标（白名单命中时零网络；兜底才走 Geocoding）────────
    points: list[dict] = []          # 待取数的点
    for role, name in (("origin", origin), ("destination", destination)):
        entry: dict = {"name": name, "role": role, "errors": []}
        bundle.locations[name] = entry

        resolved, miss = resolve_location(name, mode)
        if miss is not None:
            miss.role = role
            bundle.missing.append(miss)
            entry["errors"].append(miss.detail)
            bundle.trace.append(TraceStep(
                "resolve_location", {"name": name, "mode": mode}, False, 0, error=miss.detail))
            continue

        entry["resolved"] = resolved
        bundle.trace.append(TraceStep(
            "resolve_location", {"name": name, "mode": mode}, True, 0,
            summary=f"{resolved['matched_name']} via {resolved['source']}"))
        # 白名单命中时用坐标查大气，保证与海洋数据同点位
        query = (f"{resolved['lat']},{resolved['lon']}"
                 if resolved.get("lat") is not None else name)
        points.append({"entry": entry, "name": name, "role": role,
                       "resolved": resolved, "query": query, "required": True})

    # 航线中点：跨海航线的风险由开阔水域中段决定，两端港口是遮蔽水域会低估。
    # 实测 2026-02-05 渤海海峡停航当天，烟台港 8.0 m/s 而海峡中部 15.2 m/s。
    # best-effort —— 取不到不触发 abstention。
    if need_marine:
        coords = [p["resolved"] for p in points if p["resolved"].get("lat") is not None]
        if len(coords) >= 2:
            lat = round(sum(c["lat"] for c in coords[:2]) / 2, 4)
            lon = round(sum(c["lon"] for c in coords[:2]) / 2, 4)
            mid: dict = {"name": MIDPOINT_KEY, "role": "midpoint", "errors": [],
                         "resolved": {"lat": lat, "lon": lon, "source": "derived",
                                      "matched_name": MIDPOINT_KEY}}
            points.append({"entry": mid, "name": MIDPOINT_KEY, "role": "midpoint",
                           "resolved": mid["resolved"], "query": f"{lat},{lon}",
                           "required": False})

    # ── 阶段二：并发取数 ──────────────────────────────────────────────────
    # 一条船舶航线是 3 个点 × (大气 + 海洋) = 6 次独立的网络调用。
    # 串行约 4.2s，并发约 1s —— 而 LLM 那一步就要 27s，能省的都得省。
    jobs, meta = [], []
    for pt in points:
        jobs.append((fetch_atmos, pt["query"], target_date))
        meta.append(("atmos", pt))
        if need_marine and pt["resolved"].get("lat") is not None:
            jobs.append((fetch_marine, pt["resolved"]["lat"], pt["resolved"]["lon"], target_date))
            meta.append(("marine", pt))
    results = _timed_parallel(jobs)

    # ── 阶段三：组装、记 trace、判定充分性 ────────────────────────────────
    for (kind, pt), (data, err, ms) in zip(meta, results):
        entry, name, role, required = pt["entry"], pt["name"], pt["role"], pt["required"]

        if kind == "atmos":
            args = {"location": pt["query"], "date": target_date}
            if role == "midpoint":
                args["role"] = "midpoint"
            bundle.trace.append(TraceStep(
                "get_weather_forecast", args, data is not None, ms,
                summary=(f"wind {data['max_wind_speed_ms']} m/s, "
                         f"gust {data['max_gust_ms']} m/s" if data else ""), error=err))
            if data is None:
                entry["errors"].append(err or "")
                if required:
                    code = ("date_out_of_range" if (err or "").startswith("date_out_of_range")
                            else "atmos_unavailable")
                    bundle.missing.append(Missing(code, role, name, err or ""))
                continue
            entry["atmos"] = data
            if data["quality"] == "partial":
                bundle.quality = "partial"
            if required:
                if data["max_wind_speed_ms"] is None:
                    bundle.missing.append(Missing("wind_missing", role, name, "未取到风速"))
                # 能见度是低能见度起飞门槛的判据，缺了就不能排除高风险 —— 不能默认为安全
                if transport == "plane" and data["min_visibility_km"] is None:
                    bundle.missing.append(Missing("visibility_missing", role, name, "未取到能见度"))
        else:
            args = {"lat": pt["resolved"]["lat"], "lon": pt["resolved"]["lon"], "date": target_date}
            if role == "midpoint":
                args["role"] = "midpoint"
            bundle.trace.append(TraceStep(
                "get_marine_forecast", args, data is not None, ms,
                summary=(f"wave max {data['max_wave_height_m']} m" if data else ""), error=err))
            if data is None:
                entry["errors"].append(err or "")
                if required:
                    bundle.missing.append(Missing("wave_height_missing", role, name, err or ""))
                continue
            entry["marine"] = data

    # 中点只在真的取到东西时才进 locations（否则不该出现在证据里）
    for pt in points:
        if pt["role"] == "midpoint" and (pt["entry"].get("atmos") or pt["entry"].get("marine")):
            bundle.locations[MIDPOINT_KEY] = pt["entry"]

    return bundle


MIDPOINT_KEY = "航线中点"
