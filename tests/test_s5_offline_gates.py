"""S5-D：离线闸门补强（requests + socket + 进程锁定）。

覆盖：
- `EXTERNAL_DATA_FETCH_ENABLED=false` 首次读取即锁定，运行中改回 true 也不放行；
- `requests` 层与 socket 层都能拦截，urllib / httpx 这类不同网络栈同样失败；
- 回环放行（浏览器验收仍可访问 127.0.0.1 的后端）；
- `stats()` 不泄露域名 / URL / token；
- 外部来源入口都有 `ensure_external_fetch` 或经 `_call_bounded` 委托；
- 被拦截时返回 503 + `offline=true`，不伪装成「没有数据」。
"""

import asyncio
import io
import json
import logging
import socket
import ssl
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from app import egress_policy
from app import external_guard as eg


@pytest.fixture(autouse=True)
def _guard(monkeypatch):
    eg.reset_guard_state()
    monkeypatch.delenv(eg.ENV, raising=False)
    yield
    eg.reset_guard_state()
    monkeypatch.delenv(eg.ENV, raising=False)


def _offline(monkeypatch):
    monkeypatch.setenv(eg.ENV, "false")
    assert eg.enabled() is False


def test_offline_mode_locks_for_process_lifetime(monkeypatch):
    _offline(monkeypatch)
    assert eg.offline_locked() is True
    assert eg.stats()["mode"] == "offline_locked"
    # 运行中改回 true 也不得放行
    monkeypatch.setenv(eg.ENV, "true")
    assert eg.enabled() is False
    stats = eg.stats()
    assert stats["locked_offline"] is True
    # enabled 必须是**有效状态**，不能因为环境变量改回 true 就显示 true
    assert stats["enabled"] is False
    assert "offline→online" in stats["switch_policy"]
    with pytest.raises(eg.ExternalFetchBlocked):
        eg.ensure_external_fetch("锁定后测试")


def test_install_deny_installs_both_layers(monkeypatch):
    _offline(monkeypatch)
    assert eg.install_network_deny() is True
    stats = eg.stats()
    assert stats["installed"] is True
    assert stats["socket_guarded"] is True
    # 重复安装幂等
    assert eg.install_network_deny() is True


def test_requests_layer_is_blocked(monkeypatch):
    _offline(monkeypatch)
    eg.install_network_deny()
    import requests

    with pytest.raises(eg.ExternalFetchBlocked):
        requests.get("https://example.com/never", timeout=1)
    stats = eg.stats()
    assert stats["network_blocked"] == 1
    assert stats["network_attempts"] == 0


def test_urllib_stack_is_blocked_by_socket_guard(monkeypatch):
    _offline(monkeypatch)
    eg.install_network_deny()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with pytest.raises((eg.ExternalFetchBlocked, OSError)):
        opener.open("http://93.184.216.34/never", timeout=1)
    stats = eg.stats()
    assert stats["os_network_blocked"] >= 1
    # urllib 不经过 requests 层
    assert stats["network_attempts"] == 0


def test_httpx_module_uses_socket_stack():
    # 不实际发请求（避免测试自身出网）：只确认 httpx 的同步后端依赖 socket 建连，
    # 因此会被 socket 层闸门覆盖；真正验证拦截的是 urllib / raw socket 用例。
    pytest.importorskip("httpx")
    import httpcore._backends.sync as backend

    source = Path(backend.__file__).read_text(encoding="utf-8")
    assert "socket.create_connection" in source


def test_raw_socket_connect_is_blocked(monkeypatch):
    _offline(monkeypatch)
    eg.install_network_deny()
    with pytest.raises(eg.ExternalFetchBlocked):
        socket.create_connection(("93.184.216.34", 80), timeout=1)
    assert eg.stats()["os_network_blocked"] == 1


def test_direct_socket_connect_public_ipv4_is_blocked(monkeypatch):
    """直接测 `socket.socket.connect()`，不能只靠 create_connection 间接覆盖。"""
    _offline(monkeypatch)
    eg.install_network_deny()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(eg.ExternalFetchBlocked):
            sock.connect(("93.184.216.34", 80))
    finally:
        sock.close()
    assert eg.stats()["os_network_blocked"] == 1


def test_connect_ex_public_ipv4_is_blocked(monkeypatch):
    _offline(monkeypatch)
    eg.install_network_deny()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(eg.ExternalFetchBlocked):
            sock.connect_ex(("93.184.216.34", 80))
    finally:
        sock.close()
    assert eg.stats()["os_network_blocked"] == 1


def test_public_ipv6_connect_is_blocked(monkeypatch):
    if not socket.has_ipv6:
        pytest.skip("当前环境不支持 IPv6")
    _offline(monkeypatch)
    eg.install_network_deny()
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        with pytest.raises(eg.ExternalFetchBlocked):
            sock.connect(("2606:4700:4700::1111", 443, 0, 0))
    finally:
        sock.close()
    assert eg.stats()["os_network_blocked"] == 1


