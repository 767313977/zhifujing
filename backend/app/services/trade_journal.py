"""交易日志的读取与统计：用 `stock_daily` 现算收益 / 回撤 / 持有天数 / 止损。

分工：`api/journal.py` 只做校验与增删改，这里负责「一笔交易到底怎么样」。
指标**不落库**，改算法不用回补历史（与形态跟踪同一思路）。

口径（页面上的说明与这里保持一致）：
- **持有天数**：买入日到卖出日之间的交易日数，**含买入日与卖出日**（当天买卖 = 1）；
  未平仓算到库里最后一个有日线的交易日。
- **收益**：已平仓用 `卖出价 / 买入价 − 1`；未平仓用**最新收盘**做浮动盈亏。
- **最大回撤**：持仓期间**收盘价**相对买入价的最大跌幅（≤0），不是「峰值到谷值」。
- **止损是否触发**：持仓期间有没有收盘价跌破 `plan_stop`；`stop_executed` 表示
  触发后是否真的在止损位或更低卖出（没卖出 → False，没触发 → None）。
"""

import logging
from collections import defaultdict
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import FEE_BUY, FEE_SELL
from app.models import (
    LimitPool,
    MarketSentiment,
    StockBasic,
    StockDaily,
    TradeCalendar,
    TradeJournal,
    TradeT,
)
from app.services import relay_temp
from app.services.pattern_track import POOL_KEYS
from app.services.patterns import PATTERNS

logger = logging.getLogger(__name__)

PATTERN_NAMES = {pattern.key: pattern.name for pattern in PATTERNS}
POOL_NAMES = {
    pattern.key: pattern.name for pattern in PATTERNS if pattern.key in POOL_KEYS
}
REASON_TYPES = ("pattern", "wudao", "theme", "other")

#: 一次做 T 的费率（分数，不是百分数）：佣金买卖各万 2.5，印花税 0.05% 只在卖出收。
#: 没记股数，所以不算「最低 5 元」——见 `config.FEE_BUY` 的说明。
FEE_ROUND_TRIP = FEE_BUY + FEE_SELL

#: 做 T 的方向与触发类型（与 `models.TradeT` 的注释、前端下拉一一对应）
T_DIRECTIONS = ("long", "short")
T_DIRECTION_LABELS = {"long": "正T", "short": "倒T"}
T_TRIGGERS = ("oversold", "intraday_high", "theme_fade", "other")
T_TRIGGER_LABELS = {
    "oversold": "超跌",
    "intraday_high": "分时冲高",
    "theme_fade": "题材退潮",
    "other": "其他",
}


def reason_label(reason_type: str | None, reason_key: str | None, reason_text: str | None) -> str:
    """理由的中文名。形态 / 悟道池查注册表，题材用 key，其它用自由文本。"""
    if reason_type == "pattern":
        return PATTERN_NAMES.get(reason_key or "", reason_key or "未填写")
    if reason_type == "wudao":
        return POOL_NAMES.get(reason_key or "", reason_key or "未填写")
    if reason_type == "theme":
        return (reason_key or reason_text or "题材").strip() or "题材"
    # `other` 也认 reason_key：早先「具体理由」只有一个输入框、存进了 key，
    # 只看 text 会把那批老记录显示成「其他」（2026-10-21 清单 #2）
    return (reason_text or reason_key or "其他").strip() or "其他"


def _calendar_position(session: Session) -> dict[date, int]:
    days = session.scalars(select(TradeCalendar.trade_date).order_by(TradeCalendar.trade_date))
    return {day: index for index, day in enumerate(days)}


