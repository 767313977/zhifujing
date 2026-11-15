"""S6.10 core-business acceptance on a read-only copy of the real research DB.

Starts an isolated split-mode server:
  - Research DB: backend/data/b1_research.db opened read-only (mode=ro)
  - User DB: temporary, seeded with an admin + a normal user
  - external fetch / scheduler / real collection disabled
Then logs in over HTTP and checks the core read APIs and auth/permission/session
boundaries. It never writes the research DB and never touches production.

Output: reports/s6_10/core-business-acceptance.json + .md
"""

from __future__ import annotations

import argparse
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


def http(opener, url, method="GET", payload=None, timeout=20):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        status = exc.code
    except Exception as exc:  # noqa: BLE001
        return {"status": 0, "error": str(exc), "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = body[:500]
    return {
        "status": status,
        "bytes": len(body.encode("utf-8")),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "body": parsed,
    }


def wait_health(base, timeout=50):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=1) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return False


def signals(payload):
    """Count obvious data rows in a response without assuming one schema."""
    if isinstance(payload, list):
        return {"kind": "list", "rows": len(payload)}
    if not isinstance(payload, dict):
        return {"kind": type(payload).__name__}
    found = {}
    for key in ("rows", "items", "points", "dates", "days", "series", "members", "leaders"):
        value = payload.get(key)
        if isinstance(value, list):
            found[key] = len(value)
    for key in ("as_of", "trade_date", "latest_date", "generated_at", "date", "latest_observation_date"):
        if payload.get(key) is not None:
            found[key] = payload[key]
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--research-db", default=str(ROOT / "backend" / "data" / "b1_research.db"))
    parser.add_argument("--port", type=int, default=8023)
    parser.add_argument("--out", default=str(ROOT / "reports" / "s6_10"))
    args = parser.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    research = Path(args.research_db).resolve()
    if not research.exists():
        raise SystemExit(f"research DB missing: {research}")
    digest_before = hashlib.sha256()
    with research.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest_before.update(chunk)
    research_sha_before = digest_before.hexdigest()

    stage = Path(tempfile.mkdtemp(prefix="s6_10-core-"))
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
        "c.execute('INSERT OR REPLACE INTO app_user(username,password_hash,is_admin,created_at) VALUES(?,?,?,datetime(\"now\"))',('coreadmin',hash_password('staging-password-123'),1));"
        "c.execute('INSERT OR REPLACE INTO app_user(username,password_hash,is_admin,created_at) VALUES(?,?,?,datetime(\"now\"))',('coreuser',hash_password('staging-password-123'),0));"
        "c.commit(); c.close(); print('user db ready')"
    )
    init = subprocess.run([str(VENV_PY), "-c", init_code], cwd=str(ROOT / "backend"), env=env, capture_output=True, text=True, encoding="utf-8")
    if init.returncode != 0:
        raise SystemExit(init.stderr[-1000:])

    log_path = out / "core-business-server.log"
    process = subprocess.Popen(
        [str(VENV_PY), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(args.port), "--log-level", "warning"],
        cwd=str(ROOT),
        env=env,
        stdout=log_path.open("wb"),
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{args.port}"
    results = []
    try:
        if not wait_health(base):
            raise SystemExit("server did not become healthy")
        jar = CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        login = http(opener, f"{base}/api/auth/login", "POST", {"username": "coreadmin", "password": PASSWORD})
        if login["status"] != 200:
            raise SystemExit(f"login failed: {login}")
        me = http(opener, f"{base}/api/auth/me")
        endpoints = [
            ("market_dates", "/api/market/dates?days=10"),
            ("market_overview", "/api/market/overview"),
            ("limit_up_pool", "/api/limit/pool?type=up"),
            ("limit_down_pool", "/api/limit/pool?type=down"),
            ("limit_broken_pool", "/api/limit/pool?type=broken"),
            ("promotion", "/api/limit/promotion?days=15"),
            ("sector_heat", "/api/sectors/heat"),
            ("sector_rotation", "/api/sectors/rotation?taxonomy=kph_industry&days=10&top=10&metric=pct_chg"),
            ("sentiment_next_day", "/api/sentiment/next-day?days=60&group=first_board&metric=limit_up_count&include_pending=true"),
            ("quality_overview", "/api/quality/overview?days=30"),
            ("workbench", "/api/research/workbench?mode=OR&market=all&view=overview&limit=50&offset=0"),
            ("research_meta", "/api/research/meta"),
            ("research_trend", "/api/research/trend?date=2026-09-24&limit=50"),
            ("research_low_start", "/api/research/low-start?date=2026-09-24&limit=50"),
            ("research_sector_state", "/api/research/sector-state?date=2026-09-24&taxonomy=kph_industry&limit=50"),
            ("stock_profile", "/api/stock/000001"),
            ("stock_context", "/api/stock/000001/research-context"),
            ("stock_daily", "/api/stock/000001/daily?days=30&period=day&fq=qfq"),
            ("watchlist_admin", "/api/watchlist"),
        ]
        for name, path in endpoints:
            response = http(opener, f"{base}{path}", timeout=40)
            body = response.get("body")
            results.append(
                {
                    "name": name,
                    "path": path,
                    "status": response["status"],
                    "bytes": response.get("bytes"),
                    "elapsed_ms": response.get("elapsed_ms"),
                    "signals": signals(body) if isinstance(body, (dict, list)) else {"kind": "text"},
                    "error": response.get("error"),
                }
            )

        # cross-account isolation: admin adds 000001, normal user must not see it
        add = http(opener, f"{base}/api/watchlist", "POST", {"code": "000001"})
        admin_watch = http(opener, f"{base}/api/watchlist")
        jar_b = CookieJar()
        opener_b = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar_b))
        http(opener_b, f"{base}/api/auth/login", "POST", {"username": "coreuser", "password": PASSWORD})
        user_watch = http(opener_b, f"{base}/api/watchlist")
        admin_forbidden = http(opener_b, f"{base}/api/admin/status")
        # session expiry: logout then /auth/me must be 401
        http(opener, f"{base}/api/auth/logout", "POST")
        after_logout = http(opener, f"{base}/api/auth/me")

        # real expiry: seed an expired session + an absolute-limit session in the temp
        # user DB, then call /api/auth/me with each token (must be 401, not 200).
        expired_status = {}
        now = datetime.now()
        conn = sqlite3.connect(user_db)
        try:
            uid = conn.execute(
                "SELECT id FROM app_user WHERE username='coreuser'"
            ).fetchone()[0]
            for label, created_delta, expires_delta in (
                ("expired", timedelta(days=31), timedelta(days=-1)),
                ("over_absolute", timedelta(days=100), timedelta(days=10)),
            ):
                token = f"s610-{label}-" + os.urandom(8).hex()
                conn.execute(
                    "INSERT INTO user_session(token_hash,user_id,created_at,expires_at) "
                    "VALUES(?,?,?,?)",
                    (
                        hashlib.sha256(token.encode()).hexdigest(),
                        uid,
                        (now - created_delta).strftime("%Y-%m-%d %H:%M:%S.%f"),
                        (now + expires_delta).strftime("%Y-%m-%d %H:%M:%S.%f"),
                    ),
                )
                conn.commit()
                request = urllib.request.Request(
                    f"{base}/api/auth/me",
                    headers={"Cookie": f"fupan_session={token}"},
                )
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        expired_status[label] = response.status
                except urllib.error.HTTPError as exc:
                    expired_status[label] = exc.code
            conn.commit()
        finally:
            conn.close()

        results.append(
            {
                "name": "auth_boundaries",
                "status": 200,
                "me": me["status"],
                "watchlist_add": add["status"],
                "admin_watchlist": len(admin_watch.get("body") or []) if isinstance(admin_watch.get("body"), list) else None,
                "user_watchlist": user_watch.get("body"),
                "non_admin_status": admin_forbidden["status"],
                "after_logout_me": after_logout["status"],
                "expired_session_status": expired_status,
            }
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()

    digest_after = hashlib.sha256()
    with research.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest_after.update(chunk)
    research_sha_after = digest_after.hexdigest()

    data = {
        "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "environment": "isolated split server; research DB mode=ro; temp user DB; external/scheduler off",
        "research_db": str(research),
        "research_sha256_before": research_sha_before,
        "research_sha256_after": research_sha_after,
        "research_unchanged": research_sha_before == research_sha_after,
        "endpoints": results,
    }
    (out / "core-business-acceptance.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# S6.10 核心业务最终验收（真实研究库只读）",
        "",
        f"- 时间：{data['generated_at']}",
        f"- 研究库：`{research}`（mode=ro；before/after SHA256 一致：{data['research_unchanged']}）",
        "- 用户库：临时库；外部取数/调度关闭",
        "",
        "| 接口 | 状态 | 字节 | 关键信号 |",
        "| --- | --- | --- | --- |",
    ]
    for item in results[:-1]:
        lines.append(f"| {item['name']} | {item['status']} | {item['bytes']} | `{json.dumps(item['signals'], ensure_ascii=False)}` |")
    lines.extend(["", "## 认证/权限边界", "", f"`{json.dumps(results[-1], ensure_ascii=False)}`", "", "## 说明", "",
                  "- 本测试读取真实研究库的只读副本语义（`mode=ro`），未写入研究库；",
                  "- 空列表不等于失败，但报告中保留空值，不把空数据写成成功；",
                  "- 浏览器页面渲染与交互见 `browser/`（合成 Staging）与本轮核心流程 smoke。"])
    (out / "core-business-acceptance.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"research_unchanged": data["research_unchanged"], "endpoints": len(results) - 1}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
