"""对照实验：首板候选「按板块综合分排」vs「按量价因子分排」，次日开盘买入谁更好。

只回答一个问题：**同一批候选，换一个排序，前 N 只的次日表现有没有差别**。
不改入选集合、不改成交假设（与 `scripts/backtest_relay.py` 完全一致）：

- D 日收盘后出候选，D+1 **开盘价**买入、**收盘价**卖出；
- D+1 开盘即涨停（买不到）的剔除，单独计数；
- 基准 = 同一天全市场等权的同一口径，超额逐样本减当日基准。

    python scripts/backtest_order.py                # 最近全部可用交易日
    python scripts/backtest_order.py --days 120     # 只看最近 120 天
"""

import argparse
import logging
import statistics
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
BACKEND = SCRIPTS.parent / "backend"
for path in (str(BACKEND), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

from sqlalchemy import func, select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.models import LimitPool, StockDaily, TradeCalendar  # noqa: E402
from app.services import pools  # noqa: E402
from backtest_relay import _tradable  # noqa: E402  （同一套「买不到」判据）

logger = logging.getLogger("order")

#: 每组取前几只
TOPS = (5, 10)
#: 两种排序
ORDERS = (("板块综合分", "board"), ("量价因子分", "factor"))


def main() -> int:
    parser = argparse.ArgumentParser(description="首板排序对照实验")
    parser.add_argument("--days", type=int, default=0, help="只回看最近 N 个信号日（0 = 全部）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    with session_scope() as session:
        calendar = list(
            session.scalars(select(TradeCalendar.trade_date).order_by(TradeCalendar.trade_date))
        )
        position = {day: index for index, day in enumerate(calendar)}
        limit_days = list(
            session.scalars(
                select(LimitPool.trade_date)
                .where(LimitPool.pool_type == "up")
                .group_by(LimitPool.trade_date)
                .order_by(LimitPool.trade_date)
            )
        )
        data_end = session.scalar(select(func.max(StockDaily.trade_date)))
        usable = [
            day
            for day in limit_days
            if position.get(day) is not None
            and position[day] + 1 < len(calendar)
            and data_end is not None
            and calendar[position[day] + 1] <= data_end
        ]
        if args.days:
            usable = usable[-args.days:]
        if not usable:
            raise SystemExit("没有可用交易日")

        buckets = {
            (label, top): {"ret": [], "excess": [], "missing": 0}
            for label, _ in ORDERS
            for top in TOPS
        }

        for day in usable:
            following = calendar[position[day] + 1]
            quotes = {
                code: (open_, close, pct)
                for code, open_, close, pct in session.execute(
                    select(
                        StockDaily.code,
                        StockDaily.open,
                        StockDaily.close,
                        StockDaily.pct_chg,
                    ).where(StockDaily.trade_date == following)
                )
            }
            market_values = [
                close / open_ - 1
                for code, (open_, close, pct) in quotes.items()
                if _tradable(code, open_, close, pct) and open_ and close
            ]
            market = sum(market_values) / len(market_values) if market_values else float("nan")

            for label, order in ORDERS:
                board = pools.first_board(
                    session, trade_date=day, order=order, limit=1000
                )
                for top in TOPS:
                    bucket = buckets[(label, top)]
                    for row in board.rows[:top]:
                        quote = quotes.get(row.code)
                        if quote is None:
                            bucket["missing"] += 1
                            continue
                        open_, close, pct = quote
                        if not _tradable(row.code, open_, close, pct):
                            bucket["missing"] += 1
                            continue
                        value = close / open_ - 1
                        bucket["ret"].append(value)
                        if market == market:
                            bucket["excess"].append(value - market)

    print()
    print(f"信号日 {len(usable)} 天（{usable[0]} ~ {usable[-1]}）；买入次日开盘、卖出次日收盘")
    print()
    print(f"{'排序':<14}{'前N':>4}{'样本':>7}{'均值':>9}{'中位':>9}{'胜率':>8}{'超额':>9}{'买不到/缺':>10}")
    print("-" * 74)
    for label, _ in ORDERS:
        for top in TOPS:
            bucket = buckets[(label, top)]
            values = bucket["ret"]
            if not values:
                continue
            mean = sum(values) / len(values)
            excess = (
                sum(bucket["excess"]) / len(bucket["excess"]) if bucket["excess"] else float("nan")
            )
            win = sum(1 for value in values if value > 0) / len(values)
            print(
                f"{label:<14}{top:>4}{len(values):>7}{mean * 100:>8.2f}%"
                f"{statistics.median(values) * 100:>8.2f}%{win * 100:>7.1f}%"
                f"{excess * 100:>8.2f}%{bucket['missing']:>10}"
            )
    print()
    print("⚠️ 未扣费用；同一批候选只是换顺序，样本量相同，可直接比均值与中位")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
