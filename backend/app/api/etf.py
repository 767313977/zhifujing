"""ETF 筛选 / 行业强弱 / 单只详情接口（2026-10-31 加，ETF-3）。

口径全部在 `services.etf_screen`（复利涨幅、RPS、去重、标签），这里只做参数校验与
形状转换。`filters` 会把**实际生效**的条件原样带回页面 —— 预设展开成什么、用户
覆盖了什么，页面必须能显示出来（照 `/api/cb/screen` 的做法）。
"""

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import (
    EtfCategoryOut,
    EtfDetailOut,
    EtfPeerOut,
    EtfPointOut,
    EtfRowOut,
    EtfScreenOut,
)
from app.services import etf_screen
from app.sources.ifind import normalize_code

router = APIRouter(prefix="/api/etf", tags=["ETF"])

#: 单次最多返回多少行（去重后全市场 1500 上下，200 够看）
MAX_LIMIT = 500


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
