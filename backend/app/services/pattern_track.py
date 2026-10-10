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

⚠️ **2026-10-10 起这条守卫放宽为「覆盖够」**：不再只拦「一行都没有」，而是拦
**采得不全**的日子（判据见 `_COMPLETE_DAY_RATIO`）。原因是原来那版把只采了几十只的
残缺日当成完整交易日，`market` 就用这几十只算「全市场」。线上「收盘后到采集跑完」的
窗口里会短暂出现这种日子。

⚠️ **休市日不属于上面那种情况**：它根本不在 `trade_calendar` 里（2026-09-25 中秋就是），
所以既不会被当成缺口，也不需要补。这也意味着库里的日线只到 09-24 是**完整的**。
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
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

#: 一个窗口交易日「算不算采全了」的判据（2026-10-10 加，口径变更）。
#:
#: 原来只要求那天「有 ≥1 行行情」就当完整交易日 —— 于是采集没跑完的窗口里，
#: 09-29(53 行)、09-30、10-08、10-09(各 50 行) 这种**只采了几十只**的日子也被当成
#: 全市场，`market = total[usable].mean()` 就拿这几十只算「全市场等权基准」，
#: 会凭空得到一个偏得很远的超额。线上「收盘后到采集跑完」的窗口里会短暂出现。
#:
#: 判据：某日行数 < **窗口内单日最大行数 × 0.5** 就视为不完整（完整日 ≈ 全池 5000+，
#: 残缺日只有几十）。**用最大值而不是中位数**：本机库那个窗口里 6 个交易日竟有 4 个是
#: 残缺日（09-24/09-28 完整、其余 4 天各约 50 行），中位数会落在 ~51、×0.5 之后连残缺日
#: 都算「完整」，守卫失效；最大值反映的是全市场规模，残缺日一定远低于它。停牌较多的
#: 正常日仍有 5000 行上下，不会被误伤。
_COMPLETE_DAY_RATIO = 0.5

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


@dataclass
class _Matrix:
    """窗口内的行情矩阵 —— **一次取数、多个口径复用**（见 `_matrix`）。"""

    calendar: list[date]
    index: dict[date, int]
    date_pos: dict[date, int]
    code_pos: dict[str, int]
    #: `(窗口日, 股票)` 的复权增长因子（`1 + pct/100`）与「有没有这一行」
    growth: np.ndarray
    present: np.ndarray
    #: 每个窗口交易日**采全了没有**（判据见 `_COMPLETE_DAY_RATIO`）—— 采得不全的那天
    #: 不能拿来算全市场基准，各循环遇到它就停下（2026-10-10 加）
    complete: np.ndarray
    #: 库里有行情的最后一天在 `calendar` 里的位置（决定各循环能走多远）
    end_pos: int


def _matrix(session: Session, scan_days: list[date], track_days: int) -> _Matrix | None:
    """把各循环要用的那一段行情一次性拉成矩阵（取不到就是 None，调用方跳过）。

    为什么要独立出来（2026-10-09）：`pool_standing` 要对**六个池子**各算一遍收益，
    每遍都自己拉一次日线的话，同一份数据要读六次（30 个循环 × 30 天的并集约 30 万行、
    实测一次约 1.5s）。拆成「拉一次 + 各口径自己算」之后只读一次。

    ⚠️ 两个坑都在 `needed` 那几行里：
      1. 必须按「**最后一天真有行情**」封顶，不能只按交易日历 —— 日历是预置到年底的，
         只按日历切的话，最近那几个循环「还没走到」的日子会拿到 growth=1（没有行 =
         涨跌幅记 0），曲线会凭空多出一段平线，页面显示成「30/30 走完了」。
      2. 取**并集**而不是「最早循环 → 最晚到期日」那一整段：库里早期只有零星几天扫描
         （05-14、07-01、07-10 …），整段连续日期的跨度是 95 个交易日，而各循环真正用到
         的只有 40 来个 —— 多出来的那 55 天要白拉 30 万行（实测 2.4s → 1.5s 的差别）。
         代价是各循环的日期在矩阵里不再连续，所以下面按日历逐个查位置，不能按偏移量取。
    """
    if not scan_days:
        return None
    calendar = list(
        session.scalars(select(TradeCalendar.trade_date).order_by(TradeCalendar.trade_date))
    )
    index = {day: i for i, day in enumerate(calendar)}
    days = [day for day in scan_days if day in index]
    if not days:
        logger.warning("命中记录的日期都不在交易日历里，跳过跟踪统计")
        return None

    data_end = session.scalar(select(func.max(StockDaily.trade_date)))
    if data_end is None or data_end not in index:
        logger.warning("库里没有日线数据，跳过跟踪统计")
        return None
    end_pos = index[data_end]
    needed: set[date] = set()
    for day in days:
        start = index[day]
        needed.update(calendar[start : min(start + track_days, end_pos) + 1])
    dates = sorted(needed)
    if not dates:
        return None
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

    # 每个窗口交易日的覆盖只数 → 判断有没有采全（见 `_COMPLETE_DAY_RATIO`）。
    # `counts.max()` 取的是窗口里最全的那天（≈ 全池 5000+）；残缺日通常只有几十只，
    # 一定低于它的一半。空窗口时 max 会报错，故先兜一个 0。
    counts = present.sum(axis=1)
    threshold = int(counts.max()) * _COMPLETE_DAY_RATIO if counts.size else 0.0
    complete = counts >= threshold

    return _Matrix(
        calendar=calendar,
        index=index,
        date_pos=date_pos,
        code_pos=code_pos,
        growth=1.0 + pct / 100.0,
        present=present,
        complete=complete,
        end_pos=end_pos,
    )


