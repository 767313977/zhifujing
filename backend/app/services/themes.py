"""题材归属：涨停股的「题材 × 家数」聚合 + 单只票的所属题材。

两件事共用一份口径（开盘红**精选板块**，见 `jobs/collect_themes.py`）：

- `limit_up_themes`：「题材 × 涨停」的家数，板块页与飞书简报共用 ——
  两边各写一遍的话，改了一处忘了另一处，页面与简报会给出不一样的家数；
- `attribute`：一只票属于哪些题材，个股页「所属题材」与风控「单题材集中度」共用。

题材用的是精选板块而不是涨停池自带的行业字段，后者是**申万口径且截断 4 字**，
和板块表对不上。
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import LimitPool, SectorBasic, SectorDaily, SectorMember, StockConcept
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


@dataclass(frozen=True)
class Attribution:
    """一只票的题材归属。

    - `themes`：题材名 → 板块代码（回落 `stock_concept` 时没有代码，值为 None）；
    - `suppressed`：命中了黑名单被剔掉的标签（「科创板 / 低价股 / 重庆市 / 中报增长」
      这类身份 / 地域 / 业绩标签），调用方可以拿来给用户做悬浮说明。
    """

    themes: dict[str, str | None]
    suppressed: tuple[str, ...]


def is_non_theme(name: str) -> bool:
    """「身份 / 地域 / 业绩」类标签不算题材。

    名单直接用资金流榜那份 `api.sector.BOARD_EXCLUDE`（科创板、低价股、送转、
    中报增长、地域…），**不复制第二份** —— 两处各写一遍早晚会漂移。

    ⚠️ 延迟导入：service 层不该在模块加载期依赖 api 层（`api/sector` 自己会
    import 一堆 service，模块级互相 import 迟早成环）。
    """
    from app.api.sector import BOARD_EXCLUDE

    return any(word in name for word in BOARD_EXCLUDE)


def _latest_dates(session: Session, model, codes: list[str]) -> dict[str, date]:
    """`{代码: 该表里属于它的最新交易日}`。"""
    return dict(
        session.execute(
            select(model.code, func.max(model.trade_date))
            .where(model.code.in_(codes))
            .group_by(model.code)
        ).all()
    )


def _member_themes(
    session: Session, codes: list[str]
) -> dict[str, dict[str, str]]:
    """`sector_member` 口径：每只票**最新一次快照**里所属的精选板块。

    返回 `{代码: {板块名: 板块代码}}`。行业板块（`kph_industry`）不要 ——
    那是行业分类，不进「题材」。
    """
    latest = _latest_dates(session, SectorMember, codes)
    if not latest:
        return {}
    rows = session.execute(
        select(SectorMember.code, SectorBasic.name, SectorMember.sector_code)
        .join(SectorBasic, SectorBasic.code == SectorMember.sector_code)
        .where(
            or_(
                *[
                    and_(
                        SectorMember.code == code,
                        SectorMember.trade_date == day,
                    )
                    for code, day in latest.items()
                ]
            ),
            SectorBasic.taxonomy == TAXONOMY_SELECTED,
        )
    ).all()
    result: dict[str, dict[str, str]] = defaultdict(dict)
    for code, name, sector_code in rows:
        if name:
            result[code].setdefault(name, sector_code)
    return result


def _concept_themes(session: Session, codes: list[str]) -> dict[str, list[str]]:
    """`stock_concept` 兜底：每只票**最近一次涨停**那天挂的题材。"""
    latest = _latest_dates(session, StockConcept, codes)
    if not latest:
        return {}
    rows = session.execute(
        select(StockConcept.code, StockConcept.concept).where(
            or_(
                *[
                    and_(
                        StockConcept.code == code,
                        StockConcept.trade_date == day,
                    )
                    for code, day in latest.items()
                ]
            )
        )
    ).all()
    result: dict[str, list[str]] = defaultdict(list)
    for code, concept in rows:
        if concept:
            result[code].append(concept)
    return result


def attribute(session: Session, codes: Iterable[str]) -> dict[str, Attribution]:
    """一批票的题材归属，**口径与个股页「所属题材」一致**。

    取数顺序（2026-11-01 审计 P1）：
    1. `sector_member`：每晚预取的**全市场成分股快照**，覆盖没涨停的票；
    2. 查不到快照（新股 / 快照还没跑到 / 退市）再回落 `stock_concept` ——
       那张表只记当日涨停股，但至少比空着多一条信息。

    `stock_concept` 只收录**当日涨停股**，单靠它会让「没涨停的持仓」查不到任何
    题材、集中度静默跳过 —— 这就是这次把口径统一过来的原因。

    返回里**每个传入代码都有一条**（查不到就是空 `themes`），调用方据此数「几笔
    查不到题材」，不能靠 `dict.get` 的 None 去猜。
    """
    wanted = sorted({code for code in codes if code})
    if not wanted:
        return {}
    member = _member_themes(session, wanted)
    fallback = _concept_themes(
        session, [code for code in wanted if code not in member]
    )

    result: dict[str, Attribution] = {}
    for code in wanted:
        raw: dict[str, str | None] = dict(member.get(code) or {})
        if not raw:
            raw = dict.fromkeys(fallback.get(code, []))
        suppressed = tuple(name for name in raw if is_non_theme(name))
        themes = {
            name: sector_code
            for name, sector_code in sorted(raw.items())
            if not is_non_theme(name)
        }
        result[code] = Attribution(themes=themes, suppressed=suppressed)
    return result
