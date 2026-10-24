"""量价因子横截面 IC：这批常见因子在 A 股里到底有没有预测力。

## 为什么要先算 IC 再谈选股

形态是「是 / 否」，因子是「连续的强弱」。把因子直接塞进选股之前，先得回答一个更基础
的问题：**今天的因子值，和未来几天的收益有横截面相关性吗**。这就是 IC（信息系数）：

    IC_t = Spearman( 因子_t 的横截面排名, 未来 h 日收益的横截面排名 )

只有 IC 的均值稳定偏离 0（且 IC 的波动不把它吃掉），这个因子才值得用来排序。
IR = mean(IC) / std(IC)，t 值 = IR × √天数。

## 数据

默认用 `backend/data/history.db`（5 年、约 4900 只，`scripts/backfill_history.py` 补的），
`--db main` 切到主库（400 个交易日）。**零外部请求**。

## 因子（Alpha191 里挑的、只用日线能算的）

| key | 含义 | 方向 |
| --- | --- | --- |
| `rev5` / `rev20` | 5 / 20 日反转（取负号） | 涨多了看跌 |
| `mom60` | 60 日动量 | 涨多了看涨 |
| `volratio` | 当日量 ÷ 20 日均量 − 1 | 放量 |
| `amp20` | 20 日平均振幅 (高−低)/收 | 波动 |
| `upshadow20` | 20 日平均上影线占比 | 抛压 |
| `illiq20` | 20 日平均 |涨跌幅| ÷ 成交额（亿） | 流动性溢价（Amihud） |
| `maxret20` | 20 日内最大单日涨幅 | 彩票效应 |
| `corr20` | 20 日收盘价与成交量的相关系数 | 量价配合 |
| `gap20` | 20 日平均隔夜跳空 | 情绪 |

## 用法

    python scripts/factor_ic.py                      # 5 年历史，5 日持有期
    python scripts/factor_ic.py --horizon 10         # 换成 10 日
    python scripts/factor_ic.py --stocks 1500        # 限样本（调试用）
"""

import argparse
import logging
import math
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import StockDaily  # noqa: E402

logger = logging.getLogger("factor_ic")

HISTORY_DB = BACKEND / "data" / "history.db"
MAIN_DB = BACKEND / "data" / "fupan.db"

#: 因子窗口
FACTOR_WINDOW = 20
#: 一天至少要有这么多只票才算一个有效的横截面
MIN_CROSS_SECTION = 300


def _rolling_sum(values: np.ndarray, window: int) -> np.ndarray:
    """沿时间轴（axis=0）的滚动和。NaN 按 0 累加，配 `_rolling_count` 判断有效性。"""
    filled = np.nan_to_num(values, nan=0.0)
    cumulative = np.cumsum(filled, axis=0)
    out = np.zeros_like(cumulative)
    out[:window] = cumulative[:window]
    out[window:] = cumulative[window:] - cumulative[:-window]
    return out


def _rolling_count(values: np.ndarray, window: int) -> np.ndarray:
    return _rolling_sum((~np.isnan(values)).astype(np.float32), window)


def _rolling_mean(values: np.ndarray, window: int, min_ratio: float = 0.9) -> np.ndarray:
    total = _rolling_sum(values, window)
    count = _rolling_count(values, window)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = total / count
    mean[count < window * min_ratio] = np.nan
    return mean


def _rolling_max(values: np.ndarray, window: int) -> np.ndarray:
    """滚动最大值（窗口内要有足够多的有效行，否则 NaN）。

    `numpy.lib.stride_tricks.sliding_window_view` 会造一个 (T, N, window) 的视图，
    对 1300×4900×20 来说一展开就是 500MB —— 这里改成按偏移逐层取最大值。
    """
    out = np.full_like(values, np.nan)
    for offset in range(window):
        shifted = np.full_like(values, np.nan)
        if offset == 0:
            shifted[:] = values
        else:
            shifted[offset:] = values[:-offset]
        out = np.where(np.isnan(out), shifted, np.fmax(out, shifted))
    out[_rolling_count(values, window) < window] = np.nan
    return out


def _compounded(logs: np.ndarray, window: int, min_ratio: float = 0.9) -> np.ndarray:
    """窗口内的复利涨幅（%）：`exp(Σ log(1+pct/100)) − 1`。"""
    total = _rolling_sum(logs, window)
    count = _rolling_count(logs, window)
    out = (np.exp(total) - 1) * 100
    out[count < window * min_ratio] = np.nan
    return out


def _rolling_corr(x: np.ndarray, y: np.ndarray, window: int) -> np.ndarray:
    """滚动 Pearson 相关（量价配合用）。"""
    n = _rolling_count(x, window) * (1.0)
    sx = _rolling_sum(x, window)
    sy = _rolling_sum(y, window)
    sxx = _rolling_sum(x * x, window)
    syy = _rolling_sum(y * y, window)
    sxy = _rolling_sum(x * y, window)
    with np.errstate(invalid="ignore", divide="ignore"):
        cov = sxy / n - (sx / n) * (sy / n)
        varx = sxx / n - (sx / n) ** 2
        vary = syy / n - (sy / n) ** 2
        corr = cov / np.sqrt(varx * vary)
    corr[n < window * 0.9] = np.nan
    return corr


