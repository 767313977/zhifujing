"""S6-A5：出站安全测试矩阵（应用层）。

范围声明（**不要夸大**）：
- 这里验证的是**本进程内**的 requests / socket / DNS / TLS wrapper 与统一策略；
- 子进程、原生库、OS 防火墙、网络命名空间**没有**在这里验证，见
  `reports/s6/security/egress-test-report.md` 的 NOT_VERIFIED 项与
  `os-policy-verification.md`；
- 需要「公网对端」的用例无法在无公网连接时构造真实 peer，因此只注入对端地址
  （`_peer_host`），socket 本身仍是真实对象 —— 这属于应用层单元测试，
  不作为 OS 级证据。
"""

import http.server
import importlib.util
import json
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import requests

from app import egress_policy
from app import external_guard as eg
from app import readiness

POLICY_FILE = Path(__file__).resolve().parents[1] / "deploy" / "s6" / "egress-policy.json"


@pytest.fixture(autouse=True)
def _guard(monkeypatch):
    eg.reset_guard_state()
    egress_policy.reset_policy_cache()
    monkeypatch.delenv(eg.ENV, raising=False)
    monkeypatch.setenv("FUPAN_ENV", "development")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    yield
    eg.reset_guard_state()
    egress_policy.reset_policy_cache()
    monkeypatch.delenv(eg.ENV, raising=False)


def _online(monkeypatch):
    monkeypatch.setenv(eg.ENV, "true")
    assert eg.enabled() is True


def _offline(monkeypatch):
    monkeypatch.setenv(eg.ENV, "false")
    assert eg.enabled() is False


class _LocalHTTPServer:
    """本地 HTTP 服务（只监听 127.0.0.1），可返回 302 到外部域名。"""

    def __init__(self, status: int = 200, location: str | None = None):
        handler = self._make_handler(status, location)
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @staticmethod
    def _make_handler(status: int, location: str | None):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server 约定
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *_args):  # 静音
                return

        return Handler

    def close(self):
        self._server.shutdown()
        self._server.server_close()


# ---------------------------------------------------------------- E01–E03 客户端入口


def test_E01_requests_unauthorized_domain_denied(monkeypatch):
    _online(monkeypatch)
    eg.install_network_deny()
    with pytest.raises(eg.ExternalFetchBlocked) as excinfo:
        requests.get("https://example.com/never", timeout=1)
    assert excinfo.value.reason_code == "EGRESS_DESTINATION_DENIED"
    stats = eg.stats()
    assert stats["policy_denied"] >= 1
    assert stats["os_network_attempts"] == 0  # 连 DNS / 建连都没发生


def test_E02_urllib_unauthorized_domain_denied(monkeypatch):
    import urllib.request

    _online(monkeypatch)
    eg.install_network_deny()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with pytest.raises((eg.ExternalFetchBlocked, OSError)):
        opener.open("http://example.com/never", timeout=1)
    assert eg.stats()["policy_denied"] >= 1


def test_E03_httpx_dependency_absent_or_socket_backed():
    """httpx 未安装；若将来引入，其同步后端走 socket.create_connection（已被覆盖）。"""
    try:
        import httpcore._backends.sync as backend  # noqa: F401
    except ImportError:
        assert importlib.util.find_spec("httpx") is None
    else:
        source = Path(backend.__file__).read_text(encoding="utf-8")
        assert "socket.create_connection" in source


# ---------------------------------------------------------------- E04–E10 已连接 socket


class _FakePeer:
    """带 `getpeername()` 的哑对象：只用于让 wrapper 进入「已连接」分支。"""

    family = socket.AF_INET
    type = socket.SOCK_STREAM

    def __init__(self, peer):
        self._peer = peer

    def getpeername(self):
        return self._peer


@pytest.mark.parametrize(
    ("method_name", "payload"),
    [
        ("_guarded_send", b"x"),
        ("_guarded_sendall", b"x"),
        ("_guarded_sendfile", None),
    ],
)
def test_E04_E05_E07_stream_writes_denied_for_unregistered_peer(
    monkeypatch, method_name, payload
):
    _online(monkeypatch)
    eg.install_network_deny()
    sock = _FakePeer(("93.184.216.34", 80))
    with pytest.raises(eg.ExternalFetchBlocked):
        if method_name == "_guarded_sendfile":
            import io

            getattr(eg, method_name)(sock, io.BytesIO(b"data"))
        else:
            getattr(eg, method_name)(sock, payload)
    assert eg.stats()["policy_denied"] >= 1


