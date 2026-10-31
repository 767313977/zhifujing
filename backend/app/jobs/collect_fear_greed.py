"""恐惧贪婪指数的六项原始指标采集（**零 iFinD 配额**）。

全部从站内已有的表算出来，不新增任何外部请求：
- 波动率 / RSI  ← `index_daily`（上证指数收盘）
- 相对换手率    ← `market_sentiment.total_amount`
- 融资融券      ← `margin_daily`（沪深两市合计）
- 市场宽度      ← `stock_daily.pct_chg` 按日聚合
- 涨跌停强度    ← `market_sentiment.limit_up_count / limit_down_count`

⚠️ 宽度**不能**取 `market_sentiment.up_count`：那个字段只有当日值、历史回补时留空
（见 `models.MarketSentiment` 的注释），本地实测非空只有 6 天，做不了「一年分位」。
所以这里改从个股日线反算 —— `stock_daily` 有 250 个交易日的完整历史。

日历骨架取 `market_sentiment.trade_date`（与站点其它地方「哪天算交易日」的口径一致）。
"""

import logging
import math
from datetime import date, timedelta

from sqlalchemy import case, func, or_, select

from app.db import session_scope, upsert_many
from app.models import (
    FearGreedDaily,
    IndexDaily,
    MarginDaily,
    MarketSentiment,
    StockDaily,
    StockUniverse,
)
from app.services.coverage import coverage_ok

logger = logging.getLogger(__name__)

# 回补默认的回看自然日数（≈250 个交易日），与 `stock_daily` 的历史深度对齐
DEFAULT_BACKFILL_DAYS = 380

# 每次采集重算最近这么多个交易日 —— 与其它采集器「漏一天下次自动补」同策略。
# 重算而不是只算当天：并入历史回补的日线、订正过的成交额都会顺着刷新
REFRESH_DAYS = 30

# 算窗口类指标时要往前多取的自然日数（20 日窗口 + RSI 的平滑预热，40 个交易日足够）。
# 这段范围算出来只为喂窗口，**不写库**（见 `compute_range`）
WARMUP_DAYS = 70

# 上证指数：波动率、RSI 都以它为标的（站点的基准指数，见 `api/market.INDEX_ORDER`）
BENCHMARK_CODE = "000001.SH"

# 波动率窗口：近 20 个交易日的日对数收益率
VOL_DAYS = 20
# RSI 周期
RSI_PERIOD = 14
# 「相对」类指标的基准回看长度（相对换手率、两融变化率）
BASE_DAYS = 20

#: 两融缺值时最多沿用多少个交易日的旧值（T+1 披露，正常只差一天）。
#: 超过这个长度就记 None —— 数据源长期没更新时不能拿几周前的数冒充今天。
MARGIN_CARRY_DAYS = 3

#: 算「全市场宽度」时只认**股票**代码段：沪 6xx（含 688）、深 0xx/3xx、
#: 北交所 4xx/8xx/92x。`stock_daily` 里还会有自选同步写进来的 ETF（5xx/1xx）
#: 与可转债（11x/12x）—— 它们不是「家数」，计进去会同时污染上涨占比和覆盖率
#: （2026-10-31 复查：每天 5100~5234 行，比 5098 只的股票池还多）。
STOCK_CODE_PREFIXES = ("6", "0", "3", "4", "8", "92")


def _stock_code_filter():
    """`stock_daily.code` 属于股票代码段的条件（SQL 侧过滤，见上面常量）。"""
    return or_(*[StockDaily.code.like(f"{prefix}%") for prefix in STOCK_CODE_PREFIXES])


def _trade_calendar(session, start: date, end: date) -> list[date]:
    """区间内库里有情绪数据的交易日（升序）。"""
    return list(
        session.scalars(
            select(MarketSentiment.trade_date)
            .where(MarketSentiment.trade_date >= start, MarketSentiment.trade_date <= end)
            .order_by(MarketSentiment.trade_date)
        )
    )