def _curves_from(
    matrix: _Matrix,
    picks: dict[date, list[str]],
    track_days: int,
) -> dict[date, dict[int, tuple[np.ndarray, float]]]:
    """在一份已经拉好的矩阵上算收益曲线（口径见 `_curves`）。

    返回 `{循环日: {第 n 个交易日: (个股收益数组, 全市场均值)}}`。数组与
    `picks[day]` 一一对应（只保留两端都有行的），全市场均值算不出来时是 nan。
    """
    curves: dict[date, dict[int, tuple[np.ndarray, float]]] = {}
    for day in [day for day in picks if day in matrix.index]:
        start = matrix.date_pos.get(day)
        if start is None:
            continue  # 这一天的行情不在矩阵里（调用方没把它算进 `scan_days`）
        calendar_pos = matrix.index[day]
        picked = np.array(
            [matrix.code_pos.get(code, -1) for code, _, _ in picks[day]], dtype=np.int64
        )
        index_in = picked[picked >= 0]
        entry_present = matrix.present[start]
        has_entry = entry_present[index_in] if index_in.size else np.array([], dtype=bool)
        curve: dict[int, tuple[np.ndarray, float]] = {}
        # 累乘推进而不是每个 n 重算一遍乘积：30 个循环 × 30 个点要算 900 次，
        # 每次重乘等于把整个矩阵乘 900 遍
        accumulated = np.ones(matrix.growth.shape[1], dtype=np.float64)
        for step in range(1, track_days + 1):
            shift = calendar_pos + step
            if shift > matrix.end_pos:
                break  # 这一天的行情还没到（或还没采），循环到这儿为止
            when = matrix.calendar[shift]
            position = matrix.date_pos.get(when)
            if position is None:
                continue  # 不该发生：`_matrix` 已按并集把整段区间都取进来了
            accumulated = accumulated * matrix.growth[position]
            total = accumulated - 1.0
            if not matrix.complete[position]:
                # 这一天**没采全**（不是「一行都没有」，也包括只采了几十只那种）：
                # 拿它算「全市场等权」就是拿几十只票代表全市场，超额会偏得很远；而复利
                # 又会把它当成真实交易日推进。所以宁可把循环停在这里，页面显示「进度 N/30」。
                # 补全那天的日线之后这里自然接得上。
                #
                # ⚠️ 拦的只是「交易日、但覆盖不够」：**休市日根本不在交易日历里**
                # （2026-09-25 中秋就是，它不会被当成缺口、也不需要补），真会走到这里的
                # 是「配额让路跳过了整天的日线采集」「那次采集失败」或「采集还没跑完」。
                logger.debug(
                    "%s 的第 %d 个交易日（%s）覆盖不足（%d 行），循环统计到此前为止",
                    day,
                    step,
                    when,
                    int(matrix.present[position].sum()),
                )
                break
            usable = entry_present & matrix.present[position]
            market = float(total[usable].mean()) if usable.any() else float("nan")
            if index_in.size:
                ok = has_entry & matrix.present[position, index_in]
                values = total[index_in[ok]]
            else:
                values = np.array([])
            curve[step] = (values, market)
        if curve:
            curves[day] = curve
    return curves


