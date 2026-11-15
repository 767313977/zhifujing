"""S6.10 core-business browser check: deep link / refresh / back-forward.

Starts the same isolated split server as `s6_10_core_business_acceptance.py`
(real research DB opened read-only + temporary user DB, external fetch and
scheduler disabled), then drives a real Chrome over CDP via the existing
`scripts/s6_8_cdp_e2e.mjs --only history` scenario.

Output: reports/s6_10/browser/history_navigation.json (+ summary.json)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"


def main() -> int:
    port = 8024
    base = f"http://127.0.0.1:{port}"
    research = ROOT / "backend" / "data" / "b1_research.db"
    if not research.exists():
        raise SystemExit(f"research DB missing: {research}")

    stage = Path(tempfile.mkdtemp(prefix="s6_10-browser-"))
    user_db = stage / "user.db"
    env = {
        **os.environ,
        "DB_SPLIT_ENABLED": "true",
        "DB_PATH": str(research),
        "USER_DB_PATH": str(user_db),
        "RESEARCH_DB_READONLY": "true",
        "EXTERNAL_DATA_FETCH_ENABLED": "false",
        "SCHEDULER_ENABLED": "false",
        "WARMUP_PATTERN_CACHE": "false",
        "FUPAN_SCHEMA_MODE": "verify",
        "FUPAN_ENV": "staging",
        "ENABLE_DEV_CORS": "false",
        "IFIND_AUTH_TOKEN": "",
        "PYTHONPATH": str(ROOT / "backend"),
    }
    init_code = (
        "from app.db import init_user_db; init_user_db();"
        "import os, sqlite3;"
        "from app.services.auth import hash_password;"
        "c=sqlite3.connect(os.environ['USER_DB_PATH']);"
        "c.execute('INSERT OR REPLACE INTO app_user(username,password_hash,is_admin,created_at) VALUES(?,?,?,datetime(\"now\"))',('s7staging',hash_password('staging-password-123'),1));"
        "c.execute('INSERT OR REPLACE INTO app_user(username,password_hash,is_admin,created_at) VALUES(?,?,?,datetime(\"now\"))',('s7userb',hash_password('staging-password-123'),0));"
        "c.commit(); c.close(); print('user db ready')"
    )
    init = subprocess.run(
        [str(VENV_PY), "-c", init_code],
        cwd=str(ROOT / "backend"),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if init.returncode != 0:
        raise SystemExit(init.stderr[-1000:])

    out_dir = ROOT / "reports" / "s6_10" / "browser"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = ROOT / "reports" / "s6_10" / "browser-history-server.log"
    process = subprocess.Popen(
        [str(VENV_PY), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT),
        env=env,
        stdout=log_path.open("wb"),
        stderr=subprocess.STDOUT,
    )
    try:
        # node 脚本自己会等待页面；这里只等端口起来
        deadline = time.time() + 50
        healthy = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{base}/api/health", timeout=1) as response:
                    if response.status == 200:
                        healthy = True
                        break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)
        if not healthy:
            raise SystemExit("server did not become healthy")

        run = subprocess.run(
            [
                "node",
                "scripts/s6_8_cdp_e2e.mjs",
                "--only",
                "history",
                "--base",
                base,
                "--out",
                str(out_dir),
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        print(run.stdout)
        if run.returncode != 0:
            print(run.stderr[-2000:], file=sys.stderr)
            return run.returncode
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()

    result = json.loads((out_dir / "history_navigation.json").read_text(encoding="utf-8"))
    print(json.dumps({"pass": result.get("pass"), "steps": sorted(result.get("steps", {}))}, ensure_ascii=False))
    return 0 if result.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
