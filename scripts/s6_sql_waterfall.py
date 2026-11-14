"""S6-D1：单次请求的 SQL waterfall（只读本地库，不外呼）。

用法：
    python scripts/s6_sql_waterfall.py --db backend/data/b1_research.db \
        --date 2026-09-24 --sectors 801001,...,801035 \
        --out reports/s6/performance/sql-waterfall.json

输出：
- `executions`：按真实执行顺序，每条 SQL 的耗时与截断后的语句；
- `aggregated`：按「规范化语句」聚合（重复查询一眼可见）；
- `summary`：总数 / 总耗时 / 前 10 慢查询 / 重复次数。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services import post_market_workbench  # noqa: E402

_WS = re.compile(r"\s+")
_LIMIT = re.compile(r"\bLIMIT\s+\d+", re.IGNORECASE)


def _normalize(statement: str) -> str:
    text = _WS.sub(" ", statement.strip())
    text = _LIMIT.sub("LIMIT ?", text)
    return text[:300]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--sectors", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    sectors = [item.strip() for item in args.sectors.split(",") if item.strip()]
    uri = f"sqlite:///file:{Path(args.db).resolve().as_posix()}?mode=ro&immutable=1&uri=true"
    engine = create_engine(uri, future=True)

    executions: list[dict] = []
    started_stack: list[float] = []

    def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        started_stack.append(time.perf_counter())

    def _after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        started = started_stack.pop() if started_stack else time.perf_counter()
        executions.append(
            {
                "index": len(executions) + 1,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "sql": _normalize(statement),
                "params": len(parameters or []) if isinstance(parameters, (list, tuple)) else None,
            }
        )

    event.listen(engine, "before_cursor_execute", _before)
    event.listen(engine, "after_cursor_execute", _after)

    wall_started = time.perf_counter()
    with Session(engine) as session:
        payload = post_market_workbench.build(
            session, as_of=args.date, sector_codes=sectors, mode="OR", limit=50
        )
    wall_ms = (time.perf_counter() - wall_started) * 1000

    aggregated: dict[str, dict] = {}
    for item in executions:
        bucket = aggregated.setdefault(
            item["sql"], {"sql": item["sql"], "count": 0, "total_ms": 0.0, "max_ms": 0.0}
        )
        bucket["count"] += 1
        bucket["total_ms"] = round(bucket["total_ms"] + item["duration_ms"], 3)
        bucket["max_ms"] = max(bucket["max_ms"], item["duration_ms"])

    duplicates = sorted(
        (item for item in aggregated.values() if item["count"] > 1),
        key=lambda item: item["total_ms"],
        reverse=True,
    )
    slowest = sorted(executions, key=lambda item: item["duration_ms"], reverse=True)[:10]

    result = {
        "commit_sha": subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip(),
        "branch": subprocess.run(
            ["git", "-C", str(ROOT), "branch", "--show-current"], capture_output=True, text=True
        ).stdout.strip(),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "environment": "Windows 11, Python 3.13.13, SQLite ro&immutable=1, 本地库",
        "command": " ".join(["python", "scripts/s6_sql_waterfall.py", *sys.argv[1:]]),
        "exit_code": 0,
        "tool_version": "s6_sql_waterfall/1.0",
        "result": {
            "case": "post_market_workbench.build",
            "as_of": args.date.isoformat(),
            "sectors": sectors,
            "limit": 50,
            "total_sql": len(executions),
            "sql_budget": 25,
            "budget_met": len(executions) <= 25,
            "sum_sql_ms": round(sum(item["duration_ms"] for item in executions), 3),
            "wall_ms": round(wall_ms, 2),
            "result_json_bytes": len(json.dumps(payload, ensure_ascii=False, default=str)),
            "executions": executions,
            "aggregated": sorted(
                aggregated.values(), key=lambda item: item["total_ms"], reverse=True
            ),
            "duplicates": duplicates,
            "slowest": slowest,
        },
        "evidence_files": [args.out],
        "limitations": [
            "耗时来自单次执行，未做统计重复；P50/P95 见 benchmark-before.json。",
            "SQLite page cache 在首次执行后已热，冷启动会更慢。",
        ],
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"total_sql={len(executions)} wall={wall_ms:.1f}ms sum_sql={result['result']['sum_sql_ms']}ms")
    print("top duplicates:")
    for item in duplicates[:5]:
        print(f"  x{item['count']:2d} {item['total_ms']:7.2f}ms  {item['sql'][:110]}")


if __name__ == "__main__":
    main()
