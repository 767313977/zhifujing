"""ORM 模型。"""

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TradeCalendar(Base):
    """交易日历。iFinD 的历史行情会返回周末行，必须以此表过滤。"""

    __tablename__ = "trade_calendar"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)


class IndexDaily(Base):
    """指数日线 + 市场宽度。"""

    __tablename__ = "index_daily"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(32))
    close: Mapped[float | None] = mapped_column(Float)
    pct_chg: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    up_count: Mapped[int | None] = mapped_column(Integer)
    down_count: Mapped[int | None] = mapped_column(Integer)
    limit_up_count: Mapped[int | None] = mapped_column(Integer)
    limit_down_count: Mapped[int | None] = mapped_column(Integer)


class StockBasic(Base):
    """个股基础信息。仅覆盖自选股与选股结果，不做全市场落库。"""

    __tablename__ = "stock_basic"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(32))
    industry: Mapped[str | None] = mapped_column(String(128))
    total_mv: Mapped[float | None] = mapped_column(Float)
    float_mv: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class StockDaily(Base):
    """个股日线。"""

    __tablename__ = "stock_daily"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(32))
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    pct_chg: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    turnover: Mapped[float | None] = mapped_column(Float)
    volume_ratio: Mapped[float | None] = mapped_column(Float)


class LimitPool(Base):
    """涨停 / 跌停 / 炸板池。iFinD 无此数据，唯一来源是 akshare push2ex。"""

    __tablename__ = "limit_pool"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    # up=涨停 down=跌停 broken=炸板
    pool_type: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(32))
    pct_chg: Mapped[float | None] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    float_mv: Mapped[float | None] = mapped_column(Float)
    total_mv: Mapped[float | None] = mapped_column(Float)
    turnover: Mapped[float | None] = mapped_column(Float)
    seal_amount: Mapped[float | None] = mapped_column(Float)
    first_seal_time: Mapped[str | None] = mapped_column(String(16))
    last_seal_time: Mapped[str | None] = mapped_column(String(16))
    open_times: Mapped[int | None] = mapped_column(Integer)
    consecutive: Mapped[int | None] = mapped_column(Integer)
    industry: Mapped[str | None] = mapped_column(String(64))


class SectorDaily(Base):
    """板块行情。"""

    __tablename__ = "sector_daily"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    sector_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128))
    # 分类体系（中信 / 同花顺），不同体系口径不可比
    taxonomy: Mapped[str | None] = mapped_column(String(16))
    pct_chg: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    member_count: Mapped[int | None] = mapped_column(Integer)


class SectorMember(Base):
    """板块成分股。"""

    __tablename__ = "sector_member"

    sector_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class Lhb(Base):
    """龙虎榜。iFinD 无此数据，唯一来源是 akshare push2ex。"""

    __tablename__ = "lhb"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    reason: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(32))
    close: Mapped[float | None] = mapped_column(Float)
    pct_chg: Mapped[float | None] = mapped_column(Float)
    net_buy: Mapped[float | None] = mapped_column(Float)
    buy_amount: Mapped[float | None] = mapped_column(Float)
    sell_amount: Mapped[float | None] = mapped_column(Float)
    interpretation: Mapped[str | None] = mapped_column(Text)


class MarketSentiment(Base):
    """市场情绪物化快照。情绪曲线要快速读 60 天，实时计算太慢。"""

    __tablename__ = "market_sentiment"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    limit_up_count: Mapped[int | None] = mapped_column(Integer)
    limit_down_count: Mapped[int | None] = mapped_column(Integer)
    broken_count: Mapped[int | None] = mapped_column(Integer)
    seal_rate: Mapped[float | None] = mapped_column(Float)
    broken_rate: Mapped[float | None] = mapped_column(Float)
    max_consecutive: Mapped[int | None] = mapped_column(Integer)
    up_count: Mapped[int | None] = mapped_column(Integer)
    down_count: Mapped[int | None] = mapped_column(Integer)
    total_amount: Mapped[float | None] = mapped_column(Float)
    # 昨日涨停股今日均涨幅：打板赚钱效应
    yesterday_limit_today_avg: Mapped[float | None] = mapped_column(Float)


class Watchlist(Base):
    """自选股。"""

    __tablename__ = "watchlist"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(32))
    tags: Mapped[list | None] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ReviewNote(Base):
    """每日复盘笔记。"""

    __tablename__ = "review_note"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    market_view: Mapped[str | None] = mapped_column(Text)
    next_plan: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class ScreenPreset(Base):
    """保存的选股条件。"""

    __tablename__ = "screen_preset"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    # natural=自然语言（iFinD search_stocks） structured=本地结构化条件
    kind: Mapped[str] = mapped_column(String(16))
    conditions: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ScreenResult(Base):
    """选股结果快照。"""

    __tablename__ = "screen_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    preset_id: Mapped[int | None] = mapped_column(Integer)
    query: Mapped[str | None] = mapped_column(Text)
    code: Mapped[str] = mapped_column(String(16))
    name: Mapped[str | None] = mapped_column(String(32))
    industry: Mapped[str | None] = mapped_column(String(128))
    extra: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class CollectLog(Base):
    """采集日志。"""

    __tablename__ = "collect_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date | None] = mapped_column(Date)
    task: Mapped[str] = mapped_column(String(32))
    # ok / partial / failed
    status: Mapped[str] = mapped_column(String(16))
    rows: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)
    cost_seconds: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


Index("ix_stock_daily_code", StockDaily.code)
Index("ix_limit_pool_type", LimitPool.trade_date, LimitPool.pool_type)
Index("ix_lhb_code", Lhb.code)
