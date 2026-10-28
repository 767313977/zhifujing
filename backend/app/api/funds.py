"""资金面接口：两融、沪深股通成交额、ETF 申赎、龙虎榜机构席位。

单位约定：**所有金额一律返回「元」**。底层三张表的原始单位各不相同
（两融是亿元、沪深股通是百万元、机构与 ETF 是元），换算放在这一层做掉 ——
否则前端要为每个字段记住一个单位，迟早出错。
"""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import resolve_trade_date
from app.db import get_db
from app.jobs.collect_futures_position import VARIETIES as FUTURES_VARIETIES
from app.models import (
    EtfCategory,
    EtfShare,
    FuturesPosition,
    HsgtDaily,
    LhbInstitution,
    MarginDaily,
    StockBasic,
    StockDaily,
    StockDde,
    TradeCalendar,
)
from app.schemas import (
    DdeBoard,
    DdeItem,
    EtfFlowBoard,
    EtfFlowItem,
    EtfIndustryBoard,
    EtfIndustryItem,
    FundFlowOverview,
    FundsSeries,
    FuturesNetShort,
    FuturesSeries,
    InstitutionBoard,
    InstitutionItem,
    MarginSnapshot,
)
from app.services import etf_flow
from app.services.etf_category import UNKNOWN
from app.services.stock_meta import industries_of, sectors_of

router = APIRouter(prefix="/api/funds", tags=["资金面"])

# 亿元 → 元、百万元 → 元
YI = 1e8
MILLION = 1e6

# 走势图最多回看这么多个交易日
SERIES_MAX_DAYS = 250
SERIES_DEFAULT_DAYS = 60


def _prev_trade_date(session: Session, day: date) -> date | None:
    return session.scalar(
        select(func.max(TradeCalendar.trade_date)).where(TradeCalendar.trade_date < day)
    )


def _hsgt_total(rows: list[HsgtDaily]) -> float | None:
    """北向成交额合计（元）。

    ⚠️ **有行但 `turnover` 全是 None** 时要给 None，不能给 0（2026-09-27 修）：
    前者是「当天没披露」，后者是「成交 0 元」。原来的写法按 `if rows` 判，
    这种日子会在页面上显示成「成交 0」，与两融那边「无数据给 None」的口径也不一致。
    """
    values = [row.turnover for row in rows if row.turnover is not None]
    return sum(values) * MILLION if values else None


def _recent_trade_dates(session: Session, end: date, days: int) -> list[date]:
    """最近 `days` 个交易日，升序。"""
    rows = session.scalars(
        select(TradeCalendar.trade_date)
        .where(TradeCalendar.trade_date <= end)
        .order_by(TradeCalendar.trade_date.desc())
        .limit(days)
    )
    return sorted(rows)


def _margin_snapshot(row: MarginDaily | None) -> MarginSnapshot | None:
    if row is None:
        return None
    return MarginSnapshot(
        financing_balance=None if row.financing_balance is None else row.financing_balance * YI,
        financing_buy=None if row.financing_buy is None else row.financing_buy * YI,
        securities_balance=(
            None if row.securities_balance is None else row.securities_balance * YI
        ),
    )


def _latest_complete_margin_day(session: Session, target: date) -> date | None:
    """`target`（含）之前**沪深两市都齐全**的最近一个两融交易日。

    为什么要有它（2026-10-23 实测的漏）：两融由沪深交易所各自发布、iFinD 常要次日
    才给全，而页面顶部的日期由「最新情绪日」决定、收盘当晚就是当天 —— 于是从收盘到
    次日采集之间，`target` 当天**根本没有两融行**，资金面那两张卡片只能空着。
    ETF 与 DDE 早就做了同样的「回落到最近有数据的一天」，两融一直没做。

    判据是「两个市场都有**融资余额**」，不是「有两个 market 的行」：深市出现过只有行、
    余额为 NULL 的日子（见 `overview` 里两市合计算法的注释），那种日子合计照样给不出来。

    ⚠️ 回落之后**必须**把实际日期回给前端（`FundFlowOverview.margin_date`），否则
    面板上是昨天的数、页面顶部写着今天，比空白更容易误读。
    """
    return session.scalar(
        select(MarginDaily.trade_date)
        .where(
            MarginDaily.trade_date <= target,
            MarginDaily.financing_balance.is_not(None),
        )
        .group_by(MarginDaily.trade_date)
        .having(func.count(func.distinct(MarginDaily.market)) >= 2)
        .order_by(MarginDaily.trade_date.desc())
        .limit(1)
    )


