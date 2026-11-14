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
import re
import socket
import threading
from typing import Any
from urllib.parse import urlsplit

from app.config import get_settings

logger = logging.getLogger(__name__)

ENV = "EXTERNAL_DATA_FETCH_ENABLED"

_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}

#: 看起来像 URL / 路径 / 带 query 或 userinfo 的片段：日志与异常里一律剔除
_URL_LIKE = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.-]*://\S+|\S*[?@]\S*|\S*/\S*)")
#: 只有 `localhost` 这两个名字允许固定映射到回环 IP；其它主机名不做 DNS 豁免
_LOCALHOST_IPV4 = "127.0.0.1"
_LOCALHOST_IPV6 = "::1"


def _safe_context(context: Any) -> str:
    """把来源上下文压成安全标签：去掉 URL / path / query / userinfo，限长。"""
    text = _URL_LIKE.sub(" ", str(context or ""))
    text = " ".join(text.split())
    return text[:48] or "unlabeled"


def _safe_host(value: Any) -> str:
    """从 URL 或地址里只取 hostname；拿不到就返回 `unknown`（绝不回显原串）。"""
    if isinstance(value, (tuple, list)) and value:
        value = value[0]
    text = str(value or "").strip()
    if not text:
        return "unknown"
    if "://" in text:
        try:
            return urlsplit(text).hostname or "unknown"
        except ValueError:
            return "unknown"
    # 裸 host / IP（可能带端口）
    if text.startswith("[") and "]" in text:  # [::1]:8000
        return text[1 : text.index("]")]
    try:
        ipaddress.ip_address(text)
        return text
    except ValueError:
        pass
    if text.count(":") == 1:
        return text.split(":", 1)[0]
    return text


class ExternalFetchBlocked(RuntimeError):
    """离线验收模式：外部现取被拒绝（不是网络异常，也不该重试）。

    只携带稳定 reason code 与**安全来源标签**，不带原始 URL / path / query。
    """

    def __init__(self, reason_code: str = "EXTERNAL_DATA_FETCH_DISABLED", *, source: Any = None):
        self.reason_code = reason_code
        self.source = _safe_host(source) if source is not None else None
        message = f"离线验收模式：外部数据现取被拒绝（{reason_code}）"
        if self.source:
            message += f"；来源 {self.source}"
        super().__init__(message)


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
    "udp_attempts": 0,
    "udp_blocked": 0,
    "installed": False,
    "socket_guarded": False,
}
_original_request = None
_original_create_connection = None
_original_socket_connect = None
_original_socket_connect_ex = None
_original_socket_sendto = None
_original_socket_sendmsg = None


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
    """外部来源入口的统一闸门；关闭时抛 `ExternalFetchBlocked`。

    `context` 只作为**安全标签**使用：URL / path / query / userinfo 会被剔除，
    不会进入日志或异常文本。
    """
    with _lock:
        _stats["attempts"] += 1
    if enabled():
        return
    with _lock:
        _stats["blocked"] += 1
    label = _safe_context(context)
    logger.warning("外部取数被离线闸门阻止：%s", label)
    raise ExternalFetchBlocked("EXTERNAL_DATA_FETCH_DISABLED", source=label)


def _strict_loopback_ip(host: Any) -> bool:
    """只有**严格 IP 回环**才算安全；空值、None、普通主机名一律不放行。"""
    if host is None:
        return False
    text = str(host).strip()
    if not text:
        return False
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _mapped_localhost(host: Any) -> str | None:
    """`localhost` 固定映射到回环 IP（不交给 OS 的 hosts/DNS 解析）。

    其它任何主机名（含 `localhost.localdomain`）都不豁免，必须被拦截。
    """
    text = str(host or "").strip().lower()
    if text == "localhost":
        return _LOCALHOST_IPV4
    if text == "ip6-localhost":
        return _LOCALHOST_IPV6
    return None


def _address_host(address: Any) -> Any:
    if isinstance(address, (tuple, list)) and address:
        return address[0]
    return address


def _rewrite_localhost(address: Any, mapped: str) -> Any:
    if isinstance(address, tuple) and address:
        return (mapped, *address[1:])
    if isinstance(address, list) and address:
        return [mapped, *address[1:]]
    return address


def _is_unix_socket(sock: Any) -> bool:
    af_unix = getattr(socket, "AF_UNIX", None)
    return af_unix is not None and getattr(sock, "family", None) == af_unix


def _block_socket(method: str, address: Any) -> None:
    with _lock:
        _stats["os_network_blocked"] += 1
    logger.warning("离线验收：拦截 socket 出网 %s → %s", method, _safe_host(address))
    raise ExternalFetchBlocked("NETWORK_CONNECT_BLOCKED", source=_host_for_label(address))


def _block_udp(method: str, address: Any) -> None:
    with _lock:
        _stats["udp_blocked"] += 1
    logger.warning("离线验收：拦截 UDP 出网 %s → %s", method, _safe_host(address))
    raise ExternalFetchBlocked("NETWORK_DATAGRAM_BLOCKED", source=_host_for_label(address))


def _host_for_label(address: Any) -> Any:
    host = _address_host(address)
    return host if isinstance(host, str) else None


def _guard_outbound(method: str, address: Any) -> Any:
    """离线时统一判定并返回**可交给原函数**的地址。

    - `localhost` 固定映射到 127.0.0.1（IPv6 名映射到 ::1）；
    - 严格 IP 回环放行；
    - 其它（含空 host / None / localhost.localdomain / 公网 IP / 主机名）一律拦截。
    """
    if enabled():
        return address
    host = _address_host(address)
    if _strict_loopback_ip(host):
        return address
    mapped = _mapped_localhost(host)
    if mapped is not None:
        return _rewrite_localhost(address, mapped)
    _block_socket(method, address)


