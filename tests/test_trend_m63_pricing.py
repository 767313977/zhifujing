"""M6.3 价格域与公司行为审计工具单测。"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

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
