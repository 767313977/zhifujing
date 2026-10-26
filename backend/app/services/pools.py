"""选股池的两块数据：RPS 相对强度榜、首板一进二候选。

两个都是**盘后候选池**，不是买卖指令：本站没有盘中数据，打板要看次日承接，
这里只负责把「值得盯的几十只」按可复现的条件筛出来。

口径说明
--------
- RPS：读 `jobs/scan_rps` 落库的当日横截面分位，不做二次计算；
- 首板一进二：条件全部可调（见 `first_board` 的参数），默认值是一套保守参数，
  不是「最优参数」—— 任何参数都应该用自己的库回测过再用。
"""

import logging
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    LimitPool,
    PatternHit,
    RpsDaily,
    SectorDaily,
    SectorMember,
    StockBasic,
    StockConcept,
    StockDaily,
    TradeCalendar,
)
from app.services import (
    factor_score as factor_scores,
    limit_rules,
    pattern_track,
    rps,
    sector_rps,
    trend,
)
from app.services.patterns import PATTERNS, Bars, build_bars, is_st, usable_bar
from app.sources.base import chunked

logger = logging.getLogger(__name__)

#: 单位换算：库里金额一律是**元**，接口参数用「亿」更好读
YI = 1e8

#: 北交所代码前缀（4/8/92）。30% 涨跌停 + 流动性差异，和沪深打板不是一套玩法
_BJ_PREFIXES = ("4", "8", "92")


@dataclass(frozen=True, slots=True)
class RpsRow:
    code: str
    name: str | None
    close: float | None
    pct_chg: float | None
    ret50: float | None
    ret120: float | None
    ret250: float | None
    rps50: float | None
    rps120: float | None
    rps250: float | None
    #: 当天命中的**突破类**形态中文名（只列 `BREAKOUT_KEYS` 里的）
    hits: list[str] = field(default_factory=list)
    #: `一线红` / `三线红` / `掉队`
    tags: list[str] = field(default_factory=list)
    #: 收盘距近 250 日最高收盘的百分比（−8.5 表示比高点低 8.5%）
    off_high_pct: float | None = None
    #: 行业末段（`StockBasic.industry` 按 `-` 拆的最后一段）
    industry: str | None = None
    #: 量价因子分（0~100，仅 `order="factor"` 时计算）
    factor_score: float | None = None


@dataclass(frozen=True, slots=True)
class RpsBoard:
    trade_date: date | None
    rows: list[RpsRow]
    #: 返回行里各**一级行业**的只数，取前 5
    industry_top: list[tuple[str, int]] = field(default_factory=list)


#: 「双高」档：RPS50 ≥ 90 且 RPS120 ≥ 85 —— 2026-10-19 实验里唯一站得住的组合，
#: 而且要**叠在突破类形态上**（单独用 RPS 没有超额）。来源见
#: `docs/回测_RPS与趋势模板_20261024.md` 与 `services/patterns.py` 的注释表。
RPS_DUAL_50 = 90.0
RPS_DUAL_120 = 85.0
#: 「一线红」：三档里任意一档 ≥ 这个值
RPS_LINE_RED = 90.0

#: 突破类形态（2026-10-19 实验：叠 RPS 有效的那批）。**别往里加回踩类** ——
#: 回踩白线B1 × rps50_90 是 −8.15pp。
BREAKOUT_KEYS = (
    "new_high",
    "three_white_soldiers",
    "ma_golden_cross",
    "turtle_breakout",
    "volume_surge",
)

_PATTERN_NAMES = {pattern.key: pattern.name for pattern in PATTERNS}

#: 打开「突破形态 / 趋势模板」过滤时最多评估多少只：列表本身按强度排序，
#: 取前 3 倍 limit（至少 300）足够，否则 `mode=all` 要为一整池 5000 只算 250 根均线
_FILTER_SCAN_CAP = 300


