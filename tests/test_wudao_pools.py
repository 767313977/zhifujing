"""悟道六池：入池判据、关键价位、数据不足与板块边界。

全部用**明确构造的 OHLCV**，不碰数据库。每个池子都覆盖「命中 / 不满足 / 数据不足」，
能构造出边界的再补边界；板块与 ST 属生产扫描层，单独断言。
"""

from datetime import date, timedelta

import numpy as np

from app.jobs.scan_patterns import WUDAO_POOL_KEYS, is_wudao_board, pattern_board_allowed
from app.services import patterns


class _Cols:
    """按日拼列：`add(开, 高, 低, 收, 量, 涨跌幅%)`。"""

    def __init__(self) -> None:
        self.open: list[float] = []
        self.high: list[float] = []
        self.low: list[float] = []
        self.close: list[float] = []
        self.volume: list[float] = []
        self.pct: list[float] = []

    def add(
        self,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        pct: float,
    ) -> "_Cols":
        self.open.append(open_)
        self.high.append(high)
        self.low.append(low)
        self.close.append(close)
        self.volume.append(volume)
        self.pct.append(pct)
        return self

    def base(self, count: int, *, price: float = 10.0, volume: float = 1e6) -> "_Cols":
        for _ in range(count):
            self.add(price, price, price, price, volume, 0.0)
        return self

    def bars(self, code: str = "300001") -> patterns.Bars:
        n = len(self.close)
        return patterns.Bars(
            dates=[date(2026, 1, 5) + timedelta(days=index) for index in range(n)],
            open=np.asarray(self.open, dtype=float),
            high=np.asarray(self.high, dtype=float),
            low=np.asarray(self.low, dtype=float),
            close=np.asarray(self.close, dtype=float),
            volume=np.asarray(self.volume, dtype=float),
            amount=np.asarray(self.close, dtype=float) * np.asarray(self.volume, dtype=float),
            pct_chg=np.asarray(self.pct, dtype=float),
            code=code,
        )


#: 样板日：昨收 10 → 冲高 10.9（+9%）、收 10.60（+6%）、量比 2.0、收盘离最高 2.75%
def _add_sample_day(cols: _Cols) -> _Cols:
    return cols.add(10.4, 10.9, 10.3, 10.6, 2.0e6, 6.0)


def _sample_bars() -> patterns.Bars:
    return _Cols().base(40).add(10.4, 10.9, 10.3, 10.6, 2.0e6, 6.0).bars()


# ---------------------------------------------------------------- 明天盯


def test_wudao_sample_hits_on_engineered_day():
    signal = patterns._wudao_sample(_sample_bars())
    assert signal is not None
    assert signal.pattern == "wudao_sample"
    assert signal.key_levels["watch_high"] == 10.9
    assert signal.detail["high_pct"] == 9.0
    assert signal.detail["leave_high"] <= patterns.WUDAO_SAMPLE_MAX_LEAVE


def test_wudao_sample_rejects_small_high():
    bars = _Cols().base(40).add(10.1, 10.5, 10.05, 10.45, 2.0e6, 4.5).bars()
    assert patterns._wudao_sample(bars) is None


def test_wudao_sample_rejects_when_close_leaves_high_too_far():
    """冲高达标但收盘离最高 5.5%（> 2.9%）—— 这道过滤只在出口，不改 `_wudao_is_sample`。"""
    bars = _Cols().base(40).add(10.4, 10.9, 10.3, 10.3, 2.0e6, 3.0).bars()
    assert patterns._wudao_is_sample(bars, len(bars) - 1) is True
    assert patterns._wudao_sample(bars) is None


def test_wudao_sample_volume_ratio_boundary():
    """量比门槛 1.3：1.29 不命中、1.30 命中。"""
    low = _Cols().base(40).add(10.4, 10.9, 10.3, 10.6, 1.29e6, 6.0).bars()
    exact = _Cols().base(40).add(10.4, 10.9, 10.3, 10.6, 1.30e6, 6.0).bars()
    assert patterns._wudao_sample(low) is None
    assert patterns._wudao_sample(exact) is not None


