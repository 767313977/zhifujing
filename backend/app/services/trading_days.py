"""交易日历的共用查询（2026-10-22 体检后的纯重构，2026-10-31 收口）。

**交易日相关的判断只有这里一处权威来源**（与 `app/timeutil.today_cn()` 配套）：

- 「今天」→ `app.timeutil.today_cn()`（固定 +8 偏移，不看进程时区）；
- 「是否交易日」→ `is_trade_day()`；
- 「上一交易日」→ `previous_trade_day()`。

`is_trade_day` 原来在 4 个 job 模块里各写了一遍、`previous_trade_day` 的查询在 6 个
模块里各写了一遍（auction / funds / limit / collect_board_flow / scan_dde / scheduler），
查询完全一样 —— 口径要调整就得改十处，漏一处就是「同一天，有的任务说它是交易日、
有的说不是」「有的接口说昨日是 A、有的说是 B」。新代码一律调这里，**不要再抄 SQL**。

这里只放**不依赖采集器实例**的那一层：`DailyCollector.is_trade_day` 仍保留
（它挂在采集器上、带 iFinD 相关的前置状态），但它已经改成直接转调 `is_trade_day` ——
job 模块里需要「只查日历、不为了一次判断去构造采集器」时用这个 —— 构造
`DailyCollector` 在 iFinD 没配好时会抛 `IfindError`，那不该拦住「今天是不是交易日」
这种纯查询。
"""

import logging
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import session_scope
from app.models import TradeCalendar
from app.timeutil import today_cn

logger = logging.getLogger(__name__)


def calendar_latest(session: Session | None = None) -> date | None:
    """日历铺到的**最后一天**；日历为空时返回 None。

    用来区分两件不同的事：「这一天不是交易日」与「日历根本没铺到这一天」
    （跨年时上游还没发布次年日历）。后者判断不了，调用方应放行而不是误拒 ——
    见 `trade_journal.validate_entry`。
    """
    stmt = select(func.max(TradeCalendar.trade_date))
    if session is not None:
        return session.scalar(stmt)
    with session_scope() as db:
        return db.scalar(stmt)


def calendar_needs_refresh(session: Session | None = None) -> bool:
    """交易日历是否为空或已落后（跨年时上游还没发布次年日历）。

    口径只有这一处：`collect_daily.DailyCollector.calendar_needs_refresh` 直接转调它。
    """
    latest = calendar_latest(session)
    return latest is None or latest < today_cn()


def is_trade_day_fresh(day: date, session: Session | None = None) -> bool:
    """带**日历自愈**的交易日判断：日历为空/停更时先补一次再判断。

    裸 `is_trade_day` 在日历停更期间保守返回 False，于是跨年那几天（上游还没发布
    次年日历）所有定时链都静默空跑、没人发现。`scheduler._run_daily` 早就带了这个
    自愈，其余几处（收盘后链路、次日早间补采、告警检查）漏了 —— 2026-11-01 统一。

    补日历要构造采集器（iFinD 没配好时会抛 `IfindError`），所以只在「确实需要刷新」
    时才付这个代价；补失败照旧返回 False，不让调度崩掉。
    """
    if is_trade_day(day, session):
        return True
    if not calendar_needs_refresh(session):
        return False
    try:
        from app.jobs.collect_daily import DailyCollector

        DailyCollector().collect_calendar()
    except Exception:  # noqa: BLE001 - 自愈失败不该让「今天是不是交易日」抛异常
        logger.exception("补交易日历失败（日历为空或已落后），本次按非交易日处理")
        return False
    return is_trade_day(day, session)


def is_trade_day(day: date, session: Session | None = None) -> bool:
    """该日是否在交易日历里。

    日历为空时返回 False —— 全新部署的库要等第一次采集把日历建起来
    （见 `scheduler._run_daily` 里对 `calendar_needs_refresh` 的处理）。

    接口层手里已经有会话就传进来（`is_trade_day(day, session)`），
    job 层省略即可（函数自己开短会话）。
    """
    stmt = select(TradeCalendar.trade_date).where(TradeCalendar.trade_date == day)
    if session is not None:
        return session.scalar(stmt) is not None
    with session_scope() as db:
        return db.scalar(stmt) is not None


def recent_trade_days(end: date, count: int, session: Session | None = None) -> list[date]:
    """`end` 之前（**不含 `end`**）最近的 `count` 个交易日，由近及远。

    竞价量比回看、首板近 N 日涨停、启动补采都问同一件事 —— 这里一条 SQL
    把「上一交易日」与「前 N 个交易日」收成一处。
    """
    if count <= 0:
        return []
    stmt = (
        select(TradeCalendar.trade_date)
        .where(TradeCalendar.trade_date < end)
        .order_by(TradeCalendar.trade_date.desc())
        .limit(count)
    )
    if session is not None:
        return list(session.scalars(stmt))
    with session_scope() as db:
        return list(db.scalars(stmt))


def previous_trade_day(day: date, session: Session | None = None) -> date | None:
    """`day` 之前最近的一个交易日（**不含 `day` 本身**）；日历里没有时返回 None。

    ⚠️ 一律按**交易日历**取，不按某张业务表自己的日期：池子/快照缺一天时
    （回补没跑到、上游那天没数据），拿业务表的「上一个有数据的日子」会把 3 天前的
    批次当成「昨日」，晋级率、环比之类的对比整个错位。

    接口层手里已经有会话就传进来（`previous_trade_day(day, session)`）；
    job 层没有会话时省略，函数自己开一个短会话。两种用法走同一条 SQL。
    """
    days = recent_trade_days(day, 1, session)
    return days[0] if days else None


def next_trade_day(day: date, session: Session | None = None) -> date | None:
    """`day` 之后最近的一个交易日（**不含 `day` 本身**）；日历里没有时返回 None。

    名单一键生成计划时用（信号日的下一个交易日才是执行日）。日历只铺到当年年底，
    跨年那几天会返回 None —— 调用方必须处理，不能拿 `day + 1 天` 顶替
    （那就是把周末当交易日）。
    """
    stmt = (
        select(TradeCalendar.trade_date)
        .where(TradeCalendar.trade_date > day)
        .order_by(TradeCalendar.trade_date)
        .limit(1)
    )
    if session is not None:
        return session.scalar(stmt)
    with session_scope() as db:
        return db.scalar(stmt)
