"""个股分析：输入「代码 / 名称 / 拼音首字母」→ 直接给结论。

移植自原型 yangban-desk 的「查票分析」框（它那个框下面直接挂阶段标签 + 日期）。
站内落点是一个独立页面 `/stock-analysis`，导航里跟在「悟道之路」后面。

结论本身来自 `services/patterns.classify_phase` —— 与悟道那几张名单**共用同一批判据**，
所以这里说「明天盯」时，悟道之路的名单里一定有它；反过来它没进名单，说明被更靠前的
阶段占了（例如「吵起来了」压过「明天盯」），`phase` 的文本里会说清是哪一条。

⚠️ 原型那个框写着「技术 / 新闻 / 基本面三面合议」—— **本站没有个股新闻源**，
所以「新闻」这一面是空的、不编；「基本面」只给行业（要看市值/市盈率走个股页）；
「技术面」就是这里的阶段判定。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Lhb, LimitPool, StockBasic, StockConcept, StockDaily
from app.schemas import StockAnalysis
from app.services.stock_lookup import LookupError, resolve_code
from app.services.stock_phase import load_phase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analysis", tags=["个股分析"])


@router.get("/lookup", response_model=StockAnalysis)
def lookup(
    q: str = Query(
        ...,
        min_length=1,
        max_length=32,
        description="6 位代码 / 股票名称 / 拼音首字母（如 300654 / 世纪天鸿 / sjth）",
    ),
    session: Session = Depends(get_db),
) -> StockAnalysis:
    """解析输入并给出当天结论。

    解析规则与自选股那个输入框**共用** `services/stock_lookup.resolve_code`
    （代码 → 名称全等 → 首字母全等 → 名称包含 / 首字母开头），解析不出或多解时
    原样把它的文案转成 400 —— 那套文案本来就是写给用户看的。
    """
    try:
        code = resolve_code(q)
    except LookupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    basic = session.execute(
        select(StockBasic.name, StockBasic.industry).where(StockBasic.code == code)
    ).first()
    name = (basic[0] if basic else None) or code
    industry = basic[1] if basic else None

    latest = session.scalar(
        select(StockDaily)
        .where(StockDaily.code == code)
        .order_by(StockDaily.trade_date.desc())
        .limit(1)
    )

    # 题材：与 `api/stock.themes` 同一条路 —— 取该股出现过的**最近一天**（涨停天梯快照），
    # 所以它回答的是「最近一次涨停是因为哪个板块」，不是「它属于哪些概念」。
    # 从没涨停过的票这里就是空的，如实返回空列表。
    sectors: list[str] = []
    concept_day = session.scalar(
        select(func.max(StockConcept.trade_date)).where(StockConcept.code == code)
    )
    if concept_day is not None:
        sectors = sorted(
            session.scalars(
                select(StockConcept.concept).where(
                    StockConcept.code == code, StockConcept.trade_date == concept_day
                )
            )
        )

    limit_up_count = (
        session.scalar(
            select(func.count())
            .select_from(LimitPool)
            .where(LimitPool.code == code, LimitPool.pool_type == "up")
        )
        or 0
    )
    lhb_count = (
        session.scalar(select(func.count()).select_from(Lhb).where(Lhb.code == code)) or 0
    )

    return StockAnalysis(
        code=code,
        name=name,
        trade_date=latest.trade_date if latest else None,
        close=latest.close if latest else None,
        pct_chg=latest.pct_chg if latest else None,
        phase=load_phase(session, code),
        industry=industry,
        sectors=sectors,
        limit_up_count=int(limit_up_count),
        lhb_count=int(lhb_count),
    )
