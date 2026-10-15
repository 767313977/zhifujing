"""A 股恐惧贪婪指数（0-100，越高越贪婪）的**评分**部分。

分工：本模块只把「六项原始指标」折算成分数与分档，**不碰数据库写入**
（原始值由 `jobs/collect_fear_greed.py` 采集落库，表是 `FearGreedDaily`）。

口径：每项原始值按**滚动 250 个交易日分位**映射到 0-100 —— 也就是「今天这个
数在过去一年里排多高」。不用绝对阈值是因为各项量纲与波动区间差得太远：
成交额在 1~3 万亿之间摆、两融变化率在 ±10% 之间摆、涨停跌停差可以是 ±100。

分位窗口只用「截至当日（含当日）」的数据，所以**历史分数天然稳定**：
往后新增交易日不会改写任何已算出日期的分数。
"""

from datetime import date

# 分位窗口（交易日）。250 ≈ 一年，够覆盖一轮完整的情绪冷热
WINDOW = 250
# 窗口内至少要有这么多个非空值才出分。不足时该子指标当天记 None、**不外推**：
# 刚建库没几天就凭 3 个点算「一年分位」，读出来的百分位是假的
MIN_WINDOW = 20

# 综合分至少要有这么多个有效子指标才认，否则记 None（不拿两三项硬凑一个数）
MIN_SUBS = 3

# 分档：（上界, 名称, key），按顺序命中第一个「score <= 上界」的档。
#
# `key` 是给前端配色用的稳定标识 —— 分档阈值与中文名**只在这里写一份**，
# 前端拿 `bands()`（见下）画五档色带与分档横线，所以后端改阈值前端会跟着动，
# 不会出现「图上色带 45、数字按 50 分档」这种对不上的情况。
LEVELS: tuple[tuple[float, str, str], ...] = (
    (25, "极度恐慌", "extreme_fear"),
    (45, "恐慌", "fear"),
    (55, "中性", "neutral"),
    (75, "贪婪", "greed"),
    (100, "极度贪婪", "extreme_greed"),
)

# 六项子指标。`invert=True` 表示「原始值越大越恐慌」（只有波动率是这类）。
SUBS: tuple[dict, ...] = (
    {
        "key": "volatility",
        "label": "波动率",
        "invert": True,
        "unit": "%",
        "hint": "上证指数近 20 日日收益率的年化标准差 —— 越高越恐慌",
    },
    {
        "key": "rsi14",
        "label": "RSI",
        "invert": False,
        "unit": "",
        "hint": "上证指数 RSI(14)，越接近超买越贪婪",
    },
    {
        "key": "turnover_ratio",
        "label": "相对换手率",
        "invert": False,
        "unit": "倍",
        "hint": "两市成交额 ÷ 前 20 个交易日均值 —— 放量越猛越贪婪",
    },
    {
        "key": "margin_chg",
        "label": "融资融券",
        "invert": False,
        "unit": "%",
        "hint": "沪深两市两融余额的 20 日变化率 —— 加杠杆越猛越贪婪",
    },
    {
        "key": "breadth",
        "label": "市场宽度",
        "invert": False,
        "unit": "%",
        "hint": "全市场上涨家数占比（由个股日线反算，非乐咕乐股口径）",
    },
    {
        "key": "limit_strength",
        "label": "涨跌停强度",
        "invert": False,
        "unit": "%",
        "hint": "(涨停 − 跌停) ÷ (涨停 + 跌停)",
    },
)

SUB_KEYS = tuple(sub["key"] for sub in SUBS)


def level_of(score: float | None) -> str | None:
    """分数 → 分档中文名。None 原样返回。"""
    if score is None:
        return None
    for bound, name, _ in LEVELS:
        if score <= bound:
            return name
    return LEVELS[-1][1]


def bands() -> list[dict]:
    """五档的边界（`[lower, upper)`）与名称，给前端画色带与分档横线。

    这是分档阈值的**唯一出处**：前端不自己写 25/45/55/75 这套数字，
    所以后端调档位时图形会跟着变。
    """
    result: list[dict] = []
    lower = 0.0
    for upper, name, key in LEVELS:
        result.append({"key": key, "label": name, "lower": lower, "upper": upper})
        lower = upper
    return result


def percentile_score(values: list[float], value: float, invert: bool = False) -> float | None:
    """`value` 在 `values`（含 value 本身）里的分位，折算成 0-100。

    用**中位秩**而不是「小于等于的比例」：全部取值相同（窗口里六项都没波动，
    比如连续几十天成交额一模一样）时，前者给 50 分（中性），后者会给 100 分
    （满档贪婪）—— 那是把「没信息」读成了「极端」。
    """
    if len(values) < MIN_WINDOW:
        return None
    below = sum(1 for item in values if item < value)
    equal = sum(1 for item in values if item == value)
    rank = (below + equal / 2) / len(values) * 100
    if invert:
        rank = 100 - rank
    return round(rank, 1)


def build_series(rows) -> list[dict]:
    """把 `fear_greed_daily` 的行（**按 trade_date 升序**）折算成逐日读数序列。

    每个元素：`{trade_date, score, level, subs: [{key,label,value,score,unit,hint}]}`。
    行可以是 ORM 对象，也可以是有同名属性的行对象。
    """
    history: dict[str, list[float]] = {key: [] for key in SUB_KEYS}
    series: list[dict] = []

    for row in rows:
        subs: list[dict] = []
        scores: list[float] = []
        for sub in SUBS:
            value = getattr(row, sub["key"], None)
            score = None
            if value is not None:
                # 先入窗口再算分：分位是「今天这个数在过去一年里排多高」，
                # 落在窗口里含自己，不含自己会让今天的极值永远读成 100
                window = history[sub["key"]]
                window.append(value)
                score = percentile_score(window[-WINDOW:], value, invert=sub["invert"])
            if score is not None:
                scores.append(score)
            subs.append(
                {
                    "key": sub["key"],
                    "label": sub["label"],
                    "value": value,
                    "score": score,
                    "unit": sub["unit"],
                    "hint": sub["hint"],
                }
            )
        total = round(sum(scores) / len(scores), 1) if len(scores) >= MIN_SUBS else None
        series.append(
            {
                "trade_date": row.trade_date,
                "score": total,
                "level": level_of(total),
                "subs": subs,
            }
        )

    return series


def latest(series: list[dict]) -> dict | None:
    """序列里最后一天的读数 + 较上一个有效读数的变化量。空序列返回 None。

    变化量往回找**最近一个非空分数**，而不是只看前一行：某天的综合分可能因为
    有效子指标不足而记 None（比如两融还没发布导致只有两项有效），
    这时拿 None 减就是 None，面板上会显示成「—」而不是真实的方向。
    """
    if not series:
        return None
    last = series[-1]
    change = None
    if last["score"] is not None:
        for previous in reversed(series[:-1]):
            if previous["score"] is not None:
                change = round(last["score"] - previous["score"], 1)
                break
    return {
        "trade_date": last["trade_date"],
        "score": last["score"],
        "level": last["level"],
        "change": change,
    }


def slice_window(series: list[dict], days: int) -> tuple[list[date], list[float | None]]:
    """取序列尾巴的 `days` 天，返回（日期表, 分数表），两者等长、一一对应。"""
    tail = series[-days:] if days < len(series) else series
    return [item["trade_date"] for item in tail], [item["score"] for item in tail]
