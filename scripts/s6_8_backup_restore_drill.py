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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "reports" / "s6_8_9" / "security"))
    args = parser.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

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
