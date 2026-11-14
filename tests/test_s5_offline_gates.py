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
import json
import logging
import socket
import threading
import urllib.request
from pathlib import Path

import pytest

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


def test_dns_is_not_intercepted_but_connect_is(monkeypatch):
    """DNS 边界显式化：getaddrinfo 不拦（无法可靠在进程内约束），建连才拦。"""
    _offline(monkeypatch)
    eg.install_network_deny()
    resolved = socket.getaddrinfo("localhost", 80, proto=socket.IPPROTO_TCP)
    assert resolved
    assert eg.stats()["dns_guard"] == "NOT_INTERCEPTED"
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

