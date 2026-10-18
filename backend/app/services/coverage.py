"""日线覆盖率闸的**纯逻辑**（不碰数据库，方便单测）。

为什么单独抽出来：这个判据同时被两处用 —— 形态扫描的前置校验
（`jobs/scan_patterns._require_bars`）和简报 AI 点评的「派生读数能不能喂」
（`services/brief_ai._coverage_ok`）。两处各写一遍时，改了一边忘了另一边就会出现
「扫描说没采全、点评却照常引用恐贪/宽度」这种自相矛盾。

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
