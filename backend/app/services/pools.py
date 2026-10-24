"""选股池的两块数据：RPS 相对强度榜、首板一进二候选。

两个都是**盘后候选池**，不是买卖指令：本站没有盘中数据，打板要看次日承接，
这里只负责把「值得盯的几十只」按可复现的条件筛出来。

口径说明
--------
- RPS：读 `jobs/scan_rps` 落库的当日横截面分位，不做二次计算；
- 首板一进二：条件全部可调（见 `first_board` 的参数），默认值是一套保守参数，
  不是「最优参数」—— 任何参数都应该用自己的库回测过再用。
"""

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    LimitPool,
    RpsDaily,
    StockBasic,
    StockConcept,
    StockDaily,
    TradeCalendar,
)
from app.services import pattern_track
from app.services.patterns import is_st

logger = logging.getLogger(__name__)

#: 单位换算：库里金额一律是**元**，接口参数用「亿」更好读
YI = 1e8

#: 北交所代码前缀（4/8/92）。30% 涨跌停 + 流动性差异，和沪深打板不是一套玩法
_BJ_PREFIXES = ("4", "8", "92")


@dataclass(frozen=True, slots=True)
class RpsRow:
    code: str
    name: str | None
    close: float | None
    pct_chg: float | None
    ret120: float | None
    ret250: float | None
    rps120: float | None
    rps250: float | None


@dataclass(frozen=True, slots=True)
class RpsBoard:
    trade_date: date | None
    rows: list[RpsRow]


def rps_board(
    session: Session,
    trade_date: date | None = None,
    min_rps: float = 0,
    limit: int = 100,
) -> RpsBoard:
    """RPS 榜：按 250 日分位降序。`min_rps` 同时作用在 120 / 250 **两档**分位上。

    2026-10-24 修：原来只卡了 `rps250`，页面选「RPS ≥90」时 RPS120 只有 36、24 的票
    照样出现（10-23 的金富科技、南网数字）—— 与函数说明 / 接口参数说明都对不上。
    两档都要求有值：`rps120` 为 None 的（窗口内交易日不足，常见于长期停牌刚复牌）
    连「≥0」都谈不上，不放进榜里。
    """
    target = trade_date or session.scalar(select(func.max(RpsDaily.trade_date)))
    if target is None:
        return RpsBoard(trade_date=None, rows=[])

    rows = session.execute(
        select(
            RpsDaily.code,
            StockBasic.name,
            StockDaily.close,
            StockDaily.pct_chg,
            RpsDaily.ret120,
            RpsDaily.ret250,
            RpsDaily.rps120,
            RpsDaily.rps250,
        )
        .join(
            StockDaily,
            (StockDaily.trade_date == RpsDaily.trade_date) & (StockDaily.code == RpsDaily.code),
        )
        .outerjoin(StockBasic, StockBasic.code == RpsDaily.code)
        .where(
            RpsDaily.trade_date == target,
            RpsDaily.rps250.is_not(None),
            RpsDaily.rps250 >= min_rps,
            RpsDaily.rps120.is_not(None),
            RpsDaily.rps120 >= min_rps,
        )
        .order_by(RpsDaily.rps250.desc())
        .limit(limit)
    ).all()

    return RpsBoard(
        trade_date=target,
        rows=[
            RpsRow(
                code=row[0],
                name=row[1],
                close=row[2],
                pct_chg=row[3],
                ret120=row[4],
                ret250=row[5],
                rps120=row[6],
                rps250=row[7],
            )
            for row in rows
        ],
    )


