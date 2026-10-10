"""自选股接口。

**按用户隔离**（2026-09-28 起，见设计文档 §8.69）：主键是 `(user_id, code)`，
所以「取一只票」必须同时给用户 id —— 这里统一用 `session.get(Watchlist, (user.id, code))`。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.jobs.collect_daily import CollectionBusy, DailyCollector, collect_guard
from app.models import AppUser, StockBasic, StockDaily, Watchlist
from app.schemas import WatchlistIn, WatchlistRow
from app.services import auth
from app.services.auth import current_user
from app.services.stock_lookup import LookupError, resolve_code
from app.services.usage import QuotaLevel, level_label, quota_level
from app.sources.ifind import IfindError, normalize_code

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watchlist", tags=["自选股"])

# 「手动同步行情」的最小间隔。日常采集本来就会同步，手动这点只是「刚加完想立刻看到」
# 的补救 —— 而每次点都在花 iFinD 调用次数，所以要挡一下连点。
SYNC_COOLDOWN_SEC = 600
_sync_cooldown = auth.Cooldown(SYNC_COOLDOWN_SEC)

# 每个用户最多能放多少只自选。没有上限时，一个登录用户就能把全站 iFinD 配额
# 用「加自选 + 同步」这条链路烧光（加进来自动同步、每次同步都是一批调用），
# 而配额是全账号共享的 —— 个人使用的站，100 只足够复盘。
MAX_WATCHLIST = 100


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

    # 代码必须真在 `stock_basic` 里。`resolve_code` 对 6 位数字是**不查库直接放行**的
    # （它是通用解析器，改名/别处都要用，不该在这里收紧），所以「库里有没有这只票」
    # 只能在这一层兜住：不查的话，加一个 999999 之类的假代码进去，随后的同步就会
    # 拿它去问 iFinD —— 全是白花的配额（2026-10-11 加，见自选配额收紧）。
    if session.get(StockBasic, code) is None:
        raise HTTPException(
            status_code=400, detail=f"查不到这个代码「{code}」，请确认后重试"
        )

    # 条数上限（见 MAX_WATCHLIST）：先数再加。加自选会连带同步、而同步是花配额的，
    # 没有上限时一个账号就能靠这条链路把全站额度烧光。
    count = (
        session.scalar(
            select(func.count()).select_from(Watchlist).where(Watchlist.user_id == user.id)
        )
        or 0
    )
    if count >= MAX_WATCHLIST:
        raise HTTPException(
            status_code=400,
            detail=f"自选最多 {MAX_WATCHLIST} 只，你现在有 {count} 只，先删掉一些再加",
        )

    row = Watchlist(
        user_id=user.id,
        code=code,
        # 名字在**加入时**就落库（没传就用本地的简称）：只存代码的话，
        # 之后每次读自选都要回查 `stock_basic` 才显示得出名字
        name=payload.name or _known_name(session, code),
        note=payload.note,
    )

    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # 前端连点 / 双击时，两个并发请求都会在上面 `session.get` 那步看到「不存在」，
        # 于是都走到这里 —— 后者撞上 `(user_id, code)` 唯一约束。这**不是错误**：
        # 结果与串行路径完全一样（那一行已经在库里），所以按「已存在」返回，
        # 而不是把一个 500 丢给用户（2026-10-10 修）。
        session.rollback()
        existing = session.get(Watchlist, (user.id, code))
        if existing is None:
            # 理论上不会走到：唯一约束只可能被同一把主键挡下
            raise
        return _row(session, existing, _latest_quotes(session, [code]).get(code))
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


def _guard_quota() -> None:
    """手动同步的配额闸：配额紧张到 80% 就停手动同步。

    `PAUSE_KLINE`（已用 ≥80%）本来就要停「形态选股的全市场日线更新」，手动同步比它
    更非必需 —— 一起让路，把剩下的额度留给指数 / 情绪这些主线。日常采集仍会兜底刷新。
    """
    level = quota_level()
    if level >= QuotaLevel.PAUSE_KLINE:
        raise HTTPException(
            status_code=429,
            detail=f"iFinD 配额已用到 80%，暂停手动同步（{level_label(level)}）",
            headers={"Retry-After": "3600"},
        )


@router.post("/sync")
def sync_watchlist(
    user: AppUser = Depends(current_user),
    session: Session = Depends(get_db),
    days: int = Query(250, ge=5, le=250, description="回看的交易日数"),
) -> dict:
    """同步**自己**自选股的日线到本地缓存。

    首次加入自选时本地没有行情，需要跑一次；之后每交易日会自动刷新。

    ⚠️ 只同步自己的票、不跑全表：会员点一下就让服务器去同步全市场是没道理的，
    那也正是「谁的请求谁付账」这条最简单的分寸。全表的同步由每日采集负责。
    """
    _guard_quota()

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

    try:
        with collect_guard("自选股同步"):
            collector = DailyCollector()
            written = collector.sync_watchlist(days=days, codes=codes)
    except CollectionBusy as exc:
        # 采集正忙：这一步**根本没发起取数**，所以不占冷却 —— 否则白白要人等 10 分钟
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IfindError as exc:
        # 真去取过数了（配额已经花掉）→ 占用冷却；对外只说人话，细节进日志
        logger.warning("自选股同步失败（用户 %s）：%s", user.id, exc)
        _sync_cooldown.touch(f"user:{user.id}")
        raise HTTPException(status_code=400, detail="同步失败，请稍后再试") from exc
    _sync_cooldown.touch(f"user:{user.id}")
    return {"rows": written}
