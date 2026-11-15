"""M6.2 Batch 3：指数 / 板块 / 宽度 / RPS 只读审计与双实现核对。

产物：
- `reports/trend_engine/m62/market_data_audit.json`
- `reports/trend_engine/m62/sample_day_reconciliation.csv`

要点：
1. 指数、板块与个股交易日对齐情况、重复、断档；
2. 板块成分（60 天）与板块日线收益的一致性抽样；
3. breadth 的 SQL 窗口实现与纯 Python 实现，在抽样交易日逐项比对；
4. RPS 版本嫌疑日、重复、以及用原始收盘价重算 ret20 的横截面相关性。

用法：

    python scripts/m62_market_data_audit.py
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "backend/data/b1_research.db"
OUT = ROOT / "reports/trend_engine/m62"
SAMPLE_DAYS = ("2025-12-31", "2026-06-30", "2026-09-24")
MEMBER_SAMPLE_DAYS = ("2026-07-15", "2026-08-14", "2026-09-24")
RPS_PROBE_DAY = "2026-09-30"


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=30.0)
    connection.execute("PRAGMA query_only=ON")
    return connection


def trading_days(connection: sqlite3.Connection) -> list[str]:
    return [row[0] for row in connection.execute("SELECT DISTINCT trade_date FROM stock_daily ORDER BY trade_date")]


def limit_threshold(code: str) -> float:
    """按板块近似涨跌停阈值；ST 无法识别，属近似口径。"""
    if code.startswith(("300", "301", "688")):
        return 19.8
    if code.startswith(("4", "8", "920")):
        return 29.8
    return 9.8


def breadth_sql(connection: sqlite3.Connection, day: str) -> dict:
    start = connection.execute(
        "SELECT trade_date FROM (SELECT DISTINCT trade_date FROM stock_daily WHERE trade_date <= ?) "
        "ORDER BY trade_date DESC LIMIT 1 OFFSET 60",
        (day,),
    ).fetchone()[0]
    rows = connection.execute(
        """
        WITH base AS (
          SELECT code, trade_date, close, pct_chg, amount,
                 AVG(close) OVER w20 AS ma20,
                 AVG(close) OVER w60 AS ma60,
                 MAX(close) OVER w20 AS high20,
                 MIN(close) OVER w20 AS low20,
                 MAX(close) OVER w60 AS high60,
                 MIN(close) OVER w60 AS low60,
                 COUNT(*) OVER w20 AS n20,
                 COUNT(*) OVER w60 AS n60
          FROM stock_daily
          WHERE trade_date >= ? AND trade_date <= ? AND close IS NOT NULL
          WINDOW w20 AS (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
                 w60 AS (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW)
        )
        SELECT code, close, pct_chg, amount, ma20, ma60, high20, low20, high60, low60, n20, n60
        FROM base WHERE trade_date = ?
        """,
        (start, day, day),
    ).fetchall()
    return aggregate(rows)


def aggregate(rows) -> dict:
    universe = [
        row for row in rows if row[3] is not None and float(row[3]) > 0 and row[2] is not None and row[1] is not None
    ]
    up = sum(1 for row in universe if float(row[2]) > 0)
    down = sum(1 for row in universe if float(row[2]) < 0)
    flat = len(universe) - up - down
    ma20_rows = [row for row in universe if row[10] == 20]
    ma60_rows = [row for row in universe if row[11] == 60]
    high20_rows = [row for row in universe if row[10] == 20]
    high60_rows = [row for row in universe if row[11] == 60]
    limit_up = sum(1 for row in universe if float(row[2]) >= limit_threshold(row[0]))
    limit_down = sum(1 for row in universe if float(row[2]) <= -limit_threshold(row[0]))
    return {
        "universe": len(universe),
        "up": up,
        "down": down,
        "flat": flat,
        "above_ma20": sum(1 for row in ma20_rows if float(row[1]) > float(row[4])),
        "ma20_denominator": len(ma20_rows),
        "above_ma60": sum(1 for row in ma60_rows if float(row[1]) > float(row[5])),
        "ma60_denominator": len(ma60_rows),
        "high20": sum(1 for row in high20_rows if float(row[1]) >= float(row[6])),
        "low20": sum(1 for row in high20_rows if float(row[1]) <= float(row[7])),
        "high60": sum(1 for row in high60_rows if float(row[1]) >= float(row[8])),
        "low60": sum(1 for row in high60_rows if float(row[1]) <= float(row[9])),
        "high20_denominator": len(high20_rows),
        "high60_denominator": len(high60_rows),
        "limit_up_approx": limit_up,
        "limit_down_approx": limit_down,
    }


def breadth_python(connection: sqlite3.Connection, day: str) -> dict:
    start = connection.execute(
        "SELECT trade_date FROM (SELECT DISTINCT trade_date FROM stock_daily WHERE trade_date <= ?) "
        "ORDER BY trade_date DESC LIMIT 1 OFFSET 60",
        (day,),
    ).fetchone()[0]
    series: dict[str, list[tuple[str, float, float | None, float | None]]] = defaultdict(list)
    for code, trade_date, close, pct_chg, amount in connection.execute(
        "SELECT code, trade_date, close, pct_chg, amount FROM stock_daily "
        "WHERE trade_date >= ? AND trade_date <= ? AND close IS NOT NULL ORDER BY code, trade_date",
        (start, day),
    ):
        series[code].append((trade_date, float(close), pct_chg, amount))
    rows = []
    for code, items in series.items():
        if items[-1][0] != day:
            continue
        window20 = items[-20:]
        window60 = items[-60:]
        rows.append(
            (
                code,
                items[-1][1],
                items[-1][2],
                items[-1][3],
                float(np.mean([item[1] for item in window20])) if len(window20) == 20 else None,
                float(np.mean([item[1] for item in window60])) if len(window60) == 60 else None,
                max(item[1] for item in window20),
                min(item[1] for item in window20),
                max(item[1] for item in window60),
                min(item[1] for item in window60),
                len(window20),
                len(window60),
            )
        )
    return aggregate(rows)


def index_audit(connection: sqlite3.Connection, days: list[str]) -> dict:
    codes = connection.execute(
        "SELECT code, name, COUNT(*), MIN(trade_date), MAX(trade_date) FROM index_daily GROUP BY code ORDER BY code"
    ).fetchall()
    duplicates = connection.execute(
        "SELECT COUNT(*) FROM (SELECT code, trade_date FROM index_daily GROUP BY code, trade_date HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    last = connection.execute("SELECT MAX(trade_date) FROM index_daily").fetchone()[0]
    lag = len([day for day in days if day > last])
    missing = []
    for code, _name, _rows, first, last_code in codes:
        existing = {
            row[0]
            for row in connection.execute(
                "SELECT trade_date FROM index_daily WHERE code = ? AND trade_date BETWEEN ? AND ?", (code, first, last_code)
            )
        }
        expected = [day for day in days if first <= day <= last_code]
        gaps = [day for day in expected if day not in existing]
        missing.append({"code": code, "gaps": len(gaps), "first_gaps": gaps[:5]})
    return {
        "codes": [
            {"code": code, "name": name, "rows": rows, "first": first, "last": last}
            for code, name, rows, first, last in codes
        ],
        "duplicates": duplicates,
        "last_date": last,
        "lag_trading_days_vs_stock": lag,
        "missing_by_code": missing,
    }


def sector_audit(connection: sqlite3.Connection) -> dict:
    taxonomy = connection.execute("SELECT taxonomy, COUNT(DISTINCT sector_code) FROM sector_daily GROUP BY taxonomy").fetchall()
    duplicates = connection.execute(
        "SELECT COUNT(*) FROM (SELECT sector_code, trade_date FROM sector_daily GROUP BY sector_code, trade_date HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    per_day = connection.execute(
        "SELECT trade_date, COUNT(*) FROM sector_daily GROUP BY trade_date ORDER BY trade_date DESC LIMIT 3"
    ).fetchall()
    days = connection.execute("SELECT COUNT(DISTINCT trade_date) FROM sector_daily").fetchone()[0]
    consistency = []
    for day in MEMBER_SAMPLE_DAYS:
        rows = connection.execute(
            """
            SELECT m.sector_code, AVG(m.pct_chg), COUNT(*), d.pct_chg
            FROM sector_member m
            JOIN sector_daily d ON d.sector_code = m.sector_code AND d.trade_date = m.trade_date
            WHERE m.trade_date = ? AND m.pct_chg IS NOT NULL
            GROUP BY m.sector_code, d.pct_chg
            """,
            (day,),
        ).fetchall()
        diffs = [abs(float(row[1]) - float(row[3])) for row in rows if row[3] is not None]
        consistency.append(
            {
                "trade_date": day,
                "sectors": len(rows),
                "within_0_5pp": sum(1 for value in diffs if value <= 0.5),
                "within_1pp": sum(1 for value in diffs if value <= 1.0),
                "max_abs_diff": round(max(diffs), 3) if diffs else None,
                "median_abs_diff": round(float(np.median(diffs)), 3) if diffs else None,
            }
        )
    return {
        "taxonomy": [{"taxonomy": row[0], "sectors": row[1]} for row in taxonomy],
        "duplicates": duplicates,
        "days": days,
        "latest_days": [{"trade_date": row[0], "rows": row[1]} for row in per_day],
        "member_days": connection.execute("SELECT COUNT(DISTINCT trade_date) FROM sector_member").fetchone()[0],
        "member_range": connection.execute("SELECT MIN(trade_date), MAX(trade_date) FROM sector_member").fetchone(),
        "return_consistency": consistency,
    }


def rps_audit(connection: sqlite3.Connection) -> dict:
    duplicates = connection.execute(
        "SELECT COUNT(*) FROM (SELECT code, trade_date FROM rps_daily GROUP BY code, trade_date HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    null_days = connection.execute(
        "SELECT trade_date, COUNT(*), SUM(ret20 IS NULL), SUM(ret60 IS NULL) FROM rps_daily "
        "GROUP BY trade_date HAVING SUM(ret20 IS NULL) > 0 OR SUM(ret60 IS NULL) > 0 ORDER BY trade_date"
    ).fetchall()
    probe = recompute_ret(connection, RPS_PROBE_DAY)
    return {
        "days": connection.execute("SELECT COUNT(DISTINCT trade_date) FROM rps_daily").fetchone()[0],
        "duplicates": duplicates,
        "days_with_null_returns": [
            {"trade_date": row[0], "rows": row[1], "ret20_null": row[2], "ret60_null": row[3]} for row in null_days
        ],
        "recompute_probe": probe,
    }


def recompute_ret(connection: sqlite3.Connection, day: str) -> dict:
    days = [row[0] for row in connection.execute(
        "SELECT DISTINCT trade_date FROM stock_daily WHERE trade_date <= ? ORDER BY trade_date", (day,)
    )]
    if len(days) < 22:
        return {"status": "INSUFFICIENT_DATA"}
    base = days[-21]
    prices = {}
    for code, trade_date, close in connection.execute(
        "SELECT code, trade_date, close FROM stock_daily WHERE trade_date IN (?, ?) AND close IS NOT NULL", (base, day)
    ):
        prices.setdefault(code, {})[trade_date] = float(close)
    computed = {}
    for code, pair in prices.items():
        if base in pair and day in pair and pair[base] > 0:
            computed[code] = pair[day] / pair[base] - 1
    stored = {
        code: float(ret)
        for code, ret in connection.execute("SELECT code, ret20 FROM rps_daily WHERE trade_date = ? AND ret20 IS NOT NULL", (day,))
    }
    common = sorted(set(computed) & set(stored))
    if len(common) < 100:
        return {"status": "INSUFFICIENT_DATA", "common": len(common)}
    left = np.array([computed[code] for code in common])
    right = np.array([stored[code] for code in common])
    corr = float(np.corrcoef(left, right)[0, 1])
    diff_decimal = float(np.median(np.abs(left - right)))
    diff_percent = float(np.median(np.abs(left * 100 - right)))
    scale = "percent" if diff_percent < diff_decimal else "decimal"
    return {
        "status": "OK",
        "day": day,
        "common_codes": len(common),
        "pearson_corr": round(corr, 4),
        "unit": scale,
        "median_abs_diff": round(diff_percent if scale == "percent" else diff_decimal, 5),
        "median_abs_diff_other_unit": round(diff_decimal if scale == "percent" else diff_percent, 5),
        "computed_universe": len(computed),
        "stored_universe": len(stored),
    }


def main() -> int:
    connection = connect()
    try:
        days = trading_days(connection)
        index = index_audit(connection, days)
        sector = sector_audit(connection)
        rps = rps_audit(connection)

        breadth = []
        for day in SAMPLE_DAYS:
            sql_result = breadth_sql(connection, day)
            py_result = breadth_python(connection, day)
            limit_pool = dict(
                connection.execute(
                    "SELECT pool_type, COUNT(*) FROM limit_pool WHERE trade_date = ? GROUP BY pool_type", (day,)
                ).fetchall()
            )
            keys = [key for key in sql_result if key != "universe"]
            mismatch = {key: {"sql": sql_result[key], "python": py_result[key]} for key in keys if sql_result[key] != py_result[key]}
            breadth.append(
                {
                    "trade_date": day,
                    "sql": sql_result,
                    "python": py_result,
                    "limit_pool_counts": limit_pool,
                    "match": not mismatch,
                    "mismatch": mismatch,
                }
            )

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "db": str(DB),
            "stock_trading_days": len(days),
            "stock_last_date": days[-1],
            "index": index,
            "sector": sector,
            "rps": rps,
            "breadth_samples": breadth,
        }
        (OUT / "market_data_audit.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )

        rows = []
        for sample in breadth:
            for key, value in sample["sql"].items():
                rows.append(
                    {
                        "dataset": "breadth",
                        "trade_date": sample["trade_date"],
                        "metric": key,
                        "sql_value": value,
                        "python_value": sample["python"][key],
                        "match": sample["python"][key] == value,
                        "note": "SQL 窗口 vs 纯 Python 双实现",
                    }
                )
        for item in sector["return_consistency"]:
            rows.append(
                {
                    "dataset": "sector_return",
                    "trade_date": item["trade_date"],
                    "metric": "sector_vs_member_equal_weight",
                    "sql_value": item["sectors"],
                    "python_value": item["within_0_5pp"],
                    "match": item["within_0_5pp"] == item["sectors"],
                    "note": f"max_abs_diff={item['max_abs_diff']} median={item['median_abs_diff']}",
                }
            )
        probe = rps["recompute_probe"]
        rows.append(
            {
                "dataset": "rps",
                "trade_date": probe.get("day"),
                "metric": "ret20_recompute_corr",
                "sql_value": probe.get("pearson_corr"),
                "python_value": probe.get("median_abs_diff"),
                "match": probe.get("status") == "OK" and (probe.get("pearson_corr") or 0) > 0.95,
                "note": (
                    f"common={probe.get('common_codes')} universe={probe.get('computed_universe')} "
                    f"unit={probe.get('unit')} corr={probe.get('pearson_corr')}"
                ),
            }
        )
        with (OUT / "sample_day_reconciliation.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(json.dumps({sample["trade_date"]: sample["match"] for sample in breadth}, ensure_ascii=False))
        print(json.dumps(probe, ensure_ascii=False))
        print(json.dumps({"index_lag": index["lag_trading_days_vs_stock"], "sector_days": sector["days"]}, ensure_ascii=False))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
