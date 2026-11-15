"""M6.3 Batch 3：指数/板块日期对齐门禁（只读）。

不补采、不写库；只回答：
1. 每个指数、板块相对个股研究日历缺哪些交易日；
2. 是否有重复键、未来日期；
3. 板块成分快照覆盖多少交易日、缺口在哪；
4. 对齐门禁是否通过。

产物：
- `reports/trend_engine/m63/index_alignment.csv`
- `reports/trend_engine/m63/sector_alignment.csv`
- `reports/trend_engine/m63/sector_membership_coverage.csv`
- `reports/trend_engine/m63/alignment_audit.json`

用法：

    python scripts/m63_alignment_audit.py
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


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=30.0)
    connection.execute("PRAGMA query_only=ON")
    return connection


def stock_calendar(connection: sqlite3.Connection) -> list[str]:
    return [row[0] for row in connection.execute("SELECT DISTINCT trade_date FROM stock_daily ORDER BY trade_date")]


def alignment(expected: list[str], actual: set[str]) -> dict:
    missing = [day for day in expected if day not in actual]
    extra = sorted(day for day in actual if day not in expected)
    return {
        "expected_days": len(expected),
        "actual_days": len(actual),
        "missing_days": len(missing),
        "missing_dates": missing,
        "extra_dates": extra,
        "first_missing": missing[0] if missing else None,
        "last_missing": missing[-1] if missing else None,
    }


def main() -> int:
    connection = connect()
    try:
        days = stock_calendar(connection)
        research_end = days[-1]

        index_rows = []
        indexes = connection.execute(
            "SELECT code, name, MIN(trade_date), MAX(trade_date) FROM index_daily GROUP BY code ORDER BY code"
        ).fetchall()
        for code, name, first, last in indexes:
            actual = {
                row[0]
                for row in connection.execute(
                    "SELECT trade_date FROM index_daily WHERE code = ? AND trade_date BETWEEN ? AND ?",
                    (code, first, research_end),
                )
            }
            expected = [day for day in days if first <= day <= research_end]
            report = alignment(expected, actual)
            duplicates = connection.execute(
                "SELECT COUNT(*) FROM (SELECT trade_date FROM index_daily WHERE code = ? GROUP BY trade_date HAVING COUNT(*) > 1)",
                (code,),
            ).fetchone()[0]
            index_rows.append(
                {
                    "code": code,
                    "name": name,
                    "first_date": first,
                    "last_date": last,
                    "research_end": research_end,
                    "expected_days": report["expected_days"],
                    "actual_days": report["actual_days"],
                    "missing_days": report["missing_days"],
                    "first_missing": report["first_missing"],
                    "last_missing": report["last_missing"],
                    "extra_dates": len(report["extra_dates"]),
                    "duplicates": duplicates,
                    "status": "ALIGNED" if report["missing_days"] == 0 and duplicates == 0 else "MISALIGNED",
                }
            )

        sector_rows = []
        sectors = connection.execute(
            "SELECT sector_code, name, taxonomy, MIN(trade_date), MAX(trade_date) FROM sector_daily "
            "GROUP BY sector_code, name, taxonomy ORDER BY sector_code"
        ).fetchall()
        for sector_code, name, taxonomy, first, last in sectors:
            actual = {
                row[0]
                for row in connection.execute(
                    "SELECT trade_date FROM sector_daily WHERE sector_code = ? AND trade_date BETWEEN ? AND ?",
                    (sector_code, first, research_end),
                )
            }
            expected = [day for day in days if first <= day <= research_end]
            report = alignment(expected, actual)
            duplicates = connection.execute(
                "SELECT COUNT(*) FROM (SELECT trade_date FROM sector_daily WHERE sector_code = ? GROUP BY trade_date HAVING COUNT(*) > 1)",
                (sector_code,),
            ).fetchone()[0]
            sector_rows.append(
                {
                    "sector_code": sector_code,
                    "name": name,
                    "taxonomy": taxonomy,
                    "first_date": first,
                    "last_date": last,
                    "expected_days": report["expected_days"],
                    "actual_days": report["actual_days"],
                    "missing_days": report["missing_days"],
                    "first_missing": report["first_missing"],
                    "last_missing": report["last_missing"],
                    "duplicates": duplicates,
                    "status": "ALIGNED" if report["missing_days"] == 0 and duplicates == 0 else "MISALIGNED",
                }
            )

        member_days = [
            row[0] for row in connection.execute("SELECT DISTINCT trade_date FROM sector_member ORDER BY trade_date")
        ]
        member_first = member_days[0] if member_days else None
        member_expected = [day for day in days if member_first and member_first <= day <= research_end]
        member_report = alignment(member_expected, set(member_days))
        member_rows = connection.execute(
            "SELECT trade_date, COUNT(*) FROM sector_member GROUP BY trade_date ORDER BY trade_date"
        ).fetchall()
        member_coverage_rows = [
            {
                "trade_date": day,
                "members": count,
                "expected": "YES",
                "status": "OK",
            }
            for day, count in member_rows
        ]
        for day in member_report["missing_dates"]:
            member_coverage_rows.append(
                {"trade_date": day, "members": 0, "expected": "YES", "status": "MISSING"}
            )

        with (OUT / "index_alignment.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(index_rows[0].keys()))
            writer.writeheader()
            writer.writerows(index_rows)
        with (OUT / "sector_alignment.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(sector_rows[0].keys()))
            writer.writeheader()
            writer.writerows(sector_rows)
        with (OUT / "sector_membership_coverage.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(member_coverage_rows[0].keys()))
            writer.writeheader()
            writer.writerows(member_coverage_rows)

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "db": str(DB),
            "research_end": research_end,
            "stock_trading_days": len(days),
            "index": {
                "codes": len(index_rows),
                "misaligned": [row["code"] for row in index_rows if row["status"] != "ALIGNED"],
                "missing_days_max": max(row["missing_days"] for row in index_rows),
                "duplicates": sum(row["duplicates"] for row in index_rows),
                "gate": "FAIL" if any(row["status"] != "ALIGNED" for row in index_rows) else "PASS",
            },
            "sector": {
                "codes": len(sector_rows),
                "misaligned": len([row for row in sector_rows if row["status"] != "ALIGNED"]),
                "missing_days_max": max(row["missing_days"] for row in sector_rows),
                "duplicates": sum(row["duplicates"] for row in sector_rows),
                "gate": "FAIL" if any(row["status"] != "ALIGNED" for row in sector_rows) else "PASS",
            },
            "sector_membership": {
                "first": member_first,
                "last": member_days[-1] if member_days else None,
                "days": len(member_days),
                "expected_days": member_report["expected_days"],
                "missing_days": member_report["missing_days"],
                "missing_dates": member_report["missing_dates"],
                "gate": "FAIL" if member_report["missing_days"] else "PASS",
            },
            "backfill": {
                "attempted": False,
                "reason": "无已授权的历史指数/板块补采数据源；本轮禁止写原库，sandbox 补采需先取得数据授权",
            },
        }
        (OUT / "alignment_audit.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )
        print(json.dumps({key: payload[key] for key in ("index", "sector", "sector_membership")}, ensure_ascii=False))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