@router.get("/overview", response_model=FundFlowOverview)
def overview(
    target: date = Depends(resolve_trade_date),
    session: Session = Depends(get_db),
) -> FundFlowOverview:
    """资金面总览：当日两融、北向成交额、ETF 净申赎、机构席位。"""
    prev = _prev_trade_date(session, target)

    # 两融回落到最近一个「沪深都齐全」的交易日，理由见 `_latest_complete_margin_day`
    margin_date = _latest_complete_margin_day(session, target)
    margin_rows = (
        list(
            session.scalars(
                select(MarginDaily).where(MarginDaily.trade_date == margin_date)
            )
        )
        if margin_date is not None
        else []
    )
    by_market = {row.market: row for row in margin_rows}
    sh, sz = by_market.get("sh"), by_market.get("sz")

    # 两市合计：**只在两个市场都有数时才给**。深市常比沪市晚一天（实测），
    # 只有一个市场时相加会把「深市待披露」误报成「深市归零」——
    # 那个数字会小一大截，看起来像两融骤降。
    # 判 `is not None` 而不是真值：余额/买入额为 0 是「有数且为零」，不是「缺失」
    both = sh is not None and sz is not None
    if both and sh.financing_balance is not None and sz.financing_balance is not None:
        financing_total = (sh.financing_balance + sz.financing_balance) * YI
    else:
        financing_total = None

    financing_buy_total = None
    if both and sh.financing_buy is not None and sz.financing_buy is not None:
        financing_buy_total = (sh.financing_buy + sz.financing_buy) * YI

    # 融资余额变化：拿**上一个齐全日**的同口径合计来比（不是 target 的前一交易日 ——
    # 回落之后基准也必须跟着回落到两融自己的节奏上），同样要求两边齐全
    margin_prev = (
        _latest_complete_margin_day(session, margin_date - timedelta(days=1))
        if margin_date is not None
        else None
    )
    financing_change = None
    if margin_prev is not None and financing_total is not None:
        prev_rows = list(
            session.scalars(
                select(MarginDaily).where(MarginDaily.trade_date == margin_prev)
            )
        )
        prev_by_market = {row.market: row for row in prev_rows}
        prev_sh, prev_sz = prev_by_market.get("sh"), prev_by_market.get("sz")
        if (
            prev_sh is not None
            and prev_sz is not None
            and prev_sh.financing_balance is not None
            and prev_sz.financing_balance is not None
        ):
            financing_change = financing_total - (
                prev_sh.financing_balance + prev_sz.financing_balance
            ) * YI

    hsgt_rows = list(
        session.scalars(select(HsgtDaily).where(HsgtDaily.trade_date == target))
    )
    hsgt_turnover = _hsgt_total(hsgt_rows)
    hsgt_prev = None
    if prev is not None:
        prev_hsgt = list(
            session.scalars(select(HsgtDaily).where(HsgtDaily.trade_date == prev))
        )
        hsgt_prev = _hsgt_total(prev_hsgt)

    institutions = list(
        session.scalars(select(LhbInstitution).where(LhbInstitution.trade_date == target))
    )
    # 同一只票可能有多条上榜原因，机构净买额按**代码去重**后相加 ——
    # 直接 sum 会把同一笔机构买入按原因条数重复计算
    seen: dict[str, float] = {}
    for row in institutions:
        if row.net_amount is not None and row.code not in seen:
            seen[row.code] = row.net_amount
    institution_net = sum(seen.values()) if seen else None

    # ETF 的基准日与曲线接口（`/api/market/etf-net-subscription`）同口径：上一个
    # **覆盖达标**的快照日，而不是紧邻交易日。碎片快照日（跨境/QDII 滞后那天只有
    # 几十行、与当天零交集）当基准会得出「没得比」，详见 `services/etf_flow.MIN_ROWS`。
    etf_inflow = _etf_net_inflow(
        session, target, etf_flow.prev_covered_day(session, target)
    )

    # 期指前 20 净空（2026-10-19 加）：中金所约 16:00 才发布，取**最近一份**，
    # 所以 `futures_date` 可能早于页面选中的交易日。
    futures_date = session.scalar(
        select(func.max(FuturesPosition.trade_date)).where(
            FuturesPosition.trade_date <= target
        )
    )
    futures_net_short: list[FuturesNetShort] = []
    if futures_date is not None:
        rows = list(
            session.scalars(
                select(FuturesPosition).where(
                    FuturesPosition.trade_date == futures_date
                )
            )
        )
        order = {
            variety: index for index, variety in enumerate(FUTURES_VARIETIES)
        }
        futures_net_short = [
            FuturesNetShort(
                variety=row.variety,
                name=FUTURES_VARIETIES[row.variety],
                net_short=row.top_net_short,
                change=row.top_change,
                citic_net_short=row.citic_net_short,
                citic_change=row.citic_change,
            )
            for row in sorted(
                rows, key=lambda item: order.get(item.variety, len(order))
            )
            if row.top_net_short is not None
            and row.top_change is not None
            and row.citic_net_short is not None
            and row.citic_change is not None
        ]

    # 大盘主力净流入 = 全市场 DDE 主力净流入额之和（iFinD 口径，元）
    main_net_inflow = session.scalar(
        select(func.sum(StockDde.net_inflow)).where(StockDde.trade_date == target)
    )
    main_net_inflow_prev = None
    if prev is not None:
        main_net_inflow_prev = session.scalar(
            select(func.sum(StockDde.net_inflow)).where(StockDde.trade_date == prev)
        )

    return FundFlowOverview(
        trade_date=target,
        margin_date=margin_date,
        sh=_margin_snapshot(sh),
        sz=_margin_snapshot(sz),
        financing_total=financing_total,
        financing_buy_total=financing_buy_total,
        financing_change=financing_change,
        hsgt_turnover=hsgt_turnover,
        hsgt_turnover_prev=hsgt_prev,
        institution_net=institution_net,
        institution_count=len({row.code for row in institutions}),
        etf_net_inflow=etf_inflow,
        futures_date=futures_date,
        futures_net_short=futures_net_short,
        main_net_inflow=main_net_inflow,
        main_net_inflow_prev=main_net_inflow_prev,
    )


