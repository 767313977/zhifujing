"""日线覆盖率闸的**纯逻辑**（不碰数据库，方便单测）。

判据由形态扫描的前置校验（`jobs/scan_patterns._require_bars`）使用：
覆盖率不到阈值时不出命中，避免拿旧 K 线算出来的形态被当成当天信号。

`pool = 0`（股票池还没建）时返回 False：调用方应把「没有池子」当成不能判断，
而不是「覆盖率为 0 也算通过」。
"""

#: 当天日线至少覆盖股票池的这么多比例，才认为「采全了」。
#:
#: 2026-09-24 实测：274 只（9%）没采到，扫描仍然出了 44 条拿旧 K 线的命中。
#: 现在要求 ≥90%，低于它宁可报错 / 不喂派生读数。
MIN_DAY_COVERAGE = 0.9


def coverage_ok(count: int, pool: int, ratio: float = MIN_DAY_COVERAGE) -> bool:
    """`count` 只有 `pool` 的 `ratio` 以上才算采全。"""
    return bool(pool) and count >= pool * ratio
