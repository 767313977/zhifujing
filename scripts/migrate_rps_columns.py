"""`rps_daily` 的 B0 字段迁移 / 回滚 / 结构检查（只作用于显式指定的库）。

B0 新增 4 个可空列：`ret20`、`ret60`、`rps20`、`rps60`。旧列
`ret50/120/250`、`rps50/120/250` 的名称、含义、主键都不变。

用法：

    # 查看结构
    python scripts/migrate_rps_columns.py --db backend/data/b0_trial.db --status

    # 迁移（先在线备份）
    python scripts/migrate_rps_columns.py --db backend/data/b0_trial.db --apply \
        --backup backend/data/b0_trial_before_rps.db

    # 回滚（删除 4 个新列，保留旧列与全部旧数据）
    python scripts/migrate_rps_columns.py --db backend/data/b0_trial.db --rollback \
        --backup backend/data/b0_trial_before_rollback.db

安全约束：默认拒绝写 `backend/data/fupan.db`；确需写主库必须加 `--allow-main-db`。
SQLite 3.35+ 可直接 DROP COLUMN；本脚本仍保留「重建表」路径作为兜底。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_DB = (ROOT / "backend" / "data" / "fupan.db").resolve()

NEW_COLUMNS = ("ret20", "ret60", "rps20", "rps60")
OLD_COLUMNS = ("ret50", "ret120", "ret250", "rps50", "rps120", "rps250")

ORIGINAL_DDL = """
CREATE TABLE rps_daily_rollback (
    trade_date DATE NOT NULL,
    code VARCHAR(16) NOT NULL,
    ret50 FLOAT,
    ret120 FLOAT,
    ret250 FLOAT,
    rps50 FLOAT,
    rps120 FLOAT,
    rps250 FLOAT,
    PRIMARY KEY (trade_date, code)
)
"""


def _open(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"数据库不存在：{path}")
    return sqlite3.connect(str(path), timeout=15)


def _columns(con: sqlite3.Connection) -> list[str]:
    return [row[1] for row in con.execute("PRAGMA table_info(rps_daily)")]


def _backup(src: Path, dst: Path) -> None:
    if dst.exists():
        raise SystemExit(f"备份目标已存在，拒绝覆盖：{dst}")
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst_con = sqlite3.connect(str(dst))
    try:
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()
    print(f"已备份：{src} → {dst}")


def _table_exists(con: sqlite3.Connection) -> bool:
    return bool(
        con.execute(
            "select name from sqlite_master where type='table' and name='rps_daily'"
        ).fetchone()
    )


def apply(con: sqlite3.Connection) -> None:
    if not _table_exists(con):
        raise SystemExit("rps_daily 不存在")
    have = set(_columns(con))
    for column in NEW_COLUMNS:
        if column in have:
            print(f"已存在，跳过：{column}")
            continue
        con.execute(f"ALTER TABLE rps_daily ADD COLUMN {column} FLOAT")
        print(f"已添加：{column}")
    con.commit()


def rollback(con: sqlite3.Connection) -> None:
    if not _table_exists(con):
        raise SystemExit("rps_daily 不存在")
    have = set(_columns(con))
    missing = [c for c in NEW_COLUMNS if c not in have]
    if not missing and set(NEW_COLUMNS).issubset(have):
        # 优先直接 DROP COLUMN（SQLite >= 3.35），保留旧数据与原表结构
        for column in NEW_COLUMNS:
            con.execute(f"ALTER TABLE rps_daily DROP COLUMN {column}")
            print(f"已删除：{column}")
        con.commit()
        return
    # 兜底：重建表（只保留旧列）
    con.execute("DROP TABLE IF EXISTS rps_daily_rollback")
    con.execute(ORIGINAL_DDL)
    cols = ", ".join(OLD_COLUMNS)
    con.execute(f"INSERT INTO rps_daily_rollback(trade_date, code, {cols}) SELECT trade_date, code, {cols} FROM rps_daily")
    con.execute("DROP TABLE rps_daily")
    con.execute("ALTER TABLE rps_daily_rollback RENAME TO rps_daily")
    con.commit()
    print("已用重建表方式回滚到旧 7 列结构")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--allow-main-db", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--backup", help="执行前用 SQLite 在线备份到该路径")
    args = parser.parse_args()

    path = Path(args.db).resolve()
    if os.path.normcase(str(path)) == os.path.normcase(str(MAIN_DB)) and not args.allow_main_db:
        raise SystemExit("拒绝改主库 backend/data/fupan.db；请用隔离副本，或显式加 --allow-main-db（不推荐）")
    if args.backup:
        _backup(path, Path(args.backup).resolve())
    con = _open(path)
    before = _columns(con)
    print(f"库：{path}")
    print(f"迁移前列：{before}")
    if args.status or not (args.apply or args.rollback):
        con.close()
        return 0
    if args.apply and args.rollback:
        raise SystemExit("--apply 与 --rollback 不能同时使用")
    if args.apply:
        apply(con)
    else:
        rollback(con)
    after = _columns(con)
    print(f"迁移后列：{after}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
