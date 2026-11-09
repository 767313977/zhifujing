"""P0.9 数据覆盖状态测试（G-1 ~ G-5）。

覆盖四类「空结果」的区分、低位启动的行级 RPS 缺失、板块日期回退、板块计数的
集合关系、manifest 完整性门禁，以及管理员数据时点诊断。
"""

from datetime import date, timedelta

import pytest
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


def _calendar(session: Session, days: list[date]) -> None:
    for day in days:
        session.add(TradeCalendar(trade_date=day))


def _bars(session: Session, days: list[date], code: str, name: str = "测试") -> None:
    for index, day in enumerate(days):
        close = 10 * (1.01**index)
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
    session.add(StockBasic(code=code, name=name, industry="测试行业", free_float_shares=1e8))
    session.add(StockUniverse(code=code))


def _low_start_bars(session: Session, days: list[date], code: str, name: str = "低位") -> None:
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
    session.add(StockBasic(code=code, name=name, industry="测试行业", free_float_shares=1e8))
    session.add(StockUniverse(code=code))


def _manifest_table(session: Session, rows: list[tuple]) -> None:
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


def _sector_days(session: Session, days: list[date], boards=("A", "B", "C", "D", "E", "F")) -> None:
    for board in boards:
        for index, day in enumerate(days):
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


# ---------------------------------------------------------------- G-1 趋势覆盖


def test_trend_distinguishes_missing_rps_from_no_candidate():
    engine = _engine()
    start = date(2025, 1, 1)
    days = [start + timedelta(days=i) for i in range(260)]
    with Session(engine) as session:
        _calendar(session, days)
        _bars(session, days, "000001")
        session.commit()
        out = research_screen.trend_rows(session, trade_date=days[-1])
    assert out["rows"] == []
    assert out["coverage"]["data_status"] == "no_rps"
    assert out["coverage"]["daily_rows"] > 0
    assert out["coverage"]["rps_rows"] == 0
    assert out["coverage"]["status_text"]


def test_trend_reports_ok_when_no_candidate_qualifies():
    engine = _engine()
    start = date(2025, 1, 1)
    days = [start + timedelta(days=i) for i in range(260)]
    with Session(engine) as session:
        _calendar(session, days)
        _bars(session, days, "000001")
        session.add(RpsDaily(trade_date=days[-1], code="000001", rps60=10.0))
        session.commit()
        out = research_screen.trend_rows(session, trade_date=days[-1])
    assert out["rows"] == []
    assert out["coverage"]["data_status"] == "ok"
    assert out["coverage"]["candidates"] == 0
    assert "没有 RPS60" in out["coverage"]["note"]


def test_trend_marks_orphan_rps_date_as_no_bars():
    engine = _engine()
    start = date(2025, 1, 1)
    days = [start + timedelta(days=i) for i in range(260)]
    with Session(engine) as session:
        _calendar(session, days)
        _bars(session, days[:-1], "000001")
        # RPS 有 10-21（最后一天）、但底层日线只到前一天 —— 孤儿 RPS
        session.add(RpsDaily(trade_date=days[-1], code="000001", rps60=88.0))
        session.commit()
        out = research_screen.trend_rows(session, trade_date=days[-1])
    assert out["rows"] == []
    assert out["coverage"]["data_status"] == "no_bars"
    assert out["coverage"]["orphan_rps"] is True
    assert out["coverage"]["daily_rows"] == 0
    assert out["coverage"]["rps_rows"] == 1


def test_trend_marks_partial_rps_coverage():
    engine = _engine()
    start = date(2025, 1, 1)
    days = [start + timedelta(days=i) for i in range(260)]
    with Session(engine) as session:
        _calendar(session, days)
        for index in range(10):
            _bars(session, days, f"00000{index}")
            if index < 5:  # 只有一半的票有 RPS60
                session.add(RpsDaily(trade_date=days[-1], code=f"00000{index}", rps60=88.0))
        session.commit()
        out = research_screen.trend_rows(session, trade_date=days[-1])
    assert out["coverage"]["data_status"] == "partial_rps"
    assert out["coverage"]["rps_coverage"] == pytest.approx(0.5)
    assert out["rows"] == []


