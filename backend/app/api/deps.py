"""路由公共依赖。"""

from datetime import date

from fastapi import Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import MarketSentiment, TradeCalendar


def resolve_trade_date(
    trade_date: date | None = Query(
        None,
        alias="date",
        description="交易日，缺省取库中最新有情绪数据的一天",
    ),
    session: Session = Depends(get_db),
) -> date:
    """解析要查询的交易日。

    缺省取库中最新日期而非系统当天：周末或盘前访问时当天没有数据，
    回落到系统当天会让页面空白。

    **显式传 `date` 时必须是交易日**（2026-10-23 体检修）：以前不管传什么都直接放行，
    于是 `/api/market/overview?date=1900-01-01`、非交易日、未来日期都能拿到 200 空壳，
    与"库里没有数据就 404"的既有语义不一致，页面会显示成"有页面但没数据"。
    不传时行为完全不变（取库中最新情绪日；空库仍 404）。
    """
    if trade_date is not None:
        exists = session.scalar(
            select(TradeCalendar.trade_date).where(
                TradeCalendar.trade_date == trade_date
            )
        )
        if exists is None:
            raise HTTPException(
                status_code=404,
                detail=f"{trade_date} 不是交易日或暂无数据",
            )
        return trade_date

    latest = session.scalar(
        select(MarketSentiment.trade_date)
        .order_by(MarketSentiment.trade_date.desc())
        .limit(1)
    )
    if latest is None:
        raise HTTPException(status_code=404, detail="暂无数据，请先在「数据管理」中执行采集")
    return latest