# 货币现金类的分类名（词典见 services/etf_category.py 的「货币现金」条目）。
# 申赎计算要**整类剔除**这一类，理由见 `_money_fund_codes` 的 docstring。
MONEY_CATEGORY = "货币现金"


def _money_fund_codes(session: Session) -> set[str]:
    """货币现金类 ETF 的代码集合。

    **为什么申赎要整类剔除**（2026-10-11 核实，证据来自本地/线上库实测）：
    iFinD 的 fund_profile 对场内货基给的 `shares` 不可信 ——
    1. **单位不统一**：511880 银华日利 / 511990 华宝添益按「份」是对的
       （11.4 亿份 / 9.8 亿份 × ~100 元 ≈ 1151 / 976 亿元，与实际规模相符），
       但 511910 融通货币只给 96,000 份 × 100 元 = 960 万元，明显不是同一单位；
    2. **覆盖漂移**：两大货基只在部分交易日出现（09-21 有、09-22/23 缺），
       「缺席即跳过」意味着每天的申赎合计其实是一批随机子集的和；
    3. 货基申赎本身是现金管理行为，不代表方向性资金，对复盘口径只有噪声。
    剔除的只是**申赎金额**类计算；货基的 close / 涨跌幅 / 成交额没错，不受影响。
    """
    return set(
        session.scalars(
            select(EtfCategory.code).where(EtfCategory.category == MONEY_CATEGORY)
        )
    )


