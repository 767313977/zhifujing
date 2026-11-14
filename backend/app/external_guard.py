"""外部数据现取的**离线闸门**（S4-D → S5-D）。

`SCHEDULER_ENABLED=false` 只能关掉定时任务，**关不掉按需路由**（例如
`/api/sectors/members` 的缓存未命中、`/api/stock/{code}/dde` 的现取、个股新闻/
公告/研报、手动同步）。S3 浏览器验收就曾在临时库上触发开盘红现取。

本模块提供三层防护：

1. `ensure_external_fetch(context)`：在每个外部来源**发网之前**调用。开关关闭时
   直接抛 `ExternalFetchBlocked`，并累计 `attempts / blocked`；
2. `install_network_deny()`：在 `requests.Session.request` 与 `socket.create_connection`
   / `socket.socket.connect` 上兜底。任何漏网的 HTTP 客户端（urllib / httpx /
   aiohttp 同步路径等）最终都要建 socket，因此在建连前失败；**允许回环**，
   浏览器验收仍能访问 `127.0.0.1` 的后端；
3. **进程启动锁定**：一旦首次读到 `EXTERNAL_DATA_FETCH_ENABLED=false`，本进程
   生命周期内不可再切回 true（`locked_offline=true`），防止运行时改环境变量绕过。

开关：环境变量 `EXTERNAL_DATA_FETCH_ENABLED`（默认 true）。不要复用
`SCHEDULER_ENABLED`，两者语义不同。

⚠️ 这**不是** OS 级隔离：它只覆盖本进程内的 Python 网络栈，不能证明子进程、
原生库或宿主网络策略零出网。OS 层隔离是否实测见报告中的
`OS_EGRESS_DENY_NOT_VERIFIED`。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import threading
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

ENV = "EXTERNAL_DATA_FETCH_ENABLED"

_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


class ExternalFetchBlocked(RuntimeError):
    """离线验收模式：外部现取被拒绝（不是网络异常，也不该重试）。"""


_lock = threading.Lock()
#: 一旦在进程内读到 false 就锁定，重启才恢复（防止运行时 toggle 绕过）
_locked_offline = False
_stats: dict[str, Any] = {
    "attempts": 0,
    "blocked": 0,
    "network_attempts": 0,
    "network_blocked": 0,
    "os_network_attempts": 0,
    "os_network_blocked": 0,
    "installed": False,
    "socket_guarded": False,
}
_original_request = None
_original_create_connection = None
_original_socket_connect = None


def _raw_enabled() -> bool:
    raw = os.environ.get(ENV)
    if raw is not None and raw.strip():
        return raw.strip().lower() not in _FALSE_VALUES
    return bool(get_settings().external_data_fetch_enabled)


def enabled() -> bool:
    """每次现取都读一次；一旦读到 false 就**永久锁定**到进程结束。"""
    global _locked_offline
    with _lock:
        if _locked_offline:
            return False
        value = _raw_enabled()
        if not value:
            _locked_offline = True
        return value


def lock_offline_mode() -> bool:
    """显式锁定（应用启动时调用）；返回是否处于离线锁定。"""
    if enabled():
        return False
    return True


def offline_locked() -> bool:
    with _lock:
        return _locked_offline


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


def _is_loopback(host: Any) -> bool:
    """只放行回环；主机名只接受 localhost（不做 DNS 解析）。"""
    if host is None:
        return True
    text = str(host).strip().lower()
    if text in ("", "localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _block_socket(method: str, address: Any) -> None:
    with _lock:
        _stats["os_network_blocked"] += 1
    logger.warning("离线验收：拦截 socket 出网 %s %r", method, address)
    raise ExternalFetchBlocked(f"离线验收模式：拦截真实网络连接 {method} {address!r}")


def install_network_deny() -> bool:
    """安装 requests + socket 两层 egress deny（仅开关关闭时生效）。"""
    global _original_request, _original_create_connection, _original_socket_connect
    if enabled():
        return False
    with _lock:
        if _stats["installed"]:
            return True

    import requests.sessions as sessions

    _original_request = sessions.Session.request

    def guarded(self, method, url, *args, **kwargs):  # noqa: ANN001 - 透明包装
        if not enabled():
            with _lock:
                _stats["network_blocked"] += 1
            logger.warning("离线验收：拦截真实网络请求 %s %s", method, url)
            raise ExternalFetchBlocked(f"离线验收模式：拦截真实网络请求 {method} {url}")
        with _lock:
            _stats["network_attempts"] += 1
        return _original_request(self, method, url, *args, **kwargs)

    sessions.Session.request = guarded

    _original_create_connection = socket.create_connection

    def guarded_create_connection(address, *args, **kwargs):  # noqa: ANN001
        host = address[0] if isinstance(address, (tuple, list)) and address else address
        with _lock:
            _stats["os_network_attempts"] += 1
        if not enabled() and not _is_loopback(host):
            _block_socket("create_connection", address)
        return _original_create_connection(address, *args, **kwargs)

    socket.create_connection = guarded_create_connection

    _original_socket_connect = socket.socket.connect

    def guarded_connect(self, address):  # noqa: ANN001
        af_unix = getattr(socket, "AF_UNIX", None)
        if af_unix is not None and getattr(self, "family", None) == af_unix:
            return _original_socket_connect(self, address)
        host = address[0] if isinstance(address, (tuple, list)) and address else address
        with _lock:
            _stats["os_network_attempts"] += 1
        if not enabled() and not _is_loopback(host):
            _block_socket("socket.connect", address)
        return _original_socket_connect(self, address)

    socket.socket.connect = guarded_connect

    with _lock:
        _stats["installed"] = True
        _stats["socket_guarded"] = True
    logger.warning(
        "已安装离线网络闸门（%s=false）：requests 与 socket 出网将在发网前失败（回环放行）",
        ENV,
    )
    return True


def uninstall_network_deny() -> None:
    """测试用：恢复 `requests` 与 socket 原始方法（不清除 locked_offline）。"""
    global _original_request, _original_create_connection, _original_socket_connect
    import requests.sessions as sessions

    if _original_request is not None:
        sessions.Session.request = _original_request
        _original_request = None
    if _original_create_connection is not None:
        socket.create_connection = _original_create_connection
        _original_create_connection = None
    if _original_socket_connect is not None:
        socket.socket.connect = _original_socket_connect
        _original_socket_connect = None
    with _lock:
        _stats["installed"] = False
        _stats["socket_guarded"] = False


def reset_stats() -> None:
    with _lock:
        for key in (
            "attempts",
            "blocked",
            "network_attempts",
            "network_blocked",
            "os_network_attempts",
            "os_network_blocked",
        ):
            _stats[key] = 0


def reset_guard_state() -> None:
    """**仅测试**：恢复补丁并清除进程级锁定。业务代码不得调用。"""
    global _locked_offline
    uninstall_network_deny()
    reset_stats()
    with _lock:
        _locked_offline = False


def stats() -> dict[str, Any]:
    """只返回计数与布尔开关，**不包含域名、URL、token 或数据库路径**。

    语义：
    - `network_attempts` / `network_blocked`：只统计 `requests` 层；
    - `os_network_attempts` / `os_network_blocked`：socket 建连层（回环也算尝试）；
    - `locked_offline`：进程启动后是否已锁定离线；
    - 这些都不是「全宿主网络调用总数」，OS 级隔离是否生效需另测。
    """
    with _lock:
        return {
            "enabled": _raw_enabled(),
            "locked_offline": _locked_offline,
            "mode": "offline_locked" if _locked_offline else "online",
            **_stats,
        }

