"""相似日的分位排名（纯函数）。"""

from datetime import date

import pytest

from app.services.similar_days import _rank_map


def test_rank_map_handles_ties():
    pairs = [
        (date(2026, 1, 1), 1.0),
        (date(2026, 1, 2), 2.0),
        (date(2026, 1, 3), 2.0),
        (date(2026, 1, 4), 3.0),
    ]
    ranks = _rank_map(pairs)
    assert ranks[date(2026, 1, 1)] == pytest.approx(0.125)
    assert ranks[date(2026, 1, 2)] == pytest.approx(0.5)
    assert ranks[date(2026, 1, 3)] == pytest.approx(0.5)
    assert ranks[date(2026, 1, 4)] == pytest.approx(0.875)


def test_rank_map_empty():
    assert _rank_map([]) == {}
