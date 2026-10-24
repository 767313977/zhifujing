"""选股池接口：RPS 相对强度榜、首板一进二候选。

两者都是**盘后候选池**（本站没有盘中数据），口径全部由 `services.pools` 决定；
响应里把实际使用的筛选条件原样带回去，页面照原样展示，避免「条件悄悄变了」。
"""

from dataclasses import asdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import FirstBoardOut, FirstBoardRowOut, RpsOut, RpsRowOut
from app.services import pools

router = APIRouter(prefix="/api/pools", tags=["选股池"])


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
        limit=limit,
    )
    if board.trade_date is None:
        raise HTTPException(status_code=404, detail="还没有涨停池数据")
    return FirstBoardOut(
        trade_date=board.trade_date,
        conditions=board.conditions,
        rows=[FirstBoardRowOut(**asdict(row)) for row in board.rows],
    )
