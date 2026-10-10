"""个股详情接口。

数据来自本地缓存的日线（`stock_daily`）。库里**大部分票**是每日由
`jobs/collect_kline.py` 按 `stock_universe`（流动性 / 市值筛过的池子，3032 只）
批量落库的；但**池外的票没有**，所以首次打开某只池外个股时前端会自动调一次
同步，之后走缓存（见设计文档 8.16、8.22.2）。

「所属题材」不在这里现取：它读的是 `stock_concept`（涨停天梯的日度快照），
只覆盖涨停过的票，见 `themes()` 的说明。
"""

import logging
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db, session_scope
from app.jobs.collect_daily import (
    STOCK_FULL_DAYS,
    CollectionBusy,
    DailyCollector,
    collect_guard,
)
from app.jobs.collect_dde import CLOSE_READY, collect_stock_dde
from app.jobs.collect_dde import DEFAULT_DAYS as DDE_DAYS
from app.models import (
    AppUser,
    Lhb,
    LimitPool,
    SectorDaily,
    StockBasic,
    StockConcept,
    StockDaily,
    StockDde,
    TradeCalendar,
    Watchlist,
)
from app.schemas import (
    StockDailyRow,
    StockDdeOut,
    StockDdeRow,
    StockNewsOut,
    StockNewsRow,
    StockProfile,
    StockThemeItem,
    StockThemes,
)
from app.services import limit_rules
from app.services.auth import Cooldown, RateLimiter, current_user
from app.services.patterns import build_bars
from app.services.stock_phase import load_phase
from app.services.usage import QuotaLevel, level_label, quota_level
from app.sources.em_news import fetch_stock_news
from app.sources.ifind import IfindError, normalize_code
from app.sources.kaipanhong import TAXONOMY_SELECTED

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stock", tags=["个股"])

# 手动同步的**按用户冷却**。本站唯一的成本就是 iFinD 调用，而一次 `sync` 会真发十几次
# tools/call（`days` 上限 500，见 `sync` 的签名）—— 不加冷却的话连点 / 脚本刷能在一分钟
# 内烧掉成百上千次配额。取 60 秒：把按用户的手动同步限到 ≤1 次/分钟，足以挡住连点与刷；
# 而正常使用（同一天补几只票）不受影响 —— 单看一只票的日 K 通常要花一分多钟，真要连开
# 多只也不会卡在 60 秒内。（`/api/watchlist/sync` 那边是 600 秒，因为它一次刷全部自选、
# 且每日采集会兜底刷新，量级与本接口单只票不同。）
SYNC_COOLDOWN_SEC = 60
_sync_cooldown = Cooldown(SYNC_COOLDOWN_SEC)

# 「现取一次 DDE」的按用户限流（2026-10-10 加）。这个接口在库里没有最近交易日的数据时
# 会现取一次（`collect_stock_dde`，花 1 次 iFinD 配额），而**空结果不缓存** —— 传一个
# 无效代码（如 999999）反复请求就是无限次配额消耗，写个循环能把当月配额刷光。
#
# ⚠️ 为什么不用上面的 `Cooldown`（同 key N 秒只放行一次）：正常翻股票是**连着看好几只**，
# 每只都合法地要现取一次，「每请求冷 60 秒」会把第 2 只起直接掐掉。所以用**滑动窗口**：
# 10 分钟最多 30 次。`RateLimiter` 本来是给登录爆破用的（失败累计到阈值就锁），这里把它
# 当「放行计数器」使 —— 每次真的现取记一笔，超了就锁一段时间，期间只返回缓存。
#
# ⚠️ 也**不做「空结果负缓存」**：攻击是枚举**不同**代码，负缓存只挡得住重复同一个，
# 属于看着有用、实际挡不住的防御。
DDE_FETCH_LIMIT = 30
DDE_FETCH_WINDOW_SEC = 600
_dde_fetch_limiter = RateLimiter(
    limit=DDE_FETCH_LIMIT,
    window_sec=DDE_FETCH_WINDOW_SEC,
    lock_sec=DDE_FETCH_WINDOW_SEC,
)

