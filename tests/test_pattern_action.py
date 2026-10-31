"""形态可行动摘要（FEAT-PATTERN-ACTIONABLE）：纯函数分档，不重算指标。"""

from app.services import pattern_track


def _card(verdict: str, excess: float | None = None) -> dict:
    return {
        "start": "2026-09-01",
        "cohorts": 20,
        "track_days": 10,
        "patterns": [
            {
                "pattern": "k",
                "name": "某形态",
                "group": "趋势",
                "verdict": verdict,
                "hits": 100,
                "stocks": 80,
                "touch": 0.6,
                "stop": 0.2,
                "returns": [{"days": 10, "excess": excess, "beat_pct": 55.0}],
            }
        ],
    }


def test_verdict_to_bucket():
    for verdict, bucket in (
        ("有效", "active"),
        ("观察", "weak"),
        ("噪声", "weak"),
        ("样本不足", "insufficient"),
    ):
        data = pattern_track.action_summary(_card(verdict, 1.5))
        assert data["rows"][0]["bucket"] == bucket
        assert data["rows"][0]["excess"] == 1.5
        assert data[bucket] == ["k"]


def test_empty_card_has_note():
    data = pattern_track.action_summary({"patterns": []})
    assert data["rows"] == []
    assert data["active"] == []
    assert data["note"]


def test_experimental_patterns_are_marked_not_removed():
    """FEAT-PATTERN-EXPERIMENTAL：实验形态只在注册表里打标，不物理删除。"""
    from app.services.patterns import PATTERNS

    experimental = {pattern.key for pattern in PATTERNS if pattern.experimental}
    assert experimental == {"ma_squeeze", "downtrend_breakout", "volume_dry_bottom"}
    assert all(pattern.key for pattern in PATTERNS)
