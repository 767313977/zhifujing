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
from datetime import date, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import EtfCategory, EtfShare, TradeCalendar

# 货币现金类的分类名（词典见 services/etf_category.py 的「货币现金」条目）。
MONEY_CATEGORY = "货币现金"

# 一个交易日的快照至少要覆盖这么多只 ETF 才算「可用」。
#
# 正常日是 1650~1700 只；实测的两类碎片是 **14 只**（非交易日的货基快照，
# 09-27 / 10-07 / 10-11）与 **45 只**（跨境/QDII 的滞后快照，10-12），
# 与正常日差 35~120 倍。取 300 能把两类碎片稳稳切开，而正常日离下限还有 5 倍余量。
MIN_ROWS = 300

# 净申赎的累计口径：近 N 个**有数的交易日之和**（与分位窗口同一套约定）
SUM_WINDOWS = (5, 20)

#: 份额折算（拆分 / 合并）的判定阈值：`|前一日 close ÷ 当日 prev_close − 1|` 超过它，
#: 就认为那天是折算日，不是真实涨跌。除息一般在 5% 以内，不会误判（2026-10-31 加）。
SPLIT_RATIO = 0.20


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


#: `usable_days` 的进程内缓存：`(引擎, 最新快照日, 计算时间, 结果)`。
#: 它要对整张 `etf_share`（约 40 万行）做 group by，单次要几十毫秒到上百毫秒，
#: 而「哪几天覆盖达标」在一天里基本不变（新的一天落库才会变）。10 分钟 TTL 兜住
#: 「同一天补数据」这种改动（2026-10-22 体检）。
_USABLE_CACHE: tuple[object, date | None, datetime, list[date]] | None = None
_USABLE_TTL = timedelta(minutes=10)

#: 折算日缓存（结构：引擎, 窗口起, 窗口止, 计算时间, 结果）。理由同上：它要扫全表。
_SPLIT_CACHE: tuple[object, date, date, datetime, dict[date, set[str]]] | None = None


def _usable_days_cached(session: Session) -> list[date]:
    global _USABLE_CACHE
    latest = session.scalar(select(func.max(EtfShare.trade_date)))
    bind = session.get_bind()
    now = datetime.now()
    cached = _USABLE_CACHE
    if (
        cached is not None
        and cached[0] is bind
        and cached[1] == latest
        and now - cached[2] < _USABLE_TTL
    ):
        return cached[3]
    days = usable_days(session)
    _USABLE_CACHE = (bind, latest, now, days)
    return days


#: 「找最近覆盖达标日」时最多往前翻几个快照日。碎片日与正常日相邻（见 `MIN_ROWS`
#: 的实测记录），翻 5 天足够。
_RECENT_DAY_LOOKBACK = 5


def split_codes(session: Session, days: list[date]) -> dict[date, set[str]]:
    """这些交易日里发生**份额折算**的 ETF 代码（`{day: {code}}`）。

    **为什么必须剔**：折算日份额翻倍、价格减半，`(今日份额 − 基准份额) × 今日收盘`
    会被算成「净申购 = 原规模」。2026-10-31 实测 159583（通信设备ETF）1 拆 2 就是
    这种情形 —— 资金面面板当天会凭空多出一笔与原规模等额的假流入。

    判据用 `etf_bar` 的**交易所前收**：折算日的 `prev_close` 已按比例调整，与前一日
    `close` 差得很远（2.118 → 1.059）。除息一般 < 5%，用 20% 阈值不会误判。
    `etf_bar` 还没回补（空表）时返回空 dict，行为与改造前一致。
    """
    wanted = set(days)
    if not wanted:
        return {}
    start, end = min(days), max(days)
    bind = session.get_bind()
    global _SPLIT_CACHE
    now = datetime.now()
    cached = _SPLIT_CACHE
    if (
        cached is not None
        and cached[0] is bind
        and cached[1] == start
        and cached[2] == end
        and now - cached[3] < _USABLE_TTL
    ):
        return cached[4]

    # LAG 要拿到窗口前一根，所以往前多取一段（自然日 40 天足够覆盖假期）
    floor = start - timedelta(days=40)
    rows = session.execute(
        text(
            """
            WITH bars AS (
                SELECT trade_date, code, prev_close,
                       LAG(close) OVER (PARTITION BY code ORDER BY trade_date) AS prior_close
                FROM etf_bar
                WHERE trade_date >= :floor AND trade_date <= :end
            )
            SELECT trade_date, code
            FROM bars
            WHERE trade_date >= :start AND trade_date <= :end
              AND prior_close IS NOT NULL
              AND prev_close IS NOT NULL
              AND prev_close > 0
              AND ABS(prior_close / prev_close - 1) > :ratio
            """
        ),
        {
            "floor": floor.isoformat(),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "ratio": SPLIT_RATIO,
        },
    ).all()
    result: dict[date, set[str]] = {}
    for day, code in rows:
        day = date.fromisoformat(str(day))
        if day in wanted:
            result.setdefault(day, set()).add(str(code))
    _SPLIT_CACHE = (bind, start, end, now, result)
    return result


