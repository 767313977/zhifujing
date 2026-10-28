"""配额判档与订阅周期边界（纯函数，不碰数据库）。"""

from datetime import date

from app.services.usage import (
    CORE_DAILY,
    FULL_DAY_CALLS,
    QuotaLevel,
    _gap_trade_days,
    _level_for,
    cycle_end,
    cycle_start,
)


def test_level_normal_when_enough_for_remaining_days():
    # 141 × 5 = 705；1000 够满负荷跑完 → 正常
    assert _level_for(1000, 5) == QuotaLevel.NORMAL


def test_level_pauses_hit_dde_when_full_budget_not_enough():
    # 700 < 705，但 ≥ 71×5=355 → 只停「命中前 50 只的 DDE 补齐」
    assert _level_for(700, 5) == QuotaLevel.PAUSE_HIT_DDE


def test_level_pauses_kline_when_below_hit_dde_budget():
    # 70 < 71，但 ≥ 43 → 连全市场日线一起停
    assert _level_for(70, 1) == QuotaLevel.PAUSE_KLINE


def test_level_core_only_when_below_main_line_budget():
    # 40 < 43 → 只剩主线
    assert _level_for(40, 1) == QuotaLevel.CORE_ONLY


def test_level_core_only_on_last_day_with_zero_left():
    # collect_days=0 时按 1 天算：今天这一轮仍要花，剩 0 额度不能判正常档
    assert _level_for(0, 0) == QuotaLevel.CORE_ONLY


def test_level_normal_on_last_day_with_enough_for_one_day():
    assert _level_for(500, 0) == QuotaLevel.NORMAL


def test_cycle_boundaries_mid_month():
    assert cycle_start(date(2026, 10, 18), 17) == date(2026, 10, 17)
    assert cycle_start(date(2026, 10, 16), 17) == date(2026, 9, 17)
    assert cycle_end(date(2026, 9, 17), 17) == date(2026, 10, 16)


def test_cycle_boundaries_cross_year():
    assert cycle_start(date(2026, 1, 5), 17) == date(2025, 12, 17)
    assert cycle_end(date(2025, 12, 17), 17) == date(2026, 1, 16)


# ------------------------------------------------ 判档：日耗模型不再是死数


def test_level_for_uses_dynamic_core_budget():
    """自选股变多 → 主线日耗抬高 → 同样的剩余额度会更早判到「告急」。"""
    # 基线：剩 50、还要 1 天 → 50 < 71 但 ≥ 43，判「告急」
    assert _level_for(50, 1) == QuotaLevel.PAUSE_KLINE
    # 自选多 20 只 → 主线日耗 63，剩 60 就不够了 → CORE_ONLY
    assert _level_for(60, 1, core_daily=CORE_DAILY + 20) == QuotaLevel.CORE_ONLY
    assert _level_for(70, 1, core_daily=CORE_DAILY + 20) == QuotaLevel.PAUSE_KLINE
    # 满负荷那一档也跟着抬高
    assert (
        _level_for((FULL_DAY_CALLS + 20) * 2, 2, full_daily=FULL_DAY_CALLS + 20)
        == QuotaLevel.NORMAL
    )
    assert (
        _level_for((FULL_DAY_CALLS + 20) * 2 - 1, 2, full_daily=FULL_DAY_CALLS + 20)
        == QuotaLevel.PAUSE_HIT_DDE
    )


# ------------------------------------------------ 跨年：日历上界不能截断 days_left


def test_gap_trade_days_estimates_missing_calendar_range():
    """日历只到 12-31、周期末在 1-16 → 缺的 16 天按 5/7 折算 ≈ 12 个交易日。"""
    assert _gap_trade_days(date(2026, 12, 31), date(2026, 12, 30), date(2027, 1, 16)) == 12


def test_gap_trade_days_zero_when_calendar_covers_cycle():
    assert _gap_trade_days(date(2027, 1, 20), date(2026, 12, 30), date(2027, 1, 16)) == 0
    assert _gap_trade_days(None, date(2026, 12, 30), date(2027, 1, 16)) == 0


def test_gap_trade_days_does_not_count_past_days():
    """日历整个缺、且末日早于今天：只从今天往后估，不把已过的日子算进来。"""
    # 从今天（1-05）到周期末（1-16）11 天 × 5/7 ≈ 8（而不是从 1-01 起算的 11 天）
    assert _gap_trade_days(date(2026, 12, 31), date(2027, 1, 6), date(2027, 1, 16)) == 8