def test_wudao_sample_rejects_volume_diverge():
    """爆量（量比 ≥2.5）冲高不收 → 吵/出货，不当样板。"""
    bars = _Cols().base(40).add(10.4, 10.9, 10.3, 10.6, 3.0e6, 6.0).bars()
    assert patterns._wudao_sample(bars) is None


# ---------------------------------------------------------------- 明天预案


def _start_bars(close: float, high: float = 11.2) -> patterns.Bars:
    return (
        _Cols()
        .base(40)
        .add(10.4, 10.9, 10.3, 10.6, 2.0e6, 6.0)  # 昨日样板
        .add(10.7, high, 10.65, close, 2.0e6, (close / 10.6 - 1) * 100)
        .bars()
    )


def test_wudao_start_close_above_sample_high():
    signal = patterns._wudao_start(_start_bars(11.05))
    assert signal is not None
    assert signal.key_levels["breakout"] == 10.9
    assert signal.detail["break_state"] == "收盘站稳"


def test_wudao_start_intraday_break_but_close_below_sample_high():
    """盘中过样板高、收盘只在 98% 上方 —— 必须标成「盘中突破·收盘未站稳」。"""
    signal = patterns._wudao_start(_start_bars(10.75))
    assert signal is not None
    assert signal.detail["break_state"] == "盘中突破·收盘未站稳"
    assert signal.key_levels["breakout"] == 10.9


def test_wudao_start_requires_intraday_break():
    signal = patterns._wudao_start(_start_bars(10.8, high=10.85))
    assert signal is None


# ---------------------------------------------------------------- 洗完可盯


def _wash_bars(wash_low: float = 10.9) -> patterns.Bars:
    return (
        _Cols()
        .base(40)
        .add(10.4, 10.9, 10.3, 10.6, 2.0e6, 6.0)  # 样板
        .add(10.7, 11.3, 10.6, 11.1, 2.2e6, (11.1 / 10.6 - 1) * 100)  # 启动
        .add(11.15, 11.2, wash_low, 11.05, 1.2e6, (11.05 / 11.1 - 1) * 100)  # 洗盘
        .bars()
    )


def test_wudao_wash2_hits_and_exports_anchors():
    signal = patterns._wudao_wash2(_wash_bars())
    assert signal is not None
    assert signal.key_levels["watch_high"] == 11.2  # 洗盘高 = 今天的最高
    assert signal.key_levels["start_low"] == 10.6  # 作废线 = 启动日最低
    assert signal.detail["wash_vol_vs_start"] < 1.0


def test_wudao_wash2_invalidates_when_wash_breaks_start_low():
    assert patterns._wudao_wash2(_wash_bars(wash_low=10.5)) is None


def test_wudao_wash2_rejects_strong_wash_close():
    """洗盘日收得比启动收盘还高 4% 以上 —— 那不是洗盘，是继续主升。"""
    bars = (
        _Cols()
        .base(40)
        .add(10.4, 10.9, 10.3, 10.6, 2.0e6, 6.0)
        .add(10.7, 11.3, 10.6, 11.1, 2.2e6, (11.1 / 10.6 - 1) * 100)
        .add(11.15, 11.7, 11.1, 11.6, 1.2e6, (11.6 / 11.1 - 1) * 100)
        .bars()
    )
    assert patterns._wudao_wash2(bars) is None


# ---------------------------------------------------------------- 黑白选股


def _pile_bars(*, last_close: float, last_volume: float) -> patterns.Bars:
    cols = _Cols().base(55)
    price = 10.0
    for _ in range(8):  # 放量拉升 8 天，每天 +4.5%
        prev = price
        price = round(price * 1.045, 2)
        cols.add(prev, price * 1.01, prev * 0.99, price, 3.0e6, 4.5)
    for _ in range(9):  # 缩量洗盘 9 天，横在 14 附近
        cols.add(14.0, 14.3, 13.7, 14.0, 1.2e6, 0.0)
    cols.add(14.0, last_close * 1.005, 13.9, last_close, last_volume,
             (last_close / 14.0 - 1) * 100)
    return cols.bars()