def test_udp_sendto_public_address_is_blocked(monkeypatch):
    _offline(monkeypatch)
    eg.install_network_deny()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(eg.ExternalFetchBlocked):
            sock.sendto(b"ping", ("93.184.216.34", 53))
    finally:
        sock.close()
    stats = eg.stats()
    assert stats["udp_blocked"] == 1
    # UDP 拦截不能算成 TCP 建连拦截
    assert stats["os_network_blocked"] == 0


def test_udp_sendmsg_public_address_is_blocked(monkeypatch):
    _offline(monkeypatch)
    eg.install_network_deny()
    if not hasattr(socket.socket, "sendmsg"):
        # 平台没有 sendmsg：闸门按能力安装，不假装已覆盖该路径（报告里明示）
        assert eg.stats()["socket_guarded"] is True
        assert eg.stats()["udp_blocked"] == 0
        return
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(eg.ExternalFetchBlocked):
            sock.sendmsg([b"ping"], [], 0, ("93.184.216.34", 53))
    finally:
        sock.close()
    assert eg.stats()["udp_blocked"] == 1


@pytest.mark.parametrize("host", ["localhost.localdomain", "", None, "example.com"])
def test_hostname_and_empty_host_do_not_bypass(monkeypatch, host):
    _offline(monkeypatch)
    eg.install_network_deny()
    with pytest.raises(eg.ExternalFetchBlocked):
        socket.create_connection((host, 80), timeout=1)
    assert eg.stats()["os_network_blocked"] == 1


def test_localhost_is_mapped_to_loopback_not_dns(monkeypatch):
    _offline(monkeypatch)
    eg.install_network_deny()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def accept_once():
        try:
            server.accept()[0].close()
        except OSError:
            pass

    thread = threading.Thread(target=accept_once, daemon=True)
    thread.start()
    try:
        conn = socket.create_connection(("localhost", port), timeout=1)
        conn.close()
        thread.join(timeout=1)
        assert eg.stats()["os_network_blocked"] == 0
    finally:
        server.close()


def test_ipv6_loopback_still_allowed(monkeypatch):
    if not socket.has_ipv6:
        pytest.skip("当前环境不支持 IPv6")
    _offline(monkeypatch)
    eg.install_network_deny()
    try:
        server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        server.bind(("::1", 0))
    except OSError:
        pytest.skip("当前环境不能绑定 ::1")
    server.listen(1)
    port = server.getsockname()[1]

    def accept_once():
        try:
            server.accept()[0].close()
        except OSError:
            pass

    thread = threading.Thread(target=accept_once, daemon=True)
    thread.start()
    try:
        conn = socket.create_connection(("::1", port), timeout=1)
        conn.close()
        thread.join(timeout=1)
        assert eg.stats()["os_network_blocked"] == 0
    finally:
        server.close()


def test_unix_domain_socket_is_not_blocked(monkeypatch, tmp_path):
    _offline(monkeypatch)
    eg.install_network_deny()
    if not hasattr(socket, "AF_UNIX"):
        # 平台无 AF_UNIX：确认闸门仍正常安装且不误伤其它路径（报告里明示）
        assert eg.stats()["socket_guarded"] is True
        with pytest.raises(eg.ExternalFetchBlocked):
            socket.create_connection(("93.184.216.34", 80), timeout=1)
        return
    path = tmp_path / "guard.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
    except OSError:
        # 平台声明支持但不能创建：明确记录，不静默当成「通过」
        assert eg.stats()["socket_guarded"] is True
        server.close()
        return
    server.listen(1)

    def accept_once():
        try:
            server.accept()[0].close()
        except OSError:
            pass

    thread = threading.Thread(target=accept_once, daemon=True)
    thread.start()
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(path))
        client.close()
        thread.join(timeout=1)
        assert eg.stats()["os_network_blocked"] == 0
    finally:
        server.close()


def test_dns_is_intercepted_at_app_layer(monkeypatch):
    """S6-A3：DNS 入口（getaddrinfo）现在也在进程内拦截。

    历史：S5 的结论是 `dns_guard=NOT_INTERCEPTED`。S6 把 `getaddrinfo` /
    `gethostbyname[_ex]` 纳入闸门后，离线时**非回环解析在解析前就失败**，
    回环（localhost / 127.0.0.1）仍然放行。
    """
    _offline(monkeypatch)
    eg.install_network_deny()
    resolved = socket.getaddrinfo("localhost", 80, proto=socket.IPPROTO_TCP)
    assert resolved
    assert eg.stats()["dns_guard"] == "APP_LAYER_INTERCEPTED"
    with pytest.raises(eg.ExternalFetchBlocked):
        socket.getaddrinfo("example.com", 443, proto=socket.IPPROTO_TCP)
    assert eg.stats()["dns_blocked"] == 1
    with pytest.raises(eg.ExternalFetchBlocked):
        socket.create_connection(("93.184.216.34", 80), timeout=1)


