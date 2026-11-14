"""S7（G7）：在隔离目录里真实跑一次 `/api/ready` 的数据库门禁。

流程：
1. 造两个隔离库：research.db（研究表，跑完 chmod 只读）、user.db（用户表，可写）；
2. 用子进程 + 环境变量（`FUPAN_DB_SPLIT_ENABLED=true` 等）启动一个全新进程，
   让 `app.config` 在导入期读到拆分配置；
3. 在子进程里调用 `readiness.database_checks()` / `_database_gate()` /
   `readiness_snapshot()`，把真实结果写到
   `reports/s6-followup/g7/readiness-proof.json`。

⚠️ 只使用临时目录，不碰 `backend/data/fupan.db`。

用法：
    python scripts/s7_readiness_probe.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

CN_TZ = timezone(timedelta(hours=8))

CHILD = r"""
import json
from app import readiness

checks = readiness.database_checks()
gate, reason = readiness._database_gate(checks)
snapshot = readiness.readiness_snapshot()
print(json.dumps({
    "checks": checks,
    "database_gate": gate,
    "database_reason": reason,
    "ready": snapshot["ready"],
    "gates": snapshot["gates"],
    "blocking": snapshot["blocking"],
    "reasons": snapshot["reasons"],
}, ensure_ascii=False))
"""


def _make_dbs(directory: Path) -> tuple[Path, Path]:
    from sqlalchemy import create_engine, text

    from app.models import Base

    research = directory / "research.db"
    user = directory / "user.db"
    for path in (research, user):
        engine = create_engine(f"sqlite:///{path}", future=True)
        Base.metadata.create_all(engine)
        if path == research:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO stock_daily(code, trade_date, close) "
                        "VALUES ('000001', '2026-09-24', 10.0)"
                    )
                )
        else:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO app_user(username, password_hash, is_admin, created_at) "
                        "VALUES ('probe', 'x', 0, '2026-11-15 00:00:00')"
                    )
                )
        engine.dispose()
    return research, user


def main() -> int:
    allow = Path(tempfile.mkdtemp(prefix="s7-readiness-"))
    research, user = _make_dbs(allow)
    os.chmod(research, 0o444)
    try:
        env = {
            **os.environ,
            "DB_SPLIT_ENABLED": "true",
            "DB_PATH": str(research),
            "USER_DB_PATH": str(user),
            "RESEARCH_DB_READONLY": "true",
            "PYTHONPATH": str(ROOT / "backend"),
            "FUPAN_SCHEMA_MODE": "verify",
        }
        result = subprocess.run(
            [sys.executable, "-c", CHILD],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode != 0:
            print(result.stdout[-2000:])
            print(result.stderr[-2000:])
            return 1
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        payload.update(
            {
                "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
                "isolated_dir": str(allow),
                "research_path_readonly": True,
                "command": "python scripts/s7_readiness_probe.py",
            }
        )
        out = ROOT / "reports" / "s6-followup" / "g7" / "readiness-proof.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "database_gate": payload["database_gate"],
                    "database_reason": payload["database_reason"],
                    "checks": payload["checks"],
                },
                ensure_ascii=False,
            )
        )
        return 0 if payload["database_gate"] == "PASS" else 1
    finally:
        for path in (research, user):
            if path.exists():
                os.chmod(path, 0o666)


if __name__ == "__main__":
    raise SystemExit(main())
