"""路由公共依赖。"""

from datetime import date

from fastapi import Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import MarketSentiment


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
    """
    if trade_date is not None:
        return trade_date

    latest = session.scalar(
        select(MarketSentiment.trade_date)
        .order_by(MarketSentiment.trade_date.desc())
        .limit(1)
    )
    if latest is None:
        raise HTTPException(status_code=404, detail="暂无数据，请先在「数据管理」中执行采集")
    return latest
