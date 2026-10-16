"""大盘与情绪接口。"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import resolve_trade_date
from app.db import get_db
from app.models import (
    BreadthDaily,
    ErpDaily,
    EtfShare,
    FearGreedDaily,
    FuturesBasisDaily,
    IndexDaily,
    LimitPool,
    MarginDaily,
    MarketSentiment,
    StockDaily,
)
from app.schemas import (
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
    ObvLatest,
    ObvOut,
    RelayTempBand,
    RelayTempLatest,
    RelayTempOut,
    SentimentOut,
    TurnoverSeries,
)
from app.services import etf_flow, industry_breadth, margin_temp, nhnl, obv, relay_temp
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
    """
    target = session.scalar(select(func.max(StockDaily.trade_date)))
    if target is None:
        raise HTTPException(status_code=404, detail="暂无个股日线，请先执行采集或回补")

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
    return IndustryBreadthOut(
        trade_date=target,
        ma_window=INDUSTRY_MA_WINDOW,
        items=[IndustryBreadthItem(**item) for item in items],
        overall=round(above / total * 100, 1) if total else None,
        median=industry_breadth.median_ratio(items),
        coverage=total,
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
