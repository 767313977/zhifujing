"""涨停股的题材归属聚合。

「题材 × 涨停」的口径只有一份，板块页与飞书简报共用 ——
两边各写一遍的话，改了一处忘了另一处，页面与简报会给出不一样的家数。

题材用的是开盘红**精选板块**（见 `jobs/collect_themes.py`），不是涨停池自带的
行业字段，后者是**申万口径且截断 4 字**，和板块表对不上。
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import LimitPool, SectorDaily, StockConcept
from app.sources.kaipanhong import TAXONOMY_SELECTED

# 至少这么多只涨停股共同挂着，才算一天的「共振题材」
MIN_CLUSTER = 3


@dataclass(frozen=True)
class ThemeCluster:
    concept: str
    count: int
    pct_chg: float | None


def limit_up_themes(
    session: Session, trade_date: date
) -> tuple[list[ThemeCluster], dict[str, list[str]]]:
    """返回（共振题材，个股 → 题材）。

    只用「能在板块表里找到的」概念：个股挂的概念里混着「融资融券 / 深股通 /
    沪深300样本股」这类非板块项，它们不参与板块涨跌，也不该出现在题材共振里。

    单只票的题材**按板块当日涨跌幅降序**而不是按涨停家数：按家数排的话
    通道类概念会占满每一行，按涨跌幅排则自然浮出当天真正在涨的方向。

    个股的题材列表**不截断** —— 截断只发生在展示层，
    否则「点题材标签筛选」会按截断后的列表筛，与标签上的家数对不上。
    """
    codes = list(
        session.scalars(
            select(LimitPool.code).where(
                LimitPool.trade_date == trade_date, LimitPool.pool_type == "up"
            )
        )
    )
    if not codes:
        return [], {}

    board_pct = {
        name: pct
        for name, pct in session.execute(
            select(SectorDaily.name, SectorDaily.pct_chg).where(
                SectorDaily.trade_date == trade_date,
                SectorDaily.taxonomy == TAXONOMY_SELECTED,
            )
        ).all()
        if name
    }

    grouped: dict[str, list[str]] = {code: [] for code in codes}
    for code, concept in session.execute(
        select(StockConcept.code, StockConcept.concept).where(
            StockConcept.trade_date == trade_date, StockConcept.code.in_(codes)
        )
    ).all():
        if concept in board_pct:
            grouped.setdefault(code, []).append(concept)

    counts: dict[str, int] = {}
    for concepts in grouped.values():
        for concept in concepts:
            counts[concept] = counts.get(concept, 0) + 1

    clusters = [
        ThemeCluster(concept=concept, count=count, pct_chg=board_pct.get(concept))
        for concept, count in counts.items()
        if count >= MIN_CLUSTER
    ]
    clusters.sort(key=lambda item: (-item.count, -(item.pct_chg or 0)))

    by_stock = {
        code: sorted(concepts, key=lambda name: -(board_pct.get(name) or 0))
        for code, concepts in grouped.items()
    }
    return clusters, by_stock