# 个股新闻的 TTL 缓存 + 按用户限流（2026-10-11 加）。新闻这条源没有配额，但
# **每次打开都现取**等于让任何人拿它当爬虫去打外部站点；而新闻的价值以分钟计，
# 缓存 10 分钟完全够用。限流只挡「缓存未命中时的现取」，命中缓存不计数。
#
# ⚠️ 缓存键含用户可传的 6 位代码，理论上组合极大 → 必须**有上限**（见 _cache_news）：
# 不然反复换假代码就能把内存堆满。expired 条目在写新值时顺手清掉。
NEWS_TTL_SEC = 600
NEWS_CACHE_MAX = 256
NEWS_FETCH_LIMIT = 30
NEWS_FETCH_WINDOW_SEC = 600
_news_cache: dict[tuple[str, int], tuple[datetime, StockNewsOut]] = {}
_news_fetch_limiter = RateLimiter(
    limit=NEWS_FETCH_LIMIT,
    window_sec=NEWS_FETCH_WINDOW_SEC,
    lock_sec=NEWS_FETCH_WINDOW_SEC,
)


def _cache_news(key: tuple[str, int], value: StockNewsOut) -> None:
    """写新闻缓存并维持上限：先清过期，再超就丢最旧的一条（只是内存兜底）。"""
    now = datetime.now()
    ttl = timedelta(seconds=NEWS_TTL_SEC)
    _news_cache[key] = (now, value)
    if len(_news_cache) > NEWS_CACHE_MAX:
        for stale_key, (stamp, _) in list(_news_cache.items()):
            if now - stamp >= ttl:
                _news_cache.pop(stale_key, None)
    if len(_news_cache) > NEWS_CACHE_MAX:
        oldest = min(_news_cache, key=lambda k: _news_cache[k][0])
        _news_cache.pop(oldest, None)


# 可选的 K 线周期。周/月由本地日线重采样（`_resample`），不额外取数
PERIODS = ("day", "week", "month")

# 复权方式。**三档是与同花顺对齐的看图口径**（前端那个右键菜单的三项）：
# none = 除权(不复权)、qfq = 向前复权、hfq = 向后复权。见 `_adjusted`
FQ_MODES = ("none", "qfq", "hfq")

# DDE 一栏最多能要多少个交易日。**卡在 100 是因为来源自己的输出上限**（实测请求 120 与
# 250 个交易日都只回 100 行，并在回答里写「以下为部分数据」）—— 这个上限与配额无关，
# 开上去只会让图里悄悄少画一段，所以宁可在接口层就挡住（`collect_dde._TRUNCATED_HINTS`）。
DDE_MAX_DAYS = 100


def _code(raw: str) -> str:
    code = normalize_code(raw)
    if len(code) != 6:
        raise HTTPException(
            status_code=400, detail=f"股票代码应为 6 位数字，收到「{raw}」"
        )
    return code


def _resolve_name(session: Session, code: str) -> str | None:
    """这只股票叫什么。iFinD 的题材问句只认名称，所以必须先有名字。

    **只信库里的客观数据**（`stock_basic` / `stock_daily`），**不看** `watchlist.name`：
    那一列是用户自己 POST/PATCH 填的，而这个名字会被个股页、DDE、新闻、题材、
    新闻过滤词 `_mentions`、iFinD 题材问句一起用 —— 让会员填的名字变成「全站可见」
    的股票名，等于把展示层字段开成了可写的（2026-10-11 收紧）。

    顺序：`stock_basic`（权威的当前简称）→ `stock_daily`（日线里落的历史名称）。
    """
    basic = session.get(StockBasic, code)
    if basic and basic.name:
        return basic.name
    latest = session.scalars(
        select(StockDaily.name)
        .where(StockDaily.code == code, StockDaily.name.is_not(None))
        .order_by(StockDaily.trade_date.desc())
        .limit(1)
    ).first()
    return latest


def _pct_chg_5d(recent: list[StockDaily]) -> float | None:
    """近 5 个交易日涨跌幅（%）= 最新收盘 / 5 个交易日前的收盘 − 1。

    要 **6 根**日线：第 1 根是最新、第 6 根才是 5 个交易日前的基准。不足 6 根
    （次新股、刚缓存一天）就算不出来，返回 None —— 前端不显示这一格，**不填 0**。
    """
    if len(recent) < 6:
        return None
    base, tip = recent[5].close, recent[0].close
    if not base or tip is None:
        return None
    return (tip / base - 1) * 100


# 概况格那几个市值 / 换手 / 市盈率字段的缺省值（还没跑过建池、或名单里没这只票）
_NO_MARKET = {
    "total_mv": None,
    "pe_forecast": None,
    "free_float_mv": None,
    "actual_turnover": None,
    "asof": None,
}