def _align(calendar: list[date], by_date: dict) -> tuple[list, list, list]:
    """把按日期的取值摊到日历位置上。

    返回 `(values, ranks, compact)`：
    - `values[i]`：第 i 个交易日的值，缺了是 None
    - `ranks[i]`：该位置在 `compact` 里的下标（缺了是 None）—— 窗口类指标要用它
      定位「前 20 个**有值**的交易日」，否则中间断一天就会把窗口错开一位
    - `compact`：只含非空值、按日期升序
    """
    values = [by_date.get(day) for day in calendar]
    ranks: list[int | None] = []
    compact: list = []
    for value in values:
        if value is None:
            ranks.append(None)
        else:
            ranks.append(len(compact))
            compact.append(value)
    return values, ranks, compact


def _rsi_series(closes: list[float], period: int = RSI_PERIOD) -> list[float | None]:
    """Wilder 平滑的 RSI 序列，与 `closes` 等长；前 `period` 个位置没有值。

    先取前 `period` 个涨跌幅的简单平均做种子，之后按 `(prev*(period-1)+today)/period`
    递推 —— 这是 RSI 的标准定义（同花顺 / 通达信的默认口径）。
    """
    result: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return result
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result[i + 1] = _rsi_value(avg_gain, avg_loss)
    return result


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 2)


def _volatility(closes: list[float], end: int) -> float | None:
    """`closes[end-20..end]` 这 21 个收盘价的日对数收益率标准差，年化后转百分数。"""
    if end < VOL_DAYS:
        return None
    window = closes[end - VOL_DAYS : end + 1]
    returns = [
        math.log(window[i] / window[i - 1])
        for i in range(1, len(window))
        if window[i] > 0 and window[i - 1] > 0
    ]
    if len(returns) < VOL_DAYS:
        return None
    mean = sum(returns) / len(returns)
    # 样本标准差（ddof=1）：20 个样本用总体标准差会系统性偏低
    variance = sum((item - mean) ** 2 for item in returns) / (len(returns) - 1)
    return round(math.sqrt(variance) * math.sqrt(252) * 100, 2)


def _breadth_by_date(session, start: date, end: date) -> dict[date, float]:
    """全市场「上涨家数 ÷（上涨 + 下跌）」× 100，按日。

    一条 GROUP BY 扫过去：区间内每个交易日一行。涨跌幅恰好为 0 的票
    （停牌复牌、一字板平开）两边都不算 —— 与 `market_sentiment.up_count`
    的分母口径一致。

    ⚠️ **只统计股票代码段**（`STOCK_CODE_PREFIXES`）：自选同步会把 ETF / 可转债
    也写进 `stock_daily`，它们既不是「家数」，又会把覆盖率垫高到 100% 以上 ——
    2026-10-31 复查时每天比 5098 只的股票池多出 30~130 行，就是这些东西。

    ⚠️ **覆盖率不到 90% 的交易日直接不给值**（2026-10-31 复查，遗留项）：
    只采到几十只票时照样能算出一个占比，但那是「采样」不是「全市场」——
    与 `scan_patterns` 的覆盖率闸同一条判据（`services.coverage.MIN_DAY_COVERAGE`），
    免得采集没采全的那天静默喂给分位一个假值。股票池还没建（`pool=0`）时不判，
    保持原来的行为。
    """
    rows = session.execute(
        select(
            StockDaily.trade_date,
            func.sum(case((StockDaily.pct_chg > 0, 1), else_=0)),
            func.sum(case((StockDaily.pct_chg < 0, 1), else_=0)),
            func.count(),
        )
        .where(
            StockDaily.trade_date >= start,
            StockDaily.trade_date <= end,
            StockDaily.pct_chg.is_not(None),
            _stock_code_filter(),
        )
        .group_by(StockDaily.trade_date)
    ).all()
    pool = session.scalar(select(func.count()).select_from(StockUniverse)) or 0
    result: dict[date, float] = {}
    thin: list[date] = []
    for day, up, down, count in rows:
        up = int(up or 0)
        down = int(down or 0)
        if up + down <= 0:
            continue
        if pool and not coverage_ok(int(count or 0), pool):
            thin.append(day)
            continue
        result[day] = round(up / (up + down) * 100, 4)
    if thin:
        logger.warning(
            "市场宽度跳过 %d 个交易日：当天日线覆盖不到股票池的 90%%（%d 只）",
            len(thin),
            pool,
        )
    return result


