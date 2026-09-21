"""iFinD 调用配额：计量与外推。

为什么单独成模块而不是塞进采集流程：**额度是账号级的、被所有链路共享** ——
定时采集、形态选股、手工补数都在花同一个池子，所以「谁花了多少、还剩多少」
需要一个不隶属于任何一条链路的统一账本。

计量单位是**订阅周期**不是自然月：iFinD 后台「计量区间」是
`2026-09-17 16:55:33 ~ 2026-10-17 16:55:33` 这种滚动窗口，起点即订阅日。
按自然月统计会让周期末的用量被丢进新月份，守卫**低估**用量 → 撞墙，所以
下面的 cycle_* 一律以 `IFIND_CYCLE_START_DAY` 推出的窗口为准。

真正的调用点只有一个：`sources/ifind.py` 的 `call()`。
"""

import calendar
import logging
from datetime import date, timedelta
from enum import IntEnum

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.config import Settings, get_settings
from app.db import session_scope
from app.models import IfindUsage, TradeCalendar

logger = logging.getLogger(__name__)

# 月末用量外推至少要有这么多个已过交易日的样本才给结果
PROJECTION_MIN_DAYS = 3


class QuotaLevel(IntEnum):
    """配额紧张程度，数值越大越紧张，判定一律用 `>=`。"""

    NORMAL = 0
    # 80%：停形态选股的全市场日线更新（形态结果不再刷新，历史仍可看）
    PAUSE_KLINE = 1
    # 90%：停概念板块的 iFinD 兜底（记空值，次日由同花顺订正）
    PAUSE_CONCEPT = 2
    # 95%：只保留指数 + 涨停三池 + 情绪这条主线
    CORE_ONLY = 3


# 阈值与档位。顺序必须**从高到低**，第一个命中的就是当前档
LEVEL_THRESHOLDS = (
    (0.95, QuotaLevel.CORE_ONLY),
    (0.90, QuotaLevel.PAUSE_CONCEPT),
    (0.80, QuotaLevel.PAUSE_KLINE),
)

LEVEL_LABELS = {
    QuotaLevel.NORMAL: "正常",
    QuotaLevel.PAUSE_KLINE: "已停形态选股的全市场日线更新（配额用掉 80%）",
    QuotaLevel.PAUSE_CONCEPT: "已停概念板块的 iFinD 兜底（配额用掉 90%）",
    QuotaLevel.CORE_ONLY: "只保留指数 / 涨停三池 / 情绪（配额用掉 95%）",
}


def _day_in(year: int, month: int, day: int) -> date:
    """取某年某月的第 N 天，N 超过当月天数时用月末兜底。

    配置层已把起点日限制在 1-28，这里是防调用方传越界值直接抛 ValueError ——
    配额模块是纯记账 + 守卫，不该因为一个参数把整次采集带崩。
    """
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def cycle_start(day: date, start_day: int) -> date:
    """当前计量周期的第一天（含）。

    iFinD 按**订阅周期**计量，不是自然月：后台「计量区间」实测是
    `2026-09-17 16:55:33 ~ 2026-10-17 16:55:33` 这种滚动窗口，起止都落在
    订阅日。所以「这个窗口花了多少」必须按这个窗口算，不能用
    `day.replace(day=1)` —— 那会让周期末（如 10-01 ~ 10-16）的用量被丢进
    新自然月，守卫**低估**已用量 → 该让路时不动作 → 撞墙。

    ⚠️ 真实边界是**时刻**（16:55:33），而计量表只记到「日」。这里按日粒度
    近似：订阅日当天一律划进新周期。代价是订阅日 16:55 之前的那几次调用被
    算进新周期，即周期初**高估**用量。高估的后果只是早一点让路，低估的后果
    是真的撞墙 —— 两害相权取高估。
    """
    if day.day >= start_day:
        return _day_in(day.year, day.month, start_day)
    year, month = (day.year - 1, 12) if day.month == 1 else (day.year, day.month - 1)
    return _day_in(year, month, start_day)