def install_network_deny() -> bool:
    """安装 requests + socket 两层 egress deny（仅开关关闭时生效）。"""
    global _original_request, _original_create_connection, _original_socket_connect
    global _original_socket_connect_ex, _original_socket_sendto, _original_socket_sendmsg
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
            # 只记录 method + hostname：path / query / userinfo 绝不进日志与异常
            logger.warning("离线验收：拦截真实网络请求 %s → %s", method, _safe_host(url))
            raise ExternalFetchBlocked("NETWORK_REQUEST_BLOCKED", source=_safe_host(url))
        with _lock:
            _stats["network_attempts"] += 1
        return _original_request(self, method, url, *args, **kwargs)

    sessions.Session.request = guarded

    _original_create_connection = socket.create_connection

    def guarded_create_connection(address, *args, **kwargs):  # noqa: ANN001
        with _lock:
            _stats["os_network_attempts"] += 1
        address = _guard_outbound("create_connection", address)
        return _original_create_connection(address, *args, **kwargs)

    socket.create_connection = guarded_create_connection

    _original_socket_connect = socket.socket.connect

    def guarded_connect(self, address):  # noqa: ANN001
        if _is_unix_socket(self):
            return _original_socket_connect(self, address)
        with _lock:
            _stats["os_network_attempts"] += 1
        address = _guard_outbound("socket.connect", address)
        return _original_socket_connect(self, address)

    socket.socket.connect = guarded_connect

    if hasattr(socket.socket, "connect_ex"):
        _original_socket_connect_ex = socket.socket.connect_ex

        def guarded_connect_ex(self, address):  # noqa: ANN001
            if _is_unix_socket(self):
                return _original_socket_connect_ex(self, address)
            with _lock:
                _stats["os_network_attempts"] += 1
            address = _guard_outbound("socket.connect_ex", address)
            return _original_socket_connect_ex(self, address)

        socket.socket.connect_ex = guarded_connect_ex

    if hasattr(socket.socket, "sendto"):
        _original_socket_sendto = socket.socket.sendto

        def guarded_sendto(self, data, *args):  # noqa: ANN001
            if _is_unix_socket(self):
                return _original_socket_sendto(self, data, *args)
            address = args[-1] if args else None
            host = _address_host(address)
            if enabled() or _strict_loopback_ip(host):
                with _lock:
                    _stats["udp_attempts"] += 1
                return _original_socket_sendto(self, data, *args)
            with _lock:
                _stats["udp_attempts"] += 1
            _block_udp("socket.sendto", address)

        socket.socket.sendto = guarded_sendto

    if hasattr(socket.socket, "sendmsg"):
        _original_socket_sendmsg = socket.socket.sendmsg

        def guarded_sendmsg(self, buffers, *args):  # noqa: ANN001
            if _is_unix_socket(self):
                return _original_socket_sendmsg(self, buffers, *args)
            # sendmsg(buf, ancdata, flags, address)
            address = args[2] if len(args) >= 3 else None
            host = _address_host(address)
            if address is None or enabled() or _strict_loopback_ip(host):
                with _lock:
                    _stats["udp_attempts"] += 1
                return _original_socket_sendmsg(self, buffers, *args)
            with _lock:
                _stats["udp_attempts"] += 1
            _block_udp("socket.sendmsg", address)

        socket.socket.sendmsg = guarded_sendmsg

    with _lock:
        _stats["installed"] = True
        _stats["socket_guarded"] = True
    logger.warning(
        "已安装离线网络闸门（%s=false）：requests 与 socket 出网将在发网前失败（仅严格回环放行）",
        ENV,
    )
    return True


def uninstall_network_deny() -> None:
    """测试用：恢复 `requests` 与 socket 原始方法（不清除 locked_offline）。"""
    global _original_request, _original_create_connection, _original_socket_connect
    global _original_socket_connect_ex, _original_socket_sendto, _original_socket_sendmsg
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
    if _original_socket_connect_ex is not None:
        socket.socket.connect_ex = _original_socket_connect_ex
        _original_socket_connect_ex = None
    if _original_socket_sendto is not None:
        socket.socket.sendto = _original_socket_sendto
        _original_socket_sendto = None
    if _original_socket_sendmsg is not None:
        socket.socket.sendmsg = _original_socket_sendmsg
        _original_socket_sendmsg = None
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
            "udp_attempts",
            "udp_blocked",
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
    - `enabled` 是**有效状态**：`locked_offline=true` 时即使环境变量改回 true 也是 false；
    - `attempts` / `blocked`：来源入口（`ensure_external_fetch`）；
    - `network_attempts` / `network_blocked`：只统计 `requests` 层；
    - `os_network_attempts` / `os_network_blocked`：TCP 建连层
      （`create_connection` / `connect` / `connect_ex`，回环也算尝试）；
    - `udp_attempts` / `udp_blocked`：UDP `sendto` / `sendmsg`；
    - `dns_guard=NOT_INTERCEPTED`：DNS 查询未在进程内拦截；
    - 这些都不是「全宿主网络调用总数」，OS 级隔离是否生效需另测。
    """
    effective = enabled()
    with _lock:
        return {
            "enabled": effective,
            "locked_offline": _locked_offline,
            "mode": "offline_locked" if _locked_offline else "online",
            "switch_policy": (
                "online→offline 立即生效并锁定；offline→online 不支持（需重启进程）"
            ),
            "dns_guard": "NOT_INTERCEPTED",
            **_stats,
        }