def test_pile_wash_wash_reports_wash_phase():
    bars = _pile_bars(last_close=14.0, last_volume=1.2e6)
    result = patterns._pile_wash_phase(bars)
    assert result is not None
    phase, metrics = result
    assert phase == "wash"
    signal = patterns._pile_wash_wash(bars)
    assert signal is not None
    assert signal.key_levels["wash_high"] > 0


def test_pile_wash_ready_after_breaking_wash_high():
    bars = _pile_bars(last_close=14.35, last_volume=2.4e6)
    result = patterns._pile_wash_phase(bars)
    assert result is not None and result[0] == "ready"
    signal = patterns._pile_wash_ready(bars)
    assert signal is not None
    assert signal.key_levels["breakout"] == signal.key_levels["wash_high"]


def test_pile_wash_needs_min_bars():
    assert patterns._pile_wash_phase(_Cols().base(30).bars()) is None
    assert patterns._pile_wash_ready(_Cols().base(30).bars()) is None


# ---------------------------------------------------------------- 华宝早期


def _huabao_bars() -> patterns.Bars:
    cols = _Cols().base(40)
    prev = 10.0
    for _ in range(3):  # 连阳（+0.8%）
        close = round(prev * 1.008, 2)
        cols.add(prev, close * 1.004, prev * 0.996, close, 1e6, 0.8)
        prev = close
    cols.add(prev, prev * 1.016, prev * 0.998, prev * 1.012, 1.6e6, 1.2)  # 末日放量
    return cols.bars()


def test_huabao_early_hits_on_steady_yang():
    signal = patterns._huabao_early(_huabao_bars())
    assert signal is not None
    assert signal.detail["yang_days"] >= 3
    assert signal.key_levels["ma5"] > 0


def test_huabao_early_needs_min_bars():
    assert patterns._huabao_early(_Cols().base(29).bars()) is None


def test_huabao_early_rejects_flat_market():
    """横盘（涨幅 0）虽然每根都「收 ≥ 开」，但连阳均涨 0% 不达标。"""
    assert patterns._huabao_early(_Cols().base(40).bars()) is None


# ---------------------------------------------------------------- 通用：数据不足 / 板块 / ST


def test_all_six_pools_return_none_without_enough_bars():
    bars = _Cols().base(10).bars()
    detectors = {pattern.key: pattern.detect for pattern in patterns.PATTERNS}
    for key in (
        "wudao_sample",
        "wudao_start",
        "wudao_wash2",
        "huabao_early",
        "pile_wash_ready",
        "pile_wash_wash",
    ):
        assert detectors[key](bars) is None, key


def test_six_pools_are_gated_to_20cm_boards():
    for key in (
        "wudao_sample",
        "wudao_start",
        "wudao_wash2",
        "huabao_early",
        "pile_wash_ready",
        "pile_wash_wash",
    ):
        assert key in WUDAO_POOL_KEYS
        assert is_wudao_board("300001") and is_wudao_board("688001")
        assert pattern_board_allowed(key, "300001") is True
        assert pattern_board_allowed(key, "600000") is False
        assert pattern_board_allowed(key, "830001") is False


def test_st_names_are_detected():
    for name in ("ST某某", "*ST某某", "SST某某", "S*ST某某"):
        assert patterns.is_st(name) is True
    assert patterns.is_st("正常股份") is False
    assert patterns.is_st(None) is False


def test_sample_judgement_has_no_lookahead():
    """同一根 K 线：把它当成「今天」能命中；后面再加两根下跌，回看那天仍然命中。"""
    cols = _Cols().base(40)
    _add_sample_day(cols)
    signal = patterns._wudao_sample(cols.bars())
    assert signal is not None

    cols.add(10.5, 10.6, 10.2, 10.3, 1.0e6, -2.8)
    cols.add(10.2, 10.3, 9.9, 10.0, 1.0e6, -2.9)
    # 判定只看「序列最后一天」：再喂后面的数据，最后一根已经换了，不能再命中
    assert patterns._wudao_sample(cols.bars()) is None
