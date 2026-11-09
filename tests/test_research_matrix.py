"""P1.0-04 历史数据可用性矩阵：三个研究接口 × 11 种数据状态。

同一份构造库上同时调用 trend / low-start / sector-state，确认：
数据不足与无候选不混淆、NULL 不当 0、无历史前视、实际数据日明确、
manifest 门禁只影响广度、缓存不掩盖状态变化。
"""

from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    Base,
    RpsDaily,
    SectorDaily,
    SectorMember,
    StockBasic,
    StockDaily,
    StockUniverse,
    TradeCalendar,
)
from app.services import research_screen


def _engine():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return engine


def _calendar(session, days):
    for day in days:
        session.add(TradeCalendar(trade_date=day))


def _bars(session, days, code, name="样本", start=10.0):
    for index, day in enumerate(days):
        close = start * (1.01**index)
        session.add(
            StockDaily(
                trade_date=day,
                code=code,
                name=name,
                open=close,
                high=close * 1.01,
                low=close * 0.99,
                close=close,
                pct_chg=1.0 if index else 0.0,
                amount=1e8,
            )
        )
    session.add(StockBasic(code=code, name=name, industry="测试", free_float_shares=1e8))
    session.add(StockUniverse(code=code))


def _low_bars(session, days, code, name="低位"):
    closes = []
    for index in range(len(days)):
        if index < len(days) - 60:
            closes.append(30 - (15 * index / (len(days) - 61)))
        elif index < len(days) - 1:
            closes.append(15.0)
        else:
            closes.append(15.6)
    for index, day in enumerate(days):
        close = closes[index]
        prev = closes[index - 1] if index else close
        session.add(
            StockDaily(
                trade_date=day,
                code=code,
                name=name,
                open=close,
                high=close * 1.01,
                low=close * 0.99,
                close=close,
                pct_chg=(close / prev - 1) * 100 if prev else 0.0,
                amount=1e8 * (2 if index == len(days) - 1 else 1),
            )
        )
    session.add(StockBasic(code=code, name=name, industry="测试", free_float_shares=1e8))
    session.add(StockUniverse(code=code))


def _sector_days(session, days, boards=("A", "B", "C", "D", "E", "F")):
    for board in boards:
        for day in days:
            session.add(
                SectorDaily(
                    trade_date=day,
                    sector_code=board,
                    name=f"板块{board}",
                    taxonomy="kph_selected",
                    pct_chg=1.0 if board == "A" else 0.5,
                    amount=1e8,
                )
            )


def _manifest(session, rows):
    session.connection().exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS sector_member_backfill ("
        " trade_date TEXT NOT NULL, sector_code TEXT NOT NULL, board_name TEXT, taxonomy TEXT,"
        " status TEXT NOT NULL, reported INTEGER, unique_count INTEGER, written INTEGER,"
        " attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, updated_at TEXT NOT NULL,"
        " PRIMARY KEY (trade_date, sector_code))"
    )
    for day, board, status, reported, unique, written in rows:
        session.connection().exec_driver_sql(
            "INSERT INTO sector_member_backfill"
            " (trade_date, sector_code, status, reported, unique_count, written, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(day), board, status, reported, unique, written, "2026-11-09T00:00:00"),
        )


def _world(session, *, bars_days=260, sector_days=30):
    start = date(2025, 1, 1)
    days = [start + timedelta(days=i) for i in range(bars_days)]
    _calendar(session, days)
    _low_bars(session, days, "000002", "低位A")
    _sector = days[-sector_days:]
    _sector_days(session, _sector)
    return days


def _all_three(session, day, **sector_kwargs):
    return (
        research_screen.trend_rows(session, trade_date=day, limit=10),
        research_screen.low_start_rows(session, trade_date=day, limit=10),
        research_screen.sector_states(session, trade_date=day, limit=10, **sector_kwargs),
    )


def test_matrix_all_complete():
    engine = _engine()
    with Session(engine) as session:
        days = _world(session)
        day = days[-1]
        _bars(session, days, "000001")
        session.add(RpsDaily(trade_date=day, code="000001", rps20=70, rps60=88, rps120=80))
        session.add(RpsDaily(trade_date=day, code="000002", rps20=65, rps60=70))
        session.add(
            SectorMember(trade_date=day, sector_code="A", code="000001", name="x", pct_chg=3.0)
        )
        _manifest(session, [(day, "A", "complete", 1, 1, 1)])
        session.commit()
        trend, low, sector = _all_three(session, day)
    assert trend["coverage"]["data_status"] == "ok"
    assert trend["rows"]
    assert low["coverage"]["data_status"] == "ok"
    assert all(row["missing"] == [] for row in low["rows"])
    assert sector["coverage"]["data_status"] == "ok"
    assert sector["fallback"]["applied"] is False
    assert {row["sector_code"]: row for row in sector["rows"]}["A"]["up_ratio"] == 100.0


def test_matrix_bars_without_rps():
    engine = _engine()
    with Session(engine) as session:
        days = _world(session)
        day = days[-1]
        _bars(session, days, "000001")  # 有日线
        session.commit()
        trend, low, _ = _all_three(session, day)
    assert trend["coverage"]["data_status"] == "no_rps"
    assert trend["rows"] == []
    assert low["coverage"]["data_status"] == "no_rps"
    assert low["rows"][0]["rps_status"] == "day_missing"
    assert "当日 RPS 未采集" in low["rows"][0]["missing"][0]


