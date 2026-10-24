"""选股池的两块数据：RPS 相对强度榜、首板一进二候选。

两个都是**盘后候选池**，不是买卖指令：本站没有盘中数据，打板要看次日承接，
这里只负责把「值得盯的几十只」按可复现的条件筛出来。

口径说明
--------
- RPS：读 `jobs/scan_rps` 落库的当日横截面分位，不做二次计算；
- 首板一进二：条件全部可调（见 `first_board` 的参数），默认值是一套保守参数，
  不是「最优参数」—— 任何参数都应该用自己的库回测过再用。
"""

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import LimitPool, RpsDaily, StockBasic, StockConcept, StockDaily
from app.services.patterns import is_st

logger = logging.getLogger(__name__)

#: 单位换算：库里金额一律是**元**，接口参数用「亿」更好读
YI = 1e8

#: 北交所代码前缀（4/8/92）。30% 涨跌停 + 流动性差异，和沪深打板不是一套玩法
_BJ_PREFIXES = ("4", "8", "92")


@dataclass(frozen=True, slots=True)
class RpsRow:
    code: str
    name: str | None
    close: float | None
    pct_chg: float | None
    ret120: float | None
    ret250: float | None
    rps120: float | None
    rps250: float | None


@dataclass(frozen=True, slots=True)
class RpsBoard:
    trade_date: date | None
    rows: list[RpsRow]


def rps_board(
    session: Session,
    trade_date: date | None = None,
    min_rps: float = 0,
    limit: int = 100,
) -> RpsBoard:
    """RPS 榜：按 250 日分位降序。`min_rps` 同时作用在两档分位上（取较严的那个）。"""
    target = trade_date or session.scalar(select(func.max(RpsDaily.trade_date)))
    if target is None:
        return RpsBoard(trade_date=None, rows=[])

    rows = session.execute(
        select(
            RpsDaily.code,
            StockBasic.name,
            StockDaily.close,
            StockDaily.pct_chg,
            RpsDaily.ret120,
            RpsDaily.ret250,
            RpsDaily.rps120,
            RpsDaily.rps250,
        )
        .join(
            StockDaily,
            (StockDaily.trade_date == RpsDaily.trade_date) & (StockDaily.code == RpsDaily.code),
        )
        .outerjoin(StockBasic, StockBasic.code == RpsDaily.code)
        .where(
            RpsDaily.trade_date == target,
            RpsDaily.rps250.is_not(None),
            RpsDaily.rps250 >= min_rps,
        )
        .order_by(RpsDaily.rps250.desc())
        .limit(limit)
    ).all()

    return RpsBoard(
        trade_date=target,
        rows=[
            RpsRow(
                code=row[0],
                name=row[1],
                close=row[2],
                pct_chg=row[3],
                ret120=row[4],
                ret250=row[5],
                rps120=row[6],
                rps250=row[7],
            )
            for row in rows
        ],
    )


@dataclass(frozen=True, slots=True)
class FirstBoardRow:
    code: str
    name: str | None
    pct_chg: float | None
    price: float | None
    first_seal_time: str | None
    last_seal_time: str | None
    open_times: int | None
    float_mv: float | None
    amount: float | None
    turnover: float | None
    seal_amount: float | None
    #: 封单额 / 流通市值（%），衡量封板质量
    seal_ratio: float | None
    industry: str | None
    #: 当日涨停股里，与它同属一个精选板块最多有几只（含自己）
    theme: str | None
    theme_count: int


@dataclass(frozen=True, slots=True)
class FirstBoardBoard:
    trade_date: date | None
    conditions: dict = field(default_factory=dict)
    rows: list[FirstBoardRow] = field(default_factory=list)


def first_board(
    session: Session,
    trade_date: date | None = None,
    before: str = "140000",
    min_float_mv: float = 20,
    max_float_mv: float = 300,
    min_amount: float = 3,
    min_themes: int = 3,
    limit: int = 50,
) -> FirstBoardBoard:
    """首板一进二候选。

    条件（全部可调，金额单位**亿**）：
    - `before`：首封时间早于它（`HHMMSS`，默认 14:00 —— 只排掉尾盘偷袭，
      10:30 那种卡法实测会把池子清空：2026-10-23 全天只有 3 只在 10:30 前封板）
    - `min_float_mv` / `max_float_mv`：流通市值区间
    - `min_amount`：当日成交额下限
    - `min_themes`：当日同题材涨停家数下限（用 `stock_concept` 的精选板块口径）

    ⚠️ 只筛「首板」（`consecutive == 1`）且**没炸过板**（`open_times == 0`）的票；
    `seal_ratio` 越大说明封单相对流通盘越厚，是同样条件下的排序依据。
    """
    target = trade_date or session.scalar(
        select(func.max(LimitPool.trade_date)).where(LimitPool.pool_type == "up")
    )
    if target is None:
        return FirstBoardBoard(trade_date=None, conditions={})

    rows = list(
        session.scalars(
            select(LimitPool)
            .where(
                LimitPool.trade_date == target,
                LimitPool.pool_type == "up",
                LimitPool.consecutive == 1,
                LimitPool.open_times == 0,
                LimitPool.first_seal_time.is_not(None),
                LimitPool.first_seal_time <= before,
                LimitPool.float_mv >= min_float_mv * YI,
                LimitPool.float_mv <= max_float_mv * YI,
                LimitPool.amount >= min_amount * YI,
            )
            .order_by(LimitPool.first_seal_time)
        )
    )

    # 题材：当日每只涨停股的精选板块 → 板块内涨停家数
    concepts: dict[str, list[str]] = {}
    counter: Counter[str] = Counter()
    for code, concept in session.execute(
        select(StockConcept.code, StockConcept.concept).where(
            StockConcept.trade_date == target
        )
    ):
        concepts.setdefault(code, []).append(concept)
        counter[concept] += 1

    names = dict(
        session.execute(
            select(StockBasic.code, StockBasic.name).where(
                StockBasic.code.in_([row.code for row in rows])
            )
        ).all()
    )

    picked: list[FirstBoardRow] = []
    for row in rows:
        name = names.get(row.code) or row.name
        if is_st(name):
            continue
        # 北交所不是这套玩法：30% 涨跌停、流动性差，默认排除
        if row.code.startswith(_BJ_PREFIXES):
            continue
        stock_concepts = concepts.get(row.code) or []
        theme = max(stock_concepts, key=lambda item: counter[item], default=None)
        theme_count = counter[theme] if theme else 0
        if theme_count < min_themes:
            continue
        seal_ratio = (
            row.seal_amount / row.float_mv * 100
            if row.seal_amount and row.float_mv
            else None
        )
        picked.append(
            FirstBoardRow(
                code=row.code,
                name=name,
                pct_chg=row.pct_chg,
                price=row.price,
                first_seal_time=row.first_seal_time,
                last_seal_time=row.last_seal_time,
                open_times=row.open_times,
                float_mv=row.float_mv,
                amount=row.amount,
                turnover=row.turnover,
                seal_amount=row.seal_amount,
                seal_ratio=round(seal_ratio, 2) if seal_ratio is not None else None,
                industry=row.industry,
                theme=theme,
                theme_count=theme_count,
            )
        )

    # 题材热度优先，同热度看封单厚度
    picked.sort(key=lambda item: (item.theme_count, item.seal_ratio or 0), reverse=True)
    return FirstBoardBoard(
        trade_date=target,
        conditions={
            "before": before,
            "min_float_mv": min_float_mv,
            "max_float_mv": max_float_mv,
            "min_amount": min_amount,
            "min_themes": min_themes,
        },
        rows=picked[:limit],
    )