@dataclass(frozen=True, slots=True)
class FirstBoardRow:
    code: str
    name: str | None
    pct_chg: float | None
    price: float | None
    first_seal_time: str | None
    last_seal_time: str | None
    open_times: int | None
    float_mv: float | None
    amount: float | None
    turnover: float | None
    seal_amount: float | None
    #: 封单额 / 流通市值（%），衡量封板质量
    seal_ratio: float | None
    #: 封单额 / 当日成交额（%），衡量「有承接的封板」—— 成交额很小的板，
    #: 封单占比再高也可能是没量的一字板
    seal_to_amount: float | None
    industry: str | None
    #: 当日涨停股里，与它同属一个精选板块最多有几只（含自己）
    theme: str | None
    theme_count: int
    #: 当天是不是真首板：D 之前 10 个交易日**一次涨停都没有**（`consecutive == 1`
    #: 只保证今天是一板，前几天刚涨停过、断一天再涨停的票也会被算进来）
    recent_limit_ups: int = 0
    #: 当天的封板形态：`一字` / `T字` / None
    board_shape: str | None = None
    #: 它在 `theme` 里按首封时间的排名（1 = 该题材里最早封板），`theme_count` 是分母
    theme_rank: int | None = None
    #: 第二热的题材，悬停时显示
    theme2: str | None = None
    theme2_count: int = 0
    #: 综合排序分（0~100）：题材家数 / 封单比 / 首封时间的**当日候选内分位**加权
    score: float = 0.0


@dataclass(frozen=True, slots=True)
class FirstBoardBoard:
    trade_date: date | None
    conditions: dict = field(default_factory=dict)
    rows: list[FirstBoardRow] = field(default_factory=list)


#: 综合排序的权重（**当日候选内分位**加权）。只是起手值 —— 用页面上的
#: 「次日验证」面板对比旧排序 / 新排序的前 5 只表现之后再调。
SCORE_W_THEME = 0.4
SCORE_W_SEAL = 0.4
SCORE_W_TIME = 0.2

#: 「真首板」的回看窗口（交易日）：D 之前这么多天里一次涨停都没有才算
TRUE_FIRST_BOARD_LOOKBACK = 10


def _board_shape(
    open_: float | None, high: float | None, low: float | None, close: float | None
) -> str | None:
    """涨停当天的封板形态：`一字` / `T字` / None。

    - 一字：开 = 高 = 低 = 收（竞价就封死，全天没有波动）
    - T字：开 = 高，但盘中打开过（低 < 开）

    三个价格都来自同一行不复权日线，直接比相等即可（同一数据源、同一天）。
    """
    if None in (open_, high, low, close):
        return None
    if open_ == high == low == close:
        return "一字"
    if open_ == high and low is not None and low < open_:
        return "T字"
    return None


def _percentiles(values: list[float]) -> list[float]:
    """同一批数值在该批里的百分位（0~100，最大 = 100）。并列取同一档。"""
    if not values:
        return []
    if len(values) == 1:
        return [100.0]
    order = sorted(range(len(values)), key=lambda index: values[index])
    percent = [0.0] * len(values)
    for rank, index in enumerate(order):
        percent[index] = rank / (len(values) - 1) * 100
    return percent


