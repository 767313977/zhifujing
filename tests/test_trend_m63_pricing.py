"""M6.3 价格域与公司行为审计工具单测。"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import m63_alignment_audit as alignment  # noqa: E402
import m63_corporate_action_audit as audit  # noqa: E402


def test_classify_bands() -> None:
    assert audit.classify(0.01) == "ROUNDING_OR_MINOR_MISMATCH"
    assert audit.classify(0.5) == "DIVIDEND_LIKE_OR_PCT_BASIS_MISMATCH"
    assert audit.classify(6.0) == "LARGE_CORPORATE_ACTION_OR_ANOMALY"
    assert audit.classify(25.0) == "SPLIT_OR_DATA_ANOMALY"


def test_classify_boundaries() -> None:
    assert audit.classify(0.2) == "ROUNDING_OR_MINOR_MISMATCH"
    assert audit.classify(0.2001) == "DIVIDEND_LIKE_OR_PCT_BASIS_MISMATCH"
    assert audit.classify(5.0001) == "LARGE_CORPORATE_ACTION_OR_ANOMALY"
    assert audit.classify(20.0001) == "SPLIT_OR_DATA_ANOMALY"


def test_alignment_reports_missing_and_extra() -> None:
    report = alignment.alignment(
        ["2026-10-01", "2026-10-02", "2026-10-03"],
        {"2026-10-01", "2026-10-03", "2026-10-04"},
    )
    assert report["expected_days"] == 3
    assert report["actual_days"] == 3
    assert report["missing_days"] == 1
    assert report["missing_dates"] == ["2026-10-02"]
    assert report["extra_dates"] == ["2026-10-04"]


def test_alignment_fully_matched() -> None:
    days = ["2026-10-01", "2026-10-02"]
    report = alignment.alignment(days, set(days))
    assert report["missing_days"] == 0
    assert report["extra_dates"] == []
    assert report["first_missing"] is None
