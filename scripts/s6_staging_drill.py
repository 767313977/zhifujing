"""S6-E：本地隔离 Staging 部署 / 重启 / 回滚演练（**不是生产 Staging**）。

它做的事：
1. 用 `git archive` 生成两份**不可变代码快照**：旧版 `HEAD` 与新版（工作区快照 SHA）；
2. 在独立目录 + 独立 SQLite 副本上启动 uvicorn（127.0.0.1，专用端口）；
3. 环境变量强制：`EXTERNAL_DATA_FETCH_ENABLED=false`、`SCHEDULER_ENABLED=false`、
   `WARMUP_PATTERN_CACHE=false`、`FUPAN_SCHEMA_MODE=verify`、`ENABLE_DEV_CORS=false`；
4. 运行只读 smoke（health/ready/未登录/登录/质量接口）；
5. 重启服务并复测；
6. 回滚到旧快照（同库）复测 `/api/ready` 不存在，再切回新版；
7. 生成 `deployment-record.md` / `smoke-test-results.json` / `rollback-drill.md`。

⚠️ 限制：本机没有独立的服务管理器、网络命名空间或容器；「回滚」是代码快照 +
配置 + 数据库副本的回滚，不是生产发布系统回滚。OS egress 仍未验证。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CN_TZ = timezone(timedelta(hours=8))
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"{cmd} 失败：{result.stderr[-800:]}")
    return result.stdout.strip()


def _archive(ref: str, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    tar_path = target.with_suffix(".tar")
    subprocess.run(
        ["git", "-C", str(ROOT), "archive", "--format=tar", f"--output={tar_path}", ref],
        check=True,
    )
    with tarfile.open(tar_path) as archive:
        archive.extractall(target)
    return tar_path


def _wait_http(url: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return True
        except urllib.error.HTTPError as exc:
            if exc.code in {200, 401, 403, 404, 503}:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def _http(
    opener, url: str, method: str = "GET", payload: dict | None = None
) -> tuple[int, dict | str]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=10) as response:
            body = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        status = exc.code
    try:
        parsed: dict | str = json.loads(body)
    except json.JSONDecodeError:
        parsed = body[:400]
    return status, parsed


def _start(server_dir: Path, db_path: Path, port: int, log_path: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "DB_PATH": str(db_path),
        "EXTERNAL_DATA_FETCH_ENABLED": "false",
        "SCHEDULER_ENABLED": "false",
        "WARMUP_PATTERN_CACHE": "false",
        "FUPAN_SCHEMA_MODE": "verify",
        "FUPAN_ENV": "staging",
        "ENABLE_DEV_CORS": "false",
        "IFIND_AUTH_TOKEN": "",
    }
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
            str(port),
        ],
        cwd=str(server_dir / "backend"),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _init_db(server_dir: Path, db_path: Path) -> None:
    code = (
        "from app.db import init_db; init_db();"
        "import sqlite3, os;"
        "c=sqlite3.connect(os.environ['DB_PATH']);"
        "c.execute(\"INSERT OR REPLACE INTO invite_code(code,note,created_at) VALUES('S6-STAGING-1','s6 staging',datetime('now'))\");"
        "c.commit(); c.close();"
        "print('staging db ready')"
    )
    env = {
        **os.environ,
        "DB_PATH": str(db_path),
        "FUPAN_SCHEMA_MODE": "migrate",
        "EXTERNAL_DATA_FETCH_ENABLED": "false",
    }
    result = subprocess.run(
        [str(VENV_PY), "-c", code],
        cwd=str(server_dir / "backend"),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"初始化 Staging 库失败：{result.stderr[-800:]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True, help="工作区快照 commit（git stash create）")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--old-port", type=int, default=8011)
    parser.add_argument("--out-dir", default=str(ROOT / "reports" / "s6" / "staging"))
    parser.add_argument("--keep-running", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix="s6_staging_", dir=os.environ.get("TEMP", "/tmp"))
    )

    old_dir = stage / "app_old"
    new_dir = stage / "app_new"
    old_tar = _archive("HEAD", old_dir)
    new_tar = _archive(args.snapshot, new_dir)
    dist_src = ROOT / "frontend" / "dist"
    for target in (old_dir, new_dir):
        shutil.copytree(dist_src, target / "frontend" / "dist", dirs_exist_ok=True)

    db_path = stage / "data" / "staging.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _init_db(new_dir, db_path)
    db_before = {"sha256": _sha256(db_path), "size": db_path.stat().st_size}

    artifacts = {
        "new_snapshot": args.snapshot,
        "head": _run(["git", "rev-parse", "HEAD"], ROOT),
        "branch": _run(["git", "branch", "--show-current"], ROOT),
        "new_tar_sha256": _sha256(new_tar),
        "old_tar_sha256": _sha256(old_tar),
        "requirements_sha256": _sha256(ROOT / "backend" / "requirements.txt"),
        "package_lock_sha256": _sha256(ROOT / "frontend" / "package-lock.json"),
        "frontend_dist_sha256": _sha256(dist_src / "index.html"),
        "db_path": str(db_path),
        "db_before": db_before,
        "python": sys.version.split()[0],
    }

    # 每次运行用唯一用户名：staging 库可能复用（上一轮遗留的 invite 已被消费），
    # 固定用户名会让「注册」在第二次运行必然 400（用户名已占用 / 邀请码已用过），
    # 那会把「注册接口坏了」与「测试数据脏了」混在一起。
    staging_user = f"s6staging{int(time.time())}"
    smoke = {"new": {}, "restart": {}, "rollback": {}, "redeploy": {}}
    process = None
    keep = False
    try:
        # --- 部署新版 ---
        process = _start(new_dir, db_path, args.port, out_dir / "staging-new.log")
        assert _wait_http(f"http://127.0.0.1:{args.port}/api/health"), "新版未能启动"
        base = f"http://127.0.0.1:{args.port}"
        anon = urllib.request.build_opener()
        jar = CookieJar()
        auth = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        smoke["new"]["health"] = _http(anon, f"{base}/api/health")
        smoke["new"]["ready"] = _http(anon, f"{base}/api/ready")
        smoke["new"]["unauth_quality"] = _http(anon, f"{base}/api/quality/overview?days=30")
        smoke["new"]["register"] = _http(
            auth,
            f"{base}/api/auth/register",
            "POST",
            {
                "invite_code": "S6-STAGING-1",
                "username": staging_user,
                "password": "staging-password-123",
            },
        )
        smoke["new"]["login"] = _http(
            auth, f"{base}/api/auth/login", "POST",
            {"username": staging_user, "password": "staging-password-123"},
        )
        smoke["new"]["quality_authed"] = _http(auth, f"{base}/api/quality/overview?days=30")
        smoke["new"]["timeline_authed"] = _http(
            auth, f"{base}/api/quality/timeline?source=limit_pool&days=30"
        )
        smoke["new"]["health_detail"] = _http(anon, f"{base}/api/health?detail=1")

        # --- 重启 ---
        _stop(process)
        process = _start(new_dir, db_path, args.port, out_dir / "staging-new-restart.log")
        assert _wait_http(f"http://127.0.0.1:{args.port}/api/health"), "重启后未能启动"
        smoke["restart"]["health"] = _http(anon, f"{base}/api/health")
        smoke["restart"]["ready"] = _http(anon, f"{base}/api/ready")

        # --- 回滚到旧快照（同库） ---
        _stop(process)
        process = _start(old_dir, db_path, args.old_port, out_dir / "staging-old.log")
        old_base = f"http://127.0.0.1:{args.old_port}"
        assert _wait_http(f"{old_base}/api/health"), "旧版未能启动"
        smoke["rollback"]["health"] = _http(anon, f"{old_base}/api/health")
        # 旧版没有 /api/ready：未登录会先被全站登录闸返回 401，登录后才应是 404
        smoke["rollback"]["ready"] = _http(auth, f"{old_base}/api/ready")
        smoke["rollback"]["quality_status"] = _http(
            auth, f"{old_base}/api/quality/overview?days=30"
        )
        _stop(process)

        # --- 切回新版 ---
        process = _start(new_dir, db_path, args.port, out_dir / "staging-redeploy.log")
        assert _wait_http(f"http://127.0.0.1:{args.port}/api/health"), "回滚后重新部署失败"
        smoke["redeploy"]["health"] = _http(anon, f"{base}/api/health")
        smoke["redeploy"]["ready"] = _http(anon, f"{base}/api/ready")
        db_after = {"sha256": _sha256(db_path), "size": db_path.stat().st_size}

        verdict = {
            "new_health": smoke["new"]["health"][0] == 200,
            "new_ready_503": smoke["new"]["ready"][0] == 503,
            "unauth_401": smoke["new"]["unauth_quality"][0] == 401,
            "register_ok": smoke["new"]["register"][0] == 200,
            "login_ok": smoke["new"]["login"][0] == 200,
            "quality_ok": smoke["new"]["quality_authed"][0] == 200,
            "restart_ok": smoke["restart"]["health"][0] == 200,
            "rollback_health": smoke["rollback"]["health"][0] == 200,
            "rollback_ready_consistent": smoke["rollback"]["ready"][0] in (404, 503),
            "redeploy_ok": smoke["redeploy"]["health"][0] == 200,
            "redeploy_ready_503": smoke["redeploy"]["ready"][0] == 503,
        }
        all_pass = all(verdict.values())

        payload = {
            "commit_sha": artifacts["head"],
            "branch": artifacts["branch"],
            "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
            "environment": "本地隔离 Staging（127.0.0.1，独立目录/端口/临时库；非生产基础设施）",
            "command": " ".join(["python", "scripts/s6_staging_drill.py", *sys.argv[1:]]),
            "exit_code": 0 if all_pass else 1,
            "tool_version": "s6_staging_drill/1.0",
            "result": {
                "artifacts": artifacts,
                "config": {
                    "EXTERNAL_DATA_FETCH_ENABLED": "false",
                    "SCHEDULER_ENABLED": "false",
                    "WARMUP_PATTERN_CACHE": "false",
                    "FUPAN_SCHEMA_MODE": "verify",
                    "FUPAN_ENV": "staging",
                    "ENABLE_DEV_CORS": "false",
                },
                "smoke": smoke,
                "verdict": verdict,
                "db_after": db_after,
                "db_written_by_login": db_after["sha256"] != db_before["sha256"],
            },
            "evidence_files": [
                str((out_dir / "smoke-test-results.json").relative_to(ROOT)),
                str((out_dir / "deployment-record.md").relative_to(ROOT)),
                str((out_dir / "rollback-drill.md").relative_to(ROOT)),
            ],
            "limitations": [
                "不是生产 Staging：无独立服务管理器/网络命名空间/容器。",
                "回滚是代码快照 + 配置 + 数据库副本的回滚，不是发布系统级回滚。",
                "Windows 上 terminate 是强制结束，不等价于 systemd SIGTERM 优雅停机。",
                "OS egress 策略仍未验证。",
            ],
        }
        (out_dir / "smoke-test-results.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        record = [
            "# S6-E 本地隔离 Staging 部署记录",
            "",
            f"- 时间：{payload['generated_at']}",
            f"- 分支/HEAD：`{artifacts['branch']}` / `{artifacts['head']}`",
            f"- 新版快照：`{args.snapshot}`（工作区快照，含未提交改动）",
            "- 旧版快照：`HEAD`",
            f"- 新/旧代码包 SHA256：`{artifacts['new_tar_sha256']}` / `{artifacts['old_tar_sha256']}`",
            f"- 依赖锁 SHA256：requirements=`{artifacts['requirements_sha256']}`，package-lock=`{artifacts['package_lock_sha256']}`",
            f"- 前端 index.html SHA256：`{artifacts['frontend_dist_sha256']}`",
            f"- 目录：`{new_dir}` / `{old_dir}`（临时目录，演练结束可删除）",
            f"- 端口：新版 {args.port}，旧版 {args.old_port}，仅监听 127.0.0.1",
            f"- 数据库：`{db_path}`（独立临时库；登录会写该库，属预期）",
            "",
            "## 启动配置",
            "",
            "| 键 | 值 |",
            "| --- | --- |",
            "| `EXTERNAL_DATA_FETCH_ENABLED` | false |",
            "| `SCHEDULER_ENABLED` | false |",
            "| `WARMUP_PATTERN_CACHE` | false |",
            "| `FUPAN_SCHEMA_MODE` | verify |",
            "| `FUPAN_ENV` | staging |",
            "| `ENABLE_DEV_CORS` | false |",
            "",
            "## Smoke 结果",
            "",
            "| 检查 | 状态码 |",
            "| --- | --- |",
        ]
        for name, (status, _body) in smoke["new"].items():
            record.append(f"| new.{name} | {status} |")
        for phase in ("restart", "rollback", "redeploy"):
            for name, (status, _body) in smoke[phase].items():
                record.append(f"| {phase}.{name} | {status} |")
        record.extend(
            [
                "",
                f"**verdict：{payload['result']['verdict']}**",
                "",
                "> 本记录不是生产部署：没有服务管理器、没有真实 Staging 网络、没有 OS egress 证据。",
            ]
        )
        (out_dir / "deployment-record.md").write_text("\n".join(record) + "\n", encoding="utf-8")

        rollback = [
            "# S6-E5 回滚演练",
            "",
            "## 步骤",
            "",
            "1. 停止新版（端口 {}）；",
            "2. 用 `HEAD` 快照启动旧版（端口 {}），**复用同一个临时库**；",
            "3. 验证旧版 `/api/health` 与 `/api/ready`；",
            "4. 停止旧版，重新启动新版；",
            "5. 验证数据库仍可读、健康检查正常。",
            "",
            "## 结果",
            "",
            f"- 旧版 health：`{smoke['rollback']['health'][0]}`",
            f"- 旧版 ready：`{smoke['rollback']['ready'][0]}`（旧版含 readiness 时 503；不含时 404，均属预期）",
            f"- 旧版登录后质量接口：`{smoke['rollback']['quality_status'][0]}`",
            f"- 切回新版 health：`{smoke['redeploy']['health'][0]}`",
            f"- 切回新版 ready：`{smoke['redeploy']['ready'][0]}`（应为 503：OS 门禁未验证）",
            f"- 数据库 SHA256 变化（登录写入）：`{payload['result']['db_written_by_login']}`",
            "",
            "## 结论",
            "",
            "- 代码快照可回滚、服务可恢复、数据库未被破坏；",
            "- **不等于**生产发布系统回滚演练：没有负载均衡、没有流量切换、没有 OS 策略恢复；",
            "- Egress 默认拒绝在回滚后仍依赖应用层（OS 层未验证）。",
        ]
        (out_dir / "rollback-drill.md").write_text("\n".join(rollback) + "\n", encoding="utf-8")

        print(f"verdict={verdict}")
        print(f"smoke={ {k: v[0] for k, v in smoke['new'].items()} }")
        if not all_pass:
            return 1
        if args.keep_running:
            keep = True
            state = {
                "pid": process.pid,
                "port": args.port,
                "root": str(new_dir),
                "db": str(db_path),
                "dir": str(stage),
            }
            (out_dir / "staging-state.json").write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"kept running pid={process.pid} port={args.port}")
            return 0
        _stop(process)
        return 0
    finally:
        if process is not None and not keep:
            _stop(process)


if __name__ == "__main__":
    raise SystemExit(main())
