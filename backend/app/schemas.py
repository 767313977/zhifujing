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
