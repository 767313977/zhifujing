"""RPS 的**计算口径**（纯函数，不碰数据库）—— 线上扫描与回测共用这一份。

2026-10-24 抽出来（「选股池-8」）：之前线上按 `pct_chg` 复利 + 中位秩、回测按
`close[i]/close[i-N]` 按行对齐 + `rank/(n-1)`，两套口径在阈值边上会给出不同结论，
而页面的「双高」档正是按回测口径验证出来的。现在只有这一份：

- 区间涨幅用 `pct_chg` **复利**（库里是不复权价，直接比收盘价会被除权除息带偏）；
- 窗口按**交易日**截取（`window_values`），只复利不早于窗口起点的行；
- 窗口内实际行数至少要有 `MIN_COVERAGE`（90%），否则该档记 None；
- 分位用**中位秩**（与恐贪、估值分位同一套定义）。

回测里 `window_values` 的"起点"来自全市场交易日轴（`trade_calendar` 的并集），
不是单只票自己的日期 —— 停牌会让两者不同，而分位是横截面概念，必须按交易日。
"""

from bisect import bisect_left, bisect_right

#: 窗口内至少要有这么多比例的实际交易日，否则该档记 None。
#: 0.9 能让「停牌几天」的票照常参与，又不至于拿半个窗口当 250 日涨幅。
MIN_COVERAGE = 0.9


def ret(values: list[float], days: int) -> float | None:
    """窗口涨幅（%）。`values` 是该股在窗口内按日期升序的日涨跌幅。"""
    if len(values) < int(days * MIN_COVERAGE):
        return None
    factor = 1.0
    for pct in values[-days:]:
        factor *= 1 + pct / 100
    return round((factor - 1) * 100, 2)


def window_values(rows: list[tuple[object, float]], start: object) -> list[float]:
    """只留**不早于 `start`** 的行。

    窗口必须按交易日截取，不能按「最近 N 行」：停牌或漏采会让 N 行跨过更多交易日，
    算出来的是「更长的区间」，与说明里的「N 个交易日涨幅」不是一回事。
    """
    return [pct for day, pct in rows if day >= start]


def mid_rank(ordered: list[float], value: float) -> float:
    """中位秩百分位（0-100）。`ordered` 必须是升序、且已去掉 None。"""
    left = bisect_left(ordered, value)
    right = bisect_right(ordered, value)
    return round((left + right) / 2 / len(ordered) * 100, 1)


def percentiles(values: list[float]) -> list[float]:
    """同一批数值在该批里的百分位（0~100，最大 = 100），返回顺序与输入一致。

    ⚠️ 与 `mid_rank` **不是**同一套：这里是「排名 / (n−1)」的端点式分位，专给
    「池内排序」（首板综合分、因子打分）用 —— 只有一只票时给 100；它不用于 RPS。

    ⚠️ **并列值必须给同一分位**（2026-10-28 体检）：原来按「排序后的名次」逐个赋分，
    于是三只票的题材家数都是 3（≥3 家很常见的并列）时会拿到 `0 / 50 / 100`，
    `theme_pct × 0.4` 一项就凭空差 40 分 —— 排序结果取决于输入顺序（首封时间），
    不是候选本身的属性。现在并列取**平均名次**（与 `mid_rank` 的中位秩同义）。
    """
    if not values:
        return []
    if len(values) == 1:
        return [100.0]
    order = sorted(range(len(values)), key=lambda index: values[index])
    percent = [0.0] * len(values)
    span = len(values) - 1
    start = 0
    while start < len(values):
        end = start
        # 并列区间 [start, end]：值相等就必须同分
        while end + 1 < len(values) and values[order[end + 1]] == values[order[start]]:
            end += 1
        score = (start + end) / 2 / span * 100
        for position in range(start, end + 1):
            percent[order[position]] = score
        start = end + 1
    return percent
