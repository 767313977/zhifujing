"""M6.2 研究工具单测：WF 区块、日期聚类与证据追溯工具。"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import m62_reconcile_m61_evidence as recon  # noqa: E402


def test_purged_blocks_generates_three_test_blocks() -> None:
    days = [date(2026, 1, 1).toordinal() + index for index in range(100)]
    days = [date.fromordinal(value) for value in days]
    blocks = recon.exp.purged_blocks(days, blocks=4, embargo=20)
    assert len(blocks) == 3
    assert blocks[0]["test_start"] == str(days[25])
    assert blocks[0]["test_end"] == str(days[49])
    assert blocks[0]["embargo_start"] == str(days[5])
    assert blocks[-1]["test_end"] == str(days[-1])
    assert blocks[0]["test_days"] == days[25:50]


def test_compare_reports_differences() -> None:
    expected = {"days": 3, "mean": 0.554, "ci_low": 0.332, "ci_high": 0.67}
    same = recon.compare(expected, dict(expected))
    assert same["match"] is True
    worse = dict(expected, ci_low=0.1)
    diff = recon.compare(expected, worse)
    assert diff["match"] is False
    assert diff["differences"]["ci_low"] == {"expected": 0.332, "reproduced": 0.1}


def _sample(day: str, code: str, excess: float = 1.0, horizon: int = 20, exit_day: str | None = None):
    return SimpleNamespace(
        horizon=horizon,
        status="ok",
        excess=excess,
        signal_date=date.fromisoformat(day),
        code=code,
        exit_date=date.fromisoformat(exit_day) if exit_day else None,
    )


def test_describe_days_clusters_by_signal_date() -> None:
    samples = [
        _sample("2026-08-18", "000001"),
        _sample("2026-08-18", "000002"),
        _sample("2026-09-01", "000003"),
        _sample("2026-09-01", "000003"),  # 重复信号
        _sample("2026-09-01", "000004", horizon=60),
    ]
    stats = recon.describe_days(samples)
    assert stats["samples"] == 4
    assert stats["distinct_days"] == 2
    assert stats["per_day_counts"] == {"2026-08-18": 2, "2026-09-01": 2}
    assert stats["duplicate_signal_pairs"] == 1
    assert stats["max_per_day"] == 2


def test_label_overflow_counts_samples_past_block_end() -> None:
    samples = [
        _sample("2026-09-01", "000001", exit_day="2026-09-30"),
        _sample("2026-09-02", "000002", exit_day="2026-10-10"),
    ]
    assert recon.label_overflow(samples, date(2026, 10, 9), 20) == 1
    assert recon.label_overflow(samples, date(2026, 10, 9), 60) == 0
