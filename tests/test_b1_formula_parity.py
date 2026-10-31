"""知行超级B1：三份同花顺公式与 Python 实现的一致性。

任务书 P1-1 的要求分三层，这里各测一层：

1. **三份公式之间**：七个子信号与 `选股` 的判定行必须逐字一致（允许画线不同）；
2. **公式与 Python 的常量**：宽幅板前缀、缩量倍数、各种边界值必须能在 Python 源码里
   找到同一个数（不是「看起来一样」，是机器比对）；
3. **指标口径**：用固定 OHLCV 独立重算 EMA / MA / BBI / KDJ / RSI3 / 振幅，与
   `patterns._compute_zgb1` 的逐根数组对齐到浮点精度。

⚠️ **本环境无法运行同花顺**，所以这里只能证明「公式文本 ↔ Python 代码」一致，
**不能**宣称已经做过实盘软件级的 Golden Dataset 核对 —— 那需要从同花顺导出同一只票、
同一日期的指标读数。
"""

import re
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from app.services import patterns

ROOT = Path(__file__).resolve().parents[1]
FORMULA_DIR = ROOT / "同花顺公式"

#: 公式里的信号名 → 生产注册表 key（顺序按原式 `选股` 那一行）
SIGNALS = {
    "超卖缩量拐头B": "zgnb_turn",
    "超卖缩量B": "zgnb_shrink",
    "原始B1": "zgnb_origin",
    "超卖超缩量B": "zgnb_super_shrink",
    "回踩白线B": "zgnb_pull_white",
    "回踩超级B": "zgnb_pull_super",
    "回踩黄线B": "zgnb_pull_yellow",
}

FORMULA_FILES = {
    "主图叠加": FORMULA_DIR / "知行超级B1_主图叠加.txt",
    "技术指标": FORMULA_DIR / "知行超级B1_技术指标（副图）.txt",
    "条件选股": FORMULA_DIR / "知行超级B1_条件选股.txt",
}


def _signal_lines(text: str) -> dict[str, str]:
    """把公式里以 `:=` 赋值的那几行抽出来（去掉行内注释与首尾空白）。"""
    found: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("/*")[0].strip()
        for name in (*SIGNALS, "选股"):
            if line.startswith(f"{name}:="):
                # 条件选股版把最后一行写成 `选股:`（单冒号），统一成同一个形状再比
                found[name] = f"{name}:" + line[len(name) + 2 :]
        # 条件选股版的「选股:」没有等号
        if line.startswith("选股:") and not line.startswith("选股:="):
            found["选股"] = line
    return found


@pytest.mark.parametrize("label", list(FORMULA_FILES))
def test_formula_file_exists(label):
    assert FORMULA_FILES[label].is_file(), f"缺公式文件：{FORMULA_FILES[label]}"


def test_three_formulas_share_identical_signal_conditions():
    """主图 / 副图 / 条件选股：七个子信号 + 选股 必须逐字一致。"""
    parsed = {label: _signal_lines(path.read_text(encoding="utf-8")) for label, path in FORMULA_FILES.items()}
    for label, lines in parsed.items():
        missing = [name for name in (*SIGNALS, "选股") if name not in lines]
        assert not missing, f"{label} 缺判定行：{missing}"
    reference = parsed["技术指标"]
    for label, lines in parsed.items():
        if label == "技术指标":
            continue
        for name in (*SIGNALS, "选股"):
            assert lines[name] == reference[name], f"{label} 的「{name}」与副图版不一致"


def test_formula_signal_set_matches_registry():
    """原式七个子信号取或 = 注册表里的七个 key，一个不多一个不少。"""
    keys = {pattern.key for pattern in patterns.PATTERNS}
    for key in SIGNALS.values():
        assert key in keys, f"注册表缺 {key}"
    selection = _signal_lines(FORMULA_FILES["技术指标"].read_text(encoding="utf-8"))["选股"]
    body = selection.split(":", 1)[1].rstrip(";").strip()
    names = [item.strip() for item in body.split(" OR ")]
    assert set(names) == set(SIGNALS), f"选股那一行的信号集合变了：{names}"