def test_trend_ok_status_keeps_counts():
    engine = _engine()
    start = date(2025, 1, 1)
    days = [start + timedelta(days=i) for i in range(260)]
    with Session(engine) as session:
        _calendar(session, days)
        _bars(session, days, "000001")
        session.add(RpsDaily(trade_date=days[-1], code="000001", rps20=70, rps60=88, rps120=80))
        session.commit()
        out = research_screen.trend_rows(session, trade_date=days[-1], limit=10)
    assert out["coverage"]["data_status"] == "ok"
    assert out["coverage"]["candidates"] == 1
    assert out["coverage"]["rps_coverage"] == 1.0
    assert out["rows"][0]["code"] == "000001"


# ------------------------------------------------------------ G-2 低位启动缺失


def test_low_start_marks_whole_day_rps_missing():
    engine = _engine()
    start = date(2025, 1, 1)
    days = [start + timedelta(days=i) for i in range(260)]
    with Session(engine) as session:
        _calendar(session, days)
        _low_start_bars(session, days, "000002")
        session.commit()
        out = research_screen.low_start_rows(session, trade_date=days[-1], limit=10)
    assert out["coverage"]["data_status"] == "no_rps"
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert row["rps20"] is None
    assert row["rps_status"] == "day_missing"
    assert any("当日 RPS 未采集" in item for item in row["missing"])
    assert out["coverage"]["rps_missing_rows"] == 1


def test_low_start_distinguishes_no_row_and_window():
    engine = _engine()
    start = date(2025, 1, 1)
    days = [start + timedelta(days=i) for i in range(260)]
    with Session(engine) as session:
        _calendar(session, days)
        _low_start_bars(session, days, "000002", name="低位A")
        _low_start_bars(session, days, "000003", name="低位B")
        # A 有 RPS 行但 rps20 为空（窗口不足）；B 完全没有 RPS 行
        session.add(RpsDaily(trade_date=days[-1], code="000002", rps60=30.0))
        session.commit()
        out = research_screen.low_start_rows(session, trade_date=days[-1], limit=10)
    assert out["coverage"]["data_status"] in {"ok", "partial_rps"}
    by_code = {row["code"]: row for row in out["rows"]}
    assert by_code["000002"]["rps_status"] == "window"
    assert any("20 日窗口覆盖不足" in item for item in by_code["000002"]["missing"])
    assert by_code["000003"]["rps_status"] == "no_row"
    assert any("无该股 RPS 记录" in item for item in by_code["000003"]["missing"])
    # 缺失不改变筛选：两只票都还在结果里
    assert len(out["rows"]) == 2


def test_low_start_reports_rps_value_without_missing():
    engine = _engine()
    start = date(2025, 1, 1)
    days = [start + timedelta(days=i) for i in range(260)]
    with Session(engine) as session:
        _calendar(session, days)
        _low_start_bars(session, days, "000002")
        session.add(RpsDaily(trade_date=days[-1], code="000002", rps20=61.0))
        session.commit()
        out = research_screen.low_start_rows(session, trade_date=days[-1], limit=10)
    row = out["rows"][0]
    assert row["rps20"] == 61.0
    assert row["rps_status"] == "value"
    assert row["missing"] == []
    assert out["coverage"]["rps_missing_rows"] == 0


# ---------------------------------------------------------------- G-3 日期回退


def test_sector_fallback_reports_actual_date():
    engine = _engine()
    start = date(2026, 9, 1)
    days = [start + timedelta(days=i) for i in range(30)]
    requested = days[-1] + timedelta(days=3)
    with Session(engine) as session:
        _calendar(session, days + [requested])
        _sector_days(session, days)
        session.commit()
        out = research_screen.sector_states(session, trade_date=requested, limit=10)
    assert out["requested_date"] == str(requested)
    assert out["actual_data_date"] == str(days[-1])
    assert out["trade_date"] == str(days[-1])
    assert out["fallback"]["applied"] is True
    assert out["fallback"]["gap_trading_days"] == 1
    assert out["coverage"]["data_status"] == "fallback"
    assert "已回退" in out["fallback"]["reason"]


def test_sector_strict_mode_refuses_fallback():
    engine = _engine()
    start = date(2026, 9, 1)
    days = [start + timedelta(days=i) for i in range(30)]
    gap_days = [days[-1] + timedelta(days=i) for i in range(1, 9)]
    requested = gap_days[-1]
    with Session(engine) as session:
        _calendar(session, days + gap_days)
        _sector_days(session, days)
        session.commit()
        out = research_screen.sector_states(
            session, trade_date=requested, limit=10, allow_fallback=False
        )
    assert out["rows"] == []
    assert out["coverage"]["data_status"] == "requested_date_unavailable"
    assert out["fallback"]["applied"] is True
    assert out["fallback"]["severity"] == "high"
    assert out["requested_date"] == str(requested)


