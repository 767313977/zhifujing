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
    assert eg.stats()["locked_offline"] is True
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

    response = asyncio.run(
        external_fetch_blocked_handler(None, eg.ExternalFetchBlocked("测试拦截"))  # type: ignore[arg-type]
    )
    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["offline"] is True
    assert body["reason"] == "EXTERNAL_DATA_FETCH_DISABLED"


def test_retry_does_not_retry_blocked_fetch(monkeypatch):
    from app.sources.base import retry_call

    _offline(monkeypatch)
    with pytest.raises(eg.ExternalFetchBlocked):
        retry_call(lambda: eg.ensure_external_fetch("测试"), retries=3)
    assert eg.stats()["attempts"] == 1
