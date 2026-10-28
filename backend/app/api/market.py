"""大盘与情绪接口。"""

import logging
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import resolve_trade_date
from app.db import get_db
from app.jobs.collect_qvix import CONTRACTS as VOLATILITY_CONTRACTS
from app.models import (
    BreadthDaily,
    ErpDaily,
    EtfShare,
    FearGreedDaily,
    FuturesBasisDaily,
    HsgtDaily,
    IndexDaily,
    LimitPool,
    MarginDaily,
    MarketSentiment,
    StockDaily,
    StockUniverse,
    VolatilityDaily,
)
from app.schemas import (
    AdvanceDeclineBucket,
    AdvanceDeclineOut,
    BreadthLatest,
    BreadthOut,
    DivergenceOut,
    ErpLatest,
    ErpOut,
    EtfFlowLatest,
    EtfFlowOut,
    FearGreedBand,
    FearGreedBenchmark,
    FearGreedBrief,
    FearGreedLatest,
    FearGreedOut,
    FuturesBasisLatest,
    FuturesBasisOut,
    FuturesBasisSeries,
    IndexHistory,
    IndexQuote,
    IndexSeries,
    IndustryBreadthItem,
    IndustryBreadthOut,
    MarginTempLatest,
    MarginTempOut,
    MarketOverview,
    NhNlLatest,
    NhNlOut,
    NorthboundLatest,
    NorthboundOut,
    ObvLatest,
    ObvOut,
    RelayTempBand,
    RelayTempLatest,
    RelayTempOut,
    SentimentOut,
    TurnoverSeries,
    VolatilityLatest,
    VolatilityOut,
    VolatilitySeries,
)
from app.services import (
    coverage,
    etf_flow,
    industry_breadth,
    margin_temp,
    nhnl,
    northbound,
    obv,
    relay_temp,
)
from app.services.fear_greed import (
    WINDOW,
    bands,
    build_series,
    latest,
    percentile_score,
    slice_window,
)
from app.services.index_tech import (
    BENCHMARK_CODE,
    LOOKBACK_BARS,
    divergence,
    index_tech,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/market", tags=["复盘"])

# 图表展示顺序：大盘指数在前，小盘/北证在后。
# 末位 `880003`（平均股价 · 等权）是**自算**序列（见 `jobs/collect_avg_price.py`），
# 不是 iFinD 采的 —— 它名字叫「平均股价」、单位是**元**而非点位，所以放最后。
INDEX_ORDER = [
    "000001.SH",
    "000300.SH",
    "000016.SH",
    "399001.SZ",
    "399006.SZ",
    "000688.SH",
    "000905.SH",
    "000852.SH",
    "932000.CSI",
    "000985.CSI",
    "899050.BJ",
    "880003",
]

# 成交额柱状图的回看长度（交易日）。30 天 ≈ 一个半月，够看出量能在放大还是萎缩；
# 再长柱子就细到分不出相邻两根了
TURNOVER_BARS = 30

# 恐贪曲线的默认回看长度（交易日）。120 ≈ 半年，够看出情绪从冷到热的整段摆动
FEAR_GREED_DAYS = 120

# 市场宽度曲线的默认回看长度。250 ≈ 一年，年线上方占比这种慢变量要一年才看得出周期
BREADTH_DAYS = 250

# 接力温度曲线的默认回看长度（交易日）。与恐贪同长：打板生态的冷热摆动也是半年尺度
RELAY_TEMP_DAYS = 120

# 期指基差曲线的默认回看长度。250 ≈ 一年，贴水的季节性（分红季、对冲需求）要一年才完整
FUTURES_BASIS_DAYS = 250

# ERP 曲线的默认回看长度。250 ≈ 一年：股债性价比是慢变量，看的就是年度周期
ERP_DAYS = 250

# NH-NL 曲线的默认回看长度。250 ≈ 一年：顶底背离要一段完整的上升/下降趋势才看得出
NHNL_DAYS = 250

# 量能潮曲线的默认回看长度。250 与成交额分位窗口同长：量能的潮起潮落是年度尺度
OBV_DAYS = 250

# ETF 净申赎曲线的默认回看长度。250 ≈ 一年：份额的申赎潮与指数的年度周期对齐，
# 看的是「资金一年里是净进还是净出」以及它与指数的背离
ETF_FLOW_DAYS = 250

# 两融杠杆温度曲线的默认回看长度。250 ≈ 一年，与站内 margin_daily 的深度（约 263
# 个交易日）相称；温度本身的分位窗口只有 120 日（见 services/margin_temp）
MARGIN_TEMP_DAYS = 250

# 行业宽度：站上 MA20 的口径（与 breadth_daily.above_ma20 同一套），以及为算这个
# 均线要往前取的交易日数。回看 40 天是给停牌 / 次新留余量 —— 不满 20 根 K 线的票
# 不进分母（见 services/industry_breadth）
INDUSTRY_MA_WINDOW = 20
INDUSTRY_MA_LOOKBACK = 40

# 行业宽度的结果缓存：`(目标交易日, 该日有效行数, 计算时间, 结果)`。
# 它**读取时现算**（40 天 × 约 5500 只的 20 日窗口函数，实测 577ms），而某一天的
# 结果在「那天采全」之后就不再变 —— 情绪周期页每次打开都会请求它，所以按
# （目标交易日, 该日行数）缓存 10 分钟（2026-10-22 体检）。
#
# ⚠️ 缓存键必须带上**该日的行数**：同一天里还会陆续补进行（收盘采集的后几批、
# `api/admin/kline/recent`、个股页的 sync），日期不变、行数会变。只看日期的话，
# 「半天」算出来的旧结果会被沿用到采完之后（与 `services.stock_phase._phase_version`
# 同一个版本口径，2026-10-22 修）。
_INDUSTRY_CACHE: tuple[date, int, datetime, IndustryBreadthOut] | None = None
_INDUSTRY_TTL = timedelta(minutes=10)

#: 找「最近一个日线采全的交易日」时最多往前翻几个交易日。
#: 收盘采集按批提交，紧挨着的「今天」可能只有几百只票；再往前一天通常就是全的。
_STOCK_DAY_LOOKBACK = 5


def _valid_stock_rows(session: Session, day: date) -> int:
    """某天有多少只票有**有效**日线（`pct_chg` 非空；停牌残行不算采到）。"""
    return int(
        session.scalar(
            select(func.count())
            .select_from(StockDaily)
            .where(
                StockDaily.trade_date == day,
                StockDaily.pct_chg.is_not(None),
            )
        )
        or 0
    )


def _latest_covered_stock_day(session: Session) -> tuple[date, int] | None:
    """最近一个「日线采全」的交易日 + 那天有效行数；库空时 None。

    为什么不用 `max(trade_date)`：只要**任意一只**票当天有行（个股页的 sync、回补的
    分块写入、收盘采集的前几批），max 就会切到当天，而全市场行业占比要的是「当天
    绝大多数股票都采到了」—— 拿「半天」算出来的占比没有意义。判据复用
    `services.coverage.coverage_ok`（与形态扫描 / 简报 AI 同一道 90% 闸），池子取
    `stock_universe` 的只数（它就是「每天要采哪些票」的范围）。
    一个达标日都找不到（新库 / 池子空）时退回最新一天并照实返回行数，让响应里的
    `coverage` 字段自己说明样本有多小。
    """
    days = list(
        session.scalars(
            select(StockDaily.trade_date)
            .distinct()
            .order_by(StockDaily.trade_date.desc())
            .limit(_STOCK_DAY_LOOKBACK)
        )
    )
    if not days:
        return None
    pool = session.scalar(select(func.count()).select_from(StockUniverse)) or 0
    # 逐天算、命中即返回：最常见的「最新一天就采全」只花一次 count（约几毫秒），
    # 不必再为更早的日子白算 4 次。
    latest_rows: int | None = None
    for day in days:
        rows_on_day = _valid_stock_rows(session, day)
        if latest_rows is None:
            latest_rows = rows_on_day
        if coverage.coverage_ok(rows_on_day, int(pool)):
            return day, rows_on_day
    return days[0], int(latest_rows or 0)


# 北向活跃度曲线的默认回看长度。250 ≈ 一年，与 hsgt_daily 的深度（约 257 个交易日）相称
NORTHBOUND_DAYS = 250

# 中国波指（QVIX）曲线的默认回看长度。250 ≈ 一年：波动率有年度的冷热周期，
# 且分位窗口也是 250 日（`services.fear_greed.WINDOW`）
VOLATILITY_DAYS = 250


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


def _benchmark_series(session: Session, dates: list[date]) -> FearGreedBenchmark | None:
    """基准指数（上证指数）在 `dates` 上的收盘点位，与 `dates` 严格等长。

    与 `/index-history` 的填充方式相同：先按日期位置建桶，再逐行填进去 —— 缺日留
    None（指数采集漏了某天时图上断开，而不是把前一天的收盘价挪过来凑数）。

    `BENCHMARK_CODE` 复用 `services/index_tech` 那一个（与恐贪自身的计算基准同源），
    不在本地另写死一个代码：两处不一致的话，图上叠的线就跟恐贪的基准不是一回事了。
    """
    if not dates:
        return None
    rows = session.execute(
        select(IndexDaily.trade_date, IndexDaily.close, IndexDaily.name)
        .where(
            IndexDaily.code == BENCHMARK_CODE,
            IndexDaily.trade_date >= dates[0],
            IndexDaily.trade_date <= dates[-1],
        )
    ).all()
    if not rows:
        return None
    close_by_date = {row.trade_date: row.close for row in rows}
    return FearGreedBenchmark(
        code=BENCHMARK_CODE,
        name=rows[0].name,
        close=[close_by_date.get(day) for day in dates],
    )


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

    响应里还带一条 `benchmark`（上证指数收盘点位，与 `dates` 等长），给折线图叠一条
    对照线用：情绪读数与点位同屏才看得出「冷的时候点位在哪」。
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
        benchmark=_benchmark_series(session, dates),
    )