def _bars(
    session: Session, codes: set[str], start: date
) -> dict[str, list[tuple[date, float, float | None]]]:
    """`{code: [(交易日, 不复权收盘, 涨跌幅)]}`，只取有收盘价的行（停牌残行跳过）。

    涨跌幅是**已按除权调整的真实收益率**，`_metrics` 靠它逐日复利出「买入日口径」的
    等价价 —— 直接用不复权收盘价遇到 10 送 10 会凭空算出 −50%。
    """
    if not codes:
        return {}
    result: dict[str, list[tuple[date, float, float | None]]] = defaultdict(list)
    rows = session.execute(
        select(
            StockDaily.code,
            StockDaily.trade_date,
            StockDaily.close,
            StockDaily.pct_chg,
        ).where(
            StockDaily.code.in_(codes),
            StockDaily.trade_date >= start,
            StockDaily.close.is_not(None),
        )
    ).all()
    for code, day, close, pct_chg in rows:
        result[code].append(
            (day, float(close), float(pct_chg) if pct_chg is not None else None)
        )
    for values in result.values():
        values.sort(key=lambda item: item[0])
    return result


def _sectors(session: Session, codes: set[str]) -> dict[str, str]:
    """`{code: 板块}`：同花顺三级行业路径的末段（与个股异动页同一口径）。"""
    if not codes:
        return {}
    rows = session.execute(
        select(StockBasic.code, StockBasic.industry).where(StockBasic.code.in_(codes))
    ).all()
    result: dict[str, str] = {}
    for code, industry in rows:
        text = (industry or "").strip()
        # 同花顺三级行业是「电子-半导体-集成电路」这种**短横线**分隔的路径，
        # 以前按 "/" 切，整条路径原样显示在「板块」列里（2026-10-21 清单 #1）
        result[code] = text.split("-")[-1] if text else "未知"
    return result


def _sentiment_levels(session: Session, days: int = 400) -> dict[date, str]:
    """`{交易日: 接力温度档位}`，用于「按情绪阶段分组」。

    复用 `relay_temp` 的现算口径（不落库）；只取最近 `days` 天 —— 交易日志的
    买入日不会早到几百天以前，再往前算纯属浪费。
    """
    rows = list(
        session.scalars(
            select(MarketSentiment)
            .order_by(MarketSentiment.trade_date.desc())
            .limit(days)
        )
    )[::-1]
    if not rows:
        return {}
    start = rows[0].trade_date
    up_pool: dict[date, set[str]] = defaultdict(set)
    first_board: dict[date, set[str]] = defaultdict(set)
    pool_rows = session.execute(
        select(LimitPool.trade_date, LimitPool.code, LimitPool.consecutive).where(
            LimitPool.trade_date >= start
        )
    ).all()
    for day, code, consecutive in pool_rows:
        up_pool[day].add(code)
        if consecutive == 1:
            first_board[day].add(code)
    series = relay_temp.build_series(rows, up_pool, first_board)
    return {item["trade_date"]: item["level"] for item in series if item["level"]}


def _adjust(window: list[tuple[date, float, float | None]]) -> list[tuple[date, float]]:
    """把底仓窗口的收盘价按 `pct_chg` 逐日复利成「买入日口径」的等价价。

    窗口第一行必须是买入日（`_metrics` 已经校验过）。这样窗口里发生 10 送 10 时，
    等价价不会腰斩，回撤 / 止损 / 做 T 的降本也不会被算成假亏损。

    做 T 的成交价是当天的不复权价，换算到买入日口径用
    `price × adjusted[day] / close[day]`，所以这里必须同时留下原始收盘价（见 `_bars`）。
    """
    entry_close = window[0][1]
    cumulative = 1.0
    adjusted: list[tuple[date, float]] = []
    for index, (day, _close, pct) in enumerate(window):
        if index > 0 and pct is not None:
            cumulative *= 1.0 + pct / 100.0
        adjusted.append((day, entry_close * cumulative))
    return adjusted


def _end_basis(
    trade: TradeJournal,
    window: list[tuple[date, float, float | None]],
    adjusted: list[tuple[date, float]],
) -> float:
    """期末价的「买入日口径」：已平仓用卖出价换算，未平仓用最新等价收盘价。"""
    last_adjusted = adjusted[-1][1]
    exit_row = next((row for row in window if row[0] == trade.exit_date), None)
    if trade.exit_price is not None and exit_row is not None and exit_row[1] > 0:
        # 卖出价是实际成交价（不复权），换成「买入日口径」再和买入价比
        return last_adjusted * (float(trade.exit_price) / exit_row[1])
    return last_adjusted


