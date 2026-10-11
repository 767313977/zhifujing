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
from app.models import AuctionSnapshot, LimitPool, TradeCalendar
from app.schemas import AuctionListOut, AuctionRow, AuctionTrendOut, AuctionTrendPoint

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

    def to_row(row: AuctionSnapshot) -> AuctionRow:
        open_pct = (
            round((row.open / row.pre_close - 1) * 100, 2)
            if row.open is not None and row.pre_close
            else None
        )
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
