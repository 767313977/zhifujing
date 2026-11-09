"""B0 统一回测内核：信号研究 + 成交模拟（第一层与第二层）。

## 三个层次

1. **信号研究**：只判断历史时点 T 是否满足条件，以及之后的价格表现；
2. **成交模拟**：T 日收盘后出信号，默认 **T+1 开盘**成交；处理停牌顺延、
   开盘涨停买不进、退出日跌停无法确认成交、数据末端未走完；
3. **组合回测**：资金、仓位、持仓冲突（本轮只留接口边界，不实现）。

本模块只做 1 + 2，且**不生成信号** —— 信号由调用方（策略函数 / 脚本）按生产口径
生成，保证「回测调的就是线上那一份判定」。

## 收益口径（关键）

- 入场：`next_open` 时，买入价 = T+1 开盘价；T+1 停牌则顺延到复牌首日，
  顺延超过 `max_entry_delay` 记 `suspended`；
- 卖出：持有 h 个交易日（入场日算第 1 天）的收盘价；退出日停牌顺延，
  超过 `max_exit_delay` 记 `no_exit`；
- **收益用官方 `pct_chg` 复利**，不是原始收盘价比值。入场日当天的收益用
  `收盘/开盘 − 1`（同一根 K 线内部同一基准），之后逐日乘 `1 + pct_chg/100`。
  这样除权除息不会造出假跳空（与 `patterns.build_bars` 同一思路）；
- `entry="close"` 只作**理论对照**：信号在收盘后才成立，现实中最早只能次日开盘成交；
- 费用：`cost_bps` 是**一次完整买卖往返**的总成本（佣金+税费+滑点），
  按 `net = (1+gross) × (1 − cost_bps/10000) − 1` 扣一次，**不重复扣**。

## 统计口径

- `observation`：状态为 `ok` 或 `exit_limit_down` 的样本（有价格、但退出日可能卖不掉）；
- `execution`：只含 `ok`（入场可买、退出日未跌停）；
- 两者都输出，页面/报告必须分开写，不能把观察收益当可交易收益；
- 基准默认「同一入场日、同一退出日的全市场等权」；基准同样剔除入场涨停与退出跌停；
- MAE = 持有期内**收盘累计收益**的最小值（日线无法还原盘中最大回撤，报告要标注）；
- 分布桶：≤−10 / −10~−5 / −5~0 / 0~5 / 5~10 / >10（%）。

## 已知局限（必须随结果一起展示）

- 没有退市股名单：`history.db` 只有当前代码池，结果偏向「现存股票样本研究」；
- 日线无法确认「开盘涨停但盘中打开」等逐笔成交，只能按开盘价是否触及板价标记；
- 停牌顺延后的实际成交价与计划价不同，样本会带 `entry_delay / exit_delay` 字段；
- 指数基准用指数自身的开/收，不含成分股分红再投资差异。
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import IndexDaily, RpsDaily, StockDaily, TradeCalendar
from app.services import limit_rules

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")

#: 默认持有期（交易日）
DEFAULT_HORIZONS = (5, 10, 20, 40, 60)

#: 入场/退出顺延上限（交易日）
DEFAULT_ENTRY_DELAY = 5
DEFAULT_EXIT_DELAY = 10

#: 分布桶
DIST_BUCKETS = (
    ("<=-10", -math.inf, -10.0),
    ("-10~-5", -10.0, -5.0),
    ("-5~0", -5.0, 0.0),
    ("0~5", 0.0, 5.0),
    ("5~10", 5.0, 10.0),
    (">10", 10.0, math.inf),
)

STATUSES = (
    "ok",
    "exit_limit_down",
    "unbuyable",
    "no_quote",
    "suspended",
    "no_exit",
    "pending",
    "price_invalid",
)


@dataclass(frozen=True)
class Signal:
    code: str
    signal_date: date
    meta: dict = field(default_factory=dict)


@dataclass
class Sample:
    code: str
    signal_date: date
    horizon: int
    status: str
    entry_date: date | None = None
    exit_date: date | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    gross: float | None = None
    net: float | None = None
    mae: float | None = None
    benchmark: float | None = None
    excess: float | None = None
    entry_delay: int = 0
    exit_delay: int = 0
    flags: tuple[str, ...] = ()


@dataclass
class HorizonStat:
    horizon: int
    observation_samples: int = 0
    execution_samples: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    mean: float | None = None
    median: float | None = None
    pos_pct: float | None = None
    neg_pct: float | None = None
    excess_mean: float | None = None
    excess_median: float | None = None
    beat_pct: float | None = None
    net_mean: float | None = None
    net_median: float | None = None
    mae_mean: float | None = None
    distribution: dict[str, int] = field(default_factory=dict)
    regimes: dict[str, "HorizonStat"] = field(default_factory=dict)


@dataclass
class BacktestResult:
    strategy: str
    params: dict
    start: date | None
    end: date | None
    entry_mode: str
    cost_bps: float
    horizons: dict[int, HorizonStat] = field(default_factory=dict)
    samples: list[Sample] = field(default_factory=list)
    data_coverage: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 行情矩阵
# ---------------------------------------------------------------------------


class BarMatrix:
    """一次取数，按 (交易日, 代码) 存 OHLC / pct_chg / name。

    用二维数组而不是 `dict[(date, code)]`：5000 只 × 300 天 ≈ 150 万格，
    dict 的键对象开销会让内存翻几倍；numpy 数组约 70~90MB，够用。
    """

    def __init__(self, calendar: list[date], codes: list[str]) -> None:
        self.calendar = calendar
        self.codes = codes
        self.date_pos = {day: index for index, day in enumerate(calendar)}
        self.code_pos = {code: index for index, code in enumerate(codes)}
        shape = (len(calendar), len(codes))
        self.open = np.full(shape, np.nan)
        self.high = np.full(shape, np.nan)
        self.low = np.full(shape, np.nan)
        self.close = np.full(shape, np.nan)
        self.pct = np.full(shape, np.nan)
        self.present = np.zeros(shape, dtype=bool)
        self.name = np.full(shape, None, dtype=object)
        self.first_date: dict[str, date] = {}

    @property
    def end_pos(self) -> int:
        return len(self.calendar) - 1


def load_matrix(
    session: Session,
    start: date,
    end: date,
    *,
    codes: Iterable[str] | None = None,
) -> BarMatrix:
    """加载 `[start, end]` 的行情矩阵。

    `codes=None` 表示全市场（基准需要）；否则只加载指定代码。交易日轴优先用
    `trade_calendar`，缺日历时退化为行情自身日期并集并告警。
    """
    calendar = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date >= start, TradeCalendar.trade_date <= end)
            .order_by(TradeCalendar.trade_date)
        )
    )
    if not calendar:
        calendar = sorted(
            set(
                session.scalars(
                    select(StockDaily.trade_date)
                    .where(StockDaily.trade_date >= start, StockDaily.trade_date <= end)
                    .distinct()
                )
            )
        )
        logger.warning("trade_calendar 在 %s~%s 没有数据，退化为行情日期并集", start, end)
    restrict = codes is not None
    wanted = sorted({code for code in (codes or []) if code})
    if restrict:
        # 传了空列表 = 明确要求空池，不能退化成「全市场」
        all_codes: list[str] = []
        if wanted:
            code_rows = list(
                session.execute(
                    select(StockDaily.code)
                    .where(
                        StockDaily.trade_date >= start,
                        StockDaily.trade_date <= end,
                        StockDaily.code.in_(wanted),
                    )
                    .distinct()
                )
            )
            all_codes = sorted({row[0] for row in code_rows})
    else:
        all_codes = list(
            session.scalars(
                select(StockDaily.code)
                .where(StockDaily.trade_date >= start, StockDaily.trade_date <= end)
                .distinct()
            )
        )
    matrix = BarMatrix(calendar, all_codes)
    if not all_codes:
        return matrix

    rows = session.execute(
        select(
            StockDaily.trade_date,
            StockDaily.code,
            StockDaily.open,
            StockDaily.high,
            StockDaily.low,
            StockDaily.close,
            StockDaily.pct_chg,
            StockDaily.name,
        )
        .where(
            StockDaily.trade_date >= start,
            StockDaily.trade_date <= end,
            StockDaily.code.in_(all_codes),
        )
        .execution_options(yield_per=20000)
    )
    for when, code, open_, high, low, close, pct, name in rows:
        i = matrix.date_pos.get(when)
        j = matrix.code_pos.get(code)
        if i is None or j is None:
            continue
        matrix.present[i, j] = True
        if open_ is not None:
            matrix.open[i, j] = float(open_)
        if high is not None:
            matrix.high[i, j] = float(high)
        if low is not None:
            matrix.low[i, j] = float(low)
        if close is not None:
            matrix.close[i, j] = float(close)
        if pct is not None:
            matrix.pct[i, j] = float(pct)
        matrix.name[i, j] = name

    # 首次出现日（判次新）。只查一次，不能从矩阵里推（矩阵起点会截掉更早历史）
    for code, first in session.execute(
        select(StockDaily.code, func.min(StockDaily.trade_date))
        .where(StockDaily.code.in_(all_codes))
        .group_by(StockDaily.code)
    ):
        matrix.first_date[code] = first
    return matrix


# ---------------------------------------------------------------------------
# 交易规则
# ---------------------------------------------------------------------------


def _prev_close(close: float, pct: float | None) -> float | None:
    """由官方涨跌幅反推前收（除权日也适用）。"""
    if pct is None or close <= 0 or pct <= -100:
        return None
    value = close / (1 + pct / 100.0)
    if value <= 0 or not math.isfinite(value):
        return None
    # ⚠️ numpy.float64 的 repr 是 `np.float64(...)`，不能直接喂 Decimal；先转 Python float
    return float(Decimal(repr(float(value))).quantize(CENT, rounding=ROUND_HALF_UP))


def _entry_limit_up(
    code: str,
    name: str | None,
    day: date,
    open_price: float,
    close: float,
    pct: float | None,
) -> bool:
    """开盘价是否已经在涨停板（含一字板）。"""
    prev = _prev_close(close, pct)
    if prev is None or open_price <= 0:
        return False
    lim = limit_rules.limit_pct(code, name, day)
    board = float(
        limit_rules.board_price(
            Decimal(repr(float(prev))).quantize(CENT, rounding=ROUND_HALF_UP),
            lim,
            code,
            up=True,
        )
    )
    return open_price >= board - 0.005


def _exit_limit_down(
    code: str,
    name: str | None,
    day: date,
    close: float,
    low: float | None,
    pct: float | None,
) -> bool:
    prev = _prev_close(close, pct)
    if prev is None or low is None:
        return False
    # 同样要转 Python float：limit_rules 内部会 `Decimal(repr(close))`
    return limit_rules.is_limit_down(
        float(pct) if pct is not None else None,
        float(close),
        float(low),
        code,
        name,
        prev_close=float(prev),
        trade_date=day,
    )


# ---------------------------------------------------------------------------
# 基准
# ---------------------------------------------------------------------------


class Benchmark:
    """同一 (入场日, 退出日) 的全市场等权收益，带缓存。

    ⚠️ 基准剔除「入场开盘涨停」与「退出日跌停」的票，与策略样本同一套可成交口径；
    但**不剔除 ST / 次新**（基准代表市场，不代表可交易池）；调用方如需更严基准，
    应在策略层先限制股票池。
    """

    def __init__(self, sessions: Session, matrix: BarMatrix, mode: str = "market") -> None:
        self.session = sessions
        self.matrix = matrix
        self.mode = mode
        self._cache: dict[tuple[int, int], float | None] = {}
        self._index = None
        if mode.startswith("index:"):
            self._index = self._load_index(mode.split(":", 1)[1])

    def _load_index(self, code: str) -> dict[date, tuple[float, float | None]]:
        rows = self.session.execute(
            select(IndexDaily.trade_date, IndexDaily.close, IndexDaily.pct_chg)
            .where(IndexDaily.code == code)
            .where(
                IndexDaily.trade_date >= self.matrix.calendar[0],
                IndexDaily.trade_date <= self.matrix.calendar[-1],
            )
        ).all()
        return {day: (float(close), pct) for day, close, pct in rows if close is not None}

    def get(self, entry_pos: int, exit_pos: int) -> float | None:
        key = (entry_pos, exit_pos)
        if key in self._cache:
            return self._cache[key]
        if self._index is not None:
            value = self._index_return(entry_pos, exit_pos)
        else:
            value = self._market_return(entry_pos, exit_pos)
        self._cache[key] = value
        return value

    def _index_return(self, entry_pos: int, exit_pos: int) -> float | None:
        entry_day = self.matrix.calendar[entry_pos]
        exit_day = self.matrix.calendar[exit_pos]
        entry = self._index.get(entry_day)
        exit_row = self._index.get(exit_day)
        if not entry or not exit_row:
            return None
        # 指数没有「开盘价」字段，用收盘→收盘；报告里会标注
        first = entry[0]
        factor = 1.0
        for pos in range(entry_pos + 1, exit_pos + 1):
            day = self.matrix.calendar[pos]
            row = self._index.get(day)
            if row and row[1] is not None:
                factor *= 1 + row[1] / 100.0
        return (factor - 1) * 100 if first > 0 else None

    def _market_return(self, entry_pos: int, exit_pos: int) -> float | None:
        values: list[float] = []
        for j, code in enumerate(self.matrix.codes):
            if not self.matrix.present[entry_pos, j] or not self.matrix.present[exit_pos, j]:
                continue
            open_ = self.matrix.open[entry_pos, j]
            close_e = self.matrix.close[entry_pos, j]
            pct_e = self.matrix.pct[entry_pos, j]
            if not (np.isfinite(open_) and np.isfinite(close_e)) or open_ <= 0:
                continue
            name_e = self.matrix.name[entry_pos, j]
            if _entry_limit_up(code, name_e, self.matrix.calendar[entry_pos], open_, close_e, None if not np.isfinite(pct_e) else pct_e):
                continue
            close_x = self.matrix.close[exit_pos, j]
            low_x = self.matrix.low[exit_pos, j]
            pct_x = self.matrix.pct[exit_pos, j]
            if not np.isfinite(close_x):
                continue
            if _exit_limit_down(code, self.matrix.name[exit_pos, j], self.matrix.calendar[exit_pos], close_x, None if not np.isfinite(low_x) else low_x, None if not np.isfinite(pct_x) else pct_x):
                continue
            first = close_e / open_ - 1
            factor = 1 + first
            for pos in range(entry_pos + 1, exit_pos + 1):
                if self.matrix.present[pos, j]:
                    pct = self.matrix.pct[pos, j]
                    if np.isfinite(pct):
                        factor *= 1 + pct / 100.0
            values.append((factor - 1) * 100)
        return float(np.mean(values)) if values else None


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------


def _find_entry(matrix: BarMatrix, signal_pos: int, j: int, max_delay: int) -> tuple[int | None, str, int]:
    """找入场日。返回 (位置, 状态, 顺延天数)。"""
    if signal_pos + 1 > matrix.end_pos:
        return None, "pending", 0
    for offset in range(1, max_delay + 1):
        pos = signal_pos + offset
        if pos > matrix.end_pos:
            return None, "pending", offset
        if matrix.present[pos, j]:
            return pos, "ok", offset
    return None, "suspended", max_delay


def _find_exit(matrix: BarMatrix, entry_pos: int, j: int, horizon: int, max_delay: int) -> tuple[int | None, str, int]:
    target = entry_pos + horizon - 1
    if target > matrix.end_pos:
        return None, "pending", 0
    for offset in range(0, max_delay + 1):
        pos = target + offset
        if pos > matrix.end_pos:
            return None, "pending", offset
        if matrix.present[pos, j]:
            return pos, "ok", offset
    return None, "no_exit", max_delay


def _sample_for(
    matrix: BarMatrix,
    signal: Signal,
    horizon: int,
    *,
    entry_mode: str,
    cost_bps: float,
    max_entry_delay: int,
    max_exit_delay: int,
    benchmark: Benchmark,
    new_listing_days: int,
) -> Sample:
    j = matrix.code_pos.get(signal.code)
    if j is None:
        return Sample(signal.code, signal.signal_date, horizon, "no_quote")
    signal_pos = matrix.date_pos.get(signal.signal_date)
    if signal_pos is None:
        return Sample(signal.code, signal.signal_date, horizon, "no_quote")
    flags: list[str] = []
    if not matrix.present[signal_pos, j]:
        return Sample(signal.code, signal.signal_date, horizon, "no_quote")

    if entry_mode == "close":
        entry_pos, entry_state, entry_delay = signal_pos, "ok", 0
        entry_price = matrix.close[signal_pos, j]
    else:
        entry_pos, entry_state, entry_delay = _find_entry(matrix, signal_pos, j, max_entry_delay)
        entry_price = matrix.open[entry_pos, j] if entry_pos is not None else None
    if entry_pos is None or entry_state != "ok":
        return Sample(signal.code, signal.signal_date, horizon, entry_state, entry_delay=entry_delay, flags=tuple(flags))
    if not np.isfinite(entry_price) or entry_price <= 0:
        return Sample(
            signal.code,
            signal.signal_date,
            horizon,
            "no_quote",
            entry_date=matrix.calendar[entry_pos],
            entry_delay=entry_delay,
            flags=tuple(flags),
        )
    entry_day = matrix.calendar[entry_pos]
    name_e = matrix.name[entry_pos, j]
    close_e = matrix.close[entry_pos, j]
    pct_e = matrix.pct[entry_pos, j]
    if not np.isfinite(close_e) or close_e <= 0:
        return Sample(signal.code, signal.signal_date, horizon, "price_invalid", entry_date=entry_day, flags=tuple(flags))

    # 先打标记（ST / 次新 / 北交所），再判是否买得到 —— 这样不可买的样本也带完整标记
    first = matrix.first_date.get(signal.code)
    if first is not None:
        first_pos = matrix.date_pos.get(first)
        if first_pos is not None and entry_pos - first_pos < new_listing_days:
            flags.append("new_listing")
    if name_e and "ST" in str(name_e).upper():
        flags.append("st")
    if limit_rules.is_bse(signal.code):
        flags.append("bse")

    if entry_mode != "close" and _entry_limit_up(
        signal.code, name_e, entry_day, float(entry_price), float(close_e), None if not np.isfinite(pct_e) else float(pct_e)
    ):
        return Sample(
            signal.code,
            signal.signal_date,
            horizon,
            "unbuyable",
            entry_date=entry_day,
            entry_price=float(entry_price),
            entry_delay=entry_delay,
            flags=tuple(flags) + ("entry_limit_up",),
        )

    exit_pos, exit_state, exit_delay = _find_exit(matrix, entry_pos, j, horizon, max_exit_delay)
    if exit_pos is None or exit_state != "ok":
        return Sample(
            signal.code,
            signal.signal_date,
            horizon,
            exit_state,
            entry_date=entry_day,
            entry_price=float(entry_price),
            entry_delay=entry_delay,
            exit_delay=exit_delay,
            flags=tuple(flags),
        )
    exit_day = matrix.calendar[exit_pos]
    close_x = matrix.close[exit_pos, j]
    low_x = matrix.low[exit_pos, j]
    pct_x = matrix.pct[exit_pos, j]
    if not np.isfinite(close_x) or close_x <= 0:
        return Sample(
            signal.code,
            signal.signal_date,
            horizon,
            "price_invalid",
            entry_date=entry_day,
            entry_price=float(entry_price),
            entry_delay=entry_delay,
            exit_date=exit_day,
            exit_delay=exit_delay,
            flags=tuple(flags),
        )

    # 收益：入场日 收盘/入场价 − 1，之后逐日 pct_chg 复利
    first = close_e / float(entry_price) - 1
    factor = 1 + first
    cumulative = [first]
    for pos in range(entry_pos + 1, exit_pos + 1):
        if matrix.present[pos, j]:
            pct = matrix.pct[pos, j]
            if np.isfinite(pct):
                factor *= 1 + pct / 100.0
                cumulative.append(factor - 1)
    gross = (factor - 1) * 100
    net = ((1 + gross / 100) * (1 - cost_bps / 10000) - 1) * 100
    mae = min(cumulative) * 100 if cumulative else None
    status = "ok"
    exit_limit = _exit_limit_down(
        signal.code,
        matrix.name[exit_pos, j],
        exit_day,
        float(close_x),
        None if not np.isfinite(low_x) else float(low_x),
        None if not np.isfinite(pct_x) else float(pct_x),
    )
    if exit_limit:
        status = "exit_limit_down"
        flags.append("exit_limit_down")
    bench = benchmark.get(entry_pos, exit_pos)
    return Sample(
        signal.code,
        signal.signal_date,
        horizon,
        status,
        entry_date=entry_day,
        exit_date=exit_day,
        entry_price=float(entry_price),
        exit_price=float(close_x),
        gross=gross,
        net=net,
        mae=mae,
        benchmark=bench,
        excess=None if bench is None else gross - bench,
        entry_delay=entry_delay,
        exit_delay=exit_delay,
        flags=tuple(flags),
    )


def _stat(values: list[float]) -> dict:
    if not values:
        return {
            "mean": None,
            "median": None,
            "pos_pct": None,
            "neg_pct": None,
            "net_mean": None,
            "net_median": None,
            "mae_mean": None,
            "distribution": {label: 0 for label, _lo, _hi in DIST_BUCKETS},
        }
    arr = np.array(values, dtype=float)
    dist = {}
    for label, lo, hi in DIST_BUCKETS:
        if label == "<=-10":
            count = int((arr <= -10).sum())
        elif label == ">10":
            count = int((arr > 10).sum())
        else:
            count = int(((arr > lo) & (arr <= hi)).sum())
        dist[label] = count
    return {
        "mean": round(float(arr.mean()), 2),
        "median": round(float(np.median(arr)), 2),
        "pos_pct": round(float((arr > 0).mean() * 100), 1),
        "neg_pct": round(float((arr < 0).mean() * 100), 1),
        "distribution": dist,
    }


def _aggregate(samples: list[Sample], horizon: int) -> HorizonStat:
    stat = HorizonStat(horizon=horizon)
    stat.counts = {name: 0 for name in STATUSES}
    for sample in samples:
        stat.counts[sample.status] = stat.counts.get(sample.status, 0) + 1
    observation = [s for s in samples if s.status in ("ok", "exit_limit_down") and s.gross is not None]
    execution = [s for s in samples if s.status == "ok" and s.gross is not None]
    stat.observation_samples = len(observation)
    stat.execution_samples = len(execution)
    if execution:
        base = _stat([s.gross for s in execution])
        stat.mean = base["mean"]
        stat.median = base["median"]
        stat.pos_pct = base["pos_pct"]
        stat.neg_pct = base["neg_pct"]
        stat.distribution = base["distribution"]
        stat.net_mean = round(float(np.mean([s.net for s in execution if s.net is not None])), 2) if any(s.net is not None for s in execution) else None
        stat.net_median = round(float(np.median([s.net for s in execution if s.net is not None])), 2) if any(s.net is not None for s in execution) else None
        maes = [s.mae for s in execution if s.mae is not None]
        stat.mae_mean = round(float(np.mean(maes)), 2) if maes else None
        excesses = [s.excess for s in execution if s.excess is not None]
        if excesses:
            stat.excess_mean = round(float(np.mean(excesses)), 2)
            stat.excess_median = round(float(np.median(excesses)), 2)
            stat.beat_pct = round(float(np.mean([1 if value > 0 else 0 for value in excesses]) * 100), 1)
    else:
        stat.distribution = {label: 0 for label, _lo, _hi in DIST_BUCKETS}
    return stat


def run_signals(
    session: Session,
    signals: list[Signal],
    *,
    strategy: str,
    params: dict | None = None,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    entry_mode: str = "next_open",
    cost_bps: float = 0.0,
    max_entry_delay: int = DEFAULT_ENTRY_DELAY,
    max_exit_delay: int = DEFAULT_EXIT_DELAY,
    benchmark_mode: str = "market",
    use_universe: Iterable[str] | None = None,
    new_listing_days: int = 5,
) -> BacktestResult:
    """对一组信号做成交模拟与统计。

    `signals` 必须已经在**各自信号日收盘后**生成；本函数只负责「之后发生什么」。
    `use_universe` 给定时只加载这些代码的矩阵（基准会变窄，报告里会告警）；
    默认全市场，基准才有意义。
    """
    if entry_mode not in ("next_open", "close"):
        raise ValueError("entry_mode 只能是 next_open / close")
    if not signals:
        return BacktestResult(
            strategy=strategy,
            params=params or {},
            start=None,
            end=None,
            entry_mode=entry_mode,
            cost_bps=cost_bps,
            warnings=["没有信号"],
        )
    start = min(s.signal_date for s in signals)
    data_end = session.scalar(select(func.max(StockDaily.trade_date)))
    end = data_end or start
    matrix = load_matrix(session, start, end, codes=use_universe)
    if not matrix.calendar:
        return BacktestResult(
            strategy=strategy,
            params=params or {},
            start=start,
            end=end,
            entry_mode=entry_mode,
            cost_bps=cost_bps,
            warnings=["行情矩阵为空"],
        )
    benchmark = Benchmark(session, matrix, benchmark_mode)
    all_samples: list[Sample] = []
    for signal in signals:
        for horizon in horizons:
            all_samples.append(
                _sample_for(
                    matrix,
                    signal,
                    horizon,
                    entry_mode=entry_mode,
                    cost_bps=cost_bps,
                    max_entry_delay=max_entry_delay,
                    max_exit_delay=max_exit_delay,
                    benchmark=benchmark,
                    new_listing_days=new_listing_days,
                )
            )
    horizons_out: dict[int, HorizonStat] = {}
    for horizon in horizons:
        subset = [s for s in all_samples if s.horizon == horizon]
        stat = _aggregate(subset, horizon)
        stat.regimes = _regime_breakdown(session, subset)
        horizons_out[horizon] = stat
    warnings = [
        "MAE 是持有期内收盘累计收益的最小值，日线无法还原盘中最大回撤",
        "没有退市股名单；结果应标注为「现存股票样本研究」",
        "基准剔除入场涨停与退出跌停，但未剔除 ST / 次新",
    ]
    if use_universe is not None:
        warnings.append("基准只使用 use_universe 指定的股票池，不是全市场")
    if entry_mode == "close":
        warnings.append("entry=close 是理论口径；基准仍按次日开盘口径，两者不可直接比较")
    return BacktestResult(
        strategy=strategy,
        params=params or {},
        start=start,
        end=end,
        entry_mode=entry_mode,
        cost_bps=cost_bps,
        horizons=horizons_out,
        samples=all_samples,
        data_coverage={
            "calendar_days": len(matrix.calendar),
            "codes": len(matrix.codes),
            "signal_count": len(signals),
            "data_end": str(data_end) if data_end else None,
        },
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# 市场环境
# ---------------------------------------------------------------------------


def _regime_map(session: Session, start: date, end: date, index_code: str = "000001.SH") -> dict[date, str]:
    """确定性环境分类：指数收盘 vs MA20/MA60。

    - 进攻：close > MA20 且 MA20 > MA60
    - 防守：close < MA60
    - 震荡：其余
    - 未知：MA 不足
    """
    rows = session.execute(
        select(IndexDaily.trade_date, IndexDaily.close)
        .where(IndexDaily.code == index_code, IndexDaily.trade_date <= end)
        .order_by(IndexDaily.trade_date)
    ).all()
    closes = [(day, float(close)) for day, close in rows if close is not None]
    out: dict[date, str] = {}
    for i, (day, close) in enumerate(closes):
        if day < start:
            continue
        if i < 60:
            out[day] = "未知"
            continue
        ma20 = float(np.mean([value for _d, value in closes[i - 19 : i + 1]]))
        ma60 = float(np.mean([value for _d, value in closes[i - 59 : i + 1]]))
        if close > ma20 and ma20 > ma60:
            out[day] = "进攻"
        elif close < ma60:
            out[day] = "防守"
        else:
            out[day] = "震荡"
    return out


def _regime_breakdown(session: Session, samples: list[Sample]) -> dict[str, HorizonStat]:
    if not samples:
        return {}
    start = min(s.signal_date for s in samples)
    end = max(s.signal_date for s in samples)
    regimes = _regime_map(session, start, end)
    grouped: dict[str, list[Sample]] = defaultdict(list)
    for sample in samples:
        grouped[regimes.get(sample.signal_date, "未知")].append(sample)
    return {name: _aggregate(items, samples[0].horizon) for name, items in grouped.items()}


# ---------------------------------------------------------------------------
# 信号提供器（B0 基线用；全部只读本地库）
# ---------------------------------------------------------------------------


RPS_FIELDS = ("rps20", "rps50", "rps60", "rps120", "rps250")


def _liquid_codes(session: Session, day: date, codes: list[str], min_avg_amount: float) -> set[str]:
    """20 日平均成交额过滤（只对候选票查，不扫全市场）。"""
    if min_avg_amount <= 0 or not codes:
        return set(codes)
    days = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date < day)
            .order_by(TradeCalendar.trade_date.desc())
            .limit(20)
        )
    )
    if not days:
        return set()
    rows = session.execute(
        select(StockDaily.code, func.avg(StockDaily.amount))
        .where(StockDaily.trade_date.in_(days), StockDaily.code.in_(codes))
        .group_by(StockDaily.code)
    ).all()
    return {
        code
        for code, avg in rows
        if avg is not None and float(avg) >= min_avg_amount
    }


def signals_from_rps(
    session: Session,
    start: date,
    end: date,
    *,
    top: int = 50,
    mode: str = "rps50",
    min_rps: float = 0.0,
    min_avg_amount: float = 0.0,
    exclude_st: bool = True,
    exclude_bse: bool = True,
) -> list[Signal]:
    """每个交易日按指定 RPS 取前 `top` 只。"""
    from app.services.patterns import is_st

    if mode not in RPS_FIELDS:
        raise ValueError(f"未知 RPS 字段：{mode}（只能是 {RPS_FIELDS}）")
    field = getattr(RpsDaily, mode)
    days = list(
        session.scalars(
            select(RpsDaily.trade_date)
            .where(RpsDaily.trade_date >= start, RpsDaily.trade_date <= end)
            .group_by(RpsDaily.trade_date)
            .order_by(RpsDaily.trade_date)
        )
    )
    out: list[Signal] = []
    for day in days:
        rows = session.execute(
            select(RpsDaily.code, StockDaily.name, field)
            .join(
                StockDaily,
                (StockDaily.trade_date == RpsDaily.trade_date) & (StockDaily.code == RpsDaily.code),
            )
            .where(RpsDaily.trade_date == day, field.is_not(None), field >= min_rps)
            .order_by(field.desc(), RpsDaily.code)
            .limit(top)
        ).all()
        candidates = [row[0] for row in rows]
        liquid = _liquid_codes(session, day, candidates, min_avg_amount)
        for code, name, value in rows:
            if code not in liquid:
                continue
            if exclude_st and is_st(name):
                continue
            if exclude_bse and limit_rules.is_bse(code):
                continue
            out.append(Signal(code=code, signal_date=day, meta={"rps": float(value)}))
    return out


def signals_from_pattern(
    session: Session,
    start: date,
    end: date,
    *,
    patterns: Iterable[str],
    top: int = 50,
    min_avg_amount: float = 0.0,
) -> list[Signal]:
    """从已落库的 `pattern_hit` 取信号（只覆盖扫描过的日期）。"""
    from app.models import PatternHit
    from app.services.patterns import PATTERNS, is_st

    keys = tuple(patterns)
    if not keys:
        return []
    known = {pattern.key for pattern in PATTERNS}
    keys = tuple(key for key in keys if key in known)
    if not keys:
        return []
    rows = session.execute(
        select(PatternHit.trade_date, PatternHit.code, PatternHit.name, PatternHit.score)
        .where(
            PatternHit.trade_date >= start,
            PatternHit.trade_date <= end,
            PatternHit.pattern.in_(keys),
        )
        .order_by(PatternHit.trade_date, PatternHit.score.desc())
    ).all()
    grouped: dict[date, dict[str, tuple[str | None, float]]] = defaultdict(dict)
    for day, code, name, score in rows:
        grouped[day].setdefault(code, (name, float(score)))
    out: list[Signal] = []
    for day in sorted(grouped):
        picked = sorted(grouped[day].items(), key=lambda item: (-item[1][1], item[0]))[:top]
        liquid = _liquid_codes(session, day, [code for code, _ in picked], min_avg_amount)
        for code, (name, score) in picked:
            if code not in liquid:
                continue
            if is_st(name) or limit_rules.is_bse(code):
                continue
            out.append(Signal(code=code, signal_date=day, meta={"score": score}))
    return out


def signals_from_trend_template(
    session: Session,
    start: date,
    end: date,
    *,
    candidate_top: int = 200,
    mode: str = "rps60",
    min_rs: float = 70.0,
) -> list[Signal]:
    """RPS 候选里再叠生产「Minervini 趋势模板」（`services.trend.evaluate`）。

    先按 `mode` 取每天前 `candidate_top` 只，再对这批票加载 250 根前复权 K 线，
    满足模板的才留下。判定与 `/pools/rps?trend_template=true` 完全同一份。
    """
    from app.services import trend

    base = signals_from_rps(
        session,
        start,
        end,
        top=candidate_top,
        mode=mode,
        exclude_st=True,
        exclude_bse=True,
    )
    grouped: dict[date, list[Signal]] = defaultdict(list)
    for signal in base:
        grouped[signal.signal_date].append(signal)
    out: list[Signal] = []
    for day, signals in grouped.items():
        codes = [signal.code for signal in signals]
        bars_by_code = trend.load_bars(session, codes, day)
        rs_map = dict(
            session.execute(
                select(RpsDaily.code, RpsDaily.rps250).where(
                    RpsDaily.trade_date == day, RpsDaily.code.in_(codes)
                )
            ).all()
        )
        for signal in signals:
            bars = bars_by_code.get(signal.code)
            if bars is None:
                continue
            rs = rs_map.get(signal.code)
            if rs is None or float(rs) < min_rs:
                continue
            if trend.evaluate(bars, len(bars) - 1, float(rs)):
                out.append(signal)
    return out


def signals_from_sector_stock(
    session: Session,
    start: date,
    end: date,
    *,
    taxonomy: str = "kph_selected",
    sectors: int = 3,
    per_sector: int = 3,
) -> list[Signal]:
    """复用生产 `pools.sector_stock` 的板块→个股候选（需要 sector_member 历史）。"""
    from app.models import SectorMember
    from app.services import pools

    days = list(
        session.scalars(
            select(SectorMember.trade_date)
            .where(SectorMember.trade_date >= start, SectorMember.trade_date <= end)
            .group_by(SectorMember.trade_date)
            .order_by(SectorMember.trade_date)
        )
    )
    out: list[Signal] = []
    for day in days:
        board = pools.sector_stock(
            session,
            trade_date=day,
            taxonomy=taxonomy,
            sectors=sectors,
            per_sector=per_sector,
            limit=sectors * per_sector,
        )
        for row in board.rows:
            out.append(
                Signal(
                    code=row.code,
                    signal_date=day,
                    meta={"sector_rps20": row.rps20, "sector": row.sector_code},
                )
            )
    return out
