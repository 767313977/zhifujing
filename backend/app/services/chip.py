"""筹码分布（CYQ）：按换手率衰减，把历史成交摊到价格网格上。

**本地计算，零外部数据源** —— 输入就是 `stock_daily` 的 `high / low / close /
turnover`。这不是新指标的口径发明：业界通行做法就是「历史筹码按换手率衰减，
当日成交量按三角分布撒进 `[low, high]`」，东财也没有公开的 CYQ 接口
（2026-08-19 上游实测 `push2/api/qt/stock/cyq/get` 等写法均 404）。

口径与两个必须小心的点：

1. **必须按时间升序递推**。换手衰减是有方向的：倒序传入会把衰减反着推、
   还会把最老的收盘价当成现价 —— 结果完全错却不会报错。所以本模块自己排序。
2. **初始筹码要播种成「首日全部流通盘」**（`chips = 首日分布`），不能从全零起步。
   从零开始等于假设窗口之前没人持仓，再把窗口内的少量换手归一化成 100%：
   两个 1% 换手日（价 10 和 100）会被算成约 50/50，而真实情况是约 99% 仍在 10 附近。

⚠️ **2026-10-28 用户拍板：价格改成前复权口径**。原先直接用 `stock_daily` 的
不复权 OHLC，送转 / 大比例拆股那天价格会凭空腰斩（或翻倍），除权前的成本区被留在
旧价位上，除权后 `DEFAULT_DAYS` 个交易日内 `profit_ratio` / `avg_cost` 明显失真。
现在用**官方涨跌幅**把历史价换算到最新一根的价位上（`pct_chg` 本身就是前复权收益）：
从最后一根往前推 `scale[i] = scale[i+1] × close[i+1] ÷ (close[i] × (1+pct[i+1]/100))`，
再把 `high/low/close` 都乘上 `scale[i]`。这样除权日不再跳空，且不需要额外的复权因子表。

⚠️ 缺 `pct_chg` 的**一步**不做换算（`scale` 原地继承），因为缺了官方涨跌幅就**无法区分**
「真实涨跌」与「除权」—— 把它当成「收益 0」等于假设那一段价差全是除权，会把整段价格
归一到最后一天（实测：真实腰斩 20→10 且 pct 缺失时，`avg_cost` 会从 18.42 掉到 9.60、
`cost90` 被压成 (9.53, 9.66)）。停牌残行（`pct_chg` 空、收盘价与上一根相同）本来就不受
影响；整段都缺时 `scale` 全为 1，等价于原来的不复权口径 —— 这与 2026-10-28 的说明一致。
输出单位：价格是元，比例是 0~1（`profit_ratio = 0.09` 表示 9% 的筹码处于盈利）。
"""

import logging
from dataclasses import dataclass

import numpy as np
from sqlalchemy import select

from app.db import session_scope
from app.models import StockDaily

logger = logging.getLogger(__name__)

# 默认回看的交易日数与价格网格密度。250 根 ≈ 一年，与形态引擎的窗口一致。
DEFAULT_DAYS = 250
GRID_SIZE = 300
# 换手衰减系数：1.0 = 按真实换手率换手；同花顺口径常用 1.5~2.0 加快历史筹码消散。
DECAY = 1.0

# 少于此根数不给结果（太短的分组会把少数几天的筹码放大成「分布」）
MIN_DAYS = 20


@dataclass(slots=True)
class ChipResult:
    trade_date: object
    price: float
    profit_ratio: float
    avg_cost: float
    cost_90: tuple[float, float]
    cost_70: tuple[float, float]
    concentration_90: float | None
    concentration_70: float | None
    peak_price: float
    histogram: list[tuple[float, float]]
    days: int


def _triangular_weights(
    grid: np.ndarray, low: float, high: float, avg: float
) -> np.ndarray:
    """当日筹码在价格网格上的三角分布权重（峰值在均价，面积归一成 1）。"""
    weights = np.zeros_like(grid)
    if not np.isfinite([low, high, avg]).all() or high < low:
        return weights
    if high - low < 1e-9:  # 一字板：全部堆在一个价位
        weights[np.argmin(np.abs(grid - low))] = 1.0
        return weights
    avg = min(max(avg, low), high)
    left = (grid >= low) & (grid <= avg)
    right = (grid > avg) & (grid <= high)
    if avg - low > 1e-9:
        weights[left] = (grid[left] - low) / (avg - low)
    else:
        weights[left] = 1.0
    if high - avg > 1e-9:
        weights[right] = (high - grid[right]) / (high - avg)
    else:
        weights[right] = 1.0
    total = weights.sum()
    if total > 0:
        return weights / total
    # 当日振幅窄于网格步长时可能一个点都没落进去；映射到最近网格点，
    # 不能跳过 —— 跳过会把它那天的换手衰减也一起丢掉，低波动股会累积成明显偏差。
    weights[np.argmin(np.abs(grid - avg))] = 1.0
    return weights