def test_sector_big_gap_escalates_severity():
    engine = _engine()
    start = date(2026, 9, 1)
    days = [start + timedelta(days=i) for i in range(30)]
    gap_days = [days[-1] + timedelta(days=i) for i in range(1, 7)]
    requested = gap_days[-1]
    with Session(engine) as session:
        _calendar(session, days + gap_days)
        _sector_days(session, days)
        session.commit()
        out = research_screen.sector_states(session, trade_date=requested, limit=10)
    assert out["fallback"]["gap_trading_days"] == 6
    assert out["fallback"]["severity"] == "high"


# ------------------------------------------------------------ G-4 板块计数关系


def test_sector_counts_satisfy_set_relations():
    engine = _engine()
    start = date(2026, 9, 1)
    days = [start + timedelta(days=i) for i in range(30)]
    with Session(engine) as session:
        _calendar(session, days)
        _sector_days(session, days)  # A~F：30 天
        _sector_days(session, days[-10:], boards=("G",))  # G：只有 10 天
        session.commit()
        out = research_screen.sector_states(session, trade_date=days[-1], limit=10)
    coverage = out["coverage"]
    assert coverage["boards_in_day"] == 7
    assert coverage["boards_with_history"] == 6
    assert coverage["boards_with_rps20"] == 6
    assert coverage["excluded_short_history"] == 1
    assert coverage["excluded_no_rps20"] == 0
    assert coverage["matched"] == 6
    assert coverage["returned"] == 6
    assert coverage["truncated"] == 0
    # 集合关系：当日板块 = 有 RPS20 + 窗口不足 + 历史不足
    assert coverage["boards_in_day"] == (
        coverage["boards_with_rps20"]
        + coverage["excluded_no_rps20"]
        + coverage["excluded_short_history"]
    )


def test_sector_limit_truncation_is_reported():
    engine = _engine()
    start = date(2026, 9, 1)
    days = [start + timedelta(days=i) for i in range(30)]
    with Session(engine) as session:
        _calendar(session, days)
        _sector_days(session, days)
        session.commit()
        out = research_screen.sector_states(session, trade_date=days[-1], limit=3)
    assert len(out["rows"]) == 3
    assert out["coverage"]["returned"] == 3
    assert out["coverage"]["matched"] == 6
    assert out["coverage"]["truncated"] == 3


def test_sector_without_history_reports_insufficient():
    engine = _engine()
    start = date(2026, 9, 1)
    days = [start + timedelta(days=i) for i in range(10)]
    with Session(engine) as session:
        _calendar(session, days)
        _sector_days(session, days)
        session.commit()
        out = research_screen.sector_states(session, trade_date=days[-1], limit=10)
    assert out["rows"] == []
    assert out["coverage"]["data_status"] == "insufficient_history"


# ------------------------------------------------------------ G-5 manifest 门禁


def test_sector_manifest_partial_blocks_breadth_only():
    engine = _engine()
    start = date(2026, 9, 1)
    days = [start + timedelta(days=i) for i in range(30)]
    with Session(engine) as session:
        _calendar(session, days)
        _sector_days(session, days)
        session.add(
            SectorMember(trade_date=days[-1], sector_code="A", code="000001", name="x", pct_chg=3.0)
        )
        _manifest_table(session, [(days[-1], "A", "partial", 5, 3, 1)])
        session.commit()
        out = research_screen.sector_states(session, trade_date=days[-1], limit=10)
    row = {item["sector_code"]: item for item in out["rows"]}["A"]
    assert row["breadth_status"] == "partial"
    assert row["up_ratio"] is None
    assert row["state"] == "数据不足"
    assert row["state_daily"] != "数据不足"
    assert row["rps20"] is not None
    assert out["coverage"]["breadth_partial"] == 1


