"""选股池接口：RPS 相对强度榜、首板一进二候选 + 两者的次日验证。

候选池都是**盘后**口径（本站没有盘中数据），参数全部由 `services.pools` 决定；
响应里把实际使用的筛选条件原样带回去，页面照原样展示，避免「条件悄悄变了」。

## 验证接口为什么要缓存

`/track` 每次要把近 N 天×全市场日线过一遍（20 天约 10 万行），而且页面上的
参数一动就会重算。缓存键带上**全部条件参数 + 库里的最新交易日**，所以
「换参数」和「数据更新」都会自然失效，不存在读到隔夜旧数的可能。
"""

import threading
from dataclasses import asdict
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import LimitPool, RpsDaily
from app.schemas import (
    FirstBoardOut,
    FirstBoardRowOut,
    FirstBoardTrackDayOut,
    FirstBoardTrackOut,
    RpsOut,
    RpsRowOut,
    RpsTrackDayOut,
    RpsTrackOut,
    RpsTrackStatOut,
)
from app.services import pools

router = APIRouter(prefix="/api/pools", tags=["选股池"])

#: 验证结果的缓存时间。数据只会在收盘采集后变，10 分钟足够挡住「拖参数」的重复计算
_TRACK_TTL = timedelta(minutes=10)
_TRACK_CACHE_MAX = 128
_track_cache: dict[tuple, tuple[datetime, object]] = {}
#: FastAPI 的同步接口跑在线程池里，多个请求会并发写同一个 dict
_track_lock = threading.Lock()


def _cached(key: tuple, build):
    """按 `key` 缓存 `build()` 的结果（带 TTL 与上限）。

    键里必须包含**数据版本**（最新交易日），否则同一天里新采的数据读不到。
    """
    now = datetime.now()
    with _track_lock:
        hit = _track_cache.get(key)
        if hit is not None and now - hit[0] < _TRACK_TTL:
            return hit[1]
    value = build()
    with _track_lock:
        _track_cache[key] = (now, value)
        if len(_track_cache) > _TRACK_CACHE_MAX:
            oldest = min(_track_cache, key=lambda item: _track_cache[item][0])
            _track_cache.pop(oldest, None)
    return value


@router.get("/rps", response_model=RpsOut)
def rps(
    trade_date: date | None = Query(None, alias="date", description="缺省 = 库里最新 RPS 日"),
    min_rps: float = Query(0, ge=0, le=100, description="两档分位都要 ≥ 这个值"),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_db),
) -> RpsOut:
    """RPS 榜：按 250 日分位降序。"""
    board = pools.rps_board(session, trade_date, min_rps, limit)
    if board.trade_date is None:
        raise HTTPException(status_code=404, detail="还没有 RPS 数据（等收盘扫描跑一轮）")
    return RpsOut(
        trade_date=board.trade_date,
        rows=[RpsRowOut(**asdict(row)) for row in board.rows],
    )


@router.get("/first-board", response_model=FirstBoardOut)
def first_board(
    trade_date: date | None = Query(None, alias="date"),
    before: str = Query("140000", pattern=r"^\d{6}$", description="首封时间早于它（HHMMSS）"),
    min_float_mv: float = Query(20, ge=0, description="流通市值下限（亿）"),
    max_float_mv: float = Query(300, ge=0, description="流通市值上限（亿）"),
    min_amount: float = Query(3, ge=0, description="成交额下限（亿）"),
    min_themes: int = Query(3, ge=0, le=50, description="同题材涨停家数下限"),
    min_seal_ratio: float = Query(0, ge=0, le=100, description="封单额/流通市值 下限（%）"),
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_db),
) -> FirstBoardOut:
    """首板一进二候选（盘后）。"""
    if min_float_mv > max_float_mv:
        raise HTTPException(status_code=400, detail="流通市值下限不能大于上限")
    board = pools.first_board(
        session,
        trade_date=trade_date,
        before=before,
        min_float_mv=min_float_mv,
        max_float_mv=max_float_mv,
        min_amount=min_amount,
        min_themes=min_themes,
        min_seal_ratio=min_seal_ratio,
        limit=limit,
    )
    if board.trade_date is None:
        raise HTTPException(status_code=404, detail="还没有涨停池数据")
    return FirstBoardOut(
        trade_date=board.trade_date,
        conditions=board.conditions,
        rows=[FirstBoardRowOut(**asdict(row)) for row in board.rows],
    )


