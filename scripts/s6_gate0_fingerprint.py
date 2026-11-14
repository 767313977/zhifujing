"""S6 Gate 0 保护资产指纹采集（可重复执行，用于 before / after 对照）。

为什么单独写一个：S6 要求在 Gate 0 与最终 Gate **各采集一次**同样的指纹并逐项
对比，手工敲命令既不可重复也容易漏项。本脚本只做只读操作：

- C0 五个脚本取 **Git blob hash**（`git rev-parse HEAD:<path>`），不是工作区
  hash —— 这样即使文件被格式化，只要内容进了提交就能发现；
- 冻结信号文件取 SHA256；
- 数据库以 `mode=ro&immutable=1` 打开做 `PRAGMA integrity_check`，
  不 checkpoint、不建表、不落 WAL；
- 同时记录 WAL / SHM 是否存在与大小，避免「主库没变但 WAL 有未落盘写入」被漏掉。

输出三个文件（路径由参数指定，默认写到 reports/s6/gate0/）：

    protected-assets-*.json    完整指纹
    protected-assets-*.sha256  上述 JSON 的 SHA256（一行，便于直接比对）
    database-integrity-*.txt   三个库的 integrity_check 原始输出

用法：
    python scripts/s6_gate0_fingerprint.py --stage before
    python scripts/s6_gate0_fingerprint.py --stage after
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: C0 五个脚本 / 评价器（路径来自 S5-dependency-inventory.md 1.1，已在本轮复核）
C0_PATHS = (
    "scripts/forward_collect_tushare.py",
    "scripts/forward_store.py",
    "scripts/forward_track_c0.py",
    "scripts/forward_universe.py",
    "backend/app/services/factor_score.py",
)

#: 两份冻结信号
FROZEN_SIGNALS = (
    "backend/data/forward_c0/signals_2026-11-11.jsonl",
    "backend/data/forward_c0/signals_2026-11-11.meta.json",
)

#: 数据库：全部记录大小/mtime/integrity；仅对小于 200MB 的库计算整文件 SHA256
DB_PATHS = (
    "backend/data/fupan.db",
    "backend/data/history.db",
    "backend/data/b1_research.db",
    "backend/data/forward_research.db",
)

SHA256_MAX_BYTES = 200 * 1024 * 1024

CN_TZ = timezone(timedelta(hours=8))


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _sha256(path: Path) -> str | None:
    if path.stat().st_size > SHA256_MAX_BYTES:
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _integrity(path: Path) -> tuple[str, list[str]]:
    """只读 + immutable 打开，返回（结论, 原始行）。"""
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
    finally:
        connection.close()
    lines = [str(row[0]) for row in rows]
    ok = lines == ["ok"]
    return ("ok" if ok else "FAILED"), lines


def collect() -> tuple[dict, list[str]]:
    generated_at = datetime.now(CN_TZ).isoformat(timespec="seconds")
    assets: dict = {
        "commit_sha": _git("rev-parse", "HEAD"),
        "branch": _git("branch", "--show-current"),
        "generated_at": generated_at,
        "environment": {
            "os": os.name,
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "cwd": str(ROOT),
        },
        "command": " ".join(["python", "scripts/s6_gate0_fingerprint.py", *sys.argv[1:]]),
        "exit_code": 0,
        "tool_version": "s6_gate0_fingerprint/1.0",
        "result": {},
        "evidence_files": [],
        "limitations": [
            "数据库以 mode=ro&immutable=1 打开：不读取未 checkpoint 的 WAL 内容，"
            "integrity_check 反映主库文件本身；WAL/SHM 存在性与大小单独记录。",
            "b1_research.db 超过 200MB，不计算整文件 SHA256（按任务书只要求大小/mtime），"
            "以 integrity_check 与 size/mtime 作为对照依据。",
        ],
    }

    c0 = {}
    for rel in C0_PATHS:
        c0[rel] = {
            "git_blob": _git("rev-parse", f"HEAD:{rel}"),
            "worktree_sha256": _sha256(ROOT / rel) if (ROOT / rel).exists() else None,
        }
    assets["result"]["c0_scripts"] = c0

    frozen = {}
    integrity_lines: list[str] = []
    for rel in FROZEN_SIGNALS:
        path = ROOT / rel
        frozen[rel] = {"sha256": _sha256(path) if path.exists() else None}
    assets["result"]["frozen_signals"] = frozen

    databases = {}
    for rel in DB_PATHS:
        path = ROOT / rel
        if not path.exists():
            databases[rel] = {"exists": False}
            continue
        stat = path.stat()
        verdict, lines = _integrity(path)
        wal = path.with_name(path.name + "-wal")
        shm = path.with_name(path.name + "-shm")
        databases[rel] = {
            "exists": True,
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, CN_TZ).isoformat(timespec="seconds"),
            "sha256": _sha256(path),
            "sha256_skipped_reason": None
            if stat.st_size <= SHA256_MAX_BYTES
            else f"文件 {stat.st_size} 字节 > {SHA256_MAX_BYTES}，按任务书只记录大小/mtime",
            "integrity_check": verdict,
            "wal": {"exists": wal.exists(), "size": wal.stat().st_size if wal.exists() else 0},
            "shm": {"exists": shm.exists(), "size": shm.stat().st_size if shm.exists() else 0},
        }
        integrity_lines.append(f"{rel}\t{verdict}\t{stat.st_size}\t{stat.st_mtime_ns}")
        for line in lines:
            integrity_lines.append(f"  {line}")
    assets["result"]["databases"] = databases

    # 保护资产在 Git 中的状态（应全为空：无修改、无 untracked）
    watched = list(C0_PATHS) + list(FROZEN_SIGNALS)
    assets["result"]["git_status"] = _git("status", "--porcelain", "--", *watched) or ""
    baseline_check = subprocess.run(
        ["git", "-C", str(ROOT), "merge-base", "--is-ancestor", "3060e82", "HEAD"]
    )
    assets["result"]["git_head_contains_3060e82"] = baseline_check.returncode == 0

    # 汇总：所有保护项是否齐备
    missing = [
        rel
        for rel, info in {**c0, **frozen}.items()
        if not info.get("git_blob") and not info.get("sha256")
    ]
    bad_db = [rel for rel, info in databases.items() if info.get("integrity_check") == "FAILED"]
    assets["result"]["missing_assets"] = missing
    assets["result"]["integrity_failed"] = bad_db
    assets["result"]["all_protected_assets_present"] = not missing and not bad_db

    return assets, integrity_lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", default="before", help="证据文件名后缀（before/after/gate0/final）")
    parser.add_argument("--out-dir", default=str(ROOT / "reports" / "s6" / "gate0"))
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"protected-assets-{args.stage}"
    json_path = out_dir / f"{prefix}.json"
    sha_path = out_dir / f"{prefix}.sha256"
    integrity_path = out_dir / f"database-integrity-{args.stage}.txt"

    assets, integrity_lines = collect()
    assets["evidence_files"] = [
        str(json_path.relative_to(ROOT)),
        str(sha_path.relative_to(ROOT)),
        str(integrity_path.relative_to(ROOT)),
    ]

    payload = json.dumps(assets, ensure_ascii=False, indent=2, sort_keys=True)
    json_path.write_text(payload + "\n", encoding="utf-8")
    file_digest = hashlib.sha256((payload + "\n").encode("utf-8")).hexdigest()
    sha_path.write_text(f"{file_digest}  {prefix}.json\n", encoding="utf-8")
    integrity_path.write_text("\n".join(integrity_lines) + "\n", encoding="utf-8")

    print(f"commit={assets['commit_sha']} branch={assets['branch']}")
    print(f"json={json_path} sha256={file_digest}")
    print(
        "protected_assets_present="
        f"{assets['result']['all_protected_assets_present']} "
        f"missing={assets['result']['missing_assets']} "
        f"integrity_failed={assets['result']['integrity_failed']}"
    )
    return 0 if assets["result"]["all_protected_assets_present"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