def first_board(
    session: Session,
    trade_date: date | None = None,
    before: str = "140000",
    min_float_mv: float = 20,
    max_float_mv: float = 300,
    min_amount: float = 3,
    min_themes: int = 3,
    min_seal_ratio: float = 0,
    only_true_first_board: bool = False,
    limit: int = 50,
) -> FirstBoardBoard:
    """首板一进二候选。

    条件（全部可调，金额单位**亿**）：
    - `before`：首封时间早于它（`HHMMSS`，默认 14:00 —— 只排掉尾盘偷袭，
      10:30 那种卡法实测会把池子清空：2026-10-23 全天只有 3 只在 10:30 前封板）
    - `min_float_mv` / `max_float_mv`：流通市值区间
    - `min_amount`：当日成交额下限
    - `min_themes`：当日同题材涨停家数下限（用 `stock_concept` 的精选板块口径）
    - `min_seal_ratio`：封单额 / 流通市值 的下限（%），默认 0 = 不筛
    - `only_true_first_board`：只看**真首板**（D 之前 10 个交易日没涨停过），默认关

    ⚠️ 只筛「首板」（`consecutive == 1`）且**没炸过板**（`open_times == 0`）的票。
    `consecutive == 1` 不等于真首板 —— 前几天涨停过、断一天再涨停的票也算 1 板，
    所以另给 `recent_limit_ups`（近 10 个交易日涨停次数）让人自己决定要不要剔。

    排序（2026-10-24 改）：不再是「题材家数优先」—— 那样会把封单 0.02%（基本没封住）
    的票排到封单 1.35% 的前面。现在是三个**当日候选内分位**的加权：
    题材家数 0.4 + 封单比 0.4 + 首封早晚 0.2（见 `SCORE_W_*`）。
    """
    target = trade_date or session.scalar(
        select(func.max(LimitPool.trade_date)).where(LimitPool.pool_type == "up")
    )
    if target is None:
        return FirstBoardBoard(trade_date=None, conditions={})

    rows = list(
        session.scalars(
            select(LimitPool)
            .where(
                LimitPool.trade_date == target,
                LimitPool.pool_type == "up",
                LimitPool.consecutive == 1,
                # 没炸过板。2026-10-24 核查过：库里涨停池 18003 行（255 个交易日，
                # 2025-09-08 ~ 2026-09-24，本地库能查到的全量）`open_times` **没有空值**，
                # 所以不必写成 `== 0 OR IS NULL`；将来换数据源要重跑一遍这条 SQL。
                LimitPool.open_times == 0,
                LimitPool.first_seal_time.is_not(None),
                LimitPool.first_seal_time <= before,
                LimitPool.float_mv >= min_float_mv * YI,
                LimitPool.float_mv <= max_float_mv * YI,
                LimitPool.amount >= min_amount * YI,
            )
            .order_by(LimitPool.first_seal_time)
        )
    )

    # 题材：当日每只涨停股的精选板块 → 板块内涨停家数、板块成员（算题材内首封排名用）
    concepts: dict[str, list[str]] = {}
    members: dict[str, list[str]] = defaultdict(list)
    counter: Counter[str] = Counter()
    for code, concept in session.execute(
        select(StockConcept.code, StockConcept.concept).where(
            StockConcept.trade_date == target
        )
    ):
        concepts.setdefault(code, []).append(concept)
        members[concept].append(code)
        counter[concept] += 1

    # 当日**全部**涨停股**的首封时间（题材内排名用；候选只是其中一小部分）
    seal_times = dict(
        session.execute(
            select(LimitPool.code, LimitPool.first_seal_time).where(
                LimitPool.trade_date == target, LimitPool.pool_type == "up"
            )
        ).all()
    )
    theme_rank: dict[str, dict[str, int]] = {}
    for concept, codes_in in members.items():
        ordered = sorted(codes_in, key=lambda item: seal_times.get(item) or "235959")
        theme_rank[concept] = {
            code: index + 1 for index, code in enumerate(ordered)
        }

    # 近 10 个交易日涨停次数（真首板 = 0）。按交易日历取，停牌日不算
    prior_days = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date < target)
            .order_by(TradeCalendar.trade_date.desc())
            .limit(TRUE_FIRST_BOARD_LOOKBACK)
        )
    )
    recent_limit_ups: dict[str, int] = {}
    if prior_days:
        recent_limit_ups = dict(
            session.execute(
                select(LimitPool.code, func.count())
                .where(
                    LimitPool.trade_date.in_(prior_days),
                    LimitPool.pool_type == "up",
                )
                .group_by(LimitPool.code)
            ).all()
        )

    names = dict(
        session.execute(
            select(StockBasic.code, StockBasic.name).where(
                StockBasic.code.in_([row.code for row in rows])
            )
        ).all()
    )

    shapes = {
        code: _board_shape(open_, high, low, close)
        for code, open_, high, low, close in session.execute(
            select(
                StockDaily.code,
                StockDaily.open,
                StockDaily.high,
                StockDaily.low,
                StockDaily.close,
            ).where(
                StockDaily.trade_date == target,
                StockDaily.code.in_([row.code for row in rows]),
            )
        )
    }

    picked: list[FirstBoardRow] = []
    for row in rows:
        name = names.get(row.code) or row.name
        if is_st(name):
            continue
        # 北交所不是这套玩法：30% 涨跌停、流动性差，默认排除
        if row.code.startswith(_BJ_PREFIXES):
            continue
        stock_concepts = concepts.get(row.code) or []
        theme = max(stock_concepts, key=lambda item: counter[item], default=None)
        theme_count = counter[theme] if theme else 0
        if theme_count < min_themes:
            continue
        # 第二热的题材：同一天里另一条线索，悬停时给出来
        others = sorted(
            (item for item in stock_concepts if item != theme),
            key=lambda item: counter[item],
            reverse=True,
        )
        theme2 = others[0] if others else None
        past_ups = int(recent_limit_ups.get(row.code, 0))
        if only_true_first_board and past_ups:
            continue
        seal_ratio = (
            row.seal_amount / row.float_mv * 100
            if row.seal_amount and row.float_mv
            else None
        )
        if min_seal_ratio and (seal_ratio is None or seal_ratio < min_seal_ratio):
            continue
        seal_to_amount = (
            row.seal_amount / row.amount * 100
            if row.seal_amount and row.amount
            else None
        )
        picked.append(
            FirstBoardRow(
                code=row.code,
                name=name,
                pct_chg=row.pct_chg,
                price=row.price,
                first_seal_time=row.first_seal_time,
                last_seal_time=row.last_seal_time,
                open_times=row.open_times,
                float_mv=row.float_mv,
                amount=row.amount,
                turnover=row.turnover,
                seal_amount=row.seal_amount,
                seal_ratio=round(seal_ratio, 2) if seal_ratio is not None else None,
                seal_to_amount=(
                    round(seal_to_amount, 1) if seal_to_amount is not None else None
                ),
                industry=row.industry,
                theme=theme,
                theme_count=theme_count,
                recent_limit_ups=past_ups,
                board_shape=shapes.get(row.code),
                theme_rank=(
                    theme_rank.get(theme, {}).get(row.code) if theme else None
                ),
                theme2=theme2,
                theme2_count=counter[theme2] if theme2 else 0,
            )
        )

    # 综合分：三个**当日候选内**分位加权（理由见函数说明）。
    # 首封时间越早越好 → 取负号再算分位；封单比缺值按 0 算（等于排在最后）。
    theme_pct = _percentiles([float(item.theme_count) for item in picked])
    seal_pct = _percentiles([item.seal_ratio or 0.0 for item in picked])
    time_pct = _percentiles([-float(item.first_seal_time or "235959") for item in picked])
    scored = [
        replace(
            item,
            score=round(
                theme_pct[index] * SCORE_W_THEME
                + seal_pct[index] * SCORE_W_SEAL
                + time_pct[index] * SCORE_W_TIME,
                1,
            ),
        )
        for index, item in enumerate(picked)
    ]
    scored.sort(key=lambda item: (-item.score, -(item.seal_ratio or 0), item.code))
    return FirstBoardBoard(
        trade_date=target,
        conditions={
            "before": before,
            "min_float_mv": min_float_mv,
            "max_float_mv": max_float_mv,
            "min_amount": min_amount,
            "min_themes": min_themes,
            "min_seal_ratio": min_seal_ratio,
            "only_true_first_board": only_true_first_board,
        },
        rows=scored[:limit],
    )


