"""M6.3 RPS 版本化内核单测：百分位、窗口收益、旧日质量判定。"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.trend_engine import rps_versioning as rps  # noqa: E402


def test_percentile_rank_with_ties() -> None:
    values = np.asarray([1.0, 2.0, 2.0, 3.0])
    ranks = rps.percentile_rank(values)
    # 平均秩：1 → 1；(2,2) → 2.5；3 → 4
    assert ranks.tolist() == [0.0, 50.0, 50.0, 100.0]


def test_percentile_rank_single_value() -> None:
    assert rps.percentile_rank(np.asarray([5.0])).tolist() == [100.0]
    assert rps.percentile_rank(np.asarray([])).size == 0


def test_cross_section_ranks_are_bounded() -> None:
    ranks = rps.cross_section_ranks({"a": -1.0, "b": 0.0, "c": 1.0})
    assert ranks["a"] == 0.0
    assert ranks["c"] == 100.0
    assert 0.0 < ranks["b"] < 100.0
    assert rps.cross_section_ranks({}) == {}


def test_returns_for_window_uses_trading_rows() -> None:
    closes = np.asarray([10.0, 11.0, 12.0, 13.0])
    returns = rps.returns_for_window(closes, 2)
    assert np.isnan(returns[0]) and np.isnan(returns[1])
    assert returns[2] == pytest.approx(20.0)
    assert abs(returns[3] - (13.0 / 11.0 - 1) * 100) < 1e-9


def test_classify_legacy_day_statuses() -> None:
    stock_last = date(2026, 10, 9)
    ok = rps.classify_legacy_day(
        trade_date=date(2026, 9, 30),
        stock_rows=5000,
        rps_rows=4990,
        ret20_rows=4900,
        ret120_rows=4900,
        stock_last_date=stock_last,
    )
    assert ok.usable and ok.status == "OK"

    orphan = rps.classify_legacy_day(
        trade_date=date(2026, 10, 21),
        stock_rows=0,
        rps_rows=5091,
        ret20_rows=0,
        ret120_rows=5091,
        stock_last_date=stock_last,
    )
    assert not orphan.usable and orphan.status == "SUSPECT"

    mixed = rps.classify_legacy_day(
        trade_date=date(2026, 10, 9),
        stock_rows=5000,
        rps_rows=5000,
        ret20_rows=0,
        ret120_rows=5000,
        stock_last_date=stock_last,
    )
    assert not mixed.usable and "混版本" in mixed.reason

    partial = rps.classify_legacy_day(
        trade_date=date(2026, 9, 30),
        stock_rows=5000,
        rps_rows=3900,
        ret20_rows=3900,
        ret120_rows=3900,
        stock_last_date=stock_last,
    )
    assert partial.status == "PARTIAL" and partial.usable
