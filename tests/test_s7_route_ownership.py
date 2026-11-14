"""S6（G7）：路由数据库归属静态检查。

目的不是「跑一遍接口」，而是**在代码层**保证：

1. 所有 `/api/*` 路由都显式注入 `get_research_db` 或 `get_user_db`，
   不再使用含糊的混合入口 `get_db`；
2. 研究域模块不得出现 `get_user_db`，用户域模块不得出现 `get_research_db`
   （混合域模块按路由逐个检查：至少一个，且不能是 `get_db`）；
3. `app/api` 下不得直接 `SessionLocal()` / `session_scope()` —— 那会绕过
   「研究库只读 / 用户库可写」的连接域约束。

这些断言直接读 FastAPI 的 dependant 树与源码，属于结构性回归：
将来新增路由忘记选连接域时会立即失败。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.main import app

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "backend" / "app" / "api"

#: 只读研究域（不得注入可写用户库）
RESEARCH_ONLY_PREFIXES = (
    "/api/market",
    "/api/sentiment",
    "/api/auction",
    "/api/limit",
    "/api/themes",
    "/api/funds",
    "/api/sector",
    "/api/patterns",
    "/api/pools",
    "/api/research",
    "/api/anomaly",
    "/api/valuation",
    "/api/cb",
    "/api/etf",
    "/api/quality",
)

#: 用户域（不得注入研究库）
USER_ONLY_PREFIXES = (
    "/api/auth",
    "/api/note",
)

#: 混合域：逐路由检查必须显式选择过连接域
MIXED_PREFIXES = (
    "/api/journal",
    "/api/positions",
    "/api/plans",
    "/api/risk",
    "/api/watchlist",
    "/api/analysis",
    "/api/admin",
    "/api/premarket",
    "/api/stock",
)

_SKIP_PATHS = {"/api/health", "/api/ready"}

#: 明确**不访问数据库**的接口（静态常量 / 探活）。新增无库路由必须登记到这里，
#: 否则「每条路由都要显式声明连接域」的断言会失败。
_NO_DB_PATHS = {"/api/health", "/api/ready", "/api/patterns/catalog"}


def _dependency_names(route) -> set[str]:
    """**路由自身**声明的依赖名（只看第一层）。

    不能递归：`current_user` 内部注入 `get_user_db`、`require_admin` 又依赖
    `current_user`，递归会把「只做鉴权的用户库连接」误判成业务连接域。
    """
    names: set[str] = set()
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return names
    for child in getattr(dependant, "dependencies", ()):  # pragma: no branch
        call = getattr(child, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", str(call)))
    return names


def _explicit_sessions(route) -> set[str]:
    return _dependency_names(route) & {"get_research_db", "get_user_db"}


def _declared_domains(route) -> set[str]:
    """路由声明过的连接域（含「只鉴权」与「研究库写守卫」两种显式标记）。"""
    names = _dependency_names(route)
    domains = names & {"get_research_db", "get_user_db", "require_research_writable"}
    if "current_user" in names:
        domains = domains | {"current_user"}
    return domains


def _flatten(routes):
    """展开 FastAPI 的 `_IncludedRouter`（`include_router` 不会被平铺进 app.routes）。"""
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _flatten(included.routes)
        elif getattr(route, "path", ""):
            yield route


def _api_routes() -> list:
    return [
        route for route in _flatten(app.routes) if route.path.startswith("/api/")
    ]


def test_all_api_routes_avoid_ambiguous_get_db():
    offenders = []
    for route in _api_routes():
        names = _dependency_names(route)
        if "get_db" in names:
            offenders.append(route.path)
    assert offenders == [], f"仍在使用含糊 get_db 的路由：{offenders}"


def test_every_api_route_declares_a_connection_domain():
    missing = []
    for route in _api_routes():
        if not _declared_domains(route) and route.path not in _NO_DB_PATHS:
            missing.append(route.path)
    assert missing == [], f"未显式声明连接域的路由：{missing}"


def test_research_only_modules_never_inject_user_db():
    offenders = []
    for route in _api_routes():
        if not route.path.startswith(RESEARCH_ONLY_PREFIXES):
            continue
        if "get_user_db" in _explicit_sessions(route):
            offenders.append(route.path)
    assert offenders == [], f"研究域路由注入了可写用户库：{offenders}"


def test_user_only_modules_never_inject_research_db():
    offenders = []
    for route in _api_routes():
        if not route.path.startswith(USER_ONLY_PREFIXES):
            continue
        if "get_research_db" in _explicit_sessions(route):
            offenders.append(route.path)
    assert offenders == [], f"用户域路由注入了研究库：{offenders}"


def test_mixed_modules_choose_explicit_domain_per_route():
    offenders = []
    for route in _api_routes():
        if not route.path.startswith(MIXED_PREFIXES):
            continue
        if not _declared_domains(route):
            offenders.append(route.path)
    assert offenders == [], f"混合域路由未显式选择连接域：{offenders}"


def test_api_layer_has_no_hidden_session_factories():
    """`app/api` 下不得出现 `SessionLocal()` / `session_scope()` / `Depends(get_db)`。

    用 AST 而不是文本正则：注释与 docstring 里提到这些名字是正常的
    （说明文字），只有**真实调用**才算违规。
    """
    offenders: list[str] = []

    def _is_depends_get_db(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Depends"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "get_db"
        )

    def _is_forbidden_call(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        name = func.id if isinstance(func, ast.Name) else None
        if isinstance(func, ast.Attribute):
            name = func.attr
        return name in {"SessionLocal", "session_scope"}

    for path in API_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if _is_forbidden_call(node) or _is_depends_get_db(node):
                offenders.append(f"{path.name}:{getattr(node, 'lineno', '?')}")
    assert offenders == [], "API 层存在隐藏的默认 Session：" + ", ".join(offenders)


def test_user_table_writes_live_in_user_domain_modules():
    """兜底：用户表的写操作只应出现在用户域/混合域模块里。

    这是**粗粒度**检查：命中 `session.add(<用户表名>` / `session.delete(<用户表名>`
    形式的直接写法。Service 层通过参数拿 Session 的写法由连接域注入测试覆盖。
    """
    user_tables = ("Watchlist", "ReviewNote", "TradeJournal", "TradeT", "TradePlan",
                   "AnalysisSnapshot", "UserRiskPref", "AppUser", "InviteCode")
    allowed = set(MIXED_PREFIXES) | set(USER_ONLY_PREFIXES)
    pattern = re.compile(r"\.(add|delete)\(\s*(" + "|".join(user_tables) + r")\b")
    offenders: list[str] = []
    for path in API_DIR.glob("*.py"):
        module_prefix = "/api/" + path.stem.replace("_", "-")
        if module_prefix.startswith(tuple(p for p in allowed if p != "/api/admin")):
            continue
        if path.stem in {
            "journal", "positions", "plans", "risk", "watchlist", "analysis",
            "admin", "note", "auth",
        }:
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert offenders == [], "用户表写入出现在研究域模块：\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "module",
    ["watchlist.py", "journal.py", "plans.py", "positions.py", "risk.py", "analysis.py"],
)
def test_mixed_modules_import_both_domains(module: str):
    text = (API_DIR / module).read_text(encoding="utf-8")
    assert "get_user_db" in text, f"{module} 应注入用户库"
    assert "get_research_db" in text, f"{module} 应注入研究库"