def snapshot_rows(session: Session, day: date) -> int:
    """某天有多少只 ETF 有份额快照行（`MIN_ROWS` 判据的输入）。"""
    return int(
        session.scalar(
            select(func.count()).select_from(EtfShare).where(EtfShare.trade_date == day)
        )
        or 0
    )


def _is_covered_day(session: Session, day: date) -> bool:
    """这一天是否**覆盖达标**（判据与 `usable_days` 完全一致：日历内 + 行数 ≥ MIN_ROWS）。"""
    if snapshot_rows(session, day) < MIN_ROWS:
        return False
    return (
        session.scalar(
            select(TradeCalendar.trade_date).where(TradeCalendar.trade_date == day)
        )
        is not None
    )


def _recent_snapshot_days(
    session: Session, upper: date, *, inclusive: bool
) -> list[date]:
    """`upper` 之前（或含它）最近的几个**有快照的日期**，降序。"""
    condition = (
        EtfShare.trade_date <= upper if inclusive else EtfShare.trade_date < upper
    )
    return list(
        session.scalars(
            select(EtfShare.trade_date)
            .distinct()
            .where(condition)
            .order_by(EtfShare.trade_date.desc())
            .limit(_RECENT_DAY_LOOKBACK)
        )
    )


def _first_covered(session: Session, days: list[date]) -> date | None:
    for day in days:
        if _is_covered_day(session, day):
            return day
    return None


def latest_covered_day(session: Session, on_or_before: date) -> date | None:
    """`on_or_before`（含）之前最近一个**覆盖达标**的快照日；没有则 None。

    为什么不直接调用 `usable_days()`：它要对整张 `etf_share` 做 group by（生产约
    40 万行，每次请求几百毫秒），而这里只需要「最近一个可用日」。只看最近
    `_RECENT_DAY_LOOKBACK` 天，判据仍是同一份（日历内 + 行数 ≥ `MIN_ROWS`）。
    """
    return _first_covered(
        session, _recent_snapshot_days(session, on_or_before, inclusive=True)
    )


def prev_covered_day(session: Session, day: date) -> date | None:
    """`day` **之前**最近一个覆盖达标的快照日（「相邻两天净申赎」的基准日）。

    与 `build_series` 里基准日的取法一致（上一个覆盖达标日，不是紧邻交易日）：
    紧邻交易日可能是只有几十行的碎片快照，拿它当基准会算出一个只覆盖几十只、
    甚至交集为 0 的「净申赎」（见 `MIN_ROWS` / `build_series` 的说明）。
    """
    return _first_covered(
        session, _recent_snapshot_days(session, day, inclusive=False)
    )


