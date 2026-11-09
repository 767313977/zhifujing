"""B1.3 板块与个股共振验证：A~E 五组公平对照（5/10/20 为主，40/60 仅完整窗口）。

分组：

- A：仅个股趋势模板（基准组）
- B：A + 所属板块 RPS20 ≥ 90
- C：A + 所属板块连续强势天数 ≥ 3（RPS20 ≥ 80）
- D：A + 所属板块 RPS20 5 日改善 ≥ 10
- E：A，但按板块 RPS20 排序取前 50%（板块指标只做排序辅助）

所有组共用同一批信号日、入场/退出规则、费用、可成交性与市场过滤；
一只股票属于多个板块时，B/C/D 只要**任一**板块满足即入选，但每只股票每个信号日只计一次。
同时输出「全市场等权」与「所属最强板块等权」两套基准，区分个股自身强弱与板块整体上涨。

运行：

    python scripts/validate_sector_resonance.py --db backend/data/b1_research.db \
        --start 2026-06-29 --end 2026-09-24 \
        --report backend/data/b1_sector_resonance.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np
from sqlalchemy import func, select

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
BACKEND = ROOT / "backend"
for path in (str(BACKEND), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="B1.3 板块与个股共振验证")
    parser.add_argument("--db", required=True)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 6, 29))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 24))
    parser.add_argument("--taxonomy", default="kph_selected")
    parser.add_argument("--horizons", default="5,10,20,40,60")
    parser.add_argument("--top", type=int, default=200)
    parser.add_argument("--min-rs", type=float, default=70.0)
    parser.add_argument("--cost-bps", type=float, default=0.0)
    parser.add_argument("--report")
    return parser.parse_args()


def _horizons(text: str) -> tuple[int, ...]:
    return tuple(sorted({int(item) for item in text.split(",") if item.strip()}))


def _board_metrics(session, taxonomy: str, start: date, end: date) -> dict:
    """板块 RPS20、5 日改善、连续强势天数。只用 <= 当天的数据。"""
    from app.models import SectorDaily

    rows = session.execute(
        select(SectorDaily.trade_date, SectorDaily.sector_code, SectorDaily.pct_chg)
        .where(SectorDaily.taxonomy == taxonomy, SectorDaily.trade_date <= end)
        .order_by(SectorDaily.trade_date)
    ).all()
    by_board: dict[str, dict[date, float]] = defaultdict(dict)
    for day, code, pct in rows:
        if pct is not None:
            by_board[code][day] = float(pct)
    ret20: dict[date, dict[str, float]] = defaultdict(dict)
    for board, series in by_board.items():
        ordered = sorted(series)
        for i, day in enumerate(ordered):
            window = ordered[max(0, i - 19) : i + 1]
            if len(window) < 18:
                continue
            value = 1.0
            for item in window:
                value *= 1 + series[item] / 100
            ret20[day][board] = (value - 1) * 100
    rps20: dict[date, dict[str, float]] = {}
    for day, values in ret20.items():
        if len(values) < 5:
            continue
        ordered = sorted(values.items(), key=lambda item: item[1])
        ranks = {}
        for index, (board, _value) in enumerate(ordered):
            # 中位秩百分位
            ranks[board] = (index + 0.5) / len(ordered) * 100
        rps20[day] = ranks
    ordered_days = sorted(rps20)
    out = {}
    for board in by_board:
        prev_rps = []
        for i, day in enumerate(ordered_days):
            value = rps20[day].get(board)
            if value is None:
                prev_rps = []
                continue
            if value >= 80:
                prev_rps.append(value)
            else:
                prev_rps = []
            delta5 = None
            if i >= 5:
                old = rps20[ordered_days[i - 5]].get(board)
                if old is not None:
                    delta5 = value - old
            out[(board, day)] = {
                "rps20": value,
                "delta5": delta5,
                "strong_days": len(prev_rps),
            }
    return out


def _members(session, taxonomy: str, days: list[date], codes: list[str]) -> dict:
    from app.models import SectorBasic, SectorMember

    rows = session.execute(
        select(SectorMember.trade_date, SectorMember.code, SectorMember.sector_code)
        .join(SectorBasic, SectorBasic.code == SectorMember.sector_code)
        .where(
            SectorMember.trade_date.in_(days),
            SectorMember.code.in_(codes),
            SectorBasic.taxonomy == taxonomy,
        )
    ).all()
    out = defaultdict(set)
    for day, code, board in rows:
        out[(code, day)].add(board)
    return out


def _sector_benchmark(
    session,
    matrix,
    members_by_board: dict,
    board: str,
    signal_day: date,
    entry_day: date | None,
    exit_day: date | None,
) -> float | None:
    """所属板块等权收益（同入场/退出日、同可成交口径）。"""
    if not entry_day or not exit_day:
        return None
    from app.services import research_backtest as rb

    entry_pos = matrix.date_pos.get(entry_day)
    exit_pos = matrix.date_pos.get(exit_day)
    if entry_pos is None or exit_pos is None:
        return None
    codes = members_by_board.get((board, signal_day), set())
    values = []
    for code in codes:
        col = matrix.code_pos.get(code)
        if col is None:
            continue
        if not matrix.present[entry_pos, col] or not matrix.present[exit_pos, col]:
            continue
        open_ = matrix.open[entry_pos, col]
        close_e = matrix.close[entry_pos, col]
        pct_e = matrix.pct[entry_pos, col]
        if not np.isfinite(open_) or not np.isfinite(close_e) or open_ <= 0:
            continue
        if rb._entry_limit_up(
            code,
            matrix.name[entry_pos, col],
            entry_day,
            float(open_),
            float(close_e),
            None if not np.isfinite(pct_e) else float(pct_e),
        ):
            continue
        close_x = matrix.close[exit_pos, col]
        low_x = matrix.low[exit_pos, col]
        pct_x = matrix.pct[exit_pos, col]
        if not np.isfinite(close_x):
            continue
        if rb._exit_limit_down(
            code,
            matrix.name[exit_pos, col],
            exit_day,
            float(close_x),
            None if not np.isfinite(low_x) else float(low_x),
            None if not np.isfinite(pct_x) else float(pct_x),
        ):
            continue
        factor = close_e / open_ - 1
        factor = 1 + factor
        for pos in range(entry_pos + 1, exit_pos + 1):
            if matrix.present[pos, col]:
                pct = matrix.pct[pos, col]
                if np.isfinite(pct):
                    factor *= 1 + pct / 100
        values.append((factor - 1) * 100)
    return float(np.mean(values)) if values else None


def _stat(samples: list, horizon: int, sector_returns: dict) -> dict:
    selected = [
        sample
        for sample in samples
        if sample.horizon == horizon and sample.status == "ok" and sample.gross is not None
    ]
    if not selected:
        return {"samples": 0}
    gross = np.array([sample.gross for sample in selected], dtype=float)
    market_excess = [sample.excess for sample in selected if sample.excess is not None]
    sector_excess = [
        sample.gross - sector_returns[(sample.code, sample.signal_date, sample.horizon)]
        for sample in selected
        if sector_returns.get((sample.code, sample.signal_date, sample.horizon)) is not None
    ]
    return {
        "samples": len(selected),
        "dates": len({sample.signal_date for sample in selected}),
        "mean": round(float(gross.mean()), 2),
        "median": round(float(np.median(gross)), 2),
        "pos_pct": round(float((gross > 0).mean() * 100), 1),
        "market_excess_mean": round(float(np.mean(market_excess)), 2) if market_excess else None,
        "market_excess_median": round(float(np.median(market_excess)), 2) if market_excess else None,
        "market_beat_pct": round(float(np.mean([1 if x > 0 else 0 for x in market_excess]) * 100), 1)
        if market_excess
        else None,
        "sector_excess_mean": round(float(np.mean(sector_excess)), 2) if sector_excess else None,
        "sector_excess_median": round(float(np.median(sector_excess)), 2) if sector_excess else None,
        "sector_beat_pct": round(float(np.mean([1 if x > 0 else 0 for x in sector_excess]) * 100), 1)
        if sector_excess
        else None,
    }


def main() -> int:
    args = _args()
    path = Path(args.db).resolve()
    if not path.exists():
        raise SystemExit(f"数据库不存在：{path}")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.environ["DB_PATH"] = str(path)

    from app.db import session_scope
    from app.models import StockDaily
    from app.services import research_backtest as rb

    horizons = _horizons(args.horizons)
    with session_scope() as session:
        logging.info("生成趋势模板信号…")
        base = rb.signals_from_trend_template(
            session, args.start, args.end, candidate_top=300, mode="rps60", min_rs=60.0
        )
        grouped = defaultdict(list)
        for signal in base:
            if float(signal.meta.get("rps", -1)) >= args.min_rs:
                grouped[signal.signal_date].append(signal)
        signals = []
        for day in sorted(grouped):
            signals.extend(sorted(grouped[day], key=lambda s: (-float(s.meta["rps"]), s.code))[: args.top])
        logging.info("候选信号 %d 条", len(signals))

        days = sorted({signal.signal_date for signal in signals})
        codes = sorted({signal.code for signal in signals})
        board_members = _members(session, args.taxonomy, days, codes)
        members_by_board = defaultdict(set)
        for (code, day), boards in board_members.items():
            for board in boards:
                members_by_board[(board, day)].add(code)
        eligible = [signal for signal in signals if board_members.get((signal.code, signal.signal_date))]
        logging.info("有回补板块归属的候选 %d 条", len(eligible))

        metrics = _board_metrics(session, args.taxonomy, args.start, args.end)
        flags = {}
        for signal in eligible:
            boards = board_members[(signal.code, signal.signal_date)]
            values = [(board, metrics.get((board, signal.signal_date), {})) for board in boards]
            best_board, best = max(values, key=lambda item: item[1].get("rps20") or -1)
            flags[(signal.code, signal.signal_date)] = {
                "boards": boards,
                "best_board": best_board,
                "rps20": best.get("rps20"),
                "delta5": best.get("delta5"),
                "strong_days": best.get("strong_days", 0),
                "B": any((v.get("rps20") or 0) >= 90 for _b, v in values),
                "C": any((v.get("strong_days") or 0) >= 3 for _b, v in values),
                "D": any((v.get("delta5") or -999) >= 10 for _b, v in values),
            }
        # E：按当日最强板块 RPS20 排序取前 50%
        by_day = defaultdict(list)
        for signal in eligible:
            by_day[signal.signal_date].append(signal)
        e_keep = set()
        for day, items in by_day.items():
            items = sorted(items, key=lambda s: -(flags[(s.code, s.signal_date)]["rps20"] or -1))
            for signal in items[: max(1, len(items) // 2)]:
                e_keep.add((signal.code, signal.signal_date))

        data_end = session.scalar(select(func.max(StockDaily.trade_date)))
        matrix = rb.load_matrix(session, args.start, data_end)
        benchmark = rb.Benchmark(session, matrix, "market")
        result = rb.run_signals(
            session,
            eligible,
            strategy="sector_resonance",
            params={"taxonomy": args.taxonomy, "top": args.top, "min_rs": args.min_rs},
            horizons=horizons,
            entry_mode="next_open",
            cost_bps=args.cost_bps,
            matrix=matrix,
            benchmark=benchmark,
        )

        sector_returns = {}
        for sample in result.samples:
            if sample.status != "ok":
                continue
            info = flags.get((sample.code, sample.signal_date))
            if not info:
                continue
            value = _sector_benchmark(
                session,
                matrix,
                members_by_board,
                info["best_board"],
                sample.signal_date,
                sample.entry_date,
                sample.exit_date,
            )
            if value is not None:
                sector_returns[(sample.code, sample.signal_date, sample.horizon)] = value

        groups = {"A": eligible}
        for name in ("B", "C", "D"):
            groups[name] = [s for s in eligible if flags[(s.code, s.signal_date)][name]]
        groups["E"] = [s for s in eligible if (s.code, s.signal_date) in e_keep]

        group_output = {}
        for name, items in groups.items():
            keys = {(s.code, s.signal_date) for s in items}
            subset = [s for s in result.samples if (s.code, s.signal_date) in keys]
            group_output[name] = {
                "signals": len(items),
                **{str(h): _stat(subset, h, sector_returns) for h in horizons},
            }

        output = {
            "params": {
                "start": str(args.start),
                "end": str(args.end),
                "taxonomy": args.taxonomy,
                "horizons": list(horizons),
                "top": args.top,
                "min_rs": args.min_rs,
                "cost_bps": args.cost_bps,
                "entry": "next_open",
            },
            "data": {
                "db": str(path),
                "candidate_signals": len(signals),
                "eligible_signals": len(eligible),
                "signal_dates": len(days),
                "boards_backfilled": len({board for boards in board_members.values() for board in boards}),
                "data_end": str(data_end),
            },
            "groups": group_output,
            "note": "40/60 日只统计已走完的样本；样本不足时以 samples/dates 判断，不补造收益。",
        }
    if args.report:
        Path(args.report).write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: output[k] for k in ("params", "data")}, ensure_ascii=False, indent=2))
    for name in ("A", "B", "C", "D", "E"):
        row = output["groups"][name]
        print(
            name,
            "signals",
            row["signals"],
            {
                str(h): (
                    row[str(h)].get("samples"),
                    row[str(h)].get("market_excess_mean"),
                    row[str(h)].get("sector_excess_mean"),
                )
                for h in horizons
            },
        )
    if args.report:
        print(f"报告：{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