@router.get("/breadth", response_model=BreadthOut)
def breadth(
    days: int = Query(
        BREADTH_DAYS, ge=20, le=500, description="返回最近 N 个交易日，按日期升序"
    ),
    session: Session = Depends(get_db),
) -> BreadthOut:
    """市场宽度温度计：站上 MA20/60/250 的个股占比 + 创 60/250 日新高新低家数。

    原始值由 `jobs/collect_breadth.py` 落库，这里只读不加工。
    `above_ma250` 与 250 日新高新低在站内 K 线历史还不够一年时是 null（不满 N 根
    K 线的票不进分母），随日线回补自然长出来 —— 前端断线展示，不补 0。
    """
    rows = list(session.scalars(select(BreadthDaily).order_by(BreadthDaily.trade_date)))
    if not rows:
        raise HTTPException(status_code=404, detail="暂无市场宽度数据，请先执行采集或回补")

    tail = rows[-days:] if days < len(rows) else rows
    last = rows[-1]
    return BreadthOut(
        dates=[row.trade_date for row in tail],
        above_ma20=[row.above_ma20 for row in tail],
        above_ma60=[row.above_ma60 for row in tail],
        above_ma250=[row.above_ma250 for row in tail],
        high60=[row.high60 for row in tail],
        low60=[row.low60 for row in tail],
        high250=[row.high250 for row in tail],
        low250=[row.low250 for row in tail],
        coverage=len(rows),
        latest=BreadthLatest(
            trade_date=last.trade_date,
            above_ma20=last.above_ma20,
            above_ma60=last.above_ma60,
            above_ma250=last.above_ma250,
            high60=last.high60,
            low60=last.low60,
            high250=last.high250,
            low250=last.low250,
            universe_count=last.universe_count,
        ),
    )


