"""每日形态「评分前 N 只」的 30 个交易日跟踪（胜率统计）。

形态页的默认视图是「今天命中里评分最高的 50 只」（`/api/patterns/hits` 不传 `pattern`
时的口径，也是每天补 DDE 的同一批）。这一页回答的是另一个问题：

**每天照这份清单选出来的 50 只，选出来之后的 30 个交易日是怎么走的。**

所以每个筛选日 = **一个循环**：把这个循环的 50 只从筛选日收盘开始往后 30 个交易日
逐日算累计收益，得到一条 30 个点的曲线；再看每个点上的胜率与相对大盘的超额。
最近若干个循环并排看，就能分出「这套清单是常赢、还是只有某几天运气好」。

零 iFinD 配额：全部用库里的日线在本地算（与回测脚本一样）。

## 三个口径必须先说清楚

1. **收益用 `pct_chg` 逐日复利，不用收盘价比值。** `stock_daily` 存的是**不复权价**，
   直接拿 `close[d+n] / close[d]` 算，遇到除权的票会凭空多出一个 −30% 甚至 −90% 的
   样本（形态引擎为了同一个问题，必须在内存里重建前复权序列）。iFinD 的涨跌幅是
   **已按除权调整的真实收益率**（实测 21 个银行除权日验证过），逐日复利得到的正是
   前复权口径下的区间收益。
2. **停牌日不算收益、但两端都不能缺行。** iFinD 对停牌股把四价填成相等、涨跌幅留空，
   所以复利时跳过空值即可（空值等价于 0 涨跌）。但**筛选日与第 n 个交易日这两端都要有
   该股的行**，否则这只票在第 n 点上没有样本 —— 长期停牌、退市的票不该按 0% 计入胜率，
   那会把胜率往 50% 拉。
3. **基准是「同一天全市场平均」**（与 `scripts/backtest_patterns.py` 同一口径）。
   不这么做的话，「涨了 5%」可能只是那几天大盘在涨。所以每个点上都给
   **平均超额**与**跑赢比例**两个数。

## 样本口径：逐日、逐循环都不去重

同一只票在同一个循环里只出现一次；但它连上三个循环就算三个样本（2026-09-27 用户定）
—— 这回答的是「每天照这份清单买会怎样」。同时给出去重后的只数，便于知道重复程度。

## 未走完的循环不编造数据

第 n 个交易日的行情还没到（或该股当天没有行）时，那个点**不返回**：`progress` 只记到
能算的那个 n。页面据此显示「进度 17/30」、`samples` 也会小于当天选出的只数。

**某一天整个市场都没有行情行**时（交易日历里有、但采集被配额让路跳过或那次采集失败），
循环也停在那一天之前 —— 复利会把那种日子当成 0 涨跌，等于凭空抹掉一天的真实波动，而
曲线看上去还是连续的。等那天补上日线，这里自然就接上了。

⚠️ **休市日不属于上面那种情况**：它根本不在 `trade_calendar` 里（2026-09-25 中秋就是），
所以既不会被当成缺口，也不需要补。这也意味着库里的日线只到 09-24 是**完整的**。
"""

import logging
from collections import defaultdict
from datetime import date

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import PatternHit, StockDaily, TradeCalendar
from app.services.patterns import PATTERNS

logger = logging.getLogger(__name__)

# 默认值：最近 30 个循环（= 30 个筛选日），每个循环跟踪 30 个交易日，每天取前 50 只
DEFAULT_COHORTS = 30
DEFAULT_TOP = 50
DEFAULT_TRACK_DAYS = 30

# **统计起点**由 `Settings.pattern_track_start` 传进来（用户 2026-09-27 要求
# 「每个循环从 9 月 24 日开始统计」）：从那天起，每个有命中记录的交易日就是一个循环。
# 为什么要有起点 —— 09-24 之前那几天只扫了 3032 只（当天起才扩到 5279 只），
# 「前 50 只」是在不同大小的池子里排出来的，前后不可比。这里**不设默认值**，
# 免得配置与代码各有一份真相。