def test_matrix_rps_without_bars():
    engine = _engine()
    with Session(engine) as session:
        days = _world(session)
        orphan = days[-1] + timedelta(days=3)
        session.add(TradeCalendar(trade_date=orphan))
        session.add(RpsDaily(trade_date=orphan, code="000001", rps60=80))
        session.commit()
        trend, low, sector = _all_three(session, orphan)
    assert trend["coverage"]["data_status"] == "no_bars"
    assert trend["coverage"]["orphan_rps"] is True
    assert low["coverage"]["data_status"] == "no_bars"
    assert low["rows"] == []
    assert sector["fallback"]["applied"] is True


def test_matrix_ret20_valid_ret60_missing():
    engine = _engine()
    with Session(engine) as session:
        days = _world(session)
        day = days[-1]
        # 20 日有值、60 日为空：既不是「当日没采」，也不能当 0
        session.add(RpsDaily(trade_date=day, code="000002", rps20=72, rps60=None))
        session.commit()
        trend, low, _ = _all_three(session, day)
    assert trend["coverage"]["rps_rows"] == 1
    assert trend["coverage"]["rps60_rows"] == 0
    assert trend["coverage"]["data_status"] == "no_rps60"
    row = low["rows"][0]
    assert row["rps20"] == 72
    assert row["rps_status"] == "value"
    assert row["missing"] == []


def test_matrix_sector_daily_missing():
    engine = _engine()
    with Session(engine) as session:
        days = _world(session)
        day = days[-1] + timedelta(days=2)
        session.add(TradeCalendar(trade_date=day))
        session.commit()
        _, _, sector = _all_three(session, day)
    assert sector["coverage"]["data_status"] == "fallback"
    assert sector["actual_data_date"] == str(days[-1])
    assert sector["requested_date"] == str(day)
    assert sector["fallback"]["severity"] in {"warn", "high"}


def test_matrix_members_without_manifest():
    engine = _engine()
    with Session(engine) as session:
        days = _world(session)
        day = days[-1]
        session.add(
            SectorMember(trade_date=day, sector_code="A", code="000001", name="x", pct_chg=3.0)
        )
        session.commit()
        _, _, sector = _all_three(session, day)
    row = {item["sector_code"]: item for item in sector["rows"]}["A"]
    assert row["breadth_status"] == "missing_manifest"
    assert row["up_ratio"] is None
    assert row["rps20"] is not None  # 日线技术状态不被 manifest 抹掉
    assert row["state_daily"] != "数据不足"


def test_matrix_manifest_complete_and_consistent():
    engine = _engine()
    with Session(engine) as session:
        days = _world(session)
        day = days[-1]
        session.add(
            SectorMember(trade_date=day, sector_code="A", code="000001", name="x", pct_chg=3.0)
        )
        _manifest(session, [(day, "A", "complete", 1, 1, 1)])
        session.commit()
        _, _, sector = _all_three(session, day)
    row = {item["sector_code"]: item for item in sector["rows"]}["A"]
    assert row["breadth_status"] == "complete"
    assert row["up_ratio"] == 100.0
    assert row["missing"] == []


def test_matrix_manifest_complete_but_count_mismatch():
    engine = _engine()
    with Session(engine) as session:
        days = _world(session)
        day = days[-1]
        session.add(
            SectorMember(trade_date=day, sector_code="A", code="000001", name="x", pct_chg=3.0)
        )
        _manifest(session, [(day, "A", "complete", 3, 3, 3)])
        session.commit()
        _, _, sector = _all_three(session, day)
    row = {item["sector_code"]: item for item in sector["rows"]}["A"]
    assert row["breadth_status"] == "mismatch"
    assert row["up_ratio"] is None
    assert row["rps20"] is not None


def test_matrix_request_after_latest_bars():
    engine = _engine()
    with Session(engine) as session:
        days = _world(session)
        future = days[-1] + timedelta(days=10)
        session.add(TradeCalendar(trade_date=future))
        session.commit()
        trend, low, sector = _all_three(session, future)
    assert trend["coverage"]["data_status"] == "no_bars"
    assert low["coverage"]["data_status"] == "no_bars"
    assert sector["fallback"]["applied"] is True


def test_matrix_strict_date_mode():
    engine = _engine()
    with Session(engine) as session:
        days = _world(session)
        day = days[-1] + timedelta(days=6)
        session.add(TradeCalendar(trade_date=day))
        session.commit()
        _, _, sector = _all_three(session, day, allow_fallback=False)
    assert sector["rows"] == []
    assert sector["coverage"]["data_status"] == "requested_date_unavailable"
    assert sector["fallback"]["applied"] is True


def test_matrix_no_lookahead():
    """截止日之后的数据不得影响结果（含 RPS 与日线）。"""
    engine = _engine()
    with Session(engine) as session:
        days = _world(session)
        cutoff = days[-3]
        _bars(session, days, "000001")
        session.add(RpsDaily(trade_date=cutoff, code="000001", rps60=80))
        session.add(RpsDaily(trade_date=cutoff, code="000002", rps60=70))
        # 截止日之后的 RPS 不得被用到
        session.add(RpsDaily(trade_date=days[-1], code="000001", rps60=99))
        session.commit()
        trend, low, _ = _all_three(session, cutoff)
    assert trend["coverage"]["rps60_rows"] == 2
    rows = {row["code"]: row for row in trend["rows"]}
    assert rows["000001"]["rps60"] == 80
    assert low["coverage"]["rps60_rows"] == 2
