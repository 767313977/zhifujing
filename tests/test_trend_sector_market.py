"""M4：板块强度口径（F4/F5）与市场环境分类。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.models import (
    Base,
    BreadthDaily,
    FearGreedDaily,
    IndexDaily,
    MarketSentiment,
    RpsDaily,
    SectorDaily,
    SectorMember,
    TradeCalendar,
)
from app.services import research_screen, sector_rps
from app.services.trend_engine import market_regime, sector_strength

START = date(2025, 1, 1)
#: 90 天：板块 RPS 至少需要 5 个板块（`_sector_states` 的横截面下限），
#: 市场环境至少需要 MA60 + 5 根指数。
DAYS = [START + timedelta(days=index) for index in range(90)]


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm4.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([TradeCalendar(trade_date=day) for day in DAYS])
        yield session


def _seed_sectors(session, *, drop_none: int = 0) -> None:
    """A~E 五个板块：A 最强、E 最弱；C 末尾缺若干天 pct。

    五个板块是 `_sector_states` 横截面分位的下限（少于 5 个不出 RPS）。
    """
    for index, day in enumerate(DAYS):
        for code, name, pct, amount in (
            ("A", "板块A", 2.0, 3e9),
            ("B", "板块B", 1.0, 2e9),
            ("C", "板块C", 0.5, 1e9),
            ("D", "板块D", 1.5, 1.5e9),
            ("E", "板块E", 0.2, 0.8e9),
            ("F", "板块F", 0.8, 1.2e9),
        ):
            value = pct
            if code == "C" and drop_none and index >= len(DAYS) - drop_none:
                value = None
            session.add(
                SectorDaily(
                    trade_date=day,
                    sector_code=code,
                    name=name,
                    taxonomy="kph_selected",
                    source="kph",
                    pct_chg=value,
                    amount=amount,
                )
            )
    session.flush()


def test_sector_rps_uses_mid_rank_and_matches_sector_rps(db) -> None:
    _seed_sectors(db)
    day = DAYS[-1]
    board = research_screen.sector_states(db, trade_date=day, limit=10)
    rows = {row["sector_code"]: row for row in board["rows"]}
    reference = sector_rps.rps20_on(db, day, "kph_selected")
    for code, row in rows.items():
        if code in reference:
            assert row["rps20"] == pytest.approx(reference[code], abs=0.1)
    assert rows["A"]["rps20"] > rows["B"]["rps20"] > rows["C"]["rps20"]


def test_sector_ties_get_same_percentile(db) -> None:
    for day in DAYS:
        for code in ("A", "B", "C", "D", "E", "F"):
            db.add(
                SectorDaily(
                    trade_date=day,
                    sector_code=code,
                    name=code,
                    taxonomy="kph_selected",
                    pct_chg=1.0,
                    amount=1e9,
                )
            )
    db.flush()
    board = research_screen.sector_states(db, trade_date=DAYS[-1], limit=10)
    values = {row["rps20"] for row in board["rows"]}
    assert len(values) == 1  # 完全并列必须同分
    assert len(board["rows"]) == 6


def test_sector_coverage_gate_rejects_short_window(db) -> None:
    _seed_sectors(db, drop_none=3)  # 17/20 = 85% < 90%
    board = research_screen.sector_states(db, trade_date=DAYS[-1], limit=10)
    rows = {row["sector_code"]: row for row in board["rows"]}
    # 覆盖率不足的板块无法计算 20 日收益 → 不进入横截面排名（不是给 0 分）
    assert "C" not in rows
    assert rows["A"]["ret20"] is not None
    assert board["coverage"]["excluded_no_rps20"] >= 1


def test_sector_coverage_gate_allows_one_missing_day(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'm4b.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([TradeCalendar(trade_date=day) for day in DAYS])
        _seed_sectors(session, drop_none=1)  # 19/20 = 95% ≥ 90%
        board = research_screen.sector_states(session, trade_date=DAYS[-1], limit=10)
        rows = {row["sector_code"]: row for row in board["rows"]}
        assert rows["C"]["ret20"] is not None


def test_sector_strength_rank_and_breadth(db) -> None:
    _seed_sectors(db)
    day = DAYS[-1]
    # 手工建 backfill manifest（没有 ORM 模型）+ 成分股，验证广度可信路径
    db.execute(
        text(
            "CREATE TABLE sector_member_backfill ("
            " trade_date DATE, sector_code TEXT, status TEXT,"
            " reported INTEGER, unique_count INTEGER, written INTEGER)"
        )
    )
    db.execute(
        text(
            "INSERT INTO sector_member_backfill VALUES"
            " (:d,'A','complete',3,3,3)"
        ),
        {"d": day.isoformat()},
    )
    for code, pct, rps in (("000001", 3.0, 85.0), ("000002", 1.0, 70.0), ("000003", -2.0, 90.0)):
        db.add(SectorMember(trade_date=day, sector_code="A", code=code, name=code, pct_chg=pct))
        db.add(RpsDaily(trade_date=day, code=code, rps60=rps))
    db.flush()

    board = sector_strength.evaluate(db, trade_date=day)
    rows = {row["sector_code"]: row for row in board.rows}
    assert rows["A"]["rank"] == 1
    assert rows["A"]["up_ratio"] == pytest.approx(66.7, abs=0.1)
    assert rows["A"]["strong_members"] == 2
    assert rows["A"]["concentration_top5"] is not None
    assert rows["B"]["up_ratio"] is None  # 没有 manifest → 不猜广度
    assert rows["A"]["persistence_days"] >= 1
    assert board.coverage["status"] == "ok"
    assert board.coverage["breadth_trusted"] == 1


def _seed_index(session, days: list[date], *, falling: bool = False) -> None:
    price = 3000.0
    for day in days:
        price *= 0.998 if falling else 1.004
        session.add(IndexDaily(trade_date=day, code="000001.SH", name="上证", close=price))
    session.flush()


def test_market_regime_bull_and_extreme(db) -> None:
    _seed_index(db, DAYS)
    day = DAYS[-1]
    db.add(BreadthDaily(trade_date=day, above_ma20=62.0, above_ma60=55.0))
    db.add(MarketSentiment(trade_date=day, limit_up_count=40, limit_down_count=3))
    db.add(FearGreedDaily(trade_date=day, limit_strength=20.0, volatility=18.0))
    db.flush()

    result = market_regime.evaluate(db, day=day)
    assert result.regime == market_regime.BULL
    assert result.confidence == "high"
    assert result.model_weight_factor == 1.0

    db.query(MarketSentiment).delete()
    db.flush()
    db.add(MarketSentiment(trade_date=day, limit_up_count=5, limit_down_count=80))
    db.flush()
    extreme = market_regime.evaluate(db, day=day)
    assert extreme.regime == market_regime.EXTREME_RISK
    assert extreme.model_weight_factor == 0.0


def test_market_regime_breadth_veto_and_bear(db) -> None:
    _seed_index(db, DAYS)
    day = DAYS[-1]
    db.add(BreadthDaily(trade_date=day, above_ma20=20.0, above_ma60=18.0))
    db.add(MarketSentiment(trade_date=day, limit_up_count=30, limit_down_count=5))
    db.flush()
    vetoed = market_regime.evaluate(db, day=day)
    assert vetoed.regime == market_regime.SIDEWAYS

    falling_days = [START + timedelta(days=index) for index in range(90, 150)]
    db.add_all([TradeCalendar(trade_date=item) for item in falling_days])
    _seed_index(db, falling_days, falling=True)
    bear = market_regime.evaluate(db, day=falling_days[-1])
    assert bear.regime == market_regime.BEAR


def test_market_regime_unknown_without_index(db) -> None:
    result = market_regime.evaluate(db, day=DAYS[-1])
    assert result.regime == market_regime.UNKNOWN
    assert "index_daily" in result.missing


def test_classify_pure_rules() -> None:
    bull = {
        "close": 100.0, "ma20": 98.0, "ma60": 95.0, "ma20_slope": 0.5,
        "ma60_slope": 0.2, "above_ma20": 55.0, "limit_down": 5,
    }
    assert market_regime.classify(bull)[0] == market_regime.BULL

    bear = dict(bull, close=90.0, ma20=95.0, ma60=98.0, ma60_slope=-0.3)
    assert market_regime.classify(bear)[0] == market_regime.BEAR

    extreme = dict(bull, limit_down=100)
    assert market_regime.classify(extreme)[0] == market_regime.EXTREME_RISK

    veto = dict(bull, above_ma20=20.0)
    assert market_regime.classify(veto)[0] == market_regime.SIDEWAYS

    assert market_regime.classify({"close": 100.0})[0] == market_regime.UNKNOWN
