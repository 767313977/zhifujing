"""M6.3 Batch 5：breadth 离线物化（CSV + sandbox SQLite）。

规则严格沿用 M6.2 `06-breadth-calculation-spec.md`：
- 有效样本：close 非空、amount>0、pct_chg 非空；
- MA20/60：按该股实际交易行滚动，含当日，要求满窗口；
- 新高/新低：20/60 日窗口（含当日）的收盘极值；
- 涨停/跌停：按板块前缀的近似阈值（ST 无法识别）。

产物：
- `reports/trend_engine/m63/breadth_full_coverage.csv`
- `reports/trend_engine/m63/breadth_quality_report.json`
- sandbox 表 `breadth_v1`（`backend/data/m63_sandbox/m63_derived.db`）

用法：

    python scripts/m63_breadth_materialize.py
"""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
SCRIPTS = ROOT / "scripts"
for path in (str(BACKEND), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import m62_market_data_audit as audit62  # noqa: E402

DB = ROOT / "backend/data/b1_research.db"
SANDBOX = ROOT / "backend/data/m63_sandbox/m63_derived.db"
OUT = ROOT / "reports/trend_engine/m63"
SAMPLE_DAYS = ("2025-12-31", "2026-06-30", "2026-09-24")

SQL = """
WITH base AS (
  SELECT trade_date, code, close, pct_chg, amount,
         AVG(close) OVER w20 AS ma20,
         AVG(close) OVER w60 AS ma60,
         MAX(close) OVER w20 AS high20,
         MIN(close) OVER w20 AS low20,
         MAX(close) OVER w60 AS high60,
         MIN(close) OVER w60 AS low60,
         COUNT(*) OVER w20 AS n20,
         COUNT(*) OVER w60 AS n60
  FROM stock_daily
  WHERE close IS NOT NULL
  WINDOW w20 AS (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
         w60 AS (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW)
)
SELECT trade_date,
  SUM(CASE WHEN amount > 0 AND pct_chg IS NOT NULL THEN 1 ELSE 0 END) AS universe_count,
  SUM(CASE WHEN amount > 0 AND pct_chg > 0 THEN 1 ELSE 0 END) AS up_count,
  SUM(CASE WHEN amount > 0 AND pct_chg < 0 THEN 1 ELSE 0 END) AS down_count,
  SUM(CASE WHEN amount > 0 AND pct_chg = 0 THEN 1 ELSE 0 END) AS flat_count,
  SUM(CASE WHEN n20 = 20 AND amount > 0 AND pct_chg IS NOT NULL THEN 1 ELSE 0 END) AS ma20_denominator,
  SUM(CASE WHEN n20 = 20 AND amount > 0 AND pct_chg IS NOT NULL AND close > ma20 THEN 1 ELSE 0 END) AS above_ma20,
  SUM(CASE WHEN n60 = 60 AND amount > 0 AND pct_chg IS NOT NULL THEN 1 ELSE 0 END) AS ma60_denominator,
  SUM(CASE WHEN n60 = 60 AND amount > 0 AND pct_chg IS NOT NULL AND close > ma60 THEN 1 ELSE 0 END) AS above_ma60,
  SUM(CASE WHEN n20 = 20 AND amount > 0 AND pct_chg IS NOT NULL AND close >= high20 THEN 1 ELSE 0 END) AS high20_count,
  SUM(CASE WHEN n20 = 20 AND amount > 0 AND pct_chg IS NOT NULL AND close <= low20 THEN 1 ELSE 0 END) AS low20_count,
  SUM(CASE WHEN n60 = 60 AND amount > 0 AND pct_chg IS NOT NULL AND close >= high60 THEN 1 ELSE 0 END) AS high60_count,
  SUM(CASE WHEN n60 = 60 AND amount > 0 AND pct_chg IS NOT NULL AND close <= low60 THEN 1 ELSE 0 END) AS low60_count,
  SUM(CASE WHEN amount > 0 AND pct_chg IS NOT NULL AND pct_chg >=
        CASE WHEN substr(code,1,3) IN ('300','301','688') THEN 19.8
             WHEN substr(code,1,1) IN ('4','8') OR substr(code,1,3) = '920' THEN 29.8
             ELSE 9.8 END THEN 1 ELSE 0 END) AS limit_up_approx,
  SUM(CASE WHEN amount > 0 AND pct_chg IS NOT NULL AND pct_chg <= -
        CASE WHEN substr(code,1,3) IN ('300','301','688') THEN 19.8
             WHEN substr(code,1,1) IN ('4','8') OR substr(code,1,3) = '920' THEN 29.8
             ELSE 9.8 END THEN 1 ELSE 0 END) AS limit_down_approx
FROM base
GROUP BY trade_date
ORDER BY trade_date
"""

COLUMNS = (
    "trade_date",
    "universe_count",
    "up_count",
    "down_count",
    "flat_count",
    "ma20_denominator",
    "above_ma20",
    "ma60_denominator",
    "above_ma60",
    "high20_count",
    "low20_count",
    "high60_count",
    "low60_count",
    "limit_up_approx",
    "limit_down_approx",
)


def breadth_python_full(connection: sqlite3.Connection, day: str) -> dict:
    """独立实现：用全部可用历史（最多 500 个交易日热身），避免长期停牌股分母偏小。"""
    row = connection.execute(
        "SELECT trade_date FROM (SELECT DISTINCT trade_date FROM stock_daily WHERE trade_date <= ?) "
        "ORDER BY trade_date DESC LIMIT 1 OFFSET 500",
        (day,),
    ).fetchone()
    start = row[0] if row else connection.execute(
        "SELECT MIN(trade_date) FROM stock_daily WHERE trade_date <= ?", (day,)
    ).fetchone()[0]
    series: dict[str, list[tuple[str, float, float | None, float | None]]] = {}
    for code, trade_date, close, pct_chg, amount in connection.execute(
        "SELECT code, trade_date, close, pct_chg, amount FROM stock_daily "
        "WHERE trade_date >= ? AND trade_date <= ? AND close IS NOT NULL ORDER BY code, trade_date",
        (start, day),
    ):
        series.setdefault(code, []).append((trade_date, float(close), pct_chg, amount))
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
                sum(item[1] for item in window20) / len(window20) if len(window20) == 20 else None,
                sum(item[1] for item in window60) / len(window60) if len(window60) == 60 else None,
                max(item[1] for item in window20),
                min(item[1] for item in window20),
                max(item[1] for item in window60),
                min(item[1] for item in window60),
                len(window20),
                len(window60),
            )
        )
    return audit62.aggregate(rows)


