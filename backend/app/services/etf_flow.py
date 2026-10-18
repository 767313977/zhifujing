"""ETF 净申赎：从 `etf_share` 的逐日份额快照算净申赎序列。

**为什么不建新表**：份额本身是原始数据（已经在 `etf_share`），净申赎只是它的
「相邻快照差分 × 当日收盘价」，纯计算、随时可重算。落一份快照进库只会多出一份
可能与原始数据不一致的副本，改口径还得逐天回填 —— 与 NH-NL / OBV / ERP 同属
「数据已在库里、读取时现算」的做法（A 档）。

**三个口径坑**（每个都在对应函数里展开）：

1. **货币现金类整类剔除**（`money_fund_codes`）：货基份额的单位与覆盖都不可信，
   计进去是纯噪声。
2. **碎片日不能当基准日**（`usable_days` / `terms`）：历史上这份快照的日期来自行情源的
   「最新-交易日」，而它**不是全市场统一的一天** —— 实测（2026-10-16 线上库）：
   非交易日货基仍按自然日给净值快照（09-27/10-07/10-11 各 14 行），跨境/QDII
   则**常年比境内 ETF 晚一个交易日**（10-12 的 45 行与 10-13 的 1657 行**零交集**）。
   ⚠️ 2026-10-18 换源后，两所已经按**同一个日期**写入（见 `sources/etf_exchange`），
   理论上不再产生碎片日；但**读取端的判据保留** —— 它同时挡住历史数据与
   「两所之一没取到」的意外，删掉就等于把这道兜底一起丢了。
3. **拿不到基准就是 None，不是 0**：「今天一分钱没进来」是个结论，「没得比」只是
   缺数据，两者在图上必须能区分（与 `api/funds._etf_net_inflow` 同一口径）。

单位：对外返回值一律是**元**。
"""

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, aliased

from app.models import EtfCategory, EtfShare, TradeCalendar

# 货币现金类的分类名（词典见 services/etf_category.py 的「货币现金」条目）。
MONEY_CATEGORY = "货币现金"

# 一个交易日的快照至少要覆盖这么多只 ETF 才算「可用」。
#
# 正常日是 1650~1700 只；实测的两类碎片是 **14 只**（非交易日的货基快照，
# 09-27 / 10-07 / 10-11）与 **45 只**（跨境/QDII 的滞后快照，10-12），
# 与正常日差 35~120 倍。取 300 能把两类碎片稳稳切开，而正常日离下限还有 5 倍余量。
MIN_ROWS = 300

# 净申赎的累计口径：近 N 个**有数的交易日**之和（与分位窗口同一套约定）
SUM_WINDOWS = (5, 20)


def money_fund_codes(session: Session) -> set[str]:
    """货币现金类 ETF 的代码集合。

    **为什么申赎要整类剔除**（2026-10-11 核实，证据来自本地/线上库实测）：
    iFinD 的 fund_profile 对场内货基给的 `shares` 不可信 ——
    1. **单位不统一**：511880 银华日利 / 511990 华宝添益按「份」是对的
       （11.4 亿份 / 9.8 亿份 × ~100 元 ≈ 1151 / 976 亿元，与实际规模相符），
       但 511910 融通货币只给 96,000 份 × 100 元 = 960 万元，明显不是同一单位；
    2. **覆盖漂移**：两大货基只在部分交易日出现（09-21 有、09-22/23 缺），
       「缺席即跳过」意味着每天的申赎合计其实是一批随机子集的和；
    3. 货基申赎本身是现金管理行为，不代表方向性资金，对复盘口径只有噪声。
    剔除的只是**申赎金额**类计算；货基的 close / 涨跌幅 / 成交额没错，不受影响。
    """
    return set(
        session.scalars(
            select(EtfCategory.code).where(EtfCategory.category == MONEY_CATEGORY)
        )
    )


def row_counts(session: Session) -> dict[date, int]:
    """每个日期各有多少只 ETF 有快照行。"""
    rows = session.execute(
        select(EtfShare.trade_date, func.count()).group_by(EtfShare.trade_date)
    ).all()
    return {day: int(count) for day, count in rows}


def usable_days(session: Session) -> list[date]:
    """**覆盖达标**的日期（升序）：在交易日历里 **且** 快照不少于 `MIN_ROWS` 只。

    两个条件缺一不可：日历挡住「非交易日货基快照被标成自然日」，行数下限挡住
    「跨境/QDII 滞后一天」造成的碎片日。它们各自都能单独骗过判据 ——
    10-12 在交易日历里（是交易日），但只有 45 行；09-27 有 14 行且确实有货基
    净值，但那天休市。
    """
    counts = row_counts(session)
    calendar = set(session.scalars(select(TradeCalendar.trade_date)))
    return sorted(
        day for day, count in counts.items() if day in calendar and count >= MIN_ROWS
    )