# 只统计**注册表里当前存在**的形态。少了这道过滤，被删形态的历史命中会混进
# 「评分前 50 只」（`/hits` 与 DDE 补齐都已经加了同一道过滤，口径必须一致）
_KEYS = tuple(pattern.key for pattern in PATTERNS)

# 一个循环选中的一只票：(代码, 名称, 最高分)
Pick = tuple[str, str | None, float]


def _scan_dates(session: Session, cohorts: int, start: date) -> list[date]:
    """`start` 起、最近 `cohorts` 个**有命中记录**的交易日，升序返回。"""
    rows = session.scalars(
        select(PatternHit.trade_date)
        .where(PatternHit.pattern.in_(_KEYS), PatternHit.trade_date >= start)
        .group_by(PatternHit.trade_date)
        .order_by(PatternHit.trade_date.desc())
        .limit(cohorts)
    ).all()
    return sorted(rows)


def _picks(session: Session, days: list[date], top: int) -> dict[date, list[Pick]]:
    """每个循环的「评分前 `top` 只」，按票归并取最高分（名称取自命中记录的快照）。

    ⚠️ 并列分数时的取舍与 `/hits` **不保证逐只一致**（那边是纯 SQL `order by score
    desc`、没有 tie-break，本身也是任意的；这里显式按代码升序兜底，至少**同一天同
    一份数据每次结果相同**）。差一两只不影响统计，但别拿「第 50 位是谁」去对账。
    """
    best: dict[tuple[date, str], Pick] = {}
    if not days:
        return {}
    rows = session.execute(
        select(PatternHit.trade_date, PatternHit.code, PatternHit.name, PatternHit.score).where(
            PatternHit.trade_date.in_(days), PatternHit.pattern.in_(_KEYS)
        )
    ).all()
    for day, code, name, score in rows:
        key = (day, code)
        current = best.get(key)
        if current is None or score > current[2]:
            best[key] = (code, name, float(score))

    grouped: dict[date, list[Pick]] = defaultdict(list)
    for (day, _code), item in best.items():
        grouped[day].append(item)

    picked: dict[date, list[Pick]] = {}
    for day, items in grouped.items():
        items.sort(key=lambda item: (-item[2], item[0]))
        picked[day] = items[:top]
    return picked


