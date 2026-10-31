"""交易日口径只有一处权威来源（批次 0.1，2026-10-31）。

钉两件事：

1. `services.trading_days` 的 `previous_trade_day` / `recent_trade_days` 按**交易日历**
   取、不含 `end` 当天、由近及远 —— 所有调用点都转调它们；
2. 不再有第二套实现：各模块里私有的 `_prev_trade_date` / `_previous_trade_day`
   已经删掉，`DailyCollector.is_trade_day` 也只是转调共用版。
"""

import inspect
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.jobs import collect_daily
from app.models import Base, TradeCalendar
from app.services.trading_days import previous_trade_day, recent_trade_days

APP = Path(__file__).resolve().parents[1] / "backend" / "app"
CALENDAR = [
    date(2026, 10, 26),  # 周一
    date(2026, 10, 27),
    date(2026, 10, 28),
    date(2026, 10, 29),
    date(2026, 10, 30),
]


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'trade-days.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([TradeCalendar(trade_date=day) for day in CALENDAR])
        session.commit()
        yield session


def test_previous_trade_day_returns_nearest_earlier_day(db):
    """返回的是「日历里最近的更早一天」，不是简单的日期减一。"""
    assert previous_trade_day(date(2026, 10, 27), db) == date(2026, 10, 26)


def test_previous_trade_day_excludes_the_day_itself(db):
    assert previous_trade_day(date(2026, 10, 30), db) == date(2026, 10, 29)


def test_previous_trade_day_returns_none_before_calendar(db):
    assert previous_trade_day(date(2026, 10, 26), db) is None


def test_previous_trade_day_handles_non_trade_day(db):
    """传进来的是周六：仍然取它之前最近的交易日，不会返回 None。"""
    assert previous_trade_day(date(2026, 10, 31), db) == date(2026, 10, 30)


def test_recent_trade_days_is_newest_first_and_limited(db):
    assert recent_trade_days(date(2026, 10, 30), 3, db) == [
        date(2026, 10, 29),
        date(2026, 10, 28),
        date(2026, 10, 27),
    ]
    assert recent_trade_days(date(2026, 10, 30), 0, db) == []


def test_daily_collector_delegates_is_trade_day():
    """采集器上的方法只是薄壳，SQL 只在共用版里。"""
    source = inspect.getsource(collect_daily.DailyCollector.is_trade_day)
    assert "return is_trade_day(day)" in source
    assert "select(" not in source


def test_no_private_previous_trade_day_copies():
    """全仓（backend/）不许再出现私有的「上一交易日」查询实现。"""
    offenders = []
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "def _prev_trade_date(" in text or "def _previous_trade_day(" in text:
            offenders.append(path.relative_to(APP).as_posix())
    assert not offenders, f"这些文件还在自己查上一交易日：{offenders}"


def test_no_inline_is_trade_day_queries():
    """「是否交易日」的 SQL 也只许出现在 `trading_days.py` 里。"""
    offenders = []
    for path in APP.rglob("*.py"):
        if path.name == "trading_days.py":
            continue
        if "TradeCalendar.trade_date ==" in path.read_text(encoding="utf-8"):
            offenders.append(path.relative_to(APP).as_posix())
    assert not offenders, f"这些文件还在自己查是不是交易日：{offenders}"
