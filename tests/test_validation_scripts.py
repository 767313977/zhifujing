"""B1 验证脚本的纯函数与分组逻辑测试。"""

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, SectorDaily
from app.services.research_backtest import Sample, Signal

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


trend = _load("validate_trend")
sector = _load("validate_sector_resonance")


def _days(n: int):
    start = date(2026, 1, 5)
    return [start + timedelta(days=i) for i in range(n)]


def test_board_metrics_rps_and_strong_days():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    days = _days(25)
    boards = [("A", 3.0), ("B", -1.0), ("C", 0.5), ("D", 0.0), ("E", -0.5), ("F", 1.0)]
    with Session(engine) as session:
        for day in days:
            session.add_all(
                [
                    SectorDaily(trade_date=day, sector_code=code, name=code, taxonomy="t", pct_chg=pct)
                    for code, pct in boards
                ]
            )
        session.commit()
        metrics = sector._board_metrics(session, "t", days[0], days[-1])
    last = metrics[("A", days[-1])]
    assert last["rps20"] > 80
    assert last["strong_days"] >= 3
    assert abs(last["delta5"]) < 1e-9
    assert metrics[("B", days[-1])]["rps20"] < 20


def test_variant_filters_top_and_min_rps():
    day = date(2026, 1, 5)
    signals = [
        Signal("000001", day, {"rps": 95}),
        Signal("000002", day, {"rps": 85}),
        Signal("000003", day, {"rps": 75}),
        Signal("000004", day, {"rps": 65}),
    ]
    assert [s.code for s in trend._variant(signals, 2, 70)] == ["000001", "000002"]
    assert [s.code for s in trend._variant(signals, 10, 90)] == ["000001"]


def test_bootstrap_is_deterministic_and_clusters_by_date():
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(6)]
    samples = [
        Sample(f"00000{i}", day, 60, "ok", gross=float(i * 3), excess=float(i))
        for i, day in enumerate(days)
    ]
    first = sector._stat(samples, 60, {})
    assert first["samples"] == 6
    assert first["market_excess_mean"] == pytest.approx(2.5)

    boot1 = trend._bootstrap_by_date(samples, 60, 50, seed=7)
    boot2 = trend._bootstrap_by_date(samples, 60, 50, seed=7)
    assert boot1 == boot2
    assert boot1["dates"] == 6
    assert boot1["excess_ci95"] is not None


def test_extremes_reports_right_tail_dependence():
    samples = [
        Sample("000001", date(2026, 1, 5), 60, "ok", gross=-20.0),
        Sample("000002", date(2026, 1, 5), 60, "ok", gross=0.0),
        Sample("000003", date(2026, 1, 5), 60, "ok", gross=10.0),
        Sample("000004", date(2026, 1, 5), 60, "ok", gross=100.0),
    ]
    out = trend._extremes(samples, 60)
    assert out["samples"] == 4
    assert out["trimmed_mean_ex_top5"] < 10
    assert out["top5_threshold"] >= 10


def test_sector_excess_sign_is_stock_minus_board():
    day = date(2026, 1, 5)
    samples = [
        Sample("000001", day, 5, "ok", gross=5.0, excess=1.0),
        Sample("000002", day, 5, "ok", gross=-1.0, excess=-2.0),
    ]
    sector_returns = {("000001", day, 5): 3.0, ("000002", day, 5): 2.0}
    out = sector._stat(samples, 5, sector_returns)
    # (5-3) + (-1-2) = -1 / 2 = -0.5
    assert out["sector_excess_mean"] == pytest.approx(-0.5)
