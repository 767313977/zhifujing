"""涨跌家数补写：日期对得上才写，对不上记 skipped（2026-10-22 复查 #1）。"""

from contextlib import contextmanager
from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.jobs.collect_daily import DailyCollector
from app.models import Base, MarketSentiment

DAY = date(2026, 10, 21)


class _FakeAk:
    def __init__(self, activity: dict) -> None:
        self.activity = activity
        self.calls = 0

    def market_activity(self) -> dict:
        self.calls += 1
        return self.activity


def _engine():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(MarketSentiment(trade_date=DAY, limit_up_count=50))
        session.commit()
    return engine


def _patch_scope(monkeypatch, engine) -> None:
    @contextmanager
    def scope():
        with Session(engine) as session:
            yield session
            session.commit()

    monkeypatch.setattr("app.jobs.collect_daily.session_scope", scope)


def _collector(ak: _FakeAk) -> DailyCollector:
    collector = DailyCollector.__new__(DailyCollector)
    collector.ak = ak
    collector._step_note = None
    collector._step_status = None
    return collector


def test_writes_when_date_matches(monkeypatch):
    engine = _engine()
    _patch_scope(monkeypatch, engine)
    ak = _FakeAk(
        {"统计日期": datetime(2026, 10, 21, 15, 0), "上涨": 3200, "下跌": 1900}
    )

    assert _collector(ak).collect_activity(DAY) == 1
    with Session(engine) as session:
        row = session.get(MarketSentiment, DAY)
        assert row.up_count == 3200
        assert row.down_count == 1900


def test_skips_stale_snapshot(monkeypatch):
    engine = _engine()
    _patch_scope(monkeypatch, engine)
    collector = _collector(
        _FakeAk({"统计日期": datetime(2026, 10, 20, 15, 0), "上涨": 1, "下跌": 2})
    )

    assert collector.collect_activity(DAY) == 0
    assert collector._step_status == "skipped"
    assert "2026-10-20" in collector._step_note
    with Session(engine) as session:
        row = session.get(MarketSentiment, DAY)
        assert row.up_count is None
        assert row.down_count is None


def test_skips_when_already_filled(monkeypatch):
    engine = _engine()
    _patch_scope(monkeypatch, engine)
    with Session(engine) as session:
        session.get(MarketSentiment, DAY).up_count = 1
        session.commit()
    ak = _FakeAk({})

    assert _collector(ak).collect_activity(DAY) == 0
    assert ak.calls == 0
