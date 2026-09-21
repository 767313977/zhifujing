"""涨停股的题材归属采集（「题材 × 涨停」联动的基础）。

来源是开盘红的**涨停天梯**（`GetZhangTingTianTi`）：它逐只给出涨停股所属的
精选板块代码与名称，**一次调用拿全当日所有涨停股**，实时与历史都通。这比原先
问 iFinD「这些股票属于哪些同花顺概念」好得多 —— 那边一天要 5 次调用，还会偶发
整批空返回（见本文件旧版的注释）。

**为什么不用涨停池自带的行业字段**：那是东财申万口径、且被截断成 4 个字，
与本站板块只有 60/113 能对上（见设计文档 8.10）。按名字硬拼会大面积漏，
更糟的是会把「名字没对上」当成「这个板块没有涨停股」—— 零与未知混淆。

⚠️ 本表**只覆盖涨停股**。非涨停个股的板块归属开盘红没有可用接口（见 8.32.4），
所以个股页的题材展示也读本表 —— 对没涨停过的票是空的，如实留空而不是编一个。
落库的是**板块名称**（不是代码）：`services/themes.py` 与板块涨停家数都按名称
与 `sector_daily.name` 对齐。
"""

import logging
from datetime import date

from sqlalchemy import delete, func, select

from app.config import Settings, get_settings
from app.db import session_scope
from app.models import LimitPool, StockConcept
from app.sources.kaipanhong import KaipanhongSource

logger = logging.getLogger(__name__)


class ThemeCollector:
    """涨停股题材采集器。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.kph = KaipanhongSource(self.settings)

    def collect(self, trade_date: date) -> int:
        """拉取当日涨停股的板块归属。

        按日整段替换而不是增量 upsert：板块归属是会变的（同一只票今天归芯片、
        下个月可能归算力），只补不删会留下已经过时的旧行，而本表是「某日快照」语义。
        """
        ladder = self.kph.limit_up_ladder(trade_date)
        if not ladder:
            logger.warning("%s 涨停天梯返回 0 行，题材归属记空", trade_date)
            return 0

        # 与站内涨停池对账。两者来源不同（一个开盘红、一个东财），差几只很正常
        # （ST / 新股 / 北交所的处理口径不同），但差太多说明有一边出问题了。
        with session_scope() as session:
            pool_count = session.scalar(
                select(func.count())
                .select_from(LimitPool)
                .where(LimitPool.trade_date == trade_date, LimitPool.pool_type == "up")
            )
        if pool_count and abs(len(ladder) - pool_count) > max(5, pool_count * 0.15):
            logger.warning(
                "%s 涨停家数对不上：开盘红天梯 %d 家 vs 站内涨停池 %d 家，"
                "两边都要看一眼",
                trade_date,
                len(ladder),
                pool_count,
            )

        rows = [
            {
                "trade_date": trade_date,
                "code": item["code"],
                "concept": item["board_name"],
            }
            for item in ladder
            if item["board_name"]
        ]
        if len(rows) < len(ladder):
            logger.warning(
                "%d 只涨停股没有板块名称，已跳过", len(ladder) - len(rows)
            )

        with session_scope() as session:
            session.execute(
                delete(StockConcept).where(StockConcept.trade_date == trade_date)
            )
            for row in rows:
                session.merge(StockConcept(**row))
        logger.info(
            "%s 涨停题材落库 %d 行（天梯 %d 只）", trade_date, len(rows), len(ladder)
        )
        return len(rows)
