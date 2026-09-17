"""涨停 / 跌停 / 炸板 与龙虎榜接口。"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import resolve_trade_date
from app.db import get_db
from app.models import Lhb, LimitPool
from app.schemas import LadderLevel, LhbOut, LimitPoolOut, LimitStock

router = APIRouter(prefix="/api", tags=["复盘"])

POOL_TYPES = {"up", "down", "broken"}


def _build_ladder(rows: list[LimitPool]) -> list[LadderLevel]:
    """按连板高度分层，高度从高到低。"""
    buckets: dict[int, list[LimitPool]] = {}
    for row in rows:
        buckets.setdefault(row.consecutive or 1, []).append(row)
    return [
        LadderLevel(
            consecutive=level,
            count=len(items),
            stocks=[LimitStock.model_validate(item) for item in items],
        )
        for level, items in sorted(buckets.items(), reverse=True)
    ]


@router.get("/limit/pool", response_model=LimitPoolOut)
def limit_pool(
    pool_type: str = Query("up", alias="type", description="up=涨停 down=跌停 broken=炸板"),
    trade_date: date = Depends(resolve_trade_date),
    session: Session = Depends(get_db),
) -> LimitPoolOut:
    if pool_type not in POOL_TYPES:
        raise HTTPException(
            status_code=400, detail=f"type 只能是 {sorted(POOL_TYPES)}，收到 {pool_type}"
        )

    rows = list(
        session.scalars(
            select(LimitPool)
            .where(LimitPool.trade_date == trade_date, LimitPool.pool_type == pool_type)
            # 连板高度优先，其次封板资金（跌停池与炸板池的这两列可能为空）
            .order_by(
                LimitPool.consecutive.desc().nullslast(),
                LimitPool.seal_amount.desc().nullslast(),
            )
        )
    )
    return LimitPoolOut(
        trade_date=trade_date,
        pool_type=pool_type,
        total=len(rows),
        # 梯队只对涨停有意义：跌停是「连续跌停」、炸板池没有连板数
        ladder=_build_ladder(rows) if pool_type == "up" else None,
        stocks=[LimitStock.model_validate(row) for row in rows],
    )


@router.get("/lhb", response_model=list[LhbOut])
def lhb_list(
    trade_date: date = Depends(resolve_trade_date),
    session: Session = Depends(get_db),
) -> list[LhbOut]:
    rows = list(
        session.scalars(
            select(Lhb)
            .where(Lhb.trade_date == trade_date)
            .order_by(Lhb.net_buy.desc().nullslast())
        )
    )
    return [LhbOut.model_validate(row) for row in rows]
