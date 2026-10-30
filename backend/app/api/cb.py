"""可转债 T+0 观察池接口。

口径全部在 `services.cb_pool`（硬门槛 + 打分权重），这里只做形状转换 ——
页面上的每个数字都要能解释「为什么在池子里 / 为什么被剔掉」。
"""

from dataclasses import asdict
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import CbExcludedRowOut, CbGateOut, CbPoolOut, CbPoolRowOut
from app.services import cb_pool

router = APIRouter(prefix="/api/cb", tags=["可转债"])


@router.get("/pool", response_model=CbPoolOut)
def pool(
    trade_date: date | None = Query(None, alias="date", description="缺省 = 最近一次快照"),
    session: Session = Depends(get_db),
) -> CbPoolOut:
    """当日观察池：通过硬门槛的按评分降序，没过门槛的按成交额降序。

    不传 `date` = 最近一次快照（快照只能当天采，所以就是最近一个采集日）。
    传一个没有快照的日期会给空表并回 `trade_date=null` —— 不回落、不拿别的日子顶替，
    否则页面会把旧数据当成那天。
    """
    data = cb_pool.build(session, trade_date)
    return CbPoolOut(
        trade_date=data.trade_date,
        total=data.total,
        rows=[CbPoolRowOut(**asdict(row)) for row in data.rows],
        excluded=[CbExcludedRowOut(**asdict(row)) for row in data.excluded],
        gates=[CbGateOut(**asdict(gate)) for gate in data.gates],
    )