@router.get("/relay-temp", response_model=RelayTempOut)
def relay_temperature(
    days: int = Query(
        RELAY_TEMP_DAYS, ge=20, le=500, description="返回最近 N 个交易日，按日期升序"
    ),
    session: Session = Depends(get_db),
) -> RelayTempOut:
    """短线接力温度（0-100，越高越热）：六项打板生态指标的固定阈值合成。

    分数**不落库**、读取时现算（见 `services/relay_temp.py`）：六项原始值来自
    `market_sentiment`（封板率 / 连板高度 / 昨涨停今均涨幅 / 涨跌停家数）与
    `limit_pool`（首板晋级率），阈值改了不用回补数据。

    多读 1 天情绪行：晋级率是「昨日首板 → 今日涨停」，窗口里最早那天也需要它的
    前一交易日才能算出来。
    """
    rows = list(
        session.scalars(
            select(MarketSentiment)
            .order_by(MarketSentiment.trade_date.desc())
            .limit(days + 1)
        )
    )
    if not rows:
        raise HTTPException(status_code=404, detail="暂无情绪数据，请先执行采集或回补")
    rows.reverse()

    pool_rows = session.execute(
        select(LimitPool.trade_date, LimitPool.code, LimitPool.consecutive).where(
            LimitPool.pool_type == "up",
            LimitPool.trade_date >= rows[0].trade_date,
        )
    ).all()
    up_pool: dict[date, set[str]] = {}
    first_board: dict[date, set[str]] = {}
    for day, code, consecutive in pool_rows:
        up_pool.setdefault(day, set()).add(code)
        if consecutive == 1:
            first_board.setdefault(day, set()).add(code)

    series = relay_temp.build_series(rows, up_pool, first_board)
    dates, scores = relay_temp.slice_window(series, days)
    head = relay_temp.latest(series)
    return RelayTempOut(
        dates=dates,
        scores=scores,
        bands=[RelayTempBand(**band) for band in relay_temp.bands()],
        coverage=int(
            session.scalar(select(func.count()).select_from(MarketSentiment)) or 0
        ),
        latest=RelayTempLatest(**head, subs=series[-1]["subs"]) if head else None,
    )


@router.get("/futures-basis", response_model=FuturesBasisOut)
def futures_basis(
    days: int = Query(
        FUTURES_BASIS_DAYS, ge=20, le=500, description="返回最近 N 个交易日，按日期升序"
    ),
    session: Session = Depends(get_db),
) -> FuturesBasisOut:
    """期指年化基差：IF / IH / IC / IM 主力连续相对现货指数的年化基差（%）。

    原始值由 `jobs/collect_futures_basis.py` 落库，这里只读不加工。
    **负值 = 贴水**（对冲成本/压力）；年化按「距主力交割日约 7 天就换月」的口径算，
    跟上 akshare 主力按成交量提前换月的节奏（原委见 `ROLLOVER_MIN_DAYS`），
    原始基差不受换月口径影响（口径见面板说明）。期现有一侧缺价的交易日整天没采到，
    序列留 null，图上断线、不补 0。
    """
    rows = list(
        session.scalars(
            select(FuturesBasisDaily).order_by(
                FuturesBasisDaily.trade_date, FuturesBasisDaily.code
            )
        )
    )
    if not rows:
        raise HTTPException(status_code=404, detail="暂无期指基差数据，请先执行采集或回补")

    all_dates = sorted({row.trade_date for row in rows})
    tail_dates = all_dates[-days:] if days < len(all_dates) else all_dates
    wanted = set(tail_dates)

    # 按品种分组对齐到 dates：某品种某天缺数留 null（与其它品种断线互不牵连）
    by_code: dict[str, dict[date, FuturesBasisDaily]] = {}
    for row in rows:
        if row.trade_date in wanted:
            by_code.setdefault(row.code, {})[row.trade_date] = row

    series = [
        FuturesBasisSeries(
            code=code,
            name=next(iter(day_map.values())).name,
            annual_pct=[day_map[day].annual_pct if day in day_map else None for day in tail_dates],
            basis_pct=[day_map[day].basis_pct if day in day_map else None for day in tail_dates],
        )
        for code, day_map in sorted(by_code.items())
    ]

    last_day = all_dates[-1]
    latest = [
        FuturesBasisLatest(
            trade_date=row.trade_date,
            code=row.code,
            name=row.name,
            basis_pct=row.basis_pct,
            annual_pct=row.annual_pct,
            days_to_expiry=row.days_to_expiry,
        )
        for row in rows
        if row.trade_date == last_day
    ]
    return FuturesBasisOut(
        dates=tail_dates,
        series=series,
        coverage=len(all_dates),
        latest=latest,
    )


