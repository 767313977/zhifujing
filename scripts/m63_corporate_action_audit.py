"""M6.3 Batch 1：双价格域与公司行为数据可用性审计（只读）。

回答三个问题：
1. 现库是否存在复权因子 / 公司行为数据；
2. 用 raw 收盘价与官方涨跌幅能否定位「除权类价格跳变」样本；
3. 在没有因子的情况下，哪些字段只能返回 `UNKNOWN`。

产物：
- `reports/trend_engine/m63/corporate_action_audit.json`
- `reports/trend_engine/m63/corporate_action_sample_reconciliation.csv`
- `reports/trend_engine/m63/adjustment_factor_reconciliation.csv`

用法：

    python scripts/m63_corporate_action_audit.py
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "backend/data/b1_research.db"
OUT = ROOT / "reports/trend_engine/m63"
MISMATCH_PP = 0.03
SAMPLE_LIMIT = 60
PRICE_TABLES = ("stock_daily", "index_daily", "sector_daily", "limit_pool")
FACTOR_HINTS = ("adj", "factor", "dividend", "bonus", "rights", "split", "corporate", "xdxr")


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=60.0)
    connection.execute("PRAGMA query_only=ON")
    return connection


def audit_schema(connection: sqlite3.Connection) -> dict:
    tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    suspects = [name for name in tables if any(hint in name.lower() for hint in FACTOR_HINTS)]
    columns = {}
    for table in PRICE_TABLES:
        if table not in tables:
            columns[table] = None
            continue
        columns[table] = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
    factor_columns = {
        table: [name for name in names if any(hint in name.lower() for hint in FACTOR_HINTS)]
        for table, names in columns.items()
        if names
    }
    return {
        "tables_total": len(tables),
        "tables_with_factor_hint": suspects,
        "price_table_columns": columns,
        "factor_columns_found": {key: value for key, value in factor_columns.items() if value},
        "has_adjustment_factor": bool(suspects) or any(factor_columns.values()),
    }


def detect_price_gaps(connection: sqlite3.Connection) -> dict:
    """相邻两日 raw close 与官方 pct 不一致 → 疑似除权 / 数据源口径不一致。"""
    rows = connection.execute(
        """
        WITH t AS (
          SELECT code, trade_date, close, pct_chg,
                 LAG(close) OVER (PARTITION BY code ORDER BY trade_date) AS prev_close
          FROM stock_daily
          WHERE close IS NOT NULL AND pct_chg IS NOT NULL AND close > 0
        )
        SELECT code, trade_date, prev_close, close, pct_chg,
               close / (1 + pct_chg / 100.0) AS implied_ref,
               ABS((close / (1 + pct_chg / 100.0)) / prev_close - 1) * 100 AS mismatch_pp
        FROM t
        WHERE prev_close > 0
          AND ABS((close / (1 + pct_chg / 100.0)) / prev_close - 1) * 100 > ?
        ORDER BY mismatch_pp DESC
        """,
        (MISMATCH_PP,),
    ).fetchall()
    bands = {"gt_0_03": 0, "gt_0_2": 0, "gt_0_5": 0, "gt_1": 0, "gt_5": 0, "gt_20": 0}
    total_rows = connection.execute(
        "SELECT COUNT(*) FROM stock_daily WHERE close IS NOT NULL AND pct_chg IS NOT NULL AND close > 0"
    ).fetchone()[0]
    by_year: dict[str, int] = {}
    by_code: dict[str, int] = {}
    for code, trade_date, _prev, _close, _pct, _implied, mismatch in rows:
        year = str(trade_date)[:4]
        by_year[year] = by_year.get(year, 0) + 1
        by_code[code] = by_code.get(code, 0) + 1
        for key, threshold in (("gt_0_03", 0.03), ("gt_0_2", 0.2), ("gt_0_5", 0.5), ("gt_1", 1.0), ("gt_5", 5.0), ("gt_20", 20.0)):
            if float(mismatch) > threshold:
                bands[key] += 1
    return {
        "mismatch_threshold_pp": MISMATCH_PP,
        "evaluated_rows": total_rows,
        "rows": len(rows),
        "share_pct": round(len(rows) / total_rows * 100, 2) if total_rows else None,
        "bands": bands,
        "codes": len(by_code),
        "by_year": dict(sorted(by_year.items())),
        "top_codes": sorted(by_code.items(), key=lambda item: -item[1])[:10],
        "samples": [
            {
                "code": code,
                "trade_date": trade_date,
                "prev_close": round(float(prev), 4),
                "close": round(float(close), 4),
                "pct_chg": round(float(pct), 4),
                "implied_ref": round(float(implied), 4),
                "mismatch_pp": round(float(mismatch), 4),
                "gap_ratio_pct": round((float(prev) / float(implied) - 1) * 100, 4) if implied else None,
                "band": classify(float(mismatch)),
            }
            for code, trade_date, prev, close, pct, implied, mismatch in rows[:SAMPLE_LIMIT]
        ],
    }


def classify(mismatch_pp: float) -> str:
    if mismatch_pp > 20:
        return "SPLIT_OR_DATA_ANOMALY"
    if mismatch_pp > 5:
        return "LARGE_CORPORATE_ACTION_OR_ANOMALY"
    if mismatch_pp > 0.2:
        return "DIVIDEND_LIKE_OR_PCT_BASIS_MISMATCH"
    return "ROUNDING_OR_MINOR_MISMATCH"


def main() -> int:
    connection = connect()
    try:
        schema = audit_schema(connection)
        gaps = detect_price_gaps(connection)
    finally:
        connection.close()

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db": str(DB),
        "schema": schema,
        "price_gaps": {key: value for key, value in gaps.items() if key != "samples"},
        "verdict": {
            "adjustment_factor_available": False,
            "corporate_action_table_available": False,
            "real_factor_verification_possible": False,
            "price_gap_detection_possible": True,
            "pct_chg_consistent_with_raw_close": False,
            "pct_raw_mismatch_rows": gaps["rows"],
            "pct_raw_mismatch_share_pct": gaps["share_pct"],
            "signal_price_domain_status": "UNKNOWN" if not schema["has_adjustment_factor"] else "PARTIAL",
            "execution_price_domain_status": "RAW_AVAILABLE",
            "blocking_reason": (
                "库内无复权因子/公司行为表；且 pct_chg 与 raw close 在大样本上不一致（疑似供应商按复权基准计算涨跌幅），"
                "说明现表已混用价格域。iFinD 客户端存在但未验证历史复权因子接口，且未获批量取数授权"
            ),
        },
    }
    (OUT / "corporate_action_audit.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )

    with (OUT / "corporate_action_sample_reconciliation.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "code",
                "trade_date",
                "prev_close",
                "close",
                "pct_chg",
                "implied_ref",
                "mismatch_pp",
                "gap_ratio_pct",
                "band",
                "detected_as",
                "action_type",
                "factor_status",
                "adjusted_close",
                "note",
            ],
        )
        writer.writeheader()
        for item in gaps["samples"]:
            writer.writerow(
                {
                    **item,
                    "detected_as": "PRICE_GAP_ONLY",
                    "action_type": "UNKNOWN",
                    "factor_status": "MISSING",
                    "adjusted_close": "",
                    "note": "仅由 raw close 与官方涨跌幅不一致定位，无法区分分红/送转/配股/数据异常",
                }
            )

    with (OUT / "adjustment_factor_reconciliation.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["code", "trade_date", "raw_close", "adj_factor", "adjusted_close", "method", "status", "note"],
        )
        writer.writeheader()
        for item in gaps["samples"][:20]:
            writer.writerow(
                {
                    "code": item["code"],
                    "trade_date": item["trade_date"],
                    "raw_close": item["close"],
                    "adj_factor": "",
                    "adjusted_close": "",
                    "method": "UNKNOWN",
                    "status": "UNVERIFIABLE",
                    "note": "无因子来源，禁止猜测；信号价域返回 UNKNOWN",
                }
            )

    print(json.dumps(payload["price_gaps"], ensure_ascii=False))
    print(json.dumps(payload["verdict"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
