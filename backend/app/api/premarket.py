"""盘前 / 开盘页接口（FEAT-PREMARKET，2026-10-31）。

只有一个只读聚合：**不发任何外部请求**（竞价数据是 9:15–9:31 采样落的库，见
`jobs/collect_auction.py`）。页面默认看今天；传 `date=` 可回看历史交易日。
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser
from app.schemas import PremarketSummaryOut
from app.services import premarket
from app.services.auth import current_user

router = APIRouter(prefix="/api/premarket", tags=["盘前"])


@router.get("/summary", response_model=PremarketSummaryOut)
def summary(
    trade_date: date | None = Query(None, alias="date", description="缺省 = 今天（北京日期）"),
    user: AppUser = Depends(current_user),
    session: Session = Depends(get_db),
) -> PremarketSummaryOut:
    """盘前聚合：昨日涨停今开、悟道名单开盘位置、自选摘要、竞价新鲜度。

    - 非交易日照常返回（`is_trade_day=false` + 说明文案），不 404；
    - 竞价没采到照常返回（`auction_available=false`，竞价字段全 null）；
    - 只有「未来日期」返回 404 —— 与其它按日接口的语义一致。
    """
    try:
        data = premarket.summary(session, trade_date, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PremarketSummaryOut(**data)
