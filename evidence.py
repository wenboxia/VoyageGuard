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
import math
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
    # ── 第五轮扩充：覆盖率本身就是产品质量 ──────────────────────────────
    # Open-Meteo Geocoding 对中国地级市覆盖很差（population 字段全为 null，
    # 返回的是同名村镇：阳江→江苏、营口→黑龙江，泉州直接返回 0 条），兜底基本失效。
    # 所以海事覆盖实际等于这张白名单，必须把它扩够。每条都经 L3 验证能取到浪高。
    "万宁": (18.85, 110.6),
    "上川岛": (21.7, 112.75),
    "东山": (23.65, 117.5),
    "东方": (19.05, 108.55),
    "东营": (38.05, 119.1),
    "中山": (22.3, 113.55),
    "丹东": (39.87, 124.05),
    "儋州": (19.75, 109.1),
    "南澳": (23.45, 117.15),
    "唐山": (39.05, 118.95),
    "大长山岛": (39.25, 122.6),
    "宁德": (26.65, 119.9),
    "崇明": (31.55, 121.85),
    "平潭岛": (25.55, 119.85),
    "庙岛": (37.95, 120.7),
    "惠州": (22.6, 114.6),
    "文昌": (19.75, 111.1),
    "桂山岛": (22.15, 113.8),
    "汕尾": (22.7, 115.4),
    "江门": (21.9, 113.05),
    "洋山": (30.62, 122.07),
    "洋浦": (19.75, 109.15),
    "淮安": (34.2, 120.3),
    "滨州": (38.15, 118.2),
    "漳州": (24.2, 118.1),
    "潍坊": (37.3, 119.2),
    "獐子岛": (39.1, 122.7),
    "琼海": (19.2, 110.65),
    "盐城": (33.85, 120.75),
    "盘锦": (40.75, 121.75),
    "茂名": (21.35, 111.05),
    "莆田": (25.2, 119.2),
    "营口": (40.3, 122.1),
    "葫芦岛": (40.68, 120.9),
    "钦州": (21.6, 108.65),
    "锦州": (40.75, 121.2),
    "防城港": (21.55, 108.35),
    "阳江": (21.7, 111.9),
    "陵水": (18.4, 110.1),
    "黄骅": (38.3, 117.9),
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
# 民航机场白名单 —— 从官方航空气象数据源导出，不是手写的
# ---------------------------------------------------------------------------
# 为什么必须有这个清单：航空判据只要风速和能见度，而【任何地名都能查到风速和能见度】。
# 在加它之前，"上海 → 南极 飞机" 会返回"低风险，建议出行"。船舶侧因为需要浪高，
# 顺带获得了"这里是不是海"的校验；航空侧没有等价的天然校验。
#
# 名单直接来自 aviationweather.gov 的站点接口（中国境内发布 METAR/TAF 的机场），
# 每个 ICAO 都经该接口验证过。这样「我们覆盖哪些机场」和「哪些机场有官方气象数据」
# 由构造保证是同一件事，不会漂移。
#
# 清单只保留【实测确认正在发布 TAF 的机场】。站点库里另有 20 个标称有 METAR/TAF
# 但实际没有在国际网上发报（含拉萨 ZULS），已剔除 —— 覆盖范围必须等于数据可得性，
# 不能靠一张"理论上应该有数据"的名单。
# ── 两份清单，各管一件事 ──────────────────────────────────────────────────
#
# AIRPORT_CITIES（宽，149 个）—— **可达性契约**：这个地方到底有没有民航机场。
#   存在的理由：航空判据只要风速和能见度，而任何地名都能查到这两样。没有它，
#   "上海 → 南极 飞机" 会返回"低风险，建议出行"。
#
# AIRPORTS_WX（窄，38 个）—— **数据源升级**：哪些机场发布官方 METAR/TAF。
#   命中就用官方机场气象；没命中就回落到城市地面天气，并在证据里标明数据源。
#
# 为什么不把两者合一：合一意味着"没有官方机场气象 = 不能评估"，
# 覆盖会从任意城市塌缩到 38 个机场。而对一个决策工具来说，**覆盖率本身就是产品质量**——
# 一个大多数查询都回答"证据不足"的工具不会让任何人更安全。
#
# 而且我们的三条航空判据里，只有 LVTO 能见度那条真的需要跑道观测；
# 大风预警本来就是**对区域**发布的，城市地面风正是它的输入。
AIRPORT_CITIES: set[str] = {
    "北京", "上海", "天津", "重庆", "广州", "深圳", "成都", "杭州",
    "西安", "昆明", "南京", "郑州", "武汉", "长沙", "青岛", "厦门",
    "大连", "沈阳", "哈尔滨", "济南", "石家庄", "太原", "呼和浩特", "长春",
    "合肥", "福州", "南昌", "南宁", "海口", "贵阳", "拉萨", "兰州",
    "西宁", "银川", "乌鲁木齐", "唐山", "秦皇岛", "邯郸", "张家口", "大同",
    "运城", "包头", "鄂尔多斯", "赤峰", "呼伦贝尔", "鞍山", "丹东", "锦州",
    "延吉", "齐齐哈尔", "牡丹江", "佳木斯", "大庆", "徐州", "连云港", "常州",
    "南通", "盐城", "扬州", "无锡", "温州", "台州", "舟山", "义乌",
    "黄山", "阜阳", "烟台", "威海", "济宁", "临沂", "潍坊", "日照",
    "泉州", "武夷山", "赣州", "景德镇", "九江", "宁波", "洛阳", "南阳",
    "宜昌", "襄阳", "恩施", "张家界", "常德", "怀化", "珠海", "汕头",
    "湛江", "梅州", "揭阳", "桂林", "柳州", "北海", "三亚", "金门",
    "绵阳", "宜宾", "泸州", "南充", "西昌", "九寨沟", "丽江", "大理",
    "西双版纳", "芒市", "腾冲", "遵义", "兴义", "林芝", "日喀则", "榆林",
    "汉中", "敦煌", "嘉峪关", "格尔木", "中卫", "喀什", "库尔勒", "伊宁",
    "阿勒泰", "和田", "克拉玛依", "香港", "澳门", "台北", "高雄", "台中",
    "东京", "大阪", "首尔", "新加坡", "曼谷", "吉隆坡", "悉尼", "墨尔本",
    "伦敦", "巴黎", "法兰克福", "阿姆斯特丹", "莫斯科", "迪拜", "多哈", "纽约",
    "洛杉矶", "旧金山", "西雅图", "温哥华", "多伦多",
}


