"""S6.2：把 10 张用户表从旧单库副本迁移到独立 User DB。

安全约束（对应任务书 §3.5）：
- 只操作 `--allow-dir` 下的隔离副本；源库与目标都必须在该目录内；
- 源库以 `mode=ro` 打开，并用 SQLite backup API 生成一致性快照；
- 默认 `--dry-run`，不写任何文件；`--apply` 才创建/写入目标；
- 只建/写 10 张用户表，绝不创建研究表；
- 幂等：按主键 `INSERT OR REPLACE`，重复执行不会重复插入；
- 迁移前后核对行数、主键摘要、外键检查与源库 SHA256；
- 源库永不被修改；不删除源表、不替换生产文件。

用法：
    python scripts/s6_migrate_user_db.py \
      --source-copy /isolated/legacy-copy.db \
      --target-user-db /isolated/user.db \
      --allow-dir /isolated --dry-run

    python scripts/s6_migrate_user_db.py ... --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db_roles import USER_MODEL_NAMES  # noqa: E402

CN_TZ = timezone(timedelta(hours=8))


def _table_names() -> list[str]:
    from app import models

    return [getattr(models, name).__tablename__ for name in USER_MODEL_NAMES]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def _pk_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [row[1] for row in sorted((r for r in rows if r[5] > 0), key=lambda r: r[5])]


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]


def _digest(connection: sqlite3.Connection, table: str) -> tuple[int, str | None]:
    """返回（行数, 内容摘要）。按主键排序；无主键用 rowid；表不存在返回 (0, None)。"""
    columns = _columns(connection, table)
    if not columns:
        return 0, None
    primary = _pk_columns(connection, table)
    order = ", ".join(f'"{name}"' for name in (primary or ["rowid"]))
    select = ", ".join(f'"{name}"' for name in columns)
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(f'SELECT {select} FROM "{table}" ORDER BY {order}'):
        digest.update(repr(tuple(row)).encode("utf-8"))
        count += 1
    return count, digest.hexdigest() if count else None


def _schema_objects(connection: sqlite3.Connection, table: str) -> list[str]:
    rows = connection.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL "
        "AND type IN ('table','index','trigger') ORDER BY type='table' DESC, type, name",
        (table,),
    ).fetchall()
    return [row[0] for row in rows]


def _backup(source: Path, target: Path) -> None:
    source_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    target_conn = sqlite3.connect(target)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()


def _ensure_target_schema(
    target: sqlite3.Connection,
    snapshot: sqlite3.Connection,
    tables: list[str],
    target_path: Path,
) -> list[str]:
    """建目标表：源库有 DDL 就照抄；源库缺表则按 ORM 模型建空表。

    旧生产库可能缺少后来新增的用户表（例如 `user_risk_pref`、`analysis_snapshot`）。
    迁移必须如实记录 `source_missing`，并在 User DB 建出空表，而不是跳过或伪造数据。
    """
    created: list[str] = []
    fallback: list[str] = []
    for table in tables:
        exists = target.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists:
            continue
        objects = _schema_objects(snapshot, table)
        if objects:
            for sql in objects:
                target.execute(sql)
        else:
            fallback.append(table)
        created.append(table)
    target.commit()

    if fallback:
        from sqlalchemy import create_engine

        from app import models
        from app.db_roles import USER_MODEL_NAMES

        names = {
            getattr(models, name).__tablename__: name for name in USER_MODEL_NAMES
        }
        engine = create_engine(f"sqlite:///{target_path}", future=True)
        try:
            models.Base.metadata.create_all(
                bind=engine,
                tables=[getattr(models, names[table]).__table__ for table in fallback],
            )
        finally:
            engine.dispose()
    return created


def _copy_rows(target: sqlite3.Connection, snapshot: sqlite3.Connection, tables: list[str]) -> dict:
    result: dict[str, int] = {}
    for table in tables:
        columns = _columns(snapshot, table)
        if not columns:
            result[table] = 0
            continue
        column_sql = ", ".join(f'"{name}"' for name in columns)
        placeholders = ", ".join("?" for _ in columns)
        rows = snapshot.execute(f'SELECT {column_sql} FROM "{table}"').fetchall()
        target.executemany(
            f'INSERT OR REPLACE INTO "{table}" ({column_sql}) VALUES ({placeholders})', rows
        )
        result[table] = len(rows)
    target.commit()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-copy", required=True)
    parser.add_argument("--target-user-db", required=True)
    parser.add_argument("--allow-dir", required=True, help="隔离目录；源与目标都必须位于其下")
    parser.add_argument("--apply", action="store_true", help="真正写入；默认 dry-run")
    parser.add_argument("--dry-run", action="store_true", help="显式 dry-run（默认行为）")
    parser.add_argument(
        "--report", default=str(ROOT / "reports" / "s6" / "s62" / "migration-report.json")
    )
    args = parser.parse_args()
    if args.apply and args.dry_run:
        raise SystemExit("--apply 与 --dry-run 不能同时使用")

    source = Path(args.source_copy)
    target = Path(args.target_user_db)
    allow_dir = Path(args.allow_dir)
    report_path = Path(args.report)

    if not source.exists():
        raise SystemExit(f"源副本不存在：{source}")
    if source.resolve() == target.resolve():
        raise SystemExit("源与目标不能是同一个文件")
    if not _inside(source, allow_dir) or not _inside(target, allow_dir):
        raise SystemExit("源与目标都必须位于 --allow-dir 指定的隔离目录内")
    if target.exists() and not args.apply:
        # dry-run 不读取已有目标也行，但记录它存在，便于识别误操作
        pass

    tables = _table_names()
    source_sha_before = _sha256(source)
    target_sha_before = _sha256(target) if target.exists() else None
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="s6-migrate-") as tmp:
        snapshot_path = Path(tmp) / "snapshot.db"
        _backup(source, snapshot_path)
        snapshot = sqlite3.connect(f"file:{snapshot_path.as_posix()}?mode=ro", uri=True)
        try:
            source_state: dict[str, dict] = {}
            for table in tables:
                if not snapshot.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone():
                    continue
                rows, digest = _digest(snapshot, table)
                source_state[table] = {"rows": rows, "digest": digest}
            if not args.apply:
                if target.exists():
                    existing_conn = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
                    try:
                        existing_tables = {
                            row[0]
                            for row in existing_conn.execute(
                                "SELECT name FROM sqlite_master WHERE type='table'"
                            )
                        }
                    finally:
                        existing_conn.close()
                else:
                    existing_tables = set()
                payload = {
                    "mode": "dry-run",
                    "source": str(source),
                    "target": str(target),
                    "allow_dir": str(allow_dir),
                    "source_sha256_before": source_sha_before,
                    "source_sha256_after": _sha256(source),
                    "target_sha256_before": target_sha_before,
                    "tables": source_state,
                    "would_create": [table for table in tables if table not in existing_tables],
                    "applied": False,
                }
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(f"[dry-run] 用户表 {len(source_state)} 张，报告：{report_path}")
                return 0

            target_conn = sqlite3.connect(target)
            try:
                created = _ensure_target_schema(target_conn, snapshot, tables, target)
                copied = _copy_rows(target_conn, snapshot, tables)
                target_counts: dict[str, dict] = {}
                mismatches: list[str] = []
                for table in tables:
                    rows, digest = _digest(target_conn, table)
                    target_counts[table] = {"rows": rows, "digest": digest}
                    expected = source_state.get(table)
                    if expected is None:
                        continue
                    if expected["rows"] != rows or expected["digest"] != digest:
                        mismatches.append(table)
                fk_errors = target_conn.execute("PRAGMA foreign_key_check").fetchall()
                integrity = target_conn.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                target_conn.close()
        finally:
            snapshot.close()

    payload = {
        "mode": "apply",
        "source": str(source),
        "target": str(target),
        "allow_dir": str(allow_dir),
        "source_sha256_before": source_sha_before,
        "source_sha256_after": _sha256(source),
        "target_sha256_before": target_sha_before,
        "target_sha256_after": _sha256(target),
        "created_tables": created,
        "source_missing_tables": [table for table in tables if table not in source_state],
        "copied_rows": copied,
        "target_state": target_counts,
        "source_state": source_state,
        "mismatches": mismatches,
        "foreign_key_errors": len(fk_errors),
        "integrity_check": integrity,
        "source_unchanged": source_sha_before == _sha256(source),
        "applied": True,
        "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "verdict": "PASS" if not mismatches and not fk_errors and integrity == "ok" else "FAIL",
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"verdict={payload['verdict']} tables={len(tables)} report={report_path}")
    return 0 if payload["verdict"] == "PASS" and payload["source_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
