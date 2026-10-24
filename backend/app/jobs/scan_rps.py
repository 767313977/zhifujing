"""全市场 RPS（相对强度）扫描：近 120 / 250 日涨幅 → 横截面百分位。

零外部请求、零 iFinD 配额，只读 `stock_daily`。跑一次约 1~3 秒（130 万行流式读），
挂在收盘尾部链上每天重算当天。

## 口径

- 区间涨幅用 `pct_chg` **复利**（库里是不复权价，直接比收盘价会被除权除息带偏）；
- 窗口是**交易日**：取最近 250 个交易日，停牌缺天按实际可用天数算，
  但至少要有 90% 的天数，否则记 None；
- 分位用**中位秩**（与恐贪、估值分位同一套定义），0 = 全市场最弱、100 = 最强。

## 为什么只存「当天一行」

RPS 是横截面指标，历史值对「今天选股」没有增量；存全市场每天一行已经够用
（约 5000 行/天）。将来要做历史回测再按需回补。
"""

import logging
import time
from bisect import bisect_left, bisect_right
from datetime import date

from sqlalchemy import func, select

from app.db import session_scope, upsert_many
from app.models import RpsDaily, StockDaily, TradeCalendar

logger = logging.getLogger(__name__)

#: 两个窗口（交易日）
WINDOWS = (120, 250)

#: 窗口内至少要有这么多比例的实际交易日，否则该档记 None。
#: 0.9 能让「停牌几天」的票照常参与，又不至于拿半个窗口当 250 日涨幅。
MIN_COVERAGE = 0.9

#: 一次流式读多少行。130 万行整表读进内存约几百 MB，按块读只占常数内存。
_STREAM_BATCH = 20000


def _ret(values: list[float], days: int) -> float | None:
    """窗口涨幅（%）。`values` 是该股在窗口内按日期升序的日涨跌幅。"""
    if len(values) < int(days * MIN_COVERAGE):
        return None
    factor = 1.0
    for pct in values[-days:]:
        factor *= 1 + pct / 100
    return round((factor - 1) * 100, 2)


def _mid_rank(ordered: list[float], value: float) -> float:
    """中位秩百分位（0-100）。`ordered` 必须是升序、且已去掉 None。"""
    left = bisect_left(ordered, value)
    right = bisect_right(ordered, value)
    return round((left + right) / 2 / len(ordered) * 100, 1)


def scan(trade_date: date | None = None) -> dict:
    """重算 `trade_date`（缺省 = 库里最新日线日）的 RPS，返回写入行数。"""
    started = time.monotonic()
    with session_scope() as session:
        target = trade_date or session.scalar(
            select(func.max(StockDaily.trade_date)).where(StockDaily.pct_chg.is_not(None))
        )
        if target is None:
            return {"rows": 0, "trade_date": None, "cost_seconds": 0.0}

        trade_days = list(
            session.scalars(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date <= target)
                .order_by(TradeCalendar.trade_date.desc())
                .limit(max(WINDOWS))
            )
        )
        if not trade_days:
            return {"rows": 0, "trade_date": target, "cost_seconds": 0.0}
        start = min(trade_days)

        statement = (
            select(StockDaily.code, StockDaily.pct_chg)
            .where(
                StockDaily.trade_date >= start,
                StockDaily.trade_date <= target,
                StockDaily.pct_chg.is_not(None),
            )
            .order_by(StockDaily.code, StockDaily.trade_date)
            .execution_options(yield_per=_STREAM_BATCH)
        )

        returns: dict[str, tuple[float | None, float | None]] = {}
        current = ""
        values: list[float] = []

        def flush() -> None:
            if not current:
                return
            short, long = (_ret(values, window) for window in WINDOWS)
            if short is not None or long is not None:
                returns[current] = (short, long)

        for code, pct in session.execute(statement):
            if code != current:
                flush()
                current = code
                values = []
            values.append(float(pct))
        flush()

        # 分位：先按各窗口筛出有值的票，再排序做中位秩
        short_values = sorted(value[0] for value in returns.values() if value[0] is not None)
        long_values = sorted(value[1] for value in returns.values() if value[1] is not None)
        if not short_values and not long_values:
            return {"rows": 0, "trade_date": target, "cost_seconds": 0.0}

        records = []
        for code, (short, long) in returns.items():
            records.append(
                {
                    "trade_date": target,
                    "code": code,
                    "ret120": short,
                    "ret250": long,
                    "rps120": None if short is None else _mid_rank(short_values, short),
                    "rps250": None if long is None else _mid_rank(long_values, long),
                }
            )

        written = upsert_many(session, RpsDaily, records)

    cost = time.monotonic() - started
    logger.info(
        "RPS 扫描 %s：%d 只（120 日 %d 只 / 250 日 %d 只），用时 %.1fs",
        target,
        written,
        len(short_values),
        len(long_values),
        cost,
    )
    return {"rows": written, "trade_date": target, "cost_seconds": round(cost, 2)}