AIRPORTS_WX: dict[str, tuple[str, float, float]] = {
    "北京": ("ZBAA", 40.082, 116.603),   # Beijing Intl
    "大兴": ("ZBAD", 39.501, 116.412),   # Beijing/Daxing Arpt
    "呼和浩特": ("ZBHH", 40.854, 111.827),   # Hohhot/Baita Intl
    "石家庄": ("ZBSJ", 38.281, 114.697),   # Zhengding Arpt
    "天津": ("ZBTJ", 39.124, 117.346),   # Tianjin/Binhai Intl
    "太原": ("ZBYN", 37.747, 112.628),   # Taiyuan/Wusu Intl
    "广州": ("ZGGG", 23.392, 113.307),   # Guangzhou/Baiyun Intl
    "长沙": ("ZGHA", 28.18, 113.219),   # Changsha/Huanghua Arpt
    "桂林": ("ZGKL", 25.22, 110.04),   # Guilin/Liangjiang Intl
    "南宁": ("ZGNN", 22.609, 108.173),   # Nanning/Wuwei Intl
    "揭阳": ("ZGOW", 23.55, 116.505),   # Jieyang/Chaoshan Intl
    "潮汕": ("ZGOW", 23.55, 116.505),   # Jieyang/Chaoshan Intl
    "汕头": ("ZGOW", 23.55, 116.505),   # Jieyang/Chaoshan Intl
    "深圳": ("ZGSZ", 22.639, 113.803),   # Shenzhen/Boan Intl
    "郑州": ("ZHCC", 34.52, 113.834),   # Zhengzhou/Xinzheng Arpt
    "鄂州": ("ZHEC", 30.34237, 115.03893),   # Ezhou Huahu Arpt
    "武汉": ("ZHHH", 30.783, 114.205),   # Wuhan/Tianhe Intl
    "海口": ("ZJHK", 19.934, 110.445),   # Haikou/Meilan Intl
    "三亚": ("ZJSY", 18.303, 109.412),   # Sanya/Phoenix Intl
    "兰州": ("ZLLL", 36.513, 103.623),   # Lanzhou/Zhongchuan Arpt
    "西安": ("ZLXY", 34.449, 108.752),   # Xianyang Intl
    "昆明": ("ZPPP", 25.107, 102.934),   # Kunming/Changshui Intl
    "厦门": ("ZSAM", 24.546, 118.131),   # Xiamen-Gaoqi Intl
    "福州": ("ZSFZ", 25.936, 119.666),   # Fuzhou/Changle Intl
    "杭州": ("ZSHC", 30.229, 120.434),   # Hangzhou/Xiaoshan Intl
    "济南": ("ZSJN", 36.856, 117.206),   # Jinan Yaoqiang Intl
    "宁波": ("ZSNB", 29.827, 121.462),   # Ningbo/Lishe Intl
    "南京": ("ZSNJ", 31.739, 118.863),   # Nanjing/Lukou Intl
    "合肥": ("ZSOF", 31.99, 116.965),   # Hefei/Xinqiao Intl
    "上海": ("ZSPD", 31.146, 121.8),   # Shanghai/Pudong Intl
    "青岛": ("ZSQD", 36.362, 120.087),   # Qingdao/Jiaodong Arpt
    "重庆": ("ZUCK", 29.718, 106.639),   # Chongqing/Jiangbei Intl
    "贵阳": ("ZUGY", 26.538, 106.801),   # Guizhou/Longdongbao Arpt
    "成都": ("ZUUU", 30.576, 103.95),   # Chengdu/Shuangliu Intl
    "喀什": ("ZWSH", 39.542, 76.019),   # Kashgar Arpt
    "乌鲁木齐": ("ZWWW", 43.907, 87.474),   # Ürümqi/Diwopu Arpt
    "长春": ("ZYCC", 43.993, 125.682),   # Changchun/Longjia Intl
    "哈尔滨": ("ZYHB", 45.628, 126.259),   # Harbin/Taiping Arpt
    "大连": ("ZYTL", 38.961, 121.556),   # Dalian/Zhoushuizi Intl
    "沈阳": ("ZYTX", 41.639, 123.485),   # Shenyang/Taoxian Intl
}

