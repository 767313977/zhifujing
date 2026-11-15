"""M6.1 研究工具单测：数据审计、变体过滤/评分、Purged WF、归因统计。"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import analyze_trend_models_m61 as attribution  # noqa: E402
import audit_trend_data_m61 as audit  # noqa: E402
import run_m61_experiments as exp  # noqa: E402

from app.models import Base, RpsDaily, StockDaily  # noqa: E402


def _memory_engine():
    return create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )


def test_split_days_boundaries() -> None:
    days = [date(2025, 1, 1) + timedelta(days=index) for index in range(10)]
    splits = exp.split_days(days)
    assert [len(splits[name]) for name in ("train", "validation", "test")] == [6, 2, 2]


def test_purged_blocks_embargo() -> None:
    days = [date(2025, 1, 1) + timedelta(days=index) for index in range(40)]
    blocks = exp.purged_blocks(days, blocks=4, embargo=5)
    assert len(blocks) == 3
    first = blocks[0]
    assert first["test_start"] == str(days[10])
    assert first["embargo_start"] == str(days[5])


def test_variant_filters() -> None:
    record = {
        "regime": "BULL",
        "features": {"distance_ma20": 5.0, "ret20": 10.0, "trend_days": 20},
    }
    filters = {
        "distance_ma20": ("<=", 10.0),
        "ret20": ("<=", 30.0),
        "trend_days": ("range", 6, 60),
        "_regime": ("in", ("BULL", "SIDEWAYS")),
    }
    assert exp.passes(record, filters) is True
    assert exp.passes({**record, "regime": "BEAR"}, filters) is False
    assert exp.passes({**record, "features": {"distance_ma20": None}}, filters) is False
    assert exp.passes(record, {"trend_days": ("range", 6, 10)}) is False


def test_variant_score_weights_and_rps() -> None:
    parts = {"structure": 1.0, "breakout": 1.0, "volume": 1.0, "rps": 1.0}
    no_rps = exp.score(
        {"parts": parts}, "A", {"weights": (50, 30, 20), "rps": 0.0, "filters": {}}
    )
    with_rps = exp.score(
        {"parts": parts}, "A", {"weights": (50, 30, 20), "rps": 3.0, "filters": {}}
    )
    assert no_rps == pytest.approx(100.0)
    assert with_rps == pytest.approx(100.0)
    half = exp.score(
        {"parts": {**parts, "structure": 0.5}}, "A",
        {"weights": (50, 30, 20), "rps": 0.0, "filters": {}},
    )
    assert half == pytest.approx(75.0)


def test_audit_rps_detects_mixed_versions() -> None:
    engine = _memory_engine()
    Base.metadata.create_all(engine)
    day = date(2026, 10, 21)
    with Session(engine) as session:
        session.add(StockDaily(trade_date=day, code="000001", close=10.0, pct_chg=0.0, amount=1e8, volume=1e6))
        session.add(RpsDaily(trade_date=day, code="000001", ret120=5.0, rps120=50.0, ret250=5.0, rps250=50.0))
        session.flush()
        result = audit.audit_rps(session, [day], {day: 1})
        assert result["mixed_version_days"]
        assert result["mixed_version_days"][0]["ret20"] == 0


def test_audit_duplicate_groups() -> None:
    engine = _memory_engine()
    from sqlalchemy import text

    with Session(engine) as session:
        session.execute(text("CREATE TABLE dup_t (trade_date TEXT, code TEXT)"))
        session.execute(
            text("INSERT INTO dup_t VALUES ('2025-01-02','A'), ('2025-01-02','A')")
        )
        session.flush()
        assert audit._duplicate_groups(session, "dup_t", ("trade_date", "code")) == 1


def test_attribution_helpers() -> None:
    assert attribution._contribution([100.0, 1.0, 1.0, 1.0], 0.25) == pytest.approx(100 / 103, abs=1e-3)
    assert attribution._contribution([-1.0, -2.0], 0.5) is None
    assert attribution._t_pvalue([1.0, 1.0, 1.0]) is None
    assert attribution.mv_bucket(4e9) == "<50亿"
    assert attribution.mv_bucket(2e11) == ">1000亿"
    assert attribution.mv_bucket(None) is None
    assert attribution.trend_phase(3) == "0-5日"
    assert attribution.trend_phase(80) == ">60日"


def test_worst_best_profile_detects_direction() -> None:
    records = []
    outcome = {}
    for index in range(80):
        day = date(2025, 1, 1) + timedelta(days=index)
        good = index >= 40
        records.append(
            {
                "day": day,
                "code": f"{index:06d}",
                "features": {"rps60": 90.0 if good else 20.0, "trend_days": 30},
            }
        )
        outcome[(day, f"{index:06d}")] = {
            "gross": 5.0 if good else -5.0,
            "excess": 1.0 if good else -1.0,
        }
    result = attribution.worst_best_profile(records, outcome, 20)
    top = result["features"][0]
    assert top["factor"] == "rps60"
    assert top["standardized_diff"] > 0