def _margin_by_date(session, start: date, end: date) -> dict[date, float]:
    """沪深两市「融资余额 + 融券余额」合计（亿元），按日。

    ⚠️ **只认沪、深两所都有数的交易日**（2026-10-31 复查）。模型注释里写着深交所
    常比上交所晚一天：某天只到一所时，合计只有正常的一半，20 日变化率会变成
    −40%~−50%，这一项必然被打到「极度恐慌」，整条指数跟着偏。缺一所就**整天不给值**
    （由 `_carry_forward` 沿用上一个齐全日），绝不求和。
    """
    rows = session.execute(
        select(
            MarginDaily.trade_date,
            MarginDaily.market,
            func.coalesce(MarginDaily.financing_balance, 0.0)
            + func.coalesce(MarginDaily.securities_balance, 0.0),
        ).where(MarginDaily.trade_date >= start, MarginDaily.trade_date <= end)
    ).all()
    per_day: dict[date, dict[str, float]] = {}
    for day, market, total in rows:
        if total:
            per_day.setdefault(day, {})[market] = float(total)
    return {
        day: sum(by_market.values())
        for day, by_market in per_day.items()
        if {"sh", "sz"} <= set(by_market)
    }


def _carry_forward(
    values: list[float | None], limit: int = MARGIN_CARRY_DAYS
) -> list[float | None]:
    """缺值的交易日沿用最近一个有值的（最多连续 `limit` 个交易日）。

    为什么需要：两融是**按交易所分别披露、T+1 才齐**的（深交所更晚），所以最新一天
    经常还没有数。不沿用的话当天综合分只剩 5 项（2026-10-30 实测少 4 分，读者看到的
    是「恐慌」而不是「极度恐慌」）；沿用之后每天稳定 6 项，等真数据到了下一趟重算
    会覆盖掉。连续缺失超过 `limit` 说明源出问题了，那时宁可记 None。
    """
    return _carry_forward_dated(list(range(len(values))), values, limit)[0]


def _carry_forward_dated(
    calendar: list, values: list[float | None], limit: int = MARGIN_CARRY_DAYS
) -> tuple[list[float | None], list]:
    """`_carry_forward` 的**带来源版本**：同时返回每个位置「值取自哪一天」。

    有了来源日期，落库时才能区分「今天的两融」与「沿用的昨天（或更早）的两融」——
    15:05 到深夜那段深交所还没披露，页面上那一项其实是前一天的数，不标注的话
    读者会当成当天的（2026-10-31 用户反馈）。
    """
    out: list[float | None] = []
    sources: list = []
    last: float | None = None
    last_day = None
    gap = 0
    for day, value in zip(calendar, values):
        if value is not None:
            last, last_day, gap = value, day, 0
            out.append(value)
            sources.append(day)
            continue
        gap += 1
        if last is not None and gap <= limit:
            out.append(last)
            sources.append(last_day)
        else:
            out.append(None)
            sources.append(None)
    return out, sources


def _index_by_date(session, start: date, end: date) -> dict[date, float]:
    rows = session.execute(
        select(IndexDaily.trade_date, IndexDaily.close).where(
            IndexDaily.code == BENCHMARK_CODE,
            IndexDaily.trade_date >= start,
            IndexDaily.trade_date <= end,
            IndexDaily.close.is_not(None),
        )
    ).all()
    return {day: close for day, close in rows}


