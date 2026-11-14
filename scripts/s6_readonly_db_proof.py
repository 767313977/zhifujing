"""S6-B4：SQLite 只读隔离实测（在**临时副本**上，不触碰原库）。

为什么要单独做：`mode=ro` 在应用层写代码时很容易被误以为「只要不调用 commit 就是只读」。
这里用真实 SQLite 连接证明：INSERT / UPDATE / DELETE / DDL / PRAGMA 写 / VACUUM
在只读连接上全部被拒绝；同时用**可写副本**做对照，证明这些语句本身是合法的
（否则「失败」可能只是因为表不存在或语法错误）。

安全约束：
- 源库只以 `mode=ro` 打开并做 SQLite backup API 复制，**不执行任何写**；
- 写测试只在临时目录副本上执行；
- 结束前记录源库 size / mtime / SHA256，确认未变化。

用法：
    python scripts/s6_readonly_db_proof.py --source backend/data/forward_research.db \
        --out reports/s6/runtime/readonly-db-proof.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CN_TZ = timezone(timedelta(hours=8))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stat(path: Path) -> dict:
    info = path.stat()
    return {
        "size": info.st_size,
        "mtime": datetime.fromtimestamp(info.st_mtime, CN_TZ).isoformat(timespec="seconds"),
    }


def _copy(src: Path, dst: Path) -> None:
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _attempt(connection: sqlite3.Connection, statement: str, params: tuple = ()) -> dict:
    try:
        connection.execute(statement, params)
    except sqlite3.OperationalError as exc:
        return {"statement": statement, "blocked": True, "error": str(exc)}
    except sqlite3.DatabaseError as exc:
        return {"statement": statement, "blocked": True, "error": f"{type(exc).__name__}: {exc}"}
    else:
        return {"statement": statement, "blocked": False, "error": None}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(ROOT / "backend" / "data" / "forward_research.db"))
    parser.add_argument("--out", default=str(ROOT / "reports" / "s6" / "runtime" / "readonly-db-proof.json"))
    args = parser.parse_args()

    source_path = Path(args.source)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    before = {"stat": _stat(source_path), "sha256": _sha256(source_path)}
    payload: dict = {
        "commit_sha": subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip(),
        "branch": subprocess.run(
            ["git", "-C", str(ROOT), "branch", "--show-current"], capture_output=True, text=True
        ).stdout.strip(),
        "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "environment": "Windows 11 开发机；临时目录副本",
        "command": "python scripts/s6_readonly_db_proof.py",
        "exit_code": 0,
        "tool_version": "s6_readonly_db_proof/1.0",
        "result": {},
        "evidence_files": [str(out_path.relative_to(ROOT))],
        "limitations": [
            "写测试在临时副本上执行；源库仅以 mode=ro 打开并被 backup 读取。",
            "只读模式证明的是 SQLite 的 mode=ro 语义，不代表应用引擎已经使用只读连接。",
        ],
    }

    with tempfile.TemporaryDirectory(prefix="s6-readonly-") as tmp:
        copy_path = Path(tmp) / "copy.db"
        _copy(source_path, copy_path)

        # 1) 可写对照：证明语句本身合法
        writable = sqlite3.connect(copy_path)
        control: list[dict] = []
        try:
            control.append(_attempt(writable, "CREATE TABLE __s6_write_control (id INTEGER PRIMARY KEY, v TEXT)"))
            control.append(_attempt(writable, "INSERT INTO __s6_write_control (v) VALUES ('x')"))
            control.append(_attempt(writable, "UPDATE __s6_write_control SET v='y' WHERE v='x'"))
            control.append(_attempt(writable, "DELETE FROM __s6_write_control WHERE v='y'"))
            writable.commit()
            # 留一张有行的表给只读测试
            writable.execute("CREATE TABLE __s6_probe (id INTEGER PRIMARY KEY, v TEXT)")
            writable.execute("INSERT INTO __s6_probe (v) VALUES ('row')")
            writable.commit()
        finally:
            writable.close()

        # 2) 只读连接：所有写必须被拒绝
        readonly = sqlite3.connect(
            f"file:{copy_path.as_posix()}?mode=ro&immutable=1",
            uri=True,
            isolation_level=None,  # 关掉隐式事务，VACUUM 才能测到真正的只读拒绝
        )
        blocked: list[dict] = []
        try:
            blocked.append(_attempt(readonly, "INSERT INTO __s6_probe (v) VALUES ('new')"))
            blocked.append(_attempt(readonly, "UPDATE __s6_probe SET v='changed' WHERE id=1"))
            blocked.append(_attempt(readonly, "DELETE FROM __s6_probe WHERE id=1"))
            blocked.append(_attempt(readonly, "CREATE TABLE __s6_ddl (id INTEGER)"))
            blocked.append(_attempt(readonly, "DROP TABLE __s6_probe"))
            blocked.append(_attempt(readonly, "PRAGMA user_version = 7"))
            blocked.append(_attempt(readonly, "VACUUM"))
            integrity = readonly.execute("PRAGMA integrity_check").fetchone()[0]
            rows = readonly.execute("SELECT COUNT(*) FROM __s6_probe").fetchone()[0]
        finally:
            readonly.close()

        payload["result"] = {
            "source": str(source_path),
            "copy_used": "临时目录（脚本结束即删除）",
            "writable_control": {
                "all_succeeded": all(not item["blocked"] for item in control),
                "attempts": control,
            },
            "readonly_attempts": {
                "all_blocked": all(item["blocked"] for item in blocked),
                "attempts": blocked,
            },
            "readonly_integrity_check": integrity,
            "readonly_row_count": rows,
            "wal": {"exists": (Path(str(copy_path) + "-wal")).exists()},
            "shm": {"exists": (Path(str(copy_path) + "-shm")).exists()},
        }

    after = {"stat": _stat(source_path), "sha256": _sha256(source_path)}
    payload["result"]["source_unchanged"] = before == after
    payload["result"]["source_before"] = before
    payload["result"]["source_after"] = after
    payload["result"]["verdict"] = (
        "PASS"
        if payload["result"]["writable_control"]["all_succeeded"]
        and payload["result"]["readonly_attempts"]["all_blocked"]
        and payload["result"]["source_unchanged"]
        and payload["result"]["readonly_integrity_check"] == "ok"
        else "FAIL"
    )
    payload["exit_code"] = 0 if payload["result"]["verdict"] == "PASS" else 1

    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"verdict={payload['result']['verdict']} -> {out_path}")
    print(
        "writable_ok=", payload["result"]["writable_control"]["all_succeeded"],
        "readonly_blocked=", payload["result"]["readonly_attempts"]["all_blocked"],
        "source_unchanged=", payload["result"]["source_unchanged"],
    )
    return payload["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
