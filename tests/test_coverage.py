"""日线覆盖率闸：扫描与派生读数共用同一个纯函数。"""

from app.services.coverage import MIN_DAY_COVERAGE, coverage_ok


def test_coverage_passes_at_threshold():
    pool = 5000
    assert coverage_ok(int(pool * MIN_DAY_COVERAGE), pool)


def test_coverage_fails_just_below_threshold():
    pool = 5000
    assert not coverage_ok(int(pool * MIN_DAY_COVERAGE) - 1, pool)


def test_coverage_without_pool_is_false():
    assert not coverage_ok(0, 0)
    assert not coverage_ok(100, 0)