def compute_range(start: date, end: date) -> list[dict]:
    """算出 `[start, end]` 内每个交易日的六项原始值，返回可直接写库的行。

    会额外往前读 `WARMUP_DAYS` 天的数据喂窗口，**那段不写库** —— 只有落在
    `[start, end]` 里的交易日才产出。
    """
    warm = start - timedelta(days=WARMUP_DAYS)
    with session_scope() as session:
        calendar = _trade_calendar(session, warm, end)
        if not calendar:
            return []

        sentiment_rows = session.execute(
            select(
                MarketSentiment.trade_date,
                MarketSentiment.total_amount,
                MarketSentiment.limit_up_count,
                MarketSentiment.limit_down_count,
            ).where(
                MarketSentiment.trade_date >= warm,
                MarketSentiment.trade_date <= end,
            )
        ).all()
        amount_by_date = {day: amount for day, amount, _, _ in sentiment_rows}
        limit_by_date = {day: (up, down) for day, _, up, down in sentiment_rows}

        closes_by_date = _index_by_date(session, warm, end)
        margin_by_date = _margin_by_date(session, warm, end)
        breadth_by_date = _breadth_by_date(session, warm, end)

    amounts, _, _ = _align(calendar, amount_by_date)
    closes, close_rank, close_compact = _align(calendar, closes_by_date)
    margins, _, _ = _align(calendar, margin_by_date)
    # 两融 T+1 才齐：最新一天沿用一个齐全日，保证当天也是 6 项（见 `_carry_forward`）。
    # 同时记下每个位置的值「取自哪一天」，落库到 `margin_date` 供前端标注「沿用」。
    margins, margin_sources = _carry_forward_dated(calendar, margins)
    rsi = _rsi_series(close_compact)

    rows: list[dict] = []
    for i, day in enumerate(calendar):
        if day < start:
            continue

        rank = close_rank[i]
        volatility = _volatility(close_compact, rank) if rank is not None else None
        rsi14 = rsi[rank] if rank is not None else None

        turnover_ratio = None
        window_amounts = amounts[i - BASE_DAYS : i]
        if (
            amounts[i] is not None
            and i >= BASE_DAYS
            and all(item is not None for item in window_amounts)
            and sum(window_amounts)
        ):
            turnover_ratio = round(amounts[i] / (sum(window_amounts) / BASE_DAYS), 4)

        margin_chg = None
        margin_date = None
        base_margin = margins[i - BASE_DAYS] if i >= BASE_DAYS else None
        if margins[i] is not None and base_margin:
            margin_chg = round((margins[i] / base_margin - 1) * 100, 4)
            # 变化率算不出来（比如窗口里缺值）时不留来源日期：那天页面上本来就是「—」
            margin_date = margin_sources[i]

        limit_up, limit_down = limit_by_date.get(day, (None, None))
        limit_strength = None
        if limit_up is not None and limit_down is not None and limit_up + limit_down > 0:
            limit_strength = round((limit_up - limit_down) / (limit_up + limit_down) * 100, 2)

        rows.append(
            {
                "trade_date": day,
                "volatility": volatility,
                "rsi14": rsi14,
                "turnover_ratio": turnover_ratio,
                "margin_chg": margin_chg,
                "margin_date": margin_date,
                "breadth": breadth_by_date.get(day),
                "limit_strength": limit_strength,
            }
        )
    return rows


def collect_range(start: date, end: date) -> int:
    """算 `[start, end]` 并写库（幂等 upsert），返回写入行数。"""
    rows = compute_range(start, end)
    if not rows:
        return 0
    with session_scope() as session:
        upsert_many(session, FearGreedDaily, rows)
    return len(rows)


def latest_trade_date() -> date | None:
    """库里有情绪数据的最近交易日（与站点其它地方「哪天算交易日」同口径）。"""
    with session_scope() as session:
        return session.scalar(select(func.max(MarketSentiment.trade_date)))


def _has_rows() -> bool:
    with session_scope() as session:
        return bool(
            session.scalar(select(func.count()).select_from(FearGreedDaily))
        )


def backfill(days: int = DEFAULT_BACKFILL_DAYS) -> int:
    """回看 `days` 个自然日做一次全量回补。"""
    end = latest_trade_date()
    if end is None:
        logger.warning("市场情绪表为空，无法回补恐贪指数")
        return 0
    start = end - timedelta(days=days)
    written = collect_range(start, end)
    logger.info("恐贪指数回补 %s ~ %s：写入 %d 天", start, end, written)
    return written


def refresh(days: int = REFRESH_DAYS) -> int:
    """重算最近 `days` 个交易日。

    表里一行都没有时**自动退回全量回补** —— 这样新库 / 新部署第一趟尾部链
    就自带完整历史，不必人工去跑回补脚本。
    """
    if not _has_rows():
        return backfill()

    end = latest_trade_date()
    if end is None:
        return 0
    with session_scope() as session:
        start = session.scalar(
            select(func.min(MarketSentiment.trade_date)).where(
                MarketSentiment.trade_date.in_(
                    select(MarketSentiment.trade_date)
                    .order_by(MarketSentiment.trade_date.desc())
                    .limit(days)
                    .scalar_subquery()
                )
            )
        )
    if start is None:
        start = end - timedelta(days=days * 2)
    return collect_range(start, end)