def _market_fields(session: Session, code: str, latest: StockDaily | None) -> dict:
    """总市值 / 自由流通市值 / 实际换手率 / 动态市盈率（概况格要用的那几格）。

    数据来自 `stock_basic` —— 建池时随选股接口一并取回，**覆盖全 A、不只池内 3000 只**
    （见 `collect_universe._basics`）。没跑过建池、或名单里没有这只票，就全给 None
    （前端显示「—」，不填 0）。

    ⚠️ **三个量的口径不同，不能一起缩放**：

    - `total_mv` / `pe_forecast` 是按 `asof` 那天的价格算的 → 必须乘 `最新收盘 / asof 收盘`，
      否则显示的是最多 7 天前的市值（建池七天才跑一次）
    - `free_float_shares` 是**股数**、慢变 → 直接乘最新收盘价得自由流通市值，**不缩放**
    - `实际换手率 = 成交量 / 自由流通股`。这个口径与 iFinD 自己返回的「实际换手率」
      **逐位相同**（603773 实测 27.06775131969144），所以不另存一份
    """
    basic = session.get(StockBasic, code)
    if basic is None or latest is None or not latest.close:
        return dict(_NO_MARKET)

    # 缩放系数：asof 那天收盘 → 最新收盘。同一天就是 1
    ratio: float | None = None
    if basic.asof is not None:
        if basic.asof >= latest.trade_date:
            # 同一天：不用缩。**asof 比这只票的日线还新**时也不缩（2026-09-30 修）：
            # 那说明建池时股价是新的、而这票自己的日线还没跟上（停牌、或那天的日线
            # 还没采），拿旧收盘去除只会把一个**过时且更小**的市值显示出来 ——
            # 这时 asof 那份数据本身就是我们手上最新的，原样用即可。
            ratio = 1.0
        else:
            base_close = session.scalar(
                select(StockDaily.close).where(
                    StockDaily.code == code, StockDaily.trade_date == basic.asof
                )
            )
            # 基准日收盘拿不到就**不给市值**：宁可显示「—」，也不显示一个几周前的旧值
            ratio = latest.close / base_close if base_close else None

    def scaled(value: float | None) -> float | None:
        """随股价变的量（市值、市盈率）才缩放，股本类的量不缩。"""
        if value is None or ratio is None:
            return None
        return value * ratio

    shares = basic.free_float_shares
    return {
        "total_mv": scaled(basic.total_mv),
        "pe_forecast": scaled(basic.pe_forecast),
        "free_float_mv": shares * latest.close if shares else None,
        "actual_turnover": latest.volume / shares * 100 if shares and latest.volume else None,
        "asof": basic.asof,
    }


@router.get("/{code}", response_model=StockProfile)
def profile(
    code: str,
    user: AppUser = Depends(current_user),
    session: Session = Depends(get_db),
) -> StockProfile:
    code = _code(code)
    # 「在不在自选里」问的是**当前用户自己的**自选 —— 复合主键 (user_id, code)
    item = session.get(Watchlist, (user.id, code))
    # 取 6 根：第 1 根当 latest，第 6 根用来算「五日涨跌幅」（见 _pct_chg_5d）
    recent = list(
        session.scalars(
            select(StockDaily)
            .where(StockDaily.code == code)
            .order_by(StockDaily.trade_date.desc())
            .limit(6)
        )
    )
    latest = recent[0] if recent else None
    count, first_date, last_date = session.execute(
        select(
            func.count(),
            func.min(StockDaily.trade_date),
            func.max(StockDaily.trade_date),
        ).where(StockDaily.code == code)
    ).one()

    # 把该股与复盘数据关联起来：涨停过哪天、上过几次龙虎榜
    limit_up_dates = list(
        session.scalars(
            select(LimitPool.trade_date)
            .where(LimitPool.code == code, LimitPool.pool_type == "up")
            .order_by(LimitPool.trade_date.desc())
        )
    )
    lhb_count = (
        session.scalar(select(func.count()).select_from(Lhb).where(Lhb.code == code)) or 0
    )
    market = _market_fields(session, code, latest)
    # 悟道「阶段判定」：判定在 services/patterns.classify_phase，取数壳在
    # services/stock_phase（个股分析页也用它，两边不能各取一份日线）

    return StockProfile(
        code=code,
        name=_resolve_name(session, code),
        in_watchlist=item is not None,
        note=item.note if item else None,
        latest=StockDailyRow.model_validate(latest) if latest else None,
        day_count=count or 0,
        first_date=first_date,
        last_date=last_date,
        limit_up_dates=limit_up_dates,
        lhb_count=lhb_count,
        pct_chg_5d=_pct_chg_5d(recent),
        total_mv=market["total_mv"],
        total_mv_asof=market["asof"],
        free_float_mv=market["free_float_mv"],
        actual_turnover=market["actual_turnover"],
        pe_forecast=market["pe_forecast"],
        phase=load_phase(session, code),
    )