def rps_board(
    session: Session,
    trade_date: date | None = None,
    min_rps: float = 0,
    limit: int = 100,
    mode: str = "all",
    breakout_only: bool = False,
    trend_template: bool = False,
    order: str = "strength",
    trend_cache: dict[str, Bars] | None = None,
) -> RpsBoard:
    """RPS 榜。

    `mode`（2026-10-24 加）：
    - `all`（**默认**）：不额外卡分位（受 `min_rps` 约束），按 RPS250 降序；
    - `dual`：RPS50 ≥ 90 且 RPS120 ≥ 85，按两者之和降序；
    - `line_red`：三档里任一 ≥ 90（陶博士「一线红」），按三档最大值降序。

    ⚠️ **为什么默认不是 `dual`**：口径对齐后（选股池-8）重跑，「双高 × 突破」只在
    最近一年（255 个交易日）有效（10 日超额 +3.1~4.1%），**5 年长历史全部为负**
    （−0.4~−1.4%，超额差 −0.6~−1.6pp）。方向不一致 → 不拿它当默认档，只作参考视图。
    数字见 `docs/回测_RPS与趋势模板_20261024.md`。

    `breakout_only`：只留当天命中了 `BREAKOUT_KEYS` 里任一形态的票；
    `trend_template`：只留满足 Minervini 趋势模板的票（判据与回测共用 `services.trend`）。

    `order="factor"`（2026-10-24 改）：先在 `mode` 下筛出**全部**候选，按量价因子分
    排序，再取前 `limit` 只 —— 选出来的票和「强度前 N」不同，验证面板里两档才看得出
    差异。代价是每次要算全池（约 5000 只）的 20 日窗口：单日约 0.7s，`rps_track`
    20 天首次约 15s（接口有 10 分钟缓存）。

    ⚠️ 两个开关都是在**排序之后**按序评估最多 `_FILTER_SCAN_CAP` 只 —— 榜单是
    「最强的一批」，翻到第 300 名之后还满足突破 / 趋势的票本来也不该按强度混进来。
    `order="factor"` 时不受这个上限约束（要全池才能挑出因子分最高的）；它与
    `trend_template` 同时开时，模板仍要先对**全池**跑一遍（约 23 秒），再按因子分取前 N
    —— 换成「先排序再过滤」会得到 0 只（两种逻辑的票不重合，见下面的注释）。
    """
    target = trade_date or _latest_rps_day(session)
    if target is None:
        return RpsBoard(trade_date=None, rows=[])

    statement = (
        select(
            RpsDaily.code,
            StockBasic.name,
            StockBasic.industry,
            StockDaily.close,
            StockDaily.pct_chg,
            RpsDaily.ret50,
            RpsDaily.ret120,
            RpsDaily.ret250,
            RpsDaily.rps50,
            RpsDaily.rps120,
            RpsDaily.rps250,
        )
        .join(
            StockDaily,
            (StockDaily.trade_date == RpsDaily.trade_date) & (StockDaily.code == RpsDaily.code),
        )
        .outerjoin(StockBasic, StockBasic.code == RpsDaily.code)
        .where(RpsDaily.trade_date == target)
    )

    if mode == "dual":
        statement = statement.where(
            RpsDaily.rps50.is_not(None),
            RpsDaily.rps50 >= RPS_DUAL_50,
            RpsDaily.rps120.is_not(None),
            RpsDaily.rps120 >= RPS_DUAL_120,
        ).order_by((RpsDaily.rps50 + RpsDaily.rps120).desc())
    elif mode == "line_red":
        statement = statement.where(
            or_(
                RpsDaily.rps50 >= RPS_LINE_RED,
                RpsDaily.rps120 >= RPS_LINE_RED,
                RpsDaily.rps250 >= RPS_LINE_RED,
            )
        ).order_by(
            func.max(RpsDaily.rps50, RpsDaily.rps120, RpsDaily.rps250).desc()
        )
    elif mode == "all":
        # 现状口径：两档都要有值、都 ≥ min_rps，按 250 日分位降序
        statement = statement.where(
            RpsDaily.rps250.is_not(None),
            RpsDaily.rps250 >= min_rps,
            RpsDaily.rps120.is_not(None),
            RpsDaily.rps120 >= min_rps,
        ).order_by(RpsDaily.rps250.desc())
    else:
        raise ValueError(f"未知的 RPS 模式：{mode}")

    # `order="factor"` 要全池才能挑出「因子分最高」的票；其余情况按 limit 截断。
    if order == "factor":
        cap = None
    else:
        cap = (
            max(limit * 3, _FILTER_SCAN_CAP)
            if (breakout_only or trend_template)
            else limit
        )
    records = session.execute(statement.limit(cap)).all()
    if not records:
        return RpsBoard(trade_date=target, rows=[])

    codes = [row[0] for row in records]
    hits = _breakout_hits(session, target, codes)

    rows: list[RpsRow] = []
    for record in records:
        (
            code,
            name,
            industry,
            close,
            pct_chg,
            ret50,
            ret120,
            ret250,
            rps50,
            rps120,
            rps250,
        ) = record
        if breakout_only and not hits.get(code):
            continue
        rows.append(
            RpsRow(
                code=code,
                name=name,
                close=close,
                pct_chg=pct_chg,
                ret50=ret50,
                ret120=ret120,
                ret250=ret250,
                rps50=rps50,
                rps120=rps120,
                rps250=rps250,
                hits=hits.get(code, []),
                tags=_rps_tags(rps50, rps120, rps250),
                industry=industry.split("-")[-1] if industry else None,
            )
        )
        if len(rows) >= limit and not trend_template and order != "factor":
            break

    if trend_template:
        # ⚠️ 趋势模板是**过滤条件**，必须先于「按因子分取前 N」：全池里满足模板的 72 只，
        # 因子分没有一只进前 300 —— 反过来先排序再过滤会直接得到 0 只。
        rows = _filter_trend(session, target, rows, trend_cache)
        if order != "factor":
            rows = rows[:limit]

    if order == "factor" and rows:
        computed = factor_scores.scores(session, [row.code for row in rows], target)
        rows = [
            replace(
                row,
                factor_score=computed[row.code].score if row.code in computed else None,
            )
            for row in rows
        ]
        rows.sort(key=lambda item: (-(item.factor_score or 0.0), item.code))
        rows = rows[:limit]

    if rows:
        off_high = _off_high_pct(session, target, [row.code for row in rows])
        rows = [
            replace(row, off_high_pct=off_high.get(row.code)) for row in rows
        ]

    return RpsBoard(
        trade_date=target,
        rows=rows,
        industry_top=_industry_top(session, [row.code for row in rows]),
    )


def _latest_rps_day(session: Session) -> date | None:
    """最近一个**同时有 RPS 和日线**的交易日。

    为什么不能直接取 `max(rps_daily.trade_date)`：日线会被采集任务 `prune()`（只留
    400 天），而 `rps_daily` 不删。老库上就会出现「RPS 有 10-21、日线只剩到 10-09」
    这种组合 —— 直接取最新的话，榜单 join 不到行情，页面上是空表而不是报错。
    """
    days = list(
        session.scalars(
            select(RpsDaily.trade_date)
            .group_by(RpsDaily.trade_date)
            .order_by(RpsDaily.trade_date.desc())
            .limit(5)
        )
    )
    for day in days:
        has_quote = session.scalar(
            select(StockDaily.code).where(StockDaily.trade_date == day).limit(1)
        )
        if has_quote is not None:
            return day
    return days[0] if days else None


def _breakout_hits(session: Session, day: date, codes: list[str]) -> dict[str, list[str]]:
    """当天命中的突破类形态中文名（按 `BREAKOUT_KEYS` 过滤）。"""
    if not codes:
        return {}
    hits: dict[str, list[str]] = defaultdict(list)
    for code, key in session.execute(
        select(PatternHit.code, PatternHit.pattern).where(
            PatternHit.trade_date == day,
            PatternHit.pattern.in_(BREAKOUT_KEYS),
            PatternHit.code.in_(codes),
        )
    ):
        hits[code].append(_PATTERN_NAMES.get(key, key))
    return dict(hits)


def _rps_tags(rps50, rps120, rps250) -> list[str]:
    """`一线红` / `三线红` / `掉队`。

    - 一线红：三档里至少一档 ≥ 90；
    - 三线红：三档都 ≥ 90；
    - 掉队：RPS250 ≥ 90 但 RPS120 < 80 —— 过去一年涨得多、近半年已经掉头，
      正是旧榜（只按 250 排）排在最前面的那批。
    """
    values = [value for value in (rps50, rps120, rps250) if value is not None]
    tags: list[str] = []
    if values and len(values) == 3 and all(value >= RPS_LINE_RED for value in values):
        tags.append("三线红")
    elif any(value >= RPS_LINE_RED for value in values):
        tags.append("一线红")
    if rps250 is not None and rps120 is not None and rps250 >= RPS_LINE_RED and rps120 < 80:
        tags.append("掉队")
    return tags


def _slice_bars(bars: Bars, day: date, floor: date) -> Bars | None:
    """取「`floor` ~ `day` 之间、截至 `day` 的最后 `TREND_LOOKBACK` 根」。

    `floor` 必须与逐日现读时的窗口起点一致（`day` 往前 `TREND_LOOKBACK+10` 个交易日）——
    否则长期停牌的票会因为在更宽的窗口里凑够 250 根而被误放进来（实测差 1 只）。
    """
    low = bisect_left(bars.dates, floor)
    high = bisect_right(bars.dates, day)
    if high - low < trend.TREND_LOOKBACK:
        return None
    start = high - trend.TREND_LOOKBACK
    return Bars(
        dates=bars.dates[start:high],
        open=bars.open[start:high],
        high=bars.high[start:high],
        low=bars.low[start:high],
        close=bars.close[start:high],
        volume=bars.volume[start:high],
        amount=bars.amount[start:high],
        pct_chg=bars.pct_chg[start:high],
        code=bars.code,
    )


def _window_floor(session: Session, day: date) -> date | None:
    """逐日现读时用的窗口起点（与 `_filter_trend` 的非缓存分支同一个表达式）。"""
    days = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= day)
            .order_by(TradeCalendar.trade_date.desc())
            .limit(trend.TREND_LOOKBACK + 10)
        )
    )
    return min(days) if days else None