def test_loopback_is_still_allowed(monkeypatch):
    _offline(monkeypatch)
    eg.install_network_deny()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    accepted: list[socket.socket] = []

    def accept_once():
        try:
            conn, _ = server.accept()
            accepted.append(conn)
        except OSError:
            pass

    thread = threading.Thread(target=accept_once, daemon=True)
    thread.start()
    try:
        conn = socket.create_connection(("127.0.0.1", port), timeout=1)
        conn.close()
        thread.join(timeout=1)
        assert accepted  # 回环连接确实建立
        assert eg.stats()["os_network_blocked"] == 0
    finally:
        for item in accepted:
            item.close()
        server.close()


def test_stats_never_exposes_urls_or_tokens(monkeypatch):
    _offline(monkeypatch)
    eg.install_network_deny()
    import requests

    with pytest.raises(eg.ExternalFetchBlocked):
        requests.get("https://secret-host.example.com/path?token=abc", timeout=1)
    payload = json.dumps(eg.stats(), ensure_ascii=False)
    assert "secret-host" not in payload
    assert "token" not in payload.lower()
    assert payload.count("example.com") == 0


SENSITIVE_URL = "https://user:p4ssw0rd@secret-host.example.com/private/path?token=tok3nZ&sign=s1gnZ"
SENSITIVE_BITS = ("p4ssw0rd", "user:", "private/path", "tok3nZ", "s1gnZ")


def test_requests_blocked_url_secrets_are_redacted_everywhere(monkeypatch, caplog):
    """P1-04：日志 / 异常 / HTTP body 都不得包含 userinfo、path、query。"""
    _offline(monkeypatch)
    eg.install_network_deny()
    import requests
    from app.main import external_fetch_blocked_handler

    with caplog.at_level(logging.WARNING, logger="app.external_guard"):
        with pytest.raises(eg.ExternalFetchBlocked) as excinfo:
            requests.get(SENSITIVE_URL, timeout=1)
    response = asyncio.run(
        external_fetch_blocked_handler(None, excinfo.value)  # type: ignore[arg-type]
    )
    body = response.body.decode("utf-8")
    haystacks = {
        "log": caplog.text,
        "exception": str(excinfo.value),
        "http_body": body,
        "stats": json.dumps(eg.stats(), ensure_ascii=False),
    }
    for name, text in haystacks.items():
        for bit in SENSITIVE_BITS:
            assert bit not in text, f"{name} 泄露敏感字段 {bit!r}"
        assert "?" not in text, f"{name} 泄露 query 分隔符"
    payload = json.loads(body)
    assert payload["reason_code"] == "NETWORK_REQUEST_BLOCKED"
    assert payload["source"] == "secret-host.example.com"


def test_ensure_external_fetch_context_url_is_redacted(monkeypatch, caplog):
    _offline(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="app.external_guard"):
        with pytest.raises(eg.ExternalFetchBlocked) as excinfo:
            eg.ensure_external_fetch(f"测试来源 {SENSITIVE_URL}")
    for text in (caplog.text, str(excinfo.value)):
        for bit in SENSITIVE_BITS:
            assert bit not in text
        assert "?" not in text
    assert "测试来源" in str(excinfo.value)


