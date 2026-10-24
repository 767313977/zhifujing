"""涨停接力（首板一进二）回测：按页面上的候选口径，算次日开盘买入后 1/2/3/5 日的表现。

## 为什么单独写一个脚本（不复用 `backtest_patterns.py`）

两者回答的问题不同：

- `backtest_patterns` 是「**形态**命中了，后面 N 天怎么样」——信号从日线里算出来，
  同一只票 20 个交易日内去重；
- 这里是「**候选池**（首板 · 未炸板 · 首封时间 · 市值 · 题材家数）次日开盘买入，
  拿几天」——它本身就是逐日策略，**不去重**（每天都是一次独立的开仓机会），
  而且要按打板的成交约束算（次日竞价封死 = 买不到）。

所以候选构造**直接调生产实现** `services.pools.first_board`，回测和页面永远是同一批票；
只有成交与持有期在本地算。

## 成交假设（与 `docs/选股池回测口径` 一致）

- **买入**：D 日收盘后出候选，D+1 **开盘价**成交；
- **买不到**：D+1 开盘价已经是涨停价（竞价即封 / 一字板）→ 剔除并单独计数，不进收益；
- **卖出**：D+1 ~ D+5 的收盘（1 / 2 / 3 / 5 日四档）；
- **基准**：同一天全市场等权的同一口径（次日开盘买入、第 h 日收盘卖出，同样剔除买不到的），
  超额 = 候选均值 − 当日基准；
- **不扣费用**（佣金+印花税约 0.13%，回合越短影响越大，看 1 日档时要自己心里打个折）。

## 数据范围（决定了样本量）

`limit_pool` 只从 **2025-09** 起有完整历史（更早的只有原始接口最近 15 天），所以本回测
最多约一年、200 多个交易日；`stock_daily` 主库只留最近 400 个交易日，两者取交集。

    python scripts/backtest_relay.py                 # 全部参数组合
    python scripts/backtest_relay.py --days 120      # 只回看最近 120 个交易日
"""

import argparse
import logging
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import func, select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.models import LimitPool, StockDaily, TradeCalendar  # noqa: E402
from app.services import limit_rules, pools  # noqa: E402

logger = logging.getLogger("relay")

#: 持有期（交易日）：D+h 的收盘卖出
HORIZONS = (1, 2, 3, 5)

#: 参数组合：第一组是页面默认值，其余逐项放宽 / 收紧，看阈值到底有没有用
COMBOS: tuple[tuple[str, dict], ...] = (
    ("默认（14:00 / 20-300亿 / 题材≥3）", {}),
    ("首封 ≤10:30", {"before": "103000"}),
    ("首封 ≤11:30", {"before": "113000"}),
    ("市值 30~150 亿", {"min_float_mv": 30, "max_float_mv": 150}),
    ("题材 ≥5 家", {"min_themes": 5}),
    ("只看真首板", {"only_true_first_board": True}),
    ("封单/流通 ≥0.3%", {"min_seal_ratio": 0.3}),
)

#: 板价四舍五入到分的相对容差（与 `backtest_patterns._unbuyable` 同一口径）
_OPEN_LIMIT_TOL_CAP = 0.005


class Tally:
    """一个参数组合 × 一个持有期的累加器（分位数用 `statistics` 现算）。"""

    __slots__ = ("values", "excess")

    def __init__(self) -> None:
        self.values: list[float] = []
        #: 逐样本减去**它自己那天**的全市场均值（不是整体平均后再减）
        self.excess: list[float] = []

    def add(self, value: float, market: float) -> None:
        self.values.append(value)
        if market == market:  # 不是 nan
            self.excess.append(value - market)

    def mean(self) -> float:
        return sum(self.values) / len(self.values) if self.values else 0.0

    def excess_mean(self) -> float | None:
        return sum(self.excess) / len(self.excess) if self.excess else None

    def median(self) -> float:
        return statistics.median(self.values) if self.values else 0.0

    def win(self) -> float:
        if not self.values:
            return 0.0
        return sum(1 for value in self.values if value > 0) / len(self.values)


def _tradable(code: str, open_: float, close: float, pct: float) -> bool:
    """D+1 开盘价是不是还在涨停板上（买不到）。判据与 `backtest_patterns` 同一套。"""
    if not close or pct is None or pct <= -100 or not open_:
        return False
    prev_close = close / (1 + pct / 100)
    if prev_close <= 0:
        return False
    tol = min(_OPEN_LIMIT_TOL_CAP, 0.005 / prev_close)
    limit = limit_rules.limit_pct(code, None)
    return open_ / prev_close - 1 < limit / 100 - tol


def _load_window(session, days: list[date]) -> dict[date, dict[str, tuple]]:
    """`{交易日: {代码: (开盘, 收盘, 涨跌幅)}}`，只取这几天。"""
    data: dict[date, dict[str, tuple]] = {day: {} for day in days}
    for code, day, open_, close, pct in session.execute(
        select(
            StockDaily.code,
            StockDaily.trade_date,
            StockDaily.open,
            StockDaily.close,
            StockDaily.pct_chg,
        ).where(StockDaily.trade_date.in_(days))
    ):
        data[day][code] = (open_, close, pct)
    return data


