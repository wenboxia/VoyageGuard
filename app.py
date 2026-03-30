import os
import json
import re
import requests
from collections import defaultdict
import time as _time
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from openai import OpenAI
from ddgs import DDGS
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# LLM Client
# ---------------------------------------------------------------------------
# Dashscope (Qwen) via OpenAI-compatible interface
client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)
MODEL = "qwen-plus"

# Ollama local fallback (uncomment to switch):
# client = OpenAI(
#     api_key="ollama",
#     base_url="http://localhost:11434/v1",
# )
# MODEL = "qwen2.5:7b"

# ---------------------------------------------------------------------------
# Helper: Beaufort scale conversion
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
# Tool: get_weather_forecast
# ---------------------------------------------------------------------------
def get_weather_forecast(location: str) -> str:
    url = f"https://wttr.in/{requests.utils.quote(location)}?format=j1&lang=zh"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return json.dumps({"error": f"获取天气数据失败: {e}"}, ensure_ascii=False)

    # Current conditions
    current = data.get("current_condition", [{}])[0]
    curr_wind_kmh = float(current.get("windspeedKmph", 0))
    curr_wind_ms = round(kmh_to_ms(curr_wind_kmh), 1)
    curr_vis_km = float(current.get("visibility", 0))
    curr_temp = current.get("temp_C", "N/A")
    curr_humidity = current.get("humidity", "N/A")
    weather_desc_list = current.get("lang_zh", current.get("weatherDesc", [{}]))
    curr_desc = weather_desc_list[0].get("value", "") if weather_desc_list else ""

    current_weather = {
        "temperature_c": curr_temp,
        "wind_speed_ms": curr_wind_ms,
        "wind_speed_beaufort": ms_to_beaufort(curr_wind_ms),
        "wind_direction": current.get("winddir16Point", ""),
        "visibility_km": curr_vis_km,
        "description": curr_desc,
        "humidity_percent": curr_humidity,
    }

    # 3-day forecast
    forecast = []
    for day in data.get("weather", [])[:3]:
        hourly_winds = [float(h.get("windspeedKmph", 0)) for h in day.get("hourly", [])]
        max_wind_kmh = max(hourly_winds) if hourly_winds else 0
        max_wind_ms = round(kmh_to_ms(max_wind_kmh), 1)

        hourly_vis = [float(h.get("visibility", 999)) for h in day.get("hourly", [])]
        min_vis_km = min(hourly_vis) if hourly_vis else 999

        desc_list = day.get("hourly", [])
        desc_values = []
        for h in desc_list:
            zh = h.get("lang_zh", h.get("weatherDesc", [{}]))
            if zh:
                desc_values.append(zh[0].get("value", ""))
        desc_summary = "、".join(set(filter(None, desc_values))) or "N/A"

        forecast.append({
            "date": day.get("date", ""),
            "max_temp_c": day.get("maxtempC", ""),
            "min_temp_c": day.get("mintempC", ""),
            "max_wind_speed_ms": max_wind_ms,
            "max_wind_beaufort": ms_to_beaufort(max_wind_ms),
            "min_visibility_km": min_vis_km,
            "description": desc_summary,
        })

    result = {
        "location": location,
        "current": current_weather,
        "forecast_3days": forecast,
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tool: search_weather_warning
# ---------------------------------------------------------------------------
def search_weather_warning(query: str, region: str) -> str:
    if region == "china":
        site_filter = "site:weather.com.cn OR site:cma.gov.cn"
    else:
        site_filter = "site:windy.com OR site:weather.com"

    full_query = f"{query} {site_filter}"

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(full_query, max_results=5))
    except Exception:
        results = []

    # Fallback: plain search without site filter
    if not results:
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=5))
        except Exception:
            results = []

    if not results:
        return json.dumps({"message": "未找到相关气象预警信息"}, ensure_ascii=False)

    formatted = []
    for r in results:
        formatted.append({
            "title": r.get("title", ""),
            "summary": r.get("body", ""),
            "url": r.get("href", ""),
        })

    return json.dumps({"results": formatted}, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather_forecast",
            "description": (
                "获取指定地点的天气预报数据，返回未来3天的温度、风速(m/s)、风级、"
                "风向、能见度(km)、降水量、天气描述等结构化数据。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "地点名称，中文或英文，如：上海、三亚、Tokyo",
                    }
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_weather_warning",
            "description": (
                "从权威气象网站搜索特定地区的气象预警、台风路径、大风预警、海浪预警等专业信息。"
                "中国地区优先搜索中国气象局，境外优先搜索 Windy.com。"
                "当需要了解海浪、台风、航空气象等专业信息时使用此工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索词，如：三亚台风预警、上海大风预警",
                    },
                    "region": {
                        "type": "string",
                        "enum": ["china", "international"],
                        "description": "地区类型：china 表示中国境内，international 表示境外",
                    },
                },
                "required": ["query", "region"],
            },
        },
    },
]