@router.get("/erp", response_model=ErpOut)
def erp(
    days: int = Query(
        ERP_DAYS, ge=20, le=500, description="返回最近 N 个交易日，按日期升序"
    ),
    session: Session = Depends(get_db),
) -> ErpOut:
    """ERP 股债性价比：沪深300 盈利收益率（100/PE-TTM）− 10Y 国债收益率（百分点）。

    原始值由 `jobs/collect_erp.py` 落库；**历史分位读取时现算**（滚动 250 个
    交易日中位秩，与恐贪同一套 `percentile_score`），改窗口不用回补数据。
    ERP 越高 = 股票相对债券越便宜。
    """
    rows = list(session.scalars(select(ErpDaily).order_by(ErpDaily.trade_date)))
    if not rows:
        raise HTTPException(status_code=404, detail="暂无 ERP 数据，请先执行采集或回补")

    # 分位必须读全表算（与恐贪同理），展示窗口只切最后 days 天
    erp_values = [row.erp for row in rows]
    percentiles: list[float | None] = []
    for i, value in enumerate(erp_values):
        if value is None:
            percentiles.append(None)
            continue
        window = [v for v in erp_values[max(0, i - WINDOW + 1) : i + 1] if v is not None]
        percentiles.append(percentile_score(window, value))

    tail = rows[-days:] if days < len(rows) else rows
    offset = len(rows) - len(tail)
    tail_pct = percentiles[offset:]
    last = rows[-1]
    return ErpOut(
        dates=[row.trade_date for row in tail],
        erp=[row.erp for row in tail],
        percentile=tail_pct,
        pe_ttm=[row.pe_ttm for row in tail],
        bond_yield=[row.bond_yield for row in tail],
        window=WINDOW,
        coverage=len(rows),
        latest=ErpLatest(
            trade_date=last.trade_date,
            erp=last.erp,
            percentile=percentiles[-1],
            pe_ttm=last.pe_ttm,
            bond_yield=last.bond_yield,
        ),
    )


@router.get("/nh-nl", response_model=NhNlOut)
def nh_nl(
    days: int = Query(
        NHNL_DAYS, ge=20, le=500, description="返回最近 N 个交易日，按日期升序"
    ),
    session: Session = Depends(get_db),
) -> NhNlOut:
    """NH-NL 新高新低扩散：创 60/250 日新高家数 − 新低家数，及差值的累计净值。

    原始值直接读 `breadth_daily`（宽度温度计采集 job 落库的那张表），diff 与
    累计净值**读取时现算**（见 `services/nhnl.py`），不建新表。250 日口径在站内
    K 线历史还不够 250 个交易日时整列为 null（与宽度温度计同口径），图上断线、
    不补 0。

    累计净值（类 AD 线）是看顶底背离的那条：指数收盘创新高而净值线不创新高 =
    顶背离。响应里带 `benchmark`（上证指数收盘点位，与 `dates` 等长）叠图对照。
    """
    rows = list(session.scalars(select(BreadthDaily).order_by(BreadthDaily.trade_date)))
    if not rows:
        raise HTTPException(status_code=404, detail="暂无市场宽度数据，请先执行采集或回补")

    diff60 = [nhnl.diff(row.high60, row.low60) for row in rows]
    diff250 = [nhnl.diff(row.high250, row.low250) for row in rows]
    cum60 = nhnl.cumulative(diff60)
    cum250 = nhnl.cumulative(diff250)

    tail = rows[-days:] if days < len(rows) else rows
    offset = len(rows) - len(tail)
    dates = [row.trade_date for row in tail]
    last = rows[-1]
    return NhNlOut(
        dates=dates,
        diff60=diff60[offset:],
        diff250=diff250[offset:],
        cum60=cum60[offset:],
        cum250=cum250[offset:],
        coverage=len(rows),
        latest=NhNlLatest(
            trade_date=last.trade_date,
            high60=last.high60,
            low60=last.low60,
            diff60=diff60[-1],
            high250=last.high250,
            low250=last.low250,
            diff250=diff250[-1],
        ),
        benchmark=_benchmark_series(session, dates),
    )


