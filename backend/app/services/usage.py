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

# ---- 一次完整采集的**日耗梯度**（让路按这个顺序来，见 `LEVEL_BUDGETS`）----
#
# 实测 10-08 ~ 10-12 的整天消耗：181 / 175 / 154。拆开看，最贵的不是「地基」而是几项
# **纯增强** —— 所以让路必须「先停增强、最后才动地基」：
#
#   命中前 50 只的 DDE 补齐   70 次/天  ← 最贵，少了它只是个股页那一栏短一点
#   ETF（份额 / 行情）        59 次/天  ← 纯增强，没有下游
#   全市场日线                14 次/天  ← **地基**：形态选股、板块资金流都吃它
#   DDE 全市场扫描            14 次/天  ← **地基**：板块资金流的逐股净流入来自它
#   指数 / 涨停三池 / 情绪 / 板块 / 自选  约 43 次/天  ← 复盘主线，任何时候都保
#
# ⚠️ 2026-10-13 之前是**反的**：一到紧张档第一个停的就是「全市场日线」，于是当天形态
# 扫描失败（`_require_bars` 报「日线还没采到」）、板块资金流跟着跳过 —— 10-12 那天
# 净流入整列缺就是这么来的。
#
# ⚠️ **2026-10-14 把 ETF 那一档从 20 改成 59。** 原值写于 2026-09-22，按当时
# 「80 只一批、约 20 次」估；而 iFinD 的单批上限后来**收到了 30 只**，那个 80 只的
# 批量一直超着上限、靠拆半兜了一天 154 次（原委见 `collect_funds.ETF_BATCH`）。
# 修好批量之后 ETF 的真实日耗 = 1745 只 ÷ 30 ≈ **59 次**。
#
# 这个数不是摆设：**`ENHANCE_DAILY` 一动，`CORE_DAILY` 必须跟着动**，否则
# `LEVEL_BUDGETS` 第 3、4 档就不再单调 —— `FULL − HIT − ENHANCE − BASE` 才是
# 「连地基都停了」那一档该在的位置。这里保持
# `CORE_DAILY = 200 − 70 − 59 − 28 = 43`。
FULL_DAY_CALLS = 200  # 满负荷
HIT_DDE_DAILY = 70  # 「命中前 50 只的 DDE 补齐」
ENHANCE_DAILY = 59  # ETF（1745 只 ÷ 单批上限 30，2026-10-14 实测重算）
BASE_DAILY = 28  # 全市场日线 + DDE 全市场扫描（两块地基）
CORE_DAILY = 43  # 指数 / 涨停三池 / 情绪 / 板块 / 自选


class QuotaLevel(IntEnum):
    """配额紧张程度，数值越大越紧张，判定一律用 `>=`。

    判据是「**剩下的额度够不够按某一档的日耗撑完本周期余下的交易日**」（见
    `LEVEL_BUDGETS`），不是「整周期已经用掉多少」 —— 后者既不看你还剩多少、也不看
    还剩几天：实测 10-13 整周期已用 80.8%，可当时剩 1725 次、只剩 3 个交易日，明明
    够用却把全市场日线停了。

    ⚠️ 顺序是**先停增强、最后才停地基**（2026-10-13 改，理由见上面那张日耗表）。
    """

    NORMAL = 0
    # 停「命中前 50 只的 DDE 补齐」（70 次/天）
    PAUSE_HIT_DDE = 1
    # 再停 ETF 与概念兜底（约 59 次/天）
    PAUSE_ENHANCE = 2
    # 再停全市场日线 + DDE 全市场扫描（28 次/天）—— 地基，停了形态/板块资金流一起缺
    PAUSE_KLINE = 3
    # 只保留指数 + 涨停三池 + 情绪这条主线
    CORE_ONLY = 4


#: 让路阶梯：`(这一档还能撑住的日耗, 档位)`，**从宽到窄**，第一个
#: 「剩余额度 ≥ 日耗 × 还要采的交易日数」成立的就是当前档。
#:
#: ⚠️ 「还要采的天数」= 余下交易日（含今天）**− 今天**（见 `quota_status`）：
#: 判档是在采集途中求值的，今天那一轮要么已经跑完、要么正在跑，再算一天等于白留一天
#: 预算（实测 10-13：多算这一天会让纯增强的 ETF 白停）。周期最后一天则退化成 0 →
#: 恒为正常档，把余额花光（额度不累积，留着没用）。
#:
#: 写成表而不是一串 if，是为了让「先停谁、后停谁」一眼看完 —— 调整顺序只动这里一处。
#: 最后一档的日耗 = `FULL_DAY_CALLS − HIT_DDE_DAILY − ENHANCE_DAILY − BASE_DAILY`
#: （= `CORE_DAILY`，也就是「连地基都停了、只剩主线」）。
LEVEL_BUDGETS: tuple[tuple[int, QuotaLevel], ...] = (
    (FULL_DAY_CALLS, QuotaLevel.NORMAL),
    (FULL_DAY_CALLS - HIT_DDE_DAILY, QuotaLevel.PAUSE_HIT_DDE),
    (FULL_DAY_CALLS - HIT_DDE_DAILY - ENHANCE_DAILY, QuotaLevel.PAUSE_ENHANCE),
    (CORE_DAILY, QuotaLevel.PAUSE_KLINE),
    (0, QuotaLevel.CORE_ONLY),
)