def _rankdata(values: np.ndarray) -> np.ndarray:
    """平均秩（并列取同一档），与 `scipy.stats.rankdata` 同口径。"""
    order = np.argsort(values, kind="mergesort")
    sorted_values = values[order]
    ranks = np.empty(values.size, dtype=np.float64)
    boundaries = np.flatnonzero(np.diff(sorted_values) != 0) + 1
    starts = np.concatenate(([0], boundaries))
    ends = np.concatenate((boundaries, [values.size]))
    ranks[order] = np.repeat((starts + ends - 1) / 2.0, ends - starts)
    return ranks


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    rx = _rankdata(x)
    ry = _rankdata(y)
    rx -= rx.mean()
    ry -= ry.mean()
    denominator = math.sqrt(float((rx * rx).sum()) * float((ry * ry).sum()))
    if denominator <= 0:
        return float("nan")
    return float((rx * ry).sum() / denominator)


def _load(db_path: Path, bars: int, stocks: int) -> tuple[list, list[str], dict[str, np.ndarray]]:
    """把日线读成 `(交易日, 代码) -> 矩阵`，价格与量额都转成 float32。"""
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with Session(engine) as session:
        # 交易日直接取 `stock_daily` 自己的日期：history.db 里**没有** trade_calendar
        # （它是 backfill_history 单独建的一个精简库），用日线自己的并集两个库都能跑。
        dates = list(
            session.scalars(
                select(StockDaily.trade_date).distinct().order_by(StockDaily.trade_date)
            )
        )
        if not dates:
            raise SystemExit("stock_daily 是空的")
        dates = dates[-bars:]
        start = dates[0]
        # 取**窗口内出现过**的全部代码（不只是最后一天还在的）：否则退市股整段缺席，
        # 横截面会带上幸存者偏差。
        codes = list(
            session.scalars(
                select(StockDaily.code)
                .where(StockDaily.trade_date >= start)
                .distinct()
            )
        )
        codes.sort()
        if stocks:
            codes = codes[:stocks]

        date_pos = {day: index for index, day in enumerate(dates)}
        code_pos = {code: index for index, code in enumerate(codes)}
        shape = (len(dates), len(codes))
        fields = ("open", "high", "low", "close", "volume", "amount", "pct_chg")
        arrays = {name: np.full(shape, np.nan, dtype=np.float32) for name in fields}

        rows = session.execute(
            select(
                StockDaily.trade_date,
                StockDaily.code,
                StockDaily.open,
                StockDaily.high,
                StockDaily.low,
                StockDaily.close,
                StockDaily.volume,
                StockDaily.amount,
                StockDaily.pct_chg,
            )
            .where(
                StockDaily.trade_date >= start,
                StockDaily.code.in_(codes),
            )
            .execution_options(yield_per=50000)
        )
        for day, code, open_, high, low, close, volume, amount, pct in rows:
            row = date_pos.get(day)
            col = code_pos.get(code)
            if row is None or col is None:
                continue
            if open_ is not None:
                arrays["open"][row, col] = open_
            if high is not None:
                arrays["high"][row, col] = high
            if low is not None:
                arrays["low"][row, col] = low
            if close is not None:
                arrays["close"][row, col] = close
            if volume is not None:
                arrays["volume"][row, col] = volume
            if amount is not None:
                arrays["amount"][row, col] = amount
            if pct is not None:
                arrays["pct_chg"][row, col] = pct
    return dates, codes, arrays


