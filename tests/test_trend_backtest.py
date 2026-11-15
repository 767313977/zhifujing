"""M6 回测脚本单测：PIT、TopN 不补足、统计口径、只读连接。"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import backtest_trend_engine as bt  # noqa: E402

from app.models import Base, RpsDaily, StockDaily, TradeCalendar  # noqa: E402
from app.services import research_backtest as rb  # noqa: E402
from app.services.trend_engine import config as te_config  # noqa: E402


def _seed_prices(session: Session, days: list[date], codes: list[str], *, breakout: int) -> None:
    """80 天上涨 + 69 天窄平台 + 最后一个交易日突破（`breakout` 必须是最后一天）。"""
    for code in codes:
        price = 10.0
        history: list[float] = []
        for _ in range(80):
            price *= 1.005
            history.append(price)
        for _ in range(len(days) - 81):
            price *= 1.001
            history.append(price)
        history.append(max(history[-28:]) * 1.02)
        assert len(history) == len(days)
        for index, day in enumerate(days):
            close = history[index]
            previous = history[index - 1] if index else close
            session.add(
                StockDaily(
                    trade_date=day,
                    code=code,
                    name=code,
                    open=close * 0.99,
                    high=close * 1.003,
                    low=close * 0.985,
                    close=close,
                    pct_chg=(close / previous - 1) * 100 if index else 0.0,
                    volume=3e6 if index == breakout else 1e6,
                    amount=3e8 if index == breakout else 1e8,
                )
            )
            session.add(RpsDaily(trade_date=day, code=code, rps20=80.0, rps60=80.0))
    session.flush()


@pytest.fixture()
def trend_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bt.db'}")
    Base.metadata.create_all(engine)
    days = [date(2025, 1, 1) + timedelta(days=index) for index in range(150)]
    with Session(engine) as session:
        session.add_all([TradeCalendar(trade_date=day) for day in days])
        _seed_prices(session, days, ["000001", "000002", "000003"], breakout=149)
        yield session, engine, days


def _scan(session, days, end: date, scan_days: set[date]):
    matrix = rb.load_matrix(session, days[0], end)
    rps = bt.load_rps_matrix(session, matrix.calendar, matrix.codes)
    series = bt.build_series(matrix, 60)
    return bt.scan(session, matrix, rps, series, scan_days)


def test_scan_is_point_in_time(trend_db) -> None:
    session, _engine, days = trend_db
    point = days[-1]
    before = _scan(session, days, point, {point})
    assert before["A"], "构造样本应当有 Model A 信号"
    snapshot = sorted((item["code"], item["day"], tuple(sorted(item["parts"].items()))) for item in before["A"])

    # 追加「未来」暴跌行，再用同一个扫描日重算：结果必须逐字一致
    for offset in range(1, 6):
        day = point + timedelta(days=offset)
        session.add(TradeCalendar(trade_date=day))
        for code in ("000001", "000002", "000003"):
            session.add(
                StockDaily(
                    trade_date=day,
                    code=code,
                    name=code,
                    open=1.0,
                    high=1.0,
                    low=1.0,
                    close=1.0,
                    pct_chg=-50.0,
                    volume=1e6,
                    amount=1e8,
                )
            )
    session.flush()
    after = _scan(session, days + [point + timedelta(days=i) for i in range(1, 6)], point + timedelta(days=5), {point})
    rebuilt = sorted((item["code"], item["day"], tuple(sorted(item["parts"].items()))) for item in after["A"])
    assert rebuilt == snapshot


def test_rolling_max_prev_excludes_today() -> None:
    values = np.array([1.0, 2.0, 3.0, 100.0])
    out = bt._rolling_max_prev(values, 3)
    assert out[3] == 3.0  # 不含当日 100


def test_build_signals_top_does_not_pad_and_respects_min_score() -> None:
    records = [
        {"day": date(2025, 1, 2), "code": "A", "model": "A", "parts": {"structure": 1.0, "breakout": 1.0, "volume": 1.0, "rps": 1.0, "sector": None}},
        {"day": date(2025, 1, 2), "code": "B", "model": "A", "parts": {"structure": 0.5, "breakout": 0.5, "volume": 0.5, "rps": 0.5, "sector": None}},
    ]
    cfg = te_config.ModelAConfig()
    signals = bt.build_signals(records, configs={"A": cfg, "B": cfg}, rps_points=3.0, sector_points=0.0, top=5, min_score=0.0)
    assert len(signals) == 2
    top1 = bt.build_signals(records, configs={"A": cfg, "B": cfg}, rps_points=3.0, sector_points=0.0, top=1, min_score=0.0)
    assert len(top1) == 1 and top1[0].code == "A"
    empty = bt.build_signals(records, configs={"A": cfg, "B": cfg}, rps_points=3.0, sector_points=0.0, top=5, min_score=101.0)
    assert empty == []


def test_combined_dedupes_same_code_same_day() -> None:
    cfg_a = te_config.ModelAConfig()
    cfg_b = te_config.ModelBConfig()
    parts_a = {"structure": 1.0, "breakout": 1.0, "volume": 1.0, "rps": 1.0, "sector": None}
    parts_b = {"structure": 1.0, "pullback": 1.0, "volume": 1.0, "rps": 1.0, "sector": None}
    day = date(2025, 1, 2)
    records = [
        {"day": day, "code": "X", "model": "A", "parts": parts_a},
        {"day": day, "code": "X", "model": "B", "parts": parts_b},
    ]
    signals = bt.build_signals(
        records, configs={"A": cfg_a, "B": cfg_b}, rps_points=3.0, sector_points=0.0, top=20, min_score=0.0
    )
    assert len(signals) == 1


def test_combined_uses_each_model_weights() -> None:
    cfg_a = te_config.ModelAConfig()
    cfg_b = te_config.ModelBConfig()
    parts_b = {"structure": 1.0, "pullback": 1.0, "volume": 1.0, "rps": 1.0, "sector": None}
    score = bt.score_of(parts_b, cfg_b, 3.0, 0.0)
    assert score == pytest.approx(100.0)
    records = [{"day": date(2025, 1, 2), "code": "B", "model": "B", "parts": parts_b}]
    signals = bt.build_signals(records, configs={"A": cfg_a, "B": cfg_b}, rps_points=3.0, sector_points=0.0, top=5, min_score=90.0)
    assert len(signals) == 1


def test_stats_helpers() -> None:
    assert bt._profit_factor([10.0, -5.0, -5.0]) == 1.0
    assert bt._profit_factor([-1.0]) == 0.0
    assert bt._profit_factor([1.0]) is None  # 没有亏损样本 → 无法定义
    assert bt._max_drawdown([1.0, -3.0, 1.0]) == -3.0
    assert bt._max_consecutive_losses([1, -1, -2, 3, -1, -1, -1]) == 3
    tail = bt._tail([type("S", (), {"gross": value})() for value in (100, 1, 1, 1, 1, 1, 1, 1, 1, 1)])
    assert tail["top5_share"] is not None and tail["top5_share"] > 0.8


def test_bootstrap_returns_interval() -> None:
    samples = [
        rb.Sample(code="A", signal_date=date(2025, 1, 1) + timedelta(days=index), horizon=20, status="ok", gross=1.0, net=0.9, excess=0.5)
        for index in range(6)
    ]
    result = bt._bootstrap(samples, 20, 200, 42)
    assert result["days"] == 6
    assert result["ci_low"] <= result["mean"] <= result["ci_high"]


def test_split_boundaries_and_empty_segments() -> None:
    days = [date(2025, 1, 1) + timedelta(days=index) for index in range(10)]
    train, valid, test = bt._split(days, 0.6, 0.2)
    assert (len(train), len(valid), len(test)) == (6, 2, 2)
    assert train[0] == days[0] and test[-1] == days[-1]


def test_open_readonly_rejects_writes(tmp_path) -> None:
    path = tmp_path / "ro.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    session = bt.open_readonly(path)
    with pytest.raises(Exception):
        session.execute(StockDaily.__table__.insert().values(trade_date=date(2025, 1, 1), code="000001"))
        session.commit()
    session.close()


def test_load_matrix_exposes_volume(trend_db) -> None:
    session, _engine, days = trend_db
    matrix = rb.load_matrix(session, days[0], days[-1])
    assert matrix.volume.shape == matrix.close.shape
    assert np.isfinite(matrix.volume).any()
