"""估值分位接口：核心指数的 PE/PB 近 10 年分位，以及单指数的历史序列。

口径全部由 `services.valuation` 决定（窗口、中位秩、温度档），这里只做形状转换 ——
前端拿到的每个数字都要能在页面上标注来源与窗口，所以响应里带 `window_days`。
"""

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import (
    IndexValuationItem,
    ValuationPointOut,
    ValuationSeriesOut,
    ValuationSummaryOut,
)
from app.services import valuation

router = APIRouter(prefix="/api/valuation", tags=["估值"])


@router.get("", response_model=ValuationSummaryOut)
def summary(session: Session = Depends(get_db)) -> ValuationSummaryOut:
    """全部核心指数的当前估值 + 分位 + A 股估值温度。"""
    data = valuation.summary(session)
    return ValuationSummaryOut(
        as_of=data.as_of,
        window_days=data.window_days,
        market_pct=data.market_pct,
        market_tone=data.market_tone,
        items=[IndexValuationItem(**asdict(item)) for item in data.items],
    )


@router.get("/{code}", response_model=ValuationSeriesOut)
def one(code: str, session: Session = Depends(get_db)) -> ValuationSeriesOut:
    """单个指数的近 10 年 PE/PB 序列（给详情曲线用）。

    不认识的代码返回 404 —— 不要把「清单里没有」当成「这个指数没有数据」，
    两者在页面上要显示不同的文案。
    """
    data = valuation.series(session, code)
    if data is None:
        raise HTTPException(status_code=404, detail=f"估值清单里没有 {code}")
    return ValuationSeriesOut(
        code=data.code,
        name=data.name,
        window_days=data.window_days,
        points=[ValuationPointOut(**asdict(point)) for point in data.points],
    )