def _build_trend_bars(session: Session, days: list[date]) -> dict[str, Bars]:
    """给一批信号日准备「每只票一份」的整段前复权序列（趋势模板专用）。

    为什么要预加载：`_filter_trend` 每次要读 `TREND_LOOKBACK+10` 个交易日 × 全池
    （约 5000 只）的日线，单次约 11 秒；`rps/track` 有 20 个信号日，逐日跑就是 220 秒。
    这里只读一次**并集窗口**（信号日跨度 + 260 天），每只票 `build_bars` 一次，之后按
    信号日切片判定。

    结果与逐日构建**一致**：前复权净值都是 `pct_chg` 复利，子窗口只是整体乘了一个常数
    （等比缩放），而趋势模板全部是均线与高低点的**比值**判断，缩放不影响。
    """
    if not days:
        return {}
    last = days[-1]
    window = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= last)
            .order_by(TradeCalendar.trade_date.desc())
            .limit(trend.TREND_LOOKBACK + 10 + len(days) + 5)
        )
    )
    if not window:
        return {}
    start = min(window)

    codes = list(
        session.scalars(select(RpsDaily.code).where(RpsDaily.trade_date.in_(days)).distinct())
    )
    grouped: dict[str, list[dict]] = defaultdict(list)
    for batch in chunked(codes, 900):
        for code, trade_date, open_, high, low, close, volume, amount, pct in session.execute(
            select(
                StockDaily.code,
                StockDaily.trade_date,
                StockDaily.open,
                StockDaily.high,
                StockDaily.low,
                StockDaily.close,
                StockDaily.volume,
                StockDaily.amount,
                StockDaily.pct_chg,
            )
            .where(
                StockDaily.code.in_(batch),
                StockDaily.trade_date >= start,
                StockDaily.trade_date <= last,
            )
            .order_by(StockDaily.code, StockDaily.trade_date)
        ):
            item = {
                "date": trade_date,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
                "pct_chg": pct,
                "name": None,
            }
            if usable_bar(item):
                grouped[code].append(item)

    return {
        code: build_bars(items, code)
        for code, items in grouped.items()
        if len(items) >= trend.TREND_LOOKBACK
    }


def _filter_trend(
    session: Session,
    day: date,
    rows: list[RpsRow],
    cache: dict[str, Bars] | None = None,
) -> list[RpsRow]:
    """只留满足 Minervini 趋势模板的票（判据与回测共用 `services.trend`）。

    `cache` 给了就用预算好的一份序列切片（`rps/track` 的多日场景），否则现读日线。
    """
    if not rows:
        return rows
    if cache is not None:
        floor = _window_floor(session, day)
        if floor is None:
            return []
        kept: list[RpsRow] = []
        for row in rows:
            bars = cache.get(row.code)
            if bars is None:
                continue
            sliced = _slice_bars(bars, day, floor)
            if sliced is None:
                continue
            if trend.passes(sliced, len(sliced) - 1, row.rps250):
                kept.append(row)
        return kept
    codes = [row.code for row in rows]
    # 只读趋势模板需要的窗口：`trend.passes` 要 `TREND_LOOKBACK` 根可用日线，多留 10 天
    # 缓冲（停牌残行会被 `usable_bar` 剔掉）。不设下限时会把库里保留的 400 天全读出来
    # —— 全池（约 5000 只）实测 26.6 秒，而 `rps/track` 会乘 20 个循环，必然超时。
    days = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= day)
            .order_by(TradeCalendar.trade_date.desc())
            .limit(trend.TREND_LOOKBACK + 10)
        )
    )
    if not days:
        return []
    start = min(days)

    records: dict[str, list[dict]] = defaultdict(list)
    # 全池 5000 只时 IN 参数过多，按 900 一批（与 `api/patterns`、`stock_phase` 同一约定）
    for batch in chunked(codes, 900):
        for code, trade_date, open_, high, low, close, volume, amount, pct in session.execute(
            select(
                StockDaily.code,
                StockDaily.trade_date,
                StockDaily.open,
                StockDaily.high,
                StockDaily.low,
                StockDaily.close,
                StockDaily.volume,
                StockDaily.amount,
                StockDaily.pct_chg,
            )
            .where(
                StockDaily.code.in_(batch),
                StockDaily.trade_date >= start,
                StockDaily.trade_date <= day,
            )
            .order_by(StockDaily.code, StockDaily.trade_date)
        ):
            item = {
                "date": trade_date,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
                "pct_chg": pct,
                "name": None,
            }
            # 停牌残行（只有收盘价）不能当一根 K 线，与扫描 / 回测同口径
            if usable_bar(item):
                records[code].append(item)
    kept: list[RpsRow] = []
    for row in rows:
        items = records.get(row.code) or []
        if len(items) < trend.TREND_LOOKBACK:
            continue
        bars: Bars = build_bars(items, row.code)
        if trend.passes(bars, len(bars) - 1, row.rps250):
            kept.append(row)
    return kept


def _off_high_pct(session: Session, day: date, codes: list[str]) -> dict[str, float]:
    """收盘距近 250 个交易日最高收盘的百分比（用 `pct_chg` 复利，避开除权）。

    做法：把窗口内的日涨跌幅复利成一条以起点为 1 的净值曲线，终点归一成今天 ——
    `off = 今天 / 曲线最高 − 1`，所以永远 ≤ 0。
    """
    days = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= day)
            .order_by(TradeCalendar.trade_date.desc())
            .limit(250)
        )
    )
    if not days:
        return {}
    days.sort()
    series: dict[str, list[float]] = defaultdict(list)
    for code, pct in session.execute(
        select(StockDaily.code, StockDaily.pct_chg)
        .where(
            StockDaily.trade_date.in_(days),
            StockDaily.code.in_(codes),
            StockDaily.pct_chg.is_not(None),
        )
        .order_by(StockDaily.code, StockDaily.trade_date)
    ):
        series[code].append(float(pct))
    out: dict[str, float] = {}
    for code, values in series.items():
        if len(values) < 2:
            continue
        equity = [1.0]
        for pct in values:
            equity.append(equity[-1] * (1 + pct / 100))
        peak = max(equity)
        if peak > 0:
            out[code] = round((equity[-1] / peak - 1) * 100, 2)
    return out


def _industry_top(session: Session, codes: list[str]) -> list[tuple[str, int]]:
    """返回行里各**一级行业**的只数，取前 5。

    用一级（`A-B-C` 的第一段）而不是末段：末段太碎（"其他专用设备" 这种），
    看不出行业抱团。
    """
    if not codes:
        return []
    industries = session.execute(
        select(StockBasic.industry).where(StockBasic.code.in_(codes))
    ).scalars()
    counter: Counter[str] = Counter()
    for industry in industries:
        if industry:
            counter[industry.split("-")[0]] += 1
    return counter.most_common(5)


