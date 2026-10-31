"""今日主线题材看板接口（FEAT-THEME-BOARD，2026-10-31）。

只读聚合（涨停池 + 开盘红精选板块 + 板块日线），口径见 `services.theme_board`。
不传日期 = 最近一个有涨停数据的日子。
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import ThemeBoardOut
from app.services import theme_board
from app.timeutil import today_cn

router = APIRouter(prefix="/api/themes", tags=["主线"])


@router.get("/board", response_model=ThemeBoardOut)
def board(
    trade_date: date | None = Query(None, alias="date", description="缺省 = 最近有涨停数据的一天"),
    session: Session = Depends(get_db),
) -> ThemeBoardOut:
    """今日主线 Top N + 龙头/中军/跟风 + 新晋/延续/退潮。

    没有涨停数据时返回空行与说明（200），只有未来日期 404。
    """
    if trade_date is not None and trade_date > today_cn():
        raise HTTPException(status_code=404, detail=f"{trade_date} 还没到，没有数据")
    return ThemeBoardOut(**theme_board.board(session, trade_date))
