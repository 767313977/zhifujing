"""大盘与情绪接口。"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import resolve_trade_date
from app.db import get_db
from app.models import IndexDaily, MarketSentiment
from app.schemas import (
    DivergenceOut,
    IndexHistory,
    IndexQuote,
    IndexSeries,
    MarketOverview,
    SentimentOut,
    TurnoverSeries,
)
from app.services.index_tech import (
    BENCHMARK_CODE,
    LOOKBACK_BARS,
    divergence,
    index_tech,
)

router = APIRouter(prefix="/api/market", tags=["复盘"])

# 图表展示顺序：大盘指数在前，小盘/北证在后
INDEX_ORDER = [
    "000001.SH",
    "399001.SZ",
    "399006.SZ",
    "000688.SH",
    "000852.SZ",
    "899050.BJ",
]

# 成交额柱状图的回看长度（交易日）。30 天 ≈ 一个半月，够看出量能在放大还是萎缩；
# 再长柱子就细到分不出相邻两根了
TURNOVER_BARS = 30


@router.get("/overview", response_model=MarketOverview)
def overview(
    trade_date: date = Depends(resolve_trade_date),
    session: Session = Depends(get_db),
) -> MarketOverview:
    """今日复盘首页数据：指数卡片（含技术位）+ 情绪指标 + 背离判断。

    技术位（MA5/MA20、量比、量价标签）与背离判断都在这里**现算**：
    数据就是库里那几条指数日线，不需要新数据源，也就不存在落库与回填的问题
    （往前拉多少天都能算，见 `services/index_tech.py`）。
    """
    indexes = list(
        session.scalars(
            select(IndexDaily)
            .where(IndexDaily.trade_date == trade_date)
            .order_by(IndexDaily.code)
        )
    )
    sentiment = session.scalars(
        select(MarketSentiment).where(MarketSentiment.trade_date == trade_date)
    ).first()

    # 每个指数各取最近 LOOKBACK_BARS 天（MA20 要 20 根，量比基准再往前 5 根）。
    # 逐指数一条小查询而不是一条大 IN 查询：每天最多 6 条、每条 ≤25 行
    quotes: list[IndexQuote] = []
    for row in indexes:
        history = list(
            session.scalars(
                select(IndexDaily)
                .where(
                    IndexDaily.code == row.code,
                    IndexDaily.trade_date <= trade_date,
                )
                .order_by(IndexDaily.trade_date.desc())
                .limit(LOOKBACK_BARS)
            )
        )
        history.reverse()
        tech = index_tech(
            [item.close for item in history if item.close is not None],
            [item.amount for item in history],
            row.pct_chg,
        )
        quotes.append(IndexQuote.model_validate(row).model_copy(update=tech))

    quotes.sort(key=lambda item: INDEX_ORDER.index(item.code) if item.code in INDEX_ORDER else 99)

    judge = divergence(
        [(item.name or item.code, item.pct_chg) for item in quotes if item.code == BENCHMARK_CODE]
        or [(item.name or item.code, item.pct_chg) for item in quotes[:1]],
        up_count=sentiment.up_count if sentiment else None,
        down_count=sentiment.down_count if sentiment else None,
    )

    # 成交额序列按「截至 trade_date」取尾巴，而不是取全库最近 30 天：
    # 翻到历史日期时，后面那几天还没发生，画出来等于泄露未来
    turnover_rows = list(
        session.scalars(
            select(MarketSentiment)
            .where(MarketSentiment.trade_date <= trade_date)
            .order_by(MarketSentiment.trade_date.desc())
            .limit(TURNOVER_BARS)
        )
    )
    turnover_rows.reverse()

    turnover: TurnoverSeries | None = None
    if turnover_rows:
        days = [row.trade_date for row in turnover_rows]
        # 柱子的红绿跟**上证指数**当天的涨跌走，而不是跟成交额自己比 ——
        # K 线副图的老惯例，这样柱子同时说了「量价配合」：
        # 红柱=放量上涨、绿柱=缩量下跌，一眼能认
        bench_pct = {
            row[0]: row[1]
            for row in session.execute(
                select(IndexDaily.trade_date, IndexDaily.pct_chg).where(
                    IndexDaily.code == BENCHMARK_CODE,
                    IndexDaily.trade_date >= days[0],
                    IndexDaily.trade_date <= days[-1],
                )
            ).all()
        }
        turnover = TurnoverSeries(
            dates=days,
            amounts=[row.total_amount for row in turnover_rows],
            pct_chg=[bench_pct.get(day) for day in days],
        )

    return MarketOverview(
        trade_date=trade_date,
        indexes=quotes,
        sentiment=SentimentOut.model_validate(sentiment) if sentiment else None,
        divergence=DivergenceOut(**judge) if judge else None,
        turnover=turnover,
    )


@router.get("/sentiment", response_model=list[SentimentOut])
def sentiment_series(
    days: int = Query(60, ge=1, le=250, description="返回最近 N 个交易日，按日期升序"),
    session: Session = Depends(get_db),
) -> list[SentimentOut]:
    """情绪周期曲线数据。"""
    rows = list(
        session.scalars(
            select(MarketSentiment)
            .order_by(MarketSentiment.trade_date.desc())
            .limit(days)
        )
    )
    return [SentimentOut.model_validate(row) for row in reversed(rows)]


@router.get("/dates", response_model=list[date])
def available_dates(
    days: int = Query(60, ge=1, le=250),
    session: Session = Depends(get_db),
) -> list[date]:
    """库中已有数据的交易日，供前端日期切换。"""
    rows = list(
        session.scalars(
            select(MarketSentiment.trade_date)
            .order_by(MarketSentiment.trade_date.desc())
            .limit(days)
        )
    )
    return list(reversed(rows))


@router.get("/index-history", response_model=IndexHistory)
def index_history(
    days: int = Query(120, ge=5, le=500, description="返回最近 N 个有指数数据的交易日"),
    session: Session = Depends(get_db),
) -> IndexHistory:
    """指数历史序列，按日期升序对齐。

    用途是给短周期的情绪指标提供**长周期背景**：三池数据源只保留最近
    15 个交易日，而指数有完整历史，所以两者分开成图而不是画在同一时间轴上。
    """
    recent = list(
        session.scalars(
            select(IndexDaily.trade_date)
            .distinct()
            .order_by(IndexDaily.trade_date.desc())
            .limit(days)
        )
    )
    if not recent:
        raise HTTPException(status_code=404, detail="暂无指数数据，请先执行采集或回补")

    dates = sorted(recent)
    position = {day: i for i, day in enumerate(dates)}

    rows = list(
        session.scalars(
            select(IndexDaily)
            .where(
                IndexDaily.trade_date >= dates[0],
                IndexDaily.trade_date <= dates[-1],
            )
            .order_by(IndexDaily.code, IndexDaily.trade_date)
        )
    )

    # 按日期位置填充，缺失留 None，保证各数组与 dates 严格等长
    buckets: dict[str, dict] = {}
    for row in rows:
        bucket = buckets.setdefault(
            row.code,
            {
                "name": row.name,
                "close": [None] * len(dates),
                "pct_chg": [None] * len(dates),
                "amount": [None] * len(dates),
            },
        )
        idx = position[row.trade_date]
        bucket["close"][idx] = row.close
        bucket["pct_chg"][idx] = row.pct_chg
        bucket["amount"][idx] = row.amount

    order = {code: i for i, code in enumerate(INDEX_ORDER)}
    series = [
        IndexSeries(code=code, name=data["name"], **{k: data[k] for k in ("close", "pct_chg", "amount")})
        for code, data in sorted(
            buckets.items(), key=lambda item: order.get(item[0], len(INDEX_ORDER))
        )
    ]
    return IndexHistory(dates=dates, series=series)
