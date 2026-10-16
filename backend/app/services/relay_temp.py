"""短线接力温度（0-100，越高越热）的**评分**部分。

与恐贪指数的分工相同：本模块只把打板生态的原始值折算成分数与分档，
**不碰数据库**；原始数据直接读 `market_sentiment` 与 `limit_pool`（由既有采集
链按日积累），序列在 API 读取时现算 —— 与恐贪「原始值落库、分数现算」同一套
约定，阈值改了不用回补数据。

与恐贪刻意**不用滚动分位**，改用固定阈值映射：
- 打板类数据（连板高度、晋级率、昨涨停溢价）在本站的积累历史还短，一年分位
  没有足够的样本喂；
- 这些量天然有刻度 —— 封板率 80% 就是高、8 板就是顶，不需要「相对过去一年」
  来定义冷热。

六项子指标。`limit_strength` 与恐贪共用同一口径（涨跌停强度），其余五项是
恐贪没有覆盖的打板生态维度；**封板率与炸板率和为 100，只取封板率一项**，
避免同一信息计两次权重。
"""

# 综合分至少要有这么多个有效子指标才认（不拿一两项硬凑）
MIN_SUBS = 3

# 分档：（上界, 名称, key）。阈值与中文名**只在这里写一份**，前端拿 `bands()`
# 画色带与横线，后端调档前端跟着动。
LEVELS: tuple[tuple[float, str, str], ...] = (
    (20, "冰点", "frozen"),
    (40, "偏冷", "cold"),
    (60, "温和", "mild"),
    (80, "活跃", "hot"),
    (100, "沸腾", "boiling"),
)