def execute_tool(name: str, args: dict) -> str:
    if name == "get_weather_forecast":
        return get_weather_forecast(args["location"])
    elif name == "search_weather_warning":
        return search_weather_warning(args["query"], args["region"])
    else:
        return json.dumps({"error": f"未知工具: {name}"})


# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是一个专业的出行气象风险决策 AI Agent。用户会告诉你出发地、目的地、出行日期和交通工具类型。

## 工作流程
1. 使用 get_weather_forecast 工具获取出发地和目的地的天气预报
2. 如果风力偏高或天气异常，使用 search_weather_warning 工具搜索相关预警信息进行交叉验证
3. 根据下方安全规则进行风险评估
4. 输出结构化 JSON 结果

## 安全规则知识库

### 航空规则
- 风速红线：侧风超过 15 m/s（约 29 节 / 7级风），判定高风险（复飞/备降）
- 能见度红线：机场能见度低于 400 米（0.4 km），判定高风险（无法起降）
- 对流红线：起降地有"雷暴/thunderstorm"标识，至少判定中风险（延误）
- 中度阵雨/多云等一般天气，如风速和能见度均在安全范围内，判定低风险

### 海事规则
- 风级红线（近海游船）：阵风 6 级（10.8-13.8 m/s）及以上 → 高风险（停航）
- 风级红线（跨海客滚船）：阵风 8 级（17.2-20.7 m/s）及以上 → 高风险（强制停航）
- 浪高红线：有效浪高超过 2.5 米 → 高风险（停航）
- 浪高 1.5-2.5 米或风力 5-6 级 → 中风险

## 判断规范（优先级高于直觉估算）

1. **m/s 是唯一判断单位**：括号内的节数和蒲福风级仅供参考，
   不得以风级近似代替 m/s 数值。判断时直接比对数据的 wind_speed_ms 字段。

2. **阈值为严格不等式**：
   - "超过 X" 表示严格大于（> X），X 本身不触发更高风险等级
   - "低于 X" 表示严格小于（< X），X 本身不触发更高风险等级
   - 区间下边界包含（如浪高 1.5-2.5 m，1.5 m 本身属于中风险范围）

3. **数值精度原则**：以数据给出的精确值判断，禁止四舍五入。
   例：能见度 0.41 km > 0.4 km，不得视为"约 0.4 km"，应判低风险。

4. **兜底规则**：若所有因素（风速、能见度、雷暴、浪高）均未触发任何红线，
   无论数值多接近阈值，统一判定低风险（LOW）。接近 ≠ 超过。