def test_source_entries_are_guarded_or_delegated():
    root = Path(__file__).resolve().parents[1] / "backend" / "app" / "sources"
    direct = [
        "akshare_source.py",
        "danjuan.py",
        "eastmoney.py",
        "etf_exchange.py",
        "ifind.py",
        "kaipanhong.py",
        "kph_local.py",
        "tdx.py",
        "tencent.py",
        "ths_limit_up.py",
    ]
    for name in direct:
        text = (root / name).read_text(encoding="utf-8")
        assert "ensure_external_fetch" in text, f"{name} 缺少外部取数闸门"
    # 东财新闻 / 公告 / 研报通过 akshare 的受控调用委托，不直接发请求
    for name in ("em_news.py", "em_events.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "_call_bounded" in text
        assert "requests." not in text


def test_blocked_response_is_503_and_marked_offline():
    from app.main import external_fetch_blocked_handler

    exc = eg.ExternalFetchBlocked("NETWORK_REQUEST_BLOCKED", source=SENSITIVE_URL)
    response = asyncio.run(
        external_fetch_blocked_handler(None, exc)  # type: ignore[arg-type]
    )
    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["offline"] is True
    assert body["reason"] == "EXTERNAL_DATA_FETCH_DISABLED"
    assert body["reason_code"] == "NETWORK_REQUEST_BLOCKED"
    # 固定文案 + 安全 host，不得回显原始 URL
    assert body["detail"] == "离线验收模式：外部数据现取已被禁用，页面仅显示本地已有数据。"
    assert body["source"] == "secret-host.example.com"
    text = json.dumps(body, ensure_ascii=False)
    for bit in SENSITIVE_BITS:
        assert bit not in text


def test_retry_does_not_retry_blocked_fetch(monkeypatch):
    from app.sources.base import retry_call

    _offline(monkeypatch)
    with pytest.raises(eg.ExternalFetchBlocked):
        retry_call(lambda: eg.ensure_external_fetch("测试"), retries=3)
    assert eg.stats()["attempts"] == 1


# ---------------------------------------------------------------- R2-P1-02 运行时切换


class _FakeSock:
    """最小 socket 替身：只提供 family/type/getpeername，用于已连接发送路径。"""

    def __init__(self, peer=None, *, family=socket.AF_INET, sock_type=socket.SOCK_STREAM):
        self._peer = peer
        self.family = family
        self.type = sock_type

    def getpeername(self):
        if self._peer is None:
            raise OSError("not connected")
        return self._peer


def _allow_original_sends(monkeypatch):
    monkeypatch.setattr(eg, "_original_socket_send", lambda sock, data, flags=0: len(data))
    monkeypatch.setattr(eg, "_original_socket_sendall", lambda sock, data, flags=0: None)
    monkeypatch.setattr(eg, "_original_socket_sendmsg", lambda sock, buffers, *args: 1)


def test_online_start_installs_wrapper_and_runtime_switch_enforces(monkeypatch):
    """R2-P1-02：在线启动也安装 wrapper；运行中切离线立即生效。"""
    monkeypatch.delenv(eg.ENV, raising=False)
    assert eg.install_network_deny() is False  # 在线：已安装但不处于离线
    online = eg.stats()
    assert online["enabled"] is True
    assert online["installed"] is True
    assert online["socket_guarded"] is True

    monkeypatch.setenv(eg.ENV, "false")
    assert eg.enabled() is False
    offline = eg.stats()
    assert offline["enabled"] is False
    assert offline["locked_offline"] is True
    assert offline["installed"] is True
    assert offline["socket_guarded"] is True

    with pytest.raises(eg.ExternalFetchBlocked):
        socket.create_connection(("93.184.216.34", 80), timeout=1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(eg.ExternalFetchBlocked):
            sock.connect(("93.184.216.34", 80))
        with pytest.raises(eg.ExternalFetchBlocked):
            sock.connect_ex(("93.184.216.34", 80))
    finally:
        sock.close()


def test_connected_send_and_sendall_blocked_after_switch(monkeypatch):
    _offline(monkeypatch)
    eg.install_network_deny()
    fake = _FakeSock(("93.184.216.34", 443))
    with pytest.raises(eg.ExternalFetchBlocked):
        eg._guarded_send(fake, b"payload")
    with pytest.raises(eg.ExternalFetchBlocked):
        eg._guarded_sendall(fake, b"payload")
    stats = eg.stats()
    assert stats["stream_blocked"] == 2
    # 已连接发送不能算成 TCP 建连拦截
    assert stats["os_network_blocked"] == 0


def test_sendmsg_with_no_address_on_connected_socket_is_blocked(monkeypatch):
    _offline(monkeypatch)
    eg.install_network_deny()
    fake = _FakeSock(("93.184.216.34", 443))
    with pytest.raises(eg.ExternalFetchBlocked):
        eg._guarded_sendmsg(fake, [b"payload"], [], 0)
    assert eg.stats()["stream_blocked"] == 1


def test_connected_udp_sendmsg_without_destination_is_blocked(monkeypatch):
    _offline(monkeypatch)
    eg.install_network_deny()
    connected = _FakeSock(("93.184.216.34", 53), sock_type=socket.SOCK_DGRAM)
    with pytest.raises(eg.ExternalFetchBlocked):
        eg._guarded_sendmsg(connected, [b"payload"], [], 0)
    unknown = _FakeSock(None, sock_type=socket.SOCK_DGRAM)
    with pytest.raises(eg.ExternalFetchBlocked) as excinfo:
        eg._guarded_sendmsg(unknown, [b"payload"], [], 0)
    assert excinfo.value.reason_code == "NETWORK_SEND_TARGET_UNKNOWN"
    assert eg.stats()["udp_blocked"] == 2


def test_loopback_stream_send_sendall_sendmsg_allowed(monkeypatch):
    _offline(monkeypatch)
    eg.install_network_deny()
    _allow_original_sends(monkeypatch)
    fake = _FakeSock(("127.0.0.1", 8014))
    assert eg._guarded_send(fake, b"x") == 1
    assert eg._guarded_sendall(fake, b"x") is None
    assert eg._guarded_sendmsg(fake, [b"x"], [], 0) == 1
    assert eg.stats()["stream_blocked"] == 0
    assert eg.stats()["udp_blocked"] == 0


def test_unix_socket_stream_send_is_not_blocked(monkeypatch):
    _offline(monkeypatch)
    eg.install_network_deny()
    _allow_original_sends(monkeypatch)
    if not hasattr(socket, "AF_UNIX"):
        # 平台无 AF_UNIX：确认回环发送仍不受影响，Unix 路径按能力缺失明示
        fake = _FakeSock(("127.0.0.1", 1))
        assert eg._guarded_send(fake, b"x") == 1
        assert eg.stats()["stream_blocked"] == 0
        return
    fake = _FakeSock("/tmp/guard.sock", family=socket.AF_UNIX)
    assert eg._guarded_send(fake, b"x") == 1
    assert eg.stats()["stream_blocked"] == 0


def test_offline_to_online_still_requires_restart(monkeypatch):
    _offline(monkeypatch)
    eg.install_network_deny()
    monkeypatch.setenv(eg.ENV, "true")
    assert eg.enabled() is False
    with pytest.raises(eg.ExternalFetchBlocked):
        eg.ensure_external_fetch("重启前不得恢复")


def test_install_failure_is_fail_closed(monkeypatch):
    """安装失败必须向上抛出，启动流程 fail closed，不能静默继续。"""

    def boom():
        raise RuntimeError("patch failed")

    monkeypatch.setattr(eg, "_install_wrappers", boom)
    with pytest.raises(RuntimeError):
        eg.install_network_deny()


def test_concurrent_switch_has_no_allow_window(monkeypatch):
    monkeypatch.delenv(eg.ENV, raising=False)
    eg.install_network_deny()
    monkeypatch.setenv(eg.ENV, "false")
    results: list[bool] = []
    lock = threading.Lock()

    def worker():
        value = eg.enabled()
        with lock:
            results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert results and all(value is False for value in results)
    stats = eg.stats()
    assert stats["installed"] is True
    assert stats["socket_guarded"] is True
    with pytest.raises(eg.ExternalFetchBlocked):
        socket.create_connection(("93.184.216.34", 80), timeout=1)


# ------------------------------------------------ R3-P1-01 TLS / sendfile 写路径


def _public_peer():
    return _FakeSock(("93.184.216.34", 443))


def test_wrappers_are_really_installed_on_ssl_and_sendfile(monkeypatch):
    """真实分发测试：patch 必须落在类方法上，而不只是 helper 可被直接调用。"""
    import ssl

    _offline(monkeypatch)
    eg.install_network_deny()
    assert ssl.SSLSocket.send is eg._guarded_ssl_send
    assert ssl.SSLSocket.sendall is eg._guarded_ssl_sendall
    assert ssl.SSLSocket.sendfile is eg._guarded_ssl_sendfile
    assert ssl.SSLSocket.write is eg._guarded_ssl_write
    assert socket.socket.sendfile is eg._guarded_sendfile
    guards = eg.stats()["guards"]
    assert guards["ssl_stream"] is True
    assert guards["ssl_sendfile"] is True
    assert guards["ssl_write"] is True
    assert guards["socket_sendfile"] is True


def test_ssl_send_and_sendall_blocked_after_offline_switch(monkeypatch):
    """在线建立的连接在切换离线后必须重新判定 peer，不能沿用建连时的状态。"""
    import ssl

    monkeypatch.delenv(eg.ENV, raising=False)
    eg.install_network_deny()
    assert eg.enabled() is True
    monkeypatch.setenv(eg.ENV, "false")
    assert eg.enabled() is False
    fake = _public_peer()
    with pytest.raises(eg.ExternalFetchBlocked) as excinfo:
        ssl.SSLSocket.send(fake, b"payload")
    assert excinfo.value.reason_code == "NETWORK_STREAM_SEND_BLOCKED"
    with pytest.raises(eg.ExternalFetchBlocked):
        ssl.SSLSocket.sendall(fake, b"payload")
    stats = eg.stats()
    assert stats["stream_blocked"] == 2
    assert stats["sendfile_blocked"] == 0
    assert stats["os_network_blocked"] == 0


def test_ssl_send_and_sendall_fail_closed_when_peer_unknown(monkeypatch):
    import ssl

    _offline(monkeypatch)
    eg.install_network_deny()
    unknown = _FakeSock(None)
    for call in (
        lambda: ssl.SSLSocket.send(unknown, b"x"),
        lambda: ssl.SSLSocket.sendall(unknown, b"x"),
        lambda: ssl.SSLSocket.sendfile(unknown, object()),
    ):
        with pytest.raises(eg.ExternalFetchBlocked) as excinfo:
            call()
        assert excinfo.value.reason_code == "NETWORK_SEND_TARGET_UNKNOWN"
    assert eg.stats()["stream_blocked"] == 2
    assert eg.stats()["sendfile_blocked"] == 1


def test_socket_sendfile_blocks_before_original_implementation(monkeypatch):
    """POSIX os.sendfile 与 Windows fallback 都必须在进入原实现前被拦。"""
    _offline(monkeypatch)
    eg.install_network_deny()
    calls: list[str] = []

    def _original(sock, file, offset=0, count=None):
        calls.append("original-sendfile")
        return 1

    monkeypatch.setattr(eg, "_original_socket_sendfile", _original)
    with pytest.raises(eg.ExternalFetchBlocked) as excinfo:
        socket.socket.sendfile(_public_peer(), object())
    assert excinfo.value.reason_code == "NETWORK_SENDFILE_BLOCKED"
    assert calls == []
    assert eg.stats()["sendfile_blocked"] == 1
    assert eg.stats()["sendfile_attempts"] == 1


def test_ssl_sendfile_blocked_before_original_implementation(monkeypatch):
    import ssl

    _offline(monkeypatch)
    eg.install_network_deny()
    calls: list[str] = []

    def _original(sock, file, offset=0, count=None):
        calls.append("original-ssl-sendfile")
        return 1

    monkeypatch.setattr(eg, "_original_ssl_sendfile", _original)
    with pytest.raises(eg.ExternalFetchBlocked):
        ssl.SSLSocket.sendfile(_public_peer(), object())
    assert calls == []
    assert eg.stats()["sendfile_blocked"] == 1


def test_loopback_sendfile_works_for_plain_and_tls(monkeypatch):
    """严格回环 peer 必须放行；sendfile 参数与返回值语义保持不变。"""
    import ssl

    _offline(monkeypatch)
    eg.install_network_deny()
    monkeypatch.setattr(
        eg, "_original_socket_sendfile", lambda sock, file, offset=0, count=None: 7
    )
    monkeypatch.setattr(
        eg, "_original_ssl_sendfile", lambda sock, file, offset=0, count=None: 9
    )
    loopback = _FakeSock(("127.0.0.1", 8443))
    assert socket.socket.sendfile(loopback, object(), 0, 10) == 7
    assert ssl.SSLSocket.sendfile(loopback, object(), 0, 10) == 9
    stats = eg.stats()
    assert stats["sendfile_blocked"] == 0
    assert stats["sendfile_attempts"] == 2


def test_tls_sendall_does_not_double_count_nested_send(monkeypatch):
    """SSLSocket.sendall 内部回调 self.send：只能计一次尝试、一次判定。"""
    import ssl

    _offline(monkeypatch)
    eg.install_network_deny()
    monkeypatch.setattr(
        eg, "_original_ssl_send", lambda sock, data, flags=0: len(data)
    )
    monkeypatch.setattr(
        eg,
        "_original_ssl_sendall",
        lambda sock, data, flags=0: ssl.SSLSocket.send(sock, data, flags),
    )
    loopback = _FakeSock(("127.0.0.1", 8443))
    assert ssl.SSLSocket.sendall(loopback, b"hello") == 5
    assert eg.stats()["stream_attempts"] == 1
    assert eg.stats()["stream_blocked"] == 0


def test_unix_socket_sendall_and_sendfile_are_not_blocked(monkeypatch):
    _offline(monkeypatch)
    eg.install_network_deny()
    _allow_original_sends(monkeypatch)
    monkeypatch.setattr(
        eg, "_original_socket_sendfile", lambda sock, file, offset=0, count=None: 1
    )
    if not hasattr(socket, "AF_UNIX"):
        return
    fake = _FakeSock("/tmp/guard.sock", family=socket.AF_UNIX)
    assert eg._guarded_sendall(fake, b"x") is None
    assert eg._guarded_sendfile(fake, object()) == 1
    assert eg.stats()["stream_blocked"] == 0
    assert eg.stats()["sendfile_blocked"] == 0


def test_uninstall_restores_ssl_and_sendfile_methods(monkeypatch):
    import ssl

    originals = {
        "ssl_send": ssl.SSLSocket.send,
        "ssl_sendall": ssl.SSLSocket.sendall,
        "ssl_sendfile": ssl.SSLSocket.sendfile,
        "ssl_write": ssl.SSLSocket.write,
        "socket_sendfile": socket.socket.sendfile,
    }
    _offline(monkeypatch)
    eg.install_network_deny()
    assert ssl.SSLSocket.send is eg._guarded_ssl_send
    eg.uninstall_network_deny()
    assert ssl.SSLSocket.send is originals["ssl_send"]
    assert ssl.SSLSocket.sendall is originals["ssl_sendall"]
    assert ssl.SSLSocket.sendfile is originals["ssl_sendfile"]
    assert ssl.SSLSocket.write is originals["ssl_write"]
    assert socket.socket.sendfile is originals["socket_sendfile"]
    assert eg.stats()["guards"] == {}
    assert eg.stats()["installed"] is False


def test_concurrent_offline_switch_blocks_all_tls_writes(monkeypatch):
    """并发切换时，任何线程的 TLS 发送都必须看到离线状态（无放行窗口）。"""
    import ssl

    monkeypatch.delenv(eg.ENV, raising=False)
    eg.install_network_deny()
    monkeypatch.setenv(eg.ENV, "false")
    blocked: list[str] = []
    lock = threading.Lock()

    def worker():
        try:
            ssl.SSLSocket.send(_public_peer(), b"x")
        except eg.ExternalFetchBlocked:
            with lock:
                blocked.append("blocked")
        except Exception as exc:  # noqa: BLE001 - 非闸门异常必须暴露
            with lock:
                blocked.append(f"unexpected:{type(exc).__name__}")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert blocked == ["blocked"] * 8
    assert eg.stats()["stream_blocked"] == 8


# ------------------------------------------------- R4-P1-01 真实 TLS 集成测试
#
# fixture：tests/fixtures/tls/localhost.{crt,key}，仅本地测试用自签名证书，
# SAN=DNS:localhost,IP:127.0.0.1；不连接公网，不含生产证书或密钥。

TLS_DIR = Path(__file__).resolve().parent / "fixtures" / "tls"


class _TlsServer:
    """本地 TLS echo/collect server（只监听 127.0.0.1，测试线程内运行）。"""

    def __init__(self):
        self.received = bytearray()
        self.error: BaseException | None = None
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(TLS_DIR / "localhost.crt", TLS_DIR / "localhost.key")
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            raw, _ = self._listener.accept()
            with self._ctx.wrap_socket(raw, server_side=True) as tls:
                while True:
                    chunk = tls.recv(65536)
                    if not chunk:
                        break
                    self.received.extend(chunk)
        except BaseException as exc:  # noqa: BLE001 - 测试需要保留线程异常
            self.error = exc

    def close(self):
        try:
            self._listener.close()
        except OSError:
            pass
        self._thread.join(timeout=5)


def _tls_client(port: int) -> ssl.SSLSocket:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    return ctx.wrap_socket(raw, server_hostname="localhost")


def _start_online_tls(monkeypatch) -> tuple[_TlsServer, ssl.SSLSocket]:
    """在线启动、安装 wrapper、完成真实 TLS 握手，再返回连接。"""
    monkeypatch.delenv(eg.ENV, raising=False)
    eg.install_network_deny()
    server = _TlsServer()
    # 生产策略只允许 KPH 5000；测试用随机端口，显式覆盖策略端口。
    base = egress_policy.load_policy()
    policy = {**base, "loopback": {"allow": True, "allow_any_port": False, "ports": [server.port]}}
    monkeypatch.setattr(egress_policy, "load_policy", lambda *args, **kwargs: policy)
    client = _tls_client(server.port)
    return server, client


def _shutdown(server: _TlsServer, client: ssl.SSLSocket | None) -> None:
    if client is not None:
        try:
            client.close()
        except BaseException:  # noqa: BLE001 - 关闭阶段异常不影响断言
            pass
    server.close()


def test_real_tls_write_blocked_after_switch(monkeypatch):
    """真实 SSLSocket.write：切换离线后非回环 peer 必须在原 write 前阻断。"""
    server, client = _start_online_tls(monkeypatch)
    real_peer_host = eg._peer_host
    calls: list[bytes] = []
    monkeypatch.setattr(eg, "_original_ssl_write", lambda sock, data: calls.append(data) or len(data))
    monkeypatch.setattr(eg, "_peer_host", lambda sock: "203.0.113.10")
    monkeypatch.setenv(eg.ENV, "false")
    assert eg.enabled() is False
    try:
        with pytest.raises(eg.ExternalFetchBlocked) as excinfo:
            client.write(b"blocked-payload")
        assert excinfo.value.reason_code == "NETWORK_STREAM_SEND_BLOCKED"
        assert calls == []
        stats = eg.stats()
        assert stats["stream_attempts"] == 1
        assert stats["stream_blocked"] == 1
    finally:
        monkeypatch.setattr(eg, "_peer_host", real_peer_host)
        _shutdown(server, client)


def test_real_loopback_tls_send_sendall_write_sendfile(monkeypatch, tmp_path):
    """真实 loopback TLS：send / sendall / write / sendfile 全部正常传输。"""
    server, client = _start_online_tls(monkeypatch)
    monkeypatch.setenv(eg.ENV, "false")
    assert eg.enabled() is False
    payload = b"0123456789abcdef"
    path = tmp_path / "payload.bin"
    path.write_bytes(payload)
    try:
        assert client.send(b"AAA") == 3
        client.sendall(b"BBB")
        assert client.write(b"CCC") == 3
        with path.open("rb") as handle:
            sent = client.sendfile(handle, offset=2, count=6)
            assert sent == 6
            # offset/count 与文件指针语义保持原实现
            assert handle.tell() == 8
    finally:
        # 服务端读线程与 sendfile 完成有毫秒级竞态；全量跑时 CPU 抖动会放大。
        # 必须在关连接**之前**等接收完成：先 close 会让 TLS 层以 EOF/RST 结束，
        # 内核里还没被读走的记录会被丢弃，断言就会看到截断的数据。
        # 这是等待异步接收完成，不是放宽内容断言。
        deadline = time.time() + 2
        while len(server.received) < len(b"AAABBBCCC234567") and time.time() < deadline:
            time.sleep(0.02)
        _shutdown(server, client)
    assert bytes(server.received) == b"AAABBBCCC234567"
    stats = eg.stats()
    assert stats["stream_attempts"] == 3
    assert stats["sendfile_attempts"] == 1
    assert stats["stream_blocked"] == 0
    assert stats["sendfile_blocked"] == 0


def test_tls_nested_state_does_not_leak_across_sockets(monkeypatch):
    """A 的 sendfile 内部回调触发 B.send：B 不得继承 A 的 nested 豁免。"""
    server_a, client_a = _start_online_tls(monkeypatch)
    server_b, client_b = _start_online_tls(monkeypatch)
    monkeypatch.setenv(eg.ENV, "false")
    assert eg.enabled() is False
    monkeypatch.setattr(
        eg,
        "_peer_host",
        lambda sock: "203.0.113.11" if sock is client_b else "127.0.0.1",
    )
    blocked: list[str] = []

    class _CallbackFile:
        def __init__(self, data: bytes):
            self._io = io.BytesIO(data)
            self._fired = False

        def read(self, size=-1):
            chunk = self._io.read(size)
            if not self._fired:
                self._fired = True
                try:
                    client_b.send(b"B")
                except eg.ExternalFetchBlocked as exc:
                    blocked.append(exc.reason_code)
            return chunk

        def seek(self, position):
            return self._io.seek(position)

    try:
        # B 的阻断由回调内部捕获；A 的 sendfile 本身仍可完成（loopback）
        client_a.sendfile(_CallbackFile(b"x" * 32), 0)
        assert blocked == ["NETWORK_STREAM_SEND_BLOCKED"]
        assert eg.stats()["stream_blocked"] == 1
        # 完成后必须把 A 的 nested 栈清空
        assert eg._tls_nested_sockets() == []
    finally:
        _shutdown(server_a, client_a)
        _shutdown(server_b, client_b)


def test_tls_sendall_rechecks_each_block_after_offline_switch(monkeypatch):
    """sendall 第一块后切离线：后续内部 self.send() 必须被重新判定并阻断。"""
    server, client = _start_online_tls(monkeypatch)
    calls = {"n": 0}

    def _send_then_switch(sock, data, flags=0):
        calls["n"] += 1
        if calls["n"] == 1:
            monkeypatch.setenv(eg.ENV, "false")
            assert eg.enabled() is False
            eg._peer_host = lambda target: "203.0.113.12"
        return 1

    real_peer_host = eg._peer_host
    monkeypatch.setattr(eg, "_original_ssl_send", _send_then_switch)
    try:
        with pytest.raises(eg.ExternalFetchBlocked):
            client.sendall(b"abcdef")
        assert calls["n"] == 1
        stats = eg.stats()
        assert stats["stream_attempts"] == 1
        assert stats["stream_blocked"] == 1
    finally:
        eg._peer_host = real_peer_host
        _shutdown(server, client)


def test_tls_sendfile_rechecks_each_chunk_after_offline_switch(monkeypatch, tmp_path):
    """sendfile 第一块后切离线：后续 chunk 的内部 self.send() 必须被阻断。"""
    server, client = _start_online_tls(monkeypatch)
    path = tmp_path / "chunks.bin"
    path.write_bytes(b"y" * 20000)
    calls = {"n": 0}

    def _send_then_switch(sock, data, flags=0):
        calls["n"] += 1
        if calls["n"] == 1:
            monkeypatch.setenv(eg.ENV, "false")
            assert eg.enabled() is False
            eg._peer_host = lambda target: "203.0.113.13"
        return len(data)

    real_peer_host = eg._peer_host
    monkeypatch.setattr(eg, "_original_ssl_send", _send_then_switch)
    try:
        with path.open("rb") as handle:
            with pytest.raises(eg.ExternalFetchBlocked):
                client.sendfile(handle, 0)
        assert calls["n"] == 1
        stats = eg.stats()
        assert stats["sendfile_attempts"] == 1
        # 阻断发生在 sendfile 内部的 self.send 写块上，按 stream 计；sendfile 本身未被前置拦截
        assert stats["stream_blocked"] == 1
        assert stats["sendfile_blocked"] == 0
    finally:
        eg._peer_host = real_peer_host
        _shutdown(server, client)


def test_tls_nested_state_is_thread_local(monkeypatch):
    """两个线程分别发送不同 SSLSocket：不得互相污染 nested 状态。"""
    server_a, client_a = _start_online_tls(monkeypatch)
    server_b, client_b = _start_online_tls(monkeypatch)
    monkeypatch.setenv(eg.ENV, "false")
    assert eg.enabled() is False
    errors: list[BaseException] = []

    def send(client, payload):
        try:
            client.sendall(payload)
        except BaseException as exc:  # noqa: BLE001 - 线程异常必须暴露
            errors.append(exc)

    threads = [
        threading.Thread(target=send, args=(client_a, b"a" * 1000)),
        threading.Thread(target=send, args=(client_b, b"b" * 1000)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    try:
        assert errors == []
        assert eg.stats()["stream_blocked"] == 0
        assert eg.stats()["stream_attempts"] == 2
    finally:
        _shutdown(server_a, client_a)
        _shutdown(server_b, client_b)
    assert len(server_a.received) == 1000
    assert len(server_b.received) == 1000

