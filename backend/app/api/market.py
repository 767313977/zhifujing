"""大盘与情绪接口。"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import resolve_trade_date
from app.db import get_db
from app.models import FearGreedDaily, IndexDaily, MarketSentiment
from app.schemas import (
    DivergenceOut,
    FearGreedBand,
    FearGreedBrief,
    FearGreedLatest,
    FearGreedOut,
    IndexHistory,
    IndexQuote,
    IndexSeries,
    MarketOverview,
    SentimentOut,
    TurnoverSeries,
)
from app.services.fear_greed import WINDOW, bands, build_series, latest, slice_window
from app.services.index_tech import (
    BENCHMARK_CODE,
    LOOKBACK_BARS,
    divergence,
    index_tech,
)

logger = logging.getLogger(__name__)

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

# 恐贪曲线的默认回看长度（交易日）。120 ≈ 半年，够看出情绪从冷到热的整段摆动
FEAR_GREED_DAYS = 120


def _bands() -> list[FearGreedBand]:
    """分档阈值（后端是唯一出处，首页色带与情绪周期页共用同一份）。"""
    return [FearGreedBand(**band) for band in bands()]


def _fear_greed_series(session: Session, until: date | None = None) -> list[dict]:
    """读全表的原始值，折算成逐日读数。

    **必须读全表**（而不是只读要展示的那段）：分位是「今天这个数在过去 250 个
    交易日里排多高」，截断历史会把窗口喂瘦、算出来的分位偏高。表本身很小
    （一天一行），全读的代价可以忽略。

    `until` 用来对齐历史日期视图：翻到 9-10 那天时不能把 9-11 之后的读数算进来。
    """
    query = select(FearGreedDaily).order_by(FearGreedDaily.trade_date)
    if until is not None:
        query = query.where(FearGreedDaily.trade_date <= until)
    return build_series(session.scalars(query))


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
        # `closes` 与 `amounts` 必须**按位置一一对应**（`index_tech` 里 MA / 量比都是
        # 按位置取窗口的）。以前只过滤了 close 的 None，amount 没过 —— 一旦某天
        # `close` 为空而 `amount` 有值，两个列表就会错位、均线量比静默算错。
        # 用同一个掩码一起过（2026-10-12 修）。
        usable = [item for item in history if item.close is not None]
        tech = index_tech(
            [item.close for item in usable],
            [item.amount for item in usable],
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

    # 恐贪指数的当日简报。**失败只吞掉**：它是增强读数，算不出来不该打挂整个首页
    # （首页其它区块的取数逻辑与它完全无关）
    fear_greed: FearGreedBrief | None = None
    try:
        head = latest(_fear_greed_series(session, until=trade_date))
        if head is not None:
            fear_greed = FearGreedBrief(**head, bands=_bands())
    except Exception:  # noqa: BLE001 - 增强读数，失败不该影响首页
        logger.exception("恐贪指数简报计算失败")

    return MarketOverview(
        trade_date=trade_date,
        indexes=quotes,
        sentiment=SentimentOut.model_validate(sentiment) if sentiment else None,
        divergence=DivergenceOut(**judge) if judge else None,
        turnover=turnover,
        fear_greed=fear_greed,
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


@router.get("/fear-greed", response_model=FearGreedOut)
def fear_greed(
    days: int = Query(
        FEAR_GREED_DAYS, ge=20, le=500, description="返回最近 N 个交易日，按日期升序"
    ),
    session: Session = Depends(get_db),
) -> FearGreedOut:
    """A 股恐惧贪婪指数（0-100，越高越贪婪）的曲线 + 最新一天的分项明细。

    分数**不落库**、读取时现算（见 `services/fear_greed.py`）：窗口与分档口径改了
    只要重算，不必重新回补数据，也就不存在「原始值与分数两套口径对不上」的风险。

    `days` 下限给到 20 而不是 1：这个指数是「过去一年分位」的合成值，只看几天
    的曲线读不出任何形态，徒增误读。
    """
    series = _fear_greed_series(session)
    if not series:
        raise HTTPException(status_code=404, detail="暂无恐贪指数数据，请先执行采集或回补")

    dates, scores = slice_window(series, days)
    head = latest(series)
    return FearGreedOut(
        dates=dates,
        scores=scores,
        bands=_bands(),
        window=WINDOW,
        coverage=len(series),
        latest=FearGreedLatest(**head, subs=series[-1]["subs"]) if head else None,
    )


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