def test_sector_manifest_mismatch_blocks_complete():
    engine = _engine()
    start = date(2026, 9, 1)
    days = [start + timedelta(days=i) for i in range(30)]
    with Session(engine) as session:
        _calendar(session, days)
        _sector_days(session, days)
        session.add(
            SectorMember(trade_date=days[-1], sector_code="A", code="000001", name="x", pct_chg=3.0)
        )
        # manifest 说完整且来源 Count=5，但库里只有 1 行
        _manifest_table(session, [(days[-1], "A", "complete", 5, 5, 5)])
        session.commit()
        out = research_screen.sector_states(session, trade_date=days[-1], limit=10)
    row = {item["sector_code"]: item for item in out["rows"]}["A"]
    assert row["breadth_status"] == "mismatch"
    assert row["up_ratio"] is None
    assert "≠ 来源 5" in row["missing"][0]
    assert out["coverage"]["breadth_mismatch"] == 1


def test_sector_members_without_manifest_are_not_complete():
    engine = _engine()
    start = date(2026, 9, 1)
    days = [start + timedelta(days=i) for i in range(30)]
    with Session(engine) as session:
        _calendar(session, days)
        _sector_days(session, days)
        session.add(
            SectorMember(trade_date=days[-1], sector_code="A", code="000001", name="x", pct_chg=3.0)
        )
        session.commit()
        out = research_screen.sector_states(session, trade_date=days[-1], limit=10)
    row = {item["sector_code"]: item for item in out["rows"]}["A"]
    assert row["breadth_status"] == "missing_manifest"
    assert row["up_ratio"] is None
    assert out["coverage"]["manifest_installed"] is False
    assert out["coverage"]["breadth_unknown"] == 6


def test_sector_manifest_failed_and_unsupported():
    engine = _engine()
    start = date(2026, 9, 1)
    days = [start + timedelta(days=i) for i in range(30)]
    with Session(engine) as session:
        _calendar(session, days)
        _sector_days(session, days)
        session.add(
            SectorMember(trade_date=days[-1], sector_code="A", code="000001", name="x", pct_chg=3.0)
        )
        session.add(
            SectorMember(trade_date=days[-1], sector_code="B", code="000002", name="y", pct_chg=1.0)
        )
        _manifest_table(
            session,
            [
                (days[-1], "A", "failed", 0, 0, 0),
                (days[-1], "B", "unsupported_history", 0, 0, 0),
            ],
        )
        session.commit()
        out = research_screen.sector_states(session, trade_date=days[-1], limit=10)
    by_code = {item["sector_code"]: item for item in out["rows"]}
    assert by_code["A"]["breadth_status"] == "failed"
    assert "采集失败" in by_code["A"]["missing"][0]
    assert by_code["B"]["breadth_status"] == "unsupported_history"
    assert "不支持" in by_code["B"]["missing"][0]


# ------------------------------------------------------------ G-6 数据时点诊断


def test_data_health_lists_orphan_and_missing_rps_dates():
    engine = _engine()
    start = date(2026, 10, 8)
    days = [start + timedelta(days=i) for i in range(3)]
    with Session(engine) as session:
        _calendar(session, days)
        _bars(session, days[:2], "000001")  # 行情只到第二天
        session.add(RpsDaily(trade_date=days[0], code="000001", rps60=80))
        session.add(RpsDaily(trade_date=days[2], code="000001", rps60=80))  # 孤儿
        session.commit()
        health = research_screen.data_health(session)
    assert health["bars_latest"] == str(days[1])
    assert health["rps_latest"] == str(days[2])
    assert health["rps_newer_than_bars"] is True
    assert health["orphan_rps_dates_count"] == 1
    assert health["orphan_rps_dates"][0]["trade_date"] == str(days[2])
    assert health["missing_rps_dates_count"] == 1
    assert health["missing_rps_dates"][0] == str(days[1])


def test_meta_data_health_is_admin_only(tmp_path):
    from types import SimpleNamespace

    from app.api import research as research_api
    from app.services import data_revision

    engine = create_engine(f"sqlite:///{tmp_path / 'meta.db'}", future=True)
    Base.metadata.create_all(engine)
    day = date(2026, 10, 9)
    with Session(engine) as session:
        session.add(StockDaily(trade_date=day, code="000001", name="x", close=10, pct_chg=0, amount=1))
        session.add(RpsDaily(trade_date=day, code="000001", rps60=80))
        session.commit()
        data_revision.install(session)
        normal = research_api.meta(user=SimpleNamespace(is_admin=False), db=session)
        admin = research_api.meta(user=SimpleNamespace(is_admin=True), db=session)
    assert "data_health" not in normal
    assert admin["data_health"]["bars_latest"] == str(day)
    assert "cache" in admin