def _factors(arrays: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """每个因子一张 (交易日, 代码) 矩阵；NaN = 那天算不出来。"""
    close = arrays["close"]
    high = arrays["high"]
    low = arrays["low"]
    open_ = arrays["open"]
    volume = arrays["volume"]
    amount = arrays["amount"]
    pct = arrays["pct_chg"]
    logs = np.log1p(pct / 100.0)

    previous_close = np.full_like(close, np.nan)
    previous_close[1:] = close[:-1]
    gap = open_ / previous_close - 1

    factors = {
        # 短期反转：涨得多的后面看跌（A 股最常见的一类）
        "rev5": -_compounded(logs, 5),
        "rev20": -_compounded(logs, FACTOR_WINDOW),
        # 中期动量：与反转对着看，谁占上风是这一轮要回答的问题
        "mom60": _compounded(logs, 60),
        # 量能：当日量相对 20 日均量的倍数
        "volratio": volume / _rolling_mean(volume, FACTOR_WINDOW) - 1,
        # 波动与抛压
        "amp20": _rolling_mean((high - low) / close, FACTOR_WINDOW),
        "upshadow20": _rolling_mean((high - np.maximum(open_, close)) / close, FACTOR_WINDOW),
        # 流动性（Amihud）：单位成交额对应的涨跌幅，越大越不流动
        "illiq20": _rolling_mean(np.abs(pct) / (amount / 1e8), FACTOR_WINDOW),
        # 彩票效应：20 日内最大单日涨幅
        "maxret20": _rolling_max(pct, FACTOR_WINDOW),
        # 量价配合（20 日收盘与成交量的相关系数）
        "corr20": _rolling_corr(close, volume, FACTOR_WINDOW),
        # 隔夜情绪
        "gap20": _rolling_mean(gap, FACTOR_WINDOW),
    }
    return factors


def main() -> int:
    parser = argparse.ArgumentParser(description="量价因子横截面 IC")
    parser.add_argument("--db", choices=("history", "main"), default="history")
    parser.add_argument("--bars", type=int, default=1300, help="用多少根日线（默认 1300 ≈ 5 年）")
    parser.add_argument("--horizon", type=int, default=5, help="持有期（交易日）")
    parser.add_argument("--stocks", type=int, default=0, help="只用前 N 只（0 = 全部）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    db_path = HISTORY_DB if args.db == "history" else MAIN_DB
    if not db_path.is_file():
        raise SystemExit(f"没有 {db_path}")

    dates, codes, arrays = _load(db_path, args.bars, args.stocks)
    logger.info(
        "载入 %d 个交易日 × %d 只票（%s ~ %s，库 %s）",
        len(dates),
        len(codes),
        dates[0],
        dates[-1],
        db_path.name,
    )

    pct = arrays["pct_chg"]
    logs = np.log1p(pct / 100.0)
    # 未来 h 日收益：exp(Σ_{t+1..t+h} log(1+pct)) − 1；持有期内每天都要有涨跌幅
    cumulative = np.cumsum(np.nan_to_num(logs, nan=0.0), axis=0)
    valid = ~np.isnan(logs)
    counts = np.cumsum(valid.astype(np.int32), axis=0)
    h = args.horizon
    forward = np.full(pct.shape, np.nan, dtype=np.float32)
    forward[:-h] = ((np.exp(cumulative[h:] - cumulative[:-h]) - 1) * 100).astype(
        np.float32
    )
    full = (counts[h:] - counts[:-h]) == h
    forward[:-h][~full] = np.nan

    factors = _factors(arrays)
    results = []
    for key, matrix in factors.items():
        ics: list[float] = []
        spreads: list[float] = []
        sizes: list[int] = []
        for row in range(len(dates) - h):
            factor_values = matrix[row]
            returns = forward[row]
            mask = ~np.isnan(factor_values) & ~np.isnan(returns)
            count = int(mask.sum())
            if count < MIN_CROSS_SECTION:
                continue
            f = factor_values[mask].astype(np.float64)
            r = returns[mask].astype(np.float64)
            ic = _spearman(f, r)
            if math.isnan(ic):
                continue
            ics.append(ic)
            sizes.append(count)
            # 五分组多空：最高组 − 最低组
            rank = _rankdata(f) / count
            top = r[rank >= 0.8].mean()
            bottom = r[rank < 0.2].mean()
            spreads.append(float(top - bottom))
        if not ics:
            continue
        arr = np.array(ics)
        days = len(ics)
        mean_ic = float(arr.mean())
        std_ic = float(arr.std(ddof=1)) if days > 1 else float("nan")
        ir = mean_ic / std_ic if std_ic and not math.isnan(std_ic) else float("nan")
        t_stat = ir * math.sqrt(days) if not math.isnan(ir) else float("nan")
        results.append(
            {
                "key": key,
                "days": days,
                "ic": mean_ic,
                "ir": ir,
                "t": t_stat,
                "positive": float((arr > 0).mean()),
                "spread": float(np.mean(spreads)),
                "size": int(np.median(sizes)),
            }
        )

    results.sort(key=lambda item: item["ic"], reverse=True)
    print()
    print(f"持有期 {h} 日；横截面中位 {results[0]['size'] if results else 0} 只；IC 用 Spearman")
    print()
    print(
        f"{'因子':<12}{'有效天数':>8}{'均值IC':>9}{'IR':>7}{'t值':>8}"
        f"{'IC>0占比':>9}{'多空差(前20%-后20%)':>20}"
    )
    print("-" * 76)
    for item in results:
        print(
            f"{item['key']:<12}{item['days']:>8}{item['ic']:>9.4f}{item['ir']:>7.2f}"
            f"{item['t']:>8.2f}{item['positive'] * 100:>8.1f}%{item['spread']:>19.2f}%"
        )
    print()
    print("判读：|t| < 2 基本等于没有稳定信号；多空差要看它是否明显大于 0（未扣费用）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