@dataclass(frozen=True, slots=True)
class FirstBoardRow:
    code: str
    name: str | None
    pct_chg: float | None
    price: float | None
    first_seal_time: str | None
    last_seal_time: str | None
    open_times: int | None
    float_mv: float | None
    amount: float | None
    turnover: float | None
    seal_amount: float | None
    #: 封单额 / 流通市值（%），衡量封板质量
    seal_ratio: float | None
    #: 封单额 / 当日成交额（%），衡量「有承接的封板」—— 成交额很小的板，
    #: 封单占比再高也可能是没量的一字板
    seal_to_amount: float | None
    industry: str | None
    #: 当日涨停股里，与它同属一个精选板块最多有几只（含自己）
    theme: str | None
    theme_count: int
    #: 当天是不是真首板：D 之前 10 个交易日**一次涨停都没有**（`consecutive == 1`
    #: 只保证今天是一板，前几天刚涨停过、断一天再涨停的票也会被算进来）
    recent_limit_ups: int = 0
    #: 当天的封板形态：`一字` / `T字` / None
    board_shape: str | None = None
    #: 它在 `theme` 里按首封时间的排名（1 = 该题材里最早封板），`theme_count` 是分母
    theme_rank: int | None = None
    #: 第二热的题材，悬停时显示
    theme2: str | None = None
    theme2_count: int = 0
    #: 综合排序分（0~100）：题材家数 / 封单比 / 首封时间的**当日候选内分位**加权
    score: float = 0.0
    #: 量价因子分（0~100，仅 `order="factor"` 时计算）：rev20 / illiq20 / amp20 的池内分位加权
    factor_score: float | None = None


@dataclass(frozen=True, slots=True)
class FirstBoardBoard:
    trade_date: date | None
    conditions: dict = field(default_factory=dict)
    rows: list[FirstBoardRow] = field(default_factory=list)


#: 综合排序的权重（**当日候选内分位**加权）。只是起手值 —— 用页面上的
#: 「次日验证」面板对比旧排序 / 新排序的前 5 只表现之后再调。
SCORE_W_THEME = 0.4
SCORE_W_SEAL = 0.4
SCORE_W_TIME = 0.2

#: 「真首板」的回看窗口（交易日）：D 之前这么多天里一次涨停都没有才算
TRUE_FIRST_BOARD_LOOKBACK = 10


def _board_shape(
    open_: float | None, high: float | None, low: float | None, close: float | None
) -> str | None:
    """涨停当天的封板形态：`一字` / `T字` / None。

    - 一字：开 = 高 = 低 = 收（竞价就封死，全天没有波动）
    - T字：开 = 高，但盘中打开过（低 < 开）

    三个价格都来自同一行不复权日线，直接比相等即可（同一数据源、同一天）。
    """
    if None in (open_, high, low, close):
        return None
    if open_ == high == low == close:
        return "一字"
    if open_ == high and low is not None and low < open_:
        return "T字"
    return None


