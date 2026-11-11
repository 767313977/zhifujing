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
    assert stat.observation_samples == 0
    assert stat.mean is None


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
    assert stat.mean is None  # 理论收益只作观察，不计入“正常完成”统计
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


# ---------------------------------------------------------------------------
# 延迟窗口边界：入场最多 5 个交易日、退出最多 10 个（含 offset=0）
# ---------------------------------------------------------------------------


def _add_days(db: Session, count: int) -> list[date]:
    """从 D0 起补足 count 个连续自然日到交易日历（测试用，不代表真实日历）。"""
    days = [D0 + timedelta(days=index) for index in range(count)]
    existing = {row[0] for row in db.query(TradeCalendar.trade_date).all()}
    for day in days:
        if day not in existing:
            db.add(TradeCalendar(trade_date=day))
    db.commit()
    return days


def test_entry_delay_cap_is_five_trading_days(session):
    days = _add_days(session, 8)  # D0..D7
    _bar(session, days[0], "000001", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
    _bar(session, days[5], "000001", open_=10.2, high=10.3, low=10.1, close=10.3, pct=3.0)
    session.commit()

    result = _run(session, [rb.Signal("000001", days[0])], horizons=(1,), max_entry_delay=5)
    sample = result.samples[0]
    assert sample.status == "ok"
    assert sample.entry_date == days[5]
    assert sample.entry_delay == 5


def test_entry_after_five_delays_is_suspended_not_delayed(session):
    days = _add_days(session, 8)  # D0..D7
    _bar(session, days[0], "000001", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
    # 第 6 次机会（offset=6）不在允许窗口内 → 必须是 suspended，不能顺延到 D6
    _bar(session, days[6], "000001", open_=10.2, high=10.3, low=10.1, close=10.3, pct=3.0)
    session.commit()

    result = _run(session, [rb.Signal("000001", days[0])], horizons=(1,), max_entry_delay=5)
    sample = result.samples[0]
    assert sample.status == "suspended"
    assert sample.entry_date is None
    assert result.horizons[1].execution_samples == 0


def test_exit_delay_includes_offset_zero_and_caps_at_ten(session):
    days = _add_days(session, 14)  # D0..D13
    _bar(session, days[0], "000001", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
    _bar(session, days[1], "000001", open_=10.1, high=10.2, low=10.0, close=10.2, pct=2.0)
    # horizon=2 → 目标退出日是 D2；offset=10 对应 D12
    _bar(session, days[12], "000001", open_=10.3, high=10.4, low=10.2, close=10.4, pct=2.0)
    session.commit()

    result = _run(session, [rb.Signal("000001", days[0])], horizons=(2,), max_exit_delay=10)
    sample = result.samples[0]
    assert sample.status == "ok"
    assert sample.entry_date == days[1]
    assert sample.exit_date == days[12]
    assert sample.exit_delay == 10


def test_exit_after_ten_delays_is_no_exit(session):
    days = _add_days(session, 15)  # D0..D14
    _bar(session, days[0], "000001", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
    _bar(session, days[1], "000001", open_=10.1, high=10.2, low=10.0, close=10.2, pct=2.0)
    # 目标 D2；offset 0..10 覆盖 D2..D12；D13 是第 12 次尝试，不应被接受
    _bar(session, days[13], "000001", open_=10.3, high=10.4, low=10.2, close=10.4, pct=2.0)
    session.commit()

    result = _run(session, [rb.Signal("000001", days[0])], horizons=(2,), max_exit_delay=10)
    sample = result.samples[0]
    assert sample.status == "no_exit"
    assert sample.exit_date is None
    assert result.horizons[2].execution_samples == 0


def test_unbuyable_is_not_retried_on_next_day(session):
    _bar(session, D0, "000001", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
    _bar(session, D1, "000001", open_=11.0, high=11.0, low=11.0, close=11.0, pct=10.0)
    _bar(session, D2, "000001", open_=11.0, high=11.3, low=10.9, close=11.2, pct=1.8)
    session.commit()

    result = _run(session, [rb.Signal("000001", D0)], horizons=(1,), max_entry_delay=5)
    sample = result.samples[0]
    assert sample.status == "unbuyable"
    assert sample.entry_date == D1
    assert sample.entry_delay == 1
    assert "entry_limit_up" in sample.flags


def test_different_entry_days_shift_planned_exit(session):
    days = _add_days(session, 6)  # D0..D5
    # A：D1 正常入场，horizon=2 → D2 退出
    _bar(session, days[0], "000001", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
    _bar(session, days[1], "000001", open_=10.1, high=10.2, low=10.0, close=10.2, pct=2.0)
    _bar(session, days[2], "000001", open_=10.2, high=10.3, low=10.1, close=10.3, pct=1.0)
    # B：D1 停牌，D2 入场 → D3 退出
    _bar(session, days[0], "000002", open_=10.0, high=10.1, low=9.9, close=10.0, pct=0.0)
    _bar(session, days[2], "000002", open_=10.1, high=10.2, low=10.0, close=10.2, pct=2.0)
    _bar(session, days[3], "000002", open_=10.2, high=10.3, low=10.1, close=10.3, pct=1.0)
    session.commit()

    result = _run(
        session,
        [rb.Signal("000001", days[0]), rb.Signal("000002", days[0])],
        horizons=(2,),
        max_entry_delay=5,
    )
    by_code = {sample.code: sample for sample in result.samples}
    assert (by_code["000001"].entry_date, by_code["000001"].exit_date) == (days[1], days[2])
    assert (by_code["000002"].entry_date, by_code["000002"].exit_date) == (days[2], days[3])
    assert by_code["000002"].entry_delay == 2

