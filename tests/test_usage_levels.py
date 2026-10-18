"""配额判档与订阅周期边界（纯函数，不碰数据库）。"""

from datetime import date

from app.services.usage import QuotaLevel, _level_for, cycle_end, cycle_start


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


def test_level_normal_on_last_day_even_with_zero_left():
    # collect_days=0 = 今天就是周期最后一天：额度不累积，该花就花
    assert _level_for(0, 0) == QuotaLevel.NORMAL


def test_cycle_boundaries_mid_month():
    assert cycle_start(date(2026, 10, 18), 17) == date(2026, 10, 17)
    assert cycle_start(date(2026, 10, 16), 17) == date(2026, 9, 17)
    assert cycle_end(date(2026, 9, 17), 17) == date(2026, 10, 16)


def test_cycle_boundaries_cross_year():
    assert cycle_start(date(2026, 1, 5), 17) == date(2025, 12, 17)
    assert cycle_end(date(2025, 12, 17), 17) == date(2026, 1, 16)
