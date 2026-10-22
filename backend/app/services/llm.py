"""OpenAI 兼容的 `/chat/completions` 调用（飞书简报点评 / 个股 AI 研判共用）。

DeepSeek、通义、硅基流动、本地 Ollama 都走同一套协议，所以这里只做一件事：
**把一段 system + user 发出去，把模型的话取回来**。提示词怎么写由调用方负责。

失败一律抛 `LlmError`（网络、4xx/5xx、空回复）：两个调用方的处置方式不同 ——
简报是增强项，失败就退化成纯简报；个股研判是用户主动点的按钮，必须把那句失败
原样告诉用户，不能装作「没有内容」。
"""

import logging

import requests

from app.config import Settings

logger = logging.getLogger(__name__)


class LlmError(RuntimeError):
    """一次调用失败（未配置 / 网络 / 4xx-5xx / 空回复）。调用方决定降级还是报错。"""


__all__ = ["LlmError", "chat", "configured"]


def configured(settings: Settings) -> bool:
    """三项都填了才算可用（与 `config.py` 里那段注释一致）。"""
    return bool(
        settings.llm_base_url.strip()
        and settings.llm_api_key.strip()
        and settings.llm_model.strip()
    )


def chat(
    settings: Settings,
    system: str,
    user: str,
    *,
    max_tokens: int = 500,
    temperature: float = 0.3,
) -> str:
    """发一轮对话，返回模型正文（去掉首尾空白与空行，**保留段落换行**）。

    ⚠️ 只做一次请求，不重试：两个调用方都是「失败就降级 / 失败就提示」，
    重试只会让用户多等一个超时。
    """
    if not configured(settings):
        raise LlmError("LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 未配置齐全")
    base = settings.llm_base_url.strip().rstrip("/")
    try:
        response = requests.post(
            f"{base}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.llm_api_key.strip()}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.llm_model.strip(),
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=settings.llm_timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 —— 统一转成 LlmError，由调用方决定怎么降级
        raise LlmError(str(exc)) from exc

    content = (payload.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    # 逐行去空白、丢掉空行：保留段落分隔（个股研判要 4 段），又不让模型的多余空行撑开页面
    lines = [line.strip() for line in str(content).splitlines()]
    text = "\n".join(line for line in lines if line).strip()
    if not text:
        raise LlmError("模型返回了空内容")
    return text
