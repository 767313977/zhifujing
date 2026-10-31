"""ETF 筛选 / 行业强弱 / 单只详情接口（2026-10-31 加，ETF-3）。

口径全部在 `services.etf_screen`（复利涨幅、RPS、去重、标签），这里只做参数校验与
形状转换。`filters` 会把**实际生效**的条件原样带回页面 —— 预设展开成什么、用户
覆盖了什么，页面必须能显示出来（照 `/api/cb/screen` 的做法）。
"""

import threading
from dataclasses import asdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import EtfBar, EtfShare, StockBasic
from app.schemas import (
    EtfCategoryOut,
    EtfDetailOut,
    EtfPeerOut,
    EtfPointOut,
    EtfRelatedOut,
    EtfRowOut,
    EtfScreenOut,
    EtfTrackCurveOut,
    EtfTrackDayOut,
    EtfTrackOut,
    EtfTrackPickOut,
    EtfTrackStatOut,
    EtfTrackStatsOut,
)
from app.services import etf_category, etf_screen, etf_track
from app.sources.ifind import normalize_code

router = APIRouter(prefix="/api/etf", tags=["ETF"])

#: 单次最多返回多少行（去重后全市场 1500 上下，200 够看）
MAX_LIMIT = 500

#: 验证结果的缓存（照 `api/pools.py` 的 `_track_cache`）：键里带**最新日线日 +
#: 最新份额日**，所以换参数会重算、数据更新会自动失效，不会读到隔夜旧数。
_TRACK_TTL = timedelta(minutes=10)
_TRACK_CACHE_MAX = 64
_track_cache: dict[tuple, tuple[datetime, object]] = {}
_track_lock = threading.Lock()


def _cached(key: tuple, build):
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


@router.get("/screen", response_model=EtfScreenOut)
def screen(
    preset: str = Query("momentum", description="momentum / breakout / inflow / defensive / t0"),
    scale_min: float | None = Query(None, description="规模下限（亿）"),
    amount_min: float | None = Query(None, description="20 日均额下限（元）"),
    category: str | None = Query(None, description="分类，逗号分隔"),
    dedup: bool | None = Query(None, description="同指数去重（默认开）"),
    exclude_money: bool | None = Query(None, description="剔除货币现金"),
    exclude_bond: bool | None = Query(None, description="剔除债券"),
    sort: str | None = Query(None, description="rps60 / ret60 / amount / flow20_ratio / volatility …"),
    limit: int = Query(200, ge=1, le=MAX_LIMIT),
    trade_date: str | None = Query(None, alias="date", description="缺省 = 最近一份日线"),
    session: Session = Depends(get_db),
) -> EtfScreenOut:
    """按预设 + 覆盖条件筛一遍。不传日期 = 最近一份 ETF 日线。"""
    if preset not in etf_screen.PRESETS:
        raise HTTPException(status_code=422, detail=f"未知预设：{preset}")
    day = None
    if trade_date:
        try:
            from datetime import date as _date

            day = _date.fromisoformat(trade_date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="日期格式应为 YYYY-MM-DD") from exc
    categories = tuple(item for item in (category or "").split(",") if item) or None
    data = etf_screen.screen(
        session,
        day,
        preset=preset,
        scale_min=scale_min,
        amount_min=amount_min,
        categories=categories,
        dedup=dedup,
        exclude_money=exclude_money,
        exclude_bond=exclude_bond,
        sort=sort,
        limit=limit,
    )
    return EtfScreenOut(
        as_of=data.as_of,
        filters=data.filters,
        total=data.total,
        items=[EtfRowOut(**asdict(row)) for row in data.items],
    )


@router.get("/categories", response_model=list[EtfCategoryOut])
def categories(
    trade_date: str | None = Query(None, alias="date", description="缺省 = 最近一份日线"),
    session: Session = Depends(get_db),
) -> list[EtfCategoryOut]:
    """行业强弱一览：每类的代表 ETF 的 RPS60 中位数 + 20 日净申赎合计。"""
    from datetime import date as _date

    day = None
    if trade_date:
        try:
            day = _date.fromisoformat(trade_date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="日期格式应为 YYYY-MM-DD") from exc
    return [EtfCategoryOut(**row) for row in etf_screen.categories(session, day)]