def test_wide_board_prefixes_match_formula():
    """公式 `STRFIND(CODE,'30'…)` 的那串前缀 = `ZGNB_WIDE_PREFIXES`。"""
    text = FORMULA_FILES["技术指标"].read_text(encoding="utf-8")
    line = next(item for item in text.splitlines() if item.strip().startswith("宽幅板:="))
    prefixes = tuple(re.findall(r"STRFIND\(CODE,'(\d+)'", line))
    assert prefixes == patterns.ZGNB_WIDE_PREFIXES


@pytest.mark.parametrize(
    "token",
    ["0.416", "0.45", "0.618", "0.999", "0.975", "0.997", "11.9", "19.5", "1.8", "0.98"],
)
def test_python_keeps_formula_constants(token):
    """这些数字是七条判据的边界，公式与 Python 必须用同一个值。"""
    source = Path(patterns.__file__).read_text(encoding="utf-8")
    assert token in source, f"Python 源码里找不到公式常量 {token}"


# ---------------------------------------------------------------- 指标口径


def _bars_from_ohlcv(
    open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray,
    volume: np.ndarray, code: str = "600000",
) -> patterns.Bars:
    n = close.size
    return patterns.Bars(
        dates=[date(2025, 1, 1) + timedelta(days=index) for index in range(n)],
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        amount=close * volume,
        pct_chg=np.concatenate([[0.0], (close[1:] / close[:-1] - 1) * 100]),
        code=code,
    )


def _sample_bars(n: int = 260) -> patterns.Bars:
    rng = np.random.default_rng(20260301)
    steps = rng.normal(0.0008, 0.018, n)
    close = 20.0 * np.cumprod(1 + steps)
    open_ = close * (1 + rng.normal(0, 0.006, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.008, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.008, n)))
    volume = rng.lognormal(13.0, 0.3, n)
    return _bars_from_ohlcv(open_, high, low, close, volume)


