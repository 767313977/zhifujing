"""M6.6 增补：iFinD 个人基础版 100 次请求内的真实样本探针。

约束：
- 预算上限 100，预占式计数，失败/超时/重试都计数；
- 只调用白名单工具；不写任何数据库（record_call 置空）；
- 未确认本地存储许可 → 只保存结构摘要、哈希与派生指标，不落原始 payload；
- Token 从不打印、不写账本。

用法：

    python scripts/m66_ifind_probe.py --phase capability
    python scripts/m66_ifind_probe.py --phase g9 --batch ifind_personal_100_xxx
    python scripts/m66_ifind_probe.py --phase g3 --batch ifind_personal_100_xxx
"""

from __future__ import annotations

import argparse
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
from app.services.trend_engine import budget_guard as bg  # noqa: E402
from app.sources import ifind as ifind_module  # noqa: E402

STAGING_ROOT = ROOT / "backend/data/m66_staging"
REPORT_DIR = ROOT / "reports/trend_engine/m66/ifind_personal_100"

ALLOWED_TOOLS = {
    "initialize",
    "notifications/initialized",
    "tools/list",
    "get_stock_performance",
    "get_stock_events",
    "get_stock_info",
    "search_stocks",
    "index_data",
    "sector_data",
    "index_highfreq_quotes",
}

G9_QUERIES = [
    ("300750 2026-03-01至2026-05-31 的分红、送股、转增、配股、股本变动事件；列出公告日、股权登记日、除权除息日、派息日、每股派现（税前/税后）、送股比例、转增比例、配股比例、配股价格", "get_stock_events", ("G9-CD-02",)),
    ("600519 2026-05-01至2026-07-31 的分红、送股、转增、配股事件；列出公告日、股权登记日、除权除息日、派息日、每股派现（税前/税后）、送股比例、转增比例", "get_stock_events", ("G9-CD-01",)),
    ("002594 2026-06-01至2026-08-31 的分红、送股、转增、配股事件；列出公告日、股权登记日、除权除息日、派息日、每股派现、送股比例、转增比例", "get_stock_events", ("G9-BT-01",)),
    ("001225 2026-08-01至2026-10-31 的分红、送股、转增事件；列出公告日、股权登记日、除权除息日、派息日、送股比例、转增比例", "get_stock_events", ("G9-BT-02",)),
    ("688110 2026-08-01至2026-11-15 的分红、送股、转增事件；列出公告日、股权登记日、除权除息日、派息日、送股比例、转增比例", "get_stock_events", ("G9-BT-03",)),
    ("688270 2026-05-01至2026-07-31 的分红、送股、转增事件；列出公告日、股权登记日、除权除息日、派息日、每股派现、送股比例、转增比例", "get_stock_events", ("G9-CD-03",)),
    ("300176 2026-07-01至2026-09-30 的配股事件；列出公告日、股权登记日、除权除息日、配股比例、配股价格、实际配股数量", "get_stock_events", ("G9-RI-01",)),
    ("2025年实施配股的A股上市公司名单", "search_stocks", ("G9-RI-02",)),
    ("600519 从2026-06-15到2026-07-10 每个交易日的开盘价、最高价、最低价、收盘价、前收盘价、复权因子", "get_stock_performance", ("G9-CD-01",)),
    ("600519 从2026-06-15到2026-07-10 每个交易日的前复权收盘价、后复权收盘价", "get_stock_performance", ("G9-CD-01",)),
    ("300750 从2026-04-08到2026-05-08 每个交易日的开盘价、最高价、最低价、收盘价、前收盘价、复权因子", "get_stock_performance", ("G9-CD-02",)),
    ("300750 从2026-04-08到2026-05-08 每个交易日的前复权收盘价、后复权收盘价", "get_stock_performance", ("G9-CD-02",)),
    ("002594 从2026-07-15到2026-08-14 每个交易日的开盘价、最高价、最低价、收盘价、前收盘价、复权因子", "get_stock_performance", ("G9-BT-01",)),
    ("300176 从2026-08-01到2026-08-31 每个交易日的开盘价、最高价、最低价、收盘价、前收盘价、复权因子", "get_stock_performance", ("G9-RI-01",)),
    ("001225 从2026-09-08到2026-10-09 每个交易日的开盘价、最高价、最低价、收盘价、前收盘价、复权因子", "get_stock_performance", ("G9-BT-02",)),
    ("688270 从2026-06-01到2026-06-30 每个交易日的开盘价、最高价、最低价、收盘价、前收盘价、复权因子", "get_stock_performance", ("G9-CD-03",)),
    ("688110 从2026-09-01到2026-10-15 每个交易日的开盘价、最高价、最低价、收盘价、前收盘价、复权因子", "get_stock_performance", ("G9-BT-03",)),
    ("600519 从2025-07-01到2025-07-20 每个交易日的开盘价、最高价、最低价、收盘价、前收盘价、复权因子", "get_stock_performance", ("G9-CT-01",)),
    ("000001 从2026-01-05到2026-01-30 每个交易日的开盘价、最高价、最低价、收盘价、前收盘价、复权因子", "get_stock_performance", ("G9-CT-02",)),
]

