"""S4 查询预算：批量读取，禁止逐证券 N+1。

不按耗时断言（CI 机器差异太大），只数 SQL 条数：数据规模翻倍时条数应基本不变。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.models import (
    Base,
    CollectLog,
    LimitPool,
    MarketSentiment,
    SectorBasic,
    SectorMember,
    StockBasic,
    StockDaily,
    TradeCalendar,
)
from app.services import data_quality as dq
from app.services import sector_resonance as sr
from app.services import sentiment_next_day as snd
from app.services import stock_research_context as ctx

DAYS = 250
STOCKS = 120


@contextmanager
def count_queries(engine):
    counter = {"n": 0}

    def _before(_conn, _cursor, _statement, _parameters, _context, _many):
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _before)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _before)


def _engine():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return engine


def _seed(engine):
    start = date(2026, 1, 1)
    days = [start + timedelta(days=index) for index in range(DAYS)]
    codes = [f"6{index:05d}" for index in range(STOCKS)]
    with Session(engine) as db:
        db.add_all([TradeCalendar(trade_date=day) for day in days])
        for code in codes:
            db.add(StockBasic(code=code, name=code, industry="测试"))
        for index, day in enumerate(days):
            up = codes[:20]
            for code in up:
                db.add(
                    LimitPool(
                        trade_date=day,
                        code=code,
                        pool_type="up",
                        name=code,
                        consecutive=(index % 3) + 1,
                    )
                )
            db.add(CollectLog(trade_date=day, task="pool_up", status="ok", rows=len(up)))
            db.add(CollectLog(trade_date=day, task="pool_broken", status="ok", rows=0))
            db.add(CollectLog(trade_date=day, task="pool_down", status="ok", rows=0))
            db.add(
                MarketSentiment(
                    trade_date=day,
                    limit_up_count=len(up),
                    broken_rate=10.0,
                    max_consecutive=3,
                    up_count=3000,
                    down_count=1000,
                )
            )
            for code in codes:
                db.add(
                    StockDaily(
                        trade_date=day,
                        code=code,
                        open=10,
                        close=10,
                        pct_chg=1.0 if code in up else -0.5,
                    )
                )
        # 10 个板块 × 500 只成员 = 5000（跨板块有重叠）
        last = days[-1]
        for sector_index in range(10):
            code = f"S{sector_index:02d}"
            db.add(SectorBasic(code=code, name=code, taxonomy="test"))
            for stock_index in range(500):
                db.add(
                    SectorMember(
                        trade_date=last,
                        sector_code=code,
                        # 每个板块 500 只、板块间不重叠 → 5000 个唯一成员
                        code=f"{500000 + sector_index * 1000 + stock_index:06d}",
                        name="x",
                        fetched_at=datetime(2026, 10, 1, 22, 0),
                    )
                )
        db.commit()
    return days


@pytest.fixture(scope="module")
def seeded():
    engine = _engine()
    days = _seed(engine)
    yield engine, days
    engine.dispose()


def test_quality_overview_is_batched(seeded):
    engine, days = seeded
    with Session(engine) as db, count_queries(engine) as counter:
        data = dq.overview(db, target_date=days[-1], days=DAYS)
    assert data["as_of"] == days[-1]
    assert counter["n"] <= 25, f"quality overview 用了 {counter['n']} 条 SQL"


def test_quality_timeline_is_batched(seeded):
    engine, _days = seeded
    with Session(engine) as db, count_queries(engine) as counter:
        data = dq.timeline(db, source="limit_pool", days=DAYS)
    assert len(data["points"]) == DAYS
    assert counter["n"] <= 25, f"quality timeline 用了 {counter['n']} 条 SQL"


def test_sector_resonance_is_batched(seeded):
    engine, days = seeded
    codes = [f"S{index:02d}" for index in range(10)]
    with Session(engine) as db, count_queries(engine) as counter:
        data = sr.build(
            db,
            as_of=days[-1],
            sector_codes=codes,
            mode="OR",
            limit=500,
        )
    assert data["coverage"]["member_observed_rows"] == 5000
    assert counter["n"] <= 30, f"sector resonance 用了 {counter['n']} 条 SQL"


def test_sentiment_next_day_is_batched(seeded):
    engine, days = seeded
    with Session(engine) as db, count_queries(engine) as counter:
        data = snd.build_next_day(
            db,
            days=DAYS,
            group=snd.GROUP_FIRST,
            metric="limit_up_count",
            end_date=days[-1],
        )
    assert data["series"]
    # 250 日窗口当前约 1~2 条/日（≈88 条），**与样本只数无关**，不是逐证券 N+1。
    # 再把天数翻倍也不会按股票数增长；这里只挡住「按只查库」的退化。
    assert counter["n"] <= 120, f"sentiment next-day 用了 {counter['n']} 条 SQL"


def test_research_context_is_batched(seeded):
    engine, days = seeded
    with Session(engine) as db, count_queries(engine) as counter:
        data = ctx.build(db, code="600000", as_of=days[-1])
    assert data["resolved_trade_date"] == days[-1]
    assert counter["n"] <= 20, f"research-context 用了 {counter['n']} 条 SQL"
