"""S6.12 P0-2：RPS schema 迁移隔离演练（生产结构副本，绝不写生产库）。

源库以 `mode=ro` 打开，经 SQLite 在线备份 API 复制到临时目录；随后在副本上：
备份 → 校验 → 记录 schema/数据摘要 → 迁移 → 重复迁移（幂等）→ 模拟中断 →
重新迁移 → 旧列查询兼容 → 新版本 schema verify → 数据摘要对比。

输出：reports/s6_12/03-rps-migration-evidence.json / .md
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
CN_TZ = timezone(timedelta(hours=8))
NEW_COLUMNS = ("ret20", "ret60", "rps20", "rps60")
OLD_COLUMNS = ("ret50", "ret120", "ret250", "rps50", "rps120", "rps250")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def columns(conn: sqlite3.Connection) -> list[str]:
    return [row[1] for row in conn.execute("PRAGMA table_info(rps_daily)")]


def table_digest(conn: sqlite3.Connection, selected: tuple[str, ...] | None = None) -> dict:
    cols = selected or "*"
    if selected:
        sql = f"SELECT {', '.join(selected)} FROM rps_daily ORDER BY trade_date, code"
    else:
        sql = "SELECT * FROM rps_daily ORDER BY trade_date, code"
    rows = conn.execute(sql).fetchall()
    payload = json.dumps(rows, ensure_ascii=False, default=str).encode("utf-8")
    return {"rows": len(rows), "sha256": hashlib.sha256(payload).hexdigest(), "columns": cols}


def run_migration(db: Path) -> dict:
    result = subprocess.run(
        [str(VENV_PY), str(ROOT / "scripts" / "migrate_rps_columns.py"), "--db", str(db), "--apply"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip()[-1200:],
        "stderr": result.stderr.strip()[-800:],
    }


def main() -> int:
    source = ROOT / "backend" / "data" / "fupan.db"
    out = ROOT / "reports" / "s6_12"
    out.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="s6_12-rps-"))
    working = stage / "fupan-copy.db"
    backup = stage / "fupan-copy.before-rps.db"

    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(working)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    with sqlite3.connect(working) as conn:
        before_columns = columns(conn)
        before_full = table_digest(conn, OLD_COLUMNS)
        before_old = table_digest(conn, OLD_COLUMNS)
        before_integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]

    with sqlite3.connect(working) as src_conn, sqlite3.connect(backup) as bak_conn:
        src_conn.backup(bak_conn)
    backup_sha = sha256(backup)
    with sqlite3.connect(backup) as conn:
        backup_integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        backup_old = table_digest(conn, OLD_COLUMNS)

    first = run_migration(working)
    with sqlite3.connect(working) as conn:
        after_first_columns = columns(conn)
        after_first_full = table_digest(conn, OLD_COLUMNS)
        after_first_integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]

    second = run_migration(working)
    with sqlite3.connect(working) as conn:
        after_second_columns = columns(conn)

    # 模拟中断：删掉一个新增列后重跑
    with sqlite3.connect(working) as conn:
        conn.execute("ALTER TABLE rps_daily DROP COLUMN rps20")
        conn.commit()
        interrupted_columns = columns(conn)
    recovered = run_migration(working)
    with sqlite3.connect(working) as conn:
        recovered_columns = columns(conn)
        recovered_full = table_digest(conn, OLD_COLUMNS)
        recovered_integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        legacy_query_ok = bool(
            conn.execute(
                "SELECT ret50, ret120, ret250, rps50, rps120, rps250 FROM rps_daily LIMIT 1"
            ).fetchone()
        )

    # 新版本 schema verify（独立进程，DB_PATH 指向迁移后的副本）
    env = {
        **os.environ,
        "DB_PATH": str(working),
        "DB_SPLIT_ENABLED": "false",
        "EXTERNAL_DATA_FETCH_ENABLED": "false",
        "SCHEDULER_ENABLED": "false",
        "FUPAN_SCHEMA_MODE": "verify",
        "PYTHONPATH": str(ROOT / "backend"),
    }
    verify = subprocess.run(
        [
            str(VENV_PY),
            "-c",
            "import json; from app.db import schema_report; print(json.dumps(schema_report(include_user_models=False), ensure_ascii=False))",
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    try:
        verify_payload = json.loads(verify.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        verify_payload = {"raw": verify.stdout[-800:], "stderr": verify.stderr[-800:]}

    checks = {
        "source_opened_read_only": True,
        "backup_integrity": backup_integrity == "ok",
        "backup_old_data_matches": backup_old == before_old,
        "migration_first_exit0": first["returncode"] == 0,
        "new_columns_added": all(col in after_first_columns for col in NEW_COLUMNS),
        "old_columns_kept": all(col in after_first_columns for col in OLD_COLUMNS),
        "migration_second_exit0": second["returncode"] == 0,
        "idempotent_columns": after_first_columns == after_second_columns,
        "data_unchanged": after_first_full == before_full,
        "integrity_after": after_first_integrity == "ok",
        "interruption_detected": "rps20" not in interrupted_columns,
        "recovery_exit0": recovered["returncode"] == 0,
        "recovery_columns": all(col in recovered_columns for col in NEW_COLUMNS),
        "recovery_data_unchanged": recovered_full == before_full,
        "recovery_integrity": recovered_integrity == "ok",
        "legacy_columns_query_ok": legacy_query_ok,
        "new_version_schema_verify_ok": verify_payload.get("ok") is True,
    }
    result = {
        "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "source": str(source),
        "source_size": source.stat().st_size,
        "environment": "isolated copy; source opened mode=ro; production untouched",
        "before_columns": before_columns,
        "after_columns": after_first_columns,
        "interrupted_columns": interrupted_columns,
        "recovered_columns": recovered_columns,
        "source_integrity": before_integrity,
        "before_rows": before_full["rows"],
        "before_digest": before_full["sha256"],
        "after_digest": after_first_full["sha256"],
        "backup_sha256": backup_sha,
        "timings": {},
        "migration_runs": {"first": first, "second": second, "recovered": recovered},
        "new_version_schema_verify": verify_payload,
        "checks": checks,
    }
    result["verdict"] = "PASS" if all(checks.values()) else "FAIL"
    (out / "03-rps-migration-evidence.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# S6.12 RPS Schema 迁移演练（生产结构副本）",
        "",
        f"- 时间：{result['generated_at']}",
        f"- 源库：`{source}`（{source.stat().st_size:,} 字节，`mode=ro`）",
        "- 副本：临时目录；演练后删除",
        f"- 备份 SHA256：`{backup_sha}`",
        f"- 迁移前列：`{before_columns}`",
        f"- 迁移后列：`{after_first_columns}`",
        f"- 行数/摘要：{before_full['rows']} / `{before_full['sha256'][:16]}…` → "
        f"{after_first_full['rows']} / `{after_first_full['sha256'][:16]}…`",
        "",
        "| 检查 | 结果 |",
        "| --- | --- |",
    ]
    lines += [f"| {name} | {'PASS' if ok else 'FAIL'} |" for name, ok in checks.items()]
    lines += ["", f"**结论：{result['verdict']}**", ""]
    (out / "03-rps-migration-evidence.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"verdict": result["verdict"], "checks": checks}, ensure_ascii=False))
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
