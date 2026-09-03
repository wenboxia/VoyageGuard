"""
providers.py — LLM provider 配置（app.py 与 evals/ 共用）

生产用哪家由环境变量 VOYAGEGUARD_PROVIDER 决定（默认 qwen）。
每家的 model id 可由 VOYAGEGUARD_MODEL_<PROVIDER> 覆盖，无需改代码。
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()   # 让 evals 直接 import providers 时也能读到 .env


@dataclass(frozen=True)
class Provider:
    key: str            # 内部标识，如 "deepseek"
    label: str          # 展示名
    env_var: str        # API Key 的环境变量名
    base_url: str       # OpenAI 兼容端点
    default_model: str  # 默认 model id


# model id 于 2026-08-31 逐个探活确认过：key 可用 + 模型存在 + 支持 function calling。
# 各家版本号变动很快（Moonshot 的 moonshot-v1-* 已全部下线，DeepSeek 现役是 v4 系列），
# 所以不靠记忆写死——换代时用 client.models.list() 重新确认，再改这里或用环境变量覆盖。
PROVIDERS: dict[str, Provider] = {
    "qwen": Provider(
        key="qwen",
        label="Qwen (Dashscope)",
        env_var="DASHSCOPE_API_KEY",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen-plus",
    ),
    "deepseek": Provider(
        key="deepseek",
        label="DeepSeek",
        env_var="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-v4-pro",
    ),
    "kimi": Provider(
        key="kimi",
        label="Kimi (Moonshot)",
        env_var="MOONSHOT_API_KEY",
        base_url="https://api.moonshot.cn/v1",
        default_model="kimi-k3",
    ),
    "glm": Provider(
        key="glm",
        label="GLM (Zhipu)",
        env_var="ZHIPU_API_KEY",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-5",
    ),
}


def model_id(provider_key: str) -> str:
    """model id，允许用 VOYAGEGUARD_MODEL_<PROVIDER> 覆盖默认值。"""
    p = PROVIDERS[provider_key]
    return os.getenv(f"VOYAGEGUARD_MODEL_{provider_key.upper()}", p.default_model)


def api_key(provider_key: str) -> str:
    return os.getenv(PROVIDERS[provider_key].env_var, "") or ""


def has_key(provider_key: str) -> bool:
    k = api_key(provider_key)
    return bool(k) and k not in ("None", "your_api_key_here")


# Vercel 函数硬上限 60s。deepseek-v4-pro 实测单次约 27s，慢的时候能到 45s+，
# 再加上证据预取就会顶到上限，用户直接吃 504。给模型调用设超时，超了就降级到
# rule_only —— 那条路径本来就存在且验证过（L2 场景 6），比 504 好得多。
LLM_TIMEOUT_S = float(os.getenv("VOYAGEGUARD_LLM_TIMEOUT", "32"))


def make_client(provider_key: str) -> OpenAI:
    p = PROVIDERS[provider_key]
    return OpenAI(api_key=api_key(provider_key) or "missing", base_url=p.base_url,
                  timeout=LLM_TIMEOUT_S, max_retries=0)


def active_provider() -> str:
    """生产环境使用的 provider。未配置 key 时回退到第一个有 key 的。"""
    want = os.getenv("VOYAGEGUARD_PROVIDER", "qwen").lower()
    if want in PROVIDERS and has_key(want):
        return want
    for key in PROVIDERS:
        if has_key(key):
            return key
    return want if want in PROVIDERS else "qwen"


def available_providers() -> list[str]:
    """配置了 key 的 provider 列表（evals 用来自动跳过未配置的）。"""
    return [k for k in PROVIDERS if has_key(k)]
