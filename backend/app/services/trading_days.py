"""交易日历的共用查询（2026-10-22 体检后的纯重构）。

`is_trade_day` 原来在 4 个 job 模块里各写了一遍，查询完全一样
（`select trade_date where trade_date = :day`）—— 口径要调整就得改四处，
漏一处就是「同一天，有的任务说它是交易日、有的说不是」。

这里只放**不依赖采集器实例**的那一层：`DailyCollector.is_trade_day` 仍保留
（它挂在采集器上、带 iFinD 相关的前置状态），job 模块里需要「只查日历、
不为了一次判断去构造采集器」时用这个 —— 构造 `DailyCollector` 在 iFinD
没配好时会抛 `IfindError`，那不该拦住「今天是不是交易日」这种纯查询。
"""

from datetime import date

from sqlalchemy import select

from app.db import session_scope
from app.models import TradeCalendar


def is_trade_day(day: date) -> bool:
    """该日是否在交易日历里。

    日历为空时返回 False —— 全新部署的库要等第一次采集把日历建起来
    （见 `scheduler._run_daily` 里对 `calendar_needs_refresh` 的处理）。
    """
    with session_scope() as session:
        return (
            session.scalar(
                select(TradeCalendar.trade_date).where(TradeCalendar.trade_date == day)
            )
            is not None
        )
