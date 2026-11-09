"""显式数据库 Schema 迁移（P0.9-08）。

背景：应用启动以前会 `create_all` + `ALTER TABLE ... ADD COLUMN`，于是「启动候选
服务看一眼配置」= 一次隐式迁移。现在启动只做**只读兼容性检查**（`FUPAN_SCHEMA_MODE`
默认 `verify`），真正的结构变更必须走这个命令。

用法：
    # 只检查（只读打开，不会写库）
    python scripts/migrate_db.py --db backend/data/fupan.db --status

    # 迁移隔离库（先备份）
    python scripts/migrate_db.py --db backend/data/research_copy.db --apply \
        --backup backend/data/research_copy_before_schema.db

    # 主库需要显式加 --allow-main-db
    python scripts/migrate_db.py --db backend/data/fupan.db --apply --allow-main-db

安全边界：
- 只做「建缺表 / 补可空列 / 补索引」，不删列、不改类型、不装触发器
  （触发器由 `scripts/install_data_revision.py` 单独安装）；
- `--status` 用 `mode=ro` 打开，绝不在只读检查里写库；
- 迁移前可 `--backup`，走 SQLite 在线备份 API（含 WAL）；
- 重复执行幂等；返回非 0 表示仍有差异。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import create_engine  # noqa: E402

from app import db  # noqa: E402

MAIN_DB = ROOT / "backend" / "data" / "fupan.db"


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


def _print_report(title: str, report: dict) -> None:
    print(f"[{title}] 兼容={report['ok']}")
    for key, label in (
        ("missing_tables", "缺表"),
        ("missing_columns", "缺列"),
        ("missing_indexes", "缺索引"),
    ):
        items = report[key]
        if items:
            print(f"  {label}（{len(items)}）：{'、'.join(items)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="显式执行数据库 Schema 迁移")
    parser.add_argument("--db", required=True, help="目标 SQLite（必须显式指定）")
    parser.add_argument("--status", action="store_true", help="只检查差异，只读打开")
    parser.add_argument("--apply", action="store_true", help="执行迁移")
    parser.add_argument("--backup", help="迁移前用在线备份 API 复制到该路径")
    parser.add_argument("--allow-main-db", action="store_true", help="允许对主库执行迁移")
    args = parser.parse_args()

    target = Path(args.db)
    if not target.exists():
        raise SystemExit(f"目标库不存在：{target}")
    if _is_main_db(target) and args.apply and not args.allow_main_db:
        raise SystemExit("拒绝迁移主库结构：请先复制到隔离库，或显式加 --allow-main-db")

    if args.status or not args.apply:
        engine = create_engine(
            f"sqlite:///file:{target.resolve().as_posix()}?mode=ro&uri=true", future=True
        )
        report = db.schema_report(engine)
        engine.dispose()
        _print_report("status", report)
        return 0 if report["ok"] else 1

    if args.backup:
        _backup(target, Path(args.backup))
        print(f"[backup] {args.backup}")

    engine = create_engine(f"sqlite:///{target.as_posix()}", future=True)
    before = db.schema_report(engine)
    _print_report("before", before)
    changed = db.migrate_schema(engine)
    after = db.schema_report(engine)
    _print_report("after", after)
    print(
        f"[apply] 补列 {len(changed['columns'])}、补索引 {len(changed['indexes'])}"
        f"（幂等：再次执行应为 0/0）"
    )
    engine.dispose()
    return 0 if after["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
