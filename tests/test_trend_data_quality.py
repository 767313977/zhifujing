"""F2 数据质量闸门：RPS 混版本 / 孤儿日 / 覆盖率不足必须被识别。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, RpsDaily, SectorDaily, StockDaily
from app.services.trend_engine import data_quality

DAY = date(2026, 10, 21)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'dq.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _bars(session, count: int, day: date = DAY) -> None:
    for index in range(count):
        session.add(
            StockDaily(
                trade_date=day,
                code=f"{index:06d}",
                name="X",
                close=10.0,
                pct_chg=0.0,
                amount=1e8,
                volume=1e6,
            )
        )
    session.flush()


def test_no_bars_and_orphan_rps(db) -> None:
    status = data_quality.rps_day_status(db, DAY)
    assert status.status == "no_bars"

    db.add(RpsDaily(trade_date=DAY, code="000001", rps120=50.0, rps250=60.0))
    db.flush()
    orphan = data_quality.rps_day_status(db, DAY)
    assert orphan.status == "orphan_rps"
    assert orphan.ok is False


def test_missing_rps_and_missing_rps60(db) -> None:
    _bars(db, 5)
    assert data_quality.rps_day_status(db, DAY).status == "no_rps"

    for index in range(5):
        db.add(RpsDaily(trade_date=DAY, code=f"{index:06d}", rps120=50.0, rps250=60.0))
    db.flush()
    # 与生产 2026-10-21 同型：120/250 有值，20/60 全空
    status = data_quality.rps_day_status(db, DAY)
    assert status.status == "no_rps60"
    assert status.ok is False


def test_partial_and_ok_coverage(db) -> None:
    _bars(db, 10)
    for index in range(8):  # 80% 覆盖 → 不足
        db.add(RpsDaily(trade_date=DAY, code=f"{index:06d}", rps20=50.0, rps60=50.0))
    db.flush()
    partial = data_quality.rps_day_status(db, DAY)
    assert partial.status == "partial_rps"
    assert partial.ok is False

    for index in range(8, 10):
        db.add(RpsDaily(trade_date=DAY, code=f"{index:06d}", rps20=50.0, rps60=50.0))
    db.flush()
    ok = data_quality.rps_day_status(db, DAY)
    assert ok.status == "ok"
    assert ok.ok is True


def test_coverage_flags_reports_sector_staleness(db) -> None:
    _bars(db, 5)
    for index in range(5):
        db.add(RpsDaily(trade_date=DAY, code=f"{index:06d}", rps20=50.0, rps60=50.0))
    db.add(
        SectorDaily(
            trade_date=DAY - timedelta(days=2),
            sector_code="A",
            name="A",
            taxonomy="kph_selected",
            pct_chg=1.0,
        )
    )
    db.flush()
    flags = data_quality.coverage_flags(db, DAY)
    assert flags["rps"]["ok"] is True
    assert flags["sector_stale"] is True
    assert flags["sector_actual_date"] == str(DAY - timedelta(days=2))