def _curves(
    session: Session,
    picks: dict[date, list[str]],
    track_days: int,
) -> dict[date, dict[int, tuple[np.ndarray, float]]]:
    """每个循环在每个 n 上的收益，以及**它自己那天**的全市场均值。

    返回 `{循环日: {第 n 个交易日: (个股收益数组, 全市场均值)}}`。数组与
    `picks[day]` 一一对应（只保留两端都有行的），全市场均值算不出来时是 nan。
    """
    empty: dict = {}
    if not picks:
        return empty

    # 交易日历：跟踪天数是「几个交易日」，得按日历往后数，不能用日历天加
    calendar = list(
        session.scalars(select(TradeCalendar.trade_date).order_by(TradeCalendar.trade_date))
    )
    index = {day: i for i, day in enumerate(calendar)}
    scan_days = [day for day in picks if day in index]
    if not scan_days:
        logger.warning("命中记录的日期都不在交易日历里，跳过跟踪统计")
        return empty

    # 需要的行情 = 各循环自己那一段（`[循环日, 循环日+track_days]`）的**并集**。
    #
    # ⚠️ 两个坑都在这一行里：
    #   1. 必须按「**最后一天真有行情**」封顶，不能只按交易日历 —— 日历是预置到年底的，
    #      只按日历切的话，最近那几个循环「还没走到」的日子会拿到 growth=1（没有行 =
    #      涨跌幅记 0），曲线会凭空多出一段平线，页面显示成「30/30 走完了」。
    #   2. 取**并集**而不是「最早循环 → 最晚到期日」那一整段：库里早期只有零星几天扫描
    #      （05-14、07-01、07-10 …），整段连续日期的跨度是 95 个交易日，而各循环真正用到
    #      的只有 40 来个 —— 多出来的那 55 天要白拉 30 万行（实测 2.4s → 1.5s 的差别）。
    #      代价是各循环的日期在矩阵里不再连续，所以下面按日历逐个查位置，不能按偏移量取。
    data_end = session.scalar(select(func.max(StockDaily.trade_date)))
    if data_end is None or data_end not in index:
        logger.warning("库里没有日线数据，跳过跟踪统计")
        return empty
    end_pos = index[data_end]
    needed: set[date] = set()
    for day in scan_days:
        start = index[day]
        needed.update(calendar[start : min(start + track_days, end_pos) + 1])
    dates = sorted(needed)
    if not dates:
        return empty
    date_pos = {day: i for i, day in enumerate(dates)}

    # 一次把窗口内的日线拉出来，就地压成两张矩阵：涨跌幅、有没有这一行（停牌行也会
    # 写库，但涨跌幅为空）。`yield_per` 避免把几十万行一次性拉进内存（30 个循环
    # × 30 个交易日 ≈ 60 个交易日 × 5000 多只 ≈ 30 万行）。
    codes: list[str] = []
    code_pos: dict[str, int] = {}
    cells: list[tuple[int, int, float | None]] = []
    rows = session.execute(
        select(StockDaily.trade_date, StockDaily.code, StockDaily.pct_chg).where(
            StockDaily.trade_date.in_(dates)
        )
    ).yield_per(20000)
    for when, code, pct_value in rows:
        position = code_pos.get(code)
        if position is None:
            position = len(codes)
            code_pos[code] = position
            codes.append(code)
        cells.append((date_pos[when], position, pct_value))

    shape = (len(dates), len(codes))
    pct = np.zeros(shape, dtype=np.float64)
    present = np.zeros(shape, dtype=bool)
    for when, position, pct_value in cells:
        present[when, position] = True
        if pct_value is not None:
            pct[when, position] = float(pct_value)

    growth = 1.0 + pct / 100.0
    curves: dict[date, dict[int, tuple[np.ndarray, float]]] = {}
    for day in scan_days:
        calendar_pos = index[day]
        start = date_pos[day]
        picked = np.array([code_pos.get(code, -1) for code, _, _ in picks[day]], dtype=np.int64)
        index_in = picked[picked >= 0]
        entry_present = present[start]
        has_entry = entry_present[index_in] if index_in.size else np.array([], dtype=bool)
        curve: dict[int, tuple[np.ndarray, float]] = {}
        # 累乘推进而不是每个 n 重算一遍乘积：30 个循环 × 30 个点要算 900 次，
        # 每次重乘等于把整个矩阵乘 900 遍
        accumulated = np.ones(len(codes), dtype=np.float64)
        for step in range(1, track_days + 1):
            shift = calendar_pos + step
            if shift > end_pos:
                break  # 这一天的行情还没到（或还没采），循环到这儿为止
            when = calendar[shift]
            position = date_pos.get(when)
            if position is None:
                continue  # 不该发生：`needed` 已包含每个循环的整段区间
            accumulated = accumulated * growth[position]
            total = accumulated - 1.0
            if not present[position].any():
                # 这一天**整个市场一行都没有**：复利会把它当成 0 涨跌，等于凭空抹掉一天
                # 的真实波动，而曲线看上去还是连续的 —— 所以宁可把循环停在这里，页面
                # 显示「进度 N/30」。补上那天的日线之后这里自然接得上。
                #
                # ⚠️ 拦的只是「交易日、但一行都没有」：**休市日根本不在交易日历里**
                # （2026-09-25 中秋就是，它不会被当成缺口、也不需要补），真会走到这里的
                # 是「配额让路跳过了整天的日线采集」或「那次采集失败」。
                logger.debug("%s 的第 %d 个交易日（%s）整个市场没有日线，循环统计到此前为止", day, step, when)
                break
            usable = entry_present & present[position]
            market = float(total[usable].mean()) if usable.any() else float("nan")
            if index_in.size:
                ok = has_entry & present[position, index_in]
                values = total[index_in[ok]]
            else:
                values = np.array([])
            curve[step] = (values, market)
        if curve:
            curves[day] = curve
    return curves


