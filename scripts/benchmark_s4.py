"""S4 只读性能基准：SQL 条数 / 耗时 / Python 峰值内存。

只以 `mode=ro&immutable=1` 打开本地库，不写库、不发外部请求。
结果写入 `reports/s4_performance/benchmark.json`（默认）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import tracemalloc
from collections.abc import Callable
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services import data_quality as dq  # noqa: E402
from app.services import sector_resonance as sr  # noqa: E402
from app.services import sentiment_next_day as snd  # noqa: E402
from app.services import stock_research_context as ctx  # noqa: E402


@contextmanager
def instrument(engine):
    counter = {"sql": 0}

    def _before(_conn, _cursor, _statement, _parameters, _context, _many):
        counter["sql"] += 1

    event.listen(engine, "before_cursor_execute", _before)
    tracemalloc.start()
    started = time.perf_counter()
    try:
        yield counter
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        event.remove(engine, "before_cursor_execute", _before)
        counter["elapsed_ms"] = round(elapsed, 1)
        counter["peak_kb"] = round(peak / 1024, 1)


def _latest_trade_day(session: Session) -> date:
    value = session.execute(text("select max(trade_date) from trade_calendar")).scalar()
    return date.fromisoformat(str(value))


def _top_sectors(session: Session, day: date, limit: int) -> list[str]:
    rows = session.execute(
        text(
            "select sector_code, count(*) as c from sector_member "
            "where trade_date = :day group by sector_code order by c desc limit :limit"
        ),
        {"day": day, "limit": limit},
    ).all()
    return [row[0] for row in rows]


def _first_code(session: Session) -> str:
    return session.execute(
        text("select code from stock_basic order by code limit 1")
    ).scalar() or "600000"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="backend/data/fupan.db")
    parser.add_argument("--out", default="reports/s4_performance/benchmark.json")
    args = parser.parse_args()

    db = Path(args.db).resolve()
    uri = "sqlite:///file:" + db.as_posix() + "?mode=ro&immutable=1&uri=true"
    engine = create_engine(uri, future=True)
    results: list[dict] = []

    def run(label: str, fn: Callable[[Session], dict | None], **meta) -> None:
        with Session(engine) as session, instrument(engine) as counter:
            payload = fn(session)
        entry = {
            "case": label,
            **meta,
            "sql": counter["sql"],
            "elapsed_ms": counter["elapsed_ms"],
            "peak_kb": counter["peak_kb"],
        }
        if isinstance(payload, dict):
            for key in ("total", "shown", "series", "points", "items"):
                if key in payload:
                    value = payload[key]
                    entry[f"{key}_count"] = len(value) if isinstance(value, list) else value
        results.append(entry)
        print(json.dumps(entry, ensure_ascii=False))

    with Session(engine) as session:
        day = _latest_trade_day(session)
        # 成员快照可能落后于最新交易日：取快照自己的最后一天，避免静默跳过
        sector_day = session.execute(
            text("select max(trade_date) from sector_member")
        ).scalar()
        sector_day = date.fromisoformat(str(sector_day)) if sector_day else day
        sectors = _top_sectors(session, sector_day, 10)
        code = _first_code(session)

    for days in (30, 60, 120, 250):
        run("quality_overview", lambda s, d=days: dq.overview(s, target_date=day, days=d), days=days)
    for days in (30, 60, 120, 250):
        run(
            "quality_timeline_limit_pool",
            lambda s, d=days: dq.timeline(s, source="limit_pool", days=d),
            days=days,
        )
    for days in (20, 60, 120, 250):
        run(
            "sentiment_next_day_first_board",
            lambda s, d=days: snd.build_next_day(
                s, days=d, group=snd.GROUP_FIRST, metric="limit_up_count", end_date=day
            ),
            days=days,
        )
    for count in (1, 2, 3, 10):
        subset = sectors[:count]
        if len(subset) < count:
            continue
        for mode in ("AND", "OR"):
            run(
                "sector_resonance",
                lambda s, c=subset, m=mode: sr.build(
                    s, as_of=sector_day, sector_codes=c, mode=m, limit=500
                ),
                sectors=count,
                mode=mode,
                as_of=str(sector_day),
            )
    run(
        "research_context",
        lambda s: ctx.build(s, code=code, as_of=day),
        code=code,
    )

    payload = {
        "database": db.name,
        "as_of": str(day),
        "readonly": True,
        "results": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written {out}")


if __name__ == "__main__":
    main()
