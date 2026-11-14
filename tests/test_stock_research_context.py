"""S3-C：个股观察日研究依据（只读解释链）。"""

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import (
    Base,
    CollectLog,
    LimitPool,
    MarketSentiment,
    SectorBasic,
    SectorMember,
    StockBasic,
    StockConcept,
    StockDaily,
    TradeCalendar,
)
from app.services import stock_research_context as ctx

D1 = date(2026, 11, 10)
D2 = date(2026, 11, 11)
D3 = date(2026, 11, 12)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([TradeCalendar(trade_date=day) for day in (D1, D2, D3)])
        db.add(StockBasic(code="600000", name="合成银行", industry="银行"))
        db.add_all(
            [
                LimitPool(trade_date=D2, code="600000", pool_type="up", name="合成银行", consecutive=2),
                LimitPool(trade_date=D3, code="600000", pool_type="broken", name="合成银行"),
                # D3 涨停池另有一只票，保证池本身可信（不是 ok+0 异常）
                LimitPool(trade_date=D3, code="600001", pool_type="up", name="合成科技", consecutive=1),
                CollectLog(trade_date=D2, task="pool_up", status="ok", rows=1),
                CollectLog(trade_date=D2, task="pool_broken", status="ok", rows=0),
                CollectLog(trade_date=D2, task="pool_down", status="ok", rows=0),
                CollectLog(trade_date=D3, task="pool_up", status="ok", rows=1),
                CollectLog(trade_date=D3, task="pool_broken", status="ok", rows=1),
                CollectLog(trade_date=D3, task="pool_down", status="ok", rows=0),
                StockDaily(trade_date=D2, code="600000", open=9.5, close=10.0, pct_chg=10.0),
                StockDaily(trade_date=D3, code="600000", open=10.0, close=9.5, pct_chg=-5.0),
                MarketSentiment(
                    trade_date=D2,
                    limit_up_count=50,
                    broken_rate=20.0,
                    max_consecutive=2,
                    up_count=3000,
                    down_count=2000,
                ),
                SectorBasic(code="801001", name="芯片", taxonomy="kph_selected"),
                SectorMember(
                    trade_date=D2,
                    sector_code="801001",
                    code="600000",
                    name="合成银行",
                    fetched_at=datetime(2026, 11, 11, 22, 0),
                ),
                StockConcept(trade_date=D2, code="600000", concept="芯片"),
            ]
        )
        db.commit()
        yield db


def build(db, **kwargs):
    params = {"code": "600000", "as_of": D2}
    params.update(kwargs)
    return ctx.build(db, **params)


def test_as_known_and_later_outcome_are_separated(session):
    data = build(session)
    assert data["resolved_trade_date"] == D2
    assert data["as_known_on_day"]["limit_status"] == "UP"
    assert data["as_known_on_day"]["board_height"] == 2
    assert data["as_known_on_day"]["daily"]["pct_chg"] == 10.0
    assert data["as_known_on_day"]["themes"][0]["known_by"] == ctx.KNOWN_SAME_DAY_UNKNOWN
    assert data["as_known_on_day"]["concepts"][0]["known_by"] == ctx.KNOWN_POST_CLOSE

    outcome = data["later_outcome"]
    assert outcome["outcome_date"] == D3
    assert outcome["pct_chg"] == -5.0
    assert outcome["status"] == ctx.STATUS_VERIFIED
    assert outcome["limit_up_again"] is False

    forward = [item for item in data["evidence"] if item["is_forward_known"]]
    assert len(forward) == 1
    assert forward[0]["source_trade_date"] == D3
    assert data["historical_signal_eligible"] is False


def test_missing_pool_is_not_backfilled(session):
    session.query(CollectLog).filter(
        CollectLog.trade_date == D2, CollectLog.task == "pool_up"
    ).delete()
    session.commit()
    data = build(session)
    assert data["as_known_on_day"]["limit_status"] == "UNKNOWN"
    assert data["quality"]["status"] == ctx.STATUS_UNVERIFIABLE
    assert "POOL_LOG_MISSING_WITH_ROWS" in data["quality"]["reasons"]
    # 不会偷偷去看 D3
    assert data["resolved_trade_date"] == D2