def _etf_net_inflow(session: Session, target: date, prev: date | None) -> float | None:
    """ETF 当日净申赎（元）= Σ(份额变化 × 当日收盘价)，**不含货币现金类**。

    货基份额的单位与覆盖都不可信，计进去是噪声（见 `_money_fund_codes`）。
    没有前一日数据（首次采集）时返回 None 而不是 0 —— 「没得比」与
    「今天一分钱没进来」是两件事，后者是个结论，前者只是缺数据。

    ⚠️ 两条判据与 `services.etf_flow.build_series` **同口径**（2026-10-22 修）：
    - `prev` 由调用方传「上一个**覆盖达标**的快照日」（`etf_flow.prev_covered_day`），
      不是紧邻交易日 —— 碎片快照日（几十行）当基准会让交集为 0；
    - 当日快照不足 `MIN_ROWS` 时直接给 None：只覆盖几十只的「净申赎合计」不是
      当日的真实数，不能拿它当「资金没动」。
    """
    if prev is None:
        return None
    money = _money_fund_codes(session)
    today = {
        row.code: row
        for row in session.scalars(select(EtfShare).where(EtfShare.trade_date == target))
    }
    if len(today) < etf_flow.MIN_ROWS:
        return None
    yesterday = {
        row.code: row
        for row in session.scalars(select(EtfShare).where(EtfShare.trade_date == prev))
    }
    total = 0.0
    matched = False
    for code, row in today.items():
        if code in money:
            continue
        before = yesterday.get(code)
        if before is None or row.shares is None or before.shares is None or not row.close:
            continue
        total += (row.shares - before.shares) * row.close
        matched = True
    return total if matched else None


@router.get("/series", response_model=FundsSeries)
def series(
    days: int = Query(SERIES_DEFAULT_DAYS, ge=5, le=SERIES_MAX_DAYS),
    end: date = Depends(resolve_trade_date),
    session: Session = Depends(get_db),
) -> FundsSeries:
    """两融余额 / 融资买入额 / 北向成交额的走势。"""
    dates = _recent_trade_dates(session, end, days)
    if not dates:
        return FundsSeries(
            dates=[],
            financing_balance=[],
            financing_buy=[],
            hsgt_turnover=[],
            financing_complete=[],
        )

    start = dates[0]
    margin_rows = list(
        session.scalars(
            select(MarginDaily).where(
                MarginDaily.trade_date >= start, MarginDaily.trade_date <= end
            )
        )
    )
    hsgt_rows = list(
        session.scalars(
            select(HsgtDaily).where(
                HsgtDaily.trade_date >= start, HsgtDaily.trade_date <= end
            )
        )
    )

    margin_by_day: dict[date, dict[str, MarginDaily]] = {}
    for row in margin_rows:
        margin_by_day.setdefault(row.trade_date, {})[row.market] = row
    hsgt_by_day: dict[date, float] = {}
    for row in hsgt_rows:
        if row.turnover is not None:
            hsgt_by_day[row.trade_date] = hsgt_by_day.get(row.trade_date, 0.0) + row.turnover

    balance: list[float | None] = []
    buy: list[float | None] = []
    turnover: list[float | None] = []
    complete: list[bool] = []
    for day in dates:
        markets = margin_by_day.get(day, {})
        sh, sz = markets.get("sh"), markets.get("sz")
        # 与 overview 同口径：两市齐全才算合计；否则整条曲线会在深市数据
        # 到达前低一截，图上看起来像断崖
        full = (
            sh is not None
            and sz is not None
            and sh.financing_balance is not None
            and sz.financing_balance is not None
        )
        complete.append(full)
        balance.append(
            (sh.financing_balance + sz.financing_balance) * YI if full else None
        )
        buy.append(
            (sh.financing_buy + sz.financing_buy) * YI
            if full and sh.financing_buy is not None and sz.financing_buy is not None
            else None
        )
        turnover.append(
            hsgt_by_day[day] * MILLION if day in hsgt_by_day else None
        )

    return FundsSeries(
        dates=dates,
        financing_balance=balance,
        financing_buy=buy,
        hsgt_turnover=turnover,
        financing_complete=complete,
    )