def _stats(values: np.ndarray, market: float) -> dict:
    """一个点上的统计量。收益都是**百分数**（1.23 表示 +1.23%）。

    `market` 是**这个循环这一天**的全市场等权平均（算不出来时是 nan）。
    """
    count = int(values.size)
    if count == 0:
        return {
            "samples": 0,
            "mean": None,
            "median": None,
            "up_pct": None,
            "excess": None,
            "beat_pct": None,
        }
    out = {
        "samples": count,
        "mean": round(float(values.mean()) * 100, 2),
        "median": round(float(np.median(values)) * 100, 2),
        "up_pct": round(float((values > 0).mean()) * 100, 1),
        "excess": None,
        "beat_pct": None,
    }
    if np.isfinite(market):
        diff = values - market
        out["excess"] = round(float(diff.mean()) * 100, 2)
        out["beat_pct"] = round(float((diff > 0).mean()) * 100, 1)
    return out


def _pooled_stats(items: list[tuple[np.ndarray, float]]) -> dict:
    """把多个循环在同一个 n 上的样本摊平再统计（主线用）。

    ⚠️ 不是「各循环结果的再平均」：各循环在这个 n 上的样本数并不相等（有的循环走
    不到这么远、有的票停牌），按循环平均会让样本少的那些循环权重被放大。
    """
    if not items:
        return _stats(np.array([]), float("nan"))
    values = np.concatenate([value for value, _ in items])
    # 超额要**逐样本**比：每只票减掉它自己循环那天的全市场均值
    diff = np.concatenate(
        [value - market if np.isfinite(market) else np.full(value.size, np.nan)
         for value, market in items]
    )
    out = {
        "samples": int(values.size),
        "mean": round(float(values.mean()) * 100, 2),
        "median": round(float(np.median(values)) * 100, 2),
        "up_pct": round(float((values > 0).mean()) * 100, 1),
        "excess": None,
        "beat_pct": None,
    }
    ok = np.isfinite(diff)
    if ok.any():
        out["excess"] = round(float(diff[ok].mean()) * 100, 2)
        out["beat_pct"] = round(float((diff[ok] > 0).mean()) * 100, 1)
    return out


def track(
    session: Session,
    *,
    start: date,
    cohorts: int = DEFAULT_COHORTS,
    top: int = DEFAULT_TOP,
    track_days: int = DEFAULT_TRACK_DAYS,
) -> dict:
    """滚动统计 `start` 起最近 `cohorts` 个循环：每个循环的 50 只跟踪 `track_days` 个交易日。"""
    picks = _picks(session, _scan_dates(session, cohorts, start), top)
    curves = _curves(session, picks, track_days)
    days_desc = sorted(picks, reverse=True)

    rows = []
    unique: set[str] = set()
    total_stocks = 0
    pool: dict[int, list[tuple[np.ndarray, float]]] = defaultdict(list)
    for day in days_desc:
        unique.update(item[0] for item in picks[day])
        total_stocks += len(picks[day])
        curve = curves.get(day, {})
        points = []
        for step in sorted(curve):
            values, market = curve[step]
            points.append({"day": step, **_stats(values, market)})
            if values.size:
                pool[step].append((values, market))
        rows.append(
            {
                "trade_date": day,
                "stocks": len(picks[day]),
                # 已经能算到第几个交易日（= 这个循环走完了多少）
                "progress": max(curve) if curve else 0,
                # 曲线上的峰 / 谷由前端从 points 里取（不在这里另存一份，免得两处漂移）
                "points": points,
            }
        )

    average = [
        {"day": step, **_pooled_stats(pool[step])}
        for step in sorted(pool)
    ]
    return {
        "cohorts": cohorts,
        "track_days": track_days,
        "top": top,
        "summary": {
            "cohorts_used": len(rows),
            "stocks": total_stocks,
            "unique": len(unique),
            "first_date": rows[-1]["trade_date"] if rows else None,
            "last_date": rows[0]["trade_date"] if rows else None,
        },
        "average": average,
        "days": rows,
    }


