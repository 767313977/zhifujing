"""S6.8 R8: isolated SQLite backup / restore drill.

Creates a throw-away DB with the real schema, seeds a user + watchlist + note,
takes an online backup (sqlite3 backup API), simulates row loss, restores the
backup, and proves the data digest returns to the original value.

This never touches production DBs. Output: JSON + markdown under reports/s6_8_9/security.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CN_TZ = timezone(timedelta(hours=8))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def table_digest(conn: sqlite3.Connection, table: str) -> dict:
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
    payload = json.dumps(rows, ensure_ascii=False, default=str, sort_keys=True).encode("utf-8")
    return {"rows": len(rows), "sha256": hashlib.sha256(payload).hexdigest()}


def snapshot(conn: sqlite3.Connection, tables: list[str]) -> dict:
    return {
        "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
        "tables": {table: table_digest(conn, table) for table in tables},
    }


def _bounded_digest(conn: sqlite3.Connection, table: str, limit: int = 200) -> dict:
    """大表用「行数 + rowid 合计 + 前 N 行内容摘要」作对照，避免整表读进内存。"""
    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    rowid_sum = conn.execute(f"SELECT COALESCE(SUM(rowid), 0) FROM {table}").fetchone()[0]
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid LIMIT {limit}").fetchall()
    payload = json.dumps(rows, ensure_ascii=False, default=str).encode("utf-8")
    return {
        "rows": count,
        "rowid_sum": rowid_sum,
        "head_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _snapshot_copy(conn: sqlite3.Connection, tables: list[str]) -> dict:
    return {
        "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
        "tables": {table: _bounded_digest(conn, table) for table in tables},
    }


def _run_source_copy_drill(source: Path, out: Path) -> int:
    """S6.10：真实库（只读）→ 临时副本 → 备份 → 人为丢行 → 恢复 → 摘要对照。"""
    if not source.is_file():
        raise SystemExit(f"source DB missing: {source}")
    stage = Path(tempfile.mkdtemp(prefix="s6_10-backup-"))
    working = stage / f"{source.stem}-copy.db"
    backup_path = stage / f"{source.stem}.backup.db"
    tables = [
        "app_user",
        "watchlist",
        "review_note",
        "user_session",
        "collect_log",
        "limit_pool",
        "sector_daily",
        "stock_daily",
    ]
    timings: dict[str, float] = {}

    # 1) 源库只读打开，用在线备份 API 生成临时工作副本（不直接 copy 主文件）
    started = time.perf_counter()
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro&uri=true", uri=True)
    try:
        dst = sqlite3.connect(working)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    timings["copy_ms"] = round((time.perf_counter() - started) * 1000, 2)

    with sqlite3.connect(working) as conn:
        before = _snapshot_copy(conn, tables)

    # 2) 生产同款备份：工作副本 → 备份文件
    started = time.perf_counter()
    with sqlite3.connect(working) as src_conn, sqlite3.connect(backup_path) as backup_conn:
        src_conn.backup(backup_conn)
    timings["backup_ms"] = round((time.perf_counter() - started) * 1000, 2)
    backup_sha = sha256_file(backup_path)
    backup_size = backup_path.stat().st_size
    with sqlite3.connect(backup_path) as conn:
        backup_snapshot = _snapshot_copy(conn, tables)

    # 3) 人为丢数据（只动临时副本）
    with sqlite3.connect(working) as conn:
        deleted = conn.execute(
            "DELETE FROM collect_log WHERE rowid IN "
            "(SELECT rowid FROM collect_log ORDER BY rowid DESC LIMIT 3)"
        ).rowcount
        conn.execute(
            "DELETE FROM watchlist WHERE rowid IN "
            "(SELECT rowid FROM watchlist ORDER BY rowid DESC LIMIT 1)"
        )
        conn.commit()
        damaged = _snapshot_copy(conn, tables)

    # 4) 从备份恢复
    started = time.perf_counter()
    with sqlite3.connect(backup_path) as src_conn, sqlite3.connect(working) as dst_conn:
        src_conn.backup(dst_conn)
    timings["restore_ms"] = round((time.perf_counter() - started) * 1000, 2)
    with sqlite3.connect(working) as conn:
        restored = _snapshot_copy(conn, tables)

    backup_sha_after = sha256_file(backup_path)
    checks = {
        "source_read_only_opened": True,
        "copy_integrity": before["integrity"] == "ok",
        "backup_integrity": backup_snapshot["integrity"] == "ok",
        "backup_matches_source": backup_snapshot["tables"] == before["tables"],
        "loss_detected": damaged["tables"] != before["tables"],
        "restore_digest_equal": restored["tables"] == before["tables"],
        "restore_integrity": restored["integrity"] == "ok",
        "backup_file_not_overwritten": backup_sha_after == backup_sha,
    }
    result = {
        "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "environment": "isolated temp copy of the real DB; production untouched",
        "source_db": str(source),
        "source_size": source.stat().st_size,
        "working_copy": str(working),
        "backup_size": backup_size,
        "backup_sha256": backup_sha,
        "deleted_rows": deleted,
        "timings": timings,
        "before": before,
        "backup": backup_snapshot,
        "after_simulated_loss": damaged,
        "after_restore": restored,
        "checks": checks,
    }
    result["verdict"] = "PASS" if all(checks.values()) else "FAIL"
    (out / "backup-restore-realcopy.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md = [
        "# S6.10 真实库副本备份 / 恢复演练",
        "",
        f"- 时间：{result['generated_at']}",
        f"- 源库：`{source}`（以 `mode=ro` 打开；大小 {source.stat().st_size:,} 字节）",
        "- 环境：临时工作副本 + 临时备份文件；未写入源库",
        f"- 备份：online backup API（{timings['backup_ms']} ms，{backup_size / 1e6:.0f} MB）",
        f"- 恢复：backup → 覆盖工作副本（{timings['restore_ms']} ms）",
        "",
        "| 检查 | 结果 |",
        "| --- | --- |",
    ]
    for name, ok in checks.items():
        md.append(f"| {name} | {'PASS' if ok else 'FAIL'} |")
    md.extend(["", f"**结论：{result['verdict']}**", ""])
    (out / "backup-restore-realcopy.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps({"verdict": result["verdict"], "checks": checks, "timings": timings}, ensure_ascii=False))
    shutil.rmtree(stage, ignore_errors=True)
    return 0 if result["verdict"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "reports" / "s6_8_9" / "security"))
    #: S6.10：在**真实库的临时副本**上演练（源库只以 mode=ro 打开，不会被写入）
    parser.add_argument("--source-db", default="")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    if args.source_db:
        return _run_source_copy_drill(Path(args.source_db).resolve(), out)

    stage = Path(tempfile.mkdtemp(prefix="s6_8-backup-"))
    db_path = stage / "staging.db"
    backup_path = stage / "staging.backup.db"
    os.environ["DB_PATH"] = str(db_path)
    os.environ["FUPAN_SCHEMA_MODE"] = "migrate"
    os.environ["EXTERNAL_DATA_FETCH_ENABLED"] = "false"
    os.environ["SCHEDULER_ENABLED"] = "false"

    import sys

    sys.path.insert(0, str(ROOT / "backend"))
    from app.db import init_db  # noqa: PLC0415
    from app.services.auth import hash_password  # noqa: PLC0415

    init_db()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO app_user(username,password_hash,is_admin,created_at) VALUES(?,?,?,datetime('now'))",
            ("s6backup", hash_password("backup-drill-password"), 1),
        )
        user_id = conn.execute("SELECT id FROM app_user WHERE username='s6backup'").fetchone()[0]
        conn.execute(
            "INSERT INTO watchlist(user_id,code,name,note,added_at) VALUES(?,?,?,?,datetime('now'))",
            (user_id, "000001", "平安银行", "backup-drill"),
        )
        conn.execute(
            "INSERT INTO review_note(user_id,trade_date,market_view,updated_at) "
            "VALUES(?,?,?,datetime('now'))",
            (user_id, "2026-11-14", "backup drill note"),
        )
        conn.commit()

    tables = ["app_user", "watchlist", "review_note"]
    with sqlite3.connect(db_path) as conn:
        before = snapshot(conn, tables)
    original_sha = sha256_file(db_path)

    started = time.perf_counter()
    with sqlite3.connect(db_path) as src, sqlite3.connect(backup_path) as dst:
        src.backup(dst)
    backup_ms = round((time.perf_counter() - started) * 1000, 2)
    backup_sha = sha256_file(backup_path)
    with sqlite3.connect(backup_path) as conn:
        backup_snapshot = snapshot(conn, tables)

    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM watchlist WHERE code='000001'")
        conn.commit()
        damaged = snapshot(conn, tables)

    started = time.perf_counter()
    with sqlite3.connect(backup_path) as src, sqlite3.connect(db_path) as dst:
        src.backup(dst)
    restore_ms = round((time.perf_counter() - started) * 1000, 2)
    with sqlite3.connect(db_path) as conn:
        restored = snapshot(conn, tables)

    result = {
        "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "environment": "isolated temp DB; production untouched",
        "db_path": str(db_path),
        "backup_path": str(backup_path),
        "db_sha256_before": original_sha,
        "backup_sha256": backup_sha,
        "backup_ms": backup_ms,
        "restore_ms": restore_ms,
        "before": before,
        "backup_readable": backup_snapshot,
        "after_simulated_loss": damaged,
        "after_restore": restored,
        "checks": {
            "backup_integrity": backup_snapshot["integrity"] == "ok",
            "backup_matches_source": backup_snapshot["tables"] == before["tables"],
            "loss_detected": damaged["tables"] != before["tables"],
            "restore_digest_equal": restored["tables"] == before["tables"],
            "restore_integrity": restored["integrity"] == "ok",
        },
    }
    result["verdict"] = "PASS" if all(result["checks"].values()) else "FAIL"
    (out / "backup-restore.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    md = [
        "# S6.8 R8：隔离 Staging SQLite 备份 / 恢复演练",
        "",
        f"- 时间：{result['generated_at']}",
        "- 环境：临时目录独立库；未连接生产库",
        f"- 备份方式：sqlite3 online backup API（{backup_ms} ms）",
        f"- 恢复方式：backup → 覆盖临时库（{restore_ms} ms）",
        "",
        "| 检查 | 结果 |",
        "| --- | --- |",
    ]
    for name, ok in result["checks"].items():
        md.append(f"| {name} | {'PASS' if ok else 'FAIL'} |")
    md.extend(
        [
            "",
            f"**结论：{result['verdict']}**",
            "",
            "说明：本演练只证明隔离库的备份可读、可恢复、恢复后数据摘要一致；",
            "不等于生产备份链路/异地存储/恢复 SLA 已验收。",
        ]
    )
    (out / "backup-restore.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps({"verdict": result["verdict"], "checks": result["checks"]}, ensure_ascii=False))
    shutil.rmtree(stage, ignore_errors=True)
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