@router.get("/futures-series", response_model=FuturesSeries)
def futures_series(
    days: int = Query(SERIES_DEFAULT_DAYS, ge=5, le=SERIES_MAX_DAYS),
    end: date = Depends(resolve_trade_date),
    session: Session = Depends(get_db),
) -> FuturesSeries:
    """期指前 20 净空的走势（手）：IF / IC / IH / IM 各一条 + 四品种合计。

    与 `overview.futures_net_short`（只给最近一份）互补 —— 那边是卡片，这边是曲线。
    中金所约 16:00 发布，当天没发布的日子给 `None`（曲线上断开），不补 0。
    """
    dates = _recent_trade_dates(session, end, days)
    if not dates:
        return FuturesSeries(
            dates=[], if_net=[], ic_net=[], ih_net=[], im_net=[], total=[]
        )

    varieties = ("IF", "IC", "IH", "IM")
    by_day: dict[date, dict[str, int | None]] = {}
    for row in session.scalars(
        select(FuturesPosition).where(
            FuturesPosition.trade_date >= dates[0], FuturesPosition.trade_date <= end
        )
    ):
        by_day.setdefault(row.trade_date, {})[row.variety] = row.top_net_short

    picked: dict[str, list[int | None]] = {variety: [] for variety in varieties}
    total: list[int | None] = []
    for day in dates:
        values = by_day.get(day, {})
        row = [values.get(variety) for variety in varieties]
        for variety, value in zip(varieties, row):
            picked[variety].append(value)
        if all(value is not None for value in row):
            total.append(sum(value for value in row if value is not None))
        else:
            total.append(None)

    return FuturesSeries(
        dates=dates,
        if_net=picked["IF"],
        ic_net=picked["IC"],
        ih_net=picked["IH"],
        im_net=picked["IM"],
        total=total,
    )


def _latest_data_date(session: Session, column, target: date) -> date:
    """某张表里**最近有数据的日期**：从请求日往前找。

    ETF 份额与 DDE 都用它，原因一样：数据的落库日期由来源决定、通常**比请求日早**
    （ETF 份额 T+1 才更新；DDE 的扫描数据也是当天收盘后才逐步发布），
    按请求日直接查就会「打开就是空白」，很容易被当成功能坏了。

    回落到最近有数据的一天，**并把实际日期返回给前端显示** —— 否则面板展示的
    是前一天的数、而页面顶部写着今天，对着看会误读。
    """
    latest = session.scalar(select(func.max(column)).where(column <= target))
    return latest or target


