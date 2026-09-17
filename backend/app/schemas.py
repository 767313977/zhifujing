"""API 响应模型。"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------- 指数


class IndexQuote(ApiModel):
    code: str
    name: str | None
    close: float | None
    pct_chg: float | None
    amount: float | None
    up_count: int | None
    down_count: int | None
    limit_up_count: int | None
    limit_down_count: int | None


# --------------------------------------------------------------------- 情绪


class SentimentOut(ApiModel):
    trade_date: date
    limit_up_count: int | None
    limit_down_count: int | None
    broken_count: int | None
    seal_rate: float | None
    broken_rate: float | None
    max_consecutive: int | None
    up_count: int | None
    down_count: int | None
    total_amount: float | None
    yesterday_limit_today_avg: float | None


class MarketOverview(BaseModel):
    trade_date: date
    indexes: list[IndexQuote]
    sentiment: SentimentOut | None


class IndexSeries(BaseModel):
    """一条指数序列。各数组与 IndexHistory.dates 一一对应，缺失为 null。"""

    code: str
    name: str | None
    close: list[float | None]
    pct_chg: list[float | None]
    amount: list[float | None]


class IndexHistory(BaseModel):
    """指数历史序列，按日期升序对齐，直接喂给图表。"""

    dates: list[date]
    series: list[IndexSeries]


# --------------------------------------------------------------- 涨停板三池


class LimitStock(ApiModel):
    code: str
    name: str | None
    pct_chg: float | None
    price: float | None
    amount: float | None
    float_mv: float | None
    total_mv: float | None
    turnover: float | None
    seal_amount: float | None
    first_seal_time: str | None
    last_seal_time: str | None
    open_times: int | None
    consecutive: int | None
    industry: str | None


class LadderLevel(BaseModel):
    """涨停梯队的一层。"""

    consecutive: int
    count: int
    stocks: list[LimitStock]


class LimitPoolOut(BaseModel):
    trade_date: date
    pool_type: str
    total: int
    # 梯队分层只对涨停有意义（跌停是「连续跌停」、炸板没有连板数）
    ladder: list[LadderLevel] | None
    stocks: list[LimitStock]


class PromotionLevel(BaseModel):
    """某一档连板的晋级情况。各数组与 PromotionSeries.dates 一一对应。"""

    level: int
    label: str
    # 昨日该档的股票数
    counts: list[int]
    # 其中今日晋级到下一档的数量
    promoted: list[int]
    # 晋级率（百分比），昨日该档无票时为 null
    rates: list[float | None]


class PromotionSeries(BaseModel):
    """连板晋级率序列。打板复盘的核心指标。"""

    dates: list[date]
    levels: list[PromotionLevel]
    overall_counts: list[int]
    overall_promoted: list[int]
    overall_rates: list[float | None]


# ------------------------------------------------------------------ 龙虎榜


class LhbOut(ApiModel):
    trade_date: date
    code: str
    name: str | None
    reason: str
    close: float | None
    pct_chg: float | None
    net_buy: float | None
    buy_amount: float | None
    sell_amount: float | None
    interpretation: str | None


# --------------------------------------------------------------- 数据管理


class CollectStepResult(BaseModel):
    status: str
    rows: int
    cost: float
    message: str | None


class CollectResult(BaseModel):
    trade_date: str
    steps: dict[str, CollectStepResult]


class CollectLogOut(ApiModel):
    trade_date: date | None
    task: str
    status: str
    rows: int | None
    message: str | None
    cost_seconds: float | None
    created_at: datetime


class AdminStatus(BaseModel):
    latest_sentiment_date: date | None
    latest_limit_pool_date: date | None
    latest_lhb_date: date | None
    data_days: int
    recent_logs: list[CollectLogOut]


# -------------------------------------------------------------------- 选股器


class ScreenRunOut(BaseModel):
    """自然语言选股结果。

    列是动态的 —— iFinD 按提问内容决定返回哪些指标，且列名自带日期
    （如 `总市值[20260917]`），所以不能写死表头。
    """

    query: str
    columns: list[str]
    rows: list[dict[str, str]]
    # 匹配总数
    matched: int | None
    # 表格实际给出的行数（上限 100）
    returned: int
    # matched > returned 即为被截断，必须让用户看到
    truncated: bool
    answer: str
    cost_seconds: float


class PresetIn(BaseModel):
    name: str
    query: str


class PresetOut(ApiModel):
    id: int
    name: str
    kind: str
    conditions: dict
    created_at: datetime


# -------------------------------------------------------------------- 自选股


class WatchlistIn(BaseModel):
    code: str
    name: str | None = None
    note: str | None = None


class WatchlistOut(ApiModel):
    code: str
    name: str | None
    tags: list[str] | None
    note: str | None
    added_at: datetime
