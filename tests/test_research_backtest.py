"""B0 统一回测内核：成交、费用、基准、缺失状态与可复现性。"""

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, RpsDaily, StockDaily, TradeCalendar
from app.services import research_backtest as rb

D0 = date(2026, 1, 5)
D1 = D0 + timedelta(days=1)
D2 = D0 + timedelta(days=2)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(TradeCalendar(trade_date=day) for day in (D0, D1, D2))
        db.commit()
        yield db


def _bar(
    db: Session,
    day: date,
    code: str,
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
    pct: float,
    name: str = "测试",
) -> None:
    db.add(
        StockDaily(
            trade_date=day,
            code=code,
            name=name,
            open=open_,
            high=high,
            low=low,
            close=close,
            pct_chg=pct,
            amount=1e8,
        )
    )


def _run(db: Session, signals, **kwargs):
    return rb.run_signals(db, signals, strategy="test", **kwargs)


def test_next_open_entry_and_same_day_exit(session):
    _bar(session, D0, "000001", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
    _bar(session, D1, "000001", open_=10.5, high=11.0, low=10.4, close=11.0, pct=10.0)
    _bar(session, D2, "000001", open_=11.0, high=11.6, low=10.9, close=11.5, pct=4.55)
    session.commit()

    result = _run(session, [rb.Signal("000001", D0)], horizons=(1,), entry_mode="next_open")
    stat = result.horizons[1]
    assert stat.execution_samples == 1
    assert stat.mean == pytest.approx(round((11.0 / 10.5 - 1) * 100, 2))
    assert stat.mae_mean == pytest.approx(stat.mean)
    assert stat.counts["ok"] == 1


def test_unbuyable_when_open_at_limit_up(session):
    _bar(session, D0, "000001", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
    _bar(session, D1, "000001", open_=11.0, high=11.0, low=11.0, close=11.0, pct=10.0)
    session.commit()

    result = _run(session, [rb.Signal("000001", D0)], horizons=(1,))
    stat = result.horizons[1]
    assert stat.execution_samples == 0
    assert stat.counts["unbuyable"] == 1
    sample = result.samples[0]
    assert sample.status == "unbuyable"
    assert "entry_limit_up" in sample.flags


def test_suspended_entry_is_delayed(session):
    _bar(session, D0, "000001", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
    # D1 停牌，D2 复牌
    _bar(session, D2, "000001", open_=10.2, high=10.5, low=10.1, close=10.4, pct=4.0)
    session.commit()

    result = _run(session, [rb.Signal("000001", D0)], horizons=(1,), max_entry_delay=5)
    sample = result.samples[0]
    assert sample.status == "ok"
    assert sample.entry_date == D2
    assert sample.entry_delay == 2
    assert sample.gross == pytest.approx((10.4 / 10.2 - 1) * 100)


def test_pending_when_future_data_missing(session):
    _bar(session, D0, "000001", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
    session.commit()
    result = _run(session, [rb.Signal("000001", D0)], horizons=(5,))
    stat = result.horizons[5]
    assert stat.counts["pending"] == 1
    assert stat.execution_samples == 0


def test_exit_limit_down_is_observation_not_execution(session):
    _bar(session, D0, "000001", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
    _bar(session, D1, "000001", open_=10.5, high=11.0, low=10.4, close=11.0, pct=10.0)
    _bar(session, D2, "000001", open_=10.8, high=10.8, low=9.9, close=9.9, pct=-10.0)
    session.commit()

    result = _run(session, [rb.Signal("000001", D0)], horizons=(2,))
    stat = result.horizons[2]
    assert stat.counts["exit_limit_down"] == 1
    assert stat.observation_samples == 1
    assert stat.execution_samples == 0
    assert result.samples[0].gross is not None


def test_cost_is_applied_once(session):
    _bar(session, D0, "000001", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
    _bar(session, D1, "000001", open_=10.5, high=11.0, low=10.4, close=11.0, pct=10.0)
    session.commit()

    result = _run(session, [rb.Signal("000001", D0)], horizons=(1,), cost_bps=15)
    sample = result.samples[0]
    gross = (11.0 / 10.5 - 1) * 100
    expected_net = ((1 + gross / 100) * (1 - 15 / 10000) - 1) * 100
    assert sample.net == pytest.approx(expected_net)
    assert result.horizons[1].net_mean == pytest.approx(round(expected_net, 2))


def test_market_benchmark_is_equal_weight(session):
    # A 涨 10%，B 持平 → 等权基准 5%
    _bar(session, D0, "000001", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
    _bar(session, D0, "000002", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
    _bar(session, D1, "000001", open_=10.0, high=11.0, low=9.9, close=11.0, pct=10.0)
    _bar(session, D1, "000002", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
    session.commit()

    result = _run(session, [rb.Signal("000001", D0)], horizons=(1,))
    sample = result.samples[0]
    assert sample.gross == pytest.approx(10.0)
    assert sample.benchmark == pytest.approx(5.0)
    assert sample.excess == pytest.approx(5.0)


def test_distribution_sums_to_execution_samples(session):
    for index, (pct, close) in enumerate(((10.0, 11.0), (0.0, 10.0), (-6.0, 9.4))):
        code = f"00000{index + 1}"
        _bar(session, D0, code, open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
        _bar(session, D1, code, open_=10.0, high=max(10.0, close), low=min(10.0, close), close=close, pct=pct)
    session.commit()

    signals = [rb.Signal(f"00000{index + 1}", D0) for index in range(3)]
    result = _run(session, signals, horizons=(1,))
    stat = result.horizons[1]
    assert sum(stat.distribution.values()) == stat.execution_samples == 3


def test_signal_provider_uses_only_signal_day(session):
    _bar(session, D0, "000001", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
    session.add(RpsDaily(trade_date=D0, code="000001", rps60=80.0))
    session.commit()
    before = rb.signals_from_rps(session, D0, D0, top=10, mode="rps60")
    # 加一根未来 RPS 不应改变 D0 的信号
    session.add(RpsDaily(trade_date=D1, code="000002", rps60=99.0))
    session.commit()
    after = rb.signals_from_rps(session, D0, D0, top=10, mode="rps60")
    assert [s.code for s in before] == [s.code for s in after] == ["000001"]


def test_runs_are_reproducible(session):
    _bar(session, D0, "000001", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
    _bar(session, D1, "000001", open_=10.5, high=11.0, low=10.4, close=11.0, pct=10.0)
    session.commit()
    signals = [rb.Signal("000001", D0)]
    first = _run(session, signals, horizons=(1,))
    second = _run(session, signals, horizons=(1,))
    assert first.horizons[1].mean == second.horizons[1].mean
    assert first.horizons[1].distribution == second.horizons[1].distribution


def test_ex_rights_uses_pct_not_raw_close_ratio(session):
    """拆股/除权日原始收盘价腰斩，但官方 pct=0：收益必须是 0，不是 −50%。"""
    _bar(session, D0, "000001", open_=20.0, high=20.1, low=19.9, close=20.0, pct=0.0)
    # 10 转 10：原始价从 20 变 10，官方涨跌幅仍是 0
    _bar(session, D1, "000001", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
    session.commit()
    result = _run(session, [rb.Signal("000001", D0)], horizons=(1,))
    assert result.samples[0].gross == pytest.approx(0.0)


def test_missing_code_is_no_quote(session):
    """模拟退市/出池：信号代码在矩阵里没有行 → no_quote，不按 0 收益计入。"""
    _bar(session, D0, "000001", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
    session.commit()
    result = _run(session, [rb.Signal("999999", D0)], horizons=(1,))
    assert result.samples[0].status == "no_quote"
    assert result.horizons[1].execution_samples == 0


def test_st_flag_uses_entry_day_name(session):
    """ST 状态按入场日名称判，不按最新名称。"""
    _bar(session, D0, "000001", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0, name="正常")
    _bar(session, D1, "000001", open_=10.5, high=11.0, low=10.4, close=11.0, pct=10.0, name="ST测试")
    session.commit()
    result = _run(session, [rb.Signal("000001", D0)], horizons=(1,))
    assert "st" in result.samples[0].flags
