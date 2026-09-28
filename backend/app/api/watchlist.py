"""自选股接口。

**按用户隔离**（2026-09-28 起，见设计文档 §8.69）：主键是 `(user_id, code)`，
所以「取一只票」必须同时给用户 id —— 这里统一用 `session.get(Watchlist, (user.id, code))`。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.jobs.collect_daily import CollectionBusy, DailyCollector, collect_guard
from app.models import AppUser, StockBasic, StockDaily, Watchlist
from app.schemas import WatchlistIn, WatchlistRow
from app.services import auth
from app.services.auth import current_user
from app.services.stock_lookup import LookupError, resolve_code
from app.sources.ifind import IfindError, normalize_code

router = APIRouter(prefix="/api/watchlist", tags=["自选股"])

# 「手动同步行情」的最小间隔。日常采集本来就会同步，手动这点只是「刚加完想立刻看到」
# 的补救 —— 而每次点都在花 iFinD 调用次数，所以要挡一下连点。
SYNC_COOLDOWN_SEC = 600
_sync_cooldown = auth.Cooldown(SYNC_COOLDOWN_SEC)


def _normalize_code(raw: str) -> str:
    """统一代码写法并校验长度。**路径参数**用（那里只可能是代码）。"""
    code = normalize_code(raw)
    if len(code) != 6:
        raise HTTPException(
            status_code=400, detail=f"股票代码应为 6 位数字，收到「{raw}」"
        )
    return code


def _resolve(raw: str) -> str:
    """加入自选时把输入解析成代码：**代码 / 名称 / 拼音首字母**都收。

    路径参数（改备注、删除）不走这里 —— 那里前端给的一定是代码，多一层解析
    反而会把「000001」这种真代码绕进名称索引。
    """
    try:
        return resolve_code(raw)
    except LookupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


def _known_name(session: Session, code: str) -> str | None:
    """本地已知的股票简称（`stock_basic`）。没有就返回 None。

    加自选时用它把名字**落库**：不落的话 `watchlist.name` 永远是 None，
    每次列表都要回查 `stock_basic` 才显示得出名字（`_row` 的兜底，且那条路不写库）。
    刚加自选时本地可能还没有这只票（要等一次日线同步才有），那就留空。
    """
    basic = session.get(StockBasic, code)
    return basic.name if basic and basic.name else None


def _row(session: Session, item: Watchlist, quote: StockDaily | None) -> WatchlistRow:
    # 兜底：老数据里 `name` 可能是空的（2026-09-27 之前加自选时不落名字）。
    # 这里只补到内存对象上供本次响应使用，**不写库** —— 读路径不做写操作
    if not item.name:
        item.name = _known_name(session, item.code)
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
def list_watchlist(
    user: AppUser = Depends(current_user), session: Session = Depends(get_db)
) -> list[WatchlistRow]:
    items = list(
        session.scalars(
            select(Watchlist)
            .where(Watchlist.user_id == user.id)
            .order_by(Watchlist.added_at.desc())
        )
    )
    quotes = _latest_quotes(session, [item.code for item in items])
    return [_row(session, item, quotes.get(item.code)) for item in items]


@router.post("", response_model=WatchlistRow)
def add_watchlist(
    payload: WatchlistIn,
    user: AppUser = Depends(current_user),
    session: Session = Depends(get_db),
) -> WatchlistRow:
    """加入自选。`code` 字段收代码、股票名称或拼音首字母（见 `_resolve`）。"""
    code = _resolve(payload.code)

    existing = session.get(Watchlist, (user.id, code))
    if existing is not None:
        # 重复加入当成功处理，前端连点不会报错
        return _row(session, existing, _latest_quotes(session, [code]).get(code))

    row = Watchlist(
        user_id=user.id,
        code=code,
        # 名字在**加入时**就落库（没传就用本地的简称）：只存代码的话，
        # 之后每次读自选都要回查 `stock_basic` 才显示得出名字
        name=payload.name or _known_name(session, code),
        note=payload.note,
    )

    session.add(row)
    session.commit()
    session.refresh(row)
    return _row(session, row, None)


@router.patch("/{code}", response_model=WatchlistRow)
def update_watchlist(
    code: str,
    payload: WatchlistIn,
    user: AppUser = Depends(current_user),
    session: Session = Depends(get_db),
) -> WatchlistRow:
    """更新备注或名称。只能改自己的 —— 别人的票在这里取不到，直接 404。"""
    row = session.get(Watchlist, (user.id, _normalize_code(code)))
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
def remove_watchlist(
    code: str,
    user: AppUser = Depends(current_user),
    session: Session = Depends(get_db),
) -> dict:
    row = session.get(Watchlist, (user.id, _normalize_code(code)))
    if row is None:
        raise HTTPException(status_code=404, detail="该股票不在自选里")
    session.delete(row)
    session.commit()
    return {"ok": True}


@router.post("/sync")
def sync_watchlist(
    user: AppUser = Depends(current_user),
    session: Session = Depends(get_db),
    days: int = Query(250, ge=5, le=500, description="回看的交易日数"),
) -> dict:
    """同步**自己**自选股的日线到本地缓存。

    首次加入自选时本地没有行情，需要跑一次；之后每交易日会自动刷新。

    ⚠️ 只同步自己的票、不跑全表：会员点一下就让服务器去同步全市场是没道理的，
    那也正是「谁的请求谁付账」这条最简单的分寸。全表的同步由每日采集负责。
    """
    wait = _sync_cooldown.remaining(f"user:{user.id}")
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail=f"刚同步过，请 {int(wait) + 1} 秒后再试",
            headers={"Retry-After": str(int(wait) + 1)},
        )
    codes = list(
        session.scalars(select(Watchlist.code).where(Watchlist.user_id == user.id))
    )
    if not codes:
        return {"rows": 0}

    _sync_cooldown.touch(f"user:{user.id}")
    try:
        with collect_guard("自选股同步"):
            collector = DailyCollector()
            written = collector.sync_watchlist(days=days, codes=codes)
    except CollectionBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IfindError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"rows": written}