# ---------------------------------------------------------------- 次日验证
#
# 页面上的阈值（首封时间 / 市值 / 题材家数）都没有「标准答案」，必须用库里的数据
# 验证过再用。这两个函数回答「按当前条件选出来的票，第二天到底怎么样」——
# 参数一改，页面上的汇总行立刻跟着变，这是定阈值的唯一依据。
#
# 口径（写死了，别在页面里另算）：
# - **买入 = T+1 开盘**，所以开盘溢价与收益都从 T+1 起算；
# - T+1 开盘就一字涨停（晋级且开=高=低=收）的**买不到**，单独计数、不进收益均值；
# - `open_premium` 的分母用 `close / (1 + pct_chg/100)` 反推的**昨收**（交易所除权
#   参考价），直接用 T 日收盘会在除权日算出一个假的跳空；
# - T+1 的日线还没采到时，这一天记 `pending`，**不计入统计**。

#: 「大面」的门槛：T+1 收盘 ≤ 这个数（%）
_BIG_LOSS_PCT = -5.0


@dataclass(frozen=True, slots=True)
class TrackDay:
    trade_date: date
    next_date: date | None
    candidates: int
    promoted: int
    #: 开盘溢价 / 收盘收益均值（%）。只统计「买得到」的样本
    open_premium_mean: float | None
    close_ret_mean: float | None
    #: 一字涨停买不进 / 次日没有日线（停牌或未采）的只数
    unbuyable: int
    no_quote: int
    #: `ok` = 已验证；`pending` = 次日日线还没到
    status: str