def day_nets(
    session: Session, pairs: list[tuple[date, date]], money: set[str] | None = None
) -> dict[date, float | None]:
    """批量算多天的净申赎：`{day: 净额}`，**一条 SQL**（2026-10-22 体检）。

    净申赎 = Σ(当日份额 − 基准日份额) × 当日收盘价，**不含货币现金类**。

    为什么要有它：`build_series` 原来对窗口里每一天各发一条单日查询（250 天 =
    250 条往返），一次 `/api/etf-net-subscription` 要 300~500ms。这里把
    `(day, prev)` 对拼成一张 VALUES 临时表、与 `etf_share` 关联两次后按 day 分组，
    把 250 次往返压成 1 次。判据：**当日份额 / 收盘价非空、收盘价不为 0、
    基准日份额非空、剔除货基**；缺配对的日期不会出现在结果里（调用方按 None 处理）。

    没有可配对的只时该日不进结果 —— 「今天一分钱没进来」与「没得比」必须能区分。

    **份额折算日整只跳过**（2026-10-31 加，见 `split_codes`）：折算当天份额翻倍、
    价格减半，差分出来是「净申购 = 原规模」的假流入。跳过它 = 这一只当天不参与
    合计（不是记 0）；跨折算日的 5 / 20 日累计自然也不会把它算进去。
    """
    if not pairs:
        return {}
    splits = split_codes(session, [day for day, _ in pairs])
    values = ", ".join(
        f"(CAST(:d{index} AS TEXT), CAST(:p{index} AS TEXT))"
        for index in range(len(pairs))
    )
    params: dict[str, str] = {}
    for index, (day, prev) in enumerate(pairs):
        params[f"d{index}"] = day.isoformat()
        params[f"p{index}"] = prev.isoformat()

    split_items = [
        (day, code) for day, codes in sorted(splits.items()) for code in sorted(codes)
    ]
    split_cte = ""
    split_join = ""
    split_filter = ""
    if split_items:
        split_values = ", ".join(
            f"(CAST(:sd{index} AS TEXT), CAST(:sc{index} AS TEXT))"
            for index in range(len(split_items))
        )
        split_cte = f",\n            splits(day, code) AS (VALUES {split_values})"
        split_join = "LEFT JOIN splits sp ON sp.day = p.day AND sp.code = t.code"
        split_filter = "AND sp.code IS NULL"
        for index, (day, code) in enumerate(split_items):
            params[f"sd{index}"] = day.isoformat()
            params[f"sc{index}"] = code

    money_clause = ""
    if money:
        names = []
        for index, code in enumerate(sorted(money)):
            params[f"m{index}"] = code
            names.append(f":m{index}")
        money_clause = f"AND t.code NOT IN ({', '.join(names)})"
    rows = session.execute(
        text(
            f"""
            WITH pairs(day, prev) AS (VALUES {values}){split_cte}
            SELECT p.day AS day, SUM((t.shares - b.shares) * t.close) AS net
            FROM pairs p
            JOIN etf_share t ON t.trade_date = p.day
            JOIN etf_share b ON b.trade_date = p.prev AND b.code = t.code
            {split_join}
            WHERE t.shares IS NOT NULL
              AND b.shares IS NOT NULL
              AND t.close IS NOT NULL
              AND t.close != 0
              {money_clause}
              {split_filter}
            GROUP BY p.day
            """
        ),
        params,
    ).all()
    return {
        date.fromisoformat(str(day)): float(net) for day, net in rows if net is not None
    }


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

    usable = _usable_days_cached(session)
    usable_set = set(usable)
    position = {day: index for index, day in enumerate(calendar)}
    money = money_fund_codes(session)

    terms: list[Term] = []
    planned: list[tuple[date, date]] = []
    for day in dates:
        index = bisect_left(usable, day)
        prev = usable[index - 1] if index else None
        if day in usable_set and prev is not None:
            span = position[day] - position[prev]
            terms.append(Term(day, prev, span, None))
            planned.append((day, prev))
        else:
            terms.append(Term(day, prev, None, None))
    # 250 天一次算完（原来是逐日一条 SQL，见 `day_nets`）
    nets = day_nets(session, planned, money)
    for term in terms:
        if term.span is not None:
            term.net = nets.get(term.day)

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