def _adjust_to_latest(rows: list[dict]) -> None:
    """把 `rows` 的 high/low/close **就地**换算成以最后一根为基准的前复权价。

    推导：`pct_chg` 是前复权收益，于是 `adj_close[i] = adj_close[i+1] ÷ (1+pct[i+1]/100)`，
    且 `adj_close[-1] = close[-1]`。比例 `scale[i] = adj_close[i] / close[i]` 同时适用于
    当日的 high / low（同一根 K 线的缩放系数相同）。

    ⚠️ 某一步缺 `pct_chg` 时**不做换算**（`scale` 原地继承）：缺了官方涨跌幅就分不清
    「真实涨跌」与「除权」。原来把它当成「收益 0」，等于认定那一段价差全部来自除权 ——
    真实腰斩（无除权）会被整段归一到最后一天，分布被压成一根针（2026-10-28 复查）。
    """
    size = len(rows)
    scales = [1.0] * size
    for index in range(size - 2, -1, -1):
        current = rows[index]
        nxt = rows[index + 1]
        close = float(current["close"])
        next_close = float(nxt["close"])
        pct = nxt.get("pct_chg")
        if pct is None:
            # 没有官方涨跌幅：不猜除权，保持与后一根相同的缩放系数（等价于不复权）
            scales[index] = scales[index + 1]
            continue
        denominator = close * (1.0 + float(pct) / 100.0)
        if denominator <= 0 or next_close <= 0:
            scales[index] = scales[index + 1]
            continue
        scales[index] = scales[index + 1] * next_close / denominator
    for row, scale in zip(rows, scales, strict=True):
        if scale == 1.0:
            continue
        for key in ("high", "low", "close"):
            row[key] = float(row[key]) * scale


def distribute(
    rows: list[dict], *, grid_size: int = GRID_SIZE, decay: float = DECAY
) -> ChipResult:
    """按升序的日线行算筹码分布。

    `rows` 每项需含 `trade_date / high / low / close / turnover`（turnover 是百分数，
    0.31 表示 0.31%）。缺字段或值非正的行会被丢掉。
    """
    usable = [
        row
        for row in rows
        if row.get("high") is not None
        and row.get("low") is not None
        and row.get("close") is not None
        and row.get("turnover") is not None
        and row["high"] > 0
    ]
    usable.sort(key=lambda row: row["trade_date"])
    if len(usable) < MIN_DAYS:
        raise ValueError(f"有效日线只有 {len(usable)} 根，不足 {MIN_DAYS} 根")
    # 除权日不再跳空（见模块说明）：先把价格换算到最新一根的价位上
    _adjust_to_latest(usable)

    low = min(float(row["low"]) for row in usable)
    high = max(float(row["high"]) for row in usable)
    pad = (high - low) * 0.02 or max(low * 0.02, 0.01)
    grid = np.linspace(low - pad, high + pad, grid_size)

    chips: np.ndarray | None = None
    for row in usable:
        turnover = min(max(float(row["turnover"]) / 100.0 * decay, 0.0), 1.0)
        avg = (float(row["high"]) + float(row["low"]) + float(row["close"])) / 3.0
        weights = _triangular_weights(
            grid, float(row["low"]), float(row["high"]), avg
        )
        if weights.sum() <= 0:
            continue
        if chips is None:
            chips = weights.copy()  # 首日分布 = 期初全部流通筹码
            continue
        chips = chips * (1.0 - turnover) + weights * turnover

    if chips is None:
        raise ValueError("所有交易日的价格区间都无效，无法构建分布")
    chips = chips / chips.sum()

    price = float(usable[-1]["close"])
    cumulative = np.cumsum(chips)

    def price_at(quantile: float) -> float:
        return float(np.interp(quantile, cumulative, grid))

    p05, p15, p85, p95 = (price_at(q) for q in (0.05, 0.15, 0.85, 0.95))
    return ChipResult(
        trade_date=usable[-1]["trade_date"],
        price=price,
        profit_ratio=float(chips[grid <= price].sum()),
        avg_cost=float((grid * chips).sum()),
        cost_90=(p05, p95),
        cost_70=(p15, p85),
        concentration_90=(p95 - p05) / (p95 + p05) if p95 + p05 else None,
        concentration_70=(p85 - p15) / (p85 + p15) if p85 + p15 else None,
        peak_price=float(grid[int(np.argmax(chips))]),
        histogram=[
            (float(price_point), float(weight))
            for price_point, weight in zip(grid, chips, strict=True)
            if weight > 1e-6
        ],
        days=len(usable),
    )


def for_stock(code: str, days: int = DEFAULT_DAYS) -> ChipResult | None:
    """读一只票最近 `days` 根日线并算筹码分布；日线不足时返回 None。"""
    with session_scope() as session:
        rows = [
            {
                "trade_date": row.trade_date,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "turnover": row.turnover,
                # 前复权要用它（见 `_adjust_to_latest`）；`distribute` 的其它调用方
                # 没有这一列时按「无收益」处理，等价于不复权
                "pct_chg": row.pct_chg,
            }
            for row in session.scalars(
                select(StockDaily)
                .where(StockDaily.code == code)
                .order_by(StockDaily.trade_date.desc())
                .limit(days)
            )
        ]
    try:
        return distribute(rows)
    except ValueError as exc:
        logger.info("%s 的筹码分布跳过：%s", code, exc)
        return None