@router.get("/obv", response_model=ObvOut)
def volume_obv(
    days: int = Query(
        OBV_DAYS, ge=20, le=500, description="返回最近 N 个交易日，按日期升序"
    ),
    session: Session = Depends(get_db),
) -> ObvOut:
    """量能潮 OBV：上涨日累加成交额、下跌日扣减的累计净额（上证指数口径）。

    原始值读 `index_daily` 的基准指数（与恐贪基准同源），OBV 与成交额分位
    **读取时现算**（见 `services/obv.py`），不建新表。分位是成交额在滚动 250
    个交易日里的中位秩（与恐贪 / ERP 同一套 `percentile_score`），**必须读全表**
    算分位再切展示窗口 —— 截断历史会把窗口喂瘦、算出来的分位偏高。

    用法：OBV 与指数点位的背离 —— 指数新高而 OBV 不新高 = 量能跟不上。
    """
    rows = list(
        session.scalars(
            select(IndexDaily)
            .where(IndexDaily.code == BENCHMARK_CODE)
            .order_by(IndexDaily.trade_date)
        )
    )
    if not rows:
        raise HTTPException(status_code=404, detail="暂无指数数据，请先执行采集或回补")

    pcts = [row.pct_chg for row in rows]
    amounts = [row.amount for row in rows]
    obv_series = obv.build(pcts, amounts)
    changes = obv.daily_change(pcts, amounts)

    # 成交额分位：滚动 250 日窗口，与 ERP 端点同一套算法（窗口内过滤 None）
    percentiles: list[float | None] = []
    for i, value in enumerate(amounts):
        if value is None:
            percentiles.append(None)
            continue
        window = [v for v in amounts[max(0, i - WINDOW + 1) : i + 1] if v is not None]
        percentiles.append(percentile_score(window, value))

    tail = rows[-days:] if days < len(rows) else rows
    offset = len(rows) - len(tail)
    last_idx = len(rows) - 1
    last = rows[-1]
    return ObvOut(
        dates=[row.trade_date for row in tail],
        obv=obv_series[offset:],
        obv_change=changes[offset:],
        amount=amounts[offset:],
        percentile=percentiles[offset:],
        window=WINDOW,
        coverage=len(rows),
        latest=ObvLatest(
            trade_date=last.trade_date,
            amount=last.amount,
            obv_change=changes[last_idx],
            obv=obv_series[last_idx],
            percentile=percentiles[last_idx],
        ),
    )


@router.get("/etf-net-subscription", response_model=EtfFlowOut)
def etf_net_subscription(
    days: int = Query(
        ETF_FLOW_DAYS, ge=20, le=500, description="返回最近 N 个交易日，按日期升序"
    ),
    session: Session = Depends(get_db),
) -> EtfFlowOut:
    """ETF 净申赎：Σ(份额变化 × 当日收盘价)，及窗口内的累计净额。

    原始值读 `etf_share` 的逐日份额与收盘价，净申赎**读取时现算**
    （见 `services/etf_flow.py`），不建新表。

    **只在两个「覆盖达标」的交易日之间算**，这是这张表面向申赎口径的关键：
    快照的日期来自行情源的「最新-交易日」，而它**不是全市场统一的一天** ——
    非交易日货基仍按自然日给净值快照（09-27/10-07/10-11 各 14 行），跨境/QDII
    则常年比境内 ETF 晚一个交易日（10-12 的 45 行与 10-13 的 1657 行零交集）。
    所以基准日取「上一个达标日」而不是紧邻的交易日，跨度如实由 `span` 标出。

    货基（`货币现金` 类）整类剔除：份额单位与覆盖都不可信，计进去是噪声。

    用法：累计净额与指数背离 —— 指数新高而累计净额不新高 = 增量资金跟不上了。
    """
    end = session.scalar(select(func.max(EtfShare.trade_date)))
    if end is None:
        raise HTTPException(status_code=404, detail="暂无 ETF 份额数据，请先执行采集或回补")

    series = etf_flow.build_series(session, end, days)
    if not series.dates:
        raise HTTPException(status_code=404, detail="暂无 ETF 份额数据，请先执行采集或回补")

    latest = None
    if series.latest_index is not None:
        index = series.latest_index
        latest = EtfFlowLatest(
            trade_date=series.dates[index],
            net=series.net[index],
            span=series.span[index],
            cumulative=series.cumulative[index],
            sum5=series.sums.get(5),
            sum20=series.sums.get(20),
        )
    return EtfFlowOut(
        dates=series.dates,
        net=series.net,
        span=series.span,
        cumulative=series.cumulative,
        min_rows=etf_flow.MIN_ROWS,
        coverage=series.coverage,
        latest=latest,
        benchmark=_benchmark_series(session, series.dates),
    )