def _curves(
    session: Session,
    picks: dict[date, list[str]],
    track_days: int,
) -> dict[date, dict[int, tuple[np.ndarray, float]]]:
    """每个循环在每个 n 上的收益，以及**它自己那天**的全市场均值。

    口径说明见 `_matrix` / `_curves_from`（2026-10-09 拆成两半，好让 `pool_standing`
    六个池子共用同一份矩阵）。**签名与返回结构与拆分前一致**。
    """
    matrix = _matrix(session, list(picks), track_days)
    if matrix is None:
        return {}
    return _curves_from(matrix, picks, track_days)


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

    # 再按**整个市场采全了没有**截断：某天覆盖不足（交易日历里有、但采集被配额让路跳过、
    # 失败、或还没跑完）就停在它之前。不这么做的话，那天会显示成整列「—」，读起来像
    # 「当天全市场没涨跌」，而实际是「我们只采了几十只」。`track()` 用的是同一条规则
    # （`_COMPLETE_DAY_RATIO`），两边进度才对得上。（休市日不在这里 —— 它不在交易日历里。）
    if days:
        # ⚠️ 阈值要用**市场级**的最大行数（2026-10-10 修）：只按 `days` 里的最大值算的话，
        # 当这整个窗口恰好都是「采集没跑完」的残缺日（线上收盘后到采集跑完之间就是这样），
        # 最大行数也只有几十 → 阈值随之变小 → 判成「都完整」而不截断，进度就比 `track()` 大。
        # `track()` 的 `_matrix` 把命中日（扫描日，通常是完整的）也算进 `needed` 再取最大，
        # 所以这里同样把 `day` 并进来取最大，两边的进度才对得上。
        count_days = [day, *days]
        counts = dict(
            session.execute(
                select(StockDaily.trade_date, func.count())
                .where(StockDaily.trade_date.in_(count_days))
                .group_by(StockDaily.trade_date)
            ).all()
        )
        threshold = (max(counts.values()) if counts else 0) * _COMPLETE_DAY_RATIO
        for offset, when in enumerate(days):
            if counts.get(when, 0) < threshold:
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


# ---------------------------------------------------------------- 悟道池子「成绩单」

#: 要单独发成绩单的池子。形态页那份跟踪是**全形态混合的评分前 50**，回答不了
#: 「这个池子值不值得留」—— 「缩量洗盘中」一天几百只，本来也进不了前 50。
POOL_KEYS: tuple[str, ...] = (
    "wudao_sample",
    "wudao_start",
    "wudao_wash2",
    "huabao_early",
    "pile_wash_ready",
    "pile_wash_wash",
)

#: 收益档位（交易日）。与形态页那份一样给四档：短周期有没有指向、中期会不会还回去。
HORIZONS: tuple[int, ...] = (1, 3, 5, 10)

#: 「要过的那条线」（到线率）与「作废位」（破位率）在 `key_levels` 里的字段名，
#: 按顺序取第一个存在的。线上方的语义：watch_high=今高/洗盘高、breakout=昨高/洗盘高、
#: wash_high=洗盘高；wash_low / start_low 就是纪律里写明的那两个「作废位」。
_LINE_FIELDS = ("watch_high", "breakout", "wash_high")
_STOP_FIELDS = ("wash_low", "start_low")

#: 破位率的两种口径 —— **必须回给前端显示**，两种看起来一样就没法解释数字了。
STOP_FROM_LEVEL = "作废位"
STOP_FROM_LOW = "命中日最低价（代理）"


def _levels(key_levels: dict | None) -> tuple[float | None, float | None]:
    """从 `key_levels` 里取出（要过的线, 作废位）。缺失就是 None。"""
    data = key_levels or {}
    line = next((float(data[key]) for key in _LINE_FIELDS if data.get(key)), None)
    stop = next((float(data[key]) for key in _STOP_FIELDS if data.get(key)), None)
    return line, stop