@dataclass(frozen=True, slots=True)
class FirstBoardTrack:
    trade_date: date | None
    conditions: dict = field(default_factory=dict)
    days: list[TrackDay] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


def _mean_median(values: list[float]) -> tuple[float | None, float | None]:
    """`(均值, 中位)`，单位 %，两位小数。空样本给 (None, None)。"""
    if not values:
        return None, None
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2
    )
    return round(sum(ordered) / len(ordered), 2), round(median, 2)


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator * 100, 1) if denominator else None


def first_board_track(
    session: Session,
    days: int = 20,
    before: str = "140000",
    min_float_mv: float = 20,
    max_float_mv: float = 300,
    min_amount: float = 3,
    min_themes: int = 3,
    min_seal_ratio: float = 0,
    only_true_first_board: bool = False,
) -> FirstBoardTrack:
    """近 `days` 个有涨停池的交易日，按当前条件选出来的首板次日表现。

    条件参数与 `first_board` 完全一致（内部就是调它），所以页面上的候选和这里
    统计的样本是同一批票 —— 参数改了，两边一起变。
    """
    limit_days = list(
        session.scalars(
            select(LimitPool.trade_date)
            .where(LimitPool.pool_type == "up")
            .group_by(LimitPool.trade_date)
            .order_by(LimitPool.trade_date.desc())
            .limit(days)
        )
    )
    conditions = {
        "before": before,
        "min_float_mv": min_float_mv,
        "max_float_mv": max_float_mv,
        "min_amount": min_amount,
        "min_themes": min_themes,
        "min_seal_ratio": min_seal_ratio,
        "only_true_first_board": only_true_first_board,
    }
    if not limit_days:
        return FirstBoardTrack(trade_date=None, conditions=conditions)

    dates = sorted(limit_days)
    calendar = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date >= dates[0])
            .order_by(TradeCalendar.trade_date)
        )
    )
    position = {day: index for index, day in enumerate(calendar)}
    data_end = session.scalar(select(func.max(StockDaily.trade_date)))

    def next_trading(day: date) -> date | None:
        index = position.get(day)
        if index is None or index + 1 >= len(calendar):
            return None
        return calendar[index + 1]

    day_rows: list[TrackDay] = []
    open_values: list[float] = []
    close_values: list[float] = []
    quoted = 0  # 有次日日线的候选（含一字板）
    promoted_total = 0
    unbuyable_total = 0
    no_quote_total = 0
    big_loss_total = 0
    limit_down_total = 0

    for day in dates:
        board = first_board(
            session,
            trade_date=day,
            before=before,
            min_float_mv=min_float_mv,
            max_float_mv=max_float_mv,
            min_amount=min_amount,
            min_themes=min_themes,
            min_seal_ratio=min_seal_ratio,
            only_true_first_board=only_true_first_board,
            # 验证要看**全部**候选，不能被页面上的前 50 只截断
            limit=1000,
        )
        codes = [row.code for row in board.rows]
        following = next_trading(day)
        if not codes:
            day_rows.append(
                TrackDay(day, following, 0, 0, None, None, 0, 0, "ok")
            )
            continue
        if following is None or data_end is None or following > data_end:
            day_rows.append(
                TrackDay(day, following, len(codes), 0, None, None, 0, 0, "pending")
            )
            continue

        quotes = {
            code: (open_, high, low, close, pct)
            for code, open_, high, low, close, pct in session.execute(
                select(
                    StockDaily.code,
                    StockDaily.open,
                    StockDaily.high,
                    StockDaily.low,
                    StockDaily.close,
                    StockDaily.pct_chg,
                ).where(StockDaily.trade_date == following)
            )
        }
        pools_up: set[str] = set()
        pools_down: set[str] = set()
        for pool_type, code in session.execute(
            select(LimitPool.pool_type, LimitPool.code).where(
                LimitPool.trade_date == following,
                LimitPool.pool_type.in_(("up", "down")),
            )
        ):
            (pools_up if pool_type == "up" else pools_down).add(code)

        day_promoted = 0
        day_unbuyable = 0
        day_no_quote = 0
        day_open: list[float] = []
        day_close: list[float] = []
        for code in codes:
            quote = quotes.get(code)
            if quote is None:
                day_no_quote += 1
                continue
            open_, high, low, close, pct = quote
            promoted = code in pools_up
            if promoted:
                day_promoted += 1
            if (
                promoted
                and open_ is not None
                and high is not None
                and low is not None
                and close is not None
                and high == low == open_ == close
            ):
                # 一字涨停：开盘价就是板价，按开盘价买不进
                day_unbuyable += 1
                continue
            if not close or pct is None or pct <= -100:
                day_no_quote += 1
                continue
            # 昨收用官方涨跌幅反推：除权日直接拿 T 日收盘会算出一个假跳空
            prev_close = close / (1 + pct / 100)
            if prev_close <= 0 or open_ is None:
                day_no_quote += 1
                continue
            day_open.append((open_ / prev_close - 1) * 100)
            day_close.append(pct)
            if pct <= _BIG_LOSS_PCT:
                big_loss_total += 1
            if code in pools_down:
                limit_down_total += 1

        quoted += len(codes) - day_no_quote
        promoted_total += day_promoted
        unbuyable_total += day_unbuyable
        no_quote_total += day_no_quote
        open_values.extend(day_open)
        close_values.extend(day_close)
        day_rows.append(
            TrackDay(
                day,
                following,
                len(codes),
                day_promoted,
                _mean_median(day_open)[0],
                _mean_median(day_close)[0],
                day_unbuyable,
                day_no_quote,
                "ok",
            )
        )

    open_mean, open_median = _mean_median(open_values)
    close_mean, close_median = _mean_median(close_values)
    summary = {
        # 已验证 / 待验证的天数（「待验证」= 次日日线还没采到）
        "verified_days": sum(1 for row in day_rows if row.status == "ok"),
        "pending_days": sum(1 for row in day_rows if row.status == "pending"),
        # 样本 = 有次日日线、且买得到的候选
        "samples": len(close_values),
        "candidates": quoted,
        "promoted_rate": _rate(promoted_total, quoted),
        "open_premium_mean": open_mean,
        "open_premium_median": open_median,
        "close_ret_mean": close_mean,
        "close_ret_median": close_median,
        "big_loss_rate": _rate(big_loss_total, len(close_values)),
        "limit_down_rate": _rate(limit_down_total, len(close_values)),
        "unbuyable_rate": _rate(unbuyable_total, quoted),
        "no_quote": no_quote_total,
    }
    return FirstBoardTrack(
        trade_date=dates[-1], conditions=conditions, days=day_rows, summary=summary
    )