def cycle_end(start: date, start_day: int) -> date:
    """周期最后一天（含）= 下一个订阅日的前一天。"""
    next_year, next_month = (
        (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    )
    return _day_in(next_year, next_month, start_day) - timedelta(days=1)


def record_call(server: str, tool: str, day: date | None = None) -> None:
    """累加一次调用。

    **失败只记日志，绝不抛出**：计量是采集的副作用，不能因为它写不进库
    就把整次采集带崩。少记一次可以接受，采集中断不可以。

    用 SQL 的原子自增，而不是「读出来加一再写回去」：采集线程与接口线程可能
    同时写同一行，后者会丢计数 —— 而丢计数会让配额守卫**低估**用量，
    低估恰恰是危险的那个方向。
    """
    target = day or date.today()
    try:
        with session_scope() as session:
            session.execute(
                sqlite_insert(IfindUsage)
                .values(usage_date=target, server=server, tool=tool, calls=1)
                .on_conflict_do_update(
                    index_elements=[
                        IfindUsage.usage_date,
                        IfindUsage.server,
                        IfindUsage.tool,
                    ],
                    set_={"calls": IfindUsage.calls + 1},
                )
            )
    except Exception:  # noqa: BLE001 - 见 docstring：计量失败不能影响采集
        logger.warning("记录 iFinD 调用次数失败（%s.%s）", server, tool, exc_info=True)


def calls_on(day: date) -> int:
    with session_scope() as session:
        return int(
            session.scalar(
                select(func.coalesce(func.sum(IfindUsage.calls), 0)).where(
                    IfindUsage.usage_date == day
                )
            )
            or 0
        )


def calls_between(start: date, end: date) -> int:
    with session_scope() as session:
        return int(
            session.scalar(
                select(func.coalesce(func.sum(IfindUsage.calls), 0)).where(
                    IfindUsage.usage_date >= start, IfindUsage.usage_date <= end
                )
            )
            or 0
        )


def breakdown_between(start: date, end: date) -> list[dict]:
    """按 (server, tool) 拆开的花费明细，花得多的在前。"""
    with session_scope() as session:
        rows = session.execute(
            select(IfindUsage.server, IfindUsage.tool, func.sum(IfindUsage.calls))
            .where(IfindUsage.usage_date >= start, IfindUsage.usage_date <= end)
            .group_by(IfindUsage.server, IfindUsage.tool)
            .order_by(func.sum(IfindUsage.calls).desc())
        ).all()
    return [{"server": s, "tool": t, "calls": int(c)} for s, t, c in rows]


def trade_days_between(start: date, end: date) -> int:
    with session_scope() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(TradeCalendar)
                .where(TradeCalendar.trade_date >= start, TradeCalendar.trade_date <= end)
            )
            or 0
        )


def first_record_day(start: date, end: date) -> date | None:
    """区间内**最早有计量记录**的那天。

    计量是后加的，装上之前的历史消耗没有记录。所以外推的样本天数不能按
    「本月已过多少交易日」算，否则会出现「14 个交易日只花了 1 次」这种
    自相矛盾的输入，再外推出一个毫无意义的预计值。
    """
    with session_scope() as session:
        return session.scalar(
            select(func.min(IfindUsage.usage_date)).where(
                IfindUsage.usage_date >= start, IfindUsage.usage_date <= end
            )
        )


def cycle_trade_days(day: date, start: date, end: date) -> tuple[int, int]:
    """本周期已过的交易日数、本周期总交易日数。

    外推按**交易日**而不是自然日：调用几乎全部发生在交易日，拿自然日外推会
    把周末算进分母，把预计用量明显压低 —— 而外推偏低的唯一后果就是
    「快撞墙了却没报警」，正是这个数要防的事。
    """
    return trade_days_between(start, day), trade_days_between(start, end)


def quota_status(day: date | None = None, settings: Settings | None = None) -> dict:
    """当前计量周期的完整视图，供「数据管理」页与配额守卫共用。"""
    settings = settings or get_settings()
    today = day or date.today()
    start = cycle_start(today, settings.ifind_cycle_start_day)
    end = cycle_end(start, settings.ifind_cycle_start_day)
    quota = settings.ifind_monthly_quota
    used = calls_between(start, today)
    passed, total = cycle_trade_days(today, start, end)

    counting_since = first_record_day(start, today)
    sampled = trade_days_between(counting_since, today) if counting_since else 0
    projected = None
    # 样本太小时外推没有意义（一天的量能放大成整周期天量），宁可不给
    if sampled >= PROJECTION_MIN_DAYS and total:
        projected = round(used / sampled * total)

    return {
        "cycle_start": start,
        "cycle_end": end,
        "monthly_quota": quota,
        "cycle_calls": used,
        "cycle_remaining": max(quota - used, 0),
        "today_calls": calls_on(today),
        "cycle_trade_days_passed": passed,
        "cycle_trade_days_total": total,
        # 计量从哪天开始。它晚于周期起点时，cycle_calls 只是部分周期的消耗
        "counting_since": counting_since,
        "sampled_trade_days": sampled,
        "projected_cycle_calls": projected,
        "usage_ratio": round(used / quota, 4) if quota else None,
        "by_tool": breakdown_between(start, today),
    }


def quota_level(day: date | None = None, settings: Settings | None = None) -> QuotaLevel:
    """按当前计量周期的已用比例判定紧张程度。

    为什么要分级让路而不是一刀切停机：配额是**全账号共享**的，
    形态选股把额度烧光之后，第二天的**基础采集（指数、情绪、板块）也会一起挂**。
    复盘是核心、形态是增强，紧张时必须让形态先停。
    """
    ratio = quota_status(day, settings)["usage_ratio"]
    if ratio is None:
        return QuotaLevel.NORMAL
    for threshold, level in LEVEL_THRESHOLDS:
        if ratio >= threshold:
            return level
    return QuotaLevel.NORMAL


def level_label(level: QuotaLevel) -> str:
    return LEVEL_LABELS.get(level, "正常")