def test_E06_sendmsg_denied_for_unregistered_peer(monkeypatch):
    _online(monkeypatch)
    eg.install_network_deny()
    sock = _FakePeer(("93.184.216.34", 80))
    with pytest.raises(eg.ExternalFetchBlocked):
        eg._guarded_sendmsg(sock, [b"x"], [], 0)
    assert eg.stats()["stream_blocked"] >= 1


@pytest.mark.parametrize(
    ("method_name", "payload"),
    [
        ("_guarded_ssl_send", b"x"),
        ("_guarded_ssl_sendall", b"x"),
        ("_guarded_ssl_write", b"x"),
        ("_guarded_ssl_sendfile", None),
    ],
)
def test_E08_E09_E10_tls_writes_denied_for_unregistered_peer(
    monkeypatch, method_name, payload
):
    _online(monkeypatch)
    eg.install_network_deny()
    sock = _FakePeer(("93.184.216.34", 443))
    with pytest.raises(eg.ExternalFetchBlocked):
        if method_name == "_guarded_ssl_sendfile":
            import io

            getattr(eg, method_name)(sock, io.BytesIO(b"data"))
        else:
            getattr(eg, method_name)(sock, payload)
    assert eg.stats()["policy_denied"] >= 1


# ---------------------------------------------------------------- E11–E15 建连 / DNS


def test_E11_ipv4_literal_denied(monkeypatch):
    _online(monkeypatch)
    eg.install_network_deny()
    with pytest.raises(eg.ExternalFetchBlocked):
        socket.create_connection(("93.184.216.34", 80), timeout=1)
    assert eg.stats()["ip_literal_denied"] == 1


def test_E12_ipv6_literal_denied(monkeypatch):
    _online(monkeypatch)
    eg.install_network_deny()
    with pytest.raises(eg.ExternalFetchBlocked):
        socket.create_connection(("2001:db8::1", 443), timeout=1)
    assert eg.stats()["ip_literal_denied"] == 1


def test_E13_udp_sendto_denied(monkeypatch):
    _online(monkeypatch)
    eg.install_network_deny()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(eg.ExternalFetchBlocked):
            sock.sendto(b"x", ("8.8.8.8", 53))
    finally:
        sock.close()
    assert eg.stats()["udp_blocked"] == 1


@pytest.mark.parametrize("host", ["example.com", "dns.google"])
def test_E14_E15_external_dns_denied(monkeypatch, host):
    """DNS UDP/TCP 在应用层入口就被拒绝（解析根本没发出去）。"""
    _online(monkeypatch)
    eg.install_network_deny()
    with pytest.raises(eg.ExternalFetchBlocked):
        socket.getaddrinfo(host, 53, proto=socket.IPPROTO_UDP)
    with pytest.raises(eg.ExternalFetchBlocked):
        socket.getaddrinfo(host, 53, proto=socket.IPPROTO_TCP)
    assert eg.stats()["policy_denied"] >= 2


# ---------------------------------------------------------------- E16–E18 子进程 / 原生


def test_E16_subprocess_is_not_protected_by_app_wrapper():
    """**限制证据**：子进程里的 socket 模块是新的，不继承本进程 monkeypatch。

    这不是「已阻断」，而是「应用层管不到」；因此 OS 级隔离是 P0 阻塞项。
    """
    code = "import socket; print(getattr(socket.create_connection, '__name__', ''))"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0
    assert "__guarded" not in result.stdout


def test_E17_E18_curl_and_native_binaries_not_covered():
    """记录本机可用的外部命令（仅检查存在性，不发起网络请求）。

    curl / wget / 原生程序不受 Python wrapper 约束；结论固定为 NOT_VERIFIED。
    """
    import shutil

    available = {name: bool(shutil.which(name)) for name in ("curl", "wget", "ping", "nslookup")}
    assert set(available) == {"curl", "wget", "ping", "nslookup"}
    # 证据写入报告；这里只断言「我们确实检查过」，不声称已阻断
    assert isinstance(available["curl"], bool)


# ---------------------------------------------------------------- E19–E23 绕过路径


def test_E19_ip_literal_cannot_bypass_domain_rule(monkeypatch):
    _online(monkeypatch)
    eg.install_network_deny()
    with pytest.raises(eg.ExternalFetchBlocked) as excinfo:
        requests.get("http://93.184.216.34/", timeout=1)
    assert excinfo.value.reason_code in {
        "EGRESS_DESTINATION_DENIED",
        "NETWORK_REQUEST_BLOCKED",
    }


