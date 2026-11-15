"""M6.2 数据目录盘点（只读）：表结构、行数、日期范围、PIT 相关列。

输出 `reports/trend_engine/m62/data_catalog.json`，供 Batch 2/3 的契约与缺口分析引用。

用法：

    python scripts/m62_catalog_data.py
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATABASES = {
    "b1": ROOT / "backend/data/b1_research.db",
    "history": ROOT / "backend/data/history.db",
    "fupan": ROOT / "backend/data/fupan.db",
}
PIT_COLUMN_HINTS = (
    "created_at",
    "updated_at",
    "ingested_at",
    "available_at",
    "effective_date",
    "source",
    "version",
    "is_final",
    "revision",
)
DATE_COLUMN_HINTS = ("trade_date", "date", "effective_date", "available_at")


def open_ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15.0)
    connection.execute("PRAGMA query_only=ON")
    return connection


def columns(connection: sqlite3.Connection, table: str) -> list[dict]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [{"name": row[1], "type": row[2], "notnull": bool(row[3]), "pk": bool(row[5])} for row in rows]


def table_profile(connection: sqlite3.Connection, table: str) -> dict:
    cols = columns(connection, table)
    names = [item["name"] for item in cols]
    count = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    date_column = next((name for name in DATE_COLUMN_HINTS if name in names), None)
    date_range = None
    distinct_dates = None
    if date_column:
        row = connection.execute(
            f'SELECT MIN("{date_column}"), MAX("{date_column}"), COUNT(DISTINCT "{date_column}") FROM "{table}"'
        ).fetchone()
        date_range = {"column": date_column, "min": str(row[0]) if row[0] is not None else None,
                      "max": str(row[1]) if row[1] is not None else None, "distinct": row[2]}
        distinct_dates = row[2]
    pit_columns = [name for name in names if any(hint in name.lower() for hint in PIT_COLUMN_HINTS)]
    return {
        "table": table,
        "rows": count,
        "columns": cols,
        "date_range": date_range,
        "distinct_dates": distinct_dates,
        "pit_columns": pit_columns,
        "has_ingest_time": any(name in pit_columns for name in ("created_at", "ingested_at", "updated_at")),
    }


def database_profile(path: Path) -> dict:
    connection = open_ro(path)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        profiles = [table_profile(connection, table) for table in tables]
        indexes = [
            {"table": row[0], "name": row[1], "sql": row[2]}
            for row in connection.execute(
                "SELECT tbl_name, name, sql FROM sqlite_master WHERE type='index' ORDER BY tbl_name, name"
            )
        ]
    finally:
        connection.close()
    return {"path": str(path), "tables": profiles, "indexes": indexes}


def main() -> int:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "databases": {name: database_profile(path) for name, path in DATABASES.items()},
    }
    out = ROOT / "reports/trend_engine/m62/data_catalog.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, profile in payload["databases"].items():
        print(name, len(profile["tables"]), "tables")
        for table in profile["tables"]:
            print(
                f"  {table['table']:<28} rows={table['rows']:<10} "
                f"range={table['date_range']['min'] if table['date_range'] else None}~"
                f"{table['date_range']['max'] if table['date_range'] else None} "
                f"pit={table['pit_columns']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
