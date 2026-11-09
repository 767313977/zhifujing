"""在隔离库安装研究缓存的数据修订水位（P1）。

默认拒绝主库 `backend/data/fupan.db`：该机制会新增表与触发器，属于结构变更，
必须先在隔离库验证。

用法：
    python scripts/install_data_revision.py --copy-from backend/data/fupan.db --db backend/data/revtest.db
    python scripts/install_data_revision.py --db backend/data/b1_research_copy.db --status
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services import data_revision  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402


def _is_main_db(path: Path) -> bool:
    return os.path.normcase(str(path.resolve())) == os.path.normcase(
        str((ROOT / "backend" / "data" / "fupan.db").resolve())
    )


def _copy_db(src: Path, dst: Path) -> None:
    if not src.exists():
        raise SystemExit(f"源库不存在：{src}")
    if dst.exists():
        raise SystemExit(f"目标已存在，拒绝覆盖：{dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)
        target.execute("PRAGMA journal_mode=WAL")
    finally:
        target.close()
        source.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="安装/查看研究缓存修订水位")
    parser.add_argument("--db", required=True, help="目标 SQLite（必须显式指定）")
    parser.add_argument("--copy-from", help="先用在线备份 API 从该库复制到 --db")
    parser.add_argument("--status", action="store_true", help="只查看当前修订水位，不安装")
    parser.add_argument("--uninstall", action="store_true", help="移除表与触发器（仅隔离库）")
    args = parser.parse_args()

    target = Path(args.db)
    if _is_main_db(target) and not args.status:
        raise SystemExit("拒绝修改主库结构：请先复制到隔离库")
    if args.copy_from:
        _copy_db(Path(args.copy_from), target)
    if not target.exists():
        raise SystemExit(f"目标库不存在：{target}")

    if args.status:
        # 查看主库也只能只读打开：不能因为「看状态」而创建表/触发器
        engine = create_engine(
            f"sqlite:///file:{target.resolve().as_posix()}?mode=ro&uri=true", future=True
        )
    else:
        engine = create_engine(f"sqlite:///{target}", future=True)
    with Session(engine) as session:
        if args.status:
            if not data_revision.installed(session):
                print("未安装修订水位（使用过渡回退：data_version + 文件 stat）")
                return 0
            rows = session.connection().exec_driver_sql(
                "SELECT domain, revision FROM data_revision ORDER BY domain"
            ).all()
            meta = session.connection().exec_driver_sql(
                "SELECT value FROM data_revision_meta WHERE key='db_uuid'"
            ).first()
            print(f"db_uuid={meta[0] if meta else ''}")
            for domain, revision in rows:
                print(f"{domain}: {revision}")
            return 0
        if args.uninstall:
            data_revision.uninstall(session)
            print("已移除数据修订水位")
            return 0
        db_uuid = data_revision.install(session)
        print(f"已安装：db_uuid={db_uuid}")
        for domain, revision in session.connection().exec_driver_sql(
            "SELECT domain, revision FROM data_revision ORDER BY domain"
        ).all():
            print(f"{domain}: {revision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