@router.get("/first-board/track", response_model=FirstBoardTrackOut)
def first_board_track(
    before: str = Query("140000", pattern=r"^\d{6}$", description="首封时间早于它（HHMMSS）"),
    min_float_mv: float = Query(20, ge=0, description="流通市值下限（亿）"),
    max_float_mv: float = Query(300, ge=0, description="流通市值上限（亿）"),
    min_amount: float = Query(3, ge=0, description="成交额下限（亿）"),
    min_themes: int = Query(3, ge=0, le=50, description="同题材涨停家数下限"),
    min_seal_ratio: float = Query(0, ge=0, le=100, description="封单额/流通市值 下限（%）"),
    days: int = Query(20, ge=1, le=60, description="回看多少个有涨停池的交易日"),
    session: Session = Depends(get_db),
) -> FirstBoardTrackOut:
    """近 `days` 天按当前条件选出的首板，次日的晋级 / 开盘溢价 / 收盘收益。

    参数与 `/first-board` 完全一致 —— 改这里的参数看汇总变化，就是定阈值的办法。
    """
    if min_float_mv > max_float_mv:
        raise HTTPException(status_code=400, detail="流通市值下限不能大于上限")
    latest = session.scalar(select(func.max(LimitPool.trade_date)))
    if latest is None:
        raise HTTPException(status_code=404, detail="还没有涨停池数据")
    key = (
        "first_board",
        latest,
        days,
        before,
        min_float_mv,
        max_float_mv,
        min_amount,
        min_themes,
        min_seal_ratio,
    )
    track = _cached(
        key,
        lambda: pools.first_board_track(
            session,
            days=days,
            before=before,
            min_float_mv=min_float_mv,
            max_float_mv=max_float_mv,
            min_amount=min_amount,
            min_themes=min_themes,
            min_seal_ratio=min_seal_ratio,
        ),
    )
    return FirstBoardTrackOut(
        trade_date=track.trade_date,
        conditions=track.conditions,
        days=[FirstBoardTrackDayOut(**asdict(row)) for row in track.days],
        **track.summary,
    )


@router.get("/rps/track", response_model=RpsTrackOut)
def rps_track(
    min_rps: float = Query(0, ge=0, le=100, description="两档分位都要 ≥ 这个值"),
    top: int = Query(50, ge=1, le=200, description="每天取榜单前多少只"),
    days: int = Query(20, ge=1, le=60, description="回看多少个有 RPS 记录的交易日"),
    session: Session = Depends(get_db),
) -> RpsTrackOut:
    """RPS 榜前 `top` 只的 1 / 5 日表现与超额（只统计真跑过 RPS 的日子）。"""
    latest = session.scalar(select(func.max(RpsDaily.trade_date)))
    if latest is None:
        raise HTTPException(status_code=404, detail="还没有 RPS 数据（等收盘扫描跑一轮）")
    key = ("rps", latest, days, min_rps, top)
    track = _cached(key, lambda: pools.rps_track(session, days, min_rps, top))
    return RpsTrackOut(
        trade_date=track.trade_date,
        days=track.summary.get("days", 0),
        verified_days=track.summary.get("verified_days", 0),
        samples=track.summary.get("samples", 0),
        ret1=RpsTrackStatOut(**track.summary["ret1"]),
        ret5=RpsTrackStatOut(**track.summary["ret5"]),
        rows=[RpsTrackDayOut(**asdict(row)) for row in track.days],
    )