def detail(
    session: Session,
    day: date,
    *,
    top: int = DEFAULT_TOP,
    track_days: int = DEFAULT_TRACK_DAYS,
) -> dict:
    """某个循环选中的票，**逐只、逐日**列出它们之后每个交易日的当日涨跌幅。

    与 `track()` 的分工：那边给的是「到第 n 日的累计收益」的统计量（画曲线、算胜率），
    这里给的是原始明细 —— 回答「那 50 只到底是哪些票、之后每天各涨跌多少」。

    ⚠️ 每一列都是**那一天的当日涨跌幅**（相对前一交易日），不是从筛选日起算的累计。
    要累计就自己往上连乘，或者在 `track()` 的统计量里看。
    """
    picks = _picks(session, [day], top).get(day, [])
    empty = {
        "trade_date": day,
        "top": top,
        "track_days": track_days,
        "progress": 0,
        "days": [],
        "rows": [],
    }
    if not picks:
        return empty

    calendar = list(
        session.scalars(select(TradeCalendar.trade_date).order_by(TradeCalendar.trade_date))
    )
    index = {item: i for i, item in enumerate(calendar)}
    start = index.get(day)
    data_end = session.scalar(select(func.max(StockDaily.trade_date)))
    if start is None or data_end is None or data_end not in index:
        return empty

    # 之后 `track_days` 个交易日，按「最后一天真有行情」封顶 —— 与 `track()` 同一道
    # 边界（否则会把还没走到的日子列成空白，看着像「当天没涨跌」）
    stop = min(start + track_days, index[data_end])
    days = calendar[start + 1 : stop + 1]

    # 再按**整个市场有没有数据**截断：某天一行都没有（交易日历里有、但采集被配额让路
    # 跳过或失败）就停在它之前。不这么做的话，那天会显示成整列「—」，读起来像「当天
    # 全市场没涨跌」，而实际是「我们没这天的数据」。`track()` 用的是同一条规则，
    # 两边进度才对得上。（休市日不在这里 —— 它根本不在交易日历里。）
    if days:
        covered = {
            when
            for (when,) in session.execute(
                select(StockDaily.trade_date)
                .where(StockDaily.trade_date.in_(days))
                .group_by(StockDaily.trade_date)
            ).all()
        }
        for offset, when in enumerate(days):
            if when not in covered:
                days = days[:offset]
                break

    values: dict[str, dict[date, float | None]] = defaultdict(dict)
    if days:
        codes = [item[0] for item in picks]
        rows = session.execute(
            select(StockDaily.code, StockDaily.trade_date, StockDaily.pct_chg).where(
                StockDaily.code.in_(codes), StockDaily.trade_date.in_(days)
            )
        ).all()
        for code, when, pct_value in rows:
            values[code][when] = None if pct_value is None else round(float(pct_value), 2)

    return {
        "trade_date": day,
        "top": top,
        "track_days": track_days,
        # 这个循环已经走到第几个交易日（与 `track()` 的 progress 同一含义）
        "progress": len(days),
        # 每一列对应的实际交易日，给前端做表头提示用
        "days": [item.isoformat() for item in days],
        "rows": [
            {
                "code": code,
                "name": name,
                "score": round(score, 1),
                "pct": [values.get(code, {}).get(item) for item in days],
            }
            for code, name, score in picks
        ],
    }