def _window(
    trade: TradeJournal,
    bars: list[tuple[date, float, float | None]],
    data_end: date | None,
) -> list[tuple[date, float, float | None]]:
    """底仓的持仓窗口：买入日 ~ 卖出日（未平仓到库里最后一根日线）。"""
    end_day = trade.exit_date or data_end
    if end_day is None:
        return []
    return [
        (day, close, pct)
        for day, close, pct in bars
        if trade.entry_date <= day <= end_day
    ]


def _metrics(
    trade: TradeJournal,
    bars: list[tuple[date, float, float | None]],
    calendar_pos: dict[date, int],
    data_end: date | None,
) -> dict:
    """算一笔交易的指标；日线不够时返回 `metrics_note` 说明原因。"""
    blank = {
        "holding_days": None,
        "return_pct": None,
        "max_drawdown_pct": None,
        "stop_hit": None,
        "stop_executed": None,
        "open": trade.exit_date is None,
        "latest_date": None,
        #: 最新一根日线的**不复权**收盘价（持仓行显示「最新价」用，与行情软件一致）
        "latest_close": None,
        "metrics_note": None,
    }
    start_pos = calendar_pos.get(trade.entry_date)
    end_day = trade.exit_date or data_end
    end_pos = calendar_pos.get(end_day) if end_day else None
    if start_pos is None:
        return {**blank, "metrics_note": "买入日不在交易日历里"}
    if end_day is None or end_pos is None:
        return {**blank, "metrics_note": "卖出日不在交易日历里"}

    window = _window(trade, bars, data_end)
    if not window or window[0][0] != trade.entry_date:
        return {**blank, "metrics_note": "买入日没有日线（停牌或未采集）"}
    entry_close = window[0][1]
    if entry_close <= 0 or trade.entry_price <= 0:
        return {**blank, "metrics_note": "买入日收盘价无效"}

    # 以**买入日收盘**为基准，按 pct_chg 逐日复利出「买入日口径」的等价收盘价。
    adjusted = _adjust(window)
    basis = _end_basis(trade, window, adjusted)

    drawdown = min(
        0.0, min(value for _, value in adjusted) / trade.entry_price - 1.0
    )

    stop_hit = None
    stop_executed = None
    if trade.plan_stop is not None:
        stop_hit = any(value < trade.plan_stop for _, value in adjusted)
        if stop_hit:
            stop_executed = (
                trade.exit_price is not None and trade.exit_price <= trade.plan_stop
            )

    return {
        "holding_days": end_pos - start_pos + 1,
        "return_pct": round((basis / trade.entry_price - 1.0) * 100, 2),
        "max_drawdown_pct": round(drawdown * 100, 2),
        "stop_hit": stop_hit,
        "stop_executed": stop_executed,
        "open": trade.exit_date is None,
        "latest_date": window[-1][0],
        "latest_close": window[-1][1],
        "metrics_note": None,
    }


def _day_ranges(
    session: Session, codes: set[str], days: set[date]
) -> dict[tuple[str, date], tuple[float, float]]:
    """`{(code, 交易日): (最低, 最高)}`，只用于做 T 的软提示（价格是否在当日区间内）。"""
    if not codes or not days:
        return {}
    rows = session.execute(
        select(
            StockDaily.code,
            StockDaily.trade_date,
            StockDaily.low,
            StockDaily.high,
        ).where(StockDaily.code.in_(codes), StockDaily.trade_date.in_(days))
    ).all()
    result: dict[tuple[str, date], tuple[float, float]] = {}
    for code, day, low, high in rows:
        if low is not None and high is not None:
            result[(code, day)] = (float(low), float(high))
    return result


