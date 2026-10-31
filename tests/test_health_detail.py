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
    """HEAD 仍然可用（监控常用 `curl -I`），但**与 GET 分开挂**。"""
    routes = [route for route in app.routes if getattr(route, "path", None) == "/api/health"]
    methods = set().union(*(set(route.methods) for route in routes))
    assert {"GET", "HEAD"} <= methods
    # 2026-10-31：拆成两条路由，避免 OpenAPI 出现重复 operationId
    get_routes = [route for route in routes if "GET" in route.methods]
    head_routes = [route for route in routes if "HEAD" in route.methods]
    assert len(get_routes) == 1 and len(head_routes) == 1
    assert get_routes[0] is not head_routes[0]
    assert head_routes[0].include_in_schema is False


def test_openapi_has_no_duplicate_operation_ids():
    """OpenAPI 里不能有重复 operationId —— 否则 /docs 与生成器都会告警。"""
    schema = app.openapi()
    ids = [
        operation["operationId"]
        for path in schema["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]
    duplicated = {item for item in ids if ids.count(item) > 1}
    assert not duplicated, f"重复的 operationId：{duplicated}"


def test_health_path_item_only_documents_get():
    schema = app.openapi()
    item = schema["paths"]["/api/health"]
    assert "get" in item
    assert "head" not in item


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
