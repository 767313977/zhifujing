"""P0.2 研究指标服务测试：趋势 / 低位启动 / 板块状态。"""

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


def _calendar(session: Session, days: list[date]) -> None:
    """`trend.load_bars` 按交易日历取窗口，必须先把日历写进库。"""
    for day in days:
        session.add(TradeCalendar(trade_date=day))


def _seed_trend(session: Session, days: list[date], code: str = "000001", name: str = "测试"):
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
                amount=1e8 * (1 + index / 100),
            )
        )
    session.add(RpsDaily(trade_date=days[-1], code=code, rps20=90, rps60=88, rps120=80))
    session.add(StockBasic(code=code, name=name, industry="测试行业", free_float_shares=1e8))
    session.add(StockUniverse(code=code))


def test_trend_rows_returns_explainable_features():
    engine = _engine()
    start = date(2025, 1, 1)
    days = [start + timedelta(days=i) for i in range(260)]
    with Session(engine) as session:
        _calendar(session, days)
        _seed_trend(session, days)
        session.commit()
        out = research_screen.trend_rows(session, trade_date=days[-1], limit=10)
    assert out["trade_date"] == days[-1]
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert row["state"] == "技术特征符合"
    assert row["rps60"] == 88
    assert row["reasons"]
    assert row["strategy"]["status"].startswith("研究假设")


def test_low_start_requires_low_position_and_platform():
    engine = _engine()
    start = date(2025, 1, 1)
    days = [start + timedelta(days=i) for i in range(260)]
    with Session(engine) as session:
        _calendar(session, days)
        closes = []
        for index in range(260):
            if index < 200:
                closes.append(30 - (15 * index / 199))
            elif index < 259:
                closes.append(15.0)
            else:
                closes.append(15.6)
        for index, day in enumerate(days):
            close = closes[index]
            prev = closes[index - 1] if index else close
            session.add(
                StockDaily(
                    trade_date=day,
                    code="000002",
                    name="低位",
                    open=close,
                    high=close * 1.01,
                    low=close * 0.99,
                    close=close,
                    pct_chg=(close / prev - 1) * 100 if prev else 0.0,
                    amount=1e8 * (2 if index == len(days) - 1 else 1),
                )
            )
        session.add(RpsDaily(trade_date=days[-1], code="000002", rps20=60, rps60=40, rps120=30))
        session.add(StockBasic(code="000002", name="低位", industry="测试行业", free_float_shares=1e8))
        session.add(StockUniverse(code="000002"))
        session.commit()
        out = research_screen.low_start_rows(session, trade_date=days[-1], limit=10)
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert row["pos_pct"] < 40
    assert row["platform_days"] >= 15
    assert row["state"] in {"突破确认", "启动候选"}


def test_trend_rows_filters_st_and_bse():
    engine = _engine()
    start = date(2025, 1, 1)
    days = [start + timedelta(days=i) for i in range(260)]
    with Session(engine) as session:
        _calendar(session, days)
        _seed_trend(session, days, code="000001")
        _seed_trend(session, days, code="600001", name="ST测试")
        _seed_trend(session, days, code="830001", name="北交所")
        session.commit()
        out = research_screen.trend_rows(session, trade_date=days[-1], limit=10)
    assert [row["code"] for row in out["rows"]] == ["000001"]


def test_low_start_uses_only_data_up_to_trade_date():
    engine = _engine()
    start = date(2025, 1, 1)
    days = [start + timedelta(days=i) for i in range(260)]
    cutoff = days[250]
    with Session(engine) as session:
        _calendar(session, days)
        closes = []
        for index in range(260):
            if index < 200:
                closes.append(30 - (15 * index / 199))
            elif index <= 250:
                closes.append(15.0)
            else:
                closes.append(15.0 * (1.04 ** (index - 250)))
        for index, day in enumerate(days):
            close = closes[index]
            prev = closes[index - 1] if index else close
            session.add(
                StockDaily(
                    trade_date=day,
                    code="000002",
                    name="低位",
                    open=close,
                    high=close * 1.01,
                    low=close * 0.99,
                    close=close,
                    pct_chg=(close / prev - 1) * 100 if prev else 0.0,
                    amount=1e8,
                )
            )
        session.add(RpsDaily(trade_date=cutoff, code="000002", rps20=60, rps60=40, rps120=30))
        session.add(StockBasic(code="000002", name="低位", industry="测试行业", free_float_shares=1e8))
        session.add(StockUniverse(code="000002"))
        session.commit()
        out = research_screen.low_start_rows(session, trade_date=cutoff, limit=10)
    assert out["trade_date"] == cutoff
    row = out["rows"][0]
    # 截止日之后 9 天的暴涨不能出现在结果里
    assert row["trade_date"] == str(cutoff)
    assert row["close"] == 15.0
    assert row["state"] == "低位观察"


def test_sector_states_marks_missing_breadth():
    engine = _engine()
    start = date(2025, 1, 1)
    days = [start + timedelta(days=i) for i in range(61)]
    with Session(engine) as session:
        for board, pct in (("A", 2.0), ("B", 1.0), ("C", -1.0), ("D", 0.0), ("E", -0.5), ("F", 0.5)):
            for day in days:
                session.add(
                    SectorDaily(
                        trade_date=day,
                        sector_code=board,
                        name=f"板块{board}",
                        taxonomy="kph_selected",
                        pct_chg=pct,
                        amount=1e8,
                    )
                )
        session.add(
            SectorMember(
                trade_date=days[-1], sector_code="A", code="000001", name="测试", pct_chg=3.0
            )
        )
        session.commit()
        out = research_screen.sector_states(session, trade_date=days[-1], limit=10)
    assert out["trade_date"] == str(days[-1])
    assert len(out["rows"]) == 6
    by_code = {row["sector_code"]: row for row in out["rows"]}
    assert by_code["A"]["state"] != "数据不足"
    assert by_code["A"]["up_ratio"] == 100.0
    assert by_code["B"]["state"] == "数据不足"


def test_result_cache_hits_and_invalidates_on_data_version(tmp_path, monkeypatch):
    """文件库走缓存；数据版本（最新日线日）变化后必须重算。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'cache.db'}", future=True)
    Base.metadata.create_all(engine)
    day = date(2026, 9, 24)

    def _seed(when: date) -> None:
        with Session(engine) as session:
            session.add(
                StockDaily(
                    trade_date=when,
                    code="000001",
                    name="测试",
                    open=10,
                    high=10.2,
                    low=9.8,
                    close=10,
                    pct_chg=0.0,
                    amount=1e8,
                )
            )
            session.commit()

    _seed(day)
    calls = {"n": 0}

    def fake(*args, **kwargs):
        calls["n"] += 1
        return {"trade_date": day, "rows": [], "coverage": {}, "strategy": {}}

    monkeypatch.setattr(research_screen, "_low_start_rows", fake)
    research_screen.clear_cache()
    with Session(engine) as session:
        research_screen.low_start_rows(session, trade_date=day)
        research_screen.low_start_rows(session, trade_date=day)
    assert calls["n"] == 1

    _seed(date(2026, 9, 25))
    with Session(engine) as session:
        research_screen.low_start_rows(session, trade_date=day)
    assert calls["n"] == 2