AIRPORT_ALIASES: dict[str, str] = {
    "beijing": "北京", "shanghai": "上海", "guangzhou": "广州", "shenzhen": "深圳",
    "chengdu": "成都", "hangzhou": "杭州", "xian": "西安", "kunming": "昆明",
    "nanjing": "南京", "wuhan": "武汉", "qingdao": "青岛", "xiamen": "厦门",
    "dalian": "大连", "shenyang": "沈阳", "harbin": "哈尔滨", "chongqing": "重庆",
    "tianjin": "天津", "lhasa": "拉萨", "urumqi": "乌鲁木齐", "sanya": "三亚",
    "haikou": "海口", "changsha": "长沙", "zhengzhou": "郑州", "jinan": "济南",
    "PEK": "北京", "PKX": "大兴", "PVG": "上海", "SHA": "上海", "CAN": "广州",
    "SZX": "深圳", "CTU": "成都", "HGH": "杭州", "XIY": "西安", "KMG": "昆明",
}


def _lookup_airport_city(name: str) -> str | None:
    """可达性清单查找（宽）。返回规范名，未命中返回 None。"""
    raw = (name or "").strip()
    if raw in AIRPORT_CITIES:
        return raw
    norm = _normalize(raw)
    for alias, canonical in AIRPORT_ALIASES.items():
        if _normalize(alias) == norm and canonical in AIRPORT_CITIES:
            return canonical
    for canonical in AIRPORT_CITIES:
        if _normalize(canonical) == norm:
            return canonical
    return None