def _t_metrics(
    trade: TradeJournal,
    rows: list[TradeT],
    window: list[tuple[date, float, float | None]],
    adjusted: list[tuple[date, float]],
    ranges: dict[tuple[str, date], tuple[float, float]],
) -> list[dict]:
    """把一笔底仓下面的做 T 记录算成指标（2026-10-21 做 T 记录需求 §3）。

    价格口径：底仓窗口的收盘价按 `pct_chg` 复利成「买入日口径」的等价价
    （`_adjust`），做 T 的成交价（不复权）按 `price × adjusted[day] / close[day]`
    换算过去 —— 中间遇到 10 送 10 时，降本不会算成腰斩。

    未完成（缺第二腿）的 T 用**期末价**顶上：正 T 没卖出 = 加仓，按期末价计盈亏；
    倒 T 没接回 = 减仓，卖飞的部分按期末价计机会损失（`fly_pct`）。

    `window` / `adjusted` 为空（买入日没有日线等）时，只给完成状态和软提示，指标留 None。
    """
    if adjusted:
        close_by_day = {day: close for day, close, _pct in window}
        adj_by_day = dict(adjusted)
        end_basis: float | None = _end_basis(trade, window, adjusted)
        end_price: float | None = (
            float(trade.exit_price)
            if trade.exit_date is not None and trade.exit_price is not None
            else window[-1][1]
        )
    else:
        close_by_day, adj_by_day = {}, {}
        end_basis, end_price = None, None

    fee_pct = FEE_ROUND_TRIP * 100  # 换成百分数，与 spread_pct 同一单位

    def to_basis(price: float, day: date | None) -> float | None:
        """不复权成交价 → 买入日口径；那天没有日线（停牌 / 未采集）返回 None。"""
        if day is None:
            return None
        close = close_by_day.get(day)
        base = adj_by_day.get(day)
        if close is None or base is None or close <= 0:
            return None
        return price * base / close

    def band_warning(label: str, price: float | None, day: date | None) -> str | None:
        if price is None or day is None:
            return None
        band = ranges.get((trade.code, day))
        if band is None:
            return None
        low, high = band
        if low - 1e-9 <= price <= high + 1e-9:
            return None
        return (
            f"{label} {price:.2f} 不在 {day.strftime('%m-%d')} 的"
            f"最低 {low:.2f} ~ 最高 {high:.2f} 之间"
        )

    result: list[dict] = []
    for row in rows:
        completed = row.buy_price is not None and row.sell_price is not None
        if row.direction == "short":
            # 倒 T：先卖（第一腿）后买（第二腿）
            sell_day, buy_day = row.trade_date, row.leg2_date
        else:
            buy_day, sell_day = row.trade_date, row.leg2_date

        if row.buy_price is not None and buy_day is not None:
            buy_basis = to_basis(float(row.buy_price), buy_day)
        else:
            buy_basis = end_basis
        if row.sell_price is not None and sell_day is not None:
            sell_basis = to_basis(float(row.sell_price), sell_day)
        else:
            sell_basis = end_basis

        spread_pct = net_pct = contrib_pct = fly_pct = None
        if buy_basis is not None and sell_basis is not None and buy_basis > 0:
            spread_pct = round((sell_basis / buy_basis - 1.0) * 100, 2)
            if completed:
                net_pct = round(spread_pct - fee_pct, 2)
            ratio = float(row.ratio_pct) / 100.0
            contrib_pct = round(
                ratio * (sell_basis - buy_basis) / trade.entry_price * 100
                - ratio * fee_pct,
                2,
            )
            if row.direction == "short" and not completed and end_basis is not None:
                fly_pct = round((end_basis / sell_basis - 1.0) * 100, 2)

        warnings = [
            text
            for text in (
                band_warning("买入价", row.buy_price, buy_day),
                band_warning("卖出价", row.sell_price, sell_day),
            )
            if text
        ]

        result.append(
            {
                "id": row.id,
                "trade_id": row.trade_id,
                "trade_date": row.trade_date,
                "leg2_date": row.leg2_date,
                "direction": row.direction,
                "buy_price": row.buy_price,
                "sell_price": row.sell_price,
                "ratio_pct": row.ratio_pct,
                "trigger": row.trigger,
                "note": row.note,
                "completed": completed,
                "spread_pct": spread_pct,
                "net_pct": net_pct,
                "contrib_pct": contrib_pct,
                "fly_pct": fly_pct,
                "success": bool(completed and net_pct is not None and net_pct > 0),
                "end_price": end_price,
                "warnings": warnings,
            }
        )
    return result