G3_QUERIES = [
    ("芯片概念 2025-03-05 的成分股名单，包含证券代码与名称", "sector_data", ("G3-C1-01",), "index"),
    ("电力设备 2025-03-05 的成分股名单，包含证券代码与名称", "sector_data", ("G3-H1-01",), "index"),
]

G9_FOLLOWUP = [
    ("300750 2026年4月 分红方案：每股派息金额、股权登记日、除权除息日、派息日", "get_stock_events", ("G9-CD-02",), "stock"),
    ("600519 2026年中期分红方案：每股派息金额、股权登记日、除权除息日、派息日", "get_stock_events", ("G9-CD-01",), "stock"),
    ("2025年配股的股票", "search_stocks", ("G9-RI-02",), "stock"),
    ("600519 从2026-06-15到2026-07-10 每个交易日的后复权收盘价", "get_stock_performance", ("G9-CD-01",), "stock"),
    ("600519 从2026-06-15到2026-07-10 每个交易日的前复权收盘价", "get_stock_performance", ("G9-CD-01",), "stock"),
]

G3_DATE_CHECK = [
    ("芯片概念 2026-09-28 的成分股名单，包含证券代码与名称", "sector_data", ("G3-C1-03",), "index"),
    ("电力设备 2026-09-28 的成分股名单，包含证券代码与名称", "sector_data", ("G3-H1-03",), "index"),
]

G9_METRICS = [
    ("600519 从2026-06-15到2026-07-10 每个交易日的收盘价、复权因子", "get_stock_performance", ("G9-CD-01",), "stock"),
    ("600519 从2026-06-15到2026-07-10 每个交易日的后复权收盘价", "get_stock_performance", ("G9-CD-01",), "stock"),
    ("600519 从2026-06-15到2026-07-10 每个交易日的前复权收盘价", "get_stock_performance", ("G9-CD-01",), "stock"),
]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


def new_batch() -> Path:
    batch_id = datetime.now(timezone.utc).strftime("ifind_personal_100_%Y%m%dT%H%M%SZ")
    path = STAGING_ROOT / batch_id
    if path.exists():
        raise SystemExit(f"批次目录已存在，拒绝覆盖：{path}")
    path.mkdir(parents=True)
    return path


def load_batch(name: str) -> Path:
    path = STAGING_ROOT / name
    if not path.exists():
        raise SystemExit(f"批次不存在：{path}")
    return path


def summarize(response: dict) -> dict:
    inner = ifind_module._extract(response)[1]
    answer = inner.get("answer") or ""
    tables = ifind_module.parse_tables(answer)
    rows = [row for table in tables for row in table]
    columns = sorted({key for row in rows for key in row})
    return {
        "rows": len(rows),
        "columns": columns,
        "sample_dates": sorted({str(row.get("日期") or row.get("date") or "") for row in rows if row.get("日期") or row.get("date")})[:12],
        "answer_chars": len(answer),
        "answer_head": answer[:300],
        "payload_hash": sha256_text(json.dumps(response, ensure_ascii=False, sort_keys=True, default=str)),
        "parsed_rows": rows,
    }


