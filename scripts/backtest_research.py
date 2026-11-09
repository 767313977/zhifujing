"""B0.5 基线策略复测 CLI：调用统一回测内核，不写库、不连生产。

用法：

    python scripts/backtest_research.py --db backend/data/b0_trial.db \
        --strategy rps --rps-mode rps60 --top 50 --days 60 --entry next_open \
        --report backend/data/b0_bt_rps60.json

    python scripts/backtest_research.py --db backend/data/fupan.db \
        --strategy trend --days 120 --entry next_open

    python scripts/backtest_research.py --db backend/data/fupan.db \
        --strategy pattern --patterns platform_breakout,box_breakout --top 50

    python scripts/backtest_research.py --db backend/data/b0_trial.db \
        --strategy sector --taxonomy kph_selected --sectors 3 --per-sector 3

口径与限制见 `app.services.research_backtest` 的模块说明。所有策略参数会写进报告，
确保同一数据版本、同一参数、同一日期可复现。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
BACKEND = ROOT / "backend"
for path in (str(BACKEND), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="B0 统一回测 CLI")
    parser.add_argument("--db", required=True)
    parser.add_argument("--strategy", required=True, choices=("rps", "trend", "pattern", "sector"))
    parser.add_argument("--days", type=int, default=120, help="回看多少个交易日（默认 120）")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--horizons", default="5,10,20,40,60")
    parser.add_argument("--entry", default="next_open", choices=("next_open", "close"))
    parser.add_argument("--cost-bps", type=float, default=0.0, help="一次完整买卖往返的总成本（bps）")
    parser.add_argument("--top", type=int, default=50)
    parser.add_argument("--rps-mode", default="rps60", choices=("rps20", "rps50", "rps60", "rps120", "rps250"))
    parser.add_argument("--min-rps", type=float, default=0.0)
    parser.add_argument("--min-avg-amount", type=float, default=0.0, help="20 日平均成交额下限（元）")
    parser.add_argument("--patterns", default="platform_breakout,box_breakout,turtle_breakout")
    parser.add_argument("--taxonomy", default="kph_selected")
    parser.add_argument("--sectors", type=int, default=3)
    parser.add_argument("--per-sector", type=int, default=3)
    parser.add_argument("--benchmark", default="market", help="market 或 index:000001.SH")
    parser.add_argument("--max-calendar-days", type=int, default=400, help="矩阵窗口上限，防止一次性拉太多行")
    parser.add_argument("--samples", action="store_true", help="报告里包含逐样本明细（会很大）")
    parser.add_argument("--report")
    return parser.parse_args()


def _horizons(text: str) -> tuple[int, ...]:
    values = tuple(sorted({int(item) for item in text.split(",") if item.strip()}))
    if not values or min(values) <= 0:
        raise SystemExit("--horizons 必须是正整数")
    return values


def _date_range(session, args) -> tuple[date, date]:
    from sqlalchemy import func, select

    from app.models import StockDaily, TradeCalendar

    end = args.end or session.scalar(select(func.max(StockDaily.trade_date)))
    if end is None:
        raise SystemExit("库里没有日线")
    if args.start:
        start = args.start
    else:
        days = list(
            session.scalars(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date <= end)
                .order_by(TradeCalendar.trade_date.desc())
                .limit(args.days)
            )
        )
        if not days:
            raise SystemExit("交易日历为空")
        start = min(days)
    span = list(
        session.scalars(
            select(TradeCalendar.trade_date).where(
                TradeCalendar.trade_date >= start, TradeCalendar.trade_date <= end
            )
        )
    )
    if len(span) > args.max_calendar_days:
        raise SystemExit(
            f"窗口 {len(span)} 个交易日超过 --max-calendar-days={args.max_calendar_days}；"
            "请缩小窗口或显式放大上限（矩阵会占很多内存）。"
        )
    return start, end


def _result_json(result, include_samples: bool) -> dict:
    payload = {
        "strategy": result.strategy,
        "params": result.params,
        "start": str(result.start) if result.start else None,
        "end": str(result.end) if result.end else None,
        "entry_mode": result.entry_mode,
        "cost_bps": result.cost_bps,
        "data_coverage": result.data_coverage,
        "warnings": result.warnings,
        "horizons": {},
    }
    for horizon, stat in result.horizons.items():
        payload["horizons"][horizon] = {
            "observation_samples": stat.observation_samples,
            "execution_samples": stat.execution_samples,
            "counts": stat.counts,
            "mean": stat.mean,
            "median": stat.median,
            "pos_pct": stat.pos_pct,
            "neg_pct": stat.neg_pct,
            "excess_mean": stat.excess_mean,
            "excess_median": stat.excess_median,
            "beat_pct": stat.beat_pct,
            "net_mean": stat.net_mean,
            "net_median": stat.net_median,
            "mae_mean": stat.mae_mean,
            "distribution": stat.distribution,
            "regimes": {
                name: {
                    "observation_samples": item.observation_samples,
                    "execution_samples": item.execution_samples,
                    "mean": item.mean,
                    "median": item.median,
                    "excess_mean": item.excess_mean,
                    "beat_pct": item.beat_pct,
                }
                for name, item in stat.regimes.items()
            },
        }
    if include_samples:
        payload["samples"] = [
            {
                "code": s.code,
                "signal_date": str(s.signal_date),
                "horizon": s.horizon,
                "status": s.status,
                "entry_date": str(s.entry_date) if s.entry_date else None,
                "exit_date": str(s.exit_date) if s.exit_date else None,
                "entry_price": s.entry_price,
                "exit_price": s.exit_price,
                "gross": s.gross,
                "net": s.net,
                "mae": s.mae,
                "benchmark": s.benchmark,
                "excess": s.excess,
                "flags": list(s.flags),
            }
            for s in result.samples
        ]
    return payload


def main() -> int:
    args = _args()
    path = Path(args.db).resolve()
    if not path.exists():
        raise SystemExit(f"数据库不存在：{path}")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.environ["DB_PATH"] = str(path)

    from app.db import session_scope
    from app.services import research_backtest as rb

    horizons = _horizons(args.horizons)
    with session_scope() as session:
        start, end = _date_range(session, args)
        if args.strategy == "rps":
            signals = rb.signals_from_rps(
                session,
                start,
                end,
                top=args.top,
                mode=args.rps_mode,
                min_rps=args.min_rps,
                min_avg_amount=args.min_avg_amount,
            )
            params = {
                "top": args.top,
                "rps_mode": args.rps_mode,
                "min_rps": args.min_rps,
                "min_avg_amount": args.min_avg_amount,
            }
        elif args.strategy == "trend":
            signals = rb.signals_from_trend_template(
                session, start, end, candidate_top=max(args.top, 200), mode=args.rps_mode
            )
            params = {"candidate_top": max(args.top, 200), "rps_mode": args.rps_mode}
        elif args.strategy == "pattern":
            patterns = [item.strip() for item in args.patterns.split(",") if item.strip()]
            signals = rb.signals_from_pattern(
                session, start, end, patterns=patterns, top=args.top, min_avg_amount=args.min_avg_amount
            )
            params = {"patterns": patterns, "top": args.top, "min_avg_amount": args.min_avg_amount}
        else:
            signals = rb.signals_from_sector_stock(
                session,
                start,
                end,
                taxonomy=args.taxonomy,
                sectors=args.sectors,
                per_sector=args.per_sector,
            )
            params = {
                "taxonomy": args.taxonomy,
                "sectors": args.sectors,
                "per_sector": args.per_sector,
            }
        result = rb.run_signals(
            session,
            signals,
            strategy=args.strategy,
            params=params,
            horizons=horizons,
            entry_mode=args.entry,
            cost_bps=args.cost_bps,
            benchmark_mode=args.benchmark,
        )

    print(
        f"{args.strategy}：{start} ~ {end}，信号 {len(signals)} 条，"
        f"入场 {args.entry}，费用 {args.cost_bps} bps"
    )
    print("持有期 | 观察样本 | 可成交样本 | 均值 | 中位 | 正收益% | 超额均值 | 跑赢% | 扣费均值 | MAE均值")
    for horizon in horizons:
        stat = result.horizons[horizon]
        print(
            f"{horizon:>4} | {stat.observation_samples:>6} | {stat.execution_samples:>8} | "
            f"{stat.mean} | {stat.median} | {stat.pos_pct} | {stat.excess_mean} | "
            f"{stat.beat_pct} | {stat.net_mean} | {stat.mae_mean}"
        )
    payload = _result_json(result, args.samples)
    if args.report:
        Path(args.report).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"报告：{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
