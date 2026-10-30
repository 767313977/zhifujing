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
from app.schemas import (
    CbDetailOut,
    CbRadarOut,
    CbRadarRowOut,
    CbScreenOut,
    CbScreenRowOut,
    CbStockBondOut,
)
from app.services import convertible
from app.sources.ifind import normalize_code

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


@router.get("/by-stock/{stock_code}", response_model=list[CbStockBondOut])
def by_stock(
    stock_code: str,
    session: Session = Depends(get_db),
) -> list[CbStockBondOut]:
    """某只正股**存续**的转债（个股详情页的小卡片）。

    只读最近一次盘后快照；不联网。没有转债就返回空数组（页面据此不渲染卡片）。
    """
    code = normalize_code(stock_code)
    if len(code) != 6:
        raise HTTPException(status_code=400, detail=f"正股代码应为 6 位数字：{stock_code}")
    return [
        CbStockBondOut(**asdict(row))
        for row in convertible.bonds_of_stock(session, code)
    ]


# ⚠️ 这条**必须放在 /screen /radar /by-stock 之后**：`/api/cb/{bond_code}` 只是一段路径，
# 注册在静态路径之前就会把 `/api/cb/screen` 这类请求吃掉。FastAPI 按注册顺序匹配。
@router.get("/{bond_code}", response_model=CbDetailOut)
def detail(
    bond_code: str,
    days: int = Query(250, ge=5, le=1000, description="走势取多少个交易日"),
    trade_date: date | None = Query(None, alias="date", description="缺省 = 该券最近一份快照"),
    session: Session = Depends(get_db),
) -> CbDetailOut:
    """单券详情：条款摘要 + 最近一份快照 + 走势序列。纯读库，不联网。"""
    code = normalize_code(bond_code)
    if len(code) != 6:
        raise HTTPException(status_code=400, detail=f"转债代码应为 6 位数字：{bond_code}")
    data = convertible.detail(session, code, trade_date, days)
    if data is None:
        raise HTTPException(status_code=404, detail=f"没有这只转债：{code}")
    return CbDetailOut(**asdict(data))