def _num(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "—", "--"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def compute_price_metrics(rows_raw: list[dict], rows_hfq: list[dict], rows_qfq: list[dict]) -> dict:
    """只在内存中计算派生指标，不落原始序列。阈值先验设定为 5bp（价格四位小数舍入）。"""
    raw = {row.get("日期"): (_num(row.get("收盘价")), _num(row.get("复权因子"))) for row in rows_raw}
    hfq = {row.get("日期"): _num(row.get("收盘价")) for row in rows_hfq}
    qfq = {row.get("日期"): _num(row.get("收盘价")) for row in rows_qfq}
    dates = sorted(set(raw) & set(hfq) & set(qfq))
    points = []
    for day in dates:
        price, factor = raw[day]
        if price is None or factor is None or hfq.get(day) is None or qfq.get(day) is None:
            continue
        expected = price * factor
        points.append(
            {
                "trade_date": day,
                "raw_close": price,
                "factor": factor,
                "backward_close": hfq[day],
                "forward_close": qfq[day],
                "raw_x_factor": round(expected, 6),
                "backward_rel_bp": round(abs(hfq[day] - expected) / expected * 10000, 4),
                "forward_rel_bp": round(abs(qfq[day] - price) / price * 10000, 4),
            }
        )
    factor_changes = []
    ordered = [item for item in points if item["factor"] is not None]
    for previous, current in zip(ordered, ordered[1:]):
        if previous["factor"] != current["factor"]:
            factor_changes.append(
                {
                    "from_date": previous["trade_date"],
                    "to_date": current["trade_date"],
                    "factor_before": previous["factor"],
                    "factor_after": current["factor"],
                    "raw_return_pct": round((current["raw_close"] / previous["raw_close"] - 1) * 100, 4),
                    "backward_return_pct": round(
                        (current["backward_close"] / previous["backward_close"] - 1) * 100, 4
                    ),
                }
            )
    back_bps = [item["backward_rel_bp"] for item in points]
    fwd_bps = [item["forward_rel_bp"] for item in points]
    return {
        "joined_days": len(points),
        "points": points,
        "backward_max_rel_bp": max(back_bps) if back_bps else None,
        "backward_mean_rel_bp": round(sum(back_bps) / len(back_bps), 4) if back_bps else None,
        "forward_max_rel_bp": max(fwd_bps) if fwd_bps else None,
        "factor_changes": factor_changes,
        "prior_threshold_bp": 5.0,
        "backward_formula_within_threshold": bool(back_bps) and max(back_bps) <= 5.0,
        "forward_equals_raw_within_threshold": bool(fwd_bps) and max(fwd_bps) <= 5.0,
        "verdict": "QUERY_LEVEL_SEPARATION_ONLY_NO_FIELD_LABEL",
        "note": "前/后复权只能通过提问措辞区分，响应仍只有一个『收盘价』列；无 adjust_type 字段",
    }


def run_query(client, guard, batch: Path, tool: str, query: str, purpose: str, sample_ids: tuple[str, ...], server: str = "stock") -> dict:
    started = time.perf_counter()
    reservation = guard.reserve(
        tool_name=tool,
        endpoint=f"{server}/{tool}",
        purpose=purpose,
        sample_ids=sample_ids,
        params_hash=bg.request_hash(f"{server}.{tool}", {"query": query}),
    )
    try:
        response = client.call(server, tool, {"query": query})
        elapsed = int((time.perf_counter() - started) * 1000)
        summary = summarize(response)
        text = summary["answer_head"]
        if any(hint in text for hint in ifind_module.QUOTA_EXHAUSTED_HINTS):
            guard.record(reservation, response_status="QUOTA_EXHAUSTED", elapsed_ms=elapsed)
            raise SystemExit("供应商提示配额耗尽，立即停止")
        guard.record(
            reservation,
            response_status="OK",
            elapsed_ms=elapsed,
            returned_rows=summary["rows"],
            returned_cells=sum(max(len(row), 1) for row in summary["parsed_rows"]),
            payload_hash=summary["payload_hash"],
            storage_status="SUMMARY_ONLY",
        )
        summary.pop("parsed_rows")
        summary.update({"query": query, "tool": tool, "purpose": purpose, "sample_ids": list(sample_ids)})
        return summary
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.perf_counter() - started) * 1000)
        guard.record(
            reservation,
            response_status="ERROR",
            provider_error_code=type(exc).__name__,
            elapsed_ms=elapsed,
        )
        return {"query": query, "tool": tool, "purpose": purpose, "error": bg.redact(str(exc))[:300]}


