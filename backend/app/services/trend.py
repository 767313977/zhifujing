"""Minervini 趋势模板（8 条）—— **页面与回测共用这一份**。

原实现写在 `scripts/backtest_patterns.py` 里（2026-10-24 加的）。选股池也要用同一条
判据，两处各写一份必然漂移（`patterns.is_st` 那次就是教训），所以挪到这里，回测脚本
改成 import。

逐条对应（全部满足才算通过）：

1. 收盘 > MA150 且 > MA200
2. MA150 > MA200
3. MA200 比 20 个交易日前高（至少向上一个月）
4. MA50 > MA150 > MA200
5. 收盘 > MA50
6. 收盘 ≥ 52 周最低 × 1.3
7. 收盘 ≥ 52 周最高 × 0.75
8. RS ≥ 70 —— 用 250 日 RPS 分位（与站内 RPS 同口径），由调用方传进来

⚠️ 52 周按 **250 个交易日**算，所以 `i < 249` 一律不通过；主库只有 260 根日线，
只够判最后十来天，要下结论得用长历史回测。
"""

from collections import defaultdict
from datetime import date

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import StockDaily, TradeCalendar
from app.services.patterns import Bars, build_bars, usable_bar
from app.sources.base import chunked

#: 第 8 条的门槛（RS ≥ 70）
TREND_RS_MIN = 70.0

#: 「52 周」按交易日算
TREND_LOOKBACK = 250

#: 取数分批大小（与 `api/patterns`、`stock_phase` 同一约定：老 SQLite 单条 SQL 变量上限 999）
IN_BATCH = 900


def passes(bars: Bars, i: int, rs: float | None) -> bool:
    """第 `i` 根 K 线当天是否满足趋势模板。`rs` = 当天的 250 日 RPS 分位（0~100）。"""
    if i < TREND_LOOKBACK - 1 or rs is None:
        return False
    close, high, low = bars.close, bars.high, bars.low
    current = float(close[i])
    ma50 = float(np.mean(close[i - 49 : i + 1]))
    ma150 = float(np.mean(close[i - 149 : i + 1]))
    ma200 = float(np.mean(close[i - 199 : i + 1]))
    # MA200 的「20 个交易日前」= 窗口右端退到 i-20（含），即 [i-219, i-19)
    ma200_prev = float(np.mean(close[i - 219 : i - 19]))

    if not (current > ma150 and current > ma200 and ma150 > ma200):
        return False
    if ma200 <= ma200_prev:
        return False
    if not (ma50 > ma150 and current > ma50):
        return False

    low52 = float(np.min(low[i - 249 : i + 1]))
    high52 = float(np.max(high[i - 249 : i + 1]))
    if low52 <= 0 or high52 <= 0:
        return False
    if current < low52 * 1.3 or current < high52 * 0.75:
        return False

    return rs >= TREND_RS_MIN


def evaluate(bars: Bars, i: int, rs: float | None) -> bool | None:
    """趋势模板的三态判定：**「无法判断」是 None，不是 False**（2026-11-01，页面标签用）。

    - 可用日线不足 `TREND_LOOKBACK` 根、或当天没有 RPS250 → None；
    - 其余情况与 `passes` 完全一致（判据只有那一份）。

    `passes` 本身保持原样（回测依赖它对数据不足返回 False）；页面要区分「数据不够、
    不下结论」与「明确不满足」，否则新股会被全部打成 ✗。
    """
    if i < TREND_LOOKBACK - 1 or rs is None:
        return None
    return passes(bars, i, rs)


def load_bars(session: Session, codes: list[str], day: date) -> dict[str, Bars]:
    """给 `codes` 准备趋势模板要用的前复权 K 线（截至 `day`，每个代码一份）。

    窗口 = `day` 往前 `TREND_LOOKBACK + 10` 个交易日：`passes` 要 250 根**可用**日线，
    多留 10 天给停牌残行（`usable_bar` 会把它们剔掉）。取数按 `IN_BATCH` 分批 ——
    全池 5000 只时一次 `code IN (...)` 会超 SQLite 的变量上限。

    **只返回凑够 250 根的票**：不足的由调用方按「无法判断」（None）处理，不能当成
    「不满足」。选股池（`pools._filter_trend`）与形态页 `/hits` 共用这一份，不要写第三份。
    """
    wanted = sorted({code for code in codes if code})
    if not wanted:
        return {}
    days = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= day)
            .order_by(TradeCalendar.trade_date.desc())
            .limit(TREND_LOOKBACK + 10)
        )
    )
    if not days:
        return {}
    start = min(days)

    records: dict[str, list[dict]] = defaultdict(list)
    for batch in chunked(wanted, IN_BATCH):
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

    return {
        code: build_bars(items, code)
        for code, items in records.items()
        if len(items) >= TREND_LOOKBACK
    }
