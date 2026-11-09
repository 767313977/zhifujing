"""B1.4 新旧回测内核并行验证（只读，不替换正式 track）。

对照对象：`/api/patterns/track` 的旧口径 vs B0 统一内核。

- 信号集：直接用 `pattern_track._picks`，保证与旧 track 完全同一批票/日期；
- 旧结果：`pattern_track.track`（筛选日收盘 → 第 n 日收盘）；
- 新结果：`research_backtest.run_signals(entry_mode="close")`（同一口径）；
- 只比较 mean / median / 上涨占比；`excess/beat` 因基准时点不同（旧=筛选日收盘，
  新=默认次日开盘）必然有差异，单独标注为「预期差异」，不要求数值相等。

运行：

    python scripts/compare_track_kernel.py --db backend/data/b1_research.db \
        --start 2026-05-14 --cohorts 20 --top 50 --track-days 20 \
        --report backend/data/b1_track_compare.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

from sqlalchemy import func, select

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
BACKEND = ROOT / "backend"
for path in (str(BACKEND), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="新旧 track 并行验证")
    parser.add_argument("--db", required=True)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 5, 14))
    parser.add_argument("--cohorts", type=int, default=20)
    parser.add_argument("--top", type=int, default=50)
    parser.add_argument("--track-days", type=int, default=20)
    parser.add_argument("--report")
    return parser.parse_args()


def main() -> int:
    args = _args()
    path = Path(args.db).resolve()
    if not path.exists():
        raise SystemExit(f"数据库不存在：{path}")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.environ["DB_PATH"] = str(path)

    from app.db import session_scope
    from app.models import StockDaily
    from app.services import pattern_track, research_backtest as rb

    with session_scope() as session:
        old = pattern_track.track(
            session,
            start=args.start,
            cohorts=args.cohorts,
            top=args.top,
            track_days=args.track_days,
        )
        days = pattern_track._scan_dates(session, args.cohorts, args.start)
        picks = pattern_track._picks(session, days, args.top)
        signals = []
        for day in days:
            for item in picks.get(day, []):
                signals.append(rb.Signal(code=item[0], signal_date=day))
        logging.info("旧 track 循环 %d 天、新内核信号 %d 条", len(days), len(signals))
        if not signals:
            raise SystemExit("没有可对照的信号")
        data_end = session.scalar(select(func.max(StockDaily.trade_date)))
        matrix = rb.load_matrix(session, min(days), data_end)
        benchmark = rb.Benchmark(session, matrix, "market")
        new = rb.run_signals(
            session,
            signals,
            strategy="pattern_track_compare",
            params={"cohorts": args.cohorts, "top": args.top, "track_days": args.track_days},
            horizons=tuple(range(1, args.track_days + 1)),
            entry_mode="close",
            cost_bps=0.0,
            matrix=matrix,
            benchmark=benchmark,
        )

    old_by_day = {int(item["day"]): item for item in old["average"]}
    rows = []
    for step in range(1, args.track_days + 1):
        old_row = old_by_day.get(step, {})
        stat = new.horizons.get(step)
        if not stat:
            continue
        new_row = {
            "samples": stat.execution_samples,
            "mean": stat.mean,
            "median": stat.median,
            "up_pct": stat.pos_pct,
        }
        mean_diff = (
            round(float(new_row["mean"]) - float(old_row["mean"]), 2)
            if new_row["mean"] is not None and old_row.get("mean") is not None
            else None
        )
        median_diff = (
            round(float(new_row["median"]) - float(old_row["median"]), 2)
            if new_row["median"] is not None and old_row.get("median") is not None
            else None
        )
        up_diff = (
            round(float(new_row["up_pct"]) - float(old_row["up_pct"]), 1)
            if new_row["up_pct"] is not None and old_row.get("up_pct") is not None
            else None
        )
        rows.append(
            {
                "step": step,
                "old": old_row,
                "new": new_row,
                "mean_diff": mean_diff,
                "median_diff": median_diff,
                "up_diff": up_diff,
                "expected_diff": "基准时点不同（旧=筛选日收盘，新=次日开盘），excess/beat 不可直接比",
            }
        )

    payload = {
        "params": vars(args) | {"db": str(path)},
        "old_summary": old.get("summary"),
        "new_coverage": new.data_coverage,
        "rows": rows,
        "max_abs_mean_diff": max((abs(item["mean_diff"]) for item in rows if item["mean_diff"] is not None), default=None),
        "max_abs_median_diff": max((abs(item["median_diff"]) for item in rows if item["median_diff"] is not None), default=None),
        "max_abs_up_diff": max((abs(item["up_diff"]) for item in rows if item["up_diff"] is not None), default=None),
        "note": "同一信号集、收盘入场口径；mean/median/up 应接近。excess/beat 只做映射说明。",
    }
    if args.report:
        Path(args.report).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("step | old_mean | new_mean | diff | old_median | new_median | up_diff")
    for item in rows:
        print(
            f"{item['step']:>4} | {item['old'].get('mean')} | {item['new'].get('mean')} | "
            f"{item['mean_diff']} | {item['old'].get('median')} | {item['new'].get('median')} | {item['up_diff']}"
        )
    print(json.dumps({k: payload[k] for k in ("max_abs_mean_diff", "max_abs_median_diff", "max_abs_up_diff")}, ensure_ascii=False))
    if args.report:
        print(f"报告：{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