SUBS: tuple[dict, ...] = (
    {
        "key": "seal_rate",
        "label": "封板率",
        "unit": "%",
        "hint": "涨停 ÷（涨停 + 炸板），80% 以上算热",
    },
    {
        "key": "max_consecutive",
        "label": "连板高度",
        "unit": "板",
        "hint": "当日最高连板数，8 板封顶映射满分",
    },
    {
        "key": "promote_rate",
        "label": "晋级率",
        "unit": "%",
        "hint": "昨日首板（1 板）的票今天仍封涨停的占比",
    },
    {
        "key": "yesterday_limit_avg",
        "label": "昨涨停今表现",
        "unit": "%",
        "hint": "昨日涨停股今日平均涨幅，[-5%, +5%] 映射 0-100",
    },
    {
        "key": "limit_strength",
        "label": "涨跌停强度",
        "unit": "%",
        "hint": "(涨停 − 跌停) ÷ (涨停 + 跌停)，平移到 0-100（与恐贪同口径）",
    },
    {
        "key": "down_pressure",
        "label": "跌停压力",
        "unit": "家",
        "hint": "100 − 跌停家数（下限 0）：跌停越多越冷",
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
    """五档的边界（`[lower, upper)`）与名称，分档阈值的**唯一出处**。"""
    result: list[dict] = []
    lower = 0.0
    for upper, name, key in LEVELS:
        result.append({"key": key, "label": name, "lower": lower, "upper": upper})
        lower = upper
    return result


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return min(max(value, low), high)


def score_subs(raw: dict) -> list[dict]:
    """把一天的原始值折算成六项明细（每项含原始值与 0-100 分）。

    `raw` 的键：`seal_rate`（%）、`max_consecutive`（板）、`promote_rate`（%）、
    `yesterday_limit_avg`（%）、`limit_up_count` / `limit_down_count`（家）。
    任一原始值为 None 时该项分数为 None（不参与综合分）。
    """
    limit_up = raw.get("limit_up_count")
    limit_down = raw.get("limit_down_count")
    limit_strength = None
    if limit_up is not None and limit_down is not None and limit_up + limit_down > 0:
        limit_strength = (limit_up - limit_down) / (limit_up + limit_down) * 100

    mappers = {
        # 封板率本身就是 0-100 的百分数
        "seal_rate": (raw.get("seal_rate"), lambda v: _clamp(v)),
        # 连板高度 8 板封顶线性映射
        "max_consecutive": (
            raw.get("max_consecutive"),
            lambda v: _clamp(v / 8 * 100),
        ),
        "promote_rate": (raw.get("promote_rate"), lambda v: _clamp(v)),
        # 昨涨停今均涨幅 [-5%, +5%] → [0, 100]
        "yesterday_limit_avg": (
            raw.get("yesterday_limit_avg"),
            lambda v: _clamp((v + 5) * 10),
        ),
        # 涨跌停强度 [-100, +100] → [0, 100]
        "limit_strength": (limit_strength, lambda v: _clamp((v + 100) / 2)),
        # 跌停家数 → 压力分：0 家跌停 = 100，100 家以上 = 0
        "down_pressure": (limit_down, lambda v: _clamp(100 - v)),
    }

    subs: list[dict] = []
    for sub in SUBS:
        value, mapper = mappers[sub["key"]]
        score = round(mapper(value), 1) if value is not None else None
        # 展示用原始值：涨跌停强度是算出来的，其余直接是原始读数
        shown = limit_strength if sub["key"] == "limit_strength" else value
        if shown is not None and sub["key"] == "limit_strength":
            shown = round(shown, 2)
        subs.append(
            {
                "key": sub["key"],
                "label": sub["label"],
                "value": shown,
                "score": score,
                "unit": sub["unit"],
                "hint": sub["hint"],
            }
        )
    return subs


def combine(subs: list[dict]) -> float | None:
    """六项有效分的等权平均；有效项不足 `MIN_SUBS` 时记 None。"""
    scores = [sub["score"] for sub in subs if sub["score"] is not None]
    if len(scores) < MIN_SUBS:
        return None
    return round(sum(scores) / len(scores), 1)


def build_series(rows, up_pool: dict, first_board: dict) -> list[dict]:
    """把 `market_sentiment` 的行（**按 trade_date 升序**）折算成逐日读数序列。

    `rows`：ORM 行或有同名属性的对象（用到 seal_rate / max_consecutive /
    yesterday_limit_today_avg / limit_up_count / limit_down_count）。

    `up_pool` / `first_board`：`{trade_date: set(code)}` —— 当日涨停池的全部代码、
    以及其中首板（consecutive == 1）的代码。晋级率 = **昨日**首板集合 ∩ **今日**
    涨停池 ÷ 昨日首板数；首日没有「昨日」时该项为 None（不参与综合分）。

    每个元素：`{trade_date, score, level, subs}`，与恐贪的 `build_series` 同构。
    """
    series: list[dict] = []
    prev_first: set | None = None
    for row in rows:
        day = row.trade_date
        promote_rate = None
        if prev_first:
            promoted = len(prev_first & up_pool.get(day, set()))
            promote_rate = round(promoted / len(prev_first) * 100, 2)
        subs = score_subs(
            {
                "seal_rate": row.seal_rate,
                "max_consecutive": row.max_consecutive,
                "promote_rate": promote_rate,
                "yesterday_limit_avg": row.yesterday_limit_today_avg,
                "limit_up_count": row.limit_up_count,
                "limit_down_count": row.limit_down_count,
            }
        )
        score = combine(subs)
        series.append(
            {
                "trade_date": day,
                "score": score,
                "level": level_of(score),
                "subs": subs,
            }
        )
        # 昨日首板集合只保留最近一天：晋级率永远是「昨日首板 → 今日涨停」
        prev_first = first_board.get(day) or None
    return series


def latest(series: list[dict]) -> dict | None:
    """序列里最后一天的读数 + 较上一个**有效**读数的变化量。空序列返回 None。"""
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


def slice_window(series: list[dict], days: int) -> tuple[list, list[float | None]]:
    """取序列尾巴的 `days` 天，返回（日期表, 分数表），两者等长、一一对应。"""
    tail = series[-days:] if days < len(series) else series
    return [item["trade_date"] for item in tail], [item["score"] for item in tail]
