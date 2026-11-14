"""S7（G7）：513MB 隔离副本的用户表迁移复核。

不做「只看总行数」的浅校验，而是逐项对比：

- 源副本 SHA256（迁移前后都不变）；
- 10 张用户表：行数、主键集合摘要、关键字段摘要；
- schema fingerprint（表 / 索引 / 触发器的 `sqlite_master.sql` 排序后哈希）；
- 重复执行（幂等）；
- 中断恢复（把目标库截断到错误状态后重跑，最终与源一致）；
- 源库 integrity_check。

约束：只操作 `--allow-dir` 下的隔离副本；源生产库以 `mode=ro` 打开。

用法：
    python scripts/s7_migration_revalidate.py \
        --source backend/data/fupan.db --allow-dir <临时目录>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import models  # noqa: E402
from app.db_roles import USER_MODEL_NAMES  # noqa: E402

CN_TZ = timezone(timedelta(hours=8))

KEY_FIELDS = {
    "app_user": ("username", "is_admin", "disabled_at"),
    "invite_code": ("code", "used_by", "disabled_at"),
    "user_session": ("token_hash", "user_id", "expires_at"),
    "watchlist": ("user_id", "code", "note"),
    "review_note": ("user_id", "trade_date", "updated_at"),
    "user_risk_pref": ("user_id", "max_position_pct", "max_theme_pct"),
    "trade_journal": ("user_id", "code", "entry_date", "exit_date"),
    "trade_t": ("trade_id", "trade_date", "direction", "ratio_pct"),
    "trade_plan": ("user_id", "code", "plan_date", "status"),
    "analysis_snapshot": ("user_id", "code", "created_at"),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tables() -> list[str]:
    return [getattr(models, name).__tablename__ for name in USER_MODEL_NAMES]


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone():
        return []
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]


def _pk(connection: sqlite3.Connection, table: str) -> list[str]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [row[1] for row in sorted((r for r in rows if r[5] > 0), key=lambda r: r[5])]


def _digest(connection: sqlite3.Connection, table: str, fields: tuple[str, ...] | None = None):
    columns = _columns(connection, table)
    if not columns:
        return {"rows": 0, "pk_digest": None, "key_digest": None, "missing": True}
    primary = _pk(connection, table)
    order = ", ".join(f'"{name}"' for name in (primary or ["rowid"]))
    select = ", ".join(f'"{name}"' for name in columns)
    pk_digest = hashlib.sha256()
    key_digest = hashlib.sha256()
    count = 0
    key_cols = [name for name in (fields or ()) if name in columns]
    for row in connection.execute(f'SELECT {select} FROM "{table}" ORDER BY {order}'):
        pk_digest.update(repr(tuple(row)).encode("utf-8"))
        if key_cols:
            index = [columns.index(name) for name in key_cols]
            key_digest.update(repr(tuple(row[i] for i in index)).encode("utf-8"))
        count += 1
    return {
        "rows": count,
        "pk_digest": pk_digest.hexdigest() if count else None,
        "key_digest": key_digest.hexdigest() if count and key_cols else None,
        "missing": False,
    }


def _schema_fingerprint(connection: sqlite3.Connection, tables: list[str] | None = None) -> str:
    targets = tables if tables is not None else _tables()
    if not targets:
        return hashlib.sha256(b"").hexdigest()
    placeholders = ",".join("?" for _ in targets)
    rows = connection.execute(
        "SELECT type, name, sql FROM sqlite_master "
        f"WHERE tbl_name IN ({placeholders}) AND sql IS NOT NULL "
        "ORDER BY type, name",
        targets,
    ).fetchall()
    payload = "\n".join(f"{row[0]}|{row[1]}|{row[2]}" for row in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _snapshot(path: Path) -> dict:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            table: _digest(connection, table, KEY_FIELDS.get(table))
            for table in _tables()
        }
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        return {
            "path": str(path),
            "tables": tables,
            "schema_fingerprint": _schema_fingerprint(connection),
            "integrity_check": [row[0] for row in integrity],
        }
    finally:
        connection.close()


def _backup(source: Path, target: Path) -> None:
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()


def _run_migration(source: Path, target: Path, allow: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "s6_migrate_user_db.py"),
            "--source-copy",
            str(source),
            "--target-user-db",
            str(target),
            "--allow-dir",
            str(allow),
            "--apply",
            "--report",
            str(allow / "_migration-report.json"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _compare(
    source: dict, target: dict, source_shared_fp: str, target_shared_fp: str
) -> tuple[list[str], list[str]]:
    """返回（差异, 源缺失但目标按 ORM 建空表的表）。"""
    diffs: list[str] = []
    extra_empty: list[str] = []
    for table, info in source["tables"].items():
        other = target["tables"].get(table, {})
        if info["missing"]:
            # 旧生产库可能没有后来新增的用户表：迁移按 ORM 建空表是**预期行为**，
            # 但必须确实为空，不能凭空造数据。
            if not other.get("missing"):
                if other.get("rows") == 0:
                    extra_empty.append(table)
                else:
                    diffs.append(f"{table}: 源缺表但目标有 {other.get('rows')} 行")
            continue
        for field in ("rows", "pk_digest", "key_digest"):
            if info[field] != other.get(field):
                diffs.append(f"{table}.{field}: {info[field]} != {other.get(field)}")
    if source_shared_fp != target_shared_fp:
        diffs.append("交集 schema_fingerprint 不一致")
    return diffs, extra_empty


def _shared_fingerprint(path: Path, tables: list[str]) -> str:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return _schema_fingerprint(connection, tables)
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(ROOT / "backend" / "data" / "fupan.db"))
    parser.add_argument("--allow-dir", default=None)
    args = parser.parse_args()

    source = Path(args.source).resolve()
    if not source.exists():
        print(f"source missing: {source}")
        return 2
    allow = Path(args.allow_dir).resolve() if args.allow_dir else Path(tempfile.mkdtemp(prefix="s7-migrate-"))
    allow.mkdir(parents=True, exist_ok=True)

    started = datetime.now(CN_TZ).isoformat(timespec="seconds")
    source_sha_before = _sha256(source)
    copy_path = allow / "legacy-copy.db"
    target_path = allow / "user.db"
    _backup(source, copy_path)
    copy_sha_before = _sha256(copy_path)

    first = _run_migration(copy_path, target_path, allow)
    if first.returncode != 0:
        print(first.stdout[-2000:], first.stderr[-2000:])
        return 1

    source_snapshot = _snapshot(copy_path)
    target_snapshot_once = _snapshot(target_path)
    shared_tables = [
        table for table, info in source_snapshot["tables"].items() if not info["missing"]
    ]
    source_shared_fp = _shared_fingerprint(copy_path, shared_tables)
    diffs_first, expected_empty = _compare(
        source_snapshot,
        target_snapshot_once,
        source_shared_fp,
        _shared_fingerprint(target_path, shared_tables),
    )

    second = _run_migration(copy_path, target_path, allow)
    target_snapshot_twice = _snapshot(target_path)
    diffs_second, _ = _compare(
        source_snapshot,
        target_snapshot_twice,
        source_shared_fp,
        _shared_fingerprint(target_path, shared_tables),
    )

    # 中断恢复：把目标库里的用户数据删掉一部分（模拟写了一半），再重跑迁移
    connection = sqlite3.connect(target_path)
    try:
        for table in ("watchlist", "trade_journal", "trade_plan", "analysis_snapshot"):
            if _columns(connection, table):
                connection.execute(f'DELETE FROM "{table}"')
        connection.commit()
    finally:
        connection.close()
    third = _run_migration(copy_path, target_path, allow)
    target_snapshot_recovered = _snapshot(target_path)
    diffs_recovered, _ = _compare(
        source_snapshot,
        target_snapshot_recovered,
        source_shared_fp,
        _shared_fingerprint(target_path, shared_tables),
    )

    source_sha_after = _sha256(source)
    copy_sha_after = _sha256(copy_path)

    report = {
        "generated_at": started,
        "source": str(source),
        "source_size": source.stat().st_size,
        "isolated_dir": str(allow),
        "source_sha256_before": source_sha_before,
        "source_sha256_after": source_sha_after,
        "copy_sha256_before": copy_sha_before,
        "copy_sha256_after": copy_sha_after,
        "source_unchanged": source_sha_before == source_sha_after,
        "copy_unchanged": copy_sha_before == copy_sha_after,
        "source_integrity": source_snapshot["integrity_check"],
        "target_integrity": target_snapshot_twice["integrity_check"],
        "runs": {
            "first_exit": first.returncode,
            "second_exit": second.returncode,
            "recovery_exit": third.returncode,
        },
        "first_run_diffs": diffs_first,
        "second_run_diffs": diffs_second,
        "recovery_run_diffs": diffs_recovered,
        "tables": source_snapshot["tables"],
        "shared_tables": shared_tables,
        "expected_empty_tables": expected_empty,
        "schema_fingerprint": source_shared_fp,
        "target_schema_fingerprint": _shared_fingerprint(target_path, shared_tables),
        "full_target_schema_fingerprint": target_snapshot_twice["schema_fingerprint"],
        "idempotent": not diffs_second,
        "recovered": not diffs_recovered,
        "status": "PASS"
        if (
            not diffs_first
            and not diffs_second
            and not diffs_recovered
            and source_snapshot["integrity_check"] == ["ok"]
            and target_snapshot_twice["integrity_check"] == ["ok"]
            and source_sha_before == source_sha_after
            and copy_sha_before == copy_sha_after
        )
        else "FAIL",
    }

    out_dir = ROOT / "reports" / "s6-followup" / "g7"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "migration-revalidation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({k: report[k] for k in ("status", "idempotent", "recovered", "source_unchanged")}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
