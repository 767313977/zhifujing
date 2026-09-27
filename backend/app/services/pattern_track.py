"""每日形态「评分前 N 只」的后续走势跟踪（胜率统计）。

形态页的默认视图是「今天命中里评分最高的 50 只」（`/api/patterns/hits` 不传 `pattern`
时的口径，也是每天补 DDE 的同一批）。这一页回答的是另一个问题：**这批票后来到底
涨没涨** —— 逐日算它们 1/3/5/10 个交易日后的收益与胜率，滚动看最近 30 个交易日。

零 iFinD 配额：全部用库里的日线在本地算（与回测脚本一样）。

## 三个口径必须先说清楚

1. **收益用 `pct_chg` 逐日复利，不用收盘价比值。** `stock_daily` 存的是**不复权价**，
   直接拿 `close[d+h] / close[d]` 算，遇到除权的票会凭空多出一个 −30% 甚至 −90% 的
   样本（形态引擎为了同一个问题，必须在内存里重建前复权序列）。iFinD 的涨跌幅是
   **已按除权调整的真实收益率**（实测 21 个银行除权日验证过），逐日复利得到的正是
   前复权口径下的区间收益。
2. **停牌日不算收益、但也不能缺行。** iFinD 对停牌股把四价填成相等、涨跌幅留空，
   所以复利时跳过空值即可（空值等价于 0 涨跌）。但**信号日与到期日这两天都要有该股
   的行**，否则这只票在这个持有期上没有样本 —— 长期停牌、退市的票不该按 0% 计入胜率，
   那会把胜率往 50% 拉。缺样本时 `samples` 会小于当天选出的只数，页面要如实显示。
3. **基准是「同一天全市场平均」**（与 `scripts/backtest_patterns.py` 同一口径）。
   不这么做的话，「涨了 5%」可能只是那几天大盘在涨。所以每档持有期都给
   **平均超额**与**跑赢比例**两个数。

## 样本口径：逐日，不去重

同一只票连着三天上榜就贡献三个样本（2026-09-27 用户定），因为它回答的是「每天照这份
清单买会怎样」。同时给出去重后的只数，便于知道重复程度。

## 窗口是「最近 N 个**有命中记录**的交易日」

不是自然日，也不是日历上的最近 N 个交易日：早期命中表只有零星几天（建站时手动扫描），
用它计算时窗口会比 30 天跨得更长。页面按实际取到的天数如实显示。
"""

import logging
from collections import defaultdict
from datetime import date

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PatternHit, StockDaily, TradeCalendar
from app.services.patterns import PATTERNS

logger = logging.getLogger(__name__)

# 默认窗口与入选只数：与形态页默认视图 / DDE 补齐那批保持同一口径
DEFAULT_DAYS = 30
DEFAULT_TOP = 50
DEFAULT_HORIZONS = (1, 3, 5, 10)

# 只统计**注册表里当前存在**的形态。少了这道过滤，被删形态的历史命中会混进
# 「评分前 50 只」（`/hits` 与 DDE 补齐都已经加了同一道过滤，口径必须一致）
_KEYS = tuple(pattern.key for pattern in PATTERNS)


def _scan_dates(session: Session, days: int) -> list[date]:
    """最近 `days` 个**有命中记录**的交易日，升序返回。"""
    rows = session.scalars(
        select(PatternHit.trade_date)
        .where(PatternHit.pattern.in_(_KEYS))
        .group_by(PatternHit.trade_date)
        .order_by(PatternHit.trade_date.desc())
        .limit(days)
    ).all()
    return sorted(rows)


def _picks(session: Session, days: list[date], top: int) -> dict[date, list[str]]:
    """每天的「评分前 `top` 只」，按票归并取最高分。

    ⚠️ 并列分数时的取舍与 `/hits` **不保证逐只一致**（那边是纯 SQL `order by score
    desc`，没有 tie-break，本身也是任意的；这里显式按代码升序兜底，至少**同一天同
    一份数据每次结果相同**）。差一两只不影响胜率统计，但别拿「第 50 位是谁」去对账。
    """
    best: dict[tuple[date, str], float] = {}
    if not days:
        return {}
    rows = session.execute(
        select(PatternHit.trade_date, PatternHit.code, PatternHit.score).where(
            PatternHit.trade_date.in_(days), PatternHit.pattern.in_(_KEYS)
        )
    ).all()
    for day, code, score in rows:
        key = (day, code)
        if score > best.get(key, -np.inf):
            best[key] = score

    grouped: dict[date, list[tuple[float, str]]] = defaultdict(list)
    for (day, code), score in best.items():
        grouped[day].append((score, code))

    picked: dict[date, list[str]] = {}
    for day, items in grouped.items():
        items.sort(key=lambda item: (-item[0], item[1]))
        picked[day] = [code for _, code in items[:top]]
    return picked


