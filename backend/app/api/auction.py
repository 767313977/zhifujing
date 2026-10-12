"""竞价强度接口（集合竞价）。

数据来自 `auction_snapshot`（腾讯批量行情，交易日 9:15–9:26 逐分钟采样，
见 `jobs/collect_auction.py`）。这里只读，不做任何补齐 —— 取不到的字段返回 null，
前端显示「—」，绝不编数字。
"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    SAMPLE_ASK1_PRICE,
    SAMPLE_ASK1_VOLUME,
    SAMPLE_BID1_PRICE,
    SAMPLE_BID1_VOLUME,
    SAMPLE_COMMITTEE,
    SAMPLE_PRICE,
    AuctionSnapshot,
    LimitPool,
    StockBasic,
    StockDaily,
    TradeCalendar,
)
from app.schemas import (
    AuctionBoardItem,
    AuctionBoardsOut,
    AuctionListOut,
    AuctionRow,
    AuctionTrendOut,
    AuctionTrendPoint,
)

router = APIRouter(prefix="/api", tags=["复盘"])

logger = logging.getLogger(__name__)

# 排序字段白名单。直接拼列名会有注入风险，也挡不住打错字
SORT_FIELDS = {
    "amount": AuctionSnapshot.amount,
    "pct": AuctionSnapshot.pct_chg,
    "ratio": AuctionSnapshot.volume_ratio,
    "turnover": AuctionSnapshot.turnover,
    "float_mv": AuctionSnapshot.float_mv,
    "price": AuctionSnapshot.price,
}


def _latest_date(session: Session) -> date | None:
    return session.scalar(select(func.max(AuctionSnapshot.trade_date)))


def _prev_trade_date(session: Session, day: date) -> date | None:
    """上一个交易日（按交易日历，不按 snapshot 自己的日期）。"""
    return session.scalar(
        select(TradeCalendar.trade_date)
        .where(TradeCalendar.trade_date < day)
        .order_by(TradeCalendar.trade_date.desc())
        .limit(1)
    )


# 「竞价量比」的回看天数与最小样本数（见 `_auction_ratios`）。
RATIO_LOOKBACK = 5
RATIO_MIN_DAYS = 3

# 「9:20 撤单」的取样时刻：9:20 是不可撤单的截止线，取它**前后各一帧**算挂单量变化。
# ⚠️ 两帧的**挂单量**都得有值才算得出（腾讯竞价时段是否真给这个字段尚未核实）。
CANCEL_BEFORE = "0920"
CANCEL_AFTER = "0921"


def _auction_ratios(
    session: Session, trade_date: date, rows: list[AuctionSnapshot]
) -> dict[str, float]:
    """`{code: 竞价量比}` —— 当日竞价量 ÷ 前 5 个交易日的竞价量均值。

    口径取自同花顺对「竞价量比」的定义（其指标说明：竞价量比 = 竞价量 ÷ 前五日竞价量
    平均值）。**自己算**是因为数据源不给：腾讯的「量比」开盘前不填（见
    `jobs/collect_auction`，所以另有 9:31 补采帧取「开盘量比」），而 iFinD 全市场
    单次只给 10 只、跑不动。

    ⚠️ 分母只统计**有值**的那些天（停牌 / 采样缺失那天不计入），且至少要有
    `RATIO_MIN_DAYS` 天才给结果；否则留空 —— 拿「1 天的均值」冒充基准更误导。
    """
    if not rows:
        return {}
    days = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date < trade_date)
            .order_by(TradeCalendar.trade_date.desc())
            .limit(RATIO_LOOKBACK)
        )
    )
    if len(days) < RATIO_MIN_DAYS:
        return {}

    history: dict[str, list[float]] = {}
    for code, volume in session.execute(
        select(AuctionSnapshot.code, AuctionSnapshot.volume).where(
            AuctionSnapshot.trade_date.in_(days),
            AuctionSnapshot.code.in_([row.code for row in rows]),
        )
    ).all():
        if volume:
            history.setdefault(code, []).append(volume)

    ratios: dict[str, float] = {}
    for row in rows:
        volumes = history.get(row.code) or []
        if len(volumes) < RATIO_MIN_DAYS or not row.volume:
            continue
        mean = sum(volumes) / len(volumes)
        if mean > 0:
            ratios[row.code] = round(row.volume / mean, 2)
    return ratios


def _rank_map(values: list[float]) -> dict[float, int]:
    """值 → 名次（1 起，**并列同名次**，降序）。"""
    return {value: index + 1 for index, value in enumerate(sorted(set(values), reverse=True))}


def _frame_at(samples: list | None, hhmm: str, index: int) -> object:
    """`hhmm` 那一帧的第 `index` 列；**没有这一帧 / 越界 / 老格式 → None**。

    老行的 `samples` 只有前 3 列（2026-10-12 之前采的），所以一律越界当 None，别假设长度。
    """
    for frame in samples or []:
        if len(frame) > index and frame[0] == hhmm:
            return frame[index]
    return None


def _frame_last(samples: list | None, index: int) -> object:
    """最后一帧的第 `index` 列（竞价终值）。越界 / 老格式 → None。"""
    for frame in reversed(samples or []):
        if len(frame) > index:
            return frame[index]
    return None


def _open_volume(samples: list | None, hhmm: str, limit_up, limit_down) -> float | None:
    """`hhmm` 那一帧**涨停/跌停价上的挂单量**（股）。

    一字板时「买一」就是涨停价上的封单、跌停时「卖一」是跌停价上的封单，所以：
    涨停挂单取买一量、跌停挂单取卖一量；两边都不是就返回 None（普通票没有「挂单」可言）。
    """
    bid_price = _frame_at(samples, hhmm, SAMPLE_BID1_PRICE)
    bid_vol = _frame_at(samples, hhmm, SAMPLE_BID1_VOLUME)
    ask_price = _frame_at(samples, hhmm, SAMPLE_ASK1_PRICE)
    ask_vol = _frame_at(samples, hhmm, SAMPLE_ASK1_VOLUME)
    if limit_up and bid_price and bid_price >= limit_up and bid_vol:
        return float(bid_vol)
    if limit_down and ask_price and ask_price <= limit_down and ask_vol:
        return float(ask_vol)
    return None


def _amplitude(samples: list | None, pre_close: float | None) -> float | None:
    """**竞价价格波动率（%）** = (全程最高价 − 最低价) ÷ 昨收 × 100。

    用逐分钟竞价价（`SAMPLE_PRICE`）。只在**至少有 2 帧**且昨收有效时给值 ——
    只有 1 帧就谈不上「波动」。
    """
    prices = [
        frame[SAMPLE_PRICE]
        for frame in (samples or [])
        if len(frame) > SAMPLE_PRICE and frame[SAMPLE_PRICE]
    ]
    if len(prices) < 2 or not pre_close:
        return None
    return round((max(prices) - min(prices)) / pre_close * 100, 2)


def _metrics(
    session: Session, trade_date: date, rows: list[AuctionSnapshot]
) -> dict[str, dict]:
    """给这批票算派生指标：波动率 / 自由流通市值 / 真实竞价换手 / 竞价额占比 / 排名 / 昨日筹码。

    ⚠️ **排名必须按全市场算** —— 只拿返回的这 300 行去排，位次是错的。所以要额外查一次
    当日**全部**票的竞价涨幅与行业归属。

    「昨日首板 / 连板」不在这里 —— 它就是 `AuctionRow.consecutive`（昨日涨停池的连板数）；
    这里只补「昨日炸板」（查昨日的炸板池，与涨停池互斥）和「昨日换手」。
    """
    prev = _prev_trade_date(session, trade_date)
    codes = [row.code for row in rows]

    # --- 全市场涨幅排名 + 行业内排名 ---
    market = {
        code: pct_chg
        for code, pct_chg in session.execute(
            select(AuctionSnapshot.code, AuctionSnapshot.pct_chg).where(
                AuctionSnapshot.trade_date == trade_date
            )
        ).all()
    }
    ranks = _rank_map([pct for pct in market.values() if pct is not None])
    # 行业归属取 stock_basic 的**同花顺行业**（全 A 都有），不能借涨停池那套
    # （`limit_pool.industry` / `stock_concept` 只覆盖涨停股）
    industries = dict(session.execute(select(StockBasic.code, StockBasic.industry)).all())
    by_industry: dict[str, list[float]] = {}
    for code, pct_chg in market.items():
        industry = industries.get(code)
        if industry and pct_chg is not None:
            by_industry.setdefault(industry, []).append(pct_chg)
    industry_ranks = {name: _rank_map(values) for name, values in by_industry.items()}

    # --- 自由流通股（同花顺口径，覆盖全 A；建池时写进 stock_basic）---
    free_shares = dict(
        session.execute(
            select(StockBasic.code, StockBasic.free_float_shares).where(
                StockBasic.code.in_(codes)
            )
        ).all()
    )

    # --- 昨日：全天成交额 / 换手率（日线）+ 是否炸板（炸板池）---
    prev_daily: dict[str, tuple[float | None, float | None]] = {}
    prev_broken: set[str] = set()
    if prev is not None:
        prev_daily = {
            code: (amount, turnover)
            for code, amount, turnover in session.execute(
                select(StockDaily.code, StockDaily.amount, StockDaily.turnover).where(
                    StockDaily.trade_date == prev, StockDaily.code.in_(codes)
                )
            ).all()
        }
        prev_broken = set(
            session.scalars(
                select(LimitPool.code).where(
                    LimitPool.trade_date == prev, LimitPool.pool_type == "broken"
                )
            )
        )

    metrics: dict[str, dict] = {}
    for row in rows:
        shares = free_shares.get(row.code)
        prev_amount, prev_turnover = prev_daily.get(row.code, (None, None))
        free_mv = shares * row.price if shares and row.price else None
        # **9:20 撤单率（%）** = (9:20 挂单量 − 9:21 挂单量) ÷ 9:20 挂单量 × 100。
        # 9:20 之后不能撤单，之前挂的很多是假单；撤得越少越真。负值 = 挂单还在**增加**。
        # 只在两帧都取到「涨停/跌停价上的挂单量」时才算 —— 普通票本来就没有这个量。
        before = _open_volume(row.samples, CANCEL_BEFORE, row.limit_up, row.limit_down)
        after = _open_volume(row.samples, CANCEL_AFTER, row.limit_up, row.limit_down)
        cancel_rate = (
            round((before - after) / before * 100, 2) if before and after is not None else None
        )
        # **封成比（%）** = 涨停价上的封单额 ÷ 自由流通市值。只在**竞价就封在涨停价上**
        # 时有意义（打板核心指标：>1% 较强、>3% 超强）。
        seal_ratio = None
        if row.limit_up and row.price is not None and row.price >= row.limit_up and free_mv:
            bid_price = _frame_last(row.samples, SAMPLE_BID1_PRICE)
            bid_volume = _frame_last(row.samples, SAMPLE_BID1_VOLUME)
            if bid_price and bid_volume:
                seal_ratio = round(bid_price * bid_volume / free_mv * 100, 2)
        metrics[row.code] = {
            "amplitude": _amplitude(row.samples, row.pre_close),
            # 自由流通市值用**竞价价**（当日筹码的实时计价基准）
            "free_float_mv": free_mv,
            # 真实竞价换手 = 竞价量 ÷ 自由流通股（普通换手率的分母含大量不动的筹码，会低估）
            "free_turnover": (
                round(row.volume / shares * 100, 3) if shares and row.volume is not None else None
            ),
            # 竞价额占昨日全天成交额的比例 —— 绝对金额在大/小盘之间没有可比性
            "amount_ratio": (
                round(row.amount / prev_amount * 100, 2)
                if row.amount and prev_amount
                else None
            ),
            "rank": ranks.get(row.pct_chg) if row.pct_chg is not None else None,
            "industry_rank": (
                industry_ranks.get(industries.get(row.code) or "", {}).get(row.pct_chg)
                if row.pct_chg is not None
                else None
            ),
            "prev_broken": row.code in prev_broken,
            "prev_turnover": prev_turnover,
            # 未匹配量差（委差，股）：委买总量 − 委卖总量，取竞价终值
            "committee_diff": _frame_last(row.samples, SAMPLE_COMMITTEE),
            "cancel_rate": cancel_rate,
            "seal_ratio": seal_ratio,
        }
    return metrics


@router.get("/auction", response_model=AuctionListOut)
def auction_list(
    trade_date: date | None = Query(None, alias="date"),
    sort: str = Query("amount", description="amount/pct/ratio/turnover/float_mv/price"),
    order: str = Query("desc", description="desc/asc"),
    min_pct: float | None = Query(None, description="竞价涨幅下限（%）"),
    max_pct: float | None = Query(None, description="竞价涨幅上限（%）"),
    min_amount: float | None = Query(None, description="竞价金额下限（元）"),
    max_amount: float | None = Query(None, description="竞价金额上限（元）"),
    min_ratio: float | None = Query(None, description="量比下限"),
    max_ratio: float | None = Query(None, description="量比上限"),
    min_turnover: float | None = Query(None, description="竞价换手下限（%）"),
    max_turnover: float | None = Query(None, description="竞价换手上限（%）"),
    min_boards: int | None = Query(None, ge=1, description="昨日连板数下限（几板）"),
    limit: int = Query(200, ge=1, le=1000),
    session: Session = Depends(get_db),
) -> AuctionListOut:
    """竞价强度主表。

    缺省取库中**最新有竞价数据的一天**、按**竞价金额降序**。筛选参数对应参考图
    顶部那排筛选项（竞价涨幅 / 竞价金额 / 量比 / 换手 / 连板）。
    """
    if trade_date is None:
        trade_date = _latest_date(session)
        if trade_date is None:
            return AuctionListOut(trade_date=date.today(), total=0, shown=0, rows=[])

    # 昨日连板数：当日 9:25 时今日的涨停池还没有，所以「几板」只能取昨日口径
    prev = _prev_trade_date(session, trade_date)
    boards: dict[str, int] = {}
    if prev is not None:
        boards = {
            code: (level or 1)
            for code, level in session.execute(
                select(LimitPool.code, LimitPool.consecutive).where(
                    LimitPool.trade_date == prev, LimitPool.pool_type == "up"
                )
            ).all()
        }

    conditions = [AuctionSnapshot.trade_date == trade_date]
    if min_pct is not None:
        conditions.append(AuctionSnapshot.pct_chg >= min_pct)
    if max_pct is not None:
        conditions.append(AuctionSnapshot.pct_chg <= max_pct)
    if min_amount is not None:
        conditions.append(AuctionSnapshot.amount >= min_amount)
    if max_amount is not None:
        conditions.append(AuctionSnapshot.amount <= max_amount)
    if min_ratio is not None:
        conditions.append(AuctionSnapshot.volume_ratio >= min_ratio)
    if max_ratio is not None:
        conditions.append(AuctionSnapshot.volume_ratio <= max_ratio)
    if min_turnover is not None:
        conditions.append(AuctionSnapshot.turnover >= min_turnover)
    if max_turnover is not None:
        conditions.append(AuctionSnapshot.turnover <= max_turnover)
    if min_boards is not None:
        # 连板数不在表里，转成「昨日该档以上的代码集合」再过滤。
        # 集合规模不大（昨日涨停通常几十只），IN 不构成压力
        hit = [code for code, level in boards.items() if level >= min_boards]
        conditions.append(AuctionSnapshot.code.in_(hit))

    column = SORT_FIELDS.get(sort, AuctionSnapshot.amount)
    direction = column.asc() if order == "asc" else column.desc()
    rows = list(
        session.scalars(
            select(AuctionSnapshot)
            .where(*conditions)
            # 排序值相等（多为 null）时按代码兜底，保证分页稳定
            .order_by(direction.nullslast(), AuctionSnapshot.code)
            .limit(limit)
        )
    )

    total = session.scalar(
        select(func.count())
        .select_from(AuctionSnapshot)
        .where(AuctionSnapshot.trade_date == trade_date)
    )
    # 竞价量比（自算）：需要前几个交易日的竞价量，所以按**返回的这批票**现算
    ratios = _auction_ratios(session, trade_date, rows)
    # 派生指标（波动率 / 真实换手 / 占比 / 排名 / 昨日筹码）
    metrics = _metrics(session, trade_date, rows)

    def to_row(row: AuctionSnapshot) -> AuctionRow:
        open_pct = (
            round((row.open / row.pre_close - 1) * 100, 2)
            if row.open is not None and row.pre_close
            else None
        )
        metric = metrics.get(row.code) or {}
        return AuctionRow(
            code=row.code,
            name=row.name,
            price=row.price,
            pre_close=row.pre_close,
            pct_chg=row.pct_chg,
            open=row.open,
            open_pct=open_pct,
            amount=row.amount,
            volume=row.volume,
            turnover=row.turnover,
            volume_ratio=row.volume_ratio,
            float_mv=row.float_mv,
            total_mv=row.total_mv,
            limit_up=row.limit_up,
            limit_down=row.limit_down,
            consecutive=boards.get(row.code),
            auction_ratio=ratios.get(row.code),
            amplitude=metric.get("amplitude"),
            free_float_mv=metric.get("free_float_mv"),
            free_turnover=metric.get("free_turnover"),
            amount_ratio=metric.get("amount_ratio"),
            rank=metric.get("rank"),
            industry_rank=metric.get("industry_rank"),
            prev_broken=bool(metric.get("prev_broken")),
            prev_turnover=metric.get("prev_turnover"),
            committee_diff=metric.get("committee_diff"),
            cancel_rate=metric.get("cancel_rate"),
            seal_ratio=metric.get("seal_ratio"),
        )

    return AuctionListOut(
        trade_date=trade_date,
        total=total or 0,
        shown=len(rows),
        rows=[to_row(row) for row in rows],
    )


@router.get("/auction/trend", response_model=AuctionTrendOut)
def auction_trend(
    trade_date: date | None = Query(None, alias="date"),
    session: Session = Depends(get_db),
) -> AuctionTrendOut:
    """全市场竞价量能走势：逐分钟把各票的「累计成交额」求和。

    参考图左上角那条「竞价走势」曲线。数据是**累计值**，所以曲线单调不降 ——
    要读的是它的**斜率**（哪一分钟在放量）。
    """
    if trade_date is None:
        trade_date = _latest_date(session)
        if trade_date is None:
            return AuctionTrendOut(trade_date=date.today(), points=[])

    totals: dict[str, float] = {}
    for samples in session.scalars(
        select(AuctionSnapshot.samples).where(AuctionSnapshot.trade_date == trade_date)
    ):
        for sample in samples or []:
            if len(sample) < 3 or sample[2] is None:
                continue
            key = str(sample[0])
            totals[key] = totals.get(key, 0.0) + float(sample[2])

    return AuctionTrendOut(
        trade_date=trade_date,
        points=[
            AuctionTrendPoint(time=key, amount=round(value, 2))
            for key, value in sorted(totals.items())
        ],
    )


@router.get("/auction/boards", response_model=AuctionBoardsOut)
def auction_boards(
    trade_date: date | None = Query(None, alias="date"),
    min_count: int = Query(3, ge=1, description="板块内至少几只才进榜"),
    limit: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_db),
) -> AuctionBoardsOut:
    """板块竞价强度：按**同花顺行业（最后一级）**聚合当日的竞价数据。

    回答的是「**这个方向整体强不强**」—— 个股高开但板块无动于衷，多半是孤票；
    个股高开 + 板块整体高开，才是资金共振。给四个数：只数 / 平均竞价涨幅 /
    总竞价额 / 竞价涨停家数。

    ⚠️ 分组用 `stock_basic.industry`（同花顺三级路径，取**最后一级**），与页面上
    「开盘红板块」不是一个体系 —— 后者只覆盖涨停股、没法给全市场分组。
    「竞价涨停」判据是**竞价价 ≥ 涨停价**（竞价即封），≠ 收盘涨停。
    """
    if trade_date is None:
        trade_date = _latest_date(session)
        if trade_date is None:
            return AuctionBoardsOut(trade_date=date.today(), items=[])

    industries = dict(session.execute(select(StockBasic.code, StockBasic.industry)).all())
    buckets: dict[str, dict] = {}
    for code, pct_chg, amount, price, limit_up in session.execute(
        select(
            AuctionSnapshot.code,
            AuctionSnapshot.pct_chg,
            AuctionSnapshot.amount,
            AuctionSnapshot.price,
            AuctionSnapshot.limit_up,
        ).where(AuctionSnapshot.trade_date == trade_date)
    ).all():
        industry = industries.get(code)
        if not industry:
            continue  # 没有行业归属的（新股 / 未进池）不进榜，不塞进「其他」
        name = industry.split("-")[-1]
        bucket = buckets.setdefault(
            name, {"count": 0, "pct_sum": 0.0, "pct_n": 0, "amount": 0.0, "limit": 0}
        )
        bucket["count"] += 1
        if pct_chg is not None:
            bucket["pct_sum"] += pct_chg
            bucket["pct_n"] += 1
        if amount:
            bucket["amount"] += amount
        if price is not None and limit_up and price >= limit_up:
            bucket["limit"] += 1

    items = [
        AuctionBoardItem(
            industry=name,
            count=bucket["count"],
            avg_pct=(
                round(bucket["pct_sum"] / bucket["pct_n"], 2) if bucket["pct_n"] else None
            ),
            amount=bucket["amount"] or None,
            limit_count=bucket["limit"],
        )
        for name, bucket in buckets.items()
        if bucket["count"] >= min_count
    ]
    # 平均涨幅降序；没有涨幅的排最后
    items.sort(key=lambda item: (item.avg_pct is None, -(item.avg_pct or 0.0), -item.count))
    return AuctionBoardsOut(trade_date=trade_date, items=items[:limit])
