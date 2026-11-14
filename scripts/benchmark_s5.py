"""S5 性能基准（只读本地库，不外呼）。

```powershell
.venv/Scripts/python.exe scripts/benchmark_s5.py --db backend/data/b1_research.db `
  --date 2026-09-24 --sectors 801572,801366,803024,801085,801033 `
  --out reports/s5_performance/benchmark.json
```
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import tracemalloc
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.models import StockBasic  # noqa: E402
from app.services import data_quality, post_market_workbench, sentiment_next_day  # noqa: E402
from app.services import stock_research_context  # noqa: E402


def _uri(path: Path) -> str:
    return f"sqlite:///file:{path.resolve().as_posix()}?mode=ro&immutable=1&uri=true"


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        return None


def measure(engine, fn, repeats: int = 3) -> dict:
    statements: list[str] = []

    def _listener(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _listener)
    times: list[float] = []
    peak_kb = 0.0
    result_size = 0
    try:
        for _ in range(repeats):
            statements.clear()
            started = time.perf_counter()
            with Session(engine) as session:
                payload = fn(session)
            times.append((time.perf_counter() - started) * 1000)
            result_size = max(result_size, len(json.dumps(payload, ensure_ascii=False, default=str)))
        # 内存单独测一次：tracemalloc 会显著拖慢耗时，不混入时间样本
        sql_count = len(statements)
        tracemalloc.start()
        with Session(engine) as session:
            fn(session)
        _, peak = tracemalloc.get_traced_memory()
        peak_kb = peak / 1024
        tracemalloc.stop()
    finally:
        event.remove(engine, "before_cursor_execute", _listener)
    return {
        "runs": repeats,
        "sql_statements": sql_count,
        "elapsed_ms_median": round(statistics.median(times), 2),
        "elapsed_ms_runs": [round(value, 2) for value in times],
        "peak_memory_kb": round(peak_kb, 1),
        "json_bytes": result_size,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--sectors", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if "mode=rw" in args.db.lower() or "mode=rwc" in args.db.lower():
        raise SystemExit("拒绝写模式数据库")

    engine = create_engine(_uri(Path(args.db)), future=True)
    sectors = [item.strip() for item in args.sectors.split(",") if item.strip()]
    results: list[dict] = []
    for size in (1, 2, 3, len(sectors)):
        codes = sectors[:size]
        results.append(
            {
                "case": f"workbench_{size}_sectors",
                "params": {"sectors": codes, "mode": "OR", "limit": 50},
                **measure(
                    engine,
                    lambda session, c=codes: post_market_workbench.build(
                        session, as_of=args.date, sector_codes=c, mode="OR", limit=50
                    ),
                ),
            }
        )
    results.append(
        {
            "case": "quality_overview_1day",
            "params": {"days": 30},
            **measure(
                engine,
                lambda session: data_quality.overview(
                    session, target_date=args.date, days=30
                ),
            ),
        }
    )
    results.append(
        {
            "case": "quality_timeline_limit_pool_250",
            "params": {"days": 250},
            **measure(
                engine,
                lambda session: data_quality.timeline(
                    session, source="limit_pool", days=250
                ),
                1,
            ),
        }
    )
    results.append(
        {
            "case": "sentiment_next_day_250",
            "params": {"days": 250},
            **measure(
                engine,
                lambda session: sentiment_next_day.build_next_day(
                    session, days=250, end_date=args.date
                ),
                1,
            ),
        }
    )
    with Session(engine) as session:
        code = session.scalar(select(StockBasic.code).order_by(StockBasic.code).limit(1))
    if code:
        results.append(
            {
                "case": "research_context_single_stock",
                "params": {"code": code, "as_of": args.date.isoformat()},
                **measure(
                    engine,
                    lambda session: stock_research_context.build(
                        session, code=code, as_of=args.date
                    ),
                ),
            }
        )

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "git_sha": _git_sha(),
        "database": Path(args.db).name,
        "mode": "ro&immutable=1",
        "as_of": args.date.isoformat(),
        "environment": "Windows 10/11 local, Python venv, SQLite page cache warm after first run",
        "results": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for item in results:
        print(
            f"{item['case']}: sql={item['sql_statements']} "
            f"median={item['elapsed_ms_median']}ms peak={item['peak_memory_kb']}KB "
            f"json={item['json_bytes']}B"
        )


if __name__ == "__main__":
    main()
