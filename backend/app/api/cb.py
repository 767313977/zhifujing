"""可转债筛选 / 强赎雷达接口。

口径全部在 `services.convertible`（派生指标、预设、排序），这里只做参数校验与形状转换。
`filters` 会把**实际生效**的条件原样带回页面 —— 预设展开成什么、用户覆盖了什么，
页面必须能显示出来，否则「为什么只剩 6 只」只能靠猜。
"""

from dataclasses import asdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import CbRadarOut, CbRadarRowOut, CbScreenOut, CbScreenRowOut
from app.services import convertible

router = APIRouter(prefix="/api/cb", tags=["可转债"])

#: 单次最多返回多少行（页面默认 200；再多也没人看，还会拖慢首屏）
MAX_LIMIT = 500


@router.get("/screen", response_model=CbScreenOut)
def screen(
    preset: str = Query("t0", description="t0 / double_low / low_premium / bond_floor / revise"),
    price_min: float | None = Query(None, description="价格下限（元）"),
    price_max: float | None = Query(None, description="价格上限（元）"),
    premium_max: float | None = Query(None, description="转股溢价率上限（%）"),
    size_min: float | None = Query(None, description="剩余规模下限（亿）"),
    size_max: float | None = Query(None, description="剩余规模上限（亿）"),
    years_min: float | None = Query(None, description="剩余年限下限（年）"),
    years_max: float | None = Query(None, description="剩余年限上限（年）"),
    rating_min: str | None = Query(None, description="评级下限，如 AA-"),
    amount_min: float | None = Query(None, description="成交额下限（元）"),
    exclude_redeem: bool | None = Query(None, description="剔除已公告强赎"),
    exclude_st: bool | None = Query(None, description="剔除正股 ST"),
    exclude_not_convertible: bool | None = Query(None, description="剔除未进转股期"),
    sort: str | None = Query(None, description="score / double_low / premium / ytm / revise …"),
    limit: int = Query(200, ge=1, le=MAX_LIMIT),
    trade_date: date | None = Query(None, alias="date", description="缺省 = 最近一次快照"),
    session: Session = Depends(get_db),
) -> CbScreenOut:
    """按预设 + 覆盖条件筛一遍。不传日期 = 最近一次快照。"""
    if preset not in convertible.PRESETS:
        raise HTTPException(status_code=422, detail=f"未知预设：{preset}")
    data = convertible.screen(
        session,
        trade_date,
        preset=preset,
        price_min=price_min,
        price_max=price_max,
        premium_max=premium_max,
        size_min=size_min,
        size_max=size_max,
        years_min=years_min,
        years_max=years_max,
        rating_min=rating_min,
        amount_min=amount_min,
        exclude_redeem=exclude_redeem,
        exclude_st=exclude_st,
        exclude_not_convertible=exclude_not_convertible,
        sort=sort,
        limit=limit,
    )
    return CbScreenOut(
        as_of=data.as_of,
        filters=data.filters,
        total=data.total,
        items=[CbScreenRowOut(**asdict(row)) for row in data.items],
        source=data.source,
    )


@router.get("/radar", response_model=CbRadarOut)
def radar(
    trade_date: date | None = Query(None, alias="date", description="缺省 = 最近一次快照"),
    session: Session = Depends(get_db),
) -> CbRadarOut:
    """强赎雷达：已公告的（按最后交易日升序）+ 临近的 + 承诺不强赎的。

    这是防踩坑的一栏 —— 公告之后按约 100 元赎回，没转股的人几天里亏 20%~30%。
    """
    as_of, rows = convertible.radar(session, trade_date)
    return CbRadarOut(as_of=as_of, items=[CbRadarRowOut(**asdict(row)) for row in rows])
