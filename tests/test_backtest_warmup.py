"""回测的「按形态预热门槛」（2026-11-01 审计 P1-3）。

原来统一卡 80 根，会把只有 22~79 根、但生产端确实会判定的票整只漏掉。
这里钉住：门槛取生产常量、筛序列按最短门槛、22 根的样板日真的能命中。
"""

import sys
from datetime import date, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import backtest_patterns as bt  # noqa: E402

from app.services import patterns  # noqa: E402


def _records(count: int, *, sample_last: bool = False) -> list[dict]:
    start = date(2026, 1, 5)
    rows = [
        {
            "date": start + timedelta(days=index),
            "name": "测试股",
            "open": 10.0,
            "high": 10.0,
            "low": 10.0,
            "close": 10.0,
            "volume": 1e6,
            "amount": 1e7,
            "pct_chg": 0.0,
        }
        for index in range(count)
    ]
    if sample_last:
        # 与 `test_wudao_pools` 同一个样板日：冲高 9%、收 10.6、量比 2.0、离最高 2.75%
        rows[-1].update(
            {
                "open": 10.4,
                "high": 10.9,
                "low": 10.3,
                "close": 10.6,
                "volume": 2.0e6,
                "amount": 2.0e7,
                "pct_chg": 6.0,
            }
        )
    return rows


def test_warmup_uses_production_constants():
    assert bt._warmup("wudao_sample") == patterns.WUDAO_MIN_BARS == 22
    assert bt._warmup("wudao_start") == patterns.WUDAO_MIN_BARS + 1
    assert bt._warmup("wudao_wash2") == patterns.WUDAO_WASH_MIN_BARS
    assert bt._warmup("huabao_early") == patterns.WUDAO_HUABAO_MIN_BARS
    assert bt._warmup("pile_wash_ready") == patterns.PILE_MIN_BARS
    # B1 的 MA144 + EVERY(…,20) 预热必须保留
    assert bt._warmup("zgnb_shrink") == patterns.ZGNB_MIN_BARS == 170
    # 没列出的形态仍按 80
    assert bt._warmup("v_bottom") == bt.MIN_BARS == 80


def test_select_bars_keeps_short_series_for_wudao():
    grouped = {"300001": _records(30)}
    wudao_targets = {"wudao_sample": {}, "wudao_start": {}}
    assert "300001" in bt._select_bars(grouped, wudao_targets)

    # 只跑通用形态时仍按 80 根过滤
    assert bt._select_bars(grouped, {"v_bottom": {}}) == {}


def test_22_bar_series_can_hit_wudao_sample():
    """只有 22 根 K 线（生产最低门槛）的票，样板日必须能命中。"""
    records = _records(22, sample_last=True)
    bars = patterns.build_bars(records, "300001")
    assert len(bars) == patterns.WUDAO_MIN_BARS
    signal = patterns._wudao_sample(bars)
    assert signal is not None
    assert signal.pattern == "wudao_sample"


def test_22_bar_series_still_excluded_for_b1():
    """同一只 22 根的票，B1 需要 170 根 —— 序列能进池，但第 21 根不判。"""
    grouped = {"300001": _records(22, sample_last=True)}
    bars = bt._select_bars(grouped, {"zgnb_shrink": {}})
    assert bars == {}  # 170 根门槛直接筛掉
    assert bt._warmup("zgnb_shrink") > 22