@dataclass(frozen=True, slots=True)
class RpsTrackDay:
    trade_date: date
    picks: int
    ret1: float | None
    ret5: float | None
    excess1: float | None
    excess5: float | None
    #: `ok` = 至少能算出 1 日收益；`pending` = 次日日线还没到
    status: str


@dataclass(frozen=True, slots=True)
class RpsTrack:
    trade_date: date | None
    days: list[RpsTrackDay] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


def _empty_rps_summary() -> dict:
    return {
        "days": 0,
        "verified_days": 0,
        "samples": 0,
        "ret1": {"days": 0, "mean": None, "excess": None},
        "ret5": {"days": 0, "mean": None, "excess": None},
    }


def rps_track(
    session: Session,
    days: int = 20,
    min_rps: float = 0,
    top: int = 50,
) -> RpsTrack:
    """近 `days` 个**有 RPS 记录**的交易日，榜单前 `top` 只的 1 / 5 日表现。

    ⚠️ `rps_daily` 只存「扫描跑过的那一天」，所以这里统计的天数就是线上真实跑过的
    天数，不是自然交易日 —— 中间断过的日子没有样本，页面要如实显示 `days`。

    收益与超额**直接用 `pattern_track._curves`**（与形态跟踪同一份矩阵、同一套
    「全市场等权」基准），别在这里另写一遍。
    """
    dates = sorted(
        session.scalars(
            select(RpsDaily.trade_date)
            .group_by(RpsDaily.trade_date)
            .order_by(RpsDaily.trade_date.desc())
            .limit(days)
        )
    )
    if not dates:
        return RpsTrack(trade_date=None, summary=_empty_rps_summary())

    picks: dict[date, list[tuple[str, str | None, float]]] = {}
    for day in dates:
        board = rps_board(session, trade_date=day, min_rps=min_rps, limit=top)
        if board.rows:
            picks[day] = [
                (row.code, row.name, row.rps250 or 0.0) for row in board.rows
            ]
    if not picks:
        return RpsTrack(trade_date=dates[-1], summary=_empty_rps_summary())

    curves = pattern_track._curves(session, picks, 5)  # 复用同一份矩阵与「全市场等权」基准

    day_rows: list[RpsTrackDay] = []
    stats: dict[int, list[tuple[float, float]]] = {1: [], 5: []}
    for day in dates:
        curve = curves.get(day, {})
        values: dict[int, tuple[float | None, float | None]] = {}
        for step in (1, 5):
            entry = curve.get(step)
            if entry is None or not len(entry[0]):
                values[step] = (None, None)
                continue
            mean = float(entry[0].mean()) * 100
            market = entry[1]
            excess = mean - market * 100 if market == market else None
            values[step] = (round(mean, 2), None if excess is None else round(excess, 2))
            if excess is not None:
                stats[step].append((mean, excess))
        ok = values[1][0] is not None
        day_rows.append(
            RpsTrackDay(
                day,
                len(picks.get(day, [])),
                values[1][0],
                values[5][0],
                values[1][1],
                values[5][1],
                "ok" if ok else "pending",
            )
        )

    def _summarize(step: int) -> dict:
        items = stats[step]
        return {
            "days": len(items),
            "mean": round(sum(item[0] for item in items) / len(items), 2) if items else None,
            "excess": round(sum(item[1] for item in items) / len(items), 2) if items else None,
        }

    return RpsTrack(
        trade_date=dates[-1],
        days=day_rows,
        summary={
            "days": len(day_rows),
            "verified_days": sum(1 for row in day_rows if row.status == "ok"),
            "samples": sum(row.picks for row in day_rows if row.status == "ok"),
            "ret1": _summarize(1),
            "ret5": _summarize(5),
        },
    )