def first_board(
    session: Session,
    trade_date: date | None = None,
    before: str = "140000",
    min_float_mv: float = 20,
    max_float_mv: float = 300,
    min_amount: float = 3,
    min_themes: int = 3,
    min_seal_ratio: float = 0,
    only_true_first_board: bool = False,
    order: str = "board",
    limit: int = 50,
) -> FirstBoardBoard:
    """首板一进二候选。

    条件（全部可调，金额单位**亿**）：
    - `before`：首封时间早于它（`HHMMSS`，默认 14:00 —— 只排掉尾盘偷袭，
      10:30 那种卡法实测会把池子清空：2026-10-23 全天只有 3 只在 10:30 前封板）
    - `min_float_mv` / `max_float_mv`：流通市值区间
    - `min_amount`：当日成交额下限
    - `min_themes`：当日同题材涨停家数下限（用 `stock_concept` 的精选板块口径）
    - `min_seal_ratio`：封单额 / 流通市值 的下限（%），默认 0 = 不筛
    - `only_true_first_board`：只看**真首板**（D 之前 10 个交易日没涨停过），默认关

    ⚠️ 只筛「首板」（`consecutive == 1`）且**没炸过板**（`open_times == 0`）的票。
    `consecutive == 1` 不等于真首板 —— 前几天涨停过、断一天再涨停的票也算 1 板，
    所以另给 `recent_limit_ups`（近 10 个交易日涨停次数）让人自己决定要不要剔。

    排序（2026-10-24 改）：不再是「题材家数优先」—— 那样会把封单 0.02%（基本没封住）
    的票排到封单 1.35% 的前面。现在是三个**当日候选内分位**的加权：
    题材家数 0.4 + 封单比 0.4 + 首封早晚 0.2（见 `SCORE_W_*`）。
    """
    target = trade_date or session.scalar(
        select(func.max(LimitPool.trade_date)).where(LimitPool.pool_type == "up")
    )
    if target is None:
        return FirstBoardBoard(trade_date=None, conditions={})

    rows = list(
        session.scalars(
            select(LimitPool)
            .where(
                LimitPool.trade_date == target,
                LimitPool.pool_type == "up",
                LimitPool.consecutive == 1,
                # 没炸过板。2026-10-24 核查过：库里涨停池 18003 行（255 个交易日，
                # 2025-09-08 ~ 2026-09-24，本地库能查到的全量）`open_times` **没有空值**，
                # 所以不必写成 `== 0 OR IS NULL`；将来换数据源要重跑一遍这条 SQL。
                LimitPool.open_times == 0,
                LimitPool.first_seal_time.is_not(None),
                LimitPool.first_seal_time <= before,
                LimitPool.float_mv >= min_float_mv * YI,
                LimitPool.float_mv <= max_float_mv * YI,
                LimitPool.amount >= min_amount * YI,
            )
            .order_by(LimitPool.first_seal_time)
        )
    )

    # 题材：当日每只涨停股的精选板块 → 板块内涨停家数、板块成员（算题材内首封排名用）
    concepts: dict[str, list[str]] = {}
    members: dict[str, list[str]] = defaultdict(list)
    counter: Counter[str] = Counter()
    for code, concept in session.execute(
        select(StockConcept.code, StockConcept.concept).where(
            StockConcept.trade_date == target
        )
    ):
        concepts.setdefault(code, []).append(concept)
        members[concept].append(code)
        counter[concept] += 1

    # 当日**全部**涨停股**的首封时间（题材内排名用；候选只是其中一小部分）
    seal_times = dict(
        session.execute(
            select(LimitPool.code, LimitPool.first_seal_time).where(
                LimitPool.trade_date == target, LimitPool.pool_type == "up"
            )
        ).all()
    )
    theme_rank: dict[str, dict[str, int]] = {}
    for concept, codes_in in members.items():
        ordered = sorted(codes_in, key=lambda item: seal_times.get(item) or "235959")
        theme_rank[concept] = {
            code: index + 1 for index, code in enumerate(ordered)
        }

    # 近 10 个交易日涨停次数（真首板 = 0）。按交易日历取，停牌日不算
    prior_days = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date < target)
            .order_by(TradeCalendar.trade_date.desc())
            .limit(TRUE_FIRST_BOARD_LOOKBACK)
        )
    )
    recent_limit_ups: dict[str, int] = {}
    if prior_days:
        recent_limit_ups = dict(
            session.execute(
                select(LimitPool.code, func.count())
                .where(
                    LimitPool.trade_date.in_(prior_days),
                    LimitPool.pool_type == "up",
                )
                .group_by(LimitPool.code)
            ).all()
        )

    names = dict(
        session.execute(
            select(StockBasic.code, StockBasic.name).where(
                StockBasic.code.in_([row.code for row in rows])
            )
        ).all()
    )

    shapes = {
        code: _board_shape(open_, high, low, close)
        for code, open_, high, low, close in session.execute(
            select(
                StockDaily.code,
                StockDaily.open,
                StockDaily.high,
                StockDaily.low,
                StockDaily.close,
            ).where(
                StockDaily.trade_date == target,
                StockDaily.code.in_([row.code for row in rows]),
            )
        )
    }

    picked: list[FirstBoardRow] = []
    for row in rows:
        name = names.get(row.code) or row.name
        if is_st(name):
            continue
        # 北交所不是这套玩法：30% 涨跌停、流动性差，默认排除
        if row.code.startswith(_BJ_PREFIXES):
            continue
        stock_concepts = concepts.get(row.code) or []
        theme = max(stock_concepts, key=lambda item: counter[item], default=None)
        theme_count = counter[theme] if theme else 0
        if theme_count < min_themes:
            continue
        # 第二热的题材：同一天里另一条线索，悬停时给出来
        others = sorted(
            (item for item in stock_concepts if item != theme),
            key=lambda item: counter[item],
            reverse=True,
        )
        theme2 = others[0] if others else None
        past_ups = int(recent_limit_ups.get(row.code, 0))
        if only_true_first_board and past_ups:
            continue
        seal_ratio = (
            row.seal_amount / row.float_mv * 100
            if row.seal_amount and row.float_mv
            else None
        )
        if min_seal_ratio and (seal_ratio is None or seal_ratio < min_seal_ratio):
            continue
        seal_to_amount = (
            row.seal_amount / row.amount * 100
            if row.seal_amount and row.amount
            else None
        )
        picked.append(
            FirstBoardRow(
                code=row.code,
                name=name,
                pct_chg=row.pct_chg,
                price=row.price,
                first_seal_time=row.first_seal_time,
                last_seal_time=row.last_seal_time,
                open_times=row.open_times,
                float_mv=row.float_mv,
                amount=row.amount,
                turnover=row.turnover,
                seal_amount=row.seal_amount,
                seal_ratio=round(seal_ratio, 2) if seal_ratio is not None else None,
                seal_to_amount=(
                    round(seal_to_amount, 1) if seal_to_amount is not None else None
                ),
                industry=row.industry,
                theme=theme,
                theme_count=theme_count,
                recent_limit_ups=past_ups,
                board_shape=shapes.get(row.code),
                theme_rank=(
                    theme_rank.get(theme, {}).get(row.code) if theme else None
                ),
                theme2=theme2,
                theme2_count=counter[theme2] if theme2 else 0,
            )
        )

    # 综合分：三个**当日候选内**分位加权（理由见函数说明）。
    # 首封时间越早越好 → 取负号再算分位；封单比缺值按 0 算（等于排在最后）。
    theme_pct = rps.percentiles([float(item.theme_count) for item in picked])
    seal_pct = rps.percentiles([item.seal_ratio or 0.0 for item in picked])
    time_pct = rps.percentiles([-float(item.first_seal_time or "235959") for item in picked])
    scored = [
        replace(
            item,
            score=round(
                theme_pct[index] * SCORE_W_THEME
                + seal_pct[index] * SCORE_W_SEAL
                + time_pct[index] * SCORE_W_TIME,
                1,
            ),
        )
        for index, item in enumerate(picked)
    ]
    scored.sort(key=lambda item: (-item.score, -(item.seal_ratio or 0), item.code))

    # 因子排序（2026-10-24 加）：**不改变入选集合，只改顺序** ——
    # 三个量价因子在「今天这批候选」里各算分位再加权（见 `factor_score`）。
    if order == "factor":
        computed = factor_scores.scores(session, [row.code for row in scored], target)
        scored = [
            replace(row, factor_score=computed[row.code].score if row.code in computed else None)
            for row in scored
        ]
        scored.sort(key=lambda item: (-(item.factor_score or 0.0), -(item.score or 0.0)))

    return FirstBoardBoard(
        trade_date=target,
        conditions={
            "before": before,
            "min_float_mv": min_float_mv,
            "max_float_mv": max_float_mv,
            "min_amount": min_amount,
            "min_themes": min_themes,
            "min_seal_ratio": min_seal_ratio,
            "only_true_first_board": only_true_first_board,
            "order": order,
        },
        rows=scored[:limit],
    )


# ---------------------------------------------------------------- 次日验证
#
# 页面上的阈值（首封时间 / 市值 / 题材家数）都没有「标准答案」，必须用库里的数据
# 验证过再用。这两个函数回答「按当前条件选出来的票，第二天到底怎么样」——
# 参数一改，页面上的汇总行立刻跟着变，这是定阈值的唯一依据。
#
# 口径（写死了，别在页面里另算）：
# - **买入 = T+1 开盘**，所以开盘溢价与收益都从 T+1 起算；
# - T+1 开盘就一字涨停（晋级且开=高=低=收）的**买不到**，单独计数、不进收益均值；
# - `open_premium` 的分母用 `close / (1 + pct_chg/100)` 反推的**昨收**（交易所除权
#   参考价），直接用 T 日收盘会在除权日算出一个假的跳空；
# - T+1 的日线还没采到时，这一天记 `pending`，**不计入统计**。

#: 「大面」的门槛：T+1 收盘 ≤ 这个数（%）
_BIG_LOSS_PCT = -5.0


@dataclass(frozen=True, slots=True)
class TrackDay:
    trade_date: date
    next_date: date | None
    candidates: int
    promoted: int
    #: 开盘溢价 / 收盘收益均值（%）。只统计「买得到」的样本
    open_premium_mean: float | None
    close_ret_mean: float | None
    #: 一字涨停买不进 / 次日没有日线（停牌或未采）的只数
    unbuyable: int
    no_quote: int
    #: `ok` = 已验证；`pending` = 次日日线还没到
    status: str


@dataclass(frozen=True, slots=True)
class FirstBoardTrack:
    trade_date: date | None
    conditions: dict = field(default_factory=dict)
    days: list[TrackDay] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


def _mean_median(values: list[float]) -> tuple[float | None, float | None]:
    """`(均值, 中位)`，单位 %，两位小数。空样本给 (None, None)。"""
    if not values:
        return None, None
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2
    )
    return round(sum(ordered) / len(ordered), 2), round(median, 2)


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator * 100, 1) if denominator else None