@router.get("/track", response_model=EtfTrackOut)
def track(
    preset: str = Query("momentum", description="momentum / breakout / inflow / defensive / t0"),
    top: int = Query(5, ge=1, le=etf_track.MAX_TOP, description="每天取前几只"),
    hold: int = Query(5, ge=1, le=60, description="持有几个交易日（次日收盘起算）"),
    days: int = Query(etf_track.DEFAULT_DAYS, ge=20, le=etf_track.MAX_DAYS),
    session: Session = Depends(get_db),
) -> EtfTrackOut:
    """次日验证：近 `days` 天每天按当前预设选前 `top` 只，次日收盘起持有 `hold` 天。

    对比 510300 与「去重后全体等权」，收益单位 %、累计是逐日复利。只用当天及
    之前的数据重放历史筛选（信号日 `t` 收盘出名单、`t+1` 收盘才有成交价）。
    """
    if preset not in etf_screen.PRESETS:
        raise HTTPException(status_code=422, detail=f"未知预设：{preset}")
    bar_day = session.scalar(select(func.max(EtfBar.trade_date)))
    share_day = session.scalar(select(func.max(EtfShare.trade_date)))
    if bar_day is None:
        raise HTTPException(status_code=404, detail="还没有 ETF 日线")
    key = ("etf_track", bar_day, share_day, preset, top, hold, days)
    data = _cached(
        key, lambda: etf_track.track(session, preset=preset, top=top, hold=hold, days=days)
    )
    rows: list[EtfTrackDayOut] = []
    for row in data.rows:
        excess_hs300 = (
            round(row.ret - row.hs300_ret, 2)
            if row.ret is not None and row.hs300_ret is not None
            else None
        )
        excess_universe = (
            round(row.ret - row.universe_ret, 2)
            if row.ret is not None and row.universe_ret is not None
            else None
        )
        rows.append(
            EtfTrackDayOut(
                trade_date=row.trade_date,
                entry_date=row.entry_date,
                exit_date=row.exit_date,
                ret=row.ret,
                hs300_ret=row.hs300_ret,
                universe_ret=row.universe_ret,
                excess_hs300=excess_hs300,
                excess_universe=excess_universe,
                picks=[
                    EtfTrackPickOut(code=pick.code, name=pick.name, ret=pick.ret)
                    for pick in row.picks
                ],
            )
        )
    return EtfTrackOut(
        preset=data.preset,
        top=data.top,
        hold=data.hold,
        days=data.days,
        as_of=data.as_of,
        note=data.note,
        stats=EtfTrackStatsOut(
            strategy=EtfTrackStatOut(**data.stats["strategy"]),
            hs300=EtfTrackStatOut(**data.stats["hs300"]),
            universe=EtfTrackStatOut(**data.stats["universe"]),
        ),
        curve=[EtfTrackCurveOut(**point) for point in data.curve],
        rows=rows,
    )


@router.get("/related", response_model=EtfRelatedOut)
def related(
    code: str | None = Query(None, description="股票代码：按它的同花顺行业归类"),
    text: str | None = Query(None, description="行业 / 板块名（按 ETF 名称词典归类）"),
    limit: int = Query(5, ge=1, le=20),
    session: Session = Depends(get_db),
) -> EtfRelatedOut:
    """个股 / 板块 → 相关 ETF 的代表只（ETF-5）。

    个股走**一级行业表**（`etf_category.classify_industry`），板块 / 行业名走
    **ETF 名称词典**（`classify`）；两者不是一套词表 —— 用名称词典套行业路径会把
    「电力设备」猜成电力公用（2026-10-31 线上实测）。页面必须把 `category` 显示
    出来，别让人以为这是严格的行业映射。
    """
    industry: str | None = None
    category = etf_category.UNKNOWN
    if code:
        basic = session.get(StockBasic, normalize_code(code))
        if basic and basic.industry:
            industry = basic.industry
            category = etf_category.classify_industry(industry)
    if industry is None:
        industry = (text or "").strip() or None
        if not industry:
            raise HTTPException(
                status_code=422, detail="要给 code（股票代码）或 text（行业 / 板块名）"
            )
        category = etf_category.classify(industry)
    if category == etf_category.UNKNOWN:
        return EtfRelatedOut(industry=industry, category=category, items=[])
    data = etf_screen.screen(
        session,
        None,
        preset="momentum",
        fund_type="",
        scale_min=0,
        amount_min=0,
        above_ma20=False,
        dedup=True,
        limit=0,
    )
    items = [row for row in data.items if row.category == category]
    # 代表只：规模大的在前，规模缺的排最后，再看 20 日均额
    items.sort(key=lambda row: (row.scale is None, -(row.scale or 0), -(row.avg_amount or 0)))
    return EtfRelatedOut(
        industry=industry,
        category=category,
        items=[EtfRowOut(**asdict(row)) for row in items[:limit]],
    )


@router.get("/{code}", response_model=EtfDetailOut)
def detail(
    code: str,
    trade_date: str | None = Query(None, alias="date", description="缺省 = 最近一份日线"),
    session: Session = Depends(get_db),
) -> EtfDetailOut:
    """单只 ETF：最新一行指标 + 250 日走势（价格 + 份额）+ 同类列表。"""
    from datetime import date as _date

    plain = normalize_code(code)
    day = None
    if trade_date:
        try:
            day = _date.fromisoformat(trade_date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="日期格式应为 YYYY-MM-DD") from exc
    data = etf_screen.detail(session, plain, day)
    if data is None:
        raise HTTPException(status_code=404, detail=f"没有这只 ETF：{plain}")
    return EtfDetailOut(
        as_of=data.as_of,
        row=EtfRowOut(**asdict(data.row)),
        series=[EtfPointOut(**asdict(point)) for point in data.series],
        peers=[EtfPeerOut(**peer) for peer in data.peers],
    )