## 输出要求
完成推理后，你必须且只能输出以下 JSON，不要输出任何其他文字（不要 markdown code fence）：
{
  "risk_level": "LOW" | "MEDIUM" | "HIGH",
  "risk_label": "风险等级的中文标签，如：低风险（按计划出行）/ 中风险（延误预警）/ 高风险（停航预警）",
  "is_go_recommended": true | false,
  "core_reason": "用通俗的中文解释核心原因，引用具体气象数值和对应的安全红线",
  "alternative_advice": "给出1-2条具体可落地的备选方案",
  "weather_summary": "简要描述查到的关键气象数据（风速、能见度、天气状况等）"
}
⚠️ 再次强调：你的回复必须是且只能是一个合法 JSON 对象，从 { 开始，到 } 结束，不能有任何其他文字。"""


# ---------------------------------------------------------------------------
# Rule Validator (Safety Net)
# ---------------------------------------------------------------------------
def _extract_wave_height(description: str) -> float | None:
    """从天气描述文本中提取有效浪高（米）。"""
    match = re.search(r'(?:有效浪高|浪高)\s*([\d.]+)\s*米', description)
    if match:
        return float(match.group(1))
    return None


def validate_risk_level(llm_output: dict, weather_data_list: list, transport: str, lang: str = "zh") -> dict:
    """
    规则引擎安全网：对 LLM 输出做二次校验。
    - Override UP：气象数据越过硬红线但 LLM 判低了 → 强制升级
    - Override DOWN：所有指标均安全但 LLM 判了 HIGH → 降级为 LOW
    """
    LEVEL_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

    required_level = "LOW"  # 规则引擎推导出的最低要求
    trigger_reason = ""

    for wd in weather_data_list:
        current = wd.get("current", {})
        wind_ms = float(current.get("wind_speed_ms", 0))
        vis_km = float(current.get("visibility_km", 999))
        desc = current.get("description", "")

        # 也检查预报数据中的最大值
        forecast_wind = max(
            (float(d.get("max_wind_speed_ms", 0)) for d in wd.get("forecast_3days", [])),
            default=0,
        )
        forecast_vis = min(
            (float(d.get("min_visibility_km", 999)) for d in wd.get("forecast_3days", [])),
            default=999,
        )
        forecast_desc = " ".join(d.get("description", "") for d in wd.get("forecast_3days", []))
        all_desc = desc + " " + forecast_desc

        effective_wind = max(wind_ms, forecast_wind)
        effective_vis = min(vis_km, forecast_vis)
        wave_height = _extract_wave_height(all_desc)

        if transport == "plane":
            if effective_wind >= 15:  # >= 15: at-threshold is still dangerous
                if LEVEL_ORDER["HIGH"] > LEVEL_ORDER[required_level]:
                    required_level = "HIGH"
                    trigger_reason = (
                        f"Wind speed {effective_wind} m/s reaches aviation red line (≥15 m/s)"
                        if lang == "en" else
                        f"风速 {effective_wind} m/s 达到航空红线（≥15 m/s）"
                    )
            if effective_vis < 0.4:
                if LEVEL_ORDER["HIGH"] > LEVEL_ORDER[required_level]:
                    required_level = "HIGH"
                    trigger_reason = (
                        f"Visibility {effective_vis} km below aviation red line of 0.4 km"
                        if lang == "en" else
                        f"能见度 {effective_vis} km 低于航空红线 0.4 km"
                    )
            if "雷暴" in all_desc or "thunderstorm" in all_desc.lower():
                if LEVEL_ORDER["MEDIUM"] > LEVEL_ORDER[required_level]:
                    required_level = "MEDIUM"
                    trigger_reason = (
                        "Thunderstorm detected — at least medium risk"
                        if lang == "en" else
                        "存在雷暴，至少中风险"
                    )
        elif transport == "ship":
            if effective_wind >= 10.8:
                if LEVEL_ORDER["HIGH"] > LEVEL_ORDER[required_level]:
                    required_level = "HIGH"
                    trigger_reason = (
                        f"Wind speed {effective_wind} m/s reaches coastal vessel suspension red line (≥10.8 m/s)"
                        if lang == "en" else
                        f"风速 {effective_wind} m/s 达到近海游船停航红线（≥10.8 m/s）"
                    )
            if wave_height is not None and wave_height > 2.5:
                if LEVEL_ORDER["HIGH"] > LEVEL_ORDER[required_level]:
                    required_level = "HIGH"
                    trigger_reason = (
                        f"Significant wave height {wave_height} m exceeds red line of 2.5 m"
                        if lang == "en" else
                        f"有效浪高 {wave_height} m 超过红线 2.5 m"
                    )
            if wave_height is not None and wave_height >= 1.5:
                if LEVEL_ORDER["MEDIUM"] > LEVEL_ORDER[required_level]:
                    required_level = "MEDIUM"
                    trigger_reason = (
                        f"Significant wave height {wave_height} m in medium-risk range (1.5–2.5 m)"
                        if lang == "en" else
                        f"有效浪高 {wave_height} m 处于中风险区间（1.5-2.5 m）"
                    )
            if effective_wind >= 8.0:
                if LEVEL_ORDER["MEDIUM"] > LEVEL_ORDER[required_level]:
                    required_level = "MEDIUM"
                    trigger_reason = (
                        f"Wind speed {effective_wind} m/s in medium-risk range (Beaufort 5–6)"
                        if lang == "en" else
                        f"风速 {effective_wind} m/s 处于中风险区间（5-6 级）"
                    )

    # For ship Override DOWN: only when we have wave height data to confidently assess sea state
    has_wave_data = transport == "plane" or any(
        _extract_wave_height(
            wd.get("current", {}).get("description", "") +
            " ".join(d.get("description", "") for d in wd.get("forecast_3days", []))
        ) is not None
        for wd in weather_data_list
    )

    llm_level = llm_output.get("risk_level", "LOW")
    result = dict(llm_output)

    # Override UP：LLM 判低了，规则要求更高
    if LEVEL_ORDER[required_level] > LEVEL_ORDER.get(llm_level, 0):
        result["risk_level"] = required_level
        result["rule_override"] = True
        result["original_risk_level"] = llm_level
        if lang == "en":
            result["core_reason"] = (
                f"⚠️ Rule engine override ({llm_level}→{required_level}): {trigger_reason}\n\n"
                + result.get("core_reason", "")
            )
        else:
            result["core_reason"] = (
                f"⚠️ 规则引擎校正（{llm_level}→{required_level}）：{trigger_reason}\n\n"
                + result.get("core_reason", "")
            )

    # Override DOWN：LLM 判高了，结构化指标不支持该风险等级
    # Aviation: always safe to override (wind + visibility are reliable structured fields)
    # Ship: only override if we have wave data (wind alone is insufficient—vessel type matters)
    elif LEVEL_ORDER.get(llm_level, 0) > LEVEL_ORDER[required_level] and has_wave_data:
        result["risk_level"] = required_level
        result["rule_override"] = True
        result["original_risk_level"] = llm_level
        if lang == "en":
            result["core_reason"] = (
                f"⚠️ Rule engine override ({llm_level}→{required_level}): "
                f"Structured weather indicators do not meet the trigger conditions for {llm_level}.\n\n"
                + result.get("core_reason", "")
            )
        else:
            result["core_reason"] = (
                f"⚠️ 规则引擎校正（{llm_level}→{required_level}）：结构化气象指标未达到{llm_level}等级的触发条件\n\n"
                + result.get("core_reason", "")
            )
    else:
        result["rule_override"] = False

    return result


# ---------------------------------------------------------------------------
# Agent Loop
# ---------------------------------------------------------------------------
LANG_DIRECTIVE_EN = (
    "\n\nIMPORTANT: The user is viewing in English. "
    "You MUST write ALL text fields in the output JSON "
    "(risk_label, core_reason, alternative_advice, weather_summary) in English. "
    "Do not use any Chinese characters in those fields."
)


def run_agent(user_message: str, transport: str = "plane", lang: str = "zh") -> tuple:
    """返回 (llm_output_dict, weather_data_list)"""
    system_content = SYSTEM_PROMPT + (LANG_DIRECTIVE_EN if lang == "en" else "")
    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_message},
    ]
    max_iterations = 5
    weather_data_list = []

    for _ in range(max_iterations):
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
        )
        msg = response.choices[0].message

        # No tool calls → final answer
        if not msg.tool_calls:
            raw = msg.content or ""
            return parse_agent_output(raw), weather_data_list

        # Execute tool calls and append results
        messages.append(msg)
        for call in msg.tool_calls:
            args = json.loads(call.function.arguments)
            result = execute_tool(call.function.name, args)
            # Collect weather forecast data for rule validation
            if call.function.name == "get_weather_forecast":
                try:
                    weather_data_list.append(json.loads(result))
                except Exception:
                    pass
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": result,
            })

    raise ValueError("Agent 超过最大迭代次数，未能完成推理")


def parse_agent_output(raw: str) -> dict:
    # Strip markdown code fences if present
    match = re.search(r'\{[\s\S]*"risk_level"[\s\S]*\}', raw)
    if not match:
        raise ValueError(f"无法从模型输出中解析 JSON：{raw[:200]}")
    data = json.loads(match.group())
    # Normalize: model sometimes returns string fields as arrays
    for field in ("alternative_advice", "core_reason", "weather_summary", "risk_label"):
        if isinstance(data.get(field), list):
            data[field] = "\n".join(data[field])
    return data


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Rate Limiting (in-memory, per IP, 20 req/hour)
# ---------------------------------------------------------------------------
_rl_store: dict[str, list[float]] = defaultdict(list)
_RL_LIMIT = 20
_RL_WINDOW = 3600

def _check_rate_limit(ip: str) -> bool:
    now = _time.time()
    _rl_store[ip] = [t for t in _rl_store[ip] if now - t < _RL_WINDOW]
    if len(_rl_store[ip]) >= _RL_LIMIT:
        return False
    _rl_store[ip].append(now)
    return True


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------
app = FastAPI(title="VoyageGuard - 出行气象决策 Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AssessRequest(BaseModel):
    origin: str
    destination: str
    date: str
    transport: str   # "plane" | "ship"
    lang: str = "zh" # "zh" | "en"


TRANSPORT_LABELS = {
    "zh": {"plane": "飞机",  "ship": "船只"},
    "en": {"plane": "plane", "ship": "ship"},
}


@app.post("/api/assess")
async def assess(req: AssessRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(ip):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试（每小时限 20 次）")
    lang = req.lang if req.lang in ("zh", "en") else "zh"
    transport_label = TRANSPORT_LABELS.get(lang, TRANSPORT_LABELS["zh"]).get(req.transport, req.transport)
    if lang == "en":
        user_message = (
            f"I plan to travel from {req.origin} to {req.destination} by {transport_label} on {req.date}. "
            f"Please assess the weather risk and advise whether it is safe to travel."
        )
    else:
        user_message = (
            f"我计划于 {req.date} 乘坐{transport_label}从 {req.origin} 前往 {req.destination}。"
            f"请帮我评估气象风险，判断是否适合出行。"
        )
    try:
        llm_output, weather_data_list = run_agent(user_message, transport=req.transport, lang=lang)
        result = validate_risk_level(llm_output, weather_data_list, req.transport, lang=lang)
        return result
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent 执行失败: {e}")


# Serve frontend
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")