def load_enriched(session: Session, user_id: int) -> list[dict]:
    """该用户的全部交易（含现算指标），按买入日倒序。"""
    trades = list(
        session.scalars(
            select(TradeJournal)
            .where(TradeJournal.user_id == user_id)
            .order_by(TradeJournal.entry_date.desc(), TradeJournal.id.desc())
        )
    )
    if not trades:
        return []

    codes = {trade.code for trade in trades}
    start = min(trade.entry_date for trade in trades)
    bars = _bars(session, codes, start)
    calendar_pos = _calendar_position(session)
    data_end = session.scalar(select(func.max(StockDaily.trade_date)))
    levels = _sentiment_levels(session)
    sectors = _sectors(session, codes)

    # 做 T 明细一次取全：按底仓分组，顺便收齐要查 [low, high] 的日期
    t_rows = list(
        session.scalars(
            select(TradeT)
            .where(TradeT.user_id == user_id)
            .order_by(TradeT.trade_date, TradeT.id)
        )
    )
    t_by_trade: dict[int, list[TradeT]] = defaultdict(list)
    t_days: set[date] = set()
    for row in t_rows:
        t_by_trade[row.trade_id].append(row)
        t_days.add(row.trade_date)
        if row.leg2_date is not None:
            t_days.add(row.leg2_date)
    ranges = _day_ranges(session, codes, t_days)

    result: list[dict] = []
    for trade in trades:
        trade_bars = bars.get(trade.code, [])
        metrics = _metrics(trade, trade_bars, calendar_pos, data_end)
        window = _window(trade, trade_bars, data_end)
        adjusted = (
            _adjust(window)
            if window and window[0][0] == trade.entry_date and window[0][1] > 0
            else []
        )
        t_ops = _t_metrics(
            trade, t_by_trade.get(trade.id, []), window, adjusted, ranges
        )
        t_contrib = [item["contrib_pct"] for item in t_ops if item["contrib_pct"] is not None]
        t_contrib_pct = round(sum(t_contrib), 2) if t_contrib else None
        return_pct = metrics["return_pct"]
        result.append(
            {
                "id": trade.id,
                "code": trade.code,
                "name": trade.name,
                "entry_date": trade.entry_date,
                "entry_price": trade.entry_price,
                "exit_date": trade.exit_date,
                "exit_price": trade.exit_price,
                "position_pct": trade.position_pct,
                "reason_type": trade.reason_type,
                "reason_key": trade.reason_key,
                "reason_text": trade.reason_text,
                "reason_label": reason_label(
                    trade.reason_type, trade.reason_key, trade.reason_text
                ),
                "plan_stop": trade.plan_stop,
                "note": trade.note,
                "sentiment_level": levels.get(trade.entry_date),
                "sector": sectors.get(trade.code, "未知"),
                **metrics,
                "t_ops": t_ops,
                "t_count": len(t_ops),
                "t_contrib_pct": t_contrib_pct,
                "t_open_count": sum(1 for item in t_ops if not item["completed"]),
                # 含 T 收益：`return_pct` 的原口径不动，成绩单 / 胜率仍只看波段收益
                "return_with_t_pct": (
                    round(return_pct + t_contrib_pct, 2)
                    if return_pct is not None and t_contrib_pct is not None
                    else return_pct
                ),
                "created_at": trade.created_at,
                "updated_at": trade.updated_at,
            }
        )
    return result