def test_E20_redirect_to_unauthorized_domain_denied(monkeypatch):
    _online(monkeypatch)
    eg.install_network_deny()
    server = _LocalHTTPServer(status=302, location="http://example.com/after")
    try:
        with pytest.raises((eg.ExternalFetchBlocked, requests.RequestException)):
            requests.get(f"http://127.0.0.1:{server.port}/start", timeout=2)
    finally:
        server.close()
    assert eg.stats()["policy_denied"] >= 1


def test_E21_non_standard_port_denied(monkeypatch):
    _online(monkeypatch)
    eg.install_network_deny()
    with pytest.raises(eg.ExternalFetchBlocked):
        requests.get("https://api.tushare.pro:8443/data", timeout=1)
    assert egress_policy.host_allowed("api.tushare.pro", 8443) is False


def test_E22_proxy_env_does_not_bypass(monkeypatch):
    _online(monkeypatch)
    eg.install_network_deny()
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "*")
    with pytest.raises(eg.ExternalFetchBlocked):
        requests.get("https://example.com/never", timeout=1)
    assert eg.stats()["policy_denied"] >= 1


def test_E23_unknown_destination_denied(monkeypatch):
    _online(monkeypatch)
    eg.install_network_deny()
    assert egress_policy.describe_denial("totally-unknown.example", 443) == "UNKNOWN_DESTINATION"
    with pytest.raises(eg.ExternalFetchBlocked):
        requests.get("https://totally-unknown.example/x", timeout=1)


# ---------------------------------------------------------------- E24 策略损坏


def test_E24_policy_missing_fails_closed(monkeypatch, tmp_path):
    _online(monkeypatch)
    monkeypatch.setattr(egress_policy, "DEFAULT_POLICY_FILE", tmp_path / "missing.json")
    egress_policy.reset_policy_cache()
    eg.install_network_deny()
    assert egress_policy.policy_status()["loaded"] is False
    assert egress_policy.host_allowed("api.tushare.pro", 443) is False
    with pytest.raises(eg.ExternalFetchBlocked) as excinfo:
        requests.get("https://api.tushare.pro/data", timeout=1)
    assert excinfo.value.reason_code == "EGRESS_POLICY_ERROR"
    assert eg.stats()["policy_errors"] >= 1


def test_E24_policy_version_mismatch_fails_closed(tmp_path):
    bad = tmp_path / "policy.json"
    bad.write_text(
        json.dumps({"version": 999, "default": "deny", "entries": []}), encoding="utf-8"
    )
    egress_policy.reset_policy_cache()
    with pytest.raises(egress_policy.EgressPolicyError) as excinfo:
        egress_policy.load_policy(bad, force=True)
    assert excinfo.value.reason_code == "POLICY_VERSION_MISMATCH"


