"""M6.4 Batch 0/1：iFinD 连通性、工具字典与最小样本探针。

约束：
- 只读原库；不写任何 `*.db`（把 `record_call` 临时置空，避免用量表被写）；
- 只发起少量请求，记录调用数、估算单元格、耗时与错误码；
- 输出到 `backend/data/m64_staging/<batch_id>/`，报告只引用哈希与摘要，不输出 token。

用法：

    python scripts/m64_ifind_probe.py
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import get_settings  # noqa: E402
from app.sources import ifind as ifind_module  # noqa: E402

STAGING_ROOT = ROOT / "backend/data/m64_staging"
REPORT_DIR = ROOT / "reports/trend_engine/m64"
BATCH_ID = datetime.now(timezone.utc).strftime("batch_%Y%m%dT%H%M%SZ")
BUDGET_CELLS = 50_000


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


def save(batch_dir: Path, name: str, payload: dict) -> dict:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    path = batch_dir / name
    path.write_text(text + "\n", encoding="utf-8")
    return {"file": name, "sha256": sha256_text(text), "bytes": len(text.encode("utf-8"))}


def estimate_cells(rows: list[dict]) -> int:
    return sum(max(len(row), 1) for row in rows)


def main() -> int:
    if STAGING_ROOT.exists() and any(STAGING_ROOT.iterdir()):
        raise SystemExit(f"staging 已存在且非空，拒绝覆盖：{STAGING_ROOT}")
    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    batch_dir = STAGING_ROOT / BATCH_ID
    batch_dir.mkdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    has_token = bool((settings.ifind_auth_token or "").strip())
    ledger = {
        "batch_id": BATCH_ID,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "budget_cells": BUDGET_CELLS,
        "token_present": has_token,
        "sdk": "iFinDPy: NOT_INSTALLED",
        "transport": "existing HTTP MCP client (backend/app/sources/ifind.py)",
        "calls": [],
        "cells_estimated": 0,
        "status": "UNKNOWN",
    }
    if not has_token:
        ledger["status"] = "NO_TOKEN"
        (batch_dir / "probe_ledger.json").write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps({"status": "NO_TOKEN"}, ensure_ascii=False))
        return 1

    # 禁止把用量写进任何数据库：本进程内把 record_call 置空
    ifind_module.record_call = lambda *args, **kwargs: None  # type: ignore[assignment]
    client = ifind_module.IfindClient(settings)

    def run(server: str, tool: str, params: dict) -> dict:
        started = time.perf_counter()
        entry = {"server": server, "tool": tool, "params_keys": sorted(params)}
        try:
            response = client.call(server, tool, params)
            elapsed = round(time.perf_counter() - started, 2)
            text = json.dumps(response, ensure_ascii=False, default=str)
            entry.update(
                {
                    "ok": True,
                    "elapsed_s": elapsed,
                    "response_bytes": len(text.encode("utf-8")),
                    "payload_sha256": sha256_text(text),
                }
            )
            ledger["calls"].append(entry)
            return response
        except Exception as exc:  # noqa: BLE001 - 探针必须记录任何失败
            elapsed = round(time.perf_counter() - started, 2)
            entry.update(
                {
                    "ok": False,
                    "elapsed_s": elapsed,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:300],
                }
            )
            ledger["calls"].append(entry)
            return {"__error__": entry}

    # 1) tools/list：不消耗行情单元格，用于拿权威工具名
    tools_payload = {}
    for server in ("stock", "index"):
        started = time.perf_counter()
        try:
            client._ensure_session(server)
            _response, body = client._post(
                server, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            )
            tools = ((body or {}).get("result") or {}).get("tools") or []
            tools_payload[server] = [
                {"name": item.get("name"), "description": (item.get("description") or "")[:200]} for item in tools
            ]
            ledger["calls"].append(
                {
                    "server": server,
                    "tool": "tools/list",
                    "ok": True,
                    "elapsed_s": round(time.perf_counter() - started, 2),
                    "tools": len(tools),
                }
            )
        except Exception as exc:  # noqa: BLE001
            tools_payload[server] = []
            ledger["calls"].append(
                {
                    "server": server,
                    "tool": "tools/list",
                    "ok": False,
                    "elapsed_s": round(time.perf_counter() - started, 2),
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:300],
                }
            )
    ledger["files"] = [save(batch_dir, "tools_list.json", tools_payload)]

    stock_tools = {item["name"] for item in tools_payload.get("stock") or []}
    index_tools = {item["name"] for item in tools_payload.get("index") or []}
    ledger["stock_tool_names"] = sorted(stock_tools)
    ledger["index_tool_names"] = sorted(index_tools)

    # 2) 个股日行情最小样本（20 个日历日，约 14 个交易日）
    if "get_stock_performance" in stock_tools:
        query = "600519 从2025-07-01到2025-07-20 每个交易日的开盘价、最高价、最低价、收盘价、成交量、成交额、涨跌幅"
        response = run("stock", "get_stock_performance", {"query": query})
        if "__error__" not in response:
            inner = ifind_module._extract(response)[1]
            answer = inner.get("answer") or ""
            rows = [row for table in ifind_module.parse_tables(answer) for row in table]
            ledger["cells_estimated"] += estimate_cells(rows)
            ledger["files"].append(save(batch_dir, "stock_history_600519_20250701_20250720.json", {"rows": rows}))
            ledger["stock_history_rows"] = len(rows)

    # 3) 指数日行情最小样本
    if "index_data" in index_tools:
        query = "000001.SH 从2026-09-25到2026-10-09 每个交易日的开盘价、最高价、最低价、收盘价、涨跌幅、成交额"
        response = run("index", "index_data", {"query": query})
        if "__error__" not in response:
            inner = ifind_module._extract(response)[1]
            answer = inner.get("answer") or ""
            rows = [row for table in ifind_module.parse_tables(answer) for row in table]
            ledger["cells_estimated"] += estimate_cells(rows)
            ledger["files"].append(save(batch_dir, "index_000001SH_20260925_20261009.json", {"rows": rows}))
            ledger["index_rows"] = len(rows)

    # 4) 公司行为 / 分红：仅在工具明确存在时调用
    event_tool = next((name for name in stock_tools if "event" in name.lower()), None)
    if event_tool:
        query = "600519 2025年 分红方案、股权登记日、除权除息日、派息日"
        response = run("stock", event_tool, {"query": query})
        if "__error__" not in response:
            inner = ifind_module._extract(response)[1]
            answer = inner.get("answer") or ""
            rows = [row for table in ifind_module.parse_tables(answer) for row in table]
            ledger["cells_estimated"] += estimate_cells(rows)
            ledger["files"].append(save(batch_dir, "stock_events_600519_2025.json", {"tool": event_tool, "rows": rows}))
            ledger["event_rows"] = len(rows)

    ledger["status"] = "OK" if any(item.get("ok") for item in ledger["calls"]) else "ALL_FAILED"
    ledger["budget_used_pct"] = round(ledger["cells_estimated"] / BUDGET_CELLS * 100, 2)
    ledger["staging_dir"] = str(batch_dir)
    ledger_file = save(batch_dir, "probe_ledger.json", ledger)
    (REPORT_DIR / "api_usage_before_after.json").write_text(
        json.dumps(
            {
                "batch_id": BATCH_ID,
                "generated_at": ledger["generated_at"],
                "calls": [
                    {key: item.get(key) for key in ("server", "tool", "ok", "elapsed_s", "tools")}
                    for item in ledger["calls"]
                ],
                "cells_estimated": ledger["cells_estimated"],
                "budget_cells": BUDGET_CELLS,
                "budget_used_pct": ledger["budget_used_pct"],
                "status": ledger["status"],
                "staging_ledger_sha256": ledger_file["sha256"],
                "note": "未调用官方用量统计（无 SDK）；用量以本地估算与调用次数为准，标记 USAGE_UNVERIFIED",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": ledger["status"],
                "calls": len(ledger["calls"]),
                "stock_tools": len(stock_tools),
                "index_tools": len(index_tools),
                "stock_rows": ledger.get("stock_history_rows"),
                "index_rows": ledger.get("index_rows"),
                "event_rows": ledger.get("event_rows"),
                "cells": ledger["cells_estimated"],
                "staging": str(batch_dir),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