def test_non_trade_day_is_rejected(session):
    with pytest.raises(ValueError):
        build(session, as_of=date(2026, 11, 13))


def test_invalid_code_is_rejected(session):
    with pytest.raises(ValueError):
        build(session, code="abc")
    with pytest.raises(ValueError):
        build(session, code="999999")


def test_pending_when_no_next_trade_day(session):
    data = build(session, as_of=D3)
    assert data["later_outcome"]["outcome_date"] is None
    assert data["later_outcome"]["status"] == ctx.STATUS_PENDING
    assert data["later_outcome"]["pct_chg"] is None


def test_d1_quote_with_untrusted_pool_is_partial(session):
    session.query(CollectLog).filter(
        CollectLog.trade_date == D3, CollectLog.task == "pool_up"
    ).delete()
    session.commit()
    data = build(session)
    outcome = data["later_outcome"]
    assert outcome["pct_chg"] == -5.0
    assert outcome["status"] == ctx.STATUS_PARTIAL
    assert outcome["limit_up_again"] is None
    assert outcome["reason_code"] == "OUTCOME_LIMIT_POOL_UNVERIFIED"


def test_prior_day_theme_is_known(session):
    session.query(SectorMember).filter(SectorMember.code == "600000").update(
        {"fetched_at": datetime(2026, 11, 10, 22, 0)}
    )
    session.commit()
    data = build(session)
    assert data["as_known_on_day"]["themes"][0]["known_by"] == ctx.KNOWN_PRIOR_DAY


def test_later_theme_is_not_claimed_as_known(session):
    session.query(SectorMember).filter(SectorMember.code == "600000").update(
        {"fetched_at": datetime(2026, 11, 12, 9, 0)}
    )
    session.commit()
    data = build(session)
    assert data["as_known_on_day"]["themes"][0]["known_by"] == ctx.KNOWN_LATER
    assert "THEME_PIT_UNKNOWN" in data["quality"]["reasons"]


def test_default_as_of_uses_latest_trade_day(session, monkeypatch):
    monkeypatch.setattr(ctx, "today_cn", lambda: D3)
    data = build(session, as_of=None)
    assert data["requested_as_of"] is None
    assert data["resolved_trade_date"] == D3


def test_evidence_entries_are_complete_and_sourced(session):
    data = build(session)
    assert data["evidence"]
    for item in data["evidence"]:
        assert item["claim"]
        assert item["source_table"]
        assert item["known_by"]
        assert item["quality"]
        assert isinstance(item["is_forward_known"], bool)
    assert {item["source_table"] for item in data["evidence"]} >= {
        "limit_pool",
        "stock_daily",
        "market_sentiment",
        "sector_member",
    }


def test_service_is_read_only(session):
    before = (
        session.scalar(select(func.count()).select_from(LimitPool)),
        session.scalar(select(func.count()).select_from(StockDaily)),
        session.scalar(select(func.count()).select_from(CollectLog)),
        session.scalar(select(func.count()).select_from(SectorMember)),
    )
    build(session)
    after = (
        session.scalar(select(func.count()).select_from(LimitPool)),
        session.scalar(select(func.count()).select_from(StockDaily)),
        session.scalar(select(func.count()).select_from(CollectLog)),
        session.scalar(select(func.count()).select_from(SectorMember)),
    )
    assert before == after


def test_no_external_sync_is_called(session, monkeypatch):
    import app.api.stock as stock_api

    def boom(*_args, **_kwargs):
        raise AssertionError("research-context must not sync external data")

    monkeypatch.setattr(stock_api, "DailyCollector", boom)
    data = build(session)
    assert data["code"] == "600000"