def capability(client, guard, batch: Path) -> dict:
    result = {"servers": {}, "budget": guard.summary()}
    for server in ("stock", "index"):
        for step in ("initialize", "notifications/initialized"):
            reservation = guard.reserve(tool_name=step, endpoint=f"{server}/{step}", purpose="CAPABILITY")
            guard.record(reservation, response_status="OK")
        try:
            client._ensure_session(server)
        except Exception as exc:  # noqa: BLE001
            result["servers"][server] = {"error": bg.redact(str(exc))[:200]}
            continue
        reservation = guard.reserve(tool_name="tools/list", endpoint=f"{server}/tools/list", purpose="CAPABILITY")
        try:
            started = time.perf_counter()
            _response, body = client._post(
                server, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            )
            tools = ((body or {}).get("result") or {}).get("tools") or []
            guard.record(
                reservation,
                response_status="OK",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                returned_rows=len(tools),
            )
            result["servers"][server] = [
                {"name": item.get("name"), "description": (item.get("description") or "")[:300]} for item in tools
            ]
        except Exception as exc:  # noqa: BLE001
            guard.record(reservation, response_status="ERROR", provider_error_code=type(exc).__name__)
            result["servers"][server] = {"error": bg.redact(str(exc))[:200]}
    result["budget"] = guard.summary()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="iFinD 100 次请求内真实样本探针")
    parser.add_argument("--phase", choices=("capability", "g9", "g9-followup", "g9-metrics", "g3", "g3-date-check"), required=True)
    parser.add_argument("--batch")
    args = parser.parse_args()

    batch = load_batch(args.batch) if args.batch else new_batch()
    guard = bg.RequestBudgetGuard(batch / "request_budget_ledger.jsonl", allowed_tools=ALLOWED_TOOLS)

    settings = get_settings()
    token_present = bool((settings.ifind_auth_token or "").strip())
    if not token_present:
        print(json.dumps({"status": "NO_TOKEN"}, ensure_ascii=False))
        return 1

    ifind_module.record_call = lambda *a, **k: None  # type: ignore[assignment]
    client = ifind_module.IfindClient(settings)

    if args.phase == "capability":
        payload = capability(client, guard, batch)
        (batch / "capability_summary.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps({"phase": "capability", "servers": {k: (len(v) if isinstance(v, list) else v) for k, v in payload["servers"].items()}, "budget": payload["budget"]}, ensure_ascii=False))
        return 0

    if args.phase == "g9-metrics":
        parsed: list[list[dict]] = []
        for query, tool, sample_ids, server in G9_METRICS:
            reservation = guard.reserve(
                tool_name=tool,
                endpoint=f"{server}/{tool}",
                purpose="G9",
                sample_ids=sample_ids,
                params_hash=bg.request_hash(f"{server}.{tool}", {"query": query}),
            )
            started = time.perf_counter()
            try:
                response = client.call(server, tool, {"query": query})
                summary = summarize(response)
                guard.record(
                    reservation,
                    response_status="OK",
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    returned_rows=summary["rows"],
                    returned_cells=sum(max(len(row), 1) for row in summary["parsed_rows"]),
                    payload_hash=summary["payload_hash"],
                    storage_status="SUMMARY_ONLY",
                )
                parsed.append(summary["parsed_rows"])
            except Exception as exc:  # noqa: BLE001
                guard.record(
                    reservation,
                    response_status="ERROR",
                    provider_error_code=type(exc).__name__,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
                parsed.append([])
        metrics = compute_price_metrics(*parsed) if len(parsed) == 3 else {"error": "missing_series"}
        metrics["budget"] = guard.summary()
        (batch / "g9_metrics.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps({"phase": "g9-metrics", "metrics": metrics, "batch": str(batch)}, ensure_ascii=False))
        return 0

    if args.phase == "g9":
        queries = G9_QUERIES
    elif args.phase == "g9-followup":
        queries = G9_FOLLOWUP
    elif args.phase == "g3-date-check":
        queries = G3_DATE_CHECK
    else:
        queries = G3_QUERIES
    results = []
    purpose = "G3" if args.phase.startswith("g3") else "G9"
    for item in queries:
        query, tool, sample_ids = item[0], item[1], item[2]
        server = item[3] if len(item) > 3 else "stock"
        if guard.used >= 90:
            results.append({"query": query, "status": "SKIPPED_BUDGET_GUARD"})
            break
        results.append(run_query(client, guard, batch, tool, query, purpose, sample_ids, server))

    name = f"{args.phase}_summary.json"
    (batch / name).write_text(
        json.dumps({"phase": args.phase, "results": results, "budget": guard.summary()}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"phase": args.phase, "queries": len(results), "errors": sum(1 for r in results if r.get("error")), "budget": guard.summary(), "batch": str(batch)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
