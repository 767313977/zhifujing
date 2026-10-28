"""个股的「板块 / 行业」两个口径（多张榜共用）。

两个字段**不是一套分类**，页面上通常并排展示：

- `sectors`：开盘红「精选板块」——它来自涨停天梯（`stock_concept`），答的是
  「这只票**最近一次涨停是因为哪个题材**」，没涨停过就没有值（覆盖约 75%）。
- `industry`：同花顺三级行业路径（`stock_basic.industry`，如 `电子-半导体-集成电路Ⅲ`），
  答的是「公司做什么生意」。

口径要求（2026-10-28 从 `api/patterns.py` 抽到这里，异动页 / 形态页 / 资金面三处共用）：

- 板块取该股**出现过的最近一天**，不是当天 —— 按当天取会让九成以上行空着；
- 一次查全、在内存里按 code 挑最新的那天。写成「每只票一条相关子查询」会把接口从
  一次查询变成上千次（`code` 不是 `stock_concept` 主键首列，每次都要扫）。
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import StockBasic, StockConcept
from app.sources.base import chunked

# 与各 api 模块同一约定：老版本 SQLite 单条 SQL 变量上限 999
_IN_BATCH = 900


def sectors_of(db: Session, codes: set[str]) -> dict[str, list[str]]:
    """`{code: [板块名, ...]}`，取该股最近一次涨停所属的板块（可能多个）。"""
    if not codes:
        return {}
    concept_rows = []
    for batch in chunked(sorted(codes), _IN_BATCH):
        concept_rows.extend(
            db.execute(
                select(
                    StockConcept.code, StockConcept.trade_date, StockConcept.concept
                ).where(StockConcept.code.in_(batch))
            ).all()
        )
    latest_seen: dict[str, tuple[date, list[str]]] = {}
    for code, seen_on, concept in concept_rows:
        current = latest_seen.get(code)
        if current is None or seen_on > current[0]:
            latest_seen[code] = (seen_on, [concept])
        elif seen_on == current[0]:
            current[1].append(concept)
    return {code: sorted(names) for code, (_, names) in latest_seen.items()}


def industries_of(db: Session, codes: set[str]) -> dict[str, str | None]:
    """`{code: 同花顺三级行业路径}`。查不到就是 `None`（不编一个「其他」）。"""
    if not codes:
        return {}
    industries: dict[str, str | None] = {}
    for batch in chunked(sorted(codes), _IN_BATCH):
        industries.update(
            dict(
                db.execute(
                    select(StockBasic.code, StockBasic.industry).where(
                        StockBasic.code.in_(batch)
                    )
                ).all()
            )
        )
    return industries