def first_board_track(
    session: Session,
    days: int = 20,
    before: str = "140000",
    min_float_mv: float = 20,
    max_float_mv: float = 300,
    min_amount: float = 3,
    min_themes: int = 3,
    min_seal_ratio: float = 0,
    only_true_first_board: bool = False,
    order: str = "board",
    top: int = 0,
) -> FirstBoardTrack:
    """近 `days` 个有涨停池的交易日，按当前条件选出来的首板次日表现。

    条件参数与 `first_board` 完全一致（内部就是调它），所以页面上的候选和这里
    统计的样本是同一批票 —— 参数改了，两边一起变。

    `order` / `top`（2026-10-24 加）：只统计**排序后的前 `top` 只**，`top=0` 表示
    全部候选（默认，与加参数前完全一致）。`top>0` 时 `order` 才有意义 —— 全量统计
    下顺序不影响任何汇总数字；`order="factor"` 才能验证「按量价因子分挑前 N 只」
    到底比「按题材/封单综合分挑」好多少。
    """
    limit_days = list(
        session.scalars(
            select(LimitPool.trade_date)
            .where(LimitPool.pool_type == "up")
            .group_by(LimitPool.trade_date)
            .order_by(LimitPool.trade_date.desc())
            .limit(days)
        )
    )
    conditions = {
        "before": before,
        "min_float_mv": min_float_mv,
        "max_float_mv": max_float_mv,
        "min_amount": min_amount,
        "min_themes": min_themes,
        "min_seal_ratio": min_seal_ratio,
        "only_true_first_board": only_true_first_board,
        "order": order,
        "top": top,
    }
    if not limit_days:
        return FirstBoardTrack(trade_date=None, conditions=conditions)

    dates = sorted(limit_days)
    calendar = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date >= dates[0])
            .order_by(TradeCalendar.trade_date)
        )
    )
    position = {day: index for index, day in enumerate(calendar)}
    data_end = session.scalar(select(func.max(StockDaily.trade_date)))

    def next_trading(day: date) -> date | None:
        index = position.get(day)
        if index is None or index + 1 >= len(calendar):
            return None
        return calendar[index + 1]

    day_rows: list[TrackDay] = []
    open_values: list[float] = []
    close_values: list[float] = []
    quoted = 0  # 有次日日线的候选（含一字板）
    promoted_total = 0
    unbuyable_total = 0
    no_quote_total = 0
    big_loss_total = 0
    limit_down_total = 0

    for day in dates:
        board = first_board(
            session,
            trade_date=day,
            before=before,
            min_float_mv=min_float_mv,
            max_float_mv=max_float_mv,
            min_amount=min_amount,
            min_themes=min_themes,
            min_seal_ratio=min_seal_ratio,
            only_true_first_board=only_true_first_board,
            order=order,
            # `top=0` 时要看**全部**候选，不能被页面上的前 50 只截断
            limit=top or 1000,
        )
        codes = [row.code for row in board.rows]
        following = next_trading(day)
        if not codes:
            day_rows.append(
                TrackDay(day, following, 0, 0, None, None, 0, 0, "ok")
            )
            continue
        if following is None or data_end is None or following > data_end:
            day_rows.append(
                TrackDay(day, following, len(codes), 0, None, None, 0, 0, "pending")
            )
            continue

        quotes = {
            code: (open_, high, low, close, pct)
            for code, open_, high, low, close, pct in session.execute(
                select(
                    StockDaily.code,
                    StockDaily.open,
                    StockDaily.high,
                    StockDaily.low,
                    StockDaily.close,
                    StockDaily.pct_chg,
                ).where(StockDaily.trade_date == following)
            )
        }
        pools_up: set[str] = set()
        pools_down: set[str] = set()
        for pool_type, code in session.execute(
            select(LimitPool.pool_type, LimitPool.code).where(
                LimitPool.trade_date == following,
                LimitPool.pool_type.in_(("up", "down")),
            )
        ):
            (pools_up if pool_type == "up" else pools_down).add(code)

        day_promoted = 0
        day_unbuyable = 0
        day_no_quote = 0
        day_open: list[float] = []
        day_close: list[float] = []
        for code in codes:
            quote = quotes.get(code)
            if quote is None:
                day_no_quote += 1
                continue
            open_, high, low, close, pct = quote
            promoted = code in pools_up
            if promoted:
                day_promoted += 1
            if (
                promoted
                and open_ is not None
                and high is not None
                and low is not None
                and close is not None
                and high == low == open_ == close
            ):
                # 一字涨停：开盘价就是板价，按开盘价买不进
                day_unbuyable += 1
                continue
            if not close or pct is None or pct <= -100:
                day_no_quote += 1
                continue
            # 昨收用官方涨跌幅反推：除权日直接拿 T 日收盘会算出一个假跳空
            prev_close = close / (1 + pct / 100)
            if prev_close <= 0 or open_ is None:
                day_no_quote += 1
                continue
            day_open.append((open_ / prev_close - 1) * 100)
            day_close.append(pct)
            if pct <= _BIG_LOSS_PCT:
                big_loss_total += 1
            if code in pools_down:
                limit_down_total += 1

        quoted += len(codes) - day_no_quote
        promoted_total += day_promoted
        unbuyable_total += day_unbuyable
        no_quote_total += day_no_quote
        open_values.extend(day_open)
        close_values.extend(day_close)
        day_rows.append(
            TrackDay(
                day,
                following,
                len(codes),
                day_promoted,
                _mean_median(day_open)[0],
                _mean_median(day_close)[0],
                day_unbuyable,
                day_no_quote,
                "ok",
            )
        )

    open_mean, open_median = _mean_median(open_values)
    close_mean, close_median = _mean_median(close_values)
    summary = {
        # 已验证 / 待验证的天数（「待验证」= 次日日线还没采到）
        "verified_days": sum(1 for row in day_rows if row.status == "ok"),
        "pending_days": sum(1 for row in day_rows if row.status == "pending"),
        # 样本 = 有次日日线、且买得到的候选
        "samples": len(close_values),
        "candidates": quoted,
        "promoted_rate": _rate(promoted_total, quoted),
        "open_premium_mean": open_mean,
        "open_premium_median": open_median,
        "close_ret_mean": close_mean,
        "close_ret_median": close_median,
        "big_loss_rate": _rate(big_loss_total, len(close_values)),
        "limit_down_rate": _rate(limit_down_total, len(close_values)),
        "unbuyable_rate": _rate(unbuyable_total, quoted),
        "no_quote": no_quote_total,
    }
    return FirstBoardTrack(
        trade_date=dates[-1], conditions=conditions, days=day_rows, summary=summary
    )


@dataclass(frozen=True, slots=True)
class RpsTrackDay:
    trade_date: date
    picks: int
    ret1: float | None
    ret5: float | None
    excess1: float | None
    excess5: float | None
    #: `ok` = 至少能算出 1 日收益；`pending` = 次日日线还没到
    status: str


@dataclass(frozen=True, slots=True)
class RpsTrack:
    trade_date: date | None
    days: list[RpsTrackDay] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


