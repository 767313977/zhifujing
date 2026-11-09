"""B2.4 Track 逐样本差异审计（统一时间轴映射）。

旧 `/patterns/track` 的 step n = 信号日之后第 n 个交易日收盘；
新内核 `entry=close` 的 horizon h = 信号日之后第 h−1 个交易日收盘。
因此正确映射是：**旧 step n ↔ 新 horizon n+1**。

本脚本：

1. 用同一个 `pattern_track._picks` 信号集；
2. 旧侧用 `_matrix` + `_curves_from` 还原到**逐股票、逐 step**；
3. 新侧用 `run_signals(entry_mode="close")`，horizons 1..track_days+1；
4. 按 (code, signal_date, 绝对退出日) 对齐，输出逐样本差异与分类；
5. 不修改路由、Schema、前端。

运行：

    python scripts/compare_track_kernel.py --db backend/data/b1_research.db \
        --start 2026-05-14 --cohorts 20 --top 50 --track-days 20 \
        --report backend/data/b2_track_sample_audit.json
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
    parser = argparse.ArgumentParser(description="Track 逐样本差异审计")
    parser.add_argument("--db", required=True)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 5, 14))
    parser.add_argument("--cohorts", type=int, default=20)
    parser.add_argument("--top", type=int, default=50)
    parser.add_argument("--track-days", type=int, default=20)
    parser.add_argument("--tolerance", type=float, default=0.05, help="收益一致容差（百分点）")
    parser.add_argument("--report")
    return parser.parse_args()


def _old_gross(matrix, day, code: str, step: int) -> tuple[float | None, date | None]:
    """按旧 `_curves_from` 的同一算法还原单票 step n 的累计收益。"""
    start = matrix.date_pos.get(day)
    if start is None:
        return None, None
    col = matrix.code_pos.get(code)
    if col is None:
        return None, None
    calendar_pos = matrix.index[day]
    shift = calendar_pos + step
    if shift > matrix.end_pos:
        return None, None
    when = matrix.calendar[shift]
    position = matrix.date_pos.get(when)
    if position is None:
        return None, None
    if not matrix.present[start, col] or not matrix.present[position, col]:
        return None, None
    factor = 1.0
    for offset in range(1, step + 1):
        pos = matrix.date_pos.get(matrix.calendar[calendar_pos + offset])
        if pos is None:
            return None, None
        factor *= float(matrix.growth[pos, col])
    return (factor - 1) * 100, when


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
        days = pattern_track._scan_dates(session, args.cohorts, args.start)
        picks = pattern_track._picks(session, days, args.top)
        old_matrix = pattern_track._matrix(session, days, args.track_days)
        if old_matrix is None:
            raise SystemExit("旧矩阵为空")
        signals = [rb.Signal(code=item[0], signal_date=day) for day in days for item in picks.get(day, [])]
        data_end = session.scalar(select(func.max(StockDaily.trade_date)))
        matrix = rb.load_matrix(session, min(days), data_end)
        benchmark = rb.Benchmark(session, matrix, "market")
        new = rb.run_signals(
            session,
            signals,
            strategy="track_sample_audit",
            params={"cohorts": args.cohorts, "top": args.top, "track_days": args.track_days},
            horizons=tuple(range(1, args.track_days + 2)),
            entry_mode="close",
            cost_bps=0.0,
            matrix=matrix,
            benchmark=benchmark,
        )

    new_map = {(s.code, s.signal_date, s.horizon): s for s in new.samples}
    rows = []
    counts = {}
    for day in days:
        for step in range(1, args.track_days + 1):
            for item in picks.get(day, []):
                code = item[0]
                old_gross, old_target = _old_gross(old_matrix, day, code, step)
                sample = new_map.get((code, day, step + 1))
                new_gross = sample.gross if sample and sample.status == "ok" else None
                new_exit = sample.exit_date if sample else None
                diff = None
                if old_gross is not None and new_gross is not None:
                    diff = round(new_gross - old_gross, 4)
                if old_gross is None and new_gross is None:
                    kind = "both_missing"
                elif old_gross is None:
                    kind = "old_missing"
                elif new_gross is None:
                    kind = "new_not_executable"
                elif old_target != new_exit:
                    kind = "time_axis"
                elif abs(diff or 0) <= args.tolerance:
                    kind = "consistent"
                else:
                    kind = "unexplained"
                counts[kind] = counts.get(kind, 0) + 1
                rows.append(
                    {
                        "code": code,
                        "signal_date": str(day),
                        "old_step": step,
                        "new_horizon": step + 1,
                        "old_target_date": str(old_target) if old_target else None,
                        "new_exit_date": str(new_exit) if new_exit else None,
                        "old_gross": round(old_gross, 4) if old_gross is not None else None,
                        "new_gross": round(new_gross, 4) if new_gross is not None else None,
                        "diff": diff,
                        "old_valid": old_gross is not None,
                        "new_valid": new_gross is not None,
                        "diff_type": kind,
                    }
                )

    # 聚合到 old_step
    aggregate = {}
    for step in range(1, args.track_days + 1):
        subset = [row for row in rows if row["old_step"] == step]
        old_vals = [row["old_gross"] for row in subset if row["old_gross"] is not None]
        new_vals = [row["new_gross"] for row in subset if row["new_gross"] is not None]
        diffs = [row["diff"] for row in subset if row["diff"] is not None]
        aggregate[str(step)] = {
            "old_samples": len(old_vals),
            "new_samples": len(new_vals),
            "old_mean": round(float(sum(old_vals) / len(old_vals)), 2) if old_vals else None,
            "new_mean": round(float(sum(new_vals) / len(new_vals)), 2) if new_vals else None,
            "mean_diff": round(float(sum(diffs) / len(diffs)), 2) if diffs else None,
            "consistent": sum(1 for row in subset if row["diff_type"] == "consistent"),
            "time_axis": sum(1 for row in subset if row["diff_type"] == "time_axis"),
            "unexplained": sum(1 for row in subset if row["diff_type"] == "unexplained"),
        }

    payload = {
        "params": vars(args) | {"db": str(path)},
        "mapping": "old step n ↔ new horizon n+1（旧 step n = 信号后第 n 个交易日收盘；新 horizon h = 信号后第 h−1 个交易日收盘）",
        "counts": counts,
        "aggregate": aggregate,
        "samples": rows,
        "note": "逐样本差异；consistent 容差见 --tolerance。time_axis 表示绝对退出日不同；new_not_executable 表示新内核判为不可成交。",
    }
    if args.report:
        Path(args.report).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"counts": counts, "aggregate": aggregate}, ensure_ascii=False, indent=2))
    if args.report:
        print(f"报告：{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
