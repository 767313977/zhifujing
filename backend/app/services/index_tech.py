"""指数的技术位、量价配合度，以及「指数 vs 个股广度」的背离判断。

**全部由本地 `index_daily` 算出来，0 次外部调用** —— 均线、量比、背离都不需要
新数据源，只是同一批收盘价与成交额的再解读。所以这些字段可以在每次打开复盘页时
现算，不必落库；也就不存在「历史回填」的问题（往前 252 天都能算）。

阈值都是**判读口径**而不是事实，所以集中写在这里、并在页面上把依据数字带出来
（「上证 +0.62%，上涨家数仅 31%」），免得结论看起来像个黑盒。
"""

# 用户要的是「5 日 / 20 日关键均线」
MA_WINDOWS = (5, 20)

# 量比的基准：当日成交额 / **前** N 个交易日的均额（不含当日）。
# 不含当日是标准做法 —— 含进去的话放量当天会把基准也抬高，量比永远接近 1
VOLUME_BASELINE = 5
HEAVY_VOLUME = 1.1  # 量比 ≥ 此值记「放量」
LIGHT_VOLUME = 0.9  # ≤ 此值记「缩量」

# 算一个指数的技术位要往前取多少根日线：MA20 要 20 根，量比的基准再往前 5 根。
# 调用方直接用它做查询上限，别自己再写一个数
LOOKBACK_BARS = max(MA_WINDOWS) + VOLUME_BASELINE

# 背离判断的阈值
INDEX_MOVE = 0.3  # 指数涨跌超过这个幅度才算「明确上涨/下跌」，其余算「微幅」
BREADTH_WEAK = 40.0  # 上涨家数占比低于此值记「个股普跌」
BREADTH_STRONG = 60.0  # 高于此值记「个股普涨」

# 判断背离时看哪个指数。用上证指数：散户语境里「大盘」默认就是它
BENCHMARK_CODE = "000001.SH"


def moving_average(closes: list[float], window: int) -> float | None:
    """最近 `window` 个收盘价的均值。不足 window 根就给 None（不拿短的凑）。"""
    if len(closes) < window:
        return None
    return round(sum(closes[-window:]) / window, 2)


def index_tech(
    closes: list[float],
    amounts: list[float | None],
    pct_chg: float | None,
) -> dict:
    """一个指数的技术位。`closes` / `amounts` 都按日期**升序**，最后一个是当日。

    返回 `ma5 / ma20 / above_ma5 / above_ma20 / vol_ratio / vol_price`。
    缺数据的项一律 None：均线不足根数、前 N 日成交额有缺口时不硬凑，
    否则画出来的是「半条均线」或一个假量比。
    """
    close = closes[-1] if closes else None
    tech: dict = {}
    for window in MA_WINDOWS:
        ma = moving_average(closes, window)
        tech[f"ma{window}"] = ma
        tech[f"above_ma{window}"] = (
            None if ma is None or close is None else close > ma
        )

    # 量比：前 VOLUME_BASELINE 个交易日的均额（不含当日），必须一根不缺
    baseline = amounts[-(VOLUME_BASELINE + 1) : -1] if len(amounts) > VOLUME_BASELINE else []
    today_amount = amounts[-1] if amounts else None
    usable = [value for value in baseline if value]
    if len(usable) == VOLUME_BASELINE and today_amount:
        tech["vol_ratio"] = round(today_amount / (sum(usable) / VOLUME_BASELINE), 2)
    else:
        tech["vol_ratio"] = None

    tech["vol_price"] = vol_price_label(tech["vol_ratio"], pct_chg)
    return tech


def vol_price_label(vol_ratio: float | None, pct_chg: float | None) -> str | None:
    """量价配合标签：`放量上涨` / `缩量下跌` 这类两字组合。

    分开给两个维度（量的相对大小 + 价格方向），而不是合成一个「配合度分数」：
    分数看不出是哪种不配合，而「放量下跌」和「缩量上涨」要采取的动作完全不同。
    """
    if vol_ratio is None:
        return None
    if vol_ratio >= HEAVY_VOLUME:
        volume = "放量"
    elif vol_ratio <= LIGHT_VOLUME:
        volume = "缩量"
    else:
        volume = "平量"
    if pct_chg is None:
        return volume
    if pct_chg > 0:
        direction = "上涨"
    elif pct_chg < 0:
        direction = "下跌"
    else:
        direction = "横盘"
    return f"{volume}{direction}"


def divergence(
    benchmarks: list[tuple[str, float | None]],
    up_count: int | None,
    down_count: int | None,
) -> dict | None:
    """指数与个股广度是否背离。

    `benchmarks` 是 (指数名, 涨跌幅) 的候选，按优先级给；取第一个有值的当基准。
    返回 `{"level", "title", "detail"}`，`level` 供前端上色：

    - `weight_pull` 指数明确上涨、个股却普跌 → 权重拉抬，实际情绪偏弱
    - `theme_active` 指数下跌、个股却普涨 → 题材活跃，可轻仓参与
    - `aligned` 两者同向（普涨或普跌）

    `up_count` / `down_count` 只有当日值（乐咕只给当日），所以历史日期这里返回
    None —— 页面就不显示这一条，而不是拿旧宽度硬套。
    """
    if up_count is None or down_count is None:
        return None
    total = up_count + down_count
    if total == 0:
        return None
    breadth = up_count / total * 100
    name, pct = next(((n, p) for n, p in benchmarks if p is not None), (None, None))
    if name is None or pct is None:
        return None

    detail = (
        f"{name} {pct:+.2f}%，上涨家数 {up_count} / 下跌 {down_count}"
        f"（占比 {breadth:.0f}%）"
    )
    if pct >= INDEX_MOVE and breadth < BREADTH_WEAK:
        return {
            "level": "weight_pull",
            "title": "指数上涨但个股普跌 —— 大概率是权重拉抬，实际情绪偏弱",
            "detail": detail,
        }
    if pct <= -INDEX_MOVE and breadth > BREADTH_STRONG:
        return {
            "level": "theme_active",
            "title": "指数下跌但个股普涨 —— 题材活跃，可轻仓参与",
            "detail": detail,
        }
    if breadth > BREADTH_STRONG:
        return {
            "level": "aligned",
            "title": "指数与个股同向：普涨",
            "detail": detail,
        }
    if breadth < BREADTH_WEAK:
        return {
            "level": "aligned",
            "title": "指数与个股同向：普跌",
            "detail": detail,
        }
    return {
        "level": "aligned",
        "title": "涨跌家数均衡，指数与个股没有明显背离",
        "detail": detail,
    }