def connect_ro() -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=60.0)
    connection.execute("PRAGMA query_only=ON")
    return connection


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="M6.3 breadth 离线物化")
    parser.add_argument("--reuse-sandbox", action="store_true", help="重建 sandbox 中的 breadth_v1")
    args = parser.parse_args()
    source = connect_ro()
    try:
        rows = [dict(zip(COLUMNS, row)) for row in source.execute(SQL)]
        double_impl = []
        for day in SAMPLE_DAYS:
            sql_day = [row for row in rows if row["trade_date"] == day]
            python_day = breadth_python_full(source, day)
            if not sql_day:
                double_impl.append({"trade_date": day, "match": False, "reason": "NO_SQL_ROW"})
                continue
            sql_value = sql_day[0]
            mapping = {
                "universe": sql_value["universe_count"],
                "up": sql_value["up_count"],
                "down": sql_value["down_count"],
                "flat": sql_value["flat_count"],
                "ma20_denominator": sql_value["ma20_denominator"],
                "above_ma20": sql_value["above_ma20"],
                "ma60_denominator": sql_value["ma60_denominator"],
                "above_ma60": sql_value["above_ma60"],
                "high20_denominator": sql_value["ma20_denominator"],
                "high20": sql_value["high20_count"],
                "low20": sql_value["low20_count"],
                "high60_denominator": sql_value["ma60_denominator"],
                "high60": sql_value["high60_count"],
                "low60": sql_value["low60_count"],
            }
            mismatch = {
                key: {"sql": value, "python": python_day.get(key)}
                for key, value in mapping.items()
                if python_day.get(key) != value
            }
            double_impl.append({"trade_date": day, "match": not mismatch, "mismatch": mismatch})
        limit_pool = {
            day: dict(
                source.execute(
                    "SELECT pool_type, COUNT(*) FROM limit_pool WHERE trade_date = ? GROUP BY pool_type", (day,)
                ).fetchall()
            )
            for day in SAMPLE_DAYS
        }
        stock_days = [
            row[0] for row in source.execute("SELECT DISTINCT trade_date FROM stock_daily ORDER BY trade_date")
        ]
    finally:
        source.close()

    with (OUT / "breadth_full_coverage.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS))
        writer.writeheader()
        writer.writerows(rows)

    if not SANDBOX.exists():
        raise SystemExit(f"sandbox 不存在，先运行 RPS 脚本：{SANDBOX}")
    sandbox = sqlite3.connect(SANDBOX)
    try:
        exists = sandbox.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='breadth_v1'"
        ).fetchone()
        if exists:
            if not args.reuse_sandbox:
                raise SystemExit("breadth_v1 已存在，拒绝覆盖（如需重建请加 --reuse-sandbox）")
            sandbox.execute("DELETE FROM breadth_v1")
        else:
            sandbox.execute(
                """
                CREATE TABLE breadth_v1 (
                  trade_date TEXT PRIMARY KEY,
                  universe_count INTEGER,
                  up_count INTEGER,
                  down_count INTEGER,
                  flat_count INTEGER,
                  ma20_denominator INTEGER,
                  above_ma20 INTEGER,
                  ma60_denominator INTEGER,
                  above_ma60 INTEGER,
                  high20_count INTEGER,
                  low20_count INTEGER,
                  high60_count INTEGER,
                  low60_count INTEGER,
                  limit_up_approx INTEGER,
                  limit_down_approx INTEGER,
                  source_version TEXT NOT NULL,
                  quality_status TEXT NOT NULL
                )
                """
            )
        sandbox.executemany(
            f"INSERT INTO breadth_v1 ({','.join(COLUMNS)}, source_version, quality_status) "
            f"VALUES ({','.join(['?'] * (len(COLUMNS) + 2))})",
            [
                (*[row[column] for column in COLUMNS], "breadth_v1_m63", "APPROX_LIMIT_ST_UNKNOWN")
                for row in rows
            ],
        )
        sandbox.commit()
        stored = sandbox.execute("SELECT COUNT(*) FROM breadth_v1").fetchone()[0]
    finally:
        sandbox.close()

    computed_days = {row["trade_date"] for row in rows}
    missing_days = [day for day in stock_days if day not in computed_days]
    quality = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db": str(DB),
        "sandbox": str(SANDBOX),
        "rows": len(rows),
        "stored_rows": stored,
        "stock_trading_days": len(stock_days),
        "missing_days": missing_days,
        "first_date": rows[0]["trade_date"] if rows else None,
        "last_date": rows[-1]["trade_date"] if rows else None,
        "coverage_pct": round(len(rows) / max(len(stock_days), 1) * 100, 2),
        "universe_min": min(row["universe_count"] for row in rows) if rows else None,
        "universe_max": max(row["universe_count"] for row in rows) if rows else None,
        "zero_universe_days": [row["trade_date"] for row in rows if row["universe_count"] == 0],
        "sum_check_violations": [
            row["trade_date"]
            for row in rows
            if row["up_count"] + row["down_count"] + row["flat_count"] != row["universe_count"]
        ],
        "double_implementation": double_impl,
        "limit_pool_cross_check": [
            {
                "trade_date": day,
                "approx_up": next(row["limit_up_approx"] for row in rows if row["trade_date"] == day),
                "pool_up": limit_pool[day].get("up"),
                "approx_down": next(row["limit_down_approx"] for row in rows if row["trade_date"] == day),
                "pool_down": limit_pool[day].get("down"),
            }
            for day in SAMPLE_DAYS
        ],
        "rules_version": "m62_breadth_spec_v1",
        "write_scope": "sandbox only; b1_research.db untouched",
    }
    (OUT / "breadth_quality_report.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "rows": quality["rows"],
                "coverage_pct": quality["coverage_pct"],
                "double_ok": all(item["match"] for item in double_impl),
                "sum_violations": len(quality["sum_check_violations"]),
                "missing_days": len(missing_days),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
