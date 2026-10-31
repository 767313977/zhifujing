"""恐贪六项原始值的两处口径修复（2026-10-31 复查）。

1. **两融只认沪、深两所都齐的交易日**：只有一所时合计只有正常的一半，20 日变化率
   会被算成 −40%~−50%，那一项必然打到「极度恐慌」，整条指数跟着偏；
2. **两融缺最新一天时沿用上一个齐全日**（最多 3 个交易日），保证每天都是 6 项；
3. **市场宽度当天日线覆盖不足 90% 时记 None**，不拿几十只票冒充全市场。
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.jobs import collect_fear_greed as fg
from app.models import Base, MarginDaily, StockDaily, StockUniverse

DAY = date(2026, 10, 30)
PREV = date(2026, 10, 29)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fg.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _margin(session, day: date, market: str, balance: float = 12000.0) -> None:
    session.add(
        MarginDaily(
            trade_date=day,
            market=market,
            financing_balance=balance,
            securities_balance=100.0,
        )
    )


def test_margin_requires_both_exchanges(db):
    """深市还没披露的那天不能只拿沪市求和（否则变化率接近 −50%）。"""
    _margin(db, PREV, "sh")
    _margin(db, PREV, "sz")
    _margin(db, DAY, "sh")  # 深市缺
    db.commit()

    values = fg._margin_by_date(db, PREV, DAY)

    assert values[PREV] == pytest.approx(2 * 12100.0)
    assert DAY not in values


def test_margin_ignores_zero_rows(db):
    """深市那行存在但余额是空（写成 0）同样算「没披露」。"""
    _margin(db, PREV, "sh")
    db.add(
        MarginDaily(
            trade_date=PREV, market="sz", financing_balance=None, securities_balance=None
        )
    )
    db.commit()

    assert fg._margin_by_date(db, PREV, PREV) == {}


def test_carry_forward_fills_small_gaps_only():
    values = [10.0, None, None, 20.0, None, None, None, None, 30.0]

    out = fg._carry_forward(values, limit=3)

    # 第 5~7 个位置缺 3 天以内 → 沿用 20.0；第 8 个位置缺第 4 天 → None
    assert out == [10.0, 10.0, 10.0, 20.0, 20.0, 20.0, 20.0, None, 30.0]


def test_carry_forward_keeps_leading_gaps_empty():
    assert fg._carry_forward([None, None, 5.0]) == [None, None, 5.0]


def test_breadth_requires_daily_coverage(db):
    """覆盖不到股票池 90% 的交易日不给宽度值（10 只池子，3 只算不出「全市场」）。"""
    for index in range(10):
        db.add(StockUniverse(code=f"6000{index:02d}", name=f"票{index}"))
    for index in range(9):
        db.add(StockDaily(code=f"6000{index:02d}", trade_date=DAY, close=10.0, pct_chg=1.0))
    for index in range(3):
        db.add(StockDaily(code=f"6000{index:02d}", trade_date=PREV, close=10.0, pct_chg=1.0))
    db.commit()

    values = fg._breadth_by_date(db, PREV, DAY)

    assert values[DAY] == 100.0
    assert PREV not in values


def test_breadth_skips_check_when_pool_missing(db):
    """池子还没建时保持原行为（不然整个宽度序列都会变 None）。"""
    db.add(StockDaily(code="600519", trade_date=DAY, close=10.0, pct_chg=1.0))
    db.commit()

    assert fg._breadth_by_date(db, DAY, DAY) == {DAY: 100.0}