@router.get("/{code}/daily", response_model=list[StockDailyRow])
def daily(
    code: str,
    days: int = Query(120, ge=5, le=500, description="返回最近 N 个交易日，按日期升序"),
    fq: str = Query(
        "none",
        description="复权方式：none=除权(不复权) qfq=向前复权 hfq=向后复权。"
        "形态页必须用 qfq —— 引擎判定用的就是那条序列，不复权图上的除权跳空会让"
        "形态标注线画在错误的高度",
    ),
    vol_adjust: bool = Query(
        False,
        description="成交量也按同一复权比例缩放（只在前/后复权时有意义），见 _adjusted",
    ),
    period: str = Query(
        "day",
        description="day=日K week=周K month=月K。周/月由本地日线重采样得到，不额外取数",
    ),
    session: Session = Depends(get_db),
) -> list[StockDailyRow]:
    target = _code(code)
    if period not in PERIODS:
        raise HTTPException(
            status_code=400,
            detail=f"period 只能是 {'、'.join(PERIODS)}，收到「{period}」",
        )
    if fq not in FQ_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"fq 只能是 {'、'.join(FQ_MODES)}，收到「{fq}」",
        )
    rows = list(
        session.scalars(
            select(StockDaily)
            .where(StockDaily.code == target)
            .order_by(StockDaily.trade_date.desc())
            .limit(days)
        )
    )
    rows.reverse()
    if not rows:
        return []

    items = [_daily_item(row) for row in rows]

    # 涨跌停标记只在**日 K** 上有意义：周/月一根柱子是几天的合并，「这天涨停」在那个粒度上
    # 不成立（周涨幅够不到 10% 不代表那一周没有涨停日）。所以非日 K 一律留 None。
    #
    # ⚠️ 判定必须在 `_adjusted` / `_resample` **之前**做，理由两条：
    # 1. 判据是「收盘价 = 交易所板价」，而板价是**原始价**取整到分的概念 —— 前复权整段乘一个
    #    系数之后再取整到分没有意义（半分的边界会被缩放搅乱）
    # 2. 板价要用**前一根收盘价**当基准，所以逐行算、按交易日挂回去
    # 名称只用来认 ST（主板 ST 是 5cm），取 `StockBasic` 那份（权威的当前名称）。
    # ⚠️ 历史 ST 变更还原不了：库里没有按日的历史名称，所以「曾经 ST、现在摘帽」的票，
    #    历史上那段 5cm 涨跌停会被按 10% 判而**漏标**；反向（现在 ST、当时不是）在
    #    `limit_rules._limit_candidates` 里补了一档，不会多标。
    basic = session.get(StockBasic, target)
    name = basic.name if basic else None
    flags = _limit_flags(items, target, name) if period == "day" else {}

    if fq != "none":
        items = _adjusted(items, fq, vol_adjust=vol_adjust)
    if period != "day":
        items = _resample(items, period)

    # 按交易日取标记，而不是按下标：`_adjusted` 会把 OHLC 缺失的行剔掉，长度可能变
    empty = {"is_limit_up": None, "is_limit_down": None}
    return [
        StockDailyRow(**item, **flags.get(item["trade_date"], empty)) for item in items
    ]


def _limit_flags(items: list[dict], code: str, name: str | None) -> dict[date, dict]:
    """逐日判涨跌停，返回 {交易日: 两个标记}。

    **必须传原始价（不复权）的行**，且要按交易日升序 —— 前一根的收盘价当天板价的基准
    （除权日由 `limit_rules` 自己改用反推值，这里不管）。
    """
    out: dict[date, dict] = {}
    prev_close: float | None = None
    for item in items:
        out[item["trade_date"]] = {
            # ⚠️ 必须把 `trade_date` 传进去：主板 ST 的涨跌幅 2026-07-06 起从 5% 改成 10%，
            # 不传日期就只能按默认（当前口径）判，历史那段 ST 的 5% 板会被漏标。
            "is_limit_up": limit_rules.is_limit_up(
                item["pct_chg"],
                item["close"],
                item["high"],
                code,
                name,
                prev_close,
                trade_date=item["trade_date"],
            ),
            "is_limit_down": limit_rules.is_limit_down(
                item["pct_chg"],
                item["close"],
                item["low"],
                code,
                name,
                prev_close,
                trade_date=item["trade_date"],
            ),
        }
        prev_close = item["close"]
    return out


