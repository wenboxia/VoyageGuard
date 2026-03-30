"""
eval.py — LLM 横评脚本
测试 qwen-plus / hunyuan-turbos-latest / deepseek-v3-250324 在气象决策 agent 中的推理能力。
工具调用全部 mock，排除网络数据源干扰，评估纯推理能力。

运行：python eval.py
"""

import json
import os
import re
import time

from openai import OpenAI
from dotenv import load_dotenv

# 复用 app.py 中的 system prompt、工具定义、JSON 解析和规则引擎
from app import SYSTEM_PROMPT, TOOLS, parse_agent_output, validate_risk_level

load_dotenv()

# ---------------------------------------------------------------------------
# 模型配置
# ---------------------------------------------------------------------------
MODELS = [
    {
        "name": "qwen-plus",
        "model_id": "qwen-plus",
        "client": OpenAI(
            api_key=os.getenv("DASHSCOPE_API_KEY", ""),
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
    },
    {
        "name": "hunyuan-turbos-latest",
        "model_id": "hunyuan-turbos-latest",
        "client": OpenAI(
            api_key=os.getenv("HUNYUAN_API_KEY", ""),
            base_url="https://api.hunyuan.cloud.tencent.com/v1",
        ),
    },
    {
        "name": "deepseek-v3-250324",
        "model_id": "deepseek-v3-250324",
        "client": OpenAI(
            api_key=os.getenv("DOUBAO_API_KEY", ""),
            base_url="https://ark.cn-beijing.volces.com/api/v3",
        ),
    },
]

# ---------------------------------------------------------------------------
# Mock 数据构造辅助函数
# ---------------------------------------------------------------------------
def _weather(location, wind_ms, vis_km, description, beaufort=None):
    """构造与 get_weather_forecast 返回格式一致的 mock 天气数据。"""
    if beaufort is None:
        # 蒲福风级近似
        thresholds = [0.3, 1.6, 3.4, 5.5, 8.0, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7]
        beaufort = next((i for i, t in enumerate(thresholds) if wind_ms < t), 12)
    return {
        "location": location,
        "current": {
            "temperature_c": "22",
            "wind_speed_ms": wind_ms,
            "wind_speed_beaufort": beaufort,
            "wind_direction": "N",
            "visibility_km": vis_km,
            "description": description,
            "humidity_percent": "65",
        },
        "forecast_3days": [
            {
                "date": "2026-03-28",
                "max_temp_c": "25",
                "min_temp_c": "18",
                "max_wind_speed_ms": wind_ms,
                "max_wind_beaufort": beaufort,
                "min_visibility_km": vis_km,
                "description": description,
            }
        ],
    }


SAFE = lambda loc: _weather(loc, wind_ms=3.0, vis_km=10.0, description="晴")

NO_WARNING = json.dumps({"message": "当前无相关气象预警信息"}, ensure_ascii=False)
HAS_WARNING = json.dumps({
    "results": [{"title": "大风蓝色预警", "summary": "预计未来24小时平均风力达6级以上", "url": "http://example.com"}]
}, ensure_ascii=False)

# ---------------------------------------------------------------------------
# 30 条测试用例
# ---------------------------------------------------------------------------
# 字段说明：
#   id          — 用例编号
#   desc        — 用例描述（打印用）
#   origin      — 出发地
#   destination — 目的地
#   date        — 出行日期
#   transport   — "plane" | "ship"
#   expected    — 期望的 risk_level: "LOW" | "MEDIUM" | "HIGH"
#   mock_weather— {location关键词: weather_dict}，mock_executor 按关键词模糊匹配
#   mock_warning— search_weather_warning 的固定返回（可选，默认无预警）

TEST_CASES = [
    # ── 航空 HIGH ──────────────────────────────────────────────────────────
    {
        "id": 1,
        "desc": "[航空-HIGH] 出发地侧风 20 m/s（超过 15 m/s 红线）",
        "origin": "上海", "destination": "北京", "date": "2026-03-28", "transport": "plane",
        "expected": "HIGH", "acceptable_levels": ["HIGH"],
        "mock_weather": {
            "上海": _weather("上海", wind_ms=20.0, vis_km=8.0, description="大风"),
            "北京": SAFE("北京"),
        },
        "mock_warning": HAS_WARNING,
    },
    {
        "id": 2,
        "desc": "[航空-HIGH] 目的地能见度 0.2 km（低于 400 m 红线）",
        "origin": "上海", "destination": "重庆", "date": "2026-03-28", "transport": "plane",
        "expected": "HIGH", "acceptable_levels": ["HIGH"],
        "mock_weather": {
            "上海": SAFE("上海"),
            "重庆": _weather("重庆", wind_ms=4.0, vis_km=0.2, description="浓雾"),
        },
    },
    {
        "id": 3,
        "desc": "[航空-HIGH] 目的地风速 16 m/s（超红线）",
        "origin": "北京", "destination": "乌鲁木齐", "date": "2026-03-28", "transport": "plane",
        "expected": "HIGH", "acceptable_levels": ["HIGH"],
        "mock_weather": {
            "北京": SAFE("北京"),
            "乌鲁木齐": _weather("乌鲁木齐", wind_ms=16.0, vis_km=6.0, description="大风"),
        },
        "mock_warning": HAS_WARNING,
    },
    # ── 航空 MEDIUM ────────────────────────────────────────────────────────
    {
        "id": 4,
        "desc": "[航空-MEDIUM] 目的地雷暴（风速、能见度均达标，但雷暴触发中风险）",
        "origin": "广州", "destination": "成都", "date": "2026-03-28", "transport": "plane",
        "expected": "MEDIUM", "acceptable_levels": ["MEDIUM"],
        "mock_weather": {
            "广州": SAFE("广州"),
            "成都": _weather("成都", wind_ms=5.0, vis_km=8.0, description="雷暴"),
        },
    },
    {
        "id": 5,
        "desc": "[航空-灰色] 出发地雷暴 + 风速 12 m/s（接近红线，未超）",
        "origin": "武汉", "destination": "西安", "date": "2026-03-28", "transport": "plane",
        "expected": "MEDIUM", "acceptable_levels": ["MEDIUM", "HIGH"],
        "mock_weather": {
            "武汉": _weather("武汉", wind_ms=12.0, vis_km=5.0, description="雷暴"),
            "西安": SAFE("西安"),
        },
        "mock_warning": HAS_WARNING,
    },
    # ── 航空 LOW ───────────────────────────────────────────────────────────
    {
        "id": 6,
        "desc": "[航空-LOW] 晴天，风速 3 m/s，能见度 10 km",
        "origin": "杭州", "destination": "南京", "date": "2026-03-28", "transport": "plane",
        "expected": "LOW", "acceptable_levels": ["LOW"],
        "mock_weather": {
            "杭州": SAFE("杭州"),
            "南京": SAFE("南京"),
        },
    },
    {
        "id": 7,
        "desc": "[航空-LOW] 多云小雨，风速 8 m/s，能见度 5 km（无雷暴，均在安全范围）",
        "origin": "深圳", "destination": "厦门", "date": "2026-03-28", "transport": "plane",
        "expected": "LOW", "acceptable_levels": ["LOW"],
        "mock_weather": {
            "深圳": _weather("深圳", wind_ms=8.0, vis_km=5.0, description="多云转小雨"),
            "厦门": _weather("厦门", wind_ms=6.0, vis_km=6.0, description="阴天"),
        },
    },
    # ── 航空 边界值 ────────────────────────────────────────────────────────
    {
        "id": 8,
        "desc": "[航空-边界-HIGH] 风速恰好 15 m/s（触发红线临界值）",
        "origin": "兰州", "destination": "拉萨", "date": "2026-03-28", "transport": "plane",
        "expected": "HIGH", "acceptable_levels": ["HIGH"],
        "mock_weather": {
            "兰州": SAFE("兰州"),
            "拉萨": _weather("拉萨", wind_ms=15.0, vis_km=8.0, description="大风"),
        },
        "mock_warning": HAS_WARNING,
    },
    # ── 航空 多因素叠加 ────────────────────────────────────────────────────
    {
        "id": 9,
        "desc": "[航空-灰色] 风速 13 m/s（接近红线未超）+ 雷暴",
        "origin": "上海", "destination": "海口", "date": "2026-03-28", "transport": "plane",
        "expected": "MEDIUM", "acceptable_levels": ["MEDIUM", "HIGH"],
        "mock_weather": {
            "上海": SAFE("上海"),
            "海口": _weather("海口", wind_ms=13.0, vis_km=4.0, description="雷暴"),
        },
        "mock_warning": HAS_WARNING,
    },
    # ── 海事 HIGH ─────────────────────────────────────────────────────────
    {
        "id": 10,
        "desc": "[海事-HIGH] 风速 14 m/s（6级，超近海游船红线 10.8 m/s）",
        "origin": "上海", "destination": "舟山", "date": "2026-03-28", "transport": "ship",
        "expected": "HIGH", "acceptable_levels": ["HIGH"],
        "mock_weather": {
            "上海": SAFE("上海"),
            "舟山": _weather("舟山", wind_ms=14.0, vis_km=6.0, description="大风"),
        },
        "mock_warning": HAS_WARNING,
    },
    {
        "id": 11,
        "desc": "[海事-HIGH] 风速 22 m/s（9级，超跨海客滚船红线 17.2 m/s）",
        "origin": "大连", "destination": "烟台", "date": "2026-03-28", "transport": "ship",
        "expected": "HIGH", "acceptable_levels": ["HIGH"],
        "mock_weather": {
            "大连": SAFE("大连"),
            "烟台": _weather("烟台", wind_ms=22.0, vis_km=4.0, description="狂风"),
        },
        "mock_warning": HAS_WARNING,
    },
    {
        "id": 12,
        "desc": "[海事-HIGH] 海浪浪高 3.0 m（超过 2.5 m 红线）",
        "origin": "广州", "destination": "三亚", "date": "2026-03-28", "transport": "ship",
        "expected": "HIGH", "acceptable_levels": ["HIGH"],
        "mock_weather": {
            "广州": SAFE("广州"),
            "三亚": _weather("三亚", wind_ms=12.0, vis_km=6.0,
                          description="大浪，有效浪高 3.0 米，涌浪发展"),
        },
        "mock_warning": HAS_WARNING,
    },
    # ── 海事 MEDIUM ───────────────────────────────────────────────────────
    {
        "id": 13,
        "desc": "[海事-MEDIUM] 风速 11 m/s（约 5-6 级，中风险区间）",
        "origin": "福州", "destination": "平潭", "date": "2026-03-28", "transport": "ship",
        "expected": "MEDIUM", "acceptable_levels": ["MEDIUM"],
        "mock_weather": {
            "福州": SAFE("福州"),
            "平潭": _weather("平潭", wind_ms=11.0, vis_km=8.0, description="中浪，浪高约 2.0 米"),
        },
    },
    {
        "id": 14,
        "desc": "[海事-MEDIUM] 海浪浪高 2.0 m（1.5-2.5 m 中风险区间）",
        "origin": "海口", "destination": "三亚", "date": "2026-03-28", "transport": "ship",
        "expected": "MEDIUM", "acceptable_levels": ["MEDIUM"],
        "mock_weather": {
            "海口": SAFE("海口"),
            "三亚": _weather("三亚", wind_ms=9.0, vis_km=8.0,
                          description="轻到中浪，有效浪高 2.0 米"),
        },
    },
    # ── 海事 LOW ──────────────────────────────────────────────────────────
    {
        "id": 15,
        "desc": "[海事-LOW] 风速 3 m/s，浪高 0.5 m，晴好天气",
        "origin": "厦门", "destination": "金门", "date": "2026-03-28", "transport": "ship",
        "expected": "LOW", "acceptable_levels": ["LOW"],
        "mock_weather": {
            "厦门": SAFE("厦门"),
            "金门": _weather("金门", wind_ms=3.0, vis_km=12.0, description="晴，海面平静，浪高 0.5 米"),
        },
    },

    # ── 航空 临界值三连测 ───────────────────────────────────────────────────
    {
        "id": 16,
        "desc": "[航空-边界-LOW] 风速 14.9 m/s（刚低于 15 m/s 红线），无雷暴",
        "origin": "西宁", "destination": "成都", "date": "2026-03-28", "transport": "plane",
        "expected": "LOW", "acceptable_levels": ["LOW"],
        "mock_weather": {
            "西宁": _weather("西宁", wind_ms=14.9, vis_km=8.0, description="大风"),
            "成都": SAFE("成都"),
        },
        "mock_warning": HAS_WARNING,
    },
    {
        "id": 17,
        "desc": "[航空-边界-HIGH] 风速 15.1 m/s（刚超过 15 m/s 红线）",
        "origin": "呼和浩特", "destination": "北京", "date": "2026-03-28", "transport": "plane",
        "expected": "HIGH", "acceptable_levels": ["HIGH"],
        "mock_weather": {
            "呼和浩特": _weather("呼和浩特", wind_ms=15.1, vis_km=8.0, description="大风"),
            "北京": SAFE("北京"),
        },
        "mock_warning": HAS_WARNING,
    },
    # ── 航空 能见度边界 ────────────────────────────────────────────────────
    {
        "id": 18,
        "desc": "[航空-边界-HIGH] 能见度 0.39 km（刚低于 0.4 km 红线）",
        "origin": "南京", "destination": "合肥", "date": "2026-03-28", "transport": "plane",
        "expected": "HIGH", "acceptable_levels": ["HIGH"],
        "mock_weather": {
            "南京": SAFE("南京"),
            "合肥": _weather("合肥", wind_ms=4.0, vis_km=0.39, description="浓雾"),
        },
    },
    {
        "id": 19,
        "desc": "[航空-边界-LOW] 能见度 0.41 km（刚高于 0.4 km 红线），无雷暴，风速正常",
        "origin": "南京", "destination": "合肥", "date": "2026-03-28", "transport": "plane",
        "expected": "LOW", "acceptable_levels": ["LOW"],
        "mock_weather": {
            "南京": SAFE("南京"),
            "合肥": _weather("合肥", wind_ms=4.0, vis_km=0.41, description="薄雾"),
        },
    },
    # ── 航空 新场景 ────────────────────────────────────────────────────────
    {
        "id": 20,
        "desc": "[航空-HIGH] 出发地雷暴 + 风速恰好 15 m/s（雷暴叠加风速触红线）",
        "origin": "郑州", "destination": "长沙", "date": "2026-03-28", "transport": "plane",
        "expected": "HIGH", "acceptable_levels": ["HIGH"],
        "mock_weather": {
            "郑州": _weather("郑州", wind_ms=15.0, vis_km=5.0, description="雷暴"),
            "长沙": SAFE("长沙"),
        },
        "mock_warning": HAS_WARNING,
    },
    {
        "id": 21,
        "desc": "[航空-MEDIUM] 出发地+目的地均有雷暴，风速和能见度均正常（双侧雷暴仍中风险）",
        "origin": "武汉", "destination": "南昌", "date": "2026-03-28", "transport": "plane",
        "expected": "MEDIUM", "acceptable_levels": ["MEDIUM"],
        "mock_weather": {
            "武汉": _weather("武汉", wind_ms=6.0, vis_km=6.0, description="雷暴"),
            "南昌": _weather("南昌", wind_ms=5.0, vis_km=7.0, description="雷暴"),
        },
        "mock_warning": HAS_WARNING,
    },
    {
        "id": 22,
        "desc": "[航空-LOW] 小雨+多云，风速 10 m/s，能见度 3 km，两地均无雷暴",
        "origin": "青岛", "destination": "济南", "date": "2026-03-28", "transport": "plane",
        "expected": "LOW", "acceptable_levels": ["LOW"],
        "mock_weather": {
            "青岛": _weather("青岛", wind_ms=10.0, vis_km=3.0, description="小雨"),
            "济南": _weather("济南", wind_ms=7.0, vis_km=4.0, description="多云"),
        },
    },

    # ── 海事 浪高边界 ──────────────────────────────────────────────────────
    {
        "id": 23,
        "desc": "[海事-边界-MEDIUM] 浪高恰好 2.5 m（超过2.5m才HIGH，2.5m本身为中风险）",
        "origin": "宁波", "destination": "舟山", "date": "2026-03-28", "transport": "ship",
        "expected": "MEDIUM", "acceptable_levels": ["MEDIUM"],
        "mock_weather": {
            "宁波": SAFE("宁波"),
            "舟山": _weather("舟山", wind_ms=10.0, vis_km=6.0,
                           description="中浪，有效浪高 2.5 米"),
        },
        "mock_warning": HAS_WARNING,
    },
    {
        "id": 24,
        "desc": "[海事-边界-HIGH] 浪高 2.6 m（超过 2.5 m 红线）",
        "origin": "温州", "destination": "洞头", "date": "2026-03-28", "transport": "ship",
        "expected": "HIGH", "acceptable_levels": ["HIGH"],
        "mock_weather": {
            "温州": SAFE("温州"),
            "洞头": _weather("洞头", wind_ms=11.0, vis_km=5.0,
                           description="大浪，有效浪高 2.6 米"),
        },
        "mock_warning": HAS_WARNING,
    },
    {
        "id": 25,
        "desc": "[海事-MEDIUM] 浪高 1.5 m（中风险下边界）",
        "origin": "烟台", "destination": "蓬莱", "date": "2026-03-28", "transport": "ship",
        "expected": "MEDIUM", "acceptable_levels": ["MEDIUM"],
        "mock_weather": {
            "烟台": SAFE("烟台"),
            "蓬莱": _weather("蓬莱", wind_ms=7.0, vis_km=8.0,
                           description="轻浪，有效浪高 1.5 米"),
        },
    },
    {
        "id": 26,
        "desc": "[海事-LOW] 浪高 1.4 m（低于 1.5 m 中风险起点）",
        "origin": "威海", "destination": "刘公岛", "date": "2026-03-28", "transport": "ship",
        "expected": "LOW", "acceptable_levels": ["LOW"],
        "mock_weather": {
            "威海": SAFE("威海"),
            "刘公岛": _weather("刘公岛", wind_ms=6.0, vis_km=9.0,
                            description="微浪，有效浪高 1.4 米"),
        },
    },
    # ── 海事 风速边界 ──────────────────────────────────────────────────────
    {
        "id": 27,
        "desc": "[海事-边界-HIGH] 风速恰好 10.8 m/s（触发近海游船 6 级高风险红线）",
        "origin": "青岛", "destination": "长岛", "date": "2026-03-28", "transport": "ship",
        "expected": "HIGH", "acceptable_levels": ["HIGH"],
        "mock_weather": {
            "青岛": SAFE("青岛"),
            "长岛": _weather("长岛", wind_ms=10.8, vis_km=7.0, description="大风，6级"),
        },
        "mock_warning": HAS_WARNING,
    },
    {
        "id": 28,
        "desc": "[海事-MEDIUM] 风速 10.5 m/s（5级，刚低于 6 级红线 10.8 m/s）",
        "origin": "连云港", "destination": "朝连岛", "date": "2026-03-28", "transport": "ship",
        "expected": "MEDIUM", "acceptable_levels": ["MEDIUM"],
        "mock_weather": {
            "连云港": SAFE("连云港"),
            "朝连岛": _weather("朝连岛", wind_ms=10.5, vis_km=7.0, description="较大风浪，5级风"),
        },
    },
    # ── 海事 多因素叠加 ────────────────────────────────────────────────────
    {
        "id": 29,
        "desc": "[海事-多因素-MEDIUM] 风速 9 m/s（5级）+ 浪高 2.0 m（双中风险因素叠加）",
        "origin": "舟山", "destination": "嵊泗", "date": "2026-03-28", "transport": "ship",
        "expected": "MEDIUM", "acceptable_levels": ["MEDIUM"],
        "mock_weather": {
            "舟山": SAFE("舟山"),
            "嵊泗": _weather("嵊泗", wind_ms=9.0, vis_km=7.0,
                           description="中浪，有效浪高 2.0 米，风力 5 级"),
        },
    },
    {
        "id": 30,
        "desc": "[海事-LOW] 风速 7 m/s（4级），浪高 1.2 m，晴好",
        "origin": "珠海", "destination": "外伶仃岛", "date": "2026-03-28", "transport": "ship",
        "expected": "LOW", "acceptable_levels": ["LOW"],
        "mock_weather": {
            "珠海": SAFE("珠海"),
            "外伶仃岛": _weather("外伶仃岛", wind_ms=7.0, vis_km=12.0,
                             description="晴，微浪，浪高 1.2 米"),
        },
    },
]

TRANSPORT_LABELS = {"plane": "飞机", "ship": "船只"}

# ---------------------------------------------------------------------------
# Mock 工具执行器
# ---------------------------------------------------------------------------
def make_mock_executor(test_case):
    """返回一个 mock 函数，根据 test_case 中的预设数据响应工具调用。"""
    weather_mocks = test_case.get("mock_weather", {})
    warning_mock = test_case.get("mock_warning", NO_WARNING)

    def executor(tool_name, args):
        if tool_name == "get_weather_forecast":
            location = args.get("location", "")
            # 模糊匹配：location 与 key 互相包含即命中
            for key, data in weather_mocks.items():
                if key in location or location in key:
                    return json.dumps(data, ensure_ascii=False)
            # 未命中 → 安全天气兜底
            return json.dumps(SAFE(location), ensure_ascii=False)

        elif tool_name == "search_weather_warning":
            return warning_mock

        return json.dumps({"error": f"unknown tool: {tool_name}"})

    return executor

# ---------------------------------------------------------------------------
# Agent Loop（接受可替换的 tool executor）
# ---------------------------------------------------------------------------
def run_agent_mock(user_message, mock_executor, client, model_id):
    """与 app.py 的 run_agent 相同逻辑，但工具执行由 mock_executor 替代。"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]
    max_iterations = 5

    for _ in range(max_iterations):
        response = client.chat.completions.create(
            model=model_id,
            messages=messages,
            tools=TOOLS,
        )
        msg = response.choices[0].message

        if not msg.tool_calls:
            return msg.content or ""

        messages.append(msg)
        for call in msg.tool_calls:
            args = json.loads(call.function.arguments)
            result = mock_executor(call.function.name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": result,
            })

    return ""  # 超出迭代上限

# ---------------------------------------------------------------------------
# 单用例评估
# ---------------------------------------------------------------------------
def eval_one(test_case, client, model_id):
    """
    返回 (json_ok, llm_correct, validated_correct, elapsed_s, llm_level, validated_level)
    _correct 均基于 acceptable_levels 判断（命中任意一个即正确）
    """
    transport_label = TRANSPORT_LABELS.get(test_case["transport"], test_case["transport"])
    user_message = (
        f"我计划于 {test_case['date']} 乘坐{transport_label}"
        f"从 {test_case['origin']} 前往 {test_case['destination']}。"
        f"请帮我评估气象风险，判断是否适合出行。"
    )

    mock_executor = make_mock_executor(test_case)
    t0 = time.time()
    try:
        raw = run_agent_mock(user_message, mock_executor, client, model_id)
        elapsed = time.time() - t0
    except Exception as e:
        elapsed = time.time() - t0
        return False, False, False, elapsed, f"ERROR:{e}", f"ERROR:{e}"

    # JSON 解析
    try:
        data = parse_agent_output(raw)
        json_ok = True
        llm_level = data.get("risk_level", "")
    except Exception:
        json_ok = False
        llm_level = ""
        return False, False, False, elapsed, llm_level, llm_level

    # 规则引擎校验（使用 mock_weather 数据）
    weather_data_list = list(test_case.get("mock_weather", {}).values())
    validated = validate_risk_level(data, weather_data_list, test_case["transport"])
    validated_level = validated.get("risk_level", llm_level)

    acceptable = test_case.get("acceptable_levels", [test_case["expected"]])
    llm_correct = llm_level in acceptable
    validated_correct = validated_level in acceptable
    return json_ok, llm_correct, validated_correct, elapsed, llm_level, validated_level


# ---------------------------------------------------------------------------
# 主评估流程
# ---------------------------------------------------------------------------
def run_eval():
    print("\n" + "=" * 72)
    print("  VoyageGuard LLM Eval  |  测试用例数:", len(TEST_CASES))
    print("=" * 72)

    # 过滤掉未配置 API Key 的模型
    active_models = []
    for m in MODELS:
        key = m["client"].api_key or ""
        if not key or key in ("", "None", "your_api_key_here"):
            print(f"  ⚠  跳过 {m['name']}（API Key 未配置）")
        else:
            active_models.append(m)

    if not active_models:
        print("  所有模型的 API Key 均未配置，退出。")
        return

    # 逐模型评估
    summary = []

    for m in active_models:
        print(f"\n{'─' * 84}")
        print(f"  模型: {m['name']}")
        print(f"{'─' * 84}")
        print(f"  {'ID':>3}  {'期望':^6}  {'LLM':^6}  {'校验后':^6}  {'JSON':^4}  {'时间':>6}  {'类型':^4}  描述")
        print(f"  {'─'*3}  {'─'*6}  {'─'*6}  {'─'*6}  {'─'*4}  {'─'*6}  {'─'*4}  {'─'*36}")

        results = []
        for tc in TEST_CASES:
            json_ok, llm_correct, val_correct, elapsed, llm_level, val_level = eval_one(tc, m["client"], m["model_id"])
            results.append((json_ok, llm_correct, val_correct, elapsed, llm_level, val_level))

            llm_tick = "✓" if llm_correct else "✗"
            val_tick = "✓" if val_correct else "✗"
            override_marker = "←校正" if val_level != llm_level else ""
            j_tick = "✓" if json_ok else "✗"
            acceptable = tc.get("acceptable_levels", [tc["expected"]])
            kind = "灰色" if len(acceptable) > 1 else "硬判"
            print(
                f"  {tc['id']:>3}  {tc['expected']:^6}  {llm_tick}{llm_level:<5}  {val_tick}{val_level:<5}  "
                f"{j_tick:^4}  {elapsed:>5.1f}s  {kind:^4}  {tc['desc'][:36]}{override_marker}"
            )

        n = len(results)
        llm_acc = sum(r[1] for r in results) / n * 100
        val_acc = sum(r[2] for r in results) / n * 100
        json_rate = sum(r[0] for r in results) / n * 100
        avg_t = sum(r[3] for r in results) / n
        summary.append({
            "name": m["name"],
            "llm_accuracy": llm_acc,
            "val_accuracy": val_acc,
            "json_rate": json_rate,
            "avg_time": avg_t,
        })

    # 汇总表格
    print(f"\n{'=' * 84}")
    print("  汇总对比")
    print(f"{'=' * 84}")
    print(f"  {'模型':<24}  {'LLM准确率':^12}  {'安全网后':^12}  {'提升':^8}  {'JSON合规':^10}  {'平均时间':^10}")
    print(f"  {'─'*24}  {'─'*12}  {'─'*12}  {'─'*8}  {'─'*10}  {'─'*10}")
    for s in summary:
        delta = s['val_accuracy'] - s['llm_accuracy']
        delta_str = f"+{delta:.1f}%" if delta >= 0 else f"{delta:.1f}%"
        print(
            f"  {s['name']:<24}  {s['llm_accuracy']:>9.1f} %  "
            f"{s['val_accuracy']:>9.1f} %  {delta_str:>8}  "
            f"{s['json_rate']:>7.1f} %  {s['avg_time']:>7.1f} s"
        )
    print(f"{'=' * 84}\n")


if __name__ == "__main__":
    run_eval()
