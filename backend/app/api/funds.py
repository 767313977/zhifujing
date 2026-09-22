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
from app.models import (
    EtfCategory,
    EtfShare,
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
    InstitutionBoard,
    InstitutionItem,
    MarginSnapshot,
)
from app.services.etf_category import UNKNOWN

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


@router.get("/overview", response_model=FundFlowOverview)
def overview(
    target: date = Depends(resolve_trade_date),
    session: Session = Depends(get_db),
) -> FundFlowOverview:
    """资金面总览：当日两融、北向成交额、ETF 净申赎、机构席位。"""
    prev = _prev_trade_date(session, target)

    margin_rows = list(
        session.scalars(select(MarginDaily).where(MarginDaily.trade_date == target))
    )
    by_market = {row.market: row for row in margin_rows}
    sh, sz = by_market.get("sh"), by_market.get("sz")

    # 两市合计：**只在两个市场都有数时才给**。深市常比沪市晚一天（实测），
    # 只有一个市场时相加会把「深市待披露」误报成「深市归零」——
    # 那个数字会小一大截，看起来像两融骤降。
    if sh is not None and sz is not None and sh.financing_balance and sz.financing_balance:
        financing_total = (sh.financing_balance + sz.financing_balance) * YI
    else:
        financing_total = None

    financing_buy_total = None
    if sh is not None and sz is not None and sh.financing_buy and sz.financing_buy:
        financing_buy_total = (sh.financing_buy + sz.financing_buy) * YI

    # 融资余额变化：拿上一交易日的同口径合计来比，同样要求两边齐全
    financing_change = None
    if prev is not None and financing_total is not None:
        prev_rows = list(
            session.scalars(select(MarginDaily).where(MarginDaily.trade_date == prev))
        )
        prev_by_market = {row.market: row for row in prev_rows}
        prev_sh, prev_sz = prev_by_market.get("sh"), prev_by_market.get("sz")
        if (
            prev_sh is not None
            and prev_sz is not None
            and prev_sh.financing_balance
            and prev_sz.financing_balance
        ):
            financing_change = financing_total - (
                prev_sh.financing_balance + prev_sz.financing_balance
            ) * YI

    hsgt_rows = list(
        session.scalars(select(HsgtDaily).where(HsgtDaily.trade_date == target))
    )
    hsgt_turnover = (
        sum(row.turnover for row in hsgt_rows if row.turnover is not None) * MILLION
        if hsgt_rows
        else None
    )
    hsgt_prev = None
    if prev is not None:
        prev_hsgt = list(
            session.scalars(select(HsgtDaily).where(HsgtDaily.trade_date == prev))
        )
        if prev_hsgt:
            hsgt_prev = (
                sum(row.turnover for row in prev_hsgt if row.turnover is not None) * MILLION
            )

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

    etf_inflow = _etf_net_inflow(session, target, prev)

    return FundFlowOverview(
        trade_date=target,
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
    )


def _etf_net_inflow(session: Session, target: date, prev: date | None) -> float | None:
    """ETF 当日净申赎（元）= Σ(份额变化 × 当日收盘价)。

    没有前一日数据（首次采集）时返回 None 而不是 0 —— 「没得比」与
    「今天一分钱没进来」是两件事，后者是个结论，前者只是缺数据。
    """
    if prev is None:
        return None
    today = {
        row.code: row
        for row in session.scalars(select(EtfShare).where(EtfShare.trade_date == target))
    }
    yesterday = {
        row.code: row
        for row in session.scalars(select(EtfShare).where(EtfShare.trade_date == prev))
    }
    total = 0.0
    matched = False
    for code, row in today.items():
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
    实际数据日由 `_etf_data_date` 从请求日往前回落而来 —— 接口必须把它放进响应，
    否则前端只会看到请求的那个日期，标不出「这份 ETF 数据是哪天的」。
    **has_prev 判的是「上一交易日的份额数据在不在」，不是「上一交易日在不在」** ——
    交易日历里天天都是交易日，但份额要等第一次采集才有数；只看日期会返回
    has_prev=True + 空列表，前端于是把「没得比」显示成「今天没人申赎」，
    那是两个相反的结论。
    """
    target = _latest_data_date(session, EtfShare.trade_date, target)
    prev = _prev_trade_date(session, target)
    if prev is None:
        return target, None, [], False

    today = list(session.scalars(select(EtfShare).where(EtfShare.trade_date == target)))
    yesterday = {
        row.code: row
        for row in session.scalars(select(EtfShare).where(EtfShare.trade_date == prev))
    }
    if not yesterday:
        return target, prev, [], False

    items: list[EtfFlowItem] = []
    for row in today:
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

    `trade_date` 返回的是**实际数据日**（ETF 份额 T+1 才更新，所以通常比请求的
    日期早一天），见 `_etf_data_date`。
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

    return InstitutionBoard(trade_date=target, items=items[:limit], total=len(items))


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
    rows = list(
        session.scalars(
            select(StockDde).where(StockDde.trade_date == day, StockDde.dde.is_not(None))
        )
    )
    if not rows:
        return DdeBoard(trade_date=day, items=[], total=0)

    # 当天日线一次性读成映射。**不要用 IN(两千多个代码) 去查** —— SQLite 的绑定
    # 参数个数有上限（老版本 999），两千多个代码会直接把查询打挂
    quotes = {
        row.code: row
        for row in session.scalars(select(StockDaily).where(StockDaily.trade_date == day))
    }
    # 名字兜底：池外的票没有日线，用基础信息表补名字，免得榜上只剩代码
    basics = {
        code: name
        for code, name in session.execute(select(StockBasic.code, StockBasic.name))
    }

    items = [
        DdeItem(
            code=row.code,
            name=(quotes[row.code].name if row.code in quotes else None)
            or basics.get(row.code),
            close=quotes[row.code].close if row.code in quotes else None,
            pct_chg=quotes[row.code].pct_chg if row.code in quotes else None,
            dde=row.dde,
        )
        for row in rows
    ]
    if order == "outflow":
        items.sort(key=lambda item: item.dde or 0)
    else:
        items.sort(key=lambda item: item.dde or 0, reverse=True)
    return DdeBoard(trade_date=day, items=items[:limit], total=len(items))