def _daily_item(row: StockDaily) -> dict:
    return {
        "trade_date": row.trade_date,
        "open": row.open,
        "high": row.high,
        "low": row.low,
        "close": row.close,
        "pct_chg": row.pct_chg,
        "volume": row.volume,
        "amount": row.amount,
        "turnover": row.turnover,
    }


def _adjusted(items: list[dict], mode: str, *, vol_adjust: bool = False) -> list[dict]:
    """复权。前复权复用形态引擎的复权逻辑，而不是在前端再实现一遍 ——「按当天收盘价
    做日内换算」这个细节很容易写错，两个实现早晚会不一致。

    **向后复权就是把同一条序列换个锚点**：`build_bars` 的净值序列锚在最新价
    （`前复权[-1] == 原始收盘[-1]`），换成锚在首日（`后复权[0] == 原始收盘[0]`）只是
    整条序列乘一个常数 `原始首日收盘 / 前复权首日`。所以没必要让形态引擎再算一遍
    —— 它只用前复权，也不该为看图多一个参数。

    `vol_adjust` 打开时成交量按**同一比例反向缩放**（`量 ÷ (复权价/原始价)`）：
    除权日的量能因此与复权价一致（送转后股本变了，直接比绝对量会突然跳一档）。
    这是本机口径 —— 成交量仍记作「股」，成交额保持原始金额（钱是钱，不缩放）。

    OHLC 缺一不可：`sync_stock` 那条路径写进来的行 OHLC 可能为 None，
    `build_bars` 里 `float(None)` 会直接抛 500。缺 OHLC 的行没法做日内换算，跳过。
    """
    usable = [
        item
        for item in items
        if item["close"] is not None
        and item["pct_chg"] is not None
        and item["open"] is not None
        and item["high"] is not None
        and item["low"] is not None
    ]
    if not usable:
        return []
    bars = build_bars(
        [
            {
                "date": item["trade_date"],
                "open": item["open"],
                "high": item["high"],
                "low": item["low"],
                "close": item["close"],
                "volume": item["volume"],
                "amount": item["amount"],
                "pct_chg": item["pct_chg"],
            }
            for item in usable
        ]
    )
    # 向后复权：整条序列乘这个常数（前复权首日价不会为 0，真为 0 就退回前复权）
    scale = 1.0
    if mode == "hfq" and bars.close[0]:
        scale = usable[0]["close"] / float(bars.close[0])

    out: list[dict] = []
    for index, item in enumerate(usable):
        close = float(bars.close[index]) * scale
        # 复权比例 = 复权价 / 原始价。成交量复权要的是它的倒数
        ratio = close / item["close"] if item["close"] else 1.0
        volume = item["volume"]
        if vol_adjust and volume is not None and ratio:
            volume = volume / ratio
        out.append(
            {
                "trade_date": bars.dates[index],
                "open": float(bars.open[index]) * scale,
                "high": float(bars.high[index]) * scale,
                "low": float(bars.low[index]) * scale,
                "close": close,
                # 涨跌幅、成交额、换手率不受复权影响，照原样带过来
                "pct_chg": item["pct_chg"],
                "volume": volume,
                "amount": item["amount"],
                "turnover": item["turnover"],
            }
        )
    return out


