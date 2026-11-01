"""RPS 跟踪摘要的「样本」口径（2026-11-01 审计 P2-6）。

名单 3 只、但其中 1 只两端缺日线 → 样本应为 2，不是 3。
"""

from datetime import date
from types import SimpleNamespace

import numpy as np

from app.services import pattern_track, pools

DAY_A = date(2026, 11, 2)
DAY_B = date(2026, 10, 30)


class _FakeSession:
    """只够 `rps_track` 取日期用（`rps_board` / `_curves` 都被换掉）。"""

    def scalars(self, *_args, **_kwargs):
        return [DAY_A, DAY_B]


def test_rps_track_samples_count_only_verified_rows(monkeypatch):
    rows = [
        SimpleNamespace(code="600000", name="A", rps250=95.0),
        SimpleNamespace(code="600001", name="B", rps250=94.0),
        SimpleNamespace(code="600002", name="C", rps250=93.0),
    ]
    monkeypatch.setattr(
        pools, "rps_board", lambda *a, **k: SimpleNamespace(rows=rows)
    )
    # A 只有 2 只算出曲线（第 3 只停牌），B 有 3 只
    monkeypatch.setattr(
        pattern_track,
        "_curves",
        lambda session, picks, step: {
            DAY_A: {1: (np.array([0.01, 0.02]), 0.01), 5: (np.array([0.01, 0.02]), 0.01)},
            DAY_B: {
                1: (np.array([0.01, 0.02, 0.03]), 0.02),
                5: (np.array([0.01, 0.02, 0.03]), 0.02),
            },
        },
    )

    result = pools.rps_track(_FakeSession(), days=20)
    by_day = {row.trade_date: row for row in result.days}
    assert by_day[DAY_A].picks == 3  # 名单只数仍是 3
    assert result.summary["samples"] == 5  # 2 + 3，而不是 3 + 3