def _line_rate(
    session: Session,
    hits: dict[tuple[date, str], tuple[float | None, float | None]],
    days: int,
) -> dict:
    """到线率 / 破位率：命中后 `days` 个交易日里，**收盘**站上「线」、跌破「作废位」的比例。

    四个口径细节，错一个数字就没法解释：

    · 用**收盘**而不是最高/最低 —— 站上一条线要收盘站住才算数，影线穿一下不算
      （与判定本身「收盘 ≥ 昨高 × 0.98」同一取向）。
    · **两端都要有行**才算样本（命中日 + 窗口里至少一天）：长期停牌、退市的票不该按
      「没破位」计入，那会把破位率压低（与 `_curves` 同一条规矩）。
    · **窗口得先走完**才算样本（2026-10-09 加）：只有 1 天可看的命中，破位的「机会」
      天然比走完 5 天的少，混在一起会把比例压低。这与 `_curves`「没走到第 n 个交易日
      就不返回那个点」是同一条规矩 —— 宁可这列先空着，也不出一个掺了半截窗口的数。
    · **复权口径必须统一**（2026-10-10 加）：`key_levels` 是命中日锚定的**前复权价**
      （= 命中日原始价），而窗口里的库内收盘是**不复权**价 —— 窗口内除权（送转）会让
      两者错基准、误记破位或未到线。所以窗口收盘用真实涨跌幅（已按除权调整）从命中日
      收盘逐日复利出「命中日口径」的等价价再比（见下面循环里的说明）。
    · 作废位优先用纪律里那个（`wash_low` / `start_low`）；池子没有的话退回
      **命中日最低价**当代理，并在 `stop_source` 里写明用的是哪种 —— 页面照原样显示。
    """
    blank = {
        "samples": 0,
        "touch": None,
        "touch_samples": 0,
        "stop": None,
        "stop_source": STOP_FROM_LOW,
    }
    if not hits:
        return blank

    calendar = list(
        session.scalars(select(TradeCalendar.trade_date).order_by(TradeCalendar.trade_date))
    )
    position = {day: index for index, day in enumerate(calendar)}
    data_end = session.scalar(select(func.max(StockDaily.trade_date)))
    if data_end is None or data_end not in position:
        return blank
    elapsed = position[data_end]
    scan_positions = [position[day] for day, _ in hits if day in position]
    if not scan_positions:
        return blank
    end_pos = min(max(scan_positions) + days, len(calendar) - 1)
    first, last = calendar[min(scan_positions)], calendar[end_pos]

    series: dict[str, dict[date, tuple[float | None, float | None, float | None]]] = defaultdict(dict)
    rows = session.execute(
        select(
            StockDaily.code,
            StockDaily.trade_date,
            StockDaily.close,
            StockDaily.low,
            StockDaily.pct_chg,
        ).where(
            StockDaily.code.in_({code for _, code in hits}),
            StockDaily.trade_date >= first,
            StockDaily.trade_date <= last,
        )
    ).all()
    for code, when, close, low, pct_value in rows:
        series[code][when] = (close, low, pct_value)

    touched = touch_total = stopped = stop_total = 0
    has_stop_level = any(stop is not None for _, stop in hits.values())
    for (day, code), (line, stop) in hits.items():
        start = position.get(day)
        bars = series.get(code) or {}
        entry = bars.get(day)
        if start is None or entry is None:
            continue  # 命中日本身没有行 —— 不计入样本（见上面第二条）
        if start + days > elapsed:
            continue  # 窗口还没走完 —— 不编造（见上面第三条）
        window = calendar[start + 1 : start + days + 1]
        # ⚠️ 口径必须统一（2026-10-10 修）：`key_levels`（要过的线 / 作废位）是**命中日
        # 锚定的前复权价**（`build_bars` 锚在最后一根收盘 = 命中日，所以它就是命中日原始价），
        # 而 `stock_daily` 存的是**不复权**收盘价 —— 窗口里一旦除权（尤其送转），直接比就会
        # 误记破位 / 「未到线」。这里用真实涨跌幅（iFinD 已按除权调整）从命中日收盘**逐日复利**
        # 出「命中日口径」的等价收盘价，与 `key_levels` 同基准（与 `_curves` 同一套复利口径）。
        # 停牌日涨跌幅为空 → 记 0、不算一跳；缺行则跳过（两端都要有行，见第二条）。
        closes: list[float] = []
        entry_close = entry[0]
        if entry_close is not None:
            cumulative = 1.0
            for when in window:
                bar = bars.get(when)
                if bar is None:
                    continue
                if bar[2] is not None:
                    cumulative *= 1.0 + bar[2] / 100.0
                closes.append(entry_close * cumulative)
        if not closes:
            continue
        if line is not None:
            touch_total += 1
            if any(value >= line for value in closes):
                touched += 1
        floor = stop if stop is not None else entry[1]
        if floor is None:
            continue
        stop_total += 1
        if any(value < floor for value in closes):
            stopped += 1

    return {
        # `samples` 是**破位率**的样本数（到线率那一列用 `touch_samples`，两者可能不等：
        # 池子没有「要过的线」时不算到线，但破位照样算）
        "samples": stop_total,
        "touch": round(touched / touch_total * 100, 1) if touch_total else None,
        "touch_samples": touch_total,
        "stop": round(stopped / stop_total * 100, 1) if stop_total else None,
        "stop_source": STOP_FROM_LEVEL if has_stop_level else STOP_FROM_LOW,
    }


