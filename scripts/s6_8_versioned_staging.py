"""S6.8 B 包：真实 OLD/NEW 版本 Staging 切换与回滚（单端口，独立临时库）。

与 S6.7 的 `s6_staging_drill.py` 的区别：旧/新目录分别来自**独立 git worktree**，
各自构建 `frontend/dist`，演练中核对「实际 serving 的 index/entry 哈希」，
而不是两边复制同一份产物。

用法：
    python scripts/s6_8_versioned_staging.py \
        --old-root C:\\...\\s6_8_versions\\old \
        --new-root C:\\...\\s6_8_versions\\new \
        --port 8012 --out reports/s6_8_9/staging
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
CN_TZ = timezone(timedelta(hours=8))
USER = "s7staging"
PASSWORD = "staging-password-123"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def http_json(url: str, timeout: float = 5.0) -> tuple[int, object | None]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:
            return exc.code, None
    except Exception:
        return 0, None


def http_bytes(url: str, timeout: float = 5.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except Exception:
        return 0, b""


def wait_http(url: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _ = http_json(url, timeout=1)
        if status in (200, 401, 403, 404, 503):
            return True
        time.sleep(0.3)
    return False


def env_for(root: Path, db_path: Path) -> dict:
    return {
        **os.environ,
        "DB_PATH": str(db_path),
        "EXTERNAL_DATA_FETCH_ENABLED": "false",
        "SCHEDULER_ENABLED": "false",
        "WARMUP_PATTERN_CACHE": "false",
        "FUPAN_SCHEMA_MODE": "verify",
        "FUPAN_ENV": "staging",
        "ENABLE_DEV_CORS": "false",
        "IFIND_AUTH_TOKEN": "",
        "PYTHONPATH": str(root / "backend"),
    }


def start(root: Path, db_path: Path, port: int, log_path: Path) -> subprocess.Popen:
    log = log_path.open("ab")
    process = subprocess.Popen(
        [
            str(VENV_PY),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(root / "backend"),
        env=env_for(root, db_path),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    if not wait_http(f"http://127.0.0.1:{port}/api/health"):
        process.kill()
        raise RuntimeError(f"{root.name} 未能启动，日志：{log_path}")
    return process


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def init_db(new_root: Path, db_path: Path) -> None:
    code = (
        "from app.db import init_db; init_db();"
        "import os, sqlite3;"
        "from app.services.auth import hash_password;"
        "c=sqlite3.connect(os.environ['DB_PATH']);"
        "c.execute('INSERT OR IGNORE INTO app_user(username,password_hash,is_admin,created_at) VALUES(?,?,?,datetime(\"now\"))',('s7staging',hash_password('staging-password-123'),1));"
        "c.execute('INSERT OR IGNORE INTO app_user(username,password_hash,is_admin,created_at) VALUES(?,?,?,datetime(\"now\"))',('s7userb',hash_password('staging-password-123'),0));"
        "c.commit(); c.close(); print('db ready')"
    )
    env = {
        **os.environ,
        "DB_PATH": str(db_path),
        "FUPAN_SCHEMA_MODE": "migrate",
        "EXTERNAL_DATA_FETCH_ENABLED": "false",
        "PYTHONPATH": str(new_root / "backend"),
    }
    result = subprocess.run(
        [str(VENV_PY), "-c", code],
        cwd=str(new_root / "backend"),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"初始化临时库失败：{result.stderr[-1000:]}")


def serving_manifest(base: str) -> dict:
    status, html = http_bytes(f"{base}/")
    if status != 200:
        raise RuntimeError(f"index.html 返回 {status}")
    text = html.decode("utf-8", "replace")
    refs = sorted(set(re.findall(r'(?:src|href)="(/assets/[^"]+)"', text)))
    assets = {}
    for ref in refs:
        code, body = http_bytes(f"{base}{ref}")
        assets[ref] = {"status": code, "sha256": sha256_bytes(body) if code == 200 else None}
    return {
        "index_status": status,
        "index_sha256": sha256_bytes(html),
        "assets": assets,
        "asset_count": len(assets),
    }


def login_check(base: str, expect: str, out_path: Path) -> dict:
    result = subprocess.run(
        [
            "node",
            "scripts/s6_8_login_check.mjs",
            "--base",
            base,
            "--expect",
            expect,
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {"pass": False, "raw_stdout": result.stdout[-1000:], "stderr": result.stderr[-1000:]}
    payload["exit_code"] = result.returncode
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-root", required=True)
    parser.add_argument("--new-root", required=True)
    parser.add_argument("--port", type=int, default=8012)
    parser.add_argument("--out", default=str(ROOT / "reports" / "s6_8_9" / "staging"))
    args = parser.parse_args()

    old_root = Path(args.old_root).resolve()
    new_root = Path(args.new_root).resolve()
    out = Path(args.out).resolve()
    logs = out / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    stage = Path(tempfile.mkdtemp(prefix="s6_8_staging_"))
    db_path = stage / "staging.db"
    init_db(new_root, db_path)
    backup = stage / "staging.backup.db"
    shutil.copyfile(db_path, backup)
    db_initial = {"sha256": sha256_file(db_path), "size": db_path.stat().st_size}
    backup_info = {"sha256": sha256_file(backup), "size": backup.stat().st_size}

    old_manifest = json.loads((out / "manifest-old.json").read_text(encoding="utf-8"))
    new_manifest = json.loads((out / "manifest-new.json").read_text(encoding="utf-8"))
    base = f"http://127.0.0.1:{args.port}"

    steps: list[dict] = []
    process = None

    def record(name: str, root: Path, expect: str) -> dict:
        nonlocal process
        started = datetime.now(CN_TZ).isoformat(timespec="seconds")
        process = start(root, db_path, args.port, logs / f"{name}.log")
        try:
            health = http_json(f"{base}/api/health")
            ready = http_json(f"{base}/api/ready")
            manifest = serving_manifest(base)
            login = login_check(base, expect, out / f"login-{name}.json")
            step = {
                "step": name,
                "version_root": str(root),
                "expected": expect,
                "started_at": started,
                "health_status": health[0],
                "ready_status": ready[0],
                "serving": manifest,
                "login": login,
            }
        finally:
            stop(process)
            process = None
        steps.append(step)
        return step

    old_step = record("01-old", old_root, "blank")
    new_step = record("02-new", new_root, "success")
    rollback_step = record("03-rollback-old", old_root, "blank")
    redeploy_step = record("04-redeploy-new", new_root, "success")

    # 实际 serving 的证据：index/entry 哈希必须分别与版本 manifest 对应
    def entry_sha(manifest: dict) -> str | None:
        for item in manifest["files"]:
            if re.match(r"assets/index-.*\.js$", item["path"]):
                return item["sha256"]
        return None

    old_entry = entry_sha(old_manifest)
    new_entry = entry_sha(new_manifest)
    serving_old = old_step["serving"]["assets"].get(
        next((ref for ref in old_step["serving"]["assets"] if "/assets/index-" in ref and ref.endswith(".js")), ""),
        {},
    ).get("sha256")
    serving_new = new_step["serving"]["assets"].get(
        next((ref for ref in new_step["serving"]["assets"] if "/assets/index-" in ref and ref.endswith(".js")), ""),
        {},
    ).get("sha256")
    serving_rollback = rollback_step["serving"]["assets"].get(
        next((ref for ref in rollback_step["serving"]["assets"] if "/assets/index-" in ref and ref.endswith(".js")), ""),
        {},
    ).get("sha256")
    serving_redeploy = redeploy_step["serving"]["assets"].get(
        next((ref for ref in redeploy_step["serving"]["assets"] if "/assets/index-" in ref and ref.endswith(".js")), ""),
        {},
    ).get("sha256")

    switch_ok = (
        old_manifest["manifest_sha256"] != new_manifest["manifest_sha256"]
        and serving_old == old_entry
        and serving_new == new_entry
        and serving_rollback == old_entry
        and serving_redeploy == new_entry
    )
    login_ok = (
        old_step["login"].get("pass") is True  # 旧版按预期复现白屏
        and new_step["login"].get("pass") is True  # 新版登录/刷新/深链/退出通过
        and rollback_step["login"].get("pass") is True
        and redeploy_step["login"].get("pass") is True
    )
    db_after = {"sha256": sha256_file(db_path), "size": db_path.stat().st_size}

    payload = {
        "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "environment": "本地隔离 Staging（单端口 127.0.0.1，独立临时库）",
        "port": args.port,
        "old_root": str(old_root),
        "new_root": str(new_root),
        "old_head": subprocess.run(
            ["git", "-C", str(old_root), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip(),
        "new_head": subprocess.run(
            ["git", "-C", str(new_root), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip(),
        "manifests": {
            "old": old_manifest["manifest_sha256"],
            "new": new_manifest["manifest_sha256"],
            "different": old_manifest["manifest_sha256"] != new_manifest["manifest_sha256"],
            "old_entry_sha256": old_entry,
            "new_entry_sha256": new_entry,
        },
        "db": {"initial": db_initial, "backup": backup_info, "after": db_after},
        "steps": steps,
        "verdict": {
            "version_manifest_different": old_manifest["manifest_sha256"] != new_manifest["manifest_sha256"],
            "serving_switches_old_new": switch_ok,
            "old_known_blank_reproduced": old_step["login"].get("pass") is True,
            "new_login_smoke_pass": new_step["login"].get("pass") is True,
            "rollback_serving_old": serving_rollback == old_entry,
            "redeploy_serving_new": serving_redeploy == new_entry,
            "all_pass": switch_ok and login_ok,
        },
        "limitations": [
            "单端口顺序切换，不是负载均衡/流量切换。",
            "旧版登录白屏是已知缺陷，作为版本确实切换的对照，不是回归失败。",
            "Windows 上 terminate 非 systemd 优雅停机；OS egress 未验证。",
        ],
    }
    (out / "version_switch.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# S6.8 B 包：真实 OLD/NEW 版本 Staging 切换与回滚",
        "",
        f"- 时间：{payload['generated_at']}",
        f"- OLD：`{payload['old_head']}`（manifest `{old_manifest['manifest_sha256']}`）",
        f"- NEW：`{payload['new_head']}`（manifest `{new_manifest['manifest_sha256']}`）",
        f"- 端口：{args.port}（独立临时库 `{db_path}`）",
        "",
        "## 步骤",
        "",
        "| 步骤 | 期望 | health | ready | serving entry SHA | 登录 smoke |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for step in steps:
        entry = next(
            (
                value["sha256"]
                for ref, value in step["serving"]["assets"].items()
                if "/assets/index-" in ref and ref.endswith(".js")
            ),
            None,
        )
        lines.append(
            f"| {step['step']} | {step['expected']} | {step['health_status']} | "
            f"{step['ready_status']} | `{entry}` | {step['login'].get('pass')} |"
        )
    lines.extend(
        [
            "",
            "## 结论",
            "",
            f"- OLD/NEW manifest 不同：**{payload['verdict']['version_manifest_different']}**",
            f"- 实际 serving 在 OLD→NEW→OLD→NEW 间切换：**{payload['verdict']['serving_switches_old_new']}**",
            f"- 旧版已知白屏复现（版本对照）：**{payload['verdict']['old_known_blank_reproduced']}**",
            f"- 新版登录/刷新/深层链接/退出 smoke：**{payload['verdict']['new_login_smoke_pass']}**",
            f"- 总判定：**{'PASS' if payload['verdict']['all_pass'] else 'FAIL'}**",
            "",
            "> 单端口顺序切换，不是生产发布系统/负载均衡回滚；OS egress 未验证。",
        ]
    )
    (out / "versioned-rollback-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(payload["verdict"], ensure_ascii=False))
    return 0 if payload["verdict"]["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
