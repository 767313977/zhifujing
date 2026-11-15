"""外部数据现取的**离线闸门 + 统一出站策略**（S4-D → S5-D → S6-A）。

`SCHEDULER_ENABLED=false` 只能关掉定时任务，**关不掉按需路由**（例如
`/api/sectors/members` 的缓存未命中、`/api/stock/{code}/dde` 的现取、个股新闻/
公告/研报、手动同步）。S3 浏览器验收就曾在临时库上触发开盘红现取。

本模块提供四层防护：

1. `ensure_external_fetch(context)`：在每个外部来源**发网之前**调用。开关关闭时
   直接抛 `ExternalFetchBlocked`，并累计 `attempts / blocked`；
2. `install_network_deny()`：在 `requests.Session.request`、socket 建连/发送、
   `ssl.SSLSocket.send / sendall / sendfile / write`、`socket.socket.sendfile`、
   DNS 入口（`getaddrinfo` / `gethostbyname[_ex]`）与代理探测
   （`urllib.request.getproxies`）上兜底。**允许回环**，浏览器验收仍能访问
   `127.0.0.1` 的后端；
3. **在线允许名单**（S6-A1）：`EXTERNAL_DATA_FETCH_ENABLED=true` 时也不再无条件
   放行 —— 只能访问 `deploy/s6/egress-policy.json` 显式列出的域名 + 端口，
   IP literal 除严格回环外一律拒绝（见 `app/egress_policy.py`）；
4. **进程启动锁定**：一旦首次读到 `EXTERNAL_DATA_FETCH_ENABLED=false`，本进程
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

from app import egress_policy
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
    "stream_attempts": 0,
    "stream_blocked": 0,
    "sendfile_attempts": 0,
    "sendfile_blocked": 0,
    "dns_attempts": 0,
    "dns_blocked": 0,
    "policy_attempts": 0,
    "policy_allowed": 0,
    "policy_denied": 0,
    "policy_errors": 0,
    "ip_literal_denied": 0,
    "installed": False,
    "socket_guarded": False,
    "dns_guarded": False,
}
#: 实际安装成功的写路径能力集合（stats 原样暴露，不能只报一个总布尔值）
_guard_capabilities: dict[str, bool] = {}
_original_request = None
_original_create_connection = None
_original_socket_connect = None
_original_socket_connect_ex = None
_original_socket_sendto = None
_original_socket_sendmsg = None
_original_socket_send = None
_original_socket_sendall = None
_original_socket_sendfile = None
_original_ssl_send = None
_original_ssl_sendall = None
_original_ssl_sendfile = None
_original_ssl_write = None
_original_getaddrinfo = None
_original_gethostbyname = None
_original_gethostbyname_ex = None
_original_getproxies = None
#: 由**受控解析**记录下来的公网 IP（域名 → 地址）。直接连 IP literal 时只有命中
#: 这个集合才放行，避免「解析被允许的域名后改连任意 IP」或干脆绕过域名规则。
_authorized_ips: dict[str, bool] = {}
_AUTHORIZED_IP_CAP = 2048
#: TLS `sendall` / `sendfile` 内部会回调 `self.send()`；用**按 socket 的嵌套栈**记录
#: 当前线程正在执行外层写操作的 socket，避免线程级深度让另一个 socket 共享豁免。
_tls_call_state = threading.local()
_install_lock = threading.Lock()


def _tls_nested_sockets() -> list[int]:
    stack = getattr(_tls_call_state, "active_sockets", None)
    if stack is None:
        stack = []
        _tls_call_state.active_sockets = stack
    return stack


def _enter_tls_nested(sock: Any) -> None:
    _tls_nested_sockets().append(id(sock))


def _leave_tls_nested(sock: Any) -> None:
    stack = _tls_nested_sockets()
    if stack and stack[-1] == id(sock):
        stack.pop()
    elif id(sock) in stack:  # 异常路径兜底，保证不残留
        stack.remove(id(sock))


def _is_tls_nested(sock: Any) -> bool:
    return id(sock) in _tls_nested_sockets()


def _raw_enabled() -> bool:
    raw = os.environ.get(ENV)
    if raw is not None and raw.strip():
        return raw.strip().lower() not in _FALSE_VALUES
    return bool(get_settings().external_data_fetch_enabled)


def enabled() -> bool:
    """每次现取都读一次；一旦读到 false 就**永久锁定**到进程结束。

    R2-P1-02：首次 online → offline 时**同时**确保网络 wrapper 已安装。
    启动阶段 `main.lifespan()` 也会无条件调用 `install_network_deny()`，
    因此运行时切换不会出现「已离线但裸 socket 仍可出网」的窗口。
    """
    global _locked_offline
    with _lock:
        if _locked_offline:
            return False
        value = _raw_enabled()
        if value:
            return True
        _locked_offline = True
    # 锁外安装：`_install_wrappers()` 幂等且不回调 enabled()，避免死锁
    _install_wrappers()
    return False


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


def _loopback_policy_allows(host: Any, port: int | None) -> bool:
    """Loopback is not a free pass: only policy-listed local ports may connect."""
    try:
        return egress_policy.host_allowed(host, port)
    except Exception:  # noqa: BLE001 - policy errors must fail closed
        return False


def _block_loopback(method: str, host: Any, port: int | None) -> None:
    _block_policy(method, host, port, "LOOPBACK_PORT_DENIED")


def _guard_outbound(method: str, address: Any) -> Any:
    """统一判定并返回**可交给原函数**的地址。

    - `localhost` 固定映射到 127.0.0.1（IPv6 名映射到 ::1）；
    - 严格 IP 回环放行；
    - 在线模式：按 `egress_policy` 允许名单判定（域名 + 端口），
      IP literal 只接受由受控解析登记过的地址；
    - 离线模式：其它（含空 host / None / localhost.localdomain / 公网 IP / 主机名）
      一律拦截。
    """
    host = _address_host(address)
    if _strict_loopback_ip(host):
        if enabled() and not _loopback_policy_allows(host, _port_of(address)):
            _block_loopback(method, host, _port_of(address))
        return address
    mapped = _mapped_localhost(host)
    if mapped is not None:
        if enabled() and not _loopback_policy_allows(host, _port_of(address)):
            _block_loopback(method, host, _port_of(address))
        return _rewrite_localhost(address, mapped)
    if enabled():
        port = _port_of(address)
        if _is_ip_text(host):
            if _ip_authorized(host) and _is_public_ip(host):
                with _lock:
                    _stats["policy_allowed"] += 1
                return address
            with _lock:
                _stats["ip_literal_denied"] += 1
            _block_policy(method, host, port, "IP_LITERAL_DENIED")
        _policy_allows(host, port, method)
        return address
    _block_socket(method, address)


def _policy_environment() -> str:
    return (os.environ.get("FUPAN_ENV") or "development").strip().lower() or "development"


def _port_of(address: Any) -> int | None:
    if isinstance(address, (tuple, list)) and len(address) >= 2:
        return _as_port(address[1])
    return None


def _as_port(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _is_ip_text(value: Any) -> bool:
    try:
        ipaddress.ip_address(str(value))
        return True
    except ValueError:
        return False


def _is_public_ip(value: Any) -> bool:
    """Only globally routable addresses may be pinned as an allowed peer.

    Private / loopback / link-local / CGNAT / reserved / multicast / metadata
    addresses are rejected.  This is the second half of DNS-rebinding defence:
    an allowlisted hostname that resolves (or re-resolves) to 10.0.0.5 or
    169.254.169.254 must not become an authorised outbound peer.
    """
    try:
        address = ipaddress.ip_address(str(value))
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_global
    return address.is_global


def _record_authorized_ip(sockaddr: Any) -> None:
    host = _address_host(sockaddr)
    if not _is_ip_text(host):
        return
    try:
        if ipaddress.ip_address(str(host)).is_loopback:
            return
    except ValueError:
        return
    if not _is_public_ip(host):
        # DNS rebinding / compromised vendor DNS must not authorise private,
        # link-local or metadata addresses (169.254.169.254 included).
        return
    with _lock:
        if len(_authorized_ips) >= _AUTHORIZED_IP_CAP:
            _authorized_ips.clear()
        _authorized_ips[str(host)] = True


def _ip_authorized(host: Any) -> bool:
    with _lock:
        return str(host) in _authorized_ips


def _block_policy(action: str, host: Any, port: int | None, reason: str) -> None:
    with _lock:
        _stats["policy_denied"] += 1
        if reason.startswith("POLICY_"):
            _stats["policy_errors"] += 1
    # 只记录动作 + hostname + 端口；URL / query / userinfo 绝不进日志
    logger.warning("出站策略拒绝 %s → %s:%s（%s）", action, _safe_host(host), port, reason)
    if reason.startswith("POLICY_"):
        code = "EGRESS_POLICY_ERROR"
    elif reason == "PROXY_NOT_ALLOWED":
        code = "EGRESS_PROXY_DENIED"
    else:
        code = "EGRESS_DESTINATION_DENIED"
    raise ExternalFetchBlocked(code, source=_safe_host(host))


def _guarded_getproxies():  # noqa: ANN001
    """代理出口：策略没有显式授权代理时，把系统代理 / 环境变量代理全部过滤掉。

    ⚠️ 这条很关键：开发机与不少服务器配了系统级代理（Windows 注册表 / 环境变量），
    请求会先连到 `127.0.0.1:7890` 这类**回环代理**。回环本身是放行的，于是应用层
    看起来「只连了本机」，真正的公网访问却由代理进程完成 —— 完全绕过本闸门。
    因此这里默认返回空表；只有策略文件 `proxies` 段显式列出代理时才恢复原行为。
    """
    if egress_policy.proxies_allowed():
        return _original_getproxies()
    return {}


def _configured_proxy(session: Any, url: str) -> str | None:
    """返回本次请求实际会用到的代理地址；没有则 None。异常按「有代理」处理。"""
    try:
        explicit = getattr(session, "proxies", None) or {}
        for key in ("http", "https", "all"):
            value = explicit.get(key)
            if value:
                return str(value)
        if getattr(session, "trust_env", False):
            import urllib.request as urllib_request

            env = urllib_request.getproxies() or {}
            for key in ("http", "https", "all"):
                value = env.get(key)
                if value:
                    return str(value)
    except Exception:  # noqa: BLE001 - 判定不了就按有代理（fail closed）
        return "unknown"
    return None


def _policy_allows(host: Any, port: int | None, action: str) -> bool:
    with _lock:
        _stats["policy_attempts"] += 1
    if egress_policy.host_allowed(host, port, environment=_policy_environment()):
        with _lock:
            _stats["policy_allowed"] += 1
        return True
    _block_policy(action, host, port, egress_policy.describe_denial(host, port))


def _guarded_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: ANN001
    """DNS 入口闸门：先按策略判定主机（+端口），再做真实解析。

    为什么必须在解析前判：DNS 本身也是一次出网/信息泄露；而且「解析成功」
    不能等同于「目标已获授权」。解析结果里的公网 IP 会被登记，供随后
    `socket.connect(ip)` 的白名单校验使用。
    """
    with _lock:
        _stats["dns_attempts"] += 1
    if host is None or host == "":
        # 本机绑定（getaddrinfo(None, port, AI_PASSIVE)）不出网，放行
        return _original_getaddrinfo(host, port, family, type, proto, flags)
    mapped = _mapped_localhost(host)
    if mapped is not None:
        host = mapped
    if _strict_loopback_ip(host):
        return _original_getaddrinfo(host, port, family, type, proto, flags)
    if not enabled():
        with _lock:
            _stats["dns_blocked"] += 1
        logger.warning("离线验收：拦截 DNS 解析 → %s", _safe_host(host))
        raise ExternalFetchBlocked("NETWORK_DNS_BLOCKED", source=_safe_host(host))
    _policy_allows(host, _as_port(port), "dns.getaddrinfo")
    result = _original_getaddrinfo(host, port, family, type, proto, flags)
    for item in result:
        try:
            _record_authorized_ip(item[4])
        except (IndexError, TypeError):
            continue
    return result


def _guarded_gethostbyname(host):  # noqa: ANN001
    with _lock:
        _stats["dns_attempts"] += 1
    mapped = _mapped_localhost(host)
    if mapped is not None:
        host = mapped
    if _strict_loopback_ip(host):
        return _original_gethostbyname(host)
    if not enabled():
        with _lock:
            _stats["dns_blocked"] += 1
        raise ExternalFetchBlocked("NETWORK_DNS_BLOCKED", source=_safe_host(host))
    _policy_allows(host, None, "dns.gethostbyname")
    address = _original_gethostbyname(host)
    _record_authorized_ip((address, 0))
    return address


def _guarded_gethostbyname_ex(host):  # noqa: ANN001
    with _lock:
        _stats["dns_attempts"] += 1
    mapped = _mapped_localhost(host)
    if mapped is not None:
        host = mapped
    if _strict_loopback_ip(host):
        return _original_gethostbyname_ex(host)
    if not enabled():
        with _lock:
            _stats["dns_blocked"] += 1
        raise ExternalFetchBlocked("NETWORK_DNS_BLOCKED", source=_safe_host(host))
    _policy_allows(host, None, "dns.gethostbyname_ex")
    _name, aliases, addresses = _original_gethostbyname_ex(host)
    for address in addresses:
        _record_authorized_ip((address, 0))
    return _name, aliases, addresses


def _peer_host(sock: Any) -> str | None:
    """已连接 socket 的对端 host；拿不到返回 None（调用方按 fail closed 处理）。"""
    try:
        peer = sock.getpeername()
    except OSError:
        return None
    host = _address_host(peer)
    return host if isinstance(host, str) else None


def _guarded_request(self, method, url, *args, **kwargs):  # noqa: ANN001 - 透明包装
    if not enabled():
        with _lock:
            _stats["network_blocked"] += 1
        # 只记录 method + hostname：path / query / userinfo 绝不进日志与异常
        logger.warning("离线验收：拦截真实网络请求 %s → %s", method, _safe_host(url))
        raise ExternalFetchBlocked("NETWORK_REQUEST_BLOCKED", source=_safe_host(url))
    with _lock:
        _stats["network_attempts"] += 1
    # 在线也要按允许名单判：这是最早的入口，能给出明确拒绝原因；
    # 重定向后的最终目标会在 DNS / connect 层再判一次（E20）。
    try:
        parts = urlsplit(str(url))
    except ValueError:
        parts = None
    host = parts.hostname if parts else None
    if host:
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            port = None
        if _strict_loopback_ip(host) or _mapped_localhost(host) is not None:
            # 回环不是免费通行证：只允许策略列出的本机端口（默认 5000 KPH 代理）。
            if not _loopback_policy_allows(host, port):
                _block_loopback("requests", host, port)
            # 回环请求**强制直连**：不允许被系统代理劫持到本机代理再出公网
            kwargs["proxies"] = {"http": None, "https": None, "all": None}
        elif _is_ip_text(host):
            if not (_ip_authorized(host) and _is_public_ip(host)):
                with _lock:
                    _stats["ip_literal_denied"] += 1
                _block_policy("requests", host, port, "IP_LITERAL_DENIED")
        else:
            _policy_allows(host, port, "requests")
            if _configured_proxy(self, str(url)) is not None:
                _block_policy("requests", host, port, "PROXY_NOT_ALLOWED")
    return _original_request(self, method, url, *args, **kwargs)


def _guarded_create_connection(address, *args, **kwargs):  # noqa: ANN001
    with _lock:
        _stats["os_network_attempts"] += 1
    address = _guard_outbound("create_connection", address)
    return _original_create_connection(address, *args, **kwargs)


def _guarded_connect(self, address):  # noqa: ANN001
    if _is_unix_socket(self):
        return _original_socket_connect(self, address)
    with _lock:
        _stats["os_network_attempts"] += 1
    address = _guard_outbound("socket.connect", address)
    return _original_socket_connect(self, address)


def _guarded_connect_ex(self, address):  # noqa: ANN001
    if _is_unix_socket(self):
        return _original_socket_connect_ex(self, address)
    with _lock:
        _stats["os_network_attempts"] += 1
    address = _guard_outbound("socket.connect_ex", address)
    return _original_socket_connect_ex(self, address)


def _guarded_sendto(self, data, *args):  # noqa: ANN001
    with _lock:
        _stats["udp_attempts"] += 1
    if _is_unix_socket(self):
        return _original_socket_sendto(self, data, *args)
    address = args[-1] if args else None
    if enabled():
        host = _address_host(address)
        if _strict_loopback_ip(host) or _mapped_localhost(host) is not None:
            with _lock:
                _stats["policy_allowed"] += 1
            return _original_socket_sendto(self, data, *args)
        _block_udp("socket.sendto", address)
    if not _strict_loopback_ip(_address_host(address)):
        _block_udp("socket.sendto", address)
    return _original_socket_sendto(self, data, *args)


def _sendmsg_address(args: tuple) -> Any:
    # sendmsg(buf, ancdata, flags, address)
    return args[2] if len(args) >= 3 else None


def _guarded_sendmsg(self, buffers, *args):  # noqa: ANN001
    is_dgram = bool(getattr(self, "type", 0) & socket.SOCK_DGRAM)
    with _lock:
        _stats["udp_attempts" if is_dgram else "stream_attempts"] += 1
    if _is_unix_socket(self):
        return _original_socket_sendmsg(self, buffers, *args)
    if enabled():
        if is_dgram:
            # 在线同样不允许任意 UDP：只放行回环（本机服务发现等）。
            target = _sendmsg_address(args)
            if target is None:
                target = _peer_host(self)
            host = _address_host(target) if target is not None else None
            if host is not None and (
                _strict_loopback_ip(host) or _mapped_localhost(host) is not None
            ):
                with _lock:
                    _stats["policy_allowed"] += 1
                return _original_socket_sendmsg(self, buffers, *args)
            with _lock:
                _stats["udp_blocked"] += 1
                _stats["policy_denied"] += 1
            logger.warning("出站策略拒绝 UDP sendmsg → %s", _safe_host(host))
            raise ExternalFetchBlocked(
                "NETWORK_DATAGRAM_BLOCKED", source=_safe_host(host)
            )
        # 流式 sendmsg：显式地址或已连接对端必须是回环或受控解析登记过的地址
        target = _sendmsg_address(args) or _peer_host(self)
        if target is not None:
            host = _address_host(target)
            if not (
                _strict_loopback_ip(host) or (_ip_authorized(host) and _is_public_ip(host))
            ):
                with _lock:
                    _stats["stream_blocked"] += 1
                    _stats["policy_denied"] += 1
                logger.warning("出站策略拒绝 stream sendmsg → %s", _safe_host(host))
                raise ExternalFetchBlocked(
                    "NETWORK_STREAM_SEND_BLOCKED", source=_safe_host(host)
                )
        with _lock:
            _stats["policy_allowed"] += 1
        return _original_socket_sendmsg(self, buffers, *args)
    # 显式 address 或已连接对端都可以作为目标；两者都拿不到时**不得放行**
    target = _sendmsg_address(args)
    if target is None:
        target = _peer_host(self)
    if target is None:
        with _lock:
            _stats["udp_blocked" if is_dgram else "stream_blocked"] += 1
        logger.warning("离线验收：拦截无目标地址的 socket.sendmsg")
        raise ExternalFetchBlocked("NETWORK_SEND_TARGET_UNKNOWN")
    if not _strict_loopback_ip(_address_host(target)):
        with _lock:
            _stats["udp_blocked" if is_dgram else "stream_blocked"] += 1
        logger.warning("离线验收：拦截 socket.sendmsg → %s", _safe_host(target))
        raise ExternalFetchBlocked(
            "NETWORK_DATAGRAM_BLOCKED" if is_dgram else "NETWORK_STREAM_SEND_BLOCKED",
            source=_address_host(target),
        )
    return _original_socket_sendmsg(self, buffers, *args)


def _assert_stream_peer_allowed(
    sock: Any, method: str, *, counter: str = "stream"
) -> None:
    """所有「已连接 socket 写」共用的 fail-closed 判定入口。

    - Unix domain socket 放行（本机 IPC，不出网）；
    - 在线时只放行回环或**受控解析登记过的**公网 IP（未登记的已连接 socket 一律拒绝）；
    - 离线时按 `getpeername()` 判定：拿不到对端 → fail closed；非严格回环 → 阻断；
    - 已连接 socket 在切换 offline 后每次发送都会重新判定，不依赖建连时的状态。
    """
    if _is_unix_socket(sock):
        return
    target = _peer_host(sock)
    if enabled():
        # 在线也校验「已连接 socket 的对端」：防止建连时通过、发送时改到未授权目标。
        # 拿不到对端时不在这里阻断（未连接 socket 的 send 本身会抛 OSError），
        # 建连层的策略判定已经生效。
        if target is None or _strict_loopback_ip(target):
            # 回环端口在建连/请求入口已判定；此处无法区分「客户端 socket 的
            # 服务端端口」与「服务端 socket 的客户端临时端口」，再按端口拒绝
            # 会误伤本机 HTTP 服务。已连接 socket 只校验是否公网授权。
            return
        if _ip_authorized(target) and _is_public_ip(target):
            return
        with _lock:
            _stats[f"{counter}_blocked"] += 1
            _stats["policy_denied"] += 1
        logger.warning("出站策略拒绝已连接 socket %s → %s", method, _safe_host(target))
        raise ExternalFetchBlocked(
            "NETWORK_SENDFILE_BLOCKED" if counter == "sendfile" else "NETWORK_STREAM_SEND_BLOCKED",
            source=target,
        )
    if target is None:
        with _lock:
            _stats[f"{counter}_blocked"] += 1
        logger.warning("离线验收：拦截无对端地址的 %s 发送", method)
        raise ExternalFetchBlocked("NETWORK_SEND_TARGET_UNKNOWN")
    if not _strict_loopback_ip(target):
        with _lock:
            _stats[f"{counter}_blocked"] += 1
        logger.warning("离线验收：拦截已连接 socket %s → %s", method, _safe_host(target))
        raise ExternalFetchBlocked(
            "NETWORK_SENDFILE_BLOCKED" if counter == "sendfile" else "NETWORK_STREAM_SEND_BLOCKED",
            source=target,
        )


def _guarded_send(self, data, flags=0):  # noqa: ANN001
    with _lock:
        _stats["stream_attempts"] += 1
    _assert_stream_peer_allowed(self, "send")
    return _original_socket_send(self, data, flags)


def _guarded_sendall(self, data, flags=0):  # noqa: ANN001
    with _lock:
        _stats["stream_attempts"] += 1
    _assert_stream_peer_allowed(self, "sendall")
    return _original_socket_sendall(self, data, flags)


def _guarded_sendfile(self, file, offset=0, count=None):  # noqa: ANN001
    with _lock:
        _stats["sendfile_attempts"] += 1
    _assert_stream_peer_allowed(self, "sendfile", counter="sendfile")
    return _original_socket_sendfile(self, file, offset, count)


def _guarded_ssl_send(self, data, flags=0):  # noqa: ANN001
    # `SSLSocket.sendall()` / `sendfile()` 内部会回调本方法：
    # - 同一个 socket 的嵌套调用不重复增加公开 API 计数；
    # - 但**仍必须重新执行 peer/offline 判定**，否则发送中途切离线会被绕过；
    # - 另一个 socket 不共享嵌套豁免，必须完整计数与判定。
    if not _is_tls_nested(self):
        with _lock:
            _stats["stream_attempts"] += 1
    _assert_stream_peer_allowed(self, "ssl.send")
    return _original_ssl_send(self, data, flags)


def _guarded_ssl_sendall(self, data, flags=0):  # noqa: ANN001
    with _lock:
        _stats["stream_attempts"] += 1
    _assert_stream_peer_allowed(self, "ssl.sendall")
    _enter_tls_nested(self)
    try:
        return _original_ssl_sendall(self, data, flags)
    finally:
        _leave_tls_nested(self)


def _guarded_ssl_sendfile(self, file, offset=0, count=None):  # noqa: ANN001
    with _lock:
        _stats["sendfile_attempts"] += 1
    _assert_stream_peer_allowed(self, "ssl.sendfile", counter="sendfile")
    _enter_tls_nested(self)
    try:
        return _original_ssl_sendfile(self, file, offset, count)
    finally:
        _leave_tls_nested(self)


def _guarded_ssl_write(self, data):  # noqa: ANN001
    """`SSLSocket.write()` 直接走 `_sslobj.write()`，不经过 `send()`，必须单独保护。"""
    with _lock:
        _stats["stream_attempts"] += 1
    _assert_stream_peer_allowed(self, "ssl.write")
    return _original_ssl_write(self, data)


def _install_wrappers() -> bool:
    """安装 requests + socket 轻量 wrapper（**总是安装**，幂等）。

    wrapper 每次调用时再读 `enabled()`：在线透明放行，离线阻断非回环。
    这样：
    - 启动在线时也已安装，运行中 online → offline 没有「未安装拦截器」窗口；
    - 安装失败直接抛出，由启动流程 fail closed，不会静默留下
      `enabled=false` 但 `socket_guarded=false` 的状态。
    """
    global _original_request, _original_create_connection, _original_socket_connect
    global _original_socket_connect_ex, _original_socket_sendto, _original_socket_sendmsg
    global _original_socket_send, _original_socket_sendall, _original_socket_sendfile
    global _original_ssl_send, _original_ssl_sendall, _original_ssl_sendfile
    global _original_ssl_write
    global _original_getaddrinfo, _original_gethostbyname, _original_gethostbyname_ex
    global _original_getproxies
    with _install_lock:
        if _stats["installed"]:
            return True
        import ssl as ssl_module
        import urllib.request as urllib_request
        import requests.sessions as sessions
        import requests.utils as requests_utils

        _original_request = sessions.Session.request
        _original_create_connection = socket.create_connection
        _original_socket_connect = socket.socket.connect
        _original_socket_connect_ex = getattr(socket.socket, "connect_ex", None)
        _original_socket_sendto = getattr(socket.socket, "sendto", None)
        _original_socket_sendmsg = getattr(socket.socket, "sendmsg", None)
        _original_socket_send = socket.socket.send
        _original_socket_sendall = socket.socket.sendall
        _original_socket_sendfile = getattr(socket.socket, "sendfile", None)
        _original_ssl_send = getattr(ssl_module.SSLSocket, "send", None)
        _original_ssl_sendall = getattr(ssl_module.SSLSocket, "sendall", None)
        _original_ssl_sendfile = getattr(ssl_module.SSLSocket, "sendfile", None)
        _original_ssl_write = getattr(ssl_module.SSLSocket, "write", None)
        _original_getaddrinfo = socket.getaddrinfo
        _original_gethostbyname = socket.gethostbyname
        _original_gethostbyname_ex = socket.gethostbyname_ex
        _original_getproxies = urllib_request.getproxies

        sessions.Session.request = _guarded_request
        socket.create_connection = _guarded_create_connection
        socket.socket.connect = _guarded_connect
        if _original_socket_connect_ex is not None:
            socket.socket.connect_ex = _guarded_connect_ex
        if _original_socket_sendto is not None:
            socket.socket.sendto = _guarded_sendto
        if _original_socket_sendmsg is not None:
            socket.socket.sendmsg = _guarded_sendmsg
        socket.socket.send = _guarded_send
        socket.socket.sendall = _guarded_sendall
        # DNS 入口：解析前判策略，解析后登记授权 IP
        socket.getaddrinfo = _guarded_getaddrinfo
        socket.gethostbyname = _guarded_gethostbyname
        socket.gethostbyname_ex = _guarded_gethostbyname_ex
        # 代理出口：默认过滤掉系统 / 环境变量代理（见 _guarded_getproxies）
        urllib_request.getproxies = _guarded_getproxies
        requests_utils.getproxies = _guarded_getproxies
        # 普通 socket 的 sendfile：POSIX 走 os.sendfile，Windows 走 send() fallback；
        # 两种情况都必须在进入原实现前完成 peer 判定。
        if _original_socket_sendfile is not None:
            socket.socket.sendfile = _guarded_sendfile
        # TLS 子类自己实现 send/sendall/sendfile，必须单独 patch，不能依赖基类。
        if _original_ssl_send is not None:
            ssl_module.SSLSocket.send = _guarded_ssl_send
        if _original_ssl_sendall is not None:
            ssl_module.SSLSocket.sendall = _guarded_ssl_sendall
        if _original_ssl_sendfile is not None:
            ssl_module.SSLSocket.sendfile = _guarded_ssl_sendfile
        if _original_ssl_write is not None:
            ssl_module.SSLSocket.write = _guarded_ssl_write

        _guard_capabilities.clear()
        _guard_capabilities.update(
            {
                "requests": True,
                "socket_connect": True,
                "socket_stream": True,
                "socket_sendmsg": _original_socket_sendmsg is not None,
                "socket_sendfile": _original_socket_sendfile is not None,
                "ssl_stream": _original_ssl_send is not None
                and _original_ssl_sendall is not None,
                "ssl_sendfile": _original_ssl_sendfile is not None,
                "ssl_write": _original_ssl_write is not None,
                "dns": True,
            }
        )
        with _lock:
            _stats["installed"] = True
            _stats["socket_guarded"] = True
            _stats["dns_guarded"] = True
    logger.warning(
        "已安装离线网络闸门 wrapper（%s）：在线按允许名单放行，离线仅严格回环放行"
        "（含 DNS / TLS / sendfile）",
        ENV,
    )
    return True


def install_network_deny() -> bool:
    """安装轻量 wrapper 并返回当前是否处于离线锁定。

    与旧行为不同：**在线时也安装**（透明放行），避免运行时切换出现无拦截窗口。
    安装失败向上抛出，启动流程按 fail closed 处理。
    """
    _install_wrappers()
    return not enabled()


def uninstall_network_deny() -> None:
    """测试用：恢复 `requests`、socket 与 TLS/sendfile 原始方法（不清除 locked_offline）。"""
    global _original_request, _original_create_connection, _original_socket_connect
    global _original_socket_connect_ex, _original_socket_sendto, _original_socket_sendmsg
    global _original_socket_send, _original_socket_sendall, _original_socket_sendfile
    global _original_ssl_send, _original_ssl_sendall, _original_ssl_sendfile
    global _original_ssl_write
    global _original_getaddrinfo, _original_gethostbyname, _original_gethostbyname_ex
    global _original_getproxies
    import ssl as ssl_module
    import urllib.request as urllib_request
    import requests.sessions as sessions
    import requests.utils as requests_utils

    if _original_getproxies is not None:
        urllib_request.getproxies = _original_getproxies
        requests_utils.getproxies = _original_getproxies
        _original_getproxies = None
    if _original_getaddrinfo is not None:
        socket.getaddrinfo = _original_getaddrinfo
        _original_getaddrinfo = None
    if _original_gethostbyname is not None:
        socket.gethostbyname = _original_gethostbyname
        _original_gethostbyname = None
    if _original_gethostbyname_ex is not None:
        socket.gethostbyname_ex = _original_gethostbyname_ex
        _original_gethostbyname_ex = None

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
    if _original_socket_send is not None:
        socket.socket.send = _original_socket_send
        _original_socket_send = None
    if _original_socket_sendall is not None:
        socket.socket.sendall = _original_socket_sendall
        _original_socket_sendall = None
    if _original_socket_sendfile is not None:
        socket.socket.sendfile = _original_socket_sendfile
        _original_socket_sendfile = None
    if _original_ssl_send is not None:
        ssl_module.SSLSocket.send = _original_ssl_send
        _original_ssl_send = None
    if _original_ssl_sendall is not None:
        ssl_module.SSLSocket.sendall = _original_ssl_sendall
        _original_ssl_sendall = None
    if _original_ssl_sendfile is not None:
        ssl_module.SSLSocket.sendfile = _original_ssl_sendfile
        _original_ssl_sendfile = None
    if _original_ssl_write is not None:
        ssl_module.SSLSocket.write = _original_ssl_write
        _original_ssl_write = None
    _guard_capabilities.clear()
    with _lock:
        _authorized_ips.clear()
        _stats["installed"] = False
        _stats["socket_guarded"] = False
        _stats["dns_guarded"] = False


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
            "stream_attempts",
            "stream_blocked",
            "sendfile_attempts",
            "sendfile_blocked",
            "dns_attempts",
            "dns_blocked",
            "policy_attempts",
            "policy_allowed",
            "policy_denied",
            "policy_errors",
            "ip_literal_denied",
        ):
            _stats[key] = 0


def reset_guard_state() -> None:
    """**仅测试**：恢复补丁并清除进程级锁定。业务代码不得调用。"""
    global _locked_offline
    uninstall_network_deny()
    reset_stats()
    egress_policy.reset_policy_cache()
    with _lock:
        _locked_offline = False
        _authorized_ips.clear()


def stats() -> dict[str, Any]:
    """只返回计数与布尔开关，**不包含域名、URL、token 或数据库路径**。

    语义（每个计数只回答一件事，不能互相冒充）：
    - `enabled` 是**有效状态**：`locked_offline=true` 时即使环境变量改回 true 也是 false；
    - `attempts` / `blocked`：来源入口（`ensure_external_fetch`）；
    - `network_attempts` / `network_blocked`：只统计 `requests` 层；
    - `os_network_attempts` / `os_network_blocked`：TCP 建连层
      （`create_connection` / `connect` / `connect_ex`，回环也算尝试）；
    - `udp_attempts` / `udp_blocked`：UDP `sendto` / `sendmsg`；
    - `stream_attempts` / `stream_blocked`：已连接 socket 的 `send` / `sendall`
      （含 TLS `send` / `sendall` / `write`，以及 TCP `sendmsg`）；
    - `sendfile_attempts` / `sendfile_blocked`：`socket.socket.sendfile` 与
      `ssl.SSLSocket.sendfile`（TLS 内部回调 `send()` 不重复计数）；
    - `dns_attempts` / `dns_blocked`：`getaddrinfo` / `gethostbyname[_ex]`；
    - `policy_attempts` / `policy_allowed` / `policy_denied` / `policy_errors`：
      允许名单判定；`policy_errors` 只统计策略文件本身不可用（缺文件/坏 JSON/版本不符）；
    - `ip_literal_denied`：直接连未授权 IP literal 的次数；
    - `installed` / `socket_guarded` / `dns_guarded`：wrapper 是否真实安装（在线也应为 true）；
    - `guards`：实际安装成功的路径能力集合（requests / socket / TLS / sendfile / dns）；
    - 这些都不是「全宿主网络调用总数」，OS 级隔离是否生效需另测。
    """
    effective = enabled()
    with _lock:
        snapshot = {
            "enabled": effective,
            "locked_offline": _locked_offline,
            "mode": "offline_locked" if _locked_offline else "online",
            "switch_policy": (
                "online→offline 立即生效并锁定（wrapper 启动即安装）；"
                "offline→online 不支持（需重启进程）"
            ),
            "dns_guard": "APP_LAYER_INTERCEPTED" if _stats["dns_guarded"] else "NOT_INTERCEPTED",
            "guards": dict(_guard_capabilities),
            "authorized_ips": len(_authorized_ips),
            **_stats,
        }
    snapshot["policy"] = egress_policy.policy_status()
    snapshot["os_egress"] = "NOT_VERIFIED"
    return snapshot


def metrics() -> dict[str, Any]:
    """S6-A6：统一指标名与口径。

    规则：
    - 观测点未安装时返回 `"UNKNOWN"`，**不能返回 0**（0 表示「观测过、确实没有」）；
    - 应用层拦截 ≠ 网络包未发出；OS 防火墙拦截也不代表应用层 wrapper 捕获过；
    - `subprocess_network_attempts_total` 与 `os_firewall_denied_total` 本轮没有
      可验证的观测点，固定为 `UNKNOWN`。
    """
    snapshot = stats()
    installed = bool(snapshot.get("installed"))
    dns_guarded = bool(snapshot.get("dns_guarded"))

    def total(*keys: str) -> int | str:
        if not installed:
            return "UNKNOWN"
        return sum(int(snapshot.get(key, 0)) for key in keys)

    return {
        "egress_attempts_total": {
            "value": int(snapshot.get("attempts", 0)),
            "definition": "业务来源入口（ensure_external_fetch）调用次数",
        },
        "egress_allowed_total": {
            "value": int(snapshot.get("attempts", 0)) - int(snapshot.get("blocked", 0)),
            "definition": "来源入口放行次数（不代表网络包已发出）",
        },
        "egress_denied_total": {
            "value": total(
                "blocked",
                "network_blocked",
                "os_network_blocked",
                "udp_blocked",
                "stream_blocked",
                "sendfile_blocked",
                "dns_blocked",
            ),
            "definition": "应用层各观测点拦截次数之和（可能存在同一次调用的多层计数）",
        },
        "egress_policy_errors_total": {
            "value": "UNKNOWN" if not installed else int(snapshot.get("policy_errors", 0)),
            "definition": "策略文件缺失/解析失败/版本不匹配次数",
        },
        "dns_attempts_total": {
            "value": "UNKNOWN" if not dns_guarded else int(snapshot.get("dns_attempts", 0)),
            "definition": "进程内 DNS 入口调用次数",
        },
        "dns_denied_total": {
            "value": "UNKNOWN" if not dns_guarded else int(snapshot.get("dns_blocked", 0)),
            "definition": "进程内 DNS 入口拒绝次数",
        },
        "subprocess_network_attempts_total": {
            "value": "UNKNOWN",
            "definition": "子进程出站无法在应用层观测；需 OS 层审计",
        },
        "os_firewall_denied_total": {
            "value": "UNKNOWN",
            "definition": "本轮无 OS 防火墙审计日志接入",
        },
        "windows": {
            "since_process_start": True,
            "multi_worker_aggregation": "不适用（本服务强制单 worker）",
            "reset_rule": "计数随进程重启归零；不持久化",
        },
    }

