"""把历史 RPS 从研究库导入生产等价库（逐字段保守合并，P0.9-06）。

背景：P0.7 的导入条件写成 `rps60 IS NOT NULL`，于是「20 日有值、60 日为空」的
12,430 行被整体跳过（其中 10,747 行有可用的 `ret20/rps20`）。本脚本改为
**四个新增字段任一非空**即进入候选集，并对四个字段**逐列** `COALESCE`。

规则（与 P0.8 报告 §3.7 一致）：
- 目标缺行 → 新增（旧列一并从来源带入；来源就是同一份历史计算结果）
- 目标有行、新字段为空、来源有值 → 补齐
- 目标有行、来源值一致 → 幂等跳过
- 目标有行、两边都有值但不同 → **记冲突，不覆盖**
- 来源为空 → 不覆盖目标已有值（`COALESCE` 天然满足）
- 旧版字段（ret50/120/250、rps50/120/250）**永不修改**
- 四列全空的行不导入（停牌 / 窗口不足的缺失语义必须保留）
- 不把 NULL 当成「零收益」：这里没有任何 NULL→0 的转换

用法：
    # 只统计（默认，不写库）
    python scripts/import_rps_history.py --source data/b1_research.db --target data/prod_copy.db

    # 执行（先备份；目标必须已迁移出四个新列）
    python scripts/import_rps_history.py --source data/b1_research.db --target data/prod_copy.db \
        --apply --backup data/prod_copy_before_rps.db --report data/import_rps.json

    # 目标里还没有新列时，先跑：
    python scripts/migrate_db.py --db data/prod_copy.db --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_DB = ROOT / "backend" / "data" / "fupan.db"

NEW_FIELDS = ("ret20", "ret60", "rps20", "rps60")
OLD_FIELDS = ("ret50", "ret120", "ret250", "rps50", "rps120", "rps250")
#: 数值冲突容差：RPS 与涨幅都是两位小数，0.011 避免浮点尾差被当成冲突
TOLERANCE = 0.011
ANY_NEW_NOT_NULL = "(" + " OR ".join(f"s.{name} IS NOT NULL" for name in NEW_FIELDS) + ")"
MATCH = "t.trade_date = s.trade_date AND t.code = s.code"


def _is_main_db(path: Path) -> bool:
    return os.path.normcase(str(path.resolve())) == os.path.normcase(str(MAIN_DB.resolve()))


def _backup(src: Path, dst: Path) -> None:
    if dst.exists():
        raise SystemExit(f"备份目标已存在，拒绝覆盖：{dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _columns(con: sqlite3.Connection, schema: str, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA {schema}.table_info({table})")}


def _range(start: str | None, end: str | None) -> tuple[str, tuple]:
    clauses = ["1=1"]
    params: list[str] = []
    if start:
        clauses.append("s.trade_date >= ?")
        params.append(start)
    if end:
        clauses.append("s.trade_date <= ?")
        params.append(end)
    return " AND ".join(clauses), tuple(params)


def _summary(con: sqlite3.Connection, start: str | None, end: str | None) -> dict:
    """逐日明细 + 合计。字段顺序固定，便于对照 P0.8 的 CSV。"""
    where, params = _range(start, end)
    sql = (
        "SELECT s.trade_date, COUNT(*) AS source_rows,"
        " SUM(CASE WHEN t.code IS NULL THEN 1 ELSE 0 END) AS missing_keys,"
        + "".join(
            f" SUM(CASE WHEN t.code IS NOT NULL AND t.{name} IS NULL AND s.{name} IS NOT NULL"
            f" THEN 1 ELSE 0 END) AS fill_{name},"
            for name in NEW_FIELDS
        )
        + " SUM(CASE WHEN "
        + " OR ".join(
            f"(t.{name} IS NOT NULL AND s.{name} IS NOT NULL AND ABS(t.{name} - s.{name}) > {TOLERANCE})"
            for name in NEW_FIELDS
        )
        + " THEN 1 ELSE 0 END) AS conflicts"
        f" FROM src.rps_daily s LEFT JOIN main.rps_daily t ON {MATCH}"
        f" WHERE {where} AND {ANY_NEW_NOT_NULL} GROUP BY s.trade_date ORDER BY s.trade_date"
    )
    cursor = con.execute(sql, params)
    names = [desc[0] for desc in cursor.description]
    daily = [dict(zip(names, row)) for row in cursor.fetchall()]
    total = {name: 0 for name in names if name != "trade_date"}
    for row in daily:
        for name in total:
            total[name] += int(row[name] or 0)
    return {"daily": daily, "total": total}


def _apply(con: sqlite3.Connection, start: str | None, end: str | None) -> dict:
    """在**一个事务**里插入缺失行并逐列补值；异常整体回滚。"""
    where, params = _range(start, end)
    con.execute("BEGIN IMMEDIATE")
    try:
        inserted = con.execute(
            "INSERT INTO main.rps_daily"
            " (trade_date, code, ret120, ret250, rps120, rps250, ret50, rps50,"
            "  ret20, ret60, rps20, rps60)"
            " SELECT s.trade_date, s.code, s.ret120, s.ret250, s.rps120, s.rps250,"
            "        s.ret50, s.rps50, s.ret20, s.ret60, s.rps20, s.rps60"
            " FROM src.rps_daily s"
            f" WHERE {where} AND {ANY_NEW_NOT_NULL}"
            f"   AND NOT EXISTS (SELECT 1 FROM main.rps_daily t WHERE {MATCH})",
            params,
        ).rowcount
        updated = con.execute(
            "UPDATE main.rps_daily AS t SET"
            " ret20 = COALESCE(t.ret20, s.ret20),"
            " ret60 = COALESCE(t.ret60, s.ret60),"
            " rps20 = COALESCE(t.rps20, s.rps20),"
            " rps60 = COALESCE(t.rps60, s.rps60)"
            " FROM src.rps_daily AS s"
            f" WHERE {MATCH} AND {where} AND {ANY_NEW_NOT_NULL}",
            params,
        ).rowcount
        con.commit()
    except Exception:
        con.rollback()
        raise
    return {"inserted": inserted, "updated": updated}


def main() -> int:
    parser = argparse.ArgumentParser(description="历史 RPS 逐字段保守导入")
    parser.add_argument("--source", required=True, help="来源研究库（只读打开）")
    parser.add_argument("--target", required=True, help="目标库（生产等价副本）")
    parser.add_argument("--start", help="起始交易日（含）")
    parser.add_argument("--end", help="结束交易日（含）")
    parser.add_argument("--apply", action="store_true", help="执行导入（默认只统计）")
    parser.add_argument("--backup", help="执行前用在线备份 API 备份目标库到该路径")
    parser.add_argument("--report", help="把逐日对账写成 JSON")
    parser.add_argument("--allow-main-db", action="store_true", help="允许写主库（默认拒绝）")
    args = parser.parse_args()

    source = Path(args.source)
    target = Path(args.target)
    if not source.exists():
        raise SystemExit(f"来源库不存在：{source}")
    if not target.exists():
        raise SystemExit(f"目标库不存在：{target}")
    if source.resolve() == target.resolve():
        raise SystemExit("来源与目标不能是同一个库")
    if _is_main_db(target) and args.apply and not args.allow_main_db:
        raise SystemExit("拒绝写主库：请先用 --copy-from 建隔离副本，或显式加 --allow-main-db")

    con = sqlite3.connect(str(target), isolation_level=None, uri=True)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("ATTACH DATABASE ? AS src", (f"file:{source.as_posix()}?mode=ro",))
    try:
        missing = [name for name in NEW_FIELDS if name not in _columns(con, "main", "rps_daily")]
        if missing:
            raise SystemExit(
                "目标库缺少新列 " + "、".join(missing) + "：请先跑 "
                "`python scripts/migrate_db.py --db <目标库> --apply`"
            )
        before = _summary(con, args.start, args.end)
        report: dict = {"source": str(source), "target": str(target), "before": before}

        if not args.apply:
            print(json.dumps(before["total"], ensure_ascii=False, indent=2))
            print("（未写库；加 --apply 执行）")
            if args.report:
                Path(args.report).write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            return 0

        if args.backup:
            _backup(target, Path(args.backup))
            print(f"[backup] {args.backup}")
        report["applied"] = _apply(con, args.start, args.end)
        after = _summary(con, args.start, args.end)
        report["after"] = after
        report["target_rows"] = con.execute("SELECT COUNT(*) FROM main.rps_daily").fetchone()[0]
        report["zero_new_fields"] = con.execute(
            "SELECT COUNT(*) FROM main.rps_daily WHERE ret20 IS NULL AND ret60 IS NULL"
            " AND rps20 IS NULL AND rps60 IS NULL"
        ).fetchone()[0]
        print(json.dumps(report["applied"], ensure_ascii=False))
        print(json.dumps(after["total"], ensure_ascii=False, indent=2))
        print(f"目标总行数：{report['target_rows']}")
        if args.report:
            Path(args.report).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return 0
    finally:
        con.execute("DETACH DATABASE src")
        con.close()


if __name__ == "__main__":
    sys.exit(main())
