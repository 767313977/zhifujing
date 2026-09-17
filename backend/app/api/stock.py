"""个股详情接口。

数据来自本地缓存的日线（`stock_daily`）。本站不做全市场落库（会触发东财
频控），所以只有**自选股与主动看过的个股**会被缓存 —— 首次打开某只股票时
前端会自动调一次同步，之后走缓存。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.jobs.collect_daily import (
    STOCK_FULL_DAYS,
    CollectionBusy,
    DailyCollector,
    collect_guard,
)
from app.models import Lhb, LimitPool, StockBasic, StockDaily, Watchlist
from app.schemas import StockDailyRow, StockProfile
from app.sources.ifind import IfindError, normalize_code

router = APIRouter(prefix="/api/stock", tags=["个股"])


def _code(raw: str) -> str:
    code = normalize_code(raw)
    if len(code) != 6:
        raise HTTPException(
            status_code=400, detail=f"股票代码应为 6 位数字，收到「{raw}」"
        )
    return code


@router.get("/{code}", response_model=StockProfile)
def profile(code: str, session: Session = Depends(get_db)) -> StockProfile:
    code = _code(code)
    item = session.get(Watchlist, code)
    basic = session.get(StockBasic, code)
    latest = session.scalars(
        select(StockDaily)
        .where(StockDaily.code == code)
        .order_by(StockDaily.trade_date.desc())
        .limit(1)
    ).first()
    count, first_date, last_date = session.execute(
        select(
            func.count(),
            func.min(StockDaily.trade_date),
            func.max(StockDaily.trade_date),
        ).where(StockDaily.code == code)
    ).one()

    # 把该股与复盘数据关联起来：涨停过哪天、上过几次龙虎榜
    limit_up_dates = list(
        session.scalars(
            select(LimitPool.trade_date)
            .where(LimitPool.code == code, LimitPool.pool_type == "up")
            .order_by(LimitPool.trade_date.desc())
        )
    )
    lhb_count = (
        session.scalar(select(func.count()).select_from(Lhb).where(Lhb.code == code)) or 0
    )

    name = None
    for candidate in (item.name if item else None, basic.name if basic else None,
                      latest.name if latest else None):
        if candidate:
            name = candidate
            break

    return StockProfile(
        code=code,
        name=name,
        in_watchlist=item is not None,
        note=item.note if item else None,
        latest=StockDailyRow.model_validate(latest) if latest else None,
        day_count=count or 0,
        first_date=first_date,
        last_date=last_date,
        limit_up_dates=limit_up_dates,
        lhb_count=lhb_count,
    )


@router.get("/{code}/daily", response_model=list[StockDailyRow])
def daily(
    code: str,
    days: int = Query(120, ge=5, le=500, description="返回最近 N 个交易日，按日期升序"),
    session: Session = Depends(get_db),
) -> list[StockDailyRow]:
    rows = list(
        session.scalars(
            select(StockDaily)
            .where(StockDaily.code == _code(code))
            .order_by(StockDaily.trade_date.desc())
            .limit(days)
        )
    )
    return [StockDailyRow.model_validate(row) for row in reversed(rows)]


@router.post("/{code}/sync")
def sync(
    code: str,
    days: int = Query(STOCK_FULL_DAYS, ge=5, le=500, description="回看的交易日数"),
) -> dict:
    """同步该股日线到本地缓存。首次打开个股页时调用。"""
    try:
        with collect_guard("个股同步"):
            written = DailyCollector().sync_stock(_code(code), days=days)
    except CollectionBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IfindError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"rows": written}
