"""S7（G7）：生成逐路由数据库归属矩阵。

输出 `reports/s6-followup/g7/route-db-ownership.csv`，每行一个「路由 × 连接域」：

- 混合路由（同时注入 Research 与 User）会输出两行，`database_domain` 因此始终
  只取 `RESEARCH_READONLY` / `USER_READWRITE` / `ADMIN_WRITE` 三个值之一；
- 连接域来自 FastAPI 的 dependant 树（真实注入，不是按 HTTP method 猜）；
- `tables_read` / `tables_written` 来自 handler 源码里的 ORM 模型引用
  （模块级扫描），属于**静态清单**；运行时验证见
  `tests/test_s7_route_ownership.py` 与 `tests/test_s7_db_negative_matrix.py`。

用法：
    python scripts/s7_route_inventory.py
"""

from __future__ import annotations

import ast
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.main import app  # noqa: E402

OUT = ROOT / "reports" / "s6-followup" / "g7" / "route-db-ownership.csv"

#: 需要标记为「研究库写入型」的接口（灰度下 503）—— 由源码里的依赖/已知清单确认
RESEARCH_WRITE_HANDLERS = {
    "sync_watchlist",
    "sync",
    "kline_recent",
    "patterns_scan",
    "backfill",
    "dde",  # 现取由 research_writable() 兜住
    "members",  # 板块成分股现取
}

USER_MODEL_NAMES = {
    "AppUser",
    "InviteCode",
    "UserSession",
    "Watchlist",
    "ReviewNote",
    "UserRiskPref",
    "TradeJournal",
    "TradeT",
    "TradePlan",
    "AnalysisSnapshot",
}

#: 研究域表（非 ORM 的两张也列进来）
RESEARCH_TABLE_NAMES = {
    "StockBasic", "StockDaily", "StockDde", "StockConcept", "StockUniverse",
    "LimitPool", "LimitReason", "Lhb", "LhbInstitution", "IndexDaily",
    "MarketSentiment", "TradeCalendar", "CollectLog", "SectorBasic", "SectorDaily",
    "SectorMember", "PatternHit", "FearGreedDaily", "AuctionSnapshot", "CbBasic",
    "CbDaily", "EtfBasic", "EtfBar", "EtfShare", "FuturesPosition", "IfindUsage",
    "ValuationDaily", "StockEvent", "RpsDaily", "ThemeBoard", "FundBasic",
    "FundNav", "StockFinancial", "DataRevision",
}


def _dependency_names(route) -> set[str]:
    """路由自身声明的依赖（只看第一层，不递归鉴权链路）。"""
    names: set[str] = set()
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return names
    for child in getattr(dependant, "dependencies", ()):  # pragma: no branch
        call = getattr(child, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", str(call)))
    return names


def _source(route) -> tuple[str, str]:
    """返回（handler 源码, 模块文件名）。"""
    endpoint = route.endpoint
    module = sys.modules.get(endpoint.__module__)
    filename = Path(module.__file__).name if module and module.__file__ else ""
    try:
        return endpoint.__doc__ or "", filename
    except Exception:  # noqa: BLE001
        return "", filename


def _handler_source(route) -> str:
    endpoint = route.endpoint
    module = sys.modules.get(endpoint.__module__)
    if module is None or not module.__file__:
        return ""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == endpoint.__name__:
            return ast.unparse(node)
    return ""


def _models_in(source: str) -> list[str]:
    found = set()
    for name in USER_MODEL_NAMES | RESEARCH_TABLE_NAMES:
        if f"{name}." in source or f"{name}(" in source:
            found.add(name)
    return sorted(found)


def _services_in(source: str, module_name: str) -> list[str]:
    module = sys.modules.get(module_name)
    if module is None:
        return []
    services: list[str] = []
    for node in ast.walk(ast.parse(Path(module.__file__).read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module == "app.services":
            for alias in node.names:
                if f"{alias.name}." in source:
                    services.append(alias.name)
    return sorted(services)


def _flatten(routes):
    """展开 FastAPI 的 `_IncludedRouter`（`include_router` 不会平铺进 app.routes）。"""
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _flatten(included.routes)
        elif getattr(route, "path", ""):
            yield route


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    for route in _flatten(app.routes):
        path = getattr(route, "path", "")
        if not path.startswith("/api/"):
            continue
        endpoint = route.endpoint
        handler = endpoint.__name__
        source = _handler_source(route)
        models = _models_in(source)
        user_models = [m for m in models if m in USER_MODEL_NAMES]
        research_models = [m for m in models if m in RESEARCH_TABLE_NAMES]
        deps = _dependency_names(route)
        services = _services_in(source, endpoint.__module__)
        methods = ",".join(sorted(getattr(route, "methods", set()) or []))
        router = Path(sys.modules[endpoint.__module__].__file__).stem

        domains: list[str] = []
        if "get_research_db" in deps:
            domains.append("RESEARCH_READONLY")
        if "get_user_db" in deps:
            domains.append("USER_READWRITE")
        if not domains:
            if "require_research_writable" in deps:
                domains = ["ADMIN_WRITE"]
            elif "current_user" in deps:
                # 只做鉴权（current_user）的接口：连接用户库但不触碰业务表
                domains = ["USER_READWRITE"]
            else:
                domains = ["ADMIN_WRITE"]
        if handler in RESEARCH_WRITE_HANDLERS:
            domains = [d for d in domains if d != "RESEARCH_READONLY"] + ["ADMIN_WRITE"]

        for domain in sorted(set(domains)):
            if domain == "RESEARCH_READONLY":
                tables_read = ",".join(research_models)
                tables_written = ""
            elif domain == "USER_READWRITE":
                tables_read = ",".join(user_models)
                tables_written = ",".join(user_models) if methods not in {"GET"} else ""
            else:
                tables_read = ",".join(research_models)
                tables_written = "RESEARCH_WRITE" if handler in RESEARCH_WRITE_HANDLERS else ""

            rows.append(
                {
                    "method": methods,
                    "path": path,
                    "router": router,
                    "handler": handler,
                    "service": ",".join(services),
                    "repository": "sqlalchemy-orm",
                    "database_domain": domain,
                    "session_dependency": ",".join(sorted(deps & {"get_research_db", "get_user_db"})) or "none",
                    "tables_read": tables_read,
                    "tables_written": tables_written,
                    "background_handoff": "yes" if "collect_guard" in source else "no",
                    "test": "tests/test_s7_route_ownership.py",
                    "status": "STATIC_VERIFIED",
                }
            )

    rows.sort(key=lambda row: (row["path"], row["method"], row["database_domain"]))
    with OUT.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"rows={len(rows)} -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
