"""M6.2 Batch 2：PIT 相关字段盘点（只读）。

统计 ST 名称历史、名称变更、证券末日分布、快照时间戳、RPS 混版本日，
输出 `reports/trend_engine/m62/pit_inventory.json`。

用法：

    python scripts/m62_pit_inventory.py
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "backend/data/b1_research.db"


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=15.0)
    connection.execute("PRAGMA query_only=ON")
    return connection


def main() -> int:
    connection = connect()
    try:
        one = lambda sql, params=(): connection.execute(sql, params).fetchone()  # noqa: E731
        all_rows = lambda sql, params=(): connection.execute(sql, params).fetchall()  # noqa: E731

        st_rows, st_codes = one("SELECT COUNT(*), COUNT(DISTINCT code) FROM stock_daily WHERE name LIKE '%ST%'")
        renamed = one(
            "SELECT COUNT(*) FROM (SELECT code FROM stock_daily GROUP BY code HAVING COUNT(DISTINCT name) > 1)"
        )[0]
        last_dates = one("SELECT MIN(d), MAX(d) FROM (SELECT code, MAX(trade_date) d FROM stock_daily GROUP BY code)")
        latest = one("SELECT MAX(trade_date) FROM stock_daily")[0]
        stale_30 = one(
            "SELECT COUNT(*) FROM (SELECT code, MAX(trade_date) d FROM stock_daily GROUP BY code) WHERE d < ?",
            (sorted(row[0] for row in all_rows("SELECT DISTINCT trade_date FROM stock_daily"))[-31],),
        )[0]
        basic = one("SELECT MIN(asof), MAX(asof), COUNT(*) FROM stock_basic")
        universe = one("SELECT MIN(updated_at), MAX(updated_at), COUNT(*) FROM stock_universe")
        rps_null = all_rows(
            "SELECT trade_date, COUNT(*) FROM rps_daily WHERE ret20 IS NULL OR ret60 IS NULL GROUP BY trade_date ORDER BY trade_date"
        )
        rps_days = one("SELECT COUNT(DISTINCT trade_date), MIN(trade_date), MAX(trade_date) FROM rps_daily")
        rps_tail = all_rows(
            "SELECT trade_date, COUNT(*), SUM(ret20 IS NULL) FROM rps_daily GROUP BY trade_date ORDER BY trade_date DESC LIMIT 5"
        )
        sector_tail = all_rows("SELECT MAX(trade_date), MIN(trade_date), COUNT(DISTINCT trade_date) FROM sector_daily")
        index_tail = all_rows("SELECT MAX(trade_date), MIN(trade_date), COUNT(DISTINCT trade_date) FROM index_daily")
        member = all_rows("SELECT MIN(trade_date), MAX(trade_date), COUNT(DISTINCT trade_date) FROM sector_member")
        calendar_tail = all_rows("SELECT MAX(trade_date), COUNT(*) FROM trade_calendar")
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "db": str(DB),
            "stock_daily": {
                "latest_date": str(latest),
                "st_rows": st_rows,
                "st_codes": st_codes,
                "codes_with_name_change": renamed,
                "earliest_last_date": str(last_dates[0]),
                "latest_last_date": str(last_dates[1]),
                "codes_stale_more_than_30_trading_days": stale_30,
            },
            "stock_basic": {"asof_min": basic[0], "asof_max": basic[1], "rows": basic[2]},
            "stock_universe": {"updated_at_min": universe[0], "updated_at_max": universe[1], "rows": universe[2]},
            "rps_daily": {
                "days": rps_days[0],
                "first": rps_days[1],
                "last": rps_days[2],
                "days_with_null_ret": [{"trade_date": row[0], "rows": row[1]} for row in rps_null],
                "tail": [{"trade_date": row[0], "rows": row[1], "ret20_null": row[2]} for row in rps_tail],
            },
            "sector_daily": {"first": sector_tail[0][1], "last": sector_tail[0][0], "days": sector_tail[0][2]},
            "index_daily": {"first": index_tail[0][1], "last": index_tail[0][0], "days": index_tail[0][2]},
            "sector_member": {"first": member[0][0], "last": member[0][1], "days": member[0][2]},
            "trade_calendar": {"last": calendar_tail[0][0], "rows": calendar_tail[0][1]},
        }
    finally:
        connection.close()
    out = ROOT / "reports/trend_engine/m62/pit_inventory.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["stock_daily"], ensure_ascii=False))
    print(json.dumps(payload["rps_daily"]["tail"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