def _lookup_airport(name: str) -> tuple[str, str, float, float] | None:
    """返回 (规范中文名, ICAO, lat, lon)，未命中返回 None。清单内的机场全部发布 TAF。"""
    raw = (name or "").strip()
    if raw in AIRPORTS_WX:
        return raw, *AIRPORTS_WX[raw]
    norm = _normalize(raw)
    for alias, canonical in AIRPORT_ALIASES.items():
        if _normalize(alias) == norm and canonical in AIRPORTS_WX:
            return canonical, *AIRPORTS_WX[canonical]
    for canonical in AIRPORTS_WX:
        if _normalize(canonical) == norm:
            return canonical, *AIRPORTS_WX[canonical]
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
    route: dict = field(default_factory=dict)   # 航路采样覆盖情况，如实透出
    sigmets: list = field(default_factory=list) # 航路穿越的生效中重要气象情报

    @property
    def ok(self) -> bool:
        return not self.missing

    def as_dict(self) -> dict:
        return {
            "target_date": self.target_date,
            "transport": self.transport,
            "vessel_type": self.vessel_type,
            "locations": self.locations,
            "route": self.route,
            "sigmets": self.sigmets,
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
        if hit:      # 有官方机场气象
            canonical, icao, lat, lon = hit
            return {"lat": lat, "lon": lon, "source": "airport_whitelist",
                    "matched_name": canonical, "icao": icao}, None
        canonical = _lookup_airport_city(name)
        if canonical:  # 有机场但不发布 METAR/TAF —— 回落到城市地面天气
            return {"lat": None, "lon": None, "source": "airport_city",
                    "matched_name": canonical}, None
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


AVWX_BASE = os.getenv("VOYAGEGUARD_AVWX_BASE", "https://aviationweather.gov/api/data")

KT_TO_MS = 0.514444
SM_TO_KM = 1.60934
CN_UTC_OFFSET_H = 8      # 名单内全是中国境内机场，用固定 +8 换算"当地那一天"


def _visib_km(v) -> float | None:
    """METAR/TAF 的能见度是法定英里；'6+' 表示 10km 以上（无限制）。"""
    if v is None:
        return None
    if isinstance(v, str):
        v = v.replace("+", "").strip()
        if not v:
            return None
    try:
        return round(float(v) * SM_TO_KM, 2)
    except (TypeError, ValueError):
        return None


def _day_window_utc(target_date: str) -> tuple[int, int]:
    d = datetime.date.fromisoformat(target_date)
    start = datetime.datetime.combine(d, datetime.time(0, 0),
                                      tzinfo=datetime.timezone.utc) \
        - datetime.timedelta(hours=CN_UTC_OFFSET_H)
    return int(start.timestamp()), int(start.timestamp()) + 86400


def fetch_airport_wx(icao: str, target_date: str) -> tuple[dict | None, str | None]:
    """
    机场气象：官方 METAR（实况）与 TAF（预报）。

    为什么换掉 wttr.in：METAR/TAF 是【民航官方气象产品】，取的是机场跑道观测
    而不是城市天气，能见度是航空口径，风速单位可直接换算。wttr.in 给的是城市地面天气。

    代价（写进 README 的已知取舍）：
      · 只有名单内的机场有数据（实测确认在发报的 38 个）
      · TAF 只覆盖约 30 小时 —— 超出范围必须弃权，不能拿实况冒充预报
    """
    today = datetime.date.today().isoformat()
    lo, hi = _day_window_utc(target_date)

    winds, gusts, vis, wx = [], [], [], []
    source = None

    if True:
        try:
            tafs = _get_json(f"{AVWX_BASE}/taf?ids={icao}&format=json")
        except Exception as e:
            return None, f"TAF 请求失败: {e}"
        periods = [f for t in (tafs or []) for f in (t.get("fcsts") or [])
                   if f.get("timeTo", 0) > lo and f.get("timeFrom", 0) < hi]
        if not periods and target_date != today:
            return None, f"beyond_taf_horizon:{target_date} 超出该机场 TAF 的预报范围"
        for f in periods:
            if f.get("wspd") is not None:
                winds.append(f["wspd"] * KT_TO_MS)
            if f.get("wgst") is not None:
                gusts.append(f["wgst"] * KT_TO_MS)
            v = _visib_km(f.get("visib"))
            if v is not None:
                vis.append(v)
            if f.get("wxString"):
                wx.append(str(f["wxString"]))
        if periods:
            source = "aviationweather-taf"

    if target_date == today:
        try:
            metars = _get_json(f"{AVWX_BASE}/metar?ids={icao}&format=json")
        except Exception:
            metars = []
        for m in (metars or []):
            if m.get("wspd") is not None:
                winds.append(m["wspd"] * KT_TO_MS)
            if m.get("wgst") is not None:
                gusts.append(m["wgst"] * KT_TO_MS)
            v = _visib_km(m.get("visib"))
            if v is not None:
                vis.append(v)
            if m.get("wxString"):
                wx.append(str(m["wxString"]))
            source = source or "aviationweather-metar"

    if not winds:
        return None, f"{icao} 无可用的 METAR/TAF 数据"

    max_wind = round(max(winds), 1)
    return {
        "date": target_date,
        "max_wind_speed_ms": max_wind,
        "max_wind_beaufort": ms_to_beaufort(max_wind),
        "max_gust_ms": round(max(gusts), 1) if gusts else None,
        "min_visibility_km": min(vis) if vis else None,
        "max_temp_c": None, "min_temp_c": None,
        "description": "、".join(dict.fromkeys(wx)) or "N/A",
        "quality": "full" if source == "aviationweather-taf" else "observation_only",
        "source": source or "aviationweather",
        "fetched_at": _now_iso(),
    }, None


def fetch_aviation_wx(icao: str | None, city: str, target_date: str) -> tuple[dict | None, str | None]:
    """
    航空气象：**官方优先，取不到就回落，并在证据里标明用的是哪一种。**

    优先级
      1. METAR/TAF（官方机场气象，38 个机场，TAF 约覆盖 30 小时）
      2. wttr.in 城市地面天气（覆盖任意城市、3 天）

    为什么不是"没有官方数据就弃权"：我们的三条航空判据里，只有 LVTO 能见度那条
    真的需要跑道观测；**大风预警本来就是对区域发布的，城市地面风正是它的输入**。
    为了改善其中一条而把覆盖砍掉 90%，不划算 —— 对决策工具来说覆盖率本身就是产品质量。

    数据源差异通过 evidence 里每个数值自带的 source 字段透出，前端可见。
    """
    if icao:
        data, err = fetch_airport_wx(icao, target_date)
        if data is not None:
            return data, None
        # 官方数据取不到（超出 TAF 范围 / 该机场当时没发报）→ 回落，不弃权
    data, err = fetch_atmos(city, target_date)
    if data is not None:
        # 标明这是城市地面天气而不是跑道观测，能见度是近似值
        data["quality"] = "city_surface"
    return data, err


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
ROUTE_SAMPLE_SPACING_KM = float(os.getenv("VOYAGEGUARD_ROUTE_SPACING_KM", "200"))
ROUTE_MAX_SAMPLES = int(os.getenv("VOYAGEGUARD_ROUTE_MAX_SAMPLES", "5"))


def great_circle_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _interpolate(lat1, lon1, lat2, lon2, frac: float) -> tuple[float, float]:
    """大圆插值（slerp）。短航线用线性也够，但长航线线性会明显偏离实际航路。"""
    p1, l1 = math.radians(lat1), math.radians(lon1)
    p2, l2 = math.radians(lat2), math.radians(lon2)
    d = 2 * math.asin(math.sqrt(
        math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2))
    if d == 0:
        return lat1, lon1
    a, b = math.sin((1 - frac) * d) / math.sin(d), math.sin(frac * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    return round(math.degrees(math.atan2(z, math.sqrt(x * x + y * y))), 4), \
           round(math.degrees(math.atan2(y, x)), 4)


def route_sample_points(lat1, lon1, lat2, lon2) -> list[tuple[float, float]]:
    """
    沿航线取若干中间采样点。

    为什么不是固定一个中点：跨海航线的风险由开阔水域中段决定，而白名单里的港口
    能组合出很长的航线（上海→三亚 1905 km）。一个中点代表两千公里是严重欠采样。
    真实的短程横渡（厦门-金门 23 km、渤海海峡 154 km）仍然只取 1 个点，行为不变。
    """
    dist = great_circle_km(lat1, lon1, lat2, lon2)
    n = max(1, min(ROUTE_MAX_SAMPLES, int(dist // ROUTE_SAMPLE_SPACING_KM)))
    return [_interpolate(lat1, lon1, lat2, lon2, (i + 1) / (n + 1)) for i in range(n)]


def fetch_sigmets() -> tuple[list[dict], str | None]:
    """当前生效的国际重要气象情报（SIGMET）。"""
    try:
        data = _get_json(f"{AVWX_BASE}/isigmet?format=json")
    except Exception as e:
        return [], f"SIGMET 请求失败: {e}"
    return [x for x in (data or []) if x.get("coords")], None


def _polygons(coords) -> list[list[dict]]:
    """SIGMET 的 coords 有两种形状：单个多边形，或多个多边形的嵌套列表。统一成列表的列表。"""
    if not coords:
        return []
    raw = [coords] if isinstance(coords[0], dict) else [c for c in coords if c]
    # 少数 SIGMET 的顶点坐标带 null，剔掉；剩不足 3 个点的多边形丢弃
    out = []
    for poly in raw:
        pts = [q for q in poly
               if isinstance(q, dict) and q.get("lat") is not None and q.get("lon") is not None]
        if len(pts) >= 3:
            out.append(pts)
    return out


def _point_in_polygon(lat: float, lon: float, poly: list[dict]) -> bool:
    """射线法。SIGMET 区域是经纬度多边形，跨度不大，平面近似足够。"""
    inside, n = False, len(poly)
    j = n - 1
    for i in range(n):
        yi, xi = poly[i]["lat"], poly[i]["lon"]
        yj, xj = poly[j]["lat"], poly[j]["lon"]
        if ((xi > lon) != (xj > lon)) and \
           (lat < (yj - yi) * (lon - xi) / (xj - xi) + yi):
            inside = not inside
        j = i
    return inside


def sigmets_on_route(lat1, lon1, lat2, lon2, samples: int = 20) -> list[dict]:
    """
    航路是否穿越生效中的 SIGMET。沿大圆取 samples+1 个点做点在多边形内判断。

    只在【查询当天】才有意义 —— SIGMET 有效期只有 4-6 小时。
    """
    active, err = fetch_sigmets()
    if err or not active:
        return []
    pts = [_interpolate(lat1, lon1, lat2, lon2, i / samples) for i in range(samples + 1)]
    pts = [(lat1, lon1)] + pts + [(lat2, lon2)]
    hits = []
    for sg in active:
        polys = _polygons(sg.get("coords"))
        if any(_point_in_polygon(p[0], p[1], poly) for poly in polys for p in pts):
            hits.append({
                "firId": sg.get("firId"), "firName": sg.get("firName"),
                "hazard": sg.get("hazard"), "qualifier": sg.get("qualifier"),
                "base": sg.get("base"), "top": sg.get("top"),
                "validTimeFrom": sg.get("validTimeFrom"),
                "validTimeTo": sg.get("validTimeTo"),
                "raw": sg.get("rawSigmet"),
            })
    return hits


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
        query = (name if resolved.get("icao")
                 else (f"{resolved['lat']},{resolved['lon']}"
                       if resolved.get("lat") is not None else name))
        points.append({"entry": entry, "name": name, "role": role,
                       "resolved": resolved, "query": query, "required": True})

    # 航路采样：跨海航线的风险由开阔水域中段决定，两端港口是遮蔽水域会低估。
    # 实测 2026-02-05 渤海海峡停航当天，烟台港 8.0 m/s 而海峡中部 15.2 m/s。
    #
    # 采样点数按距离定（每 ~200 km 一个，上限 5）。但有个几何限制：中国海岸线是弯的，
    # 远距离两港之间的【大圆直线会切进内陆】——实测 538 km 以内的采样点全在海上，
    # 845 km 以上全部落到陆地。所以采样点要自过滤：取不到浪高的点就是不在航路上，
    # 丢掉并把覆盖情况如实透出，而不是假装采到了。
    if need_marine:
        coords = [p["resolved"] for p in points if p["resolved"].get("lat") is not None]
        if len(coords) >= 2:
            a, b = coords[0], coords[1]
            dist_km = great_circle_km(a["lat"], a["lon"], b["lat"], b["lon"])
            samples = route_sample_points(a["lat"], a["lon"], b["lat"], b["lon"])
            bundle.route = {"distance_km": round(dist_km, 1), "sampled": len(samples)}
            for idx, (lat, lon) in enumerate(samples, 1):
                key = MIDPOINT_KEY if len(samples) == 1 else f"{MIDPOINT_KEY} {idx}/{len(samples)}"
                mid: dict = {"name": key, "role": "midpoint", "errors": [],
                             "resolved": {"lat": lat, "lon": lon, "source": "derived",
                                          "matched_name": key}}
                points.append({"entry": mid, "name": key, "role": "midpoint",
                               "resolved": mid["resolved"], "query": f"{lat},{lon}",
                               "required": False})

    # ── 阶段二：并发取数 ──────────────────────────────────────────────────
    # 一条船舶航线是 3 个点 × (大气 + 海洋) = 6 次独立的网络调用。
    # 串行约 4.2s，并发约 1s —— 而 LLM 那一步就要 27s，能省的都得省。
    jobs, meta = [], []
    for pt in points:
        if transport == "plane":
            # 航空：官方 METAR/TAF 优先，取不到回落到城市地面天气（标明数据源）
            jobs.append((fetch_aviation_wx, pt["resolved"].get("icao"), pt["name"], target_date))
        else:
            # 海事：wttr.in 的地面天气（船在水面，地面气象就是正确的变量）
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
                    e = err or ""
                    if e.startswith("date_out_of_range"):
                        code = "date_out_of_range"
                    elif e.startswith("beyond_taf_horizon"):
                        code = "beyond_taf_horizon"
                    else:
                        code = "atmos_unavailable"
                    bundle.missing.append(Missing(code, role, name, e))
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

    # SIGMET：航路危险天气的官方产品，但有效期只有 4-6 小时，
    # 所以只在查【当天】时有意义。查明天/后天时不做，并在文档里说明原因。
    if transport == "plane" and target_date == datetime.date.today().isoformat():
        coords = [p["resolved"] for p in points
                  if p["resolved"].get("lat") is not None and p["required"]]
        if len(coords) >= 2:
            t0 = time.perf_counter()
            hits = sigmets_on_route(coords[0]["lat"], coords[0]["lon"],
                                    coords[1]["lat"], coords[1]["lon"])
            bundle.sigmets = hits
            bundle.trace.append(TraceStep(
                "check_route_sigmets", {"date": target_date}, True,
                int((time.perf_counter() - t0) * 1000),
                summary=f"航路穿越 {len(hits)} 条生效中的重要气象情报"))

    # 航路采样点必须拿到【海洋】数据才算数 —— 取不到就说明它落在陆地上，
    # 不在真实航路上，不能拿它的地面天气去当航路气象。
    on_water = 0
    for pt in points:
        if pt["role"] != "midpoint":
            continue
        if pt["entry"].get("marine"):
            bundle.locations[pt["name"]] = pt["entry"]
            on_water += 1

    if bundle.route:
        bundle.route["on_water"] = on_water
        if on_water == 0 and bundle.route["distance_km"] > 300:
            bundle.route["note"] = (
                "航线较长，两港之间的大圆路径穿越陆地，无法沿航路采样。"
                "以下结论只基于两端港口的气象条件，未覆盖航程中段。")
            bundle.quality = "partial"

    return bundle


MIDPOINT_KEY = "航线中点"