LEVEL_LABELS = {
    QuotaLevel.NORMAL: "正常",
    QuotaLevel.PAUSE_HIT_DDE: "余下额度偏紧：已停命中前 50 只的 DDE 补齐",
    QuotaLevel.PAUSE_ENHANCE: "余下额度不足：已停 ETF 与概念兜底",
    QuotaLevel.PAUSE_KLINE: "余下额度告急：已停全市场日线与 DDE 扫描（形态 / 板块资金流当天会缺）",
    QuotaLevel.CORE_ONLY: "余下额度只够主线：只保留指数 / 涨停三池 / 情绪",
}

# ---- 手工压档（应急开关，2026-10-14 加）----
#
# 为什么要有：阶梯的判据是「剩下的额度按**日耗模型**够不够撑完余下交易日」，而它对
# **一次性开销**完全无感，于是会出现「今天判成正常、明天就没额度了」这种跨天分配失灵。
#
# 2026-10-15 就是这一例：那天 `余下 200 ≥ 200 × 1` → 判**正常档**，会放全速跑
# （实测约 170 次/天），跑完只剩零头给 10-16；而 10-16 是周期末、还要跑一整条主线。
# 用户要求手动把它压到 ① 档（停命中前 50 只的 DDE 补齐，省约 70 次）。
#
# 用法：`{日期: 档位}`，**只覆盖列出的那一天**，其余日期一律照常按阶梯自动判。
#
# ⚠️ 这是应急开关，**用完记得把条目删掉**：留着会让那天永远不走自动判档，
# 而阶梯的自动判据本身是对的（见 `LEVEL_BUDGETS`）—— 别让它变成第二套隐藏规则。
FORCED_LEVELS: dict[date, QuotaLevel] = {
    date(2026, 10, 15): QuotaLevel.PAUSE_HIT_DDE,
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

    # 让路判据（2026-10-13 改）：**剩下的额度够不够按某一档的日耗撑完还要采的交易日**
    days_left = trade_days_between(today, end)  # 含今天，只用于展示
    remaining = max(quota - used, 0)
    # 判档按「还要采几天」= 上面 − 今天：判档发生在采集途中，今天那轮已经在跑/跑完，
    # 再算一天等于白留一天预算（2026-10-13 用户实测后要求放宽这一天）
    level = _level_for(remaining, max(days_left - 1, 0))
    # 手工压档（见 `FORCED_LEVELS`）：只覆盖列出的那一天，其余一律照常。
    # 压档只改「判成哪一档」，不改用量统计 —— 所以页面上的已用/剩余仍是真实值。
    forced = FORCED_LEVELS.get(today)
    if forced is not None:
        level = forced
    # 满负荷的余下需求 ÷ 剩余额度：只给页面看（>1 = 满负荷撑不完，该让路了），
    # 判档本身不用它
    need_calls = FULL_DAY_CALLS * days_left
    need_ratio = round(need_calls / remaining, 4) if remaining > 0 else None

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
        # 让路判据的输入与结果：余下交易日（含今天）、满负荷需求、需求 ÷ 剩余，
        # 以及**当前档位**（页面直接显示，省得前端再拼一遍判据）
        "days_left": days_left,
        "need_calls": need_calls,
        "need_ratio": need_ratio,
        "level": int(level),
        "level_label": level_label(level),
        "by_tool": breakdown_between(start, today),
    }


def _level_for(remaining: int, collect_days: int) -> QuotaLevel:
    """`LEVEL_BUDGETS` 里第一个「剩余额度 ≥ 日耗 × 还要采的交易日数」的档位。

    `collect_days` **不含今天**（调用方传 `余下交易日 − 1`，理由见 `quota_status`）。
    `collect_days = 0`（今天就是周期最后一天，或本周期已经过完）时每档都成立 →
    恒为 NORMAL ✓ —— 额度不累积，最后一天该花就花。
    """
    for daily, level in LEVEL_BUDGETS:
        if remaining >= daily * collect_days:
            return level
    return QuotaLevel.CORE_ONLY


def quota_level(day: date | None = None, settings: Settings | None = None) -> QuotaLevel:
    """当前档位（判据与顺序见 `QuotaLevel` / `LEVEL_BUDGETS`）。

    ⚠️ 看的是「**剩下的额度够不够撑完本周期余下的交易日**」，不是「整周期已经用掉
    多少」—— 后者既不看还剩多少、也不看还剩几天，会在明明够用的时候把日线停掉
    （实测 10-13）。让路的顺序也是**先停增强、最后才停地基**。
    """
    return QuotaLevel(quota_status(day, settings)["level"])


def level_label(level: QuotaLevel) -> str:
    return LEVEL_LABELS.get(level, "正常")
