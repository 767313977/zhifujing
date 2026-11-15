"""S7（S6.4）：拆分形态 Staging 短窗 soak + 故障注入。

**明确不是 24 小时 soak。** 本脚本只做：

1. 在隔离目录建「Research DB（只读）+ User DB（可写）」；
2. 以拆分形态启动 uvicorn（外部取数/scheduler/预热全部关闭）；
3. 在时间窗内混合跑：研究读、用户写、研究写拒绝路径；
4. （可选）故障注入：SIGTERM、SIGKILL + 重启、User DB 只读、Research DB 缺失、
   User DB 写锁争用；
5. 输出 `reports/s6-followup/s64/soak-metrics.json` 与 `fault-injection.json`。

用法：
    python scripts/s7_soak_split.py --minutes 3
    python scripts/s7_soak_split.py --minutes 2 --faults
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
CN_TZ = timezone(timedelta(hours=8))
USER = "s7staging"
PASSWORD = "staging-password-123"
PORT = 8021

RESEARCH_READS = [
    "/api/health",
    "/api/ready",
    "/api/market/overview",
    "/api/limit/promotion?days=15",
    "/api/sentiment/next-day?days=60&group=first_board&metric=limit_up_count&include_pending=true",
    "/api/research/workbench?mode=AND&market=all&view=overview&limit=50&offset=0",
]
RESEARCH_WRITE_PATHS = ["/api/watchlist/sync", "/api/stock/000001/sync"]
USER_WRITES = [
    ("POST", "/api/watchlist", {"code": "000001"}),
    ("DELETE", "/api/watchlist/000001", None),
    ("PUT", "/api/risk/pref", {"max_position_pct": 25.0, "max_theme_pct": 45.0,
                               "heat_consecutive": 6, "heat_temperature": 80,
                               "strict_mode": False}),
]


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * q)))], 2)


def _build_dbs(directory: Path) -> tuple[Path, Path]:
    from datetime import date as _date

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db_roles import user_table_objects
    from app.models import (
        AppUser,
        Base,
        LimitPool,
        MarketSentiment,
        StockBasic,
        StockDaily,
        TradeCalendar,
    )
    from app.services.auth import hash_password

    research = directory / "research.db"
    user = directory / "user.db"
    user_names = {table.name for table in user_table_objects()}
    research_tables = [
        table for table in Base.metadata.sorted_tables if table.name not in user_names
    ]

    engine = create_engine(f"sqlite:///{research}", future=True)
    Base.metadata.create_all(engine, tables=research_tables)
    with Session(engine) as session:
        session.add(StockBasic(code="000001", name="平安银行", industry="银行"))
        session.add(
            StockDaily(
                code="000001", trade_date=_date(2026, 11, 14), close=10.5, pct_chg=1.2
            )
        )
        session.add(TradeCalendar(trade_date=_date(2026, 11, 14)))
        session.add(
            MarketSentiment(
                trade_date=_date(2026, 11, 14),
                limit_up_count=40,
                limit_down_count=5,
                max_consecutive=4,
            )
        )
        session.add(
            LimitPool(
                trade_date=_date(2026, 11, 14),
                code="000001",
                name="平安银行",
                pool_type="up",
                pct_chg=10.0,
            )
        )
        session.commit()
    engine.dispose()

    engine = create_engine(f"sqlite:///{user}", future=True)
    Base.metadata.create_all(engine, tables=user_table_objects())
    with Session(engine) as session:
        session.add(
            AppUser(
                username=USER,
                password_hash=hash_password(PASSWORD),
                is_admin=True,
            )
        )
        session.commit()
    engine.dispose()
    return research, user


def _env(research: Path, user: Path) -> dict:
    return {
        **os.environ,
        "DB_SPLIT_ENABLED": "true",
        "DB_PATH": str(research),
        "USER_DB_PATH": str(user),
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


def _start(research: Path, user: Path, log_path: Path) -> subprocess.Popen:
    log = log_path.open("ab")
    return subprocess.Popen(
        [
            str(VENV_PY),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=_env(research, user),
        stdout=log,
        stderr=log,
    )


def _wait_http(url: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status in {200, 401, 403, 404, 503}:
                    return True
        except urllib.error.HTTPError as exc:
            if exc.code in {200, 401, 403, 404, 503}:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _call(opener, url: str, method: str = "GET", payload: dict | None = None) -> tuple[int, float]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=25) as response:
            response.read(1024)
            return response.status, (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as exc:
        exc.read(1024)
        return exc.code, (time.perf_counter() - started) * 1000
    except Exception:
        return 0, (time.perf_counter() - started) * 1000


def _login(base: str):
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    request = urllib.request.Request(
        f"{base}/api/auth/login",
        data=json.dumps({"username": USER, "password": PASSWORD}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=10) as response:
        if response.status != 200:
            raise SystemExit(f"登录失败：{response.status}")
    return opener


def _integrity(path: Path) -> str:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()


def _soak(opener, base: str, minutes: float) -> dict:
    records: list[dict] = []
    user_writes_ok = 0
    user_writes_fail = 0
    denied_expected = 0
    readiness_failures = 0
    started_at = time.time()
    deadline = started_at + minutes * 60
    while time.time() < deadline:
        for path in RESEARCH_READS:
            status, elapsed = _call(opener, f"{base}{path}")
            records.append(
                {
                    "kind": "research_read",
                    "path": path,
                    "status": status,
                    "ms": round(elapsed, 2),
                    "t": round(time.time() - started_at, 2),
                }
            )
            if path == "/api/ready" and status == 503:
                readiness_failures += 1
        for method, path, payload in USER_WRITES:
            status, elapsed = _call(opener, f"{base}{path}", method, payload)
            records.append(
                {
                    "kind": "user_write",
                    "path": path,
                    "status": status,
                    "ms": round(elapsed, 2),
                    "t": round(time.time() - started_at, 2),
                }
            )
            if 200 <= status < 300:
                user_writes_ok += 1
            else:
                user_writes_fail += 1
        for path in RESEARCH_WRITE_PATHS:
            status, elapsed = _call(opener, f"{base}{path}", "POST")
            records.append(
                {
                    "kind": "research_write_denied",
                    "path": path,
                    "status": status,
                    "ms": round(elapsed, 2),
                    "t": round(time.time() - started_at, 2),
                }
            )
            if status == 503:
                denied_expected += 1
        time.sleep(0.2)

    by_path: dict[str, dict] = {}
    for record in records:
        bucket = by_path.setdefault(record["path"], {"count": 0, "statuses": {}, "times": []})
        bucket["count"] += 1
        bucket["statuses"][str(record["status"])] = bucket["statuses"].get(str(record["status"]), 0) + 1
        bucket["times"].append(record["ms"])

    times = [record["ms"] for record in records]
    ready_503 = sum(1 for r in records if r["path"] == "/api/ready" and r["status"] == 503)
    expected_503 = sum(1 for r in records if r["kind"] == "research_write_denied" and r["status"] == 503)
    errors_5xx = sum(
        1
        for r in records
        if r["status"] >= 500
        and not (r["path"] == "/api/ready" and r["status"] == 503)
        and not (r["kind"] == "research_write_denied" and r["status"] == 503)
    )
    transport = sum(1 for r in records if r["status"] == 0)
    return {
        "total_requests": len(records),
        "research_read": sum(1 for r in records if r["kind"] == "research_read"),
        "user_write_ok": user_writes_ok,
        "user_write_fail": user_writes_fail,
        "research_write_denied_503": denied_expected,
        "ready_503": ready_503,
        "readiness_failures": readiness_failures,
        "errors_5xx": errors_5xx,
        "transport_errors": transport,
        "latency_ms": {
            "p50": _percentile(times, 0.50),
            "p95": _percentile(times, 0.95),
            "p99": _percentile(times, 0.99),
            "max": round(max(times), 2) if times else None,
        },
        "by_path": {
            path: {
                "count": bucket["count"],
                "statuses": bucket["statuses"],
                "p95_ms": _percentile(bucket["times"], 0.95),
            }
            for path, bucket in by_path.items()
        },
        "expected_503_research_write": expected_503,
        "timeline": _timeline(records),
    }


def _timeline(records: list[dict]) -> list[dict]:
    """按分钟聚合，供短时 smoke 的 `short_smoke_timeline.jsonl` 使用。"""
    buckets: dict[int, list[dict]] = {}
    for record in records:
        minute = int(record.get("t", 0) // 60)
        buckets.setdefault(minute, []).append(record)
    timeline = []
    for minute in sorted(buckets):
        rows = buckets[minute]
        times = [row["ms"] for row in rows]
        unexpected_5xx = sum(
            1
            for row in rows
            if row["status"] >= 500
            and not (row["path"] == "/api/ready" and row["status"] == 503)
            and not (row["kind"] == "research_write_denied" and row["status"] == 503)
        )
        timeline.append(
            {
                "minute": minute,
                "requests": len(rows),
                "statuses": {
                    str(code): sum(1 for row in rows if row["status"] == code)
                    for code in sorted({row["status"] for row in rows})
                },
                "unexpected_5xx": unexpected_5xx,
                "p95_ms": _percentile(times, 0.95),
            }
        )
    return timeline


def _stop(process: subprocess.Popen, timeout: float = 20) -> int | None:
    if process.poll() is not None:
        return process.returncode
    process.send_signal(signal.SIGTERM)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            return process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            return None


def _rename_with_retry(source: Path, target: Path, attempts: int = 20) -> None:
    for _ in range(attempts):
        try:
            source.rename(target)
            return
        except PermissionError:
            time.sleep(0.5)
    raise PermissionError(f"无法重命名（文件仍被占用）：{source}")


def _get_json(opener, url: str) -> tuple[int, dict]:
    try:
        with opener.open(url, timeout=25) as response:
            return response.status, json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {}
    except Exception:
        return 0, {}


def _faults(base: str, user_db: Path, research_db: Path, log_path: Path) -> list[dict]:
    results: list[dict] = []

    def add(name: str, ok: bool, detail: dict) -> None:
        results.append({"name": name, **detail, "status": "PASS" if ok else "FAIL"})

    # 1) SIGTERM → 进程应退出，重启后 health 正常
    process = _start(research_db, user_db, log_path)
    _wait_http(f"{base}/api/health")
    opener = _login(base)
    process.send_signal(signal.SIGTERM)
    try:
        code = process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        code = -1
    add("SIGTERM", code is not None, {"exit_code": code})

    # 2) SIGKILL → 重启后 health 正常、User DB integrity ok
    process = _start(research_db, user_db, log_path)
    _wait_http(f"{base}/api/health")
    process.kill()
    process.wait(timeout=20)
    process = _start(research_db, user_db, log_path)
    healthy = _wait_http(f"{base}/api/health")
    add(
        "SIGKILL_RESTART",
        healthy and _integrity(user_db) == "ok",
        {"health": healthy, "user_db_integrity": _integrity(user_db)},
    )

    # 3) User DB 只读 → readiness 必须 FAIL（USER_DB_NOT_WRITABLE）
    os.chmod(user_db, stat.S_IREAD)
    try:
        status, payload = _get_json(opener, f"{base}/api/ready")
        reason = str(payload.get("reasons", {}).get("DATABASE_READ_ONLY_VERIFIED", ""))
        add(
            "USER_DB_READONLY_READINESS",
            status == 503 and "USER_DB_NOT_WRITABLE" in reason,
            {"http_status": status, "reason": reason},
        )
    finally:
        os.chmod(user_db, stat.S_IWRITE | stat.S_IREAD)

    # 4) Research DB 缺失 → **启动即 fail closed**（Windows 下运行中的库文件被锁，
    #    只能先停服务再改名，然后验证新进程拒绝启动）
    _stop(process)
    moved = research_db.with_suffix(".moved")
    _rename_with_retry(research_db, moved)
    probe = None
    try:
        probe = _start(research_db, user_db, log_path)
        time.sleep(8)
        exited = probe.poll() is not None
        status, _payload = _get_json(opener, f"{base}/api/ready")
        add(
            "RESEARCH_DB_MISSING_STARTUP_FAIL_CLOSED",
            exited and status == 0,
            {"process_exited": exited, "exit_code": probe.poll(), "ready_status": status},
        )
    finally:
        if probe is not None and probe.poll() is None:
            probe.kill()
            try:
                probe.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        _rename_with_retry(moved, research_db)
        process = _start(research_db, user_db, log_path)
        _wait_http(f"{base}/api/health")

    # 5) User DB 写锁争用：请求必须**有界**失败或等待后成功
    lock_connection = sqlite3.connect(user_db, timeout=1, check_same_thread=False)
    release = threading.Event()

    def hold_lock() -> None:
        lock_connection.execute("BEGIN IMMEDIATE")
        release.wait(timeout=25)
        lock_connection.rollback()
        lock_connection.close()

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    time.sleep(0.5)
    status, elapsed = _call(opener, f"{base}/api/watchlist", "POST", {"code": "000001"})
    release.set()
    holder.join(timeout=10)
    add(
        "USER_DB_LOCK_CONTENTION",
        elapsed < 20000 and (status >= 400 or status == 200),
        {"http_status": status, "elapsed_ms": round(elapsed, 2), "bounded_lt_20000ms": elapsed < 20000},
    )

    _stop(process)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=3)
    parser.add_argument("--faults", action="store_true")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="只启动拆分形态服务（浏览器验收用），Ctrl-C 退出；不跑 soak",
    )
    parser.add_argument("--out-dir", default=str(ROOT / "reports" / "s6-followup" / "s64"))
    args = parser.parse_args()

    allow = Path(tempfile.mkdtemp(prefix="s7-soak-"))
    research_db, user_db = _build_dbs(allow)
    os.chmod(research_db, stat.S_IREAD)
    log_path = allow / "server.log"
    base = f"http://127.0.0.1:{PORT}"
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    process = _start(research_db, user_db, log_path)
    try:
        if not _wait_http(f"{base}/api/health"):
            print(log_path.read_text(encoding="utf-8", errors="replace")[-2000:])
            return 1
        if args.serve:
            print(
                json.dumps(
                    {
                        "base": base,
                        "username": USER,
                        "password": PASSWORD,
                        "isolated_dir": str(allow),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            try:
                process.wait()
            except KeyboardInterrupt:
                pass
            return 0
        opener = _login(base)
        metrics = _soak(opener, base, args.minutes)

        # 故障注入会重启/杀掉进程，必须先把 soak 用的服务停掉，避免端口冲突
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
        faults = _faults(base, user_db, research_db, log_path) if args.faults else []
        ready_after = 0
        payload = {
            "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
            "branch": subprocess.run(
                ["git", "-C", str(ROOT), "branch", "--show-current"],
                capture_output=True, text=True,
            ).stdout.strip(),
            "commit_sha": subprocess.run(
                ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                capture_output=True, text=True,
            ).stdout.strip(),
            "environment": "本地隔离拆分 Staging（Research 只读 + User 可写）",
            "split_mode": True,
            "duration_minutes": args.minutes,
            "requested_24h": False,
            "status": "SOAK_NOT_24H",
            "metrics": metrics,
            "faults": faults,
            "user_db_integrity_after": _integrity(user_db),
            "research_db_integrity": _integrity(research_db),
            "ready_status_after": ready_after,
            "limitations": [
                "不是 24 小时 soak；S6_STAGING_SOAK_PASSED 仍为 NOT_RUN。",
                "本机无 OS 级网络隔离，Egress 拒绝只验证了应用层 503。",
                "未采集 CPU/RSS/文件描述符（未安装 psutil）。",
            ],
        }
        (out_dir / "soak-metrics.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (out_dir / "fault-injection.json").write_text(
            json.dumps(
                {
                    "generated_at": payload["generated_at"],
                    "environment": payload["environment"],
                    "faults": faults,
                    "status": "PASS" if faults and all(f["status"] == "PASS" for f in faults)
                    else ("NOT_RUN" if not faults else "FAIL"),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"metrics": metrics, "faults": faults}, ensure_ascii=False))
        ok = metrics["errors_5xx"] == 0 and metrics["transport_errors"] == 0
        ok = ok and (not faults or all(f["status"] == "PASS" for f in faults))
        return 0 if ok else 1
    finally:
        if process.poll() is None:
            process.kill()
        os.chmod(research_db, stat.S_IWRITE | stat.S_IREAD)
        if log_path.exists():
            shutil.copyfile(log_path, out_dir / "staging-server.log")
        # 只保留证据，不保留大文件副本
        shutil.rmtree(allow, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