def pool_standing(
    session: Session,
    *,
    start: date,
    cohorts: int = DEFAULT_COHORTS,
    hold_days: int = max(HORIZONS),
    line_days: int = 5,
) -> dict:
    """按池子发成绩单：收益（1/3/5/10 日）+ 到线率 + 破位率。

    一句话说清它回答什么：**「每天照这个池子全买，之后会怎样」**。形态页那份跟踪回答的是
    「评分前 50 只怎么样」，而悟道这些池子（尤其是几百只的安静型池子）根本进不了前 50，
    必须按池子单独算 —— 这也是「这个池子值不值得留」的唯一依据。

    ⚠️ `start` 要传 `Settings.pool_track_start`（＝2026-10-08，候选池定稿那天）：
    更早的命中是**另一套候选口径**下的名单，混进来会把两个分布平均掉。
    想统计更早的日子，正确做法是用现在的判定把那些天**重扫一遍**（幂等、零配额），
    不是把起点往前挪。

    六池共用**同一份行情矩阵**（`_matrix` 拉一次、`_curves_from` 各算一遍），
    否则同一份 30 万行要读六次。
    """
    names = {pattern.key: pattern.name for pattern in PATTERNS}
    scan_days = _scan_dates(session, cohorts, start)
    base = {
        "start": start.isoformat(),
        "cohorts": len(scan_days),
        "scan_days": [day.isoformat() for day in scan_days],
        "track_days": hold_days,
        "line_days": line_days,
    }
    if not scan_days:
        return {**base, "pools": []}

    rows = session.execute(
        select(
            PatternHit.trade_date,
            PatternHit.code,
            PatternHit.name,
            PatternHit.pattern,
            PatternHit.score,
            PatternHit.key_levels,
        ).where(PatternHit.trade_date.in_(scan_days), PatternHit.pattern.in_(POOL_KEYS))
    ).all()

    picks: dict[str, dict[date, list[Pick]]] = defaultdict(lambda: defaultdict(list))
    levels: dict[str, dict[tuple[date, str], tuple[float | None, float | None]]] = defaultdict(dict)
    for day, code, name, pattern, score, key_levels in rows:
        picks[pattern][day].append((code, name, float(score)))
        levels[pattern][(day, code)] = _levels(key_levels)

    matrix = _matrix(session, scan_days, hold_days)
    pools: list[dict] = []
    for key in POOL_KEYS:
        pool_picks = picks.get(key) or {}
        entry = {
            "pattern": key,
            "name": names.get(key, key),
            "hits": sum(len(items) for items in pool_picks.values()),
            "stocks": len({code for items in pool_picks.values() for code, _, _ in items}),
            "scan_days": len(pool_picks),
            "returns": [],
            **(_line_rate(session, levels.get(key) or {}, line_days)),
        }
        if matrix is not None and pool_picks:
            curves = _curves_from(matrix, pool_picks, hold_days)
            for horizon in HORIZONS:
                stat = _pooled_stats(
                    [curve[horizon] for curve in curves.values() if horizon in curve]
                )
                entry["returns"].append({"days": horizon, **stat})
        else:
            for horizon in HORIZONS:
                entry["returns"].append({"days": horizon, **_stats(np.array([]), float("nan"))})
        pools.append(entry)
    return {**base, "pools": pools}
