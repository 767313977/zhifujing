"""S6.8 F1：workbench 53 条 SQL 的 fingerprint / 调用栈 / 分类剖析。

只读：数据库以 `mode=ro&immutable=1` 打开；不写库、不改查询。
输出：performance/sql_profile.json + performance/sql_profile.md

用法：
    python scripts/s6_8_sql_profile.py \
        --db backend/data/b1_research.db --date 2026-09-24 \
        --sectors 801572,801366,803024,801085,801033 \
        --out reports/s6_8_9/performance
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import time
import traceback
from collections import defaultdict
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "backend"))

from app.services import post_market_workbench  # noqa: E402


def _uri(path: Path) -> str:
    return f"sqlite:///file:{path.resolve().as_posix()}?mode=ro&immutable=1&uri=true"


def _normalize(statement: str) -> str:
    return re.sub(r"\s+", " ", statement.strip())


def _app_stack() -> list[str]:
    frames = []
    for frame in traceback.extract_stack()[:-1]:
        if "site-packages" in frame.filename or "sqlalchemy" in frame.filename:
            continue
        if "/scripts/" in frame.filename or "\\scripts\\" in frame.filename:
            continue
        frames.append(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}")
    return frames[-4:]


def _classify(statement: str, stack: list[str]) -> str:
    lowered = statement.lower()
    if "cache" in " ".join(stack).lower():
        return "cache"
    if "sector_member" in lowered or "sector" in " ".join(stack).lower():
        return "per_sector"
    if "ifind_usage" in lowered or "permission" in " ".join(stack).lower():
        return "permission"
    return "fixed"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--sectors", required=True)
    parser.add_argument("--out", default=str(ROOT / "reports" / "s6_8_9" / "performance"))
    args = parser.parse_args()

    engine = create_engine(_uri(Path(args.db)), future=True)
    sectors = [item.strip() for item in args.sectors.split(",") if item.strip()]
    records: list[dict] = []
    timings: dict[int, float] = {}

    def before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        timings[id(cursor)] = time.perf_counter()
        records.append(
            {
                "statement": _normalize(statement),
                "stack": _app_stack(),
            }
        )

    def after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        started = timings.pop(id(cursor), None)
        if started is not None and records:
            records[-1]["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    started = time.perf_counter()
    try:
        with Session(engine) as session:
            payload = post_market_workbench.build(
                session, as_of=args.date, sector_codes=sectors, mode="OR", limit=50
            )
    finally:
        event.remove(engine, "before_cursor_execute", before)
        event.remove(engine, "after_cursor_execute", after)
    total_ms = round((time.perf_counter() - started) * 1000, 2)

    groups: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "elapsed_ms": 0.0, "stacks": set(), "sample": ""}
    )
    for record in records:
        fingerprint = hashlib.sha256(record["statement"].encode("utf-8")).hexdigest()[:16]
        group = groups[fingerprint]
        group["count"] += 1
        group["elapsed_ms"] += record.get("elapsed_ms", 0)
        group["stacks"].update(record["stack"])
        group["sample"] = record["statement"][:240]
        group["classification"] = _classify(record["statement"], record["stack"])

    profiles = []
    for fingerprint, group in groups.items():
        profiles.append(
            {
                "fingerprint": fingerprint,
                "count": group["count"],
                "elapsed_ms": round(group["elapsed_ms"], 3),
                "classification": group["classification"],
                "stacks": sorted(group["stacks"]),
                "sample_sql": group["sample"],
            }
        )
    profiles.sort(key=lambda item: (-item["count"], -item["elapsed_ms"]))

    counts = [record.get("elapsed_ms", 0) for record in records]
    payload_out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "db": args.db,
        "as_of": args.date.isoformat(),
        "sectors": sectors,
        "total_sql": len(records),
        "unique_fingerprints": len(profiles),
        "total_ms": total_ms,
        "sql_elapsed_ms_median": round(statistics.median(counts), 3) if counts else None,
        "by_classification": {
            key: sum(1 for item in profiles if item["classification"] == key for _ in range(item["count"]))
            for key in ("fixed", "per_sector", "cache", "permission")
        },
        "budget": 25,
        "status": "PERF_SQL_BUDGET_NOT_MET" if len(records) > 25 else "PASS",
        "result_bytes": len(json.dumps(payload, ensure_ascii=False, default=str)),
        "profiles": profiles,
    }
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "sql_profile.json").write_text(
        json.dumps(payload_out, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# S6.8 F1：workbench SQL 剖析",
        "",
        f"- 数据库：`{args.db}`（只读 `mode=ro&immutable=1`）",
        f"- 日期 / 板块：`{args.date}` / {len(sectors)} 个",
        f"- 总 SQL：**{len(records)}**（预算 25）→ `{payload_out['status']}`",
        f"- 唯一 fingerprint：{len(profiles)}；总耗时 {total_ms} ms",
        "",
        "| # | count | 分类 | 耗时 ms | fingerprint | 调用栈 | SQL 片段 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for index, item in enumerate(profiles[:20], 1):
        stack = "<br>".join(item["stacks"][:3])
        sql = item["sample_sql"].replace("|", "\\|")[:120]
        lines.append(
            f"| {index} | {item['count']} | {item['classification']} | {item['elapsed_ms']} | "
            f"`{item['fingerprint']}` | {stack} | `{sql}` |"
        )
    lines.extend(
        [
            "",
            "## 结论",
            "",
            "- 本轮只做剖析，**未改查询、未改缓存、未调预算**；",
            "- `PERF_SQL_BUDGET_NOT_MET` 原样保留；",
            "- 优化前置条件：结果语义、PIT、事务一致性和跨用户缓存隔离不变，并有等价性测试与前后对照。",
        ]
    )
    (out / "sql_profile.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "total_sql": len(records),
                "unique": len(profiles),
                "status": payload_out["status"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
