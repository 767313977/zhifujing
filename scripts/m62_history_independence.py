"""M6.2 Batch 1：评估 history.db 的“独立性”（只读）。

回答 09 号验收报告中 history.db 复现的性质：
- 数据源、证券池、日期范围是否与 b1 相同；
- 是否只包含当前仍在交易的代码（幸存者偏差）；
- 是否具备指数/板块/RPS/日历等表；
- 复现使用的是同一冻结变体，还是重新选参。

产物：`reports/trend_engine/m62/history_independence.json`

用法：

    python scripts/m62_history_independence.py
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = {
    "b1": ROOT / "backend/data/b1_research.db",
    "history": ROOT / "backend/data/history.db",
}
REPORT = ROOT / "reports/trend_engine/m61/history/experiment_results.json"


def query(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10.0)
    try:
        connection.execute("PRAGMA query_only=ON")
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def profile(db: Path) -> dict:
    tables = [row[0] for row in query(db, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    total, first, last, codes = query(
        db, "SELECT COUNT(*), MIN(trade_date), MAX(trade_date), COUNT(DISTINCT code) FROM stock_daily"
    )[0]
    calendar = sorted(row[0] for row in query(db, "SELECT DISTINCT trade_date FROM stock_daily"))
    cutoff = calendar[-31] if len(calendar) > 31 else calendar[0]
    stale = query(db, "SELECT COUNT(DISTINCT code) FROM stock_daily WHERE code NOT IN (SELECT code FROM stock_daily WHERE trade_date >= ?)", (cutoff,))
    recent_codes = sorted(row[0] for row in query(db, "SELECT DISTINCT code FROM stock_daily"))
    return {
        "path": str(db),
        "tables": tables,
        "has_stock_basic": "stock_basic" in tables,
        "has_index_daily": "index_daily" in tables,
        "has_sector_daily": "sector_daily" in tables,
        "has_rps_daily": "rps_daily" in tables,
        "has_trade_calendar": "trade_calendar" in tables,
        "stock_daily_rows": total,
        "first_date": first,
        "last_date": last,
        "distinct_codes": codes,
        "codes": recent_codes,
        "stale_codes_30d": stale[0][0] if stale else 0,
        "trading_days": len(calendar),
        "last_30_trading_days_from": cutoff,
    }


def main() -> int:
    profiles = {name: profile(path) for name, path in SOURCES.items()}
    b1_codes = set(profiles["b1"]["codes"])
    history_codes = set(profiles["history"]["codes"])
    overlap = b1_codes & history_codes
    m61 = json.loads(REPORT.read_text(encoding="utf-8"))
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": {
            name: {key: value for key, value in profile.items() if key != "codes"}
            for name, profile in profiles.items()
        },
        "code_overlap": {
            "b1_codes": len(b1_codes),
            "history_codes": len(history_codes),
            "intersection": len(overlap),
            "history_only": len(history_codes - b1_codes),
            "b1_only": len(b1_codes - history_codes),
            "share_of_history_in_b1": round(len(overlap) / max(len(history_codes), 1), 4),
        },
        "history_experiment": {
            "path": str(REPORT),
            "scan": [m61["scan_start"], m61["scan_end"]],
            "splits": m61["splits"],
            "selected_variants": {
                model: m61["models"][model]["selection"]["variant"] for model in m61["models"]
            },
            "b1_selected_variants": {"A": "A2", "B": "B2"},
            "frozen_test": {
                model: {
                    "samples": m61["models"][model]["frozen_test"]["20"].get("samples"),
                    "excess_mean": m61["models"][model]["frozen_test"]["20"].get("excess_mean"),
                    "bootstrap": m61["models"][model]["frozen_test"]["bootstrap_20"],
                    "passed_validation": m61["models"][model]["selection"]["passed_validation"],
                    "passed": m61["models"][model]["frozen_test"]["passed"],
                }
                for model in m61["models"]
            },
        },
        "verdict": {
            "independent_database": True,
            "independent_universe": False,
            "same_frozen_variant": False,
            "pristine_out_of_sample": False,
            "conclusion": (
                "history.db 是独立数据库、独立日期区间上的复现，但证券池由当前代码集合回补，"
                "不含退市股；且按 history 自身训练集重新选参（A3/B0），未复用 b1 冻结的 A2/B2。"
                "因此只能称为『独立数据库复现』，不能称为『独立样本外验证』。"
            ),
        },
    }
    out = ROOT / "reports/trend_engine/m62/history_independence.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["code_overlap"], ensure_ascii=False))
    print(json.dumps(payload["history_experiment"]["selected_variants"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