def _group_stats(items: list[dict]) -> dict:
    closed = [
        item
        for item in items
        if item["exit_date"] is not None and item["return_pct"] is not None
    ]
    wins = sum(1 for item in closed if item["return_pct"] > 0)
    returns = [item["return_pct"] for item in closed]
    drawdowns = [
        item["max_drawdown_pct"]
        for item in closed
        if item["max_drawdown_pct"] is not None
    ]
    holding = [
        item["holding_days"] for item in closed if item["holding_days"] is not None
    ]
    return {
        "trades": len(items),
        "closed": len(closed),
        "open": sum(1 for item in items if item["exit_date"] is None),
        "win_rate": round(wins / len(closed) * 100, 1) if closed else None,
        "avg_return": round(sum(returns) / len(returns), 2) if returns else None,
        "avg_drawdown": (
            round(sum(drawdowns) / len(drawdowns), 2) if drawdowns else None
        ),
        "avg_holding_days": (
            round(sum(holding) / len(holding), 1) if holding else None
        ),
    }


def _t_group_stats(items: list[dict]) -> dict:
    """做 T 的分组成绩。

    **成功率与平均价差只看已完成的**（两腿都有成交价）；卖飞（倒 T 没接回）单独统计，
    不算进失败。「未完成」的按期末价计贡献，所以 `total_contrib_pct` 仍算它们。
    """
    completed = [item for item in items if item["completed"]]
    nets = [item["net_pct"] for item in completed if item["net_pct"] is not None]
    contribs = [item["contrib_pct"] for item in items if item["contrib_pct"] is not None]
    fly = [item["fly_pct"] for item in items if item["fly_pct"] is not None]
    wins = sum(1 for item in completed if item["success"])
    return {
        "count": len(items),
        "completed": len(completed),
        "open": len(items) - len(completed),
        "success_rate": (
            round(wins / len(completed) * 100, 1) if completed else None
        ),
        "avg_net_pct": round(sum(nets) / len(nets), 2) if nets else None,
        "total_contrib_pct": round(sum(contribs), 2) if contribs else None,
        "fly_count": len(fly),
        "avg_fly_pct": round(sum(fly) / len(fly), 2) if fly else None,
    }


def _t_stats(rows: list[dict]) -> dict:
    """把 `t_ops` 摊平，按方向 / 按触发各出一张成绩表。"""
    flat = [item for row in rows for item in row["t_ops"]]
    return {
        "summary": {"label": "全部做T", **_t_group_stats(flat)},
        "by_direction": [
            {
                "label": T_DIRECTION_LABELS[key],
                **_t_group_stats(
                    [item for item in flat if item["direction"] == key]
                ),
            }
            for key in T_DIRECTIONS
        ],
        "by_trigger": [
            {
                "label": T_TRIGGER_LABELS[key],
                **_t_group_stats([item for item in flat if item["trigger"] == key]),
            }
            for key in T_TRIGGERS
        ],
    }


def stats(session: Session, user_id: int) -> dict:
    """按理由 / 情绪档位 / 板块分组看自己的胜率与平均收益。

    **只统计已平仓**：未平仓的浮动盈亏会把「这类票到底赚不赚钱」搅浑；
    它们的笔数单独放在 `open` 里。
    """
    rows = load_enriched(session, user_id)

    def group(key) -> list[dict]:
        buckets: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            buckets[key(row)].append(row)
        result = [{"label": label, **_group_stats(items)} for label, items in buckets.items()]
        result.sort(
            key=lambda item: (
                item["avg_return"] is None,
                -(item["avg_return"] or 0.0),
            )
        )
        return result

    summary = _group_stats(rows)
    return {
        "summary": {"label": "全部交易", **summary},
        "by_reason": group(lambda item: item["reason_label"]),
        "by_sentiment": group(lambda item: item["sentiment_level"] or "未知"),
        "by_sector": group(lambda item: item["sector"] or "未知"),
        "t_stats": _t_stats(rows),
    }