def _empty_rps_summary() -> dict:
    return {
        "days": 0,
        "verified_days": 0,
        "samples": 0,
        "ret1": {"days": 0, "mean": None, "excess": None},
        "ret5": {"days": 0, "mean": None, "excess": None},
    }


def rps_track(
    session: Session,
    days: int = 20,
    min_rps: float = 0,
    top: int = 50,
    mode: str = "all",
    breakout_only: bool = False,
    trend_template: bool = False,
    order: str = "strength",
) -> RpsTrack:
    """近 `days` 个**有 RPS 记录**的交易日，榜单前 `top` 只的 1 / 5 日表现。

    ⚠️ `rps_daily` 只存「扫描跑过的那一天」，所以这里统计的天数就是线上真实跑过的
    天数，不是自然交易日 —— 中间断过的日子没有样本，页面要如实显示 `days`。

    收益与超额**直接用 `pattern_track._curves`**（与形态跟踪同一份矩阵、同一套
    「全市场等权」基准），别在这里另写一遍。
    """
    dates = sorted(
        session.scalars(
            select(RpsDaily.trade_date)
            .group_by(RpsDaily.trade_date)
            .order_by(RpsDaily.trade_date.desc())
            .limit(days)
        )
    )
    if not dates:
        return RpsTrack(trade_date=None, summary=_empty_rps_summary())

    # 趋势模板要读全池日线：20 个信号日逐日各读一遍约 220 秒，这里只读一次并集窗口
    trend_cache = _build_trend_bars(session, dates) if trend_template else None

    picks: dict[date, list[tuple[str, str | None, float]]] = {}
    for day in dates:
        board = rps_board(
            session,
            trade_date=day,
            min_rps=min_rps,
            limit=top,
            mode=mode,
            breakout_only=breakout_only,
            trend_template=trend_template,
            order=order,
            trend_cache=trend_cache,
        )
        if board.rows:
            picks[day] = [
                (row.code, row.name, row.rps250 or 0.0) for row in board.rows
            ]
    if not picks:
        return RpsTrack(trade_date=dates[-1], summary=_empty_rps_summary())

    curves = pattern_track._curves(session, picks, 5)  # 复用同一份矩阵与「全市场等权」基准

    day_rows: list[RpsTrackDay] = []
    stats: dict[int, list[tuple[float, float]]] = {1: [], 5: []}
    for day in dates:
        curve = curves.get(day, {})
        values: dict[int, tuple[float | None, float | None]] = {}
        for step in (1, 5):
            entry = curve.get(step)
            if entry is None or not len(entry[0]):
                values[step] = (None, None)
                continue
            mean = float(entry[0].mean()) * 100
            market = entry[1]
            excess = mean - market * 100 if market == market else None
            values[step] = (round(mean, 2), None if excess is None else round(excess, 2))
            if excess is not None:
                stats[step].append((mean, excess))
        ok = values[1][0] is not None
        day_rows.append(
            RpsTrackDay(
                day,
                len(picks.get(day, [])),
                values[1][0],
                values[5][0],
                values[1][1],
                values[5][1],
                "ok" if ok else "pending",
            )
        )

    def _summarize(step: int) -> dict:
        items = stats[step]
        return {
            "days": len(items),
            "mean": round(sum(item[0] for item in items) / len(items), 2) if items else None,
            "excess": round(sum(item[1] for item in items) / len(items), 2) if items else None,
        }

    return RpsTrack(
        trade_date=dates[-1],
        days=day_rows,
        summary={
            "days": len(day_rows),
            "verified_days": sum(1 for row in day_rows if row.status == "ok"),
            "samples": sum(row.picks for row in day_rows if row.status == "ok"),
            "ret1": _summarize(1),
            "ret5": _summarize(5),
        },
    )


#: 板块强弱的分位线（与 `scripts/backtest_sector_stock.py` 同一套）
SECTOR_STRONG = 90.0
SECTOR_WEAK = 10.0


@dataclass(frozen=True, slots=True)
class SectorStockRow:
    code: str
    name: str | None
    sector_code: str
    sector_name: str | None
    #: 所属板块当天在全部板块里的 RPS20 分位
    rps20: float
    #: 板块当日涨幅（%）
    sector_pct_chg: float | None
    #: 个股当日涨幅（%）
    pct_chg: float | None
    #: 量价因子分（仅 `order="factor"` 时计算）
    factor_score: float | None = None


@dataclass(frozen=True, slots=True)
class SectorStockSector:
    code: str
    name: str | None
    rps20: float
    pct_chg: float | None
    member_count: int | None


@dataclass(frozen=True, slots=True)
class SectorStockBoard:
    trade_date: date | None
    conditions: dict = field(default_factory=dict)
    sectors: list[SectorStockSector] = field(default_factory=list)
    rows: list[SectorStockRow] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SectorStockTrackDay:
    trade_date: date
    picks: int
    ret1: float | None
    excess1: float | None
    status: str


@dataclass(frozen=True, slots=True)
class SectorStockTrack:
    trade_date: date | None
    conditions: dict = field(default_factory=dict)
    days: list[SectorStockTrackDay] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