def day_net(
    session: Session, day: date, prev: date | None, money: set[str] | None = None
) -> float | None:
    """单日净申赎（元）= Σ(份额变化 × 当日收盘价)，**不含货币现金类**。

    与 `api/funds._etf_net_inflow` 的逐行循环等价，只是把求和交给 SQL：
    一年窗口 × 1700 只 = 40 万行，全读进内存做差分会把一次请求变成几十兆的分配。
    留在 Python 侧的判据只有三条，与原来一一对应：**当日份额非空、当日收盘价非空
    且不为 0、基准日份额非空**；基准日缺席（join 不上）即跳过该只。

    没有可配对的只时返回 **None 而不是 0**。
    """
    if prev is None:
        return None
    today = aliased(EtfShare)
    before = aliased(EtfShare)
    stmt = (
        select(func.sum((today.shares - before.shares) * today.close))
        .select_from(today)
        .join(before, and_(before.code == today.code, before.trade_date == prev))
        .where(
            today.trade_date == day,
            today.shares.is_not(None),
            before.shares.is_not(None),
            today.close.is_not(None),
            # `not row.close` 的原语义：收盘价为 0 也算「算不出金额」
            today.close != 0,
        )
    )
    if money:
        stmt = stmt.where(today.code.not_in(money))
    return session.scalar(stmt)


@dataclass
class Term:
    """窗口里的一天：`(日期, 基准日, 交易日跨度, 净申赎)`。

    `prev` 是**上一个覆盖达标的交易日**，不一定是紧邻的交易日 —— 见 `build_series`。
    `span` = 从 `prev` 到 `day` 之间的交易日步数（正常相邻两天为 1）。
    """

    day: date
    prev: date | None
    span: int | None
    net: float | None


@dataclass
class FlowSeries:
    """窗口内的净申赎序列。各数组与 `dates` 严格等长。"""

    dates: list[date]
    net: list[float | None]
    span: list[int | None]
    cumulative: list[float | None]
    # 最近一个**可算**的日期（当日快照还没采到时可能是窗口内较早的一天）
    latest_date: date | None
    latest_index: int | None
    # 表里可用的交易日数（不是窗口长度）—— 说明这条曲线背后有多少历史
    coverage: int
    # 近 N 个有数的交易日之和，键是窗口长度
    sums: dict[int, float | None]


def build_series(session: Session, end: date, days: int) -> FlowSeries:
    """取截至 `end` 的最近 `days` 个交易日，算逐日净申赎与窗口内累计。

    **基准日取「上一个覆盖达标的交易日」，不是紧邻的交易日。** 因为每个交易日
    实际会落进两个日期（境内 → 当天、跨境/QDII → 前一天，见模块 docstring），
    紧邻的前一天常是 45 行的碎片日：拿它当基准，能配上的只有那 45 只，且**交集为
    零**（实测 10-12 ∩ 10-13 = 0）→ 净额会是 None，白丢一个正常日。
    改用上一个达标日后，10-13 的基准日回到 10-09（1653 行），量出来的就是
    「10-09 收盘以来全市场的净申赎」—— 跨度是 2 个交易日，由 `span` 如实标出。

    窗口按**交易日历**铺（不是按有数据的日期铺）：采集漏掉的日子在图上就是断点，
    而不是被悄悄跳过、让相邻两天看起来是连着的。累计从 0 起加，断点日留 None、
    累加器保持不动（见 `_cumulative`）。
    """
    calendar = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= end)
            .order_by(TradeCalendar.trade_date)
        )
    )
    dates = calendar[-days:] if days < len(calendar) else calendar
    if not dates:
        return FlowSeries([], [], [], [], None, None, 0, {})

    usable = usable_days(session)
    usable_set = set(usable)
    position = {day: index for index, day in enumerate(calendar)}
    money = money_fund_codes(session)

    terms: list[Term] = []
    for day in dates:
        index = bisect_left(usable, day)
        prev = usable[index - 1] if index else None
        if day in usable_set and prev is not None:
            span = position[day] - position[prev]
            terms.append(Term(day, prev, span, day_net(session, day, prev, money)))
        else:
            terms.append(Term(day, prev, None, None))

    net = [term.net for term in terms]
    latest_index = next(
        (i for i in range(len(terms) - 1, -1, -1) if net[i] is not None), None
    )
    return FlowSeries(
        dates=dates,
        net=net,
        span=[term.span for term in terms],
        cumulative=_cumulative(net),
        latest_date=dates[latest_index] if latest_index is not None else None,
        latest_index=latest_index,
        coverage=len(usable),
        sums={
            size: window_sum(net, latest_index, size) for size in SUM_WINDOWS
        },
    )


def _cumulative(net: list[float | None]) -> list[float | None]:
    """净申赎的逐日累计（**输入按交易日升序**），与输入等长。

    与 `services.nhnl.cumulative` 同一语义：None 的日子记 None（图上断开），
    累加器保持不动 —— 断口是缺数据，不是「资金归零」。
    """
    result: list[float | None] = []
    acc = 0.0
    for value in net:
        if value is None:
            result.append(None)
            continue
        acc += value
        result.append(acc)
    return result


def window_sum(net: list[float | None], end_index: int | None, size: int) -> float | None:
    """截至 `end_index` 的**最近 `size` 个有数的交易日**净申赎之和（元）。

    **跳过缺数的日子**（而不是要求连续）：口径是「近 N 个有数的交易日」，
    与分位窗口一个套路（`services/obv` 的成交额分位同样在窗口内过滤 None）。
    若写成「中间缺一天就给 None」，这张表在采集稀疏期会长期给不出数字 ——
    而计算里最要紧的部分是「有数据的那几天资金是进还是出」，不是把缺的那天
    当成 0（当成 0 才是错的：那天既不是零流入也不是零流出）。

    非交易日断口不计入（`net` 只在交易日历上铺），所以「近 5 个有数交易日」
    在正常时期就是「近 5 个交易日」。
    """
    if end_index is None:
        return None
    values = [value for value in net[: end_index + 1] if value is not None]
    if len(values) < size:
        return None
    return sum(values[-size:])
