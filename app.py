"""
app.py — FastAPI 路由层（同时是 Vercel Python Function 的入口，顶层 `app` 变量不能改名）

请求流程：
    POST /api/assess
      → evidence.build_evidence()      确定性预取必需证据
      → 证据不足？ 直接 abstain，不调 LLM（省掉一次注定被丢弃的模型调用）
      → agent.run_agent()              ReAct 循环，模型可调补充证据工具
      → rules.evaluate()               规则引擎：abstain / Override UP / Override DOWN
      → 返回结论 + evidence + 真实 trace
"""

import os
import time as _time
from collections import defaultdict

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agent
import evidence
import providers
import rules

load_dotenv()

app = FastAPI(title="VoyageGuard - 出行气象决策 Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------
# 注意：内存实现，在 serverless 上按实例隔离，只能挡住单实例内的连续爆刷。
# 真正的限流需要外部存储（KV / Redis），本项目范围内不做，见 README「已知取舍」。
_rl_store: dict[str, list[float]] = defaultdict(list)
_RL_LIMIT = int(os.getenv("VOYAGEGUARD_RATE_LIMIT", "20"))
_RL_WINDOW = 3600


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(ip: str) -> bool:
    now = _time.time()
    _rl_store[ip] = [t for t in _rl_store[ip] if now - t < _RL_WINDOW]
    if len(_rl_store[ip]) >= _RL_LIMIT:
        return False
    _rl_store[ip].append(now)
    return True


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
class AssessRequest(BaseModel):
    origin: str
    destination: str
    date: str
    transport: str                    # "plane" | "ship"
    vessel_type: str | None = None    # "small" | "large" | "unknown"，仅 ship 有意义
    lang: str = "zh"                  # "zh" | "en"


@app.post("/api/assess")
async def assess(req: AssessRequest, request: Request):
    if not _check_rate_limit(_client_ip(request)):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试（每小时限 20 次）")

    lang = req.lang if req.lang in ("zh", "en") else "zh"
    transport = req.transport if req.transport in ("plane", "ship") else "plane"
    vessel_type = None
    if transport == "ship":
        # unknown 是默认值：用户答不上来时按最严（小船）标准判，往安全方向错
        vessel_type = req.vessel_type if req.vessel_type in ("small", "large") else "unknown"

    try:
        bundle = evidence.build_evidence(
            req.origin, req.destination, req.date, transport, vessel_type)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"证据预取失败: {e}")

    trace = [s.as_dict() for s in bundle.trace]
    meta: dict = {}

    if not bundle.ok:
        # 证据不足 → 结论已经确定，不必再花一次模型调用
        trace.append({
            "kind": "rule", "name": "abstention_gate", "args": {},
            "ok": True, "latency_ms": 0,
            "summary": f"证据不足（缺 {len(bundle.missing)} 项），跳过模型调用直接弃权",
            "error": None,
        })
        result = rules.evaluate(None, bundle, lang=lang)
    else:
        user_message = agent.build_user_message(
            req.origin, req.destination, req.date, transport, vessel_type, bundle, lang)
        llm_output, llm_trace, meta = agent.run_agent(user_message, lang=lang)
        trace.extend(llm_trace)
        result = rules.evaluate(llm_output, bundle, lang=lang)
        trace.append({
            "kind": "rule", "name": "rule_engine", "args": {},
            "ok": True, "latency_ms": 0,
            "summary": f"decision_source={result.get('decision_source')} → {result.get('risk_level')}",
            "error": None,
        })

    result["evidence"] = bundle.as_dict()
    result["trace"] = trace
    result["meta"] = meta
    return result


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "provider": providers.active_provider(),
        "model": providers.model_id(providers.active_provider()),
        "ports_in_whitelist": len(evidence.PORTS),
    }


# ---------------------------------------------------------------------------
# 前端
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")
