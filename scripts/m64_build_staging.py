"""M6.4 Batch 5：iFinD staging 数据库与元数据落地。

只读原始库；在 `backend/data/m64_staging/<batch>/m64_ifind_staging.db` 新建表并写入本批次真实样本。
不写任何原始数据库。

用法：

    python scripts/m64_build_staging.py
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.trend_engine import ifind_guard as guard  # noqa: E402

STAGING_ROOT = ROOT / "backend/data/m64_staging"
REPORT_DIR = ROOT / "reports/trend_engine/m64"
SOURCE_DB = ROOT / "backend/data/b1_research.db"
BATCH_ID = sorted(STAGING_ROOT.glob("batch_*"))[-1].name if STAGING_ROOT.exists() else ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def load(batch: Path, name: str):
    path = batch / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    if not BATCH_ID:
        raise SystemExit("staging 批次不存在，先运行探针与样本脚本")
    batch = STAGING_ROOT / BATCH_ID
    db_path = batch / "m64_ifind_staging.db"
    if db_path.exists():
        raise SystemExit(f"拒绝覆盖已存在 staging 库：{db_path}")
    source_before = sha256_file(SOURCE_DB)
    ingested_at = datetime.now(timezone.utc).isoformat()
    index_rows = load(batch, "g1_index_rows.json") or []
    events = load(batch, "g9_events.json") or {}
    sector_rows = load(batch, "g2_sector_rows.json") or []
    ledger = load(batch, "sample_call_ledger.json") or {}
    probe = load(batch, "probe_ledger.json") or {}

    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE provider_manifest (
          provider TEXT, transport TEXT, sdk TEXT, functions TEXT,
          batch_id TEXT, created_at TEXT, PRIMARY KEY (provider, batch_id)
        );
        CREATE TABLE raw_payload_manifest (
          batch_id TEXT, file TEXT, sha256 TEXT, bytes INTEGER, PRIMARY KEY (batch_id, file)
        );
        CREATE TABLE index_daily (
          batch_id TEXT, code TEXT, trade_date TEXT,
          open REAL, high REAL, low REAL, close REAL, pct_chg REAL, amount REAL,
          source TEXT, source_function TEXT, source_version TEXT,
          available_at TEXT, ingested_at TEXT, payload_hash TEXT, quality_status TEXT,
          PRIMARY KEY (batch_id, code, trade_date)
        );
        CREATE TABLE sector_daily_raw (
          batch_id TEXT, query TEXT, code TEXT, name TEXT, trade_date TEXT,
          metric TEXT, value TEXT, source_function TEXT, ingested_at TEXT, payload_hash TEXT,
          PRIMARY KEY (batch_id, query, code, trade_date, metric)
        );
        CREATE TABLE price_factor (
          batch_id TEXT, code TEXT, trade_date TEXT, factor REAL,
          factor_type TEXT, formula TEXT, source_function TEXT,
          available_at TEXT, ingested_at TEXT, quality_status TEXT,
          PRIMARY KEY (batch_id, code, trade_date)
        );
        CREATE TABLE corporate_action (
          batch_id TEXT, code TEXT, event_type TEXT,
          record_date TEXT, ex_date TEXT, pay_date TEXT, share_date TEXT,
          cash_per_share REAL, shares_per_share REAL, announcement_at TEXT,
          source_function TEXT, available_at TEXT, ingested_at TEXT, quality_status TEXT,
          PRIMARY KEY (batch_id, code, record_date, ex_date)
        );
        CREATE TABLE ingestion_run (
          batch_id TEXT PRIMARY KEY, calls INTEGER, cells INTEGER, status TEXT,
          started_at TEXT, finished_at TEXT, notes TEXT
        );
        CREATE TABLE ingestion_error (
          batch_id TEXT, dataset TEXT, error_code TEXT, detail TEXT, created_at TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO provider_manifest VALUES (?,?,?,?,?,?)",
        (
            "iFinD",
            "HTTP MCP (existing client)",
            "iFinDPy NOT_INSTALLED",
            "get_stock_performance,get_stock_events,index_data,sector_data",
            BATCH_ID,
            ingested_at,
        ),
    )
    for file in sorted(batch.glob("*.json")):
        text = file.read_text(encoding="utf-8")
        connection.execute(
            "INSERT INTO raw_payload_manifest VALUES (?,?,?,?)",
            (BATCH_ID, file.name, guard.payload_hash(text), len(text.encode("utf-8"))),
        )

    for row in index_rows:
        connection.execute(
            "INSERT OR REPLACE INTO index_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                BATCH_ID,
                row["code"],
                row["trade_date"],
                row.get("open"),
                row.get("high"),
                row.get("low"),
                row.get("close"),
                row.get("pct_chg"),
                row.get("amount"),
                "iFinD",
                "index_data",
                "ifind_mcp_20261115",
                None,
                ingested_at,
                guard.payload_hash(row),
                "OK" if row.get("close") is not None else "PARTIAL",
            ),
        )

    for code, rows in events.items():
        for row in rows:
            record_date = _date(row.get("股权登记日"))
            ex_date = _date(row.get("除权除息日"))
            pay_date = _date(row.get("派息日"))
            if not (record_date or ex_date):
                continue
            connection.execute(
                "INSERT OR REPLACE INTO corporate_action VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    BATCH_ID,
                    code,
                    "UNKNOWN",
                    record_date,
                    ex_date,
                    pay_date,
                    None,
                    None,
                    None,
                    None,
                    "get_stock_events",
                    None,
                    ingested_at,
                    "UNKNOWN_MISSING_AMOUNT_AND_ANNOUNCEMENT",
                ),
            )

    for row in sector_rows:
        code = str(row.get("证券代码") or "")
        name = str(row.get("证券简称") or "")
        day = None
        for key, value in row.items():
            if key == "日期":
                day = str(value)
            if key not in ("证券代码", "证券简称", "日期", "query"):
                connection.execute(
                    "INSERT OR REPLACE INTO sector_daily_raw VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        BATCH_ID,
                        row.get("query", ""),
                        code,
                        name,
                        day,
                        key,
                        str(value),
                        "sector_data",
                        ingested_at,
                        guard.payload_hash([code, day, key, value]),
                    ),
                )

    calls = len(probe.get("calls") or []) + len(ledger.get("calls") or [])
    cells = int(probe.get("cells_estimated") or 0) + int(ledger.get("cells") or 0)
    connection.execute(
        "INSERT INTO ingestion_run VALUES (?,?,?,?,?,?,?)",
        (BATCH_ID, calls, cells, "PARTIAL", ingested_at, ingested_at, "G9 字段不足；G1 样本通过；G3 无接口"),
    )
    errors = [
        ("price_factor", "FACTOR_FIELD_NOT_RETURNED", "NL 查询未返回复权因子列；收盘价为复权值但未标注前/后复权"),
        ("corporate_action", "ANNOUNCEMENT_TIME_MISSING", "get_stock_events 仅返回登记/除权/派息日与进度"),
        ("corporate_action", "CASH_PER_SHARE_MISSING", "无每股派现/送转比例字段，无法做账户会计"),
        ("sector_membership", "NO_MEMBERSHIP_TOOL", "当前账号工具集无历史板块成分接口"),
    ]
    connection.executemany(
        "INSERT INTO ingestion_error VALUES (?,?,?,?,?)",
        [(BATCH_ID, dataset, code, detail, ingested_at) for dataset, code, detail in errors],
    )
    connection.commit()
    counts = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "index_daily",
            "sector_daily_raw",
            "price_factor",
            "corporate_action",
            "ingestion_error",
        )
    }
    connection.close()
    source_after = sha256_file(SOURCE_DB)
    staging_sha = sha256_file(db_path)
    manifest = {
        "generated_at": ingested_at,
        "batch_id": BATCH_ID,
        "staging_db": str(db_path),
        "staging_sha256": staging_sha,
        "source_db_sha256_before": source_before,
        "source_db_sha256_after": source_after,
        "source_db_unchanged": source_before == source_after,
        "counts": counts,
        "calls": calls,
        "cells": cells,
        "budget_cells": 50_000,
        "budget_used_pct": round(cells / 50_000 * 100, 2),
        "status": "PARTIAL",
    }
    (REPORT_DIR / "ingestion_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    usage = {
        "batch_id": BATCH_ID,
        "sdk": "NOT_INSTALLED",
        "probe_calls": len(probe.get("calls") or []),
        "sample_calls": len(ledger.get("calls") or []),
        "cells_estimated": cells,
        "budget_cells": 50_000,
        "budget_used_pct": manifest["budget_used_pct"],
        "usage_stats": "USAGE_UNVERIFIED",
        "note": "无 SDK，未能调用官方用量统计；以本地调用次数与单元格估算为准",
    }
    (REPORT_DIR / "api_usage_before_after.json").write_text(
        json.dumps(usage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"counts": counts, "source_unchanged": manifest["source_db_unchanged"], "staging": str(db_path)}, ensure_ascii=False))
    return 0


def _date(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text or None


if __name__ == "__main__":
    raise SystemExit(main())