def test_E24_policy_rejects_ip_entries(tmp_path):
    bad = tmp_path / "policy.json"
    bad.write_text(
        json.dumps(
            {
                "version": 1,
                "default": "deny",
                "entries": [
                    {
                        "id": "X",
                        "host_suffix": "93.184.216.34",
                        "ports": [443],
                        "protocols": ["tcp"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(egress_policy.EgressPolicyError) as excinfo:
        egress_policy.load_policy(bad, force=True)
    assert excinfo.value.reason_code == "POLICY_IP_ENTRY_FORBIDDEN"


# ---------------------------------------------------------------- E25–E28 运行态


def test_E25_os_firewall_missing_blocks_readiness():
    snapshot = readiness.readiness_snapshot()
    assert snapshot["gates"]["EGRESS_OS_POLICY_VERIFIED"] == readiness.NOT_VERIFIED
    assert snapshot["ready"] is False
    assert "EGRESS_OS_POLICY_VERIFIED" in snapshot["blocking"]


def test_E27_loopback_legit_access_allowed(monkeypatch):
    _online(monkeypatch)
    eg.install_network_deny()
    server = _LocalHTTPServer(status=200)
    try:
        response = requests.get(f"http://127.0.0.1:{server.port}/ok", timeout=2)
        assert response.status_code == 200
        assert response.content == b"ok"
    finally:
        server.close()
    # localhost 也映射到回环，不走系统 DNS
    assert socket.getaddrinfo("localhost", 80, proto=socket.IPPROTO_TCP)


def test_E28_allowlisted_only_by_host_and_port():
    assert egress_policy.host_allowed("api.tushare.pro", 443) is True
    assert egress_policy.host_allowed("api.tushare.pro", 80) is False
    assert egress_policy.host_allowed("push2his.eastmoney.com", 443) is True
    assert egress_policy.host_allowed("evil-eastmoney.com", 443) is False
    assert egress_policy.host_allowed("api.tushare.pro", 443, environment="production") is True


def test_policy_inventory_has_no_credentials():
    text = POLICY_FILE.read_text(encoding="utf-8")
    for marker in ("token", "password", "secret", "Authorization", "Bearer"):
        assert marker.lower() not in text.lower()


# ---------------------------------------------------------------- 脱敏


def test_url_credentials_are_redacted(monkeypatch, caplog):
    _online(monkeypatch)
    eg.install_network_deny()
    caplog.set_level("WARNING")
    with pytest.raises(eg.ExternalFetchBlocked) as excinfo:
        requests.get(
            "https://user:supersecret@example.com/path?token=abc123&api_key=xyz",
            timeout=1,
        )
    message = str(excinfo.value)
    assert "supersecret" not in message
    assert "abc123" not in message
    assert "xyz" not in message
    assert all("supersecret" not in record.getMessage() for record in caplog.records)
    assert all("abc123" not in record.getMessage() for record in caplog.records)


def test_metrics_unknown_before_install_and_counters_after(monkeypatch):
    eg.reset_guard_state()
    metrics = eg.metrics()
    assert metrics["egress_policy_errors_total"]["value"] == "UNKNOWN"
    assert metrics["dns_attempts_total"]["value"] == "UNKNOWN"
    assert metrics["subprocess_network_attempts_total"]["value"] == "UNKNOWN"
    assert metrics["os_firewall_denied_total"]["value"] == "UNKNOWN"

    _online(monkeypatch)
    eg.install_network_deny()
    metrics = eg.metrics()
    assert metrics["dns_attempts_total"]["value"] == 0
    assert metrics["egress_policy_errors_total"]["value"] == 0


def test_offline_dns_blocked(monkeypatch):
    _offline(monkeypatch)
    eg.install_network_deny()
    with pytest.raises(eg.ExternalFetchBlocked):
        socket.getaddrinfo("example.com", 443)
    assert eg.stats()["dns_blocked"] == 1


def test_system_proxy_is_filtered_out(monkeypatch):
    """系统代理（含 Windows 注册表）在策略未授权代理时必须被过滤成空表。"""
    _online(monkeypatch)
    eg.install_network_deny()
    import urllib.request as urllib_request
    import requests.utils as requests_utils

    assert urllib_request.getproxies() == {}
    assert requests_utils.getproxies() == {}
    assert egress_policy.proxies_allowed() is False


def test_startup_order_and_readiness_endpoint(monkeypatch):
    """启动顺序证明：调度器启动那一刻，网络闸门必须已经安装。

    同时验证 health（liveness）与 ready 分离：OS 级策略未验证时 ready 必须 false。
    （本环境未安装 httpx，无法用 Starlette TestClient；直接驱动 lifespan。）
    """
    import asyncio

    monkeypatch.setenv(eg.ENV, "false")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")

    import app.main as main_module

    observed: dict[str, object] = {}

    def _spy_start_scheduler():
        observed["installed"] = eg.stats()["installed"]
        observed["dns_guarded"] = eg.stats()["dns_guarded"]
        return None

    monkeypatch.setattr(main_module, "start_scheduler", _spy_start_scheduler)

    async def _run():
        async with main_module.lifespan(main_module.app):
            observed["snapshot"] = readiness.readiness_snapshot()

    asyncio.run(_run())

    assert observed["installed"] is True
    assert observed["dns_guarded"] is True
    snapshot = observed["snapshot"]
    assert snapshot["ready"] is False
    assert snapshot["gates"]["EGRESS_OS_POLICY_VERIFIED"] == readiness.NOT_VERIFIED
    assert snapshot["gates"]["EXTERNAL_FETCH_DISABLED"] == readiness.PASS
    assert snapshot["gates"]["SCHEDULER_DISABLED"] == readiness.PASS
    # 路由已注册（readiness 端点存在，且与 health 分开）
    paths = {getattr(route, "path", None) for route in main_module.app.routes}
    assert {"/api/health", "/api/ready"} <= paths