@router.get("/margin-temp", response_model=MarginTempOut)
def margin_leverage_temp(
    days: int = Query(
        MARGIN_TEMP_DAYS, ge=20, le=500, description="返回最近 N 个交易日，按日期升序"
    ),
    session: Session = Depends(get_db),
) -> MarginTempOut:
    """两融杠杆温度：沪深合计两融余额、20 日变化率与分位温度。

    原始值读 `margin_daily`，余额 / 变化率 / 温度**读取时现算**
    （见 `services/margin_temp.py`），不建新表。

    **只在两个交易所都披露的日子算合计**：两所披露时点不同（实测深交所晚一天），
    只有一所时相加会把「深市待披露」误报成「深市归零」—— 那个数字会小一大截、
    看起来像两融骤降。回采会把这天补齐（`EDB_LOOKBACK_DAYS` 回看 30 天），所以这种
    日子只可能出现在最新一两天，跳过去只是让曲线晚一天、不留永久缺口。

    温度的分位**必须读全表**算再切展示窗口（与 OBV / ERP 同一套 `percentile_score`）——
    截断历史会把窗口喂瘦、算出来的温度偏高。
    """
    rows = session.execute(
        select(
            MarginDaily.trade_date,
            func.sum(
                func.coalesce(MarginDaily.financing_balance, 0.0)
                + func.coalesce(MarginDaily.securities_balance, 0.0)
            ).label("total"),
            func.count(MarginDaily.market).label("markets"),
        )
        .group_by(MarginDaily.trade_date)
        .order_by(MarginDaily.trade_date)
    ).all()
    if not rows:
        raise HTTPException(status_code=404, detail="暂无两融数据，请先执行采集或回补")

    dates: list[date] = []
    balances: list[float | None] = []
    for day, total, markets in rows:
        # 两所齐全、且合计为正才算数（合计为 0 是「两所都没数」，不是「真归零」）
        ok = markets == 2 and total is not None and total > 0
        dates.append(day)
        balances.append(round(float(total), 1) if ok else None)

    chg20 = margin_temp.change_rate(balances)

    # 温度：chg20 在滚动 WINDOW 个观测窗口里的中位秩（窗口内过滤 None，与 OBV 同套路）
    temperatures: list[float | None] = []
    for i, value in enumerate(chg20):
        if value is None:
            temperatures.append(None)
            continue
        window = [v for v in chg20[max(0, i - margin_temp.WINDOW + 1) : i + 1] if v is not None]
        temperatures.append(percentile_score(window, value))

    tail = min(days, len(dates))
    offset = len(dates) - tail
    last = len(dates) - 1
    balance = balances[last]
    prev_balance = balances[last - 1] if last >= 1 else None
    balance_chg = (
        round(balance - prev_balance, 1)
        if balance is not None and prev_balance is not None
        else None
    )
    return MarginTempOut(
        dates=dates[offset:],
        balance=balances[offset:],
        chg20=chg20[offset:],
        temperature=temperatures[offset:],
        window=margin_temp.WINDOW,
        chg_days=margin_temp.CHG_DAYS,
        coverage=sum(1 for value in balances if value is not None),
        latest=MarginTempLatest(
            trade_date=dates[last],
            balance=balance,
            balance_chg=balance_chg,
            chg20=chg20[last],
            temperature=temperatures[last],
        ),
    )


@router.get("/industry-breadth", response_model=IndustryBreadthOut)
def industry_breadth_api(session: Session = Depends(get_db)) -> IndustryBreadthOut:
    """行业宽度（**当日横截面**）：各一级行业里「站上 MA20」的个股占比。

    原始值读 `stock_daily` × `stock_basic.industry`，占比**读取时现算**
    （见 `services/industry_breadth.py`），不建新表。行业口径取同花顺行业路径的
    第一段（31 个一级行业）；「站上 MA20」与 `breadth_daily.above_ma20` 同一套约定
    （不满 20 根 K 线的票不进分母）。

    ⚠️ 这是**横截面**读数（当日谁强谁弱），不是时间序列 —— 逐日算 250 天的行业占比
    要好几秒（按票的 20 日窗口函数跑几十万行），而情绪周期页是 `Promise.all` 拉齐
    后才渲染、会整页卡住。所以只取当日：窗口取目标日往前 40 个交易日、只聚合一天，
    实测约 0.4 秒。

    ⚠️ 目标日取「最近一个**日线采全**的交易日」，不是 `max(trade_date)`（2026-10-22 修，
    见 `_latest_covered_stock_day`）：收盘采集是按批写库的，采集中「今天」可能只有
    几百只票，拿它算全市场占比会得出一个没有意义的数。
    """
    found = _latest_covered_stock_day(session)
    if found is None:
        raise HTTPException(status_code=404, detail="暂无个股日线，请先执行采集或回补")
    target, rows_on_target = found

    global _INDUSTRY_CACHE
    now = datetime.now()
    cached = _INDUSTRY_CACHE
    if (
        cached is not None
        and cached[0] == target
        and cached[1] == rows_on_target
        and now - cached[2] < _INDUSTRY_TTL
    ):
        return cached[3]

    # 目标日往前取 LOOKBACK 个交易日当 MA 窗口（只需当日的 20 日均线，多取的用于预热）
    bars = list(
        session.scalars(
            select(StockDaily.trade_date)
            .where(StockDaily.trade_date <= target)
            .distinct()
            .order_by(StockDaily.trade_date.desc())
            .limit(INDUSTRY_MA_LOOKBACK)
        )
    )
    start = min(bars)

    # 裸 SQL：按票的 20 日窗口函数用 SQLAlchemy 表达式写会比这长得多。日期在 SQLite
    # 里就是 'YYYY-MM-DD' 文本，与 isoformat() 直接可比。
    rows = session.execute(
        text(
            """
            WITH w AS (
              SELECT d.trade_date, d.code, d.close,
                     AVG(d.close) OVER (PARTITION BY d.code ORDER BY d.trade_date
                                        ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20,
                     COUNT(d.close) OVER (PARTITION BY d.code ORDER BY d.trade_date
                                          ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS n
              FROM stock_daily d
              WHERE d.trade_date >= :start AND d.trade_date <= :target
            )
            SELECT b.industry AS industry,
                   COUNT(*) AS total,
                   SUM(CASE WHEN w.close >= w.ma20 THEN 1 ELSE 0 END) AS above
            FROM w JOIN stock_basic b ON b.code = w.code
            WHERE w.trade_date = :target AND w.close IS NOT NULL AND w.n >= :window
            GROUP BY b.industry
            """
        ),
        {
            "start": start.isoformat(),
            "target": target.isoformat(),
            "window": INDUSTRY_MA_WINDOW,
        },
    ).all()

    items = industry_breadth.aggregate(rows)
    total = sum(item["total"] for item in items)
    above = sum(item["above"] for item in items)
    result = IndustryBreadthOut(
        trade_date=target,
        ma_window=INDUSTRY_MA_WINDOW,
        items=[IndustryBreadthItem(**item) for item in items],
        overall=round(above / total * 100, 1) if total else None,
        median=industry_breadth.median_ratio(items),
        coverage=total,
    )
    _INDUSTRY_CACHE = (target, rows_on_target, now, result)
    return result