def _ema(values: np.ndarray, span: int) -> np.ndarray:
    alpha = 2.0 / (span + 1.0)
    out = np.empty(values.size)
    out[0] = values[0]
    for i in range(1, values.size):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def _ma(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(values.size, np.nan)
    for i in range(window - 1, values.size):
        out[i] = values[i - window + 1 : i + 1].mean()
    return out


def _sma(values: np.ndarray, n: int, m: int) -> np.ndarray:
    """通达信 SMA(X,N,M)：首个**有效**样本直接当初值，NaN 不参与递推。"""
    out = np.full(values.size, np.nan)
    prev = float("nan")
    for i in range(values.size):
        x = float(values[i])
        if not np.isfinite(x):
            out[i] = prev
            continue
        prev = x if not np.isfinite(prev) else (m * x + (n - m) * prev) / n
        out[i] = prev
    return out


def _rsi3(close: np.ndarray) -> np.ndarray:
    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    up = _sma(gain, 3, 1)
    down = _sma(loss, 3, 1)
    return np.divide(100.0 * up, up + down, out=np.full(close.size, np.nan), where=(up + down) > 0)


def test_indicators_match_independent_recompute():
    bars = _sample_bars()
    common = patterns._compute_zgb1(bars)
    assert common["enough"] is True

    close, high, low = bars.close, bars.high, bars.low
    n = close.size
    white = _ema(_ema(close, 10), 10)
    yellow = (_ma(close, 14) + _ma(close, 28) + _ma(close, 57) + _ma(close, 144)) / 4
    bbi = (_ma(close, 3) + _ma(close, 6) + _ma(close, 12) + _ma(close, 24)) / 4

    rsv = np.full(n, np.nan)
    for i in range(8, n):
        low9 = low[i - 8 : i + 1].min()
        high9 = high[i - 8 : i + 1].max()
        rsv[i] = 100.0 * (close[i] - low9) / (high9 - low9)
    k = _sma(rsv, 3, 1)
    d = _sma(k, 3, 1)
    j = 3 * k - 2 * d

    rsi = _rsi3(close)
    fields = common["fields"]
    last = n - 1
    assert fields["rsi3"][last] == pytest.approx(rsi[last], rel=1e-12)
    assert fields["kdj_j"][last] == pytest.approx(j[last], rel=1e-12)
    assert fields["vol_shrink"][last] == pytest.approx(
        bars.volume[last] / bars.volume[last - 19 : last + 1].max(), rel=1e-12
    )
    assert fields["dist_white"][last] == pytest.approx(
        abs(close[last] - white[last]) / close[last] * 100, rel=1e-12
    )
    assert fields["dist_yellow"][last] == pytest.approx(
        abs(close[last] - yellow[last]) / yellow[last] * 100, rel=1e-12
    )
    assert fields["dist_bbi"][last] == pytest.approx(
        abs(close[last] - bbi[last]) / close[last] * 100, rel=1e-12
    )
    assert fields["amp20"][last] == pytest.approx(
        (high[last - 19 : last + 1].max() - low[last - 19 : last + 1].min())
        / low[last - 19 : last + 1].min()
        * 100,
        rel=1e-12,
    )
    assert fields["amp50"][last] == pytest.approx(
        (high[last - 49 : last + 1].max() - low[last - 49 : last + 1].min())
        / low[last - 49 : last + 1].min()
        * 100,
        rel=1e-12,
    )
    # 趋势白线 / 大哥黄线（`_zgb1_common` 也把它们交给调用方，支撑位用）
    assert common["white"][last] == pytest.approx(white[last], rel=1e-12)
    assert common["yellow"][last] == pytest.approx(yellow[last], rel=1e-12)


def test_insufficient_history_yields_no_signal():
    """不足 `ZGNB_MIN_BARS`（MA144 预热）时一律不判定 —— 不猜。"""
    bars = _sample_bars(patterns.ZGNB_MIN_BARS - 1)
    assert patterns._compute_zgb1(bars)["enough"] is False
    detectors = {pattern.key: pattern.detect for pattern in patterns.PATTERNS}
    for key in SIGNALS.values():
        assert detectors[key](bars) is None
    assert patterns._zgb1_signal(bars, "zgnb_shrink", window=1) is None


def _truncate(bars: patterns.Bars, end: int) -> patterns.Bars:
    return patterns.Bars(
        dates=bars.dates[:end],
        open=bars.open[:end],
        high=bars.high[:end],
        low=bars.low[:end],
        close=bars.close[:end],
        volume=bars.volume[:end],
        amount=bars.amount[:end],
        pct_chg=bars.pct_chg[:end],
        code=bars.code,
    )


@pytest.mark.parametrize("key", list(SIGNALS.values()))
def test_fast_path_matches_truncated_detect(key):
    """回测的快速通道（`zgb1_at`）与「截断到那根再判」必须完全一致。

    `ago` 那一项除外：它表示「距序列末尾多少根」，截断版恒为 0、整段版是 n−1−t，
    两者都对，且不参与任何筛选。
    """
    bars = _sample_bars(300)
    detector = {pattern.key: pattern.detect for pattern in patterns.PATTERNS}[key]
    for t in (168, 169, 170, 220, 299):
        fast = patterns.zgb1_at(bars, key, t)
        slow = detector(_truncate(bars, t + 1))
        assert (fast is None) == (slow is None), (key, t)
        if fast is None:
            continue
        assert fast.score == pytest.approx(slow.score)
        assert fast.key_levels == slow.key_levels
        assert {k: v for k, v in fast.detail.items() if k != "ago"} == {
            k: v for k, v in slow.detail.items() if k != "ago"
        }


def test_flat_market_fires_nothing():
    """连续无波动：RSV / 振幅退化为 0 或 NaN，七条都不该命中（也不该抛）。"""
    values = np.full(240, 10.0)
    bars = _bars_from_ohlcv(values.copy(), values.copy(), values.copy(), values.copy(), np.full(240, 1e6))
    common = patterns._compute_zgb1(bars)
    assert common["enough"] is True
    assert all(not bool(common["fired"][key][-1]) for key in SIGNALS.values())
