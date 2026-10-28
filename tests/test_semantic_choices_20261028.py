"""2026-10-28 四项口径拍板后的行为（用户确认「1234 都做」）。

四条都会改变页面上的数字，所以逐条钉住，防止以后被当成 bug 又改回去：

1. 形态引擎的「涨停」门槛按板块分档（不再是全市场 9.5%）；
2. 筹码分布用前复权价（送转日不再腰斩）；
3. 杀猪盘 20 日涨幅用官方涨跌幅连乘（复权口径）；
4. 洗盘箱体上沿**不含当日**（`broke` / `gone` 分支重新可达）。
"""

from datetime import date, timedelta

import numpy as np
import pytest

from app.services import chip, patterns, trap


# ------------------------------------------------ 1. 涨停门槛按板块分档


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("600000", 9.5),  # 沪深主板 10%
        ("000001", 9.5),
        ("300750", 19.5),  # 创业板 20%
        ("688981", 19.5),  # 科创板 20%
        ("830799", 29.5),  # 北交所 30%
    ],
)
def test_limit_threshold_follows_board(code, expected):
    assert patterns.limit_threshold(code) == expected


def test_limit_threshold_defaults_to_mainboard_when_code_missing():
    """个股页那几处不跑形态、不传代码 —— 退化成主板 10% 而不是崩。"""
    assert patterns.limit_threshold("") == 9.5


def test_shrink_limit_up_ignores_mainboard_10pct_on_chinext():
    """创业板 +10% 不再算「缩量涨停」；同样的形态在主板仍然算。"""
    def bars_for(code: str) -> patterns.Bars:
        size = 8
        close = np.array([10.0] * (size - 1) + [11.0])
        return patterns.Bars(
            dates=[date(2026, 9, 1) + timedelta(days=index) for index in range(size)],
            open=np.array([9.9] * (size - 1) + [10.2]),
            high=np.array([10.1] * (size - 1) + [11.0]),
            low=np.array([9.8] * (size - 1) + [10.1]),
            close=close,
            volume=np.array([1000.0] * (size - 1) + [200.0]),
            amount=np.array([10000.0] * size),
            pct_chg=np.array([0.0] * (size - 1) + [10.0]),
            code=code,
        )

    assert patterns._shrink_limit_up(bars_for("600000")) is not None
    assert patterns._shrink_limit_up(bars_for("300750")) is None


# ------------------------------------------------ 2. 筹码：前复权


def test_chip_adjust_scales_pre_split_prices_to_latest():
    """2:1 拆股：100 元那根要缩到 50，与拆后的 50 元同一价位。"""
    rows = [
        {
            "trade_date": date(2026, 9, 1),
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "turnover": 1.0,
            "pct_chg": 0.0,
        },
        {
            "trade_date": date(2026, 9, 2),
            "high": 50.5,
            "low": 49.5,
            "close": 50.0,
            "turnover": 1.0,
            # 拆股日涨跌幅是 0（前复权口径），不复权收盘从 100 跳到 50
            "pct_chg": 0.0,
        },
    ]
    chip._adjust_to_latest(rows)
    assert rows[0]["close"] == pytest.approx(50.0)
    assert rows[0]["high"] == pytest.approx(50.5)
    assert rows[0]["low"] == pytest.approx(49.5)
    # 最新一根永远不缩放
    assert rows[1]["close"] == pytest.approx(50.0)


def test_chip_adjust_keeps_normal_days_unchanged():
    """普通上涨日（100 → 110、+10%）前复权后历史价不动。"""
    rows = [
        {
            "trade_date": date(2026, 9, 1),
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "turnover": 1.0,
            "pct_chg": 0.0,
        },
        {
            "trade_date": date(2026, 9, 2),
            "high": 111.0,
            "low": 109.0,
            "close": 110.0,
            "turnover": 1.0,
            "pct_chg": 10.0,
        },
    ]
    chip._adjust_to_latest(rows)
    assert rows[0]["close"] == pytest.approx(100.0)
    assert rows[1]["close"] == pytest.approx(110.0)


# ------------------------------------------------ 3. 杀猪盘 20 日涨幅：复权


class _Bar:
    def __init__(self, pct_chg: float | None) -> None:
        self.pct_chg = pct_chg


def test_trap_ret_pct_chains_official_returns():
    daily = [_Bar(1.0) for _ in range(21)]
    assert trap._ret_pct(daily, 20) == pytest.approx(1.01**20 - 1)


def test_trap_ret_pct_none_when_any_return_missing():
    daily = [_Bar(1.0) for _ in range(21)]
    daily[-3].pct_chg = None
    assert trap._ret_pct(daily, 20) is None
    assert trap._ret_pct(daily[:20], 20) is None  # 根数不够


# ------------------------------------------------ 4. 洗盘箱体上沿不含当日


def _wash_bars() -> patterns.Bars:
    """60 根：平盘 → 6 根放量拉升（10→13）→ 缩量横盘（12.6~12.7）→ 今天放量突破 13.2。

    索引按**绝对下标**排（`_pile_wash_phase` 内部会减掉 look 起点 `off`）：
    0~19 平盘 / 20~25 拉升 / 26~58 洗盘 / 59 今天。
    原来箱体上沿把「今天 13.25 的最高价」也算了进去，于是 `close > wash_high` 恒假、
    永远出不来 `ready`；现在上沿只取昨天之前的 12.9。
    """
    size = 60
    close = np.full(size, 10.0)
    high = np.full(size, 10.1)
    low = np.full(size, 9.9)
    volume = np.full(size, 1000.0)
    pct = np.zeros(size)

    surge = [10.3, 10.7, 11.2, 11.7, 12.3, 13.0]
    for offset, price in enumerate(surge):
        index = 20 + offset
        close[index] = price
        high[index] = price + 0.05
        low[index] = price - 0.05
        volume[index] = 3000.0
        pct[index] = (price / (10.0 if offset == 0 else surge[offset - 1]) - 1) * 100

    # 洗盘段：26~58 的箱体（12.55~12.9）
    for index in range(26, 59):
        close[index] = 12.6 if index <= 43 else 12.7
        high[index] = 12.9
        low[index] = 12.55
        pct[index] = (close[index] / close[index - 1] - 1) * 100

    # 今天：收盘越过箱体上沿，但最高价（13.25）本来会把旧实现的阈值顶到 13.25
    close[-1] = 13.2
    high[-1] = 13.25
    low[-1] = 13.0
    pct[-1] = (13.2 / 12.7 - 1) * 100

    return patterns.Bars(
        dates=[date(2026, 6, 1) + timedelta(days=index) for index in range(size)],
        open=close.copy(),
        high=high,
        low=low,
        close=close,
        volume=volume,
        amount=np.full(size, 1e7),
        pct_chg=pct,
        code="600000",
    )


def test_wash_box_high_excludes_today_and_breakout_is_reachable():
    result = patterns._pile_wash_phase(_wash_bars())
    assert result is not None
    phase, metrics = result
    assert metrics["wash_high"] == pytest.approx(12.9), "箱体上沿不能含当日最高价"
    assert phase == "ready", "收盘越过箱体上沿时应该判「预备」，而不是永远停在 wash"
