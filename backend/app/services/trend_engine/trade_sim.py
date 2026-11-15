"""交易级回测：组合资金、仓位、T+1 成交、涨跌停/停牌、成本与多种退出规则。

与 `research_backtest`（信号研究内核）的区别：

- 本模块模拟**真实资金曲线**：现金、持仓、每日市值、换手；
- 信号日收盘后生成信号，**下一交易日开盘**才允许买入；
- 卖出决策在收盘后做出，**下一交易日开盘**执行（不假设当日收盘能成交）；
- 涨停开盘不能买、跌停开盘不能卖；停牌顺延并在超时后标记；
- 佣金（双边）、印花税（卖出）、滑点（双边）全部计入；
- 组合约束：最大持仓数、单股目标仓位、现金不足按比例分配、100 股整手。

本模块不查库，输入是 `bars_by_code`（每只票按日期升序的 OHLCV）与信号列表，
便于单元测试与离线复现。所有价格必须是**前复权**序列。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import numpy as np

from app.services import limit_rules


@dataclass(frozen=True, slots=True)
class DailyBar:
    open: float
    high: float
    low: float
    close: float
    pct: float | None
    amount: float | None
    volume: float | None


@dataclass(frozen=True, slots=True)
class Signal:
    code: str
    day: date
    score: float = 0.0


@dataclass(frozen=True, slots=True)
class SimConfig:
    initial_cash: float = 1_000_000.0
    max_positions: int = 10
    position_pct: float = 0.10
    #: 成本（bps）：佣金双边、印花税仅卖出、滑点双边
    commission_bps: float = 3.0
    stamp_bps: float = 10.0
    slippage_bps: float = 10.0
    min_commission: float = 5.0
    #: 退出规则
    horizon: int = 20
    use_atr_stop: bool = False
    atr_window: int = 14
    atr_stop_multiple: float = 2.0
    use_trailing: bool = False
    trailing_multiple: float = 3.0
    use_ma_exit: bool = False
    ma_exit_window: int = 20
    use_structure_exit: bool = False
    structure_days: int = 40
    use_stagnation_exit: bool = False
    max_entry_delay: int = 5
    max_exit_delay: int = 10


@dataclass
class Position:
    code: str
    shares: int
    entry_date: date
    entry_price: float
    stop_price: float | None = None
    peak_close: float = 0.0
    entry_index: int = 0
    hold_days: int = 0
    last_price: float = 0.0


@dataclass
class Trade:
    code: str
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    return_pct: float
    reason: str
    hold_days: int


@dataclass
class SimResult:
    equity: list[tuple[date, float]] = field(default_factory=list)
    #: 每日「持仓市值 / 总权益」（0~1），用于计算平均暴露
    exposure: list[float] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    skipped: dict = field(default_factory=dict)


class Series:
    """一只票的日期索引与预算指标（MA / ATR / 结构低点 / 量能比）。"""

    def __init__(self, code: str, bars: dict[date, DailyBar]) -> None:
        self.code = code
        self.days = sorted(bars)
        self.bars = bars
        self.index = {day: pos for pos, day in enumerate(self.days)}
        close = np.asarray([bars[day].close for day in self.days], dtype=float)
        high = np.asarray([bars[day].high for day in self.days], dtype=float)
        low = np.asarray([bars[day].low for day in self.days], dtype=float)
        self.close = close
        self.amount = np.asarray([bars[day].amount or 0.0 for day in self.days], dtype=float)
        self.ma20 = _rolling_mean(close, 20)
        self.ma60 = _rolling_mean(close, 60)
        self.atr = _atr(high, low, close, 14)
        self.amount_mean20 = _rolling_mean(self.amount, 20)

    def pos(self, day: date) -> int | None:
        return self.index.get(day)

    def prior_low(self, position: int, days: int) -> float | None:
        start = position - days
        if start < 0:
            return None
        chunk = self.close[start:position]
        return float(np.min(chunk)) if chunk.size else None

    def atr_value(self, position: int) -> float | None:
        if position < 0 or position >= len(self.atr):
            return None
        value = float(self.atr[position])
        return value if np.isfinite(value) else None

    def ma_value(self, position: int, window: int) -> float | None:
        array = self.ma20 if window == 20 else self.ma60
        if position < 0 or position >= len(array):
            return None
        value = float(array[position])
        return value if np.isfinite(value) else None

    def amount_ratio(self, position: int) -> float | None:
        if position <= 0:
            return None
        base = float(self.amount_mean20[position - 1])
        if not np.isfinite(base) or base <= 0:
            return None
        return float(self.amount[position]) / base


def _rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(values.size, np.nan)
    if values.size < window:
        return out
    sums = np.convolve(values, np.ones(window), mode="valid")
    out[window - 1 :] = sums / window
    return out


def _atr(high, low, close, window: int) -> np.ndarray:
    size = high.size
    tr = high - low
    if size > 1:
        prev = close[:-1]
        tr[1:] = np.maximum.reduce(
            [high[1:] - low[1:], np.abs(high[1:] - prev), np.abs(low[1:] - prev)]
        )
    out = np.full(size, np.nan)
    if size >= window:
        sums = np.convolve(tr, np.ones(window), mode="valid")
        out[window - 1 :] = sums / window
    return out


def _limit_price(prev_close: float, code: str, name: str | None, day: date) -> tuple[float, float]:
    limit = limit_rules.limit_pct(code, name, day)
    base = Decimal(repr(float(prev_close)))
    return (
        float(limit_rules.board_price(base, limit, code, up=True)),
        float(limit_rules.board_price(base, limit, code, up=False)),
    )


def _prev_close(bar: DailyBar) -> float | None:
    if bar.pct is None or bar.pct <= -100:
        return None
    value = bar.close / (1 + bar.pct / 100)
    return value if value > 0 else None


def simulate(
    *,
    bars_by_code: dict[str, dict[date, DailyBar]],
    calendar: list[date],
    signals: list[Signal],
    config: SimConfig | None = None,
    names: dict[str, str | None] | None = None,
) -> SimResult:
    config = config or SimConfig()
    names = names or {}
    series = {code: Series(code, bars) for code, bars in bars_by_code.items()}
    signals_by_day: dict[date, list[Signal]] = {}
    for signal in signals:
        if signal.code in series:
            signals_by_day.setdefault(signal.day, []).append(signal)

    cash = config.initial_cash
    positions: dict[str, Position] = {}
    pending_buys: list[Signal] = []
    pending_sells: dict[str, str] = {}
    buy_delay: dict[str, int] = {}
    sell_delay: dict[str, int] = {}
    result = SimResult()
    skipped = {"limit_up": 0, "limit_down": 0, "suspended": 0, "cash": 0, "slots": 0}
    day_index = {day: index for index, day in enumerate(calendar)}

    for today in calendar:
        # 1) 先卖（释放现金与仓位）
        for code in list(pending_sells):
            position = positions.get(code)
            if position is None:
                pending_sells.pop(code, None)
                continue
            item = series[code]
            pos = item.pos(today)
            if pos is None:
                skipped["suspended"] += 1
                sell_delay[code] = sell_delay.get(code, 0) + 1
                if sell_delay[code] > config.max_exit_delay:
                    pending_sells.pop(code, None)
                continue
            bar = item.bars[today]
            prev = _prev_close(bar)
            if prev is not None:
                _up, down = _limit_price(prev, code, names.get(code), today)
                if bar.open <= down + 0.005:
                    skipped["limit_down"] += 1
                    sell_delay[code] = sell_delay.get(code, 0) + 1
                    if sell_delay[code] > config.max_exit_delay:
                        pending_sells.pop(code, None)
                    continue
            price = bar.open * (1 - config.slippage_bps / 10000)
            gross = price * position.shares
            fee = max(
                config.min_commission, gross * (config.commission_bps + config.stamp_bps) / 10000
            )
            cash += gross - fee
            cost = position.entry_price * position.shares
            receive = gross - fee
            pnl = receive - cost
            result.trades.append(
                Trade(
                    code=code,
                    entry_date=position.entry_date,
                    exit_date=today,
                    entry_price=position.entry_price,
                    exit_price=price,
                    shares=position.shares,
                    pnl=round(pnl, 2),
                    return_pct=round(pnl / cost * 100, 2) if cost else 0.0,
                    reason=pending_sells.pop(code),
                    hold_days=position.hold_days,
                )
            )
            positions.pop(code, None)
            sell_delay.pop(code, None)

        # 2) 再买（下一交易日开盘）
        if pending_buys:
            slots = config.max_positions - len(positions)
            ranked = sorted(pending_buys, key=lambda item: (-item.score, item.code))
            pending_buys = []
            for signal in ranked:
                if slots <= 0:
                    skipped["slots"] += 1
                    continue
                code = signal.code
                if code in positions:
                    continue
                item = series[code]
                pos = item.pos(today)
                if pos is None:
                    skipped["suspended"] += 1
                    buy_delay[code] = buy_delay.get(code, 0) + 1
                    if buy_delay[code] <= config.max_entry_delay:
                        pending_buys.append(signal)
                    continue
                bar = item.bars[today]
                prev = _prev_close(bar)
                if prev is not None:
                    up, _down = _limit_price(prev, code, names.get(code), today)
                    if bar.open >= up - 0.005:
                        skipped["limit_up"] += 1
                        continue
                equity = cash + _positions_value(positions, series, today)
                target = equity * config.position_pct
                budget = min(target, cash)
                price = bar.open * (1 + config.slippage_bps / 10000)
                shares = int(budget / price / 100) * 100
                if shares <= 0:
                    skipped["cash"] += 1
                    continue
                gross = shares * price
                fee = max(config.min_commission, gross * config.commission_bps / 10000)
                if gross + fee > cash:
                    skipped["cash"] += 1
                    continue
                cash -= gross + fee
                atr_value = item.atr_value(pos)
                stop = None
                if config.use_atr_stop and atr_value:
                    stop = price - config.atr_stop_multiple * atr_value
                positions[code] = Position(
                    code=code,
                    shares=shares,
                    entry_date=today,
                    entry_price=price,
                    stop_price=stop,
                    peak_close=bar.close,
                    entry_index=day_index.get(today, 0),
                    last_price=bar.close,
                )
                buy_delay.pop(code, None)
                slots -= 1

        # 3) 收盘后更新持仓、评估退出
        for code, position in list(positions.items()):
            item = series[code]
            pos = item.pos(today)
            if pos is None:
                continue
            bar = item.bars[today]
            position.last_price = bar.close
            position.hold_days = day_index.get(today, position.entry_index) - position.entry_index
            position.peak_close = max(position.peak_close, bar.close)
            if code in pending_sells:
                continue
            reason = _exit_reason(item, pos, position, bar, config)
            if reason:
                pending_sells[code] = reason

        # 4) 记录当日权益
        position_value = _positions_value(positions, series, today)
        equity = cash + position_value
        result.equity.append((today, round(equity, 2)))
        result.exposure.append(round(position_value / equity, 4) if equity > 0 else 0.0)

        # 5) 当日收盘信号 → 下一交易日开盘执行
        for signal in signals_by_day.get(today, []):
            if signal.code not in positions and signal.code not in buy_delay:
                pending_buys.append(signal)

    result.skipped = skipped
    result.metrics = _metrics(result, config)
    return result


def _positions_value(positions: dict[str, Position], series: dict[str, Series], day: date) -> float:
    """持仓市值。停牌日没有 K 线时沿用最后一次收盘价（不是按 0 计）。"""
    total = 0.0
    for code, position in positions.items():
        item = series[code]
        pos = item.pos(day)
        price = item.bars[day].close if pos is not None else position.last_price
        total += position.shares * price
    return total


def _exit_reason(item: Series, pos: int, position: Position, bar: DailyBar, config: SimConfig) -> str | None:
    if position.hold_days >= config.horizon:
        return "horizon"
    if config.use_atr_stop and position.stop_price is not None:
        if bar.close < position.stop_price:
            return "atr_stop"
    if config.use_trailing:
        atr_value = item.atr_value(pos)
        if atr_value:
            trailing = position.peak_close - config.trailing_multiple * atr_value
            if bar.close < trailing:
                return "trailing"
    if config.use_ma_exit:
        ma = item.ma_value(pos, config.ma_exit_window)
        if ma is not None and bar.close < ma:
            return "ma_break"
    if config.use_structure_exit:
        low = item.prior_low(pos, config.structure_days)
        if low is not None and bar.close < low:
            return "structure_low"
    if config.use_stagnation_exit:
        ratio = item.amount_ratio(pos)
        if ratio is not None and ratio > 2.0 and (bar.pct or 0) <= 0 and bar.close < bar.open:
            return "stagnation"
    return None


def _metrics(result: SimResult, config: SimConfig) -> dict:
    if not result.equity:
        return {"status": "empty"}
    values = np.asarray([value for _day, value in result.equity], dtype=float)
    initial = config.initial_cash
    total_return = values[-1] / initial - 1
    peak = np.maximum.accumulate(values)
    drawdown = values / peak - 1
    max_dd = float(drawdown.min())
    # 回撤持续天数
    longest = current = 0
    for value, high in zip(values, peak):
        if value < high:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    trades = result.trades
    gross = [trade.pnl for trade in trades]
    wins = [value for value in gross if value > 0]
    losses = [value for value in gross if value <= 0]
    profit_factor = (
        round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None
    )
    turnover = 0.0
    if trades:
        traded = sum(trade.entry_price * trade.shares + trade.exit_price * trade.shares for trade in trades)
        turnover = round(traded / float(values.mean()), 2)
    exposure = round(float(np.mean(result.exposure)), 4) if result.exposure else None
    return {
        "days": len(values),
        "total_return_pct": round(total_return * 100, 2),
        "annualized_pct": round(((values[-1] / initial) ** (252 / max(len(values), 1)) - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "max_drawdown_days": longest,
        "trades": len(trades),
        "win_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "profit_factor": profit_factor,
        "turnover": turnover,
        "avg_hold_days": round(float(np.mean([trade.hold_days for trade in trades])), 1) if trades else None,
        "final_equity": round(float(values[-1]), 2),
        "exposure": exposure,
        "skipped": result.skipped,
    }
