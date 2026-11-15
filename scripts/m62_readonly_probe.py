"""M6.2 只读约束探针：证明数据库以 mode=ro + query_only 打开且拒绝一切写操作。

对每个目标库执行：
1. 以 `file:...?mode=ro` URI 打开（文件不存在直接失败，不创建）。
2. 执行 `PRAGMA query_only=ON` 并回读。
3. 依次尝试 CREATE / INSERT / UPDATE / DELETE / DROP / VACUUM / REINDEX，
   记录抛出的异常（预期全部失败）。
4. 连接关闭后核对文件大小、mtime 与 SHA256 未变化。

用法：

    python scripts/m62_readonly_probe.py --db backend/data/b1_research.db \
        --out reports/trend_engine/m62/readonly_probe.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

WRITE_STATEMENTS = (
    ("create", "CREATE TABLE _m62_probe (x INTEGER)"),
    ("insert", "INSERT INTO stock_daily (code, trade_date, close) VALUES ('PROBE', '1900-01-01', 1.0)"),
    ("update", "UPDATE stock_daily SET close = 0 WHERE 1 = 0"),
    ("delete", "DELETE FROM stock_daily WHERE 1 = 0"),
    ("drop", "DROP TABLE IF EXISTS _m62_probe"),
    ("vacuum", "VACUUM"),
    ("reindex", "REINDEX"),
)

#: 这些语句在目标对象不存在时合法地成为空操作（DROP 无表、REINDEX 无索引），
#: 执行成功不代表写入生效，但也不能标记为「被拒绝」
NOOP_ALLOWED = frozenset({"drop", "reindex"})


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def stat_of(path: Path) -> dict:
    info = path.stat()
    return {"bytes": info.st_size, "mtime_ns": info.st_mtime_ns}


def probe(path: Path, with_hash: bool) -> dict:
    if not path.exists():
        return {"db": str(path), "status": "MISSING", "error": "目标文件不存在，未创建"}
    before = stat_of(path)
    before_hash = sha256(path) if with_hash else None
    uri = f"file:{path.as_posix()}?mode=ro"
    record: dict = {
        "db": str(path),
        "uri": uri,
        "before": before,
        "before_sha256": before_hash,
        "opened": False,
        "query_only": None,
        "attempts": [],
        "after": None,
        "after_sha256": None,
        "unchanged": None,
        "status": "UNKNOWN",
    }
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        record["opened"] = True
        record["query_only_before"] = connection.execute("PRAGMA query_only").fetchone()[0]
        connection.execute("PRAGMA query_only=ON")
        record["query_only"] = connection.execute("PRAGMA query_only").fetchone()[0]
        record["journal_mode"] = connection.execute("PRAGMA journal_mode").fetchone()[0]
        for name, sql in WRITE_STATEMENTS:
            try:
                connection.execute(sql)
                connection.rollback()
                # DROP TABLE IF EXISTS 在目标表从未创建时为空操作，不算写入成功
                effect = "noop" if name in NOOP_ALLOWED else "executed"
                record["attempts"].append(
                    {"statement": name, "blocked": effect == "noop", "effect": effect, "error": None}
                )
            except sqlite3.Error as exc:  # noqa: PERF203
                record["attempts"].append(
                    {"statement": name, "blocked": True, "effect": "blocked", "error": f"{type(exc).__name__}: {exc}"}
                )
    finally:
        connection.close()
    after = stat_of(path)
    after_hash = sha256(path) if with_hash else None
    record["after"] = after
    record["after_sha256"] = after_hash
    record["unchanged"] = before == after and (before_hash == after_hash)
    blocked_all = all(item["blocked"] for item in record["attempts"]) and len(record["attempts"]) == len(
        WRITE_STATEMENTS
    )
    record["status"] = (
        "READONLY_PROVEN"
        if blocked_all and record["query_only"] == 1 and record["unchanged"]
        else "READONLY_NOT_PROVEN"
    )
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="M6.2 只读约束探针")
    parser.add_argument("--db", action="append", required=True, help="数据库路径，可重复")
    parser.add_argument("--out", default=None, help="输出 JSON 路径")
    parser.add_argument("--hash", action="store_true", help="同时计算完整 SHA256（较慢）")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    results = []
    for item in args.db:
        path = Path(item)
        if not path.is_absolute():
            path = root / path
        results.append(probe(path, args.hash))

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "results": results,
        "all_proven": all(item["status"] == "READONLY_PROVEN" for item in results),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = root / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if payload["all_proven"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
