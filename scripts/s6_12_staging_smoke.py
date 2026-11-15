"""S6.12 隔离 Staging 冒烟：验证写入边界与基本可用性。

临时库（当前 schema）+ 临时端口；外部取数与调度关闭；不接触生产。
检查：health 200、登录、首页数据、个股、板块成分股纯读、两个同步接口 409、
研究表前后摘要一致。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
CN_TZ = timezone(timedelta(hours=8))
PASSWORD = "staging-password-123"
TABLES = ("stock_daily", "sector_member", "ifind_usage", "stock_event_cache", "stock_financial")


def http(opener, url, method="GET", payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def digest(db: Path) -> dict:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        result = {}
        for table in TABLES:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            payload = json.dumps(rows, ensure_ascii=False, default=str).encode()
            result[table] = {"rows": len(rows), "sha256": hashlib.sha256(payload).hexdigest()}
        return result
    finally:
        conn.close()


def main() -> int:
    stage = Path(tempfile.mkdtemp(prefix="s6_12-staging-"))
    db = stage / "staging.db"
    env = {
        **os.environ,
        "DB_PATH": str(db),
        "DB_SPLIT_ENABLED": "false",
        "EXTERNAL_DATA_FETCH_ENABLED": "false",
        "SCHEDULER_ENABLED": "false",
        "WARMUP_PATTERN_CACHE": "false",
        "FUPAN_SCHEMA_MODE": "migrate",
        "FUPAN_ENV": "staging",
        "ENABLE_DEV_CORS": "false",
        "IFIND_AUTH_TOKEN": "",
        "PYTHONPATH": str(ROOT / "backend"),
    }
    seed = (
        "from app.db import init_db; init_db();"
        "import sqlite3, os;"
        "from app.services.auth import hash_password;"
        "c=sqlite3.connect(os.environ['DB_PATH']);"
        "c.execute('INSERT INTO app_user(username,password_hash,is_admin,created_at) VALUES(?,?,?,datetime(\"now\"))',('s12admin',hash_password('staging-password-123'),1));"
        "c.execute('INSERT INTO stock_basic(code,name,updated_at) VALUES(?,?,datetime(\"now\"))',('000001','平安银行'));"
        "c.execute('INSERT INTO sector_basic(code,name,taxonomy,updated_at) VALUES(?,?,?,datetime(\"now\"))',('S612','测试板块','test'));"
        "c.execute('INSERT INTO stock_daily(trade_date,code,close,pct_chg) VALUES(?,?,?,?)',('2026-11-13','000001',10.0,1.0));"
        "c.execute('INSERT INTO market_sentiment(trade_date,limit_up_count,limit_down_count) VALUES(?,?,?)',('2026-11-13',40,3));"
        "c.commit(); c.close(); print('seeded')"
    )
    init = subprocess.run(
        [str(VENV_PY), "-c", seed],
        cwd=str(ROOT / "backend"),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if init.returncode != 0:
        raise SystemExit(init.stderr[-1000:])

    log = stage / "server.log"
    port = 8025
    proc = subprocess.Popen(
        [str(VENV_PY), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT),
        env=env,
        stdout=log.open("wb"),
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    results = {}
    before = digest(db)
    try:
        deadline = time.time() + 50
        healthy = False
        while time.time() < deadline:
            status, _ = http(urllib.request.build_opener(), f"{base}/api/health", timeout=1)
            if status == 200:
                healthy = True
                break
            time.sleep(0.4)
        if not healthy:
            raise SystemExit("server not healthy")

        jar = CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        results["health"] = http(opener, f"{base}/api/health")[0]
        results["login"] = http(opener, f"{base}/api/auth/login", "POST", {"username": "s12admin", "password": PASSWORD})[0]
        results["market_overview"] = http(opener, f"{base}/api/market/overview")[0]
        results["stock_profile"] = http(opener, f"{base}/api/stock/000001")[0]
        sector_status, sector_body = http(opener, f"{base}/api/sectors/members?code=S612")
        results["sector_members"] = sector_status
        results["sector_members_body"] = sector_body[:300]
        results["watchlist_sync"] = http(opener, f"{base}/api/watchlist/sync", "POST")[0]
        results["stock_sync"] = http(opener, f"{base}/api/stock/000001/sync", "POST")[0]
        results["ready"] = http(opener, f"{base}/api/ready")[0]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        after = digest(db)

    checks = {
        "health_200": results.get("health") == 200,
        "login_200": results.get("login") == 200,
        "market_200": results.get("market_overview") == 200,
        "stock_200": results.get("stock_profile") == 200,
        "sector_members_200": results.get("sector_members") == 200,
        "watchlist_sync_409": results.get("watchlist_sync") == 409,
        "stock_sync_409": results.get("stock_sync") == 409,
        "research_tables_unchanged": before == after,
    }
    output = {
        "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "environment": "isolated temp DB; external fetch/scheduler off; no production access",
        "results": results,
        "checks": checks,
        "before": before,
        "after": after,
    }
    output["verdict"] = "PASS" if all(checks.values()) else "FAIL"
    out = ROOT / "reports" / "s6_12"
    (out / "05-staging-smoke.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"verdict": output["verdict"], "checks": checks}, ensure_ascii=False))
    return 0 if output["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