@router.get("/northbound-activity", response_model=NorthboundOut)
def northbound_activity(
    days: int = Query(
        NORTHBOUND_DAYS, ge=20, le=500, description="返回最近 N 个交易日，按日期升序"
    ),
    session: Session = Depends(get_db),
) -> NorthboundOut:
    """北向活跃度（外资参与度）：沪深股通成交额 ÷ 两市成交额（%）。

    ⚠️ **只有成交额，没有净流入**：2024-08-19 起交易所不再按日披露买入 / 卖出金额，
    「北向净流入」在数据源层面已经不存在（见 `models.HsgtDaily`）。所以这是
    **活跃度 / 参与度，不含方向**。

    原始值读 `hsgt_daily`（成交额，百万元）与 `market_sentiment.total_amount`
    （元），占比**读取时现算**（见 `services/northbound.py`），不建新表。

    日期骨架取 `market_sentiment`（与恐贪 / 宽度同一份）：**通道关闭日没有北向数据，
    那个点记 null、图上断开** —— 断口是「没数据」，不是「北向没交易」。
    """
    rows = list(
        session.execute(
            select(MarketSentiment.trade_date, MarketSentiment.total_amount).order_by(
                MarketSentiment.trade_date
            )
        ).all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="暂无两市成交额，请先执行采集或回补")

    # 沪深股通成交额按日汇总（原始单位百万元，展示换算成亿元）
    hsgt = {
        day: float(total)
        for day, total in session.execute(
            select(HsgtDaily.trade_date, func.sum(HsgtDaily.turnover)).group_by(
                HsgtDaily.trade_date
            )
        ).all()
        if total is not None
    }

    dates = [day for day, _ in rows]
    amounts = [amount for _, amount in rows]
    turnovers = [hsgt.get(day) for day in dates]
    ratios = northbound.ratio(turnovers, amounts)
    turnover_yi = [
        None if t is None else round(t * northbound.MILLION_TO_YI, 1) for t in turnovers
    ]

    tail = min(days, len(dates))
    offset = len(dates) - tail

    # 最新读数取**最后一个有北向数据的日子**：通道关闭或采集滞后时，末尾可能没有值，
    # 直接取最后一天会让顶部读数变成一整排「—」
    last = next((i for i in range(len(ratios) - 1, -1, -1) if ratios[i] is not None), None)
    latest = (
        None
        if last is None
        else NorthboundLatest(
            trade_date=dates[last],
            turnover=turnover_yi[last],
            ratio=ratios[last],
            ratio_ma5=northbound.mean_tail(ratios, last, 5),
            ratio_ma20=northbound.mean_tail(ratios, last, 20),
        )
    )
    return NorthboundOut(
        dates=dates[offset:],
        ratio=ratios[offset:],
        turnover=turnover_yi[offset:],
        coverage=sum(1 for t in turnovers if t is not None),
        latest=latest,
        benchmark=_benchmark_series(session, dates[offset:]),
    )


