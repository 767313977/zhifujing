"""`/api/health` 的默认最小响应与 `detail=1` 内网分支（2026-10-31 加）。

这里**不用 `TestClient`**：环境里没装 `httpx`，而 `health()` 只用到
`request.client.host`（见 `services.auth.client_ip`），一个最小桩就够，
顺便把「公网即使带 detail=1 也拿不到排查字段」这条安全边界钉住。
"""

from types import SimpleNamespace

import pytest

from app.main import app, health


def _request(host: str) -> SimpleNamespace:
    """够 `client_ip(request)` 用的最小 Request 桩。"""
    return SimpleNamespace(client=SimpleNamespace(host=host))


def test_health_minimal():
    """默认（不带 detail）只回 {"status": "ok"} —— 部署/监控的兼容面。"""
    assert health(_request("127.0.0.1")) == {"status": "ok"}
    # 显式 detail=0 与不带参数等价
    assert health(_request("127.0.0.1"), detail=0) == {"status": "ok"}


def test_health_head_supported():
    """HEAD 与 GET 挂在同一条路由上（监控常用 `curl -I`）。"""
    routes = [route for route in app.routes if getattr(route, "path", None) == "/api/health"]
    assert routes, "没找到 /api/health 路由"
    assert {"GET", "HEAD"} <= set(routes[0].methods)


def test_health_detail_local():
    """本机带 detail=1：额外回三个 bool，且**只有**这三个非敏感字段。"""
    payload = health(_request("127.0.0.1"), detail=1)
    assert payload["status"] == "ok"
    for key in ("ifind_configured", "frontend_built", "db_exists"):
        assert isinstance(payload[key], bool)
    # 敏感信息一个都不能有（路径 / token / 主机名）
    assert set(payload) == {
        "status",
        "ifind_configured",
        "frontend_built",
        "db_exists",
    }


@pytest.mark.parametrize("host", ["192.168.1.10", "10.0.0.5", "::1"])
def test_health_detail_private_network(host):
    """内网段同样放行（判据复用登录闸的 `_is_local_or_private`）。"""
    assert "db_exists" in health(_request(host), detail=1)


def test_health_detail_public_stays_minimal():
    """公网即使带 detail=1，也只拿到最小响应。

    ⚠️ 别拿 `203.0.113.x` 这类文档段当「公网」—— Python 的 `ipaddress` 把保留段
    也算成 `is_private=True`（3.13 起），拿它测会假阴性。这里用真实公网 IP。
    """
    assert health(_request("8.8.8.8"), detail=1) == {"status": "ok"}