def _market_returns(
    data: dict[date, dict[str, tuple]], days: list[date]
) -> dict[int, float]:
    """全市场等权的 h 日收益（次日开盘买入、第 h 日收盘卖出）。"""
    entry_day = days[0]
    codes = data[entry_day]
    out: dict[int, float] = {}
    for h in HORIZONS:
        exit_day = days[h - 1]
        exits = data[exit_day]
        total = 0.0
        count = 0
        for code, (open_, close, pct) in codes.items():
            if code not in exits:
                continue
            if not _tradable(code, open_, close, pct):
                continue
            exit_close = exits[code][1]
            if not open_ or not exit_close:
                continue
            total += exit_close / open_ - 1
            count += 1
        out[h] = total / count if count else float("nan")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="首板一进二（涨停接力）回测")
    parser.add_argument("--days", type=int, default=0, help="只回看最近 N 个交易日（0 = 全部）")
    parser.add_argument("--combos", default="", help="只跑名字里含这些关键词的组合（逗号分隔）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    wanted = [item.strip() for item in args.combos.split(",") if item.strip()]

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
        if data_end is None:
            raise SystemExit("stock_daily 是空的，先跑 collect_kline")

        # 只留下「后面还有 max(HORIZONS) 个交易日、且日线已采到」的日子
        usable: list[date] = []
        for day in limit_days:
            index = position.get(day)
            if index is None or index + max(HORIZONS) >= len(calendar):
                continue
            if calendar[index + max(HORIZONS)] > data_end:
                continue
            usable.append(day)
        if args.days:
            usable = usable[-args.days:]
        if not usable:
            raise SystemExit("没有可用交易日：涨停池与日线的交集为空")
        logger.info(
            "回测区间 %s ~ %s（%d 个交易日），日线截至 %s",
            usable[0],
            usable[-1],
            len(usable),
            data_end,
        )

        tallies: dict[str, dict[int, Tally]] = {
            label: {h: Tally() for h in HORIZONS} for label, _ in COMBOS
        }
        unbuyable: dict[str, int] = defaultdict(int)
        candidates: dict[str, int] = defaultdict(int)
        daily_rows = 0

        for day in usable:
            index = position[day]
            window = calendar[index + 1 : index + 1 + max(HORIZONS)]
            data = _load_window(session, window)
            market = _market_returns(data, window)
            entry_day = window[0]
            quotes = data[entry_day]

            for label, params in COMBOS:
                board = pools.first_board(session, trade_date=day, limit=1000, **params)
                for row in board.rows:
                    quote = quotes.get(row.code)
                    if quote is None:
                        continue
                    candidates[label] += 1
                    open_, close, pct = quote
                    if not _tradable(row.code, open_, close, pct):
                        unbuyable[label] += 1
                        continue
                    for h in HORIZONS:
                        exit_close = data[window[h - 1]].get(row.code, (None, None, None))[1]
                        if not exit_close or not open_:
                            continue
                        tallies[label][h].add(exit_close / open_ - 1, market[h])
            daily_rows += 1

    # ---------------- 报告 ----------------
    print()
    print(f"回测区间：{usable[0]} ~ {usable[-1]}（{len(usable)} 个交易日）")
    print("买入 = 次日开盘；卖出 = 第 h 个交易日收盘；超额 = 候选均值 − 当日全市场等权")
    print()
    header = f"{'参数组合':<28}{'候选':>6}{'买不到':>7}"
    for h in HORIZONS:
        header += f"{h:>9}日收益{h:>6}日超额"
    print(header)
    print("-" * len(header))
    for label, _ in COMBOS:
        if wanted and not any(word in label for word in wanted):
            continue
        line = f"{label:<28}{candidates[label]:>6}{unbuyable[label]:>7}"
        for h in HORIZONS:
            tally = tallies[label][h]
            excess = tally.excess_mean()
            shown = "     —" if excess is None else f"{excess * 100:>+8.2f}%"
            line += f"{tally.mean() * 100:>9.2f}%{shown}"
        print(line)

    print()
    print("各组合的中位收益（打板收益右偏，均值会被少数大肉签拉高，必须一起看中位）")
    print()
    header = f"{'参数组合':<28}"
    for h in HORIZONS:
        header += f"{h:>9}日中位{h:>6}日胜率"
    print(header)
    print("-" * len(header))
    for label, _ in COMBOS:
        if wanted and not any(word in label for word in wanted):
            continue
        line = f"{label:<28}"
        for h in HORIZONS:
            tally = tallies[label][h]
            line += f"{tally.median() * 100:>8.2f}%{tally.win() * 100:>8.1f}%"
        print(line)

    print()
    print(f"扫描 {daily_rows} 个信号日；候选 = 当日按该参数选出的只数（含买不到的）")
    print("⚠️ 未扣费用（双边约 0.13%），1 日档的实际收益要再减 0.13pp 左右")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