def _resample(items: list[dict], period: str) -> list[dict]:
    """把逐日行情合并成周 K / 月 K。

    分组：周用 ISO 周（`isocalendar()[:2]`，跨年也不会把相邻两天算成同一周），
    月用「年 + 月」。每根的 `trade_date` 取该组**最后一个交易日** —— 图上这根柱子
    标在周末，与常见软件的画法一致。

    周/月的涨跌幅库里没有，只能用相邻两根的收盘价算；第一根没有前一根，记 None
    （成交量柱会退回按「收 - 开」染色）。成交量 / 成交额求和，组内全空就给 None
    （缺数据与 0 是两回事）。

    **换手率不参与重采样**：它是个比率，几天相加没意义、取均值也不成立，
    所以这里不带 `turnover` 键，由 `StockDailyRow` 的默认值落成 None（前端就不显示这项）。
    """
    grouped: list[tuple[tuple, list[dict]]] = []
    for item in items:
        day = item["trade_date"]
        key = (
            day.isocalendar()[:2] if period == "week" else (day.year, day.month)
        )
        if grouped and grouped[-1][0] == key:
            grouped[-1][1].append(item)
        else:
            grouped.append((key, [item]))

    result: list[dict] = []
    previous_close: float | None = None
    for _, group in grouped:
        closes = [row["close"] for row in group if row["close"] is not None]
        opens = [row["open"] for row in group if row["open"] is not None]
        highs = [row["high"] for row in group if row["high"] is not None]
        lows = [row["low"] for row in group if row["low"] is not None]
        volumes = [row["volume"] for row in group if row["volume"] is not None]
        amounts = [row["amount"] for row in group if row["amount"] is not None]
        close = closes[-1] if closes else None
        result.append(
            {
                "trade_date": group[-1]["trade_date"],
                "open": opens[0] if opens else None,
                "high": max(highs) if highs else None,
                "low": min(lows) if lows else None,
                "close": close,
                "pct_chg": (
                    None
                    if close is None or not previous_close
                    else (close / previous_close - 1) * 100
                ),
                "volume": sum(volumes) if volumes else None,
                "amount": sum(amounts) if amounts else None,
            }
        )
        previous_close = close
    return result


def _expected_latest(session: Session) -> date | None:
    """**应该已经拿到终值**的最近交易日 —— 用来判断缓存的 DDE 过期没有。

    不能简单用「今天或之前的最大交易日」：交易日历里今天也算交易日，而**今天要过了
    收盘时刻（15:05）才有终值**（见 `collect_dde.CLOSE_READY`）。盘中若拿今天当基准，
    每次请求都会判定「缓存过期」→ 打开个股页就白花一次 iFinD 配额。
    """
    days = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= date.today())
            .order_by(TradeCalendar.trade_date.desc())
            .limit(2)
        )
    )
    if not days:
        return None
    if days[0] == date.today() and datetime.now().time() < CLOSE_READY:
        return days[1] if len(days) > 1 else None
    return days[0]


def _read_dde(session: Session, code: str, days: int) -> list[StockDde]:
    """取最近 N 个交易日，返回**升序**（左旧右新，与 daily 接口一致）。"""
    rows = list(
        session.scalars(
            select(StockDde)
            .where(StockDde.code == code)
            .order_by(StockDde.trade_date.desc())
            .limit(days)
        )
    )
    return list(reversed(rows))


def _stock_dde(code: str, days: int, user_id: int) -> tuple[list[StockDde], str | None]:
    """读库；缓存过期就现取一次再读。返回（数据, 取不到的原因）。

    与 `api/sector.py` 的 `_board_members` 同一套：现取那次是**另一个事务**写的，
    用请求自带的 session 接着读看不到（SQLite WAL 下读事务是一个快照），
    所以每次都要重新开一个 session 去读。

    `user_id` 只用于**现取的限流**（见 `DDE_FETCH_LIMIT`）：限流键取用户而不是 IP，
    因为这里花的是「账号的配额」，跟请求从哪个 IP 来无关。
    """
    with session_scope() as reader:
        cached = _read_dde(reader, code, days)
        latest = _expected_latest(reader)

    # 已经有数据、且最新一行就是最近交易日 → 直接用缓存，不花配额
    if cached and latest and cached[-1].trade_date >= latest:
        return cached, None

    # 要真花钱了，先过限流闸（记一笔放在**取数之前**：无论取数成不成，配额都已经花了）
    key = f"dde:{user_id}"
    wait = _dde_fetch_limiter.retry_after(key)
    if wait is not None:
        logger.warning(
            "个股 %s 的 DDE 现取被限流（用户 %s 还要等 %.0f 秒）", code, user_id, wait
        )
        return cached, (
            f"取数过于频繁（每 {DDE_FETCH_WINDOW_SEC // 60} 分钟最多 {DDE_FETCH_LIMIT} 次），"
            f"请 {int(wait) + 1} 秒后再试"
        )
    _dde_fetch_limiter.fail(key)

    try:
        _, truncated = collect_stock_dde(code, days)
    except IfindError as exc:
        # 细节只进日志：`exc` 里可能带上游主机名 / 响应片段，不该回给前端
        logger.warning("个股 %s 的 DDE 取数失败：%s", code, exc)
        return cached, "iFinD 取数失败，请稍后再试"

    with session_scope() as reader:
        rows = _read_dde(reader, code, days)
    if rows:
        # 被来源截断时如实说明：图里的窗口比参数要的短，不说明就成了静默少数据
        note = (
            "来源单次最多返回 100 行，这次被截断了：图里只有最近 100 行"
            if truncated
            else None
        )
        return rows, note
    return cached, "iFinD 没返回这只票的 DDE（次新股或已退市的话可能是空的）"


