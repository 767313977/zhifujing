"""S4-B：盘后数据可信度控制台（只读诊断）。

全部使用独立内存 SQLite，不连真实库、不发外部请求。
"""

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import (
    AuctionSnapshot,
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

D1 = date(2026, 11, 3)
D2 = date(2026, 11, 4)
D3 = date(2026, 11, 5)
D4 = date(2026, 11, 6)
FUTURE = date(2026, 11, 9)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([TradeCalendar(trade_date=day) for day in (D1, D2, D3, D4)])
        db.add(StockBasic(code="600000", name="合成银行", industry="银行"))
        for day in (D1, D2, D3, D4):
            db.add(StockDaily(trade_date=day, code="600000", open=10, close=10, pct_chg=1))
            db.add(
                MarketSentiment(
                    trade_date=day,
                    limit_up_count=30,
                    broken_rate=20.0,
                    max_consecutive=2,
                    up_count=3000,
                    down_count=2000,
                )
            )
            db.add(
                CollectLog(
                    trade_date=day,
                    task="pool_up",
                    status="ok",
                    rows=1,
                    created_at=datetime(day.year, day.month, day.day, 16, 0),
                )
            )
            db.add(
                CollectLog(
                    trade_date=day,
                    task="pool_broken",
                    status="ok",
                    rows=0,
                    created_at=datetime(day.year, day.month, day.day, 16, 0),
                )
            )
            db.add(
                CollectLog(
                    trade_date=day,
                    task="pool_down",
                    status="ok",
                    rows=0,
                    created_at=datetime(day.year, day.month, day.day, 16, 0),
                )
            )
        db.add_all(
            [
                LimitPool(trade_date=D1, code="600000", pool_type="up", name="合成银行", consecutive=1),
                SectorBasic(code="801001", name="芯片", taxonomy="kph_selected"),
                SectorBasic(code="801002", name="软件", taxonomy="kph_selected"),
                SectorMember(
                    trade_date=D1,
                    sector_code="801001",
                    code="600000",
                    name="合成银行",
                    fetched_at=datetime(2026, 11, 3, 22, 0),
                ),
            ]
        )
        db.commit()
        yield db


def _source(result, source):
    return {item["source"]: item for item in result["items"]}[source]


def test_limit_pool_statuses_and_snapshot_retained(session):
    # D2：成功之后又有一次失败 → 旧快照保留，状态 PARTIAL
    # D2 当天先成功落库，之后又有一次失败尝试
    session.add(
        LimitPool(trade_date=D2, code="600000", pool_type="up", name="合成银行", consecutive=1)
    )
    session.add(
        CollectLog(
            trade_date=D2,
            task="pool_up",
            status="failed",
            rows=0,
            created_at=datetime(2026, 11, 4, 20, 0),
        )
    )
    # D3：有行但完全没有日志
    session.add(LimitPool(trade_date=D3, code="600000", pool_type="up", name="合成银行", consecutive=1))
    for task in ("pool_up", "pool_broken", "pool_down"):
        session.query(CollectLog).filter(
            CollectLog.trade_date == D3, CollectLog.task == task
        ).delete()
    session.commit()

    result = dq.overview(session, target_date=D1, days=10)
    first = _source(result, "limit_pool")
    assert first["status"] == dq.STATUS_VERIFIED

    day2 = dq.overview(session, target_date=D2, days=10)
    item2 = _source(day2, "limit_pool")
    assert item2["status"] == dq.STATUS_PARTIAL
    assert "LATEST_ATTEMPT_FAILED_SNAPSHOT_RETAINED" in item2["reason_codes"]
    assert item2["latest_valid_snapshot"] == D2
    assert item2["latest_attempt"]["status"] == "failed"

    day3 = dq.overview(session, target_date=D3, days=10)
    item3 = _source(day3, "limit_pool")
    assert item3["status"] == dq.STATUS_UNVERIFIABLE
    assert "POOL_LOG_MISSING_WITH_ROWS" in item3["reason_codes"]


def test_ok_with_zero_rows_is_unverifiable(session):
    session.query(LimitPool).delete()
    session.commit()
    result = dq.overview(session, target_date=D1, days=10)
    item = _source(result, "limit_pool")
    assert item["status"] == dq.STATUS_UNVERIFIABLE
    assert "UP_POOL_EMPTY_WITH_OK_LOG" in item["reason_codes"]


def test_sector_member_is_partial_without_completion_proof(session):
    result = dq.overview(session, target_date=D1, days=10)
    item = _source(result, "sector_member")
    assert item["status"] == dq.STATUS_PARTIAL
    assert item["completion_proof"] is False
    assert "NO_MEMBERS_COMPLETION_LOG" in item["reason_codes"]
    assert item["observed_rows"] == 1
    assert item["observed_sectors"] == 1
    assert item["expected_sectors"] == 2  # 当前目录里两个精选板块
    assert item["taxonomies"][0]["completion_proof"] is False

    empty = dq.overview(session, target_date=D2, days=10)
    assert _source(empty, "sector_member")["status"] == dq.STATUS_NOT_COLLECTED


def test_market_sentiment_null_fields_are_partial(session):
    session.query(MarketSentiment).filter(MarketSentiment.trade_date == D1).update(
        {"broken_rate": None}
    )
    session.commit()
    result = dq.overview(session, target_date=D1, days=10)
    item = _source(result, "market_sentiment")
    assert item["status"] == dq.STATUS_PARTIAL
    assert "SENTIMENT_FIELDS_NULL" in item["reason_codes"]
    assert item["missing_fields"] == ["broken_rate"]


def test_auction_missing_0926_is_partial(session):
    session.add(AuctionSnapshot(trade_date=D1, code="600000", price=10))
    session.add(
        CollectLog(
            trade_date=D1,
            task="auction_frame",
            status="ok",
            rows=1,
            message='{"scheduled_frame": "0925"}',
            created_at=datetime(2026, 11, 3, 9, 25),
        )
    )
    session.commit()
    item = _source(dq.overview(session, target_date=D1, days=10), "auction_snapshot")
    assert item["status"] == dq.STATUS_PARTIAL
    assert "AUCTION_0926_MISSING" in item["reason_codes"]
    assert item["has_0926"] is False


def test_auction_without_rows_is_not_collected(session):
    item = _source(dq.overview(session, target_date=D1, days=10), "auction_snapshot")
    assert item["status"] == dq.STATUS_NOT_COLLECTED
    assert "AUCTION_NOT_COLLECTED" in item["reason_codes"]
    assert item["observed_rows"] == 0


def test_trade_calendar_non_trade_and_future(session, monkeypatch):
    monkeypatch.setattr(dq, "today_cn", lambda: D3)
    weekend = date(2026, 11, 1)  # 早于 today 且不在日历里
    result = dq.overview(session, target_date=weekend, days=10)
    calendar = _source(result, "trade_calendar")
    assert calendar["status"] == dq.STATUS_NOT_TRADE_DAY
    assert calendar["is_trade_day"] is False

    future = dq.overview(session, target_date=FUTURE, days=10)
    assert _source(future, "trade_calendar")["status"] == dq.STATUS_PENDING
    assert all(item["status"] == dq.STATUS_PENDING for item in future["items"])


def test_timeline_is_per_day_and_validates_source(session, monkeypatch):
    monkeypatch.setattr(dq, "today_cn", lambda: D4)
    data = dq.timeline(session, source="limit_pool", days=4)
    assert data["source"] == "limit_pool"
    assert [point["trade_date"] for point in data["points"]] == [D1, D2, D3, D4]
    assert all(point["source"] == "limit_pool" for point in data["points"])
    with pytest.raises(ValueError):
        dq.timeline(session, source="not_a_source", days=4)
    with pytest.raises(ValueError):
        dq.timeline(session, source="limit_pool", days=0)


def test_days_cap_is_enforced(session):
    with pytest.raises(ValueError):
        dq.overview(session, target_date=D1, days=dq.MAX_DAYS + 1)


def test_service_is_read_only(session):
    before = (
        session.scalar(select(func.count()).select_from(LimitPool)),
        session.scalar(select(func.count()).select_from(StockDaily)),
        session.scalar(select(func.count()).select_from(CollectLog)),
        session.scalar(select(func.count()).select_from(SectorMember)),
        session.scalar(select(func.count()).select_from(AuctionSnapshot)),
    )
    dq.overview(session, target_date=D1, days=30)
    dq.timeline(session, source="sector_member", days=30)
    after = (
        session.scalar(select(func.count()).select_from(LimitPool)),
        session.scalar(select(func.count()).select_from(StockDaily)),
        session.scalar(select(func.count()).select_from(CollectLog)),
        session.scalar(select(func.count()).select_from(SectorMember)),
        session.scalar(select(func.count()).select_from(AuctionSnapshot)),
    )
    assert before == after


def test_no_external_source_is_touched(session, monkeypatch):
    import app.sources.kaipanhong as kph
    import app.sources.tencent as tencent

    def boom(*_args, **_kwargs):
        raise AssertionError("quality console must not call external sources")

    monkeypatch.setattr(tencent, "fetch_spot", boom)
    monkeypatch.setattr(kph.KaipanhongSource, "_post", boom)
    data = dq.overview(session, target_date=D1, days=10)
    assert data["items"]
    assert "database" in data