def sector_stock(
    session: Session,
    trade_date: date | None = None,
    taxonomy: str = "kph_selected",
    scope: str = "strong",
    order: str = "gain",
    sectors: int = 3,
    per_sector: int = 3,
    limit: int = 50,
) -> SectorStockBoard:
    """从板块强度落到个股：强势（或弱势）板块里挑当日最强的几只。

    ⚠️ **这组候选的历史超额是负的**（2026-10-25 用补回来的 60 天成分股检验，已剔 ST /
    北交所）：IC −0.046、强势板块选股超额 −0.17%、强势板块 × 突破 −0.30%
    （全市场突破 +0.18%）。数字见 `docs/回测_板块到个股_20261025.md`。

    上线它的目的**不是推荐**，而是让人看见「板块强 ≠ 个股强」；页面上必须如实标注
    负超额，`scope="weak"` 那一档是同一套检验的反向对照（近 20 日 +0.11%，扣费用后
    基本抹平，同样不能当策略用）。

    口径：
    - 板块强度 = `sector_rps.rps20_on`（与页面第三块、研究脚本同一份）；
    - `scope="strong"` 取 RPS20 ≥ 90 的前 `sectors` 个，`weak` 取 ≤ 10 的前几个；
    - 每个板块的成员按当日涨幅（`order="gain"`）或量价因子分（`order="factor"`）取前
      `per_sector` 只，跨板块去重（先命中的板块优先）。
    """
    conditions = {
        "taxonomy": taxonomy,
        "scope": scope,
        "order": order,
        "sectors": sectors,
        "per_sector": per_sector,
    }
    target = trade_date or session.scalar(
        select(func.max(SectorDaily.trade_date)).where(SectorDaily.taxonomy == taxonomy)
    )
    if target is None:
        return SectorStockBoard(trade_date=None, conditions=conditions)

    rps20 = sector_rps.rps20_on(session, target, taxonomy)
    if not rps20:
        return SectorStockBoard(trade_date=target, conditions=conditions)

    ranked = sorted(rps20.items(), key=lambda item: item[1], reverse=scope == "strong")
    if scope == "strong":
        picked = [(code, value) for code, value in ranked if value >= SECTOR_STRONG][:sectors]
    else:
        picked = [(code, value) for code, value in ranked if value <= SECTOR_WEAK][:sectors]
    if not picked:
        return SectorStockBoard(trade_date=target, conditions=conditions)

    meta = {
        code: (name, pct_chg, member_count)
        for code, name, pct_chg, member_count in session.execute(
            select(
                SectorDaily.sector_code,
                SectorDaily.name,
                SectorDaily.pct_chg,
                SectorDaily.member_count,
            ).where(
                SectorDaily.trade_date == target,
                SectorDaily.taxonomy == taxonomy,
            )
        )
    }

    codes = [code for code, _value in picked]
    # 当天没采到的板块（RPS 用的是窗口内数据）退到最近一次的名称 —— 否则榜单上
    # 会出现一个没名字的板块。但**当日涨幅不补**：那天就是没采到，补旧值会误导。
    missing = [code for code in codes if code not in meta]
    if missing:
        seen: set[str] = set()
        for code, name, count in session.execute(
            select(
                SectorDaily.sector_code,
                SectorDaily.name,
                SectorDaily.member_count,
            )
            .where(
                SectorDaily.taxonomy == taxonomy,
                SectorDaily.sector_code.in_(missing),
                SectorDaily.trade_date < target,
            )
            .order_by(SectorDaily.sector_code, SectorDaily.trade_date.desc())
        ):
            if code in seen:
                continue
            seen.add(code)
            meta[code] = (name, None, count)

    by_sector: dict[str, list[tuple[str, str | None, float | None]]] = defaultdict(list)
    for code, name, sector, pct in session.execute(
        select(
            SectorMember.code,
            SectorMember.name,
            SectorMember.sector_code,
            SectorMember.pct_chg,
        ).where(
            SectorMember.trade_date == target,
            SectorMember.sector_code.in_(codes),
        )
    ):
        # 与首板池同一套剔除：ST（风险警示）与北交所（30% 涨跌停 + 流动性另一套玩法）
        if is_st(name) or limit_rules.is_bse(code):
            continue
        by_sector[sector].append((code, name, pct))

    computed = (
        factor_scores.scores(
            session,
            [item[0] for sector in codes for item in by_sector.get(sector, [])],
            target,
        )
        if order == "factor"
        else {}
    )

    rows: list[SectorStockRow] = []
    seen: set[str] = set()
    for sector, value in picked:
        name, sector_pct, _members = meta.get(sector, (None, None, None))
        items = by_sector.get(sector, [])
        if order == "factor":
            items = sorted(
                items,
                key=lambda item: -(computed[item[0]].score if item[0] in computed else -1.0),
            )
        else:
            items = sorted(items, key=lambda item: -(item[2] or 0.0))
        for code, stock_name, pct in items[:per_sector]:
            if code in seen:
                continue
            seen.add(code)
            rows.append(
                SectorStockRow(
                    code=code,
                    name=stock_name,
                    sector_code=sector,
                    sector_name=name,
                    rps20=value,
                    sector_pct_chg=sector_pct,
                    pct_chg=pct,
                    factor_score=(
                        computed[code].score if code in computed else None
                    ),
                )
            )

    board_sectors = [
        SectorStockSector(
            code=sector,
            name=meta.get(sector, (None, None, None))[0],
            rps20=value,
            pct_chg=meta.get(sector, (None, None, None))[1],
            member_count=meta.get(sector, (None, None, None))[2],
        )
        for sector, value in picked
    ]
    return SectorStockBoard(
        trade_date=target,
        conditions=conditions,
        sectors=board_sectors,
        rows=rows[:limit],
    )


def sector_stock_track(
    session: Session,
    days: int = 20,
    taxonomy: str = "kph_selected",
    scope: str = "strong",
    order: str = "gain",
    sectors: int = 3,
    per_sector: int = 3,
) -> SectorStockTrack:
    """近 `days` 个有成分股的交易日，`sector_stock` 选出的票的次日表现。

    收益与超额直接用 `pattern_track._curves`（与形态跟踪、RPS 验证同一份矩阵与
    「当日全市场等权」基准），别在这里另写一套。
    """
    conditions = {
        "taxonomy": taxonomy,
        "scope": scope,
        "order": order,
        "sectors": sectors,
        "per_sector": per_sector,
    }
    dates = sorted(
        session.scalars(
            select(SectorMember.trade_date)
            .group_by(SectorMember.trade_date)
            .order_by(SectorMember.trade_date.desc())
            .limit(days)
        )
    )
    if not dates:
        return SectorStockTrack(trade_date=None, conditions=conditions)

    picks: dict[date, list[tuple[str, str | None, float]]] = {}
    for day in dates:
        board = sector_stock(
            session,
            trade_date=day,
            taxonomy=taxonomy,
            scope=scope,
            order=order,
            sectors=sectors,
            per_sector=per_sector,
            limit=per_sector * sectors,
        )
        if board.rows:
            picks[day] = [(row.code, row.name, row.rps20) for row in board.rows]
    if not picks:
        return SectorStockTrack(trade_date=dates[-1], conditions=conditions)

    curves = pattern_track._curves(session, picks, 1)

    day_rows: list[SectorStockTrackDay] = []
    stats: list[tuple[float, float]] = []
    pooled: list[float] = []
    for day in dates:
        curve = curves.get(day, {})
        entry = curve.get(1)
        ret1 = excess1 = None
        if entry is not None and len(entry[0]):
            values = entry[0] * 100  # 转成百分数，与页面其它读数一致
            mean = float(values.mean())
            ret1 = round(mean, 2)
            if entry[1] == entry[1]:
                excess1 = round(mean - entry[1] * 100, 2)
                stats.append((ret1, excess1))
                pooled.extend(float(value) for value in values)
        day_rows.append(
            SectorStockTrackDay(
                day,
                len(picks.get(day, [])),
                ret1,
                excess1,
                "ok" if ret1 is not None else "pending",
            )
        )

    verified = [row for row in day_rows if row.status == "ok"]
    mean, median = _mean_median(pooled)
    return SectorStockTrack(
        trade_date=dates[-1],
        conditions=conditions,
        days=day_rows,
        summary={
            "days": len(day_rows),
            "verified_days": len(verified),
            "samples": len(pooled),
            "ret1": {
                "days": len(stats),
                "mean": mean,
                "median": median,
                # 胜率 = 收益 > 0 的样本占比；大面 = 收益 ≤ −5% 的样本占比
                "up_pct": round(sum(1 for value in pooled if value > 0) / len(pooled) * 100, 1)
                if pooled
                else None,
                "big_loss": round(
                    sum(1 for value in pooled if value <= -5.0) / len(pooled) * 100, 1
                )
                if pooled
                else None,
                "excess": round(sum(item[1] for item in stats) / len(stats), 2)
                if stats
                else None,
            },
        },
    )

