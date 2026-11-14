"""外部数据现取的**离线闸门**（S4-D）。

`SCHEDULER_ENABLED=false` 只能关掉定时任务，**关不掉按需路由**（例如
`/api/sectors/members` 的缓存未命中、`/api/stock/{code}/dde` 的现取、个股新闻/
公告/研报、手动同步）。S3 浏览器验收就曾在临时库上触发开盘红现取。

本模块提供两件事：

1. `ensure_external_fetch(context)`：在每个外部来源**发网之前**调用。开关关闭时
   直接抛 `ExternalFetchBlocked`，并累计 `attempts / blocked`；
2. `install_network_deny()`：在最外层 `requests` 上再兜一层。**只在隔离环境显式
   安装**（应用启动时发现开关关闭才装），任何漏网的请求也会在发网前失败，并累计
   `network_attempts / network_blocked`。生产默认开关为 true，不会安装。

开关：环境变量 `EXTERNAL_DATA_FETCH_ENABLED`（默认 true）。不要复用
`SCHEDULER_ENABLED`，两者语义不同。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

ENV = "EXTERNAL_DATA_FETCH_ENABLED"

_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


class ExternalFetchBlocked(RuntimeError):
    """离线验收模式：外部现取被拒绝（不是网络异常，也不该重试）。"""


_lock = threading.Lock()
_stats: dict[str, Any] = {
    "attempts": 0,
    "blocked": 0,
    "network_attempts": 0,
    "network_blocked": 0,
    "installed": False,
}
_original_request = None


def enabled() -> bool:
    """每次现取都读一次：环境变量优先，其次 Settings。"""
    raw = os.environ.get(ENV)
    if raw is not None and raw.strip():
        return raw.strip().lower() not in _FALSE_VALUES
    return bool(get_settings().external_data_fetch_enabled)


def ensure_external_fetch(context: str) -> None:
    """外部来源入口的统一闸门；关闭时抛 `ExternalFetchBlocked`。"""
    with _lock:
        _stats["attempts"] += 1
    if enabled():
        return
    with _lock:
        _stats["blocked"] += 1
    logger.warning("外部取数被离线闸门阻止：%s", context)
    raise ExternalFetchBlocked(
        f"离线验收模式：禁止外部数据现取（{ENV}=false），当前仅显示本地数据（{context}）"
    )


def install_network_deny() -> bool:
    """在 `requests` 最外层安装 egress deny（仅开关关闭时生效）。"""
    global _original_request
    if enabled():
        return False
    with _lock:
        if _stats["installed"]:
            return True
    import requests.sessions as sessions

    original = sessions.Session.request
    _original_request = original

    def guarded(self, method, url, *args, **kwargs):  # noqa: ANN001 - 透明包装
        if not enabled():
            with _lock:
                _stats["network_blocked"] += 1
            logger.warning("离线验收：拦截真实网络请求 %s %s", method, url)
            raise ExternalFetchBlocked(
                f"离线验收模式：拦截真实网络请求 {method} {url}"
            )
        with _lock:
            _stats["network_attempts"] += 1
        return original(self, method, url, *args, **kwargs)

    sessions.Session.request = guarded
    with _lock:
        _stats["installed"] = True
    logger.warning("已安装离线网络闸门（%s=false）：requests 调用将在发网前失败", ENV)
    return True


def uninstall_network_deny() -> None:
    """测试用：恢复 `requests` 原始方法。"""
    global _original_request
    if _original_request is None:
        return
    import requests.sessions as sessions

    sessions.Session.request = _original_request
    _original_request = None
    with _lock:
        _stats["installed"] = False


def stats() -> dict[str, Any]:
    with _lock:
        return {
            "enabled": enabled(),
            **_stats,
        }


def reset_stats() -> None:
    with _lock:
        for key in ("attempts", "blocked", "network_attempts", "network_blocked"):
            _stats[key] = 0