def _etf_flows(
    session: Session, target: date
) -> tuple[date, date | None, list[EtfFlowItem], bool]:
    """当日每只 ETF 的份额变化与净申赎。两个 ETF 接口共用。

    返回 (**实际数据日**, 对比基准日, 明细, 有没有可比基准)。
    实际数据日取「最近一个**覆盖达标**的份额快照日」、基准日取它之前最近的达标日
    （都来自 `services.etf_flow`，与 `/api/market/etf-net-subscription` 的曲线同口径）。

    **为什么不能直接用「请求日 / 上一交易日」**（2026-10-22 修）：快照日期不是全市场
    统一的一天（跨境/QDII 晚一天），会造出只有几十行的**碎片快照日**。拿紧邻交易日
    当基准，今天能配上的只有那几十只、常与当天零交集 → 返回 has_prev=True + 空列表，
    前端把「没得比」显示成「今天没人申赎」，那是两个相反的结论。基准日覆盖不足
    （< `MIN_ROWS`）时一律 `has_prev=False`。
    """
    requested = target
    target = etf_flow.latest_covered_day(session, requested)
    if target is None:
        # 一个达标快照日都没有（新库 / 只有碎片快照）：保持旧行为 —— 返回最近有数据的
        # 那天 + 空列表 + has_prev=False，页面显示「没有可比数据」而不是报错。
        return (
            _latest_data_date(session, EtfShare.trade_date, requested),
            None,
            [],
            False,
        )
    prev = etf_flow.prev_covered_day(session, target)
    if prev is None:
        return target, None, [], False

    today = list(session.scalars(select(EtfShare).where(EtfShare.trade_date == target)))
    yesterday = {
        row.code: row
        for row in session.scalars(select(EtfShare).where(EtfShare.trade_date == prev))
    }
    if not yesterday:
        return target, prev, [], False

    # 货币现金类整类不进申赎榜：单只榜按净申赎排名，货基的错误份额变化会霸榜；
    # 行业榜也会多出一行噪声。理由与证据见 `_money_fund_codes`（2026-10-11）。
    money = _money_fund_codes(session)
    items: list[EtfFlowItem] = []
    for row in today:
        if row.code in money:
            continue
        before = yesterday.get(row.code)
        if before is None or row.shares is None or before.shares is None:
            continue
        delta = row.shares - before.shares
        items.append(
            EtfFlowItem(
                code=row.code,
                name=row.name,
                close=row.close,
                pct_chg=row.pct_chg,
                amount=row.amount,
                shares=row.shares,
                share_delta=delta,
                # 净申赎金额用当日收盘价估算：份额变化 × 价格。这是近似 ——
                # 申赎按当日净值成交而非收盘价，但两者通常贴得很近
                net_inflow=None if not row.close else delta * row.close,
            )
        )
    return target, prev, items, True


def _sort_flows(items: list[EtfFlowItem], order: str) -> None:
    if order == "amount":
        items.sort(key=lambda item: item.amount or 0, reverse=True)
    elif order == "outflow":
        items.sort(key=lambda item: item.net_inflow or 0)
    else:
        items.sort(key=lambda item: item.net_inflow or 0, reverse=True)


@router.get("/etf", response_model=EtfFlowBoard)
def etf_flows(
    target: date = Depends(resolve_trade_date),
    limit: int = Query(30, ge=5, le=200),
    order: str = Query("inflow", pattern="^(inflow|outflow|amount)$"),
    session: Session = Depends(get_db),
) -> EtfFlowBoard:
    """ETF 申赎排行（按单只）。份额变化 = 净申购，是真金白银的资金进出。

    `trade_date` 返回的是**实际数据日**（ETF 份额 T+1 才更新、且会剔除碎片快照日，
    所以通常比请求的日期早），见 `_etf_flows`；`prev_date` 是实际用的基准日。
    """
    actual, prev, items, has_prev = _etf_flows(session, target)
    _sort_flows(items, order)
    return EtfFlowBoard(
        trade_date=actual, prev_date=prev, has_prev=has_prev, items=items[:limit]
    )


# 行业榜里每个分类附带几只明细。给太多响应会很大（宽基一类就有 400 只），
# 而看全量明细本来就有「按单只」视图 —— 这几条只用来回答「这个行业是谁在动」
INDUSTRY_FUND_LIMIT = 12