def _forward_returns(
    session: Session,
    picks: dict[date, list[str]],
    horizons: tuple[int, ...],
) -> dict[date, dict[int, tuple[np.ndarray, np.ndarray]]]:
    """算每只票在各持有期上的收益，同时带上**它自己那一天的**全市场均值。

    返回 `{信号日: {持有期: (收益数组, 逐样本基准数组)}}`，收益数组与 `picks[day]`
    一一对应、**不可用时为空数组**（次日/到期日缺行）。基准做成「逐样本对齐」是为了
    合计时能直接摊平 —— 用池子里的总基准均值去比，会在各天样本数不等时算错。
    """
    empty: dict = {}
    if not picks:
        return empty

    # 交易日历：持有期是「几个交易日」，得按日历往后数，不能用日历天加
    calendar = list(
        session.scalars(select(TradeCalendar.trade_date).order_by(TradeCalendar.trade_date))
    )
    index = {day: i for i, day in enumerate(calendar)}
    scan_days = [day for day in picks if day in index]
    if not scan_days:
        logger.warning("命中记录的日期都不在交易日历里，跳过跟踪统计")
        return empty

    # 需要哪些日期的行情：信号日 → 到期日（含起点，复利要用中间每一天）
    needed: set[date] = set()
    targets: dict[tuple[date, int], date] = {}
    for day in scan_days:
        start = index[day]
        for horizon in horizons:
            stop = start + horizon
            if stop >= len(calendar):
                continue  # 到期日还没到（日历尽头），这一档没有样本
            target = calendar[stop]
            targets[(day, horizon)] = target
            needed.update(calendar[start : stop + 1])
    if not needed:
        return empty

    dates = sorted(needed)
    date_pos = {day: i for i, day in enumerate(dates)}

    # 一次把窗口内的日线拉出来，就地压成两张矩阵：涨跌幅、有没有这一行（停牌行也会
    # 写库，但涨跌幅为空）。`yield_per` 避免把几十万行一次性拉进内存（一个 30 天窗口
    # 约 40 个交易日 × 5000 多只 ≈ 20 万行）。
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

    stocks: dict[date, dict[int, tuple[np.ndarray, np.ndarray]]] = {}
    growth = 1.0 + pct / 100.0
    for day in scan_days:
        start = date_pos[day]
        picked = np.array([code_pos.get(code, -1) for code in picks[day]], dtype=np.int64)
        stocks[day] = {}
        for horizon in horizons:
            target = targets.get((day, horizon))
            if target is None:
                continue
            stop = date_pos[target]
            if stop <= start:
                continue
            # 复利：中间每一天的 (1 + 涨跌幅) 连乘。停牌日涨跌幅为 0 → 贡献 1，正好
            total = np.prod(growth[start + 1 : stop + 1], axis=0) - 1.0
            # 基准：同一天全市场（信号日与到期日都有行）的等权平均
            usable = present[start] & present[stop]
            market = float(total[usable].mean()) if usable.any() else float("nan")
            index_in = picked[picked >= 0]
            if index_in.size:
                ok = present[start, index_in] & present[stop, index_in]
                values = total[index_in[ok]]
            else:
                values = np.array([])
            stocks[day][horizon] = (values, np.full(values.size, market))
    return stocks


def _stats(values: np.ndarray, market: np.ndarray) -> dict:
    """一档持有期的统计量。收益都是**百分数**（1.23 表示 +1.23%）。

    `market` 是**逐样本对齐**的全市场均值（每只票对应它自己信号日那天的基准）。
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
    ok = np.isfinite(market)
    if ok.any():
        diff = values[ok] - market[ok]
        out["excess"] = round(float(diff.mean()) * 100, 2)
        out["beat_pct"] = round(float((diff > 0).mean()) * 100, 1)
    return out


def track(
    session: Session,
    *,
    days: int = DEFAULT_DAYS,
    top: int = DEFAULT_TOP,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> dict:
    """滚动统计最近 `days` 个扫描日的「评分前 `top` 只」的后续走势。"""
    horizons = tuple(sorted({int(item) for item in horizons if int(item) > 0}))
    picks = _picks(session, _scan_dates(session, days), top)
    forward = _forward_returns(session, picks, horizons)
    days_desc = sorted(picks, reverse=True)

    rows = []
    unique: set[str] = set()
    total_stocks = 0
    pool: dict[int, list[np.ndarray]] = {horizon: [] for horizon in horizons}
    pool_market: dict[int, list[np.ndarray]] = {horizon: [] for horizon in horizons}
    for day in days_desc:
        unique.update(picks[day])
        total_stocks += len(picks[day])
        metrics = []
        for horizon in horizons:
            values, market = forward.get(day, {}).get(horizon, (np.array([]), np.array([])))
            metrics.append({"horizon": horizon, **_stats(values, market)})
            if values.size:
                pool[horizon].append(values)
                pool_market[horizon].append(market)
        rows.append({"trade_date": day, "stocks": len(picks[day]), "horizons": metrics})

    summary_metrics = []
    for horizon in horizons:
        # ⚠️ 合计不是「各天结果的再平均」：短持有期的样本数比长持有期多（最近几天还没
        # 走完 10 日），按天平均会把样本少的那几天权重放大。这里把所有样本摊平再算。
        values = np.concatenate(pool[horizon]) if pool[horizon] else np.array([])
        market = np.concatenate(pool_market[horizon]) if pool_market[horizon] else np.array([])
        summary_metrics.append({"horizon": horizon, **_stats(values, market)})

    return {
        "window_days": days,
        "top": top,
        "horizons": list(horizons),
        "summary": {
            "days": len(rows),
            "stocks": total_stocks,
            "unique": len(unique),
            "first_date": rows[-1]["trade_date"] if rows else None,
            "last_date": rows[0]["trade_date"] if rows else None,
            "horizons": summary_metrics,
        },
        "days": rows,
    }