@router.get("/{code}/dde", response_model=StockDdeOut)
def dde(
    code: str,
    days: int = Query(
        DDE_DAYS, ge=5, le=DDE_MAX_DAYS, description="返回最近 N 个交易日，按日期升序"
    ),
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> StockDdeOut:
    """个股的 **DDE 与主力净流入**（iFinD 口径，日频）。

    **这两个指标只有 iFinD 有**：akshare 的 `stock_fund_flow_*` 是同花顺公开页的
    资金流（只有即时 / 3日 / 5日 / 10日窗口、不给历史），口径与 DDE 也不是一回事。

    **数据是按需抓的**：库里没有最近交易日的数据时现取一次并落库（与板块成分股同一套），
    之后读库。每次现取花 1 次 iFinD 配额 —— 别在这个接口外面套全市场循环。

    ⚠️ **现取按用户限流**（`DDE_FETCH_LIMIT`，10 分钟 30 次）：空结果不落库，所以
    无效代码反复请求原本每次都能花掉 1 次配额。被限流时**不报错**，照常返回缓存 +
    一句说明（`note`）。
    """
    code = _code(code)
    rows, note = _stock_dde(code, days, user.id)
    return StockDdeOut(
        code=code,
        name=_resolve_name(session, code),
        days=len(rows),
        rows=[StockDdeRow.model_validate(row) for row in rows],
        note=note,
    )


@router.get("/{code}/news", response_model=StockNewsOut)
def news(
    code: str,
    limit: int = Query(20, ge=1, le=50, description="返回最近 N 条"),
    session: Session = Depends(get_db),
    user: AppUser = Depends(current_user),
) -> StockNewsOut:
    """个股新闻（**东财口径**，按发布时间倒序，只留讲这只票的那几条）。

    **缓存 + 限流**（2026-10-11 加，见 `NEWS_TTL_SEC`）：这条源没有配额、一次请求就够，
    但原来每次打开都现取 —— 那就等于把外部站点开放给人当爬虫。现在同一 `(代码, 条数)`
    在 10 分钟内直接吃缓存，未命中才现取，并按用户限流现取的次数。

    ⚠️ 取不到时把原因写进 `note`（页面照常显示其它内容），**不把它当 500**：
    个股页少一块新闻不该让整页报错。空列表与「源挂了」分得开 —— 前者是「真没搜到」。

    ⚠️ `hidden` 要带给前端：被筛掉的是「只在正文表格里提到代码」的名单类稿件，
    不**静默**丢 —— 界面上写「另隐去 N 条」（规则见 `sources/em_news.fetch_stock_news`）。
    """
    code = _code(code)
    key = (code, limit)
    cached = _news_cache.get(key)
    if cached is not None and datetime.now() - cached[0] < timedelta(seconds=NEWS_TTL_SEC):
        return cached[1]

    # 缓存没命中、要真去打外部源了 → 先过按用户限流（命中缓存不计数）
    user_key = f"news:{user.id}"
    wait = _news_fetch_limiter.retry_after(user_key)
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail=f"新闻刷新过于频繁，请 {int(wait) + 1} 秒后再试",
            headers={"Retry-After": str(int(wait) + 1)},
        )
    _news_fetch_limiter.fail(user_key)

    name = _resolve_name(session, code)
    try:
        found = fetch_stock_news(code, name=name, limit=limit)
    except Exception as exc:  # noqa: BLE001 - 源挂了不该让整页 500
        # 细节只进日志（可能带上游主机名 / 响应片段），对外给通用文案
        logger.warning("取 %s 的个股新闻失败：%s", code, exc)
        return StockNewsOut(
            code=code, name=name, rows=[], note="新闻源暂时没返回数据，请稍后再试"
        )
    note = None if found.items else "东财这条源没搜到这只票的新闻（小盘股 / 次新常见）"
    result = StockNewsOut(
        code=code,
        name=name,
        rows=[StockNewsRow.model_validate(row) for row in found.items],
        hidden=found.hidden,
        note=note,
    )
    _cache_news(key, result)
    return result


