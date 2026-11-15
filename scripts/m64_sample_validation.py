"""M6.4 Batch 2/3/4：最小真实样本（复权/公司行为、指数、板块）。

只做少量 iFinD 调用，全部落 staging；不写任何原始数据库。
输出：staging 原始 JSON/CSV + `g9_sample_validation.json`、`g1_validation.json`、`g2_g3_validation.json`。

用法：

    python scripts/m64_sample_validation.py
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import sys
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
DB = ROOT / "backend/data/b1_research.db"
BUDGET_CELLS = 50_000
INDEXES = ("000001.SH", "000688.SH", "000852.SH", "399001.SZ", "399006.SZ", "899050.BJ")
SECTOR_QUERIES = ("半导体 行业板块", "白酒 概念板块")


def parse_number(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "None", "nan"}:
        return None
    scale = 1.0
    if "亿" in text:
        scale = 1e8
    elif "万" in text:
        scale = 1e4
    text = re.sub(r"[^0-9.\-]", "", text)
    if not text:
        return None
    try:
        return float(text) * scale
    except ValueError:
        return None


def parse_date(value) -> str | None:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    return None


def trading_days() -> list[str]:
    connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        return [row[0] for row in connection.execute("SELECT DISTINCT trade_date FROM stock_daily ORDER BY trade_date")]
    finally:
        connection.close()


def main() -> int:
    settings = get_settings()
    ifind_module.record_call = lambda *args, **kwargs: None  # type: ignore[assignment]
    client = ifind_module.IfindClient(settings)
    batches = sorted(STAGING_ROOT.glob("batch_*"))
    if not batches:
        raise SystemExit("先运行 m64_ifind_probe.py")
    batch_dir = batches[-1]

    ledger = {"calls": [], "cells": 0}

    def nl(server: str, tool: str, query: str) -> list[dict]:
        response = client.call(server, tool, {"query": query})
        inner = ifind_module._extract(response)[1]
        answer = inner.get("answer") or ""
        rows = [row for table in ifind_module.parse_tables(answer) for row in table]
        ledger["calls"].append({"server": server, "tool": tool, "query": query, "rows": len(rows)})
        ledger["cells"] += sum(max(len(row), 1) for row in rows)
        return rows

    # ---------- G9：复权价格 / 因子 / 公司行为 ----------
    factor_rows = nl(
        "stock",
        "get_stock_performance",
        "600519 从2025-07-01到2025-07-20 每个交易日的前复权收盘价、后复权收盘价、复权因子",
    )
    event_queries = [
        ("600519", "600519 2025年 分红方案、股权登记日、除权除息日、派息日"),
        ("002594", "002594 2025年 送股、转增、拆股、股权登记日、除权除息日"),
        ("300750", "300750 2025年 配股、股权登记日、除权除息日"),
    ]
    events = {code: nl("stock", "get_stock_events", query) for code, query in event_queries}

    # ---------- G1：六大指数 ----------
    days = trading_days()
    research_end = days[-1]
    connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        index_last = connection.execute("SELECT MAX(trade_date) FROM index_daily").fetchone()[0]
    finally:
        connection.close()
    missing = [day for day in days if day > index_last]
    window_start = days[max(0, days.index(missing[0]) - 5)] if missing else "2026-09-15"
    index_rows: list[dict] = []
    index_validation = {}
    for code in INDEXES:
        rows = nl(
            "index",
            "index_data",
            f"{code} 从{window_start}到{research_end} 每个交易日的开盘价、最高价、最低价、收盘价、涨跌幅、成交额",
        )
        normalized = []
        for row in rows:
            day = parse_date(row.get("日期"))
            if day is None or day < window_start or day > research_end:
                continue
            normalized.append(
                {
                    "code": code,
                    "trade_date": day,
                    "open": parse_number(row.get("开盘价（单位：元）")),
                    "high": parse_number(row.get("最高价（单位：元）")),
                    "low": parse_number(row.get("最低价（单位：元）")),
                    "close": parse_number(row.get("收盘价（单位：元）") or row.get("收盘价")),
                    "pct_chg": parse_number(row.get("涨跌幅（单位：%）")),
                    "amount": parse_number(row.get("成交金额（单位：元）") or row.get("成交额（单位：元）")),
                }
            )
        index_rows.extend(normalized)
        got = {row["trade_date"] for row in normalized}
        expected = [day for day in days if window_start <= day <= research_end]
        missing_days = [day for day in expected if day not in got]
        index_validation[code] = {
            "expected_days": len(expected),
            "actual_days": len([row for row in normalized if row["close"] is not None]),
            "missing_days": missing_days,
            "duplicates": len(normalized) - len({(row["trade_date"]) for row in normalized}),
            "status": "PASS" if not missing_days else "FAIL",
        }

    # ---------- G2：板块样本 ----------
    sector_rows: list[dict] = []
    for query in SECTOR_QUERIES:
        rows = nl("index", "sector_data", f"{query} 从{window_start}到{research_end} 每个交易日的收盘价、涨跌幅")
        for row in rows:
            sector_rows.append({"query": query, **row})

    # ---------- 落盘 ----------
    def dump(name: str, payload) -> None:
        (batch_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )

    dump("g9_factor_rows.json", factor_rows)
    dump("g9_events.json", events)
    dump("g1_index_rows.json", index_rows)
    dump("g2_sector_rows.json", sector_rows)

    with (REPORT_DIR / "index_daily_staging.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(index_rows[0].keys()))
        writer.writeheader()
        writer.writerows(index_rows)
    with (batch_dir / "sector_daily_staging.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = sorted({key for row in sector_rows for key in row}) or ["query"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(sector_rows)

    g9 = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "factor_query_rows": len(factor_rows),
        "factor_columns": sorted({key for row in factor_rows for key in row}),
        "factor_available": any("复权" in key or "因子" in key for row in factor_rows for key in row),
        "events": {
            code: {"rows": len(rows), "columns": sorted({key for row in rows for key in row})}
            for code, rows in events.items()
        },
        "announcement_time_available": any(
            "公告" in key for rows in events.values() for row in rows for key in row
        ),
        "cash_per_share_available": any(
            "派" in key or "分红" in key for rows in events.values() for row in rows for key in row
        ),
        "verdict": "UNVERIFIED",
        "reason": "需根据字段判断：若仅返回日期与进度、没有因子/每股派现/公告时间，则 G9 不能 PASS",
    }
    if not g9["factor_available"]:
        g9["verdict"] = "FAIL"
        g9["reason"] = "接口未返回复权因子或前/后复权价格字段"
    elif not g9["announcement_time_available"]:
        g9["verdict"] = "PARTIAL"
        g9["reason"] = "有复权字段但无公告时间，PIT 不能 PASS"

    g1 = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_start": window_start,
        "research_end": research_end,
        "missing_window": missing,
        "indexes": index_validation,
        "all_pass": all(item["status"] == "PASS" for item in index_validation.values()),
    }
    g2 = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "queries": list(SECTOR_QUERIES),
        "rows": len(sector_rows),
        "columns": sorted({key for row in sector_rows for key in row}),
        "historical_membership_supported": False,
        "verdict": "PARTIAL",
        "reason": "板块日线可取样；历史成分无对应工具，G3 无法通过",
    }
    dump("g9_sample_validation.json", g9)
    for name, payload in (("g9_sample_validation.json", g9), ("g1_validation.json", g1), ("g2_g3_validation.json", g2)):
        (REPORT_DIR / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )
    dump("sample_call_ledger.json", ledger)
    print(
        json.dumps(
            {
                "g9": g9["verdict"],
                "g9_factor_columns": g9["factor_columns"],
                "g1_pass": g1["all_pass"],
                "g1_status": {k: v["status"] for k, v in index_validation.items()},
                "sector_rows": len(sector_rows),
                "calls": len(ledger["calls"]),
                "cells": ledger["cells"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
