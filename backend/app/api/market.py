"""大盘与情绪接口。"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import resolve_trade_date
from app.db import get_db
from app.models import IndexDaily, MarketSentiment
from app.schemas import IndexHistory, IndexQuote, IndexSeries, MarketOverview, SentimentOut

router = APIRouter(prefix="/api/market", tags=["复盘"])

# 图表展示顺序：大盘指数在前
INDEX_ORDER = ["000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "000852.SH"]


@router.get("/overview", response_model=MarketOverview)
def overview(
    trade_date: date = Depends(resolve_trade_date),
    session: Session = Depends(get_db),
) -> MarketOverview:
    """今日复盘首页数据：指数卡片 + 情绪指标。"""
    indexes = list(
        session.scalars(
            select(IndexDaily)
            .where(IndexDaily.trade_date == trade_date)
            .order_by(IndexDaily.code)
        )
    )
    sentiment = session.scalars(
        select(MarketSentiment).where(MarketSentiment.trade_date == trade_date)
    ).first()
    return MarketOverview(
        trade_date=trade_date,
        indexes=[IndexQuote.model_validate(row) for row in indexes],
        sentiment=SentimentOut.model_validate(sentiment) if sentiment else None,
    )


@router.get("/sentiment", response_model=list[SentimentOut])
def sentiment_series(
    days: int = Query(60, ge=1, le=250, description="返回最近 N 个交易日，按日期升序"),
    session: Session = Depends(get_db),
) -> list[SentimentOut]:
    """情绪周期曲线数据。"""
    rows = list(
        session.scalars(
            select(MarketSentiment)
            .order_by(MarketSentiment.trade_date.desc())
            .limit(days)
        )
    )
    return [SentimentOut.model_validate(row) for row in reversed(rows)]


@router.get("/dates", response_model=list[date])
def available_dates(
    days: int = Query(60, ge=1, le=250),
    session: Session = Depends(get_db),
) -> list[date]:
    """库中已有数据的交易日，供前端日期切换。"""
    rows = list(
        session.scalars(
            select(MarketSentiment.trade_date)
            .order_by(MarketSentiment.trade_date.desc())
            .limit(days)
        )
    )
    return list(reversed(rows))


@router.get("/index-history", response_model=IndexHistory)
def index_history(
    days: int = Query(120, ge=5, le=500, description="返回最近 N 个有指数数据的交易日"),
    session: Session = Depends(get_db),
) -> IndexHistory:
    """指数历史序列，按日期升序对齐。

    用途是给短周期的情绪指标提供**长周期背景**：三池数据源只保留最近
    15 个交易日，而指数有完整历史，所以两者分开成图而不是画在同一时间轴上。
    """
    recent = list(
        session.scalars(
            select(IndexDaily.trade_date)
            .distinct()
            .order_by(IndexDaily.trade_date.desc())
            .limit(days)
        )
    )
    if not recent:
        raise HTTPException(status_code=404, detail="暂无指数数据，请先执行采集或回补")

    dates = sorted(recent)
    position = {day: i for i, day in enumerate(dates)}

    rows = list(
        session.scalars(
            select(IndexDaily)
            .where(
                IndexDaily.trade_date >= dates[0],
                IndexDaily.trade_date <= dates[-1],
            )
            .order_by(IndexDaily.code, IndexDaily.trade_date)
        )
    )

    # 按日期位置填充，缺失留 None，保证各数组与 dates 严格等长
    buckets: dict[str, dict] = {}
    for row in rows:
        bucket = buckets.setdefault(
            row.code,
            {
                "name": row.name,
                "close": [None] * len(dates),
                "pct_chg": [None] * len(dates),
                "amount": [None] * len(dates),
            },
        )
        idx = position[row.trade_date]
        bucket["close"][idx] = row.close
        bucket["pct_chg"][idx] = row.pct_chg
        bucket["amount"][idx] = row.amount

    order = {code: i for i, code in enumerate(INDEX_ORDER)}
    series = [
        IndexSeries(code=code, name=data["name"], **{k: data[k] for k in ("close", "pct_chg", "amount")})
        for code, data in sorted(
            buckets.items(), key=lambda item: order.get(item[0], len(INDEX_ORDER))
        )
    ]
    return IndexHistory(dates=dates, series=series)