@router.get("/volatility", response_model=VolatilityOut)
def volatility_index(
    days: int = Query(
        VOLATILITY_DAYS, ge=20, le=500, description="返回最近 N 个交易日，按日期升序"
    ),
    session: Session = Depends(get_db),
) -> VolatilityOut:
    """中国波指（QVIX）：50ETF / 300ETF 期权隐含波动率（%）。

    ⚠️ **不是交易所官方 iVX**（那条 2018 年停更）—— 数据由**期权论坛**按方差互换
    思路合成，站内只负责落库与读取（见 `jobs/collect_qvix.py` 与 `models.VolatilityDaily`）。
    加权方案不在我们手里，这是「直取现成指数，换 10 年历史 + 零计算」的取舍。

    单位是百分数（17.95 = 17.95%），**与指数反向**：指数跌、波动率升。分位与
    OBV / ERP 同一套 `percentile_score`，**必须读全表算**再切展示窗口。
    """
    rows = list(
        session.scalars(
            select(VolatilityDaily).order_by(VolatilityDaily.trade_date, VolatilityDaily.code)
        )
    )
    if not rows:
        raise HTTPException(status_code=404, detail="暂无波动率数据，请先执行采集或回补")

    # 日期骨架用两个品种的**并集**：50ETF 从 2015 年就有、300ETF 到 2019 年底才有，
    # 用并集才能让两边都对齐到同一条 x 轴（300ETF 早年的空档留 null、图上断开）
    dates_all = sorted({row.trade_date for row in rows})
    closes: dict[str, dict[date, float]] = {}
    for row in rows:
        if row.close is not None:
            closes.setdefault(row.code, {})[row.trade_date] = row.close

    tail = min(days, len(dates_all))
    offset = len(dates_all) - tail

    series: list[VolatilitySeries] = []
    latest: list[VolatilityLatest] = []
    for code, (_, label) in VOLATILITY_CONTRACTS.items():
        values = [closes.get(code, {}).get(day) for day in dates_all]

        # 分位：读全表算（窗口内过滤 None），再跟着展示窗口切
        percentiles: list[float | None] = []
        for i, value in enumerate(values):
            if value is None:
                percentiles.append(None)
                continue
            window = [v for v in values[max(0, i - WINDOW + 1) : i + 1] if v is not None]
            percentiles.append(percentile_score(window, value))

        # 最新读数取**最后一个有值的日子**：接口偶尔当天还没更新，末尾可能是空
        last = next((i for i in range(len(values) - 1, -1, -1) if values[i] is not None), None)
        if last is None:
            continue
        prev = values[last - 1] if last >= 1 else None
        chg = (
            round((values[last] / prev - 1) * 100, 2) if prev not in (None, 0) else None
        )
        latest.append(
            VolatilityLatest(
                trade_date=dates_all[last],
                code=code,
                name=label,
                close=values[last],
                chg=chg,
                percentile=percentiles[last],
            )
        )
        series.append(VolatilitySeries(code=code, name=label, close=values[offset:]))

    return VolatilityOut(
        dates=dates_all[offset:],
        series=series,
        window=WINDOW,
        coverage=len(dates_all),
        latest=latest,
        benchmark=_benchmark_series(session, dates_all[offset:]),
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


#: 涨跌分布的分档。区间统一是「左开右闭」（-7 < x ≤ -5），首尾是开区间。
#: 11 档是刻意压到的数量：再多柱子就细到看不清，再少（例如只分 5 档）就看不出
#: 「大跌家数」和「小跌家数」的区别 —— 而这两者的含义完全不同。
AD_BUCKETS: tuple[tuple[str, float | None, float | None], ...] = (
    ("≤-7%", None, -7.0),
    ("-7~-5%", -7.0, -5.0),
    ("-5~-3%", -5.0, -3.0),
    ("-3~-1%", -3.0, -1.0),
    ("-1~0%", -1.0, 0.0),
    ("平盘", 0.0, 0.0),
    ("0~1%", 0.0, 1.0),
    ("1~3%", 1.0, 3.0),
    ("3~5%", 3.0, 5.0),
    ("5~7%", 5.0, 7.0),
    (">7%", 7.0, None),
)


@router.get("/advance-decline", response_model=AdvanceDeclineOut)
def advance_decline(
    trade_date: date | None = Query(None, description="缺省 = 库里最新的个股日线日"),
    session: Session = Depends(get_db),
) -> AdvanceDeclineOut:
    """全 A 涨跌分布：各区间家数 + 平均/中位数涨跌幅。

    只统计**当天有真实涨跌幅**的票（`pct_chg` 非空）：停牌残行是「有收盘价、
    没涨跌幅」，把它们算进「平盘」会让平盘家数凭空多出几百只。
    """
    target = trade_date or session.scalar(
        select(func.max(StockDaily.trade_date)).where(StockDaily.pct_chg.is_not(None))
    )
    if target is None:
        raise HTTPException(status_code=404, detail="库里还没有个股日线，先跑采集")

    values = [
        float(value)
        for value in session.scalars(
            select(StockDaily.pct_chg).where(
                StockDaily.trade_date == target, StockDaily.pct_chg.is_not(None)
            )
        )
    ]
    if not values:
        raise HTTPException(status_code=404, detail=f"{target} 没有个股涨跌幅")

    counts: list[int] = []
    for label, lower, upper in AD_BUCKETS:
        if label == "平盘":
            counts.append(sum(1 for value in values if value == 0))
        elif lower is None:
            counts.append(sum(1 for value in values if value <= upper))
        elif upper is None:
            counts.append(sum(1 for value in values if value > lower))
        else:
            # ⚠️ 上界为 0 的那一档（`-1~0%`）必须**排除 0**（2026-10-28 体检）：
            # 0 归「平盘」单独计一次，而原来的 `lower < value <= upper` 把 0 也算进了
            # 这一档 —— 同一个 0 被数两遍。实测 2026-10-09：各档求和 5354，而总数
            # 5249、平盘 105，差的正好是那 105 只。
            if upper == 0:
                counts.append(sum(1 for value in values if lower < value < 0))
            else:
                counts.append(sum(1 for value in values if lower < value <= upper))

    ordered = sorted(values)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2
    )
    return AdvanceDeclineOut(
        trade_date=target,
        total=len(values),
        up=sum(1 for value in values if value > 0),
        down=sum(1 for value in values if value < 0),
        flat=sum(1 for value in values if value == 0),
        avg_pct=round(sum(values) / len(values), 2),
        median_pct=round(median, 2),
        buckets=[
            AdvanceDeclineBucket(label=label, lower=lower, upper=upper, count=count)
            for (label, lower, upper), count in zip(AD_BUCKETS, counts, strict=True)
        ],
    )
