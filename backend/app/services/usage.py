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

# 一次**完整**采集的日耗上界：指数 / 涨停三池 / 情绪 + 全市场日线 + DDE 扫描 +
# 命中前 50 只补齐 + ETF + 板块。实测 10-08 ~ 10-12 是 181 / 175 / 154 次，取 200 留余量。
#
# ⚠️ 为什么用**常数**而不是「近期实测日均」：让路一旦生效，当天用量立刻掉下来
# （10-13 全被挡，只花了 20 次）—— 拿近期实测当需求，等于让守卫把自己造成的低谷
# 当成新常态：第二天又判「够用」→ 放开 → 再挡回，来回抖。常数不会被子系统自己的
# 行为带偏（代价是采集规模大变时要手动调一次）。
FULL_DAY_CALLS = 200


class QuotaLevel(IntEnum):
    """配额紧张程度，数值越大越紧张，判定一律用 `>=`。

    ⚠️ **判据是「余下几个交易日够不够花」，不是「整周期已用多少」**（2026-10-13 改）。
    按「已用比例」让路有两个盲点：不看**还剩多少额度**、也不看**还剩几个交易日**。
    实测 10-13：整周期已用 80.8% → 判定让路，可当时剩 1725 次、本周期只剩 3 个交易日
    （满打满算约 500 次）—— 明明够用，却把全市场日线全挡了，于是当天没有日线、
    形态扫描失败、整站数据停在前一天。现在按 `need_ratio`（余下需求 ÷ 剩余额度）判。
    """

    NORMAL = 0
    # 余下额度只够预估需求的 80%：停形态选股的全市场日线更新（形态结果不再刷新）
    PAUSE_KLINE = 1
    # 90%：停概念板块的 iFinD 兜底（记空值，次日由同花顺订正）
    PAUSE_CONCEPT = 2
    # 95%：只保留指数 + 涨停三池 + 情绪这条主线
    CORE_ONLY = 3


# 阈值与档位。顺序必须**从高到低**，第一个命中的就是当前档。
#
# 判据是 `need_ratio` = 余下交易日的预估需求 ÷ 剩余额度。语义与旧的「已用 80% 就
# 留 20% 缓冲」是同一个尺度，只是从「看过去」改成「看余下」：0.8 表示剩余额度只
# 覆盖得住预估需求的 80%（缺口 20%）。
LEVEL_THRESHOLDS = (
    (0.95, QuotaLevel.CORE_ONLY),
    (0.90, QuotaLevel.PAUSE_CONCEPT),
    (0.80, QuotaLevel.PAUSE_KLINE),
)

LEVEL_LABELS = {
    QuotaLevel.NORMAL: "正常",
    QuotaLevel.PAUSE_KLINE: "剩余额度不足以覆盖余下交易日：已停全市场日线更新",
    QuotaLevel.PAUSE_CONCEPT: "剩余额度不足：已停概念板块的 iFinD 兜底",
    QuotaLevel.CORE_ONLY: "剩余额度告急：只保留指数 / 涨停三池 / 情绪",
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
    today_calls = calls_on(today)

    counting_since = first_record_day(start, today)
    sampled = trade_days_between(counting_since, today) if counting_since else 0
    # 「今天」只有已经花过调用时才算一天样本：周期末的清晨 / 非交易日查询时当天
    # 采集还没跑，used 里今天贡献为 0，把 0 计进分母会稀释日均 → projected 偏低 →
    # 80/90% 让路比应有的更晚 —— 外推低估正是本模块要防的方向，宁保守。
    # 注意：非交易日的今天本来就不在样本里（日历表没有这行），不能再减一天；
    # 只有「今天是交易日 且 今天零调用」才把样本窗缩到上一交易日。counting_since
    # 那天计入的既有语义不变 —— 它是最早有记录的日子、必有调用，不会被减到。
    # sampled 减到 0 时下方 `>= PROJECTION_MIN_DAYS` 不成立，projected 留 None，
    # 与现状 counting_since 为 None 时的行为一致，不会除零。
    if counting_since and today_calls == 0 and trade_days_between(today, today):
        sampled -= 1
    projected = None
    # 样本太小时外推没有意义（一天的量能放大成整周期天量），宁可不给
    if sampled >= PROJECTION_MIN_DAYS and total:
        projected = round(used / sampled * total)

    # 让路判据（2026-10-13 改）：**余下交易日的预估需求 ÷ 剩余额度**
    days_left = trade_days_between(today, end)  # 含今天
    remaining = max(quota - used, 0)
    need_calls = FULL_DAY_CALLS * days_left
    if remaining > 0:
        need_ratio: float | None = round(need_calls / remaining, 4)
    else:
        # 剩余为 0：还有交易日要采就是最紧张（None 交给 `quota_level` 处理），
        # 周期已经过完（没有余下交易日）则无所谓
        need_ratio = None if need_calls else 0.0

    return {
        "cycle_start": start,
        "cycle_end": end,
        "monthly_quota": quota,
        "cycle_calls": used,
        "cycle_remaining": max(quota - used, 0),
        "today_calls": today_calls,
        "cycle_trade_days_passed": passed,
        "cycle_trade_days_total": total,
        # 计量从哪天开始。它晚于周期起点时，cycle_calls 只是部分周期的消耗
        "counting_since": counting_since,
        "sampled_trade_days": sampled,
        "projected_cycle_calls": projected,
        "usage_ratio": round(used / quota, 4) if quota else None,
        # 让路判据的三个输入：余下交易日（含今天）、按满负荷估的需求、需求 ÷ 剩余
        "days_left": days_left,
        "need_calls": need_calls,
        "need_ratio": need_ratio,
        "by_tool": breakdown_between(start, today),
    }


def quota_level(day: date | None = None, settings: Settings | None = None) -> QuotaLevel:
    """按**余下交易日够不够花**判定紧张程度（见 `QuotaLevel` 的说明）。

    为什么分级让路而不是一刀切停机：配额是**全账号共享**的，形态选股把额度烧光之后，
    第二天的**基础采集（指数、情绪、板块）也会一起挂**。复盘是核心、形态是增强，
    紧张时必须让形态先停。

    ⚠️ 判据是 `need_ratio`（余下需求 ÷ 剩余额度），不是整周期已用比例 —— 后者不看
    还剩多少、也不看还剩几天，会在「明明够用」的时候把日线停掉（实测 10-13）。
    """
    ratio = quota_status(day, settings)["need_ratio"]
    if ratio is None:
        # 剩余额度为 0、而本周期还有交易日要采：最紧张
        return QuotaLevel.CORE_ONLY
    for threshold, level in LEVEL_THRESHOLDS:
        if ratio >= threshold:
            return level
    return QuotaLevel.NORMAL


def level_label(level: QuotaLevel) -> str:
    return LEVEL_LABELS.get(level, "正常")