@router.get("/etf-industry", response_model=EtfIndustryBoard)
def etf_industry(
    target: date = Depends(resolve_trade_date),
    limit: int = Query(30, ge=5, le=200),
    order: str = Query("inflow", pattern="^(inflow|outflow|amount)$"),
    session: Session = Depends(get_db),
) -> EtfIndustryBoard:
    """ETF 申赎按行业 / 主题汇总。

    分类来自 `services/etf_category` 的名称关键词词典 —— **没有任何数据源
    直接给 ETF 的行业归属**，词典实测覆盖 96% 只数 / 99.9% 成交额。
    """
    actual, prev, items, has_prev = _etf_flows(session, target)
    if not has_prev:
        return EtfIndustryBoard(
            trade_date=actual, prev_date=prev, has_prev=False, items=[]
        )

    categories = {
        code: category
        for code, category in session.execute(
            select(EtfCategory.code, EtfCategory.category)
        )
    }
    # 不在映射表里的归「其他」，而不是让它从榜上消失 ——
    # 消失会让人以为这只 ETF 今天没有申赎
    groups: dict[str, list[EtfFlowItem]] = {}
    for item in items:
        groups.setdefault(categories.get(item.code, UNKNOWN), []).append(item)

    board: list[EtfIndustryItem] = []
    for category, members in groups.items():
        inflows = [m.net_inflow for m in members if m.net_inflow is not None]
        amounts = [m.amount for m in members if m.amount is not None]
        # 涨跌幅按**成交额加权**：等权会让一只成交几百万的小 ETF 和几百亿的
        # 宽基平分权重，那个平均值说明不了这个行业今天到底涨没涨
        num = sum(m.pct_chg * m.amount for m in members if m.pct_chg is not None and m.amount)
        den = sum(m.amount for m in members if m.pct_chg is not None and m.amount)
        # 明细也按净申赎降序，与榜单口径一致
        members.sort(key=lambda item: item.net_inflow or 0, reverse=True)
        board.append(
            EtfIndustryItem(
                category=category,
                fund_count=len(members),
                net_inflow=sum(inflows) if inflows else None,
                amount=sum(amounts) if amounts else None,
                pct_chg=num / den if den else None,
                funds=members[:INDUSTRY_FUND_LIMIT],
            )
        )

    if order == "amount":
        board.sort(key=lambda item: item.amount or 0, reverse=True)
    elif order == "outflow":
        board.sort(key=lambda item: item.net_inflow or 0)
    else:
        board.sort(key=lambda item: item.net_inflow or 0, reverse=True)
    return EtfIndustryBoard(
        trade_date=actual, prev_date=prev, has_prev=True, items=board[:limit]
    )


@router.get("/institutions", response_model=InstitutionBoard)
def institutions(
    target: date = Depends(resolve_trade_date),
    limit: int = Query(30, ge=5, le=200),
    order: str = Query("net", pattern="^(net|buy|sell)$"),
    session: Session = Depends(get_db),
) -> InstitutionBoard:
    """龙虎榜机构席位排行（只含机构专用席位）。

    同一只票可能因多条上榜原因出现多行，这里**按代码取第一条** ——
    机构买卖额是股票的属性，不是「上榜原因」的属性，按行展开会重复计算。
    """
    rows = list(
        session.scalars(select(LhbInstitution).where(LhbInstitution.trade_date == target))
    )
    seen: set[str] = set()
    items: list[InstitutionItem] = []
    for row in rows:
        if row.code in seen:
            continue
        seen.add(row.code)
        items.append(
            InstitutionItem(
                code=row.code,
                name=row.name,
                close=row.close,
                pct_chg=row.pct_chg,
                buy_count=row.buy_count,
                sell_count=row.sell_count,
                buy_amount=row.buy_amount,
                sell_amount=row.sell_amount,
                net_amount=row.net_amount,
                reason=row.reason,
            )
        )

    if order == "buy":
        items.sort(key=lambda item: item.buy_amount or 0, reverse=True)
    elif order == "sell":
        items.sort(key=lambda item: item.sell_amount or 0, reverse=True)
    else:
        items.sort(key=lambda item: item.net_amount or 0, reverse=True)

    # 板块 / 行业只查「要返回的那几十只」：先排序截断，再统一补两列，
    # 口径与形态页 / 异动页同一份实现（`services.stock_meta`）
    top = items[:limit]
    codes = {item.code for item in top}
    sector_map = sectors_of(session, codes)
    industry_map = industries_of(session, codes)
    for item in top:
        item.sectors = sector_map.get(item.code, [])
        item.industry = industry_map.get(item.code)

    return InstitutionBoard(trade_date=target, items=top, total=len(items))