@router.get("/{code}/themes", response_model=StockThemes)
def themes(code: str, session: Session = Depends(get_db)) -> StockThemes:
    """该股**最近一次涨停时**所属的开盘红精选板块，附带板块最近一个交易日的涨跌幅。

    数据来自 `stock_concept`（涨停天梯的日度快照），这里取该股出现过的最近一天 ——
    所以它回答的是「这只票最近一次涨停是因为哪个板块」，**不是**「它属于哪些概念」。
    这个语义差别要记住：开盘红没有非涨停个股的板块归属接口（见设计文档 8.32.4），
    对从未涨停过的票这里就是空的，如实返回空列表，不编一个概念出来。
    """
    code = _code(code)
    name = _resolve_name(session, code)

    latest = session.scalar(
        select(func.max(StockConcept.trade_date)).where(StockConcept.code == code)
    )
    concepts: list[str] = []
    if latest is not None:
        concepts = sorted(
            session.scalars(
                select(StockConcept.concept).where(
                    StockConcept.code == code, StockConcept.trade_date == latest
                )
            )
        )

    # 板块表现：取板块表里精选口径的最新交易日，能对上的顺带给板块代码
    board_date = session.scalar(
        select(func.max(SectorDaily.trade_date)).where(
            SectorDaily.taxonomy == TAXONOMY_SELECTED
        )
    )
    boards: dict[str, tuple[str | None, float | None]] = {}
    if board_date is not None:
        boards = {
            row.name: (row.sector_code, row.pct_chg)
            for row in session.scalars(
                select(SectorDaily).where(
                    SectorDaily.trade_date == board_date,
                    SectorDaily.taxonomy == TAXONOMY_SELECTED,
                )
            )
            if row.name
        }

    items = [
        StockThemeItem(
            concept=concept,
            board_code=boards.get(concept, (None, None))[0],
            pct_chg=boards.get(concept, (None, None))[1],
        )
        for concept in concepts
    ]
    # 能对上板块的排前面，其次按板块涨跌幅降序 —— 一眼看到这只票最热的题材
    items.sort(key=lambda item: (item.board_code is None, -(item.pct_chg or 0)))

    return StockThemes(code=code, name=name, board_date=board_date, themes=items)


def _guard_quota() -> None:
    """手动同步的配额闸：配额紧张到 80% 就停手动同步。

    `PAUSE_KLINE`（已用 ≥80%）本来就要停形态选股的全市场日线更新，手动同步比它更
    非必需 —— 一起让路，把剩下的额度留给指数 / 情绪主线。日常采集仍会兜底刷新。
    """
    level = quota_level()
    if level >= QuotaLevel.PAUSE_KLINE:
        raise HTTPException(
            status_code=429,
            detail=f"iFinD 配额已用到 80%，暂停手动同步（{level_label(level)}）",
            headers={"Retry-After": "3600"},
        )


@router.post("/{code}/sync")
def sync(
    code: str,
    days: int = Query(STOCK_FULL_DAYS, ge=5, le=250, description="回看的交易日数"),
    user: AppUser = Depends(current_user),
) -> dict:
    """同步该股日线到本地缓存。首次打开个股页时调用。

    ⚠️ **按用户冷却 `SYNC_COOLDOWN_SEC` 秒**（见模块顶部的说明）：本站唯一的成本
    就是 iFinD 调用，一次 sync 真发十几次请求，不挡连点 / 脚本刷就会白烧配额。
    冷却**只在真去取过数时**才记账（成功或 `IfindError`）—— `CollectionBusy`（采集
    正忙）根本没发起取数，不占冷却，前端可立刻重试（与自选同步同一口径，2026-10-11 统一）。
    """
    _guard_quota()

    wait = _sync_cooldown.remaining(f"user:{user.id}")
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail=f"刚同步过，请 {int(wait) + 1} 秒后再试",
            headers={"Retry-After": str(int(wait) + 1)},
        )
    try:
        with collect_guard("个股同步"):
            written = DailyCollector().sync_stock(_code(code), days=days)
    except CollectionBusy as exc:
        # 采集正忙：没发起取数，不占冷却
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IfindError as exc:
        # 真去取过数了（配额已经花掉）→ 占用冷却；对外只说人话，细节进日志
        logger.warning("个股 %s 同步失败（用户 %s）：%s", code, user.id, exc)
        _sync_cooldown.touch(f"user:{user.id}")
        raise HTTPException(status_code=400, detail="同步失败，请稍后再试") from exc
    _sync_cooldown.touch(f"user:{user.id}")
    return {"rows": written}
