"""生产库只读审计（P1.1-04）。

只在**一致性备份副本**上运行：以 `mode=ro` 打开，只执行 SELECT / PRAGMA，
不建表、不补列、不写触发器、不 checkpoint。

输出（JSON 或文本）：
- 文件大小、`quick_check`、库身份（路径 + size + mtime）；
- 各主要表的行数、首末日期、交易日数；
- 按交易日历外连接的逐日覆盖矩阵（行情 / RPS / 板块日线 / 成分股）；
- RPS 新字段覆盖与孤儿日期（有 RPS、无当日行情）；
- manifest 状态分布（表不存在时明确标记）；
- `collect_log` 最近成功时间与最近失败；
- 代码 / 日期格式一致性抽样。

用法：
    python scripts/prod_readonly_audit.py --db /path/to/prod_copy.db \
        --start 2026-07-03 --end 2026-11-09 --json audit.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

TABLES = ("stock_daily", "rps_daily", "sector_daily", "sector_member")


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _table_stats(con: sqlite3.Connection, name: str) -> dict:
    if not _has_table(con, name):
        return {"exists": False}
    row = con.execute(
        f"SELECT COUNT(*), MIN(trade_date), MAX(trade_date), COUNT(DISTINCT trade_date) FROM {name}"
    ).fetchone()
    return {
        "exists": True,
        "rows": row[0],
        "min_date": row[1],
        "max_date": row[2],
        "trade_days": row[3],
    }


def _coverage(con: sqlite3.Connection, start: str | None, end: str | None) -> list[dict]:
    if not _has_table(con, "trade_calendar"):
        return []
    clauses = ["1=1"]
    params: list[str] = []
    if start:
        clauses.append("c.trade_date >= ?")
        params.append(start)
    if end:
        clauses.append("c.trade_date <= ?")
        params.append(end)
    where = " AND ".join(clauses)
    sql = (
        "SELECT c.trade_date,"
        " (SELECT COUNT(*) FROM stock_daily d WHERE d.trade_date=c.trade_date),"
        " (SELECT COUNT(*) FROM rps_daily r WHERE r.trade_date=c.trade_date),"
        " (SELECT COUNT(*) FROM rps_daily r WHERE r.trade_date=c.trade_date AND r.rps60 IS NOT NULL),"
        " (SELECT COUNT(DISTINCT sector_code) FROM sector_daily s WHERE s.trade_date=c.trade_date),"
        " (SELECT COUNT(*) FROM sector_member m WHERE m.trade_date=c.trade_date)"
        f" FROM trade_calendar c WHERE {where} ORDER BY c.trade_date"
    )
    try:
        rows = con.execute(sql, tuple(params)).fetchall()
    except sqlite3.OperationalError:
        # 老库没有 rps60 列：退化成不含 RPS60 的版本
        sql = sql.replace(
            " (SELECT COUNT(*) FROM rps_daily r WHERE r.trade_date=c.trade_date AND r.rps60 IS NOT NULL),",
            "",
        )
        rows = con.execute(sql, tuple(params)).fetchall()
        return [
            {
                "trade_date": row[0],
                "stock_rows": row[1],
                "rps_rows": row[2],
                "sector_boards": row[3],
                "member_rows": row[4],
            }
            for row in rows
        ]
    return [
        {
            "trade_date": row[0],
            "stock_rows": row[1],
            "rps_rows": row[2],
            "rps60_rows": row[3],
            "sector_boards": row[4],
            "member_rows": row[5],
        }
        for row in rows
    ]


def _rps_state(con: sqlite3.Connection) -> dict:
    stats = _table_stats(con, "rps_daily")
    if not stats["exists"]:
        return stats
    columns = {row[1] for row in con.execute("PRAGMA table_info(rps_daily)")}
    new_columns = [name for name in ("ret20", "ret60", "rps20", "rps60") if name in columns]
    result: dict = {"exists": True, "columns": len(columns), "new_columns": new_columns, **stats}
    if new_columns:
        nulls = {
            name: con.execute(f"SELECT COUNT(*) FROM rps_daily WHERE {name} IS NULL").fetchone()[0]
            for name in new_columns
        }
        result["null_counts"] = nulls
        result["all_new_null"] = con.execute(
            "SELECT COUNT(*) FROM rps_daily WHERE " + " AND ".join(f"{n} IS NULL" for n in new_columns)
        ).fetchone()[0]
    result["orphan_dates"] = [
        {"trade_date": row[0], "rps_rows": row[1]}
        for row in con.execute(
            "SELECT r.trade_date, COUNT(*) FROM rps_daily r"
            " WHERE NOT EXISTS (SELECT 1 FROM stock_daily d WHERE d.trade_date=r.trade_date)"
            " GROUP BY r.trade_date ORDER BY r.trade_date DESC LIMIT 20"
        )
    ]
    return result


def _manifest_state(con: sqlite3.Connection) -> dict:
    if not _has_table(con, "sector_member_backfill"):
        return {"exists": False}
    rows = con.execute(
        "SELECT status, COUNT(*), MIN(trade_date), MAX(trade_date) FROM sector_member_backfill GROUP BY status"
    ).fetchall()
    return {
        "exists": True,
        "status": [
            {"status": row[0], "boards": row[1], "min_date": row[2], "max_date": row[3]}
            for row in rows
        ],
    }


def _collect_state(con: sqlite3.Connection) -> dict:
    if not _has_table(con, "collect_log"):
        return {"exists": False}
    last_ok = con.execute(
        "SELECT task, MAX(created_at) FROM collect_log WHERE status IN ('ok','partial') GROUP BY task"
    ).fetchall()
    recent = con.execute(
        "SELECT created_at, trade_date, task, status, rows, substr(message, 1, 80)"
        " FROM collect_log WHERE status IN ('failed','skipped','empty')"
        " ORDER BY created_at DESC LIMIT 10"
    ).fetchall()
    return {
        "exists": True,
        "last_ok_by_task": [{"task": row[0], "last_ok": row[1]} for row in last_ok],
        "recent_non_ok": [
            {
                "created_at": row[0],
                "trade_date": row[1],
                "task": row[2],
                "status": row[3],
                "rows": row[4],
                "message": row[5],
            }
            for row in recent
        ],
    }


def _formats(con: sqlite3.Connection) -> dict:
    """代码 / 日期格式一致性抽样（只读）。"""
    out: dict = {}
    for table in ("stock_daily", "rps_daily", "sector_member"):
        if not _has_table(con, table):
            continue
        out[table] = {
            "code_lengths": [
                {"length": row[0], "rows": row[1]}
                for row in con.execute(
                    f"SELECT LENGTH(code), COUNT(*) FROM {table} GROUP BY LENGTH(code)"
                )
            ],
            "date_lengths": [
                {"length": row[0], "rows": row[1]}
                for row in con.execute(
                    f"SELECT LENGTH(trade_date), COUNT(*) FROM {table} GROUP BY LENGTH(trade_date)"
                )
            ],
        }
    return out


def audit(path: Path, start: str | None, end: str | None) -> dict:
    con = _connect(path)
    try:
        stat = path.stat()
        result: dict = {
            "db": str(path),
            "size_bytes": stat.st_size,
            "mtime": stat.st_mtime,
            "quick_check": con.execute("PRAGMA quick_check").fetchone()[0],
            "tables": {name: _table_stats(con, name) for name in TABLES},
            "rps": _rps_state(con),
            "manifest": _manifest_state(con),
            "collect_log": _collect_state(con),
            "formats": _formats(con),
            "coverage": _coverage(con, start, end),
        }
        return result
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="生产库只读审计（请在一致性备份副本上运行）")
    parser.add_argument("--db", required=True, help="备份副本路径（以 mode=ro 打开）")
    parser.add_argument("--start", help="覆盖矩阵起始日（含）")
    parser.add_argument("--end", help="覆盖矩阵结束日（含）")
    parser.add_argument("--json", help="写出 JSON")
    args = parser.parse_args()

    path = Path(args.db)
    if not path.exists():
        raise SystemExit(f"库不存在：{path}")
    result = audit(path, args.start, args.end)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json:
        Path(args.json).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