# DDE 榜的条数上限。当天全市场扫描的实际覆盖是两三千只，给到 200 已经够看
# 「最强的那些」；再大只是把响应撑肥（这个榜是按值排序的，不是全量清单）
DDE_LIMIT_MAX = 200


@router.get("/dde", response_model=DdeBoard)
def dde_board(
    target: date = Depends(resolve_trade_date),
    limit: int = Query(30, ge=5, le=DDE_LIMIT_MAX),
    order: str = Query("inflow", pattern="^(inflow|outflow)$"),
    session: Session = Depends(get_db),
) -> DdeBoard:
    """个股 5日DDE 排名。**零额外配额** —— 数据来自每天采集链末尾的全市场扫描。

    扫描那边一次抓全市场（`jobs/scan_dde`，一天 14 次调用），这里只是把它读出来；
    打开这个页面不再花任何配额，也不用新增采集步骤。

    三点如实交代：

    1. `trade_date` 返回**实际数据日**（从请求日往前找最近有 DDE 的那天）——
       扫描数据同样在收盘后才逐步发布，按请求日直接查会是空白；
    2. `total` 是当天**有 DDE 的票数**（覆盖度），不是全市场只数；
    3. 涨跌幅与收盘价取自日线，而日线只覆盖「流动性池 + 池外涨停」，池外那部分票
       这两列**留空**（面板显示「—」）—— 不用 0 顶替，「0%」与「没有数据」是两件事。

    口径：这里的 `dde` 就是个股页那一栏的 5日DDE（同一张表，实测两条路径完全一致）。
    """
    day = _latest_data_date(session, StockDde.trade_date, target)
    # 只取两列、只构造「要返回的那几十个」DdeItem（2026-10-22 体检）：
    # 以前把当天 5000 多行的 ORM 实体全建出来、又给每一行都建一个 pydantic 对象，
    # 最后才切前 30 个 —— 实测 300ms 里大半花在这上面。
    raw = session.execute(
        select(StockDde.code, StockDde.dde).where(
            StockDde.trade_date == day, StockDde.dde.is_not(None)
        )
    ).all()
    if not raw:
        return DdeBoard(trade_date=day, items=[], total=0)

    # 当天日线一次性读成映射。**不要用 IN(两千多个代码) 去查** —— SQLite 的绑定
    # 参数个数有上限（老版本 999），两千多个代码会直接把查询打挂
    quotes = {
        code: (name, close, pct_chg)
        for code, name, close, pct_chg in session.execute(
            select(
                StockDaily.code, StockDaily.name, StockDaily.close, StockDaily.pct_chg
            ).where(StockDaily.trade_date == day)
        )
    }
    # 名字兜底：池外的票没有日线，用基础信息表补名字，免得榜上只剩代码
    basics = {
        code: name
        for code, name in session.execute(select(StockBasic.code, StockBasic.name))
    }

    raw.sort(key=lambda item: item[1] or 0, reverse=order != "outflow")
    # 与机构榜一样：先截断再补「板块 / 行业」，只对要返回的那几十只发查询
    top = raw[:limit]
    codes = {code for code, _ in top}
    sector_map = sectors_of(session, codes)
    industry_map = industries_of(session, codes)
    items = []
    for code, dde in top:
        name, close, pct_chg = quotes.get(code, (None, None, None))
        items.append(
            DdeItem(
                code=code,
                name=name or basics.get(code),
                close=close,
                pct_chg=pct_chg,
                dde=dde,
                sectors=sector_map.get(code, []),
                industry=industry_map.get(code),
            )
        )
    return DdeBoard(trade_date=day, items=items, total=len(raw))
