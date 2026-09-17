"""自选股接口。"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.jobs.collect_daily import CollectionBusy, DailyCollector, collect_guard
from app.models import StockBasic, StockDaily, Watchlist
from app.schemas import WatchlistIn, WatchlistRow
from app.sources.ifind import IfindError, normalize_code

router = APIRouter(prefix="/api/watchlist", tags=["自选股"])


def _normalize_code(raw: str) -> str:
    """统一代码写法并校验长度。"""
    code = normalize_code(raw)
    if len(code) != 6:
        raise HTTPException(
            status_code=400, detail=f"股票代码应为 6 位数字，收到「{raw}」"
        )
    return code


def _latest_quotes(session: Session, codes: list[str]) -> dict[str, StockDaily]:
    """每个代码取最新一条本地缓存的日线。

    取的是缓存而不是实时行情：页面因此不必联网、打开即读。
    实时性由采集任务与页面的「同步行情」按钮保证。
    """
    if not codes:
        return {}
    newest = (
        select(StockDaily.code, func.max(StockDaily.trade_date).label("latest"))
        .where(StockDaily.code.in_(codes))
        .group_by(StockDaily.code)
        .subquery()
    )
    rows = session.scalars(
        select(StockDaily).join(
            newest,
            (StockDaily.code == newest.c.code)
            & (StockDaily.trade_date == newest.c.latest),
        )
    )
    return {row.code: row for row in rows}


def _row(session: Session, item: Watchlist, quote: StockDaily | None) -> WatchlistRow:
    if not item.name:
        basic = session.get(StockBasic, item.code)
        if basic and basic.name:
            item.name = basic.name
    return WatchlistRow(
        code=item.code,
        name=item.name,
        tags=item.tags,
        note=item.note,
        added_at=item.added_at,
        latest_date=quote.trade_date if quote else None,
        close=quote.close if quote else None,
        pct_chg=quote.pct_chg if quote else None,
    )


@router.get("", response_model=list[WatchlistRow])
def list_watchlist(session: Session = Depends(get_db)) -> list[WatchlistRow]:
    items = list(session.scalars(select(Watchlist).order_by(Watchlist.added_at.desc())))
    quotes = _latest_quotes(session, [item.code for item in items])
    return [_row(session, item, quotes.get(item.code)) for item in items]


@router.post("", response_model=WatchlistRow)
def add_watchlist(
    payload: WatchlistIn, session: Session = Depends(get_db)
) -> WatchlistRow:
    code = _normalize_code(payload.code)

    existing = session.get(Watchlist, code)
    if existing is not None:
        # 重复加入当成功处理，前端连点不会报错
        return _row(session, existing, _latest_quotes(session, [code]).get(code))

    row = Watchlist(code=code, name=payload.name, note=payload.note)
    session.add(row)
    session.commit()
    session.refresh(row)
    return _row(session, row, None)


@router.patch("/{code}", response_model=WatchlistRow)
def update_watchlist(
    code: str,
    payload: WatchlistIn,
    session: Session = Depends(get_db),
) -> WatchlistRow:
    """更新备注或名称。"""
    row = session.get(Watchlist, _normalize_code(code))
    if row is None:
        raise HTTPException(status_code=404, detail="该股票不在自选里")
    if payload.note is not None:
        row.note = payload.note
    if payload.name:
        row.name = payload.name
    session.commit()
    session.refresh(row)
    return _row(session, row, _latest_quotes(session, [row.code]).get(row.code))


@router.delete("/{code}")
def remove_watchlist(code: str, session: Session = Depends(get_db)) -> dict:
    row = session.get(Watchlist, _normalize_code(code))
    if row is None:
        raise HTTPException(status_code=404, detail="该股票不在自选里")
    session.delete(row)
    session.commit()
    return {"ok": True}


@router.post("/sync")
def sync_watchlist(
    days: int = Query(250, ge=5, le=500, description="回看的交易日数"),
) -> dict:
    """同步全部自选股的日线到本地缓存。

    首次加入自选时本地没有行情，需要跑一次；之后每交易日会自动刷新。
    """
    try:
        with collect_guard("自选股同步"):
            collector = DailyCollector()
            written = collector.sync_watchlist(days=days)
    except CollectionBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IfindError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"rows": written}
