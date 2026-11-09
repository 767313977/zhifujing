"""B0.2 成分股回补：完整性判定、幂等 manifest、历史板块目录。"""

import importlib.util
import sqlite3
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, SectorBasic, SectorDaily, StockDaily, TradeCalendar

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_sector_members.py"
spec = importlib.util.spec_from_file_location("backfill_sector_members", SCRIPT)
backfill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backfill)


@pytest.mark.parametrize(
    "reported,unique,written,error,expected",
    [
        (100, 100, 100, None, "complete"),
        (100, 80, 80, None, "partial"),
        (100, 0, 0, None, "failed"),
        (0, 0, 0, None, "source_empty"),
        (None, 0, 0, None, "unverified"),
        (None, 10, 10, None, "unverified"),
        (None, 0, 0, "RuntimeError: errcode=1020", "unsupported_history"),
        (100, 90, 90, "TimeoutError: timeout", "failed"),
    ],
)
def test_classify_status(reported, unique, written, error, expected):
    status, _note = backfill._classify(reported, unique, written, error)
    assert status == expected


def test_refuses_main_db(tmp_path):
    assert backfill._is_main_db(backfill.ROOT / "backend" / "data" / "fupan.db")
    assert not backfill._is_main_db(tmp_path / "trial.db")


def test_manifest_upsert_is_idempotent(tmp_path):
    con = sqlite3.connect(str(tmp_path / "m.db"))
    backfill._ensure_manifest(con)
    item = {
        "trade_date": "2026-09-24",
        "sector_code": "801001",
        "board_name": "芯片",
        "taxonomy": "kph_selected",
        "status": "partial",
        "reported": 100,
        "unique": 90,
        "written": 90,
        "attempts": 1,
        "last_error": "short",
    }
    backfill._upsert_manifest(con, item)
    item.update({"status": "complete", "unique": 100, "written": 100, "last_error": None})
    backfill._upsert_manifest(con, item)
    rows = backfill._manifest_rows(con)
    assert len(rows) == 1
    assert rows[("2026-09-24", "801001")]["status"] == "complete"
    con.close()


def test_day_boards_uses_sector_daily_not_current_directory():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    day = date(2026, 9, 24)
    with Session(engine) as session:
        session.add_all(
            [
                # 当天实际存在的两个板块
                SectorDaily(trade_date=day, sector_code="801001", name="芯片", taxonomy="kph_selected"),
                SectorDaily(trade_date=day, sector_code="801002", name="算力", taxonomy="kph_selected"),
                # 当前目录里还有一个「今天才新增」的板块，不应进入当天计划
                SectorBasic(code="801999", name="新板块", taxonomy="kph_selected"),
            ]
        )
        session.commit()
        boards = backfill._day_boards(session, day)
    assert [item["code"] for item in boards] == ["801001", "801002"]


def test_day_boards_top_and_taxonomy():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    day = date(2026, 9, 24)
    with Session(engine) as session:
        session.add_all(
            [
                SectorDaily(trade_date=day, sector_code="801001", name="强", taxonomy="kp", strength=90),
                SectorDaily(trade_date=day, sector_code="801002", name="中", taxonomy="kp", strength=70),
                SectorDaily(trade_date=day, sector_code="801003", name="弱", taxonomy="kp", strength=50),
                SectorDaily(trade_date=day, sector_code="901001", name="行业", taxonomy="ind", strength=99),
            ]
        )
        session.commit()
        boards = backfill._day_boards(session, day, taxonomy="kp", top=2, order="strength")
    assert [item["code"] for item in boards] == ["801001", "801002"]


def test_target_days_does_not_silently_truncate():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    days = [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23)]
    with Session(engine) as session:
        session.add_all(TradeCalendar(trade_date=day) for day in days)
        session.add(
            StockDaily(trade_date=days[-1], code="000001", name="x", close=10, pct_chg=1.0)
        )
        session.commit()
        args = SimpleNamespace(end=None, start=days[0], days=3, max_days=1)
        with pytest.raises(SystemExit):
            backfill._target_days(session, args)
