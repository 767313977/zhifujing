"""低吸反转策略的实盘可行性研究（信号层）。

## 只回答一个问题

「高流动性股票里的短期反转 / 低振幅信号，在 D+1 开盘入场、扣掉往返成本之后，
是否还能给出有价值的排序？」

不新增指标、不改生产权重、不改默认选股参数。

## 四组预定义对照

| 策略 | 排序分 |
| --- | --- |
| A | `rev20` 原始值降序（跌得越多越靠前） |
| B | `0.5 × rev20 分位 + 0.5 × (−amp20) 分位`（固定 50/50，不调） |
| C | 生产 `factor_score.score_series()` 的 `score`（0.3 / 0.3 / 0.4） |
| D | 同 C，但先按信号日历史 20 日均额取高流动性前 50% / 25% |

## 时点与成交

- 因子只用 **D 日及之前** 的行情（D 日收盘后可知）；
- 入场 **D+1 开盘**（`entry_mode="next_open"`），成交/退出/跌停一律交给
  `services.research_backtest` 的实现，不另写一套；
- 成本是**往返综合**：10 / 30 / 50 bps 三档敏感性；
- 没有完整退出价 → 内核标 `pending`，不计入已实现收益。

## 数据边界

只读 `backend/data/history.db`（`mode=ro`），不打开主库、不打开任何演练库，
不补采外部数据。研究库缺退市股 → 结果必须带 `SURVIVORSHIP_BIAS`。
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import StockDaily  # noqa: E402
from app.services import factor_score  # noqa: E402
from app.services import rps as rps_rules  # noqa: E402
from app.services.research_backtest import (  # noqa: E402
    Benchmark,
    Signal,
    load_matrix,
    run_signals,
)

logger = logging.getLogger("reversal")

HISTORY_DB = BACKEND / "data" / "history.db"

WINDOW = 20
LIQUIDITY_WINDOW = 20
TOP_N = 20
HORIZONS = (1, 5, 10)
COSTS = (10.0, 30.0, 50.0)


@dataclass(frozen=True)
class Strategy:
    key: str
    label: str
    factors: tuple[str, ...]
    liquidity: str  # "all" / "top50" / "top25"


STRATEGIES: tuple[Strategy, ...] = (
    Strategy("A", "A rev20 单因子", ("rev20",), "all"),
    Strategy("B", "B rev20+低振幅", ("rev20", "amp20"), "all"),
    Strategy("C", "C 生产三因子", ("rev20", "illiq20", "amp20"), "all"),
    Strategy("D50", "D 三因子+高流动前50%", ("rev20", "illiq20", "amp20"), "top50"),
    Strategy("D25", "D 三因子+高流动前25%", ("rev20", "illiq20", "amp20"), "top25"),
)


def _readonly_engine(path: Path):
    return create_engine(f"sqlite:///file:{path.as_posix()}?mode=ro&uri=true")


def _code_columns(matrix) -> dict[str, np.ndarray]:
    """每只票「有行情的交易日位置」数组，供窗口切片用。"""
    return {
        code: np.nonzero(matrix.present[:, j])[0]
        for j, code in enumerate(matrix.codes)
    }


def _build_series(matrix, cols, pos: int, code: str, j: int) -> list[tuple] | None:
    positions = cols[code]
    end = int(np.searchsorted(positions, pos, side="right"))
    if end < WINDOW:
        return None
    idx = positions[end - WINDOW : end]
    out = []
    for i in idx:
        amount = matrix.amount[i, j]
        out.append(
            (
                matrix.calendar[int(i)],
                float(matrix.pct[i, j]),
                float(matrix.high[i, j]),
                float(matrix.low[i, j]),
                float(matrix.close[i, j]),
                float(amount) if np.isfinite(amount) else 0.0,
            )
        )
    return out


def _liquidity(matrix, positions, pos: int, j: int) -> float | None:
    """信号日历史 20 日均额（只用 `pos` 及之前的行，不碰未来）。"""
    end = int(np.searchsorted(positions, pos, side="right"))
    if end < LIQUIDITY_WINDOW:
        return None
    idx = positions[end - LIQUIDITY_WINDOW : end]
    values = matrix.amount[idx, j]
    values = values[np.isfinite(values)]
    if values.size == 0:
        return None
    return float(values.mean())


def _factor_percentiles(scores: dict) -> dict[str, dict[str, float]]:
    """三个因子的**池内分位**（缺失按 0，与生产 `score_series` 同一语义）。

    amp20 取负后再排名：振幅越低分位越高。
    """
    codes = list(scores)
    out: dict[str, dict[str, float]] = {code: {} for code in codes}
    for key in ("rev20", "illiq20", "amp20"):
        raw = []
        for code in codes:
            value = getattr(scores[code], key)
            if value is None:
                raw.append(None)
            else:
                raw.append(-value if key == "amp20" else value)
        ordered = rps_rules.percentiles([v for v in raw if v is not None])
        cursor = 0
        for index, code in enumerate(codes):
            if raw[index] is None:
                out[code][key] = 0.0
            else:
                out[code][key] = ordered[cursor]
                cursor += 1
    return out


def _rank_top(scored: list[tuple[str, float]], top_n: int) -> list[str]:
    """确定性排序：分数降序，同分按代码升序。"""
    scored.sort(key=lambda item: (-item[1], item[0]))
    return [code for code, _score in scored[:top_n]]


def build_signals(
    matrix,
    *,
    start: date | None = None,
    end: date | None = None,
    top_n: int = TOP_N,
) -> dict[str, list[Signal]]:
    """逐交易日生成四组策略的信号（全部只用 D 日及之前的数据）。"""
    cols = _code_columns(matrix)
    codes = matrix.codes
    j_of = {code: j for j, code in enumerate(codes)}
    signals: dict[str, list[Signal]] = {s.key: [] for s in STRATEGIES}
    days_used = 0

    for pos, day in enumerate(matrix.calendar):
        if start is not None and day < start:
            continue
        if end is not None and day > end:
            continue
        # 当天有行情的票才可能是候选（停牌票当天没有可用信号）
        today = [code for code in codes if matrix.present[pos, j_of[code]]]
        if len(today) < 30:
            continue

        series: dict[str, list[tuple]] = {}
        liquidity: dict[str, float] = {}
        for code in today:
            j = j_of[code]
            window = _build_series(matrix, cols, pos, code, j)
            if window is None:
                continue
            series[code] = window
            value = _liquidity(matrix, cols[code], pos, j)
            if value is not None:
                liquidity[code] = value
        if len(series) < 30:
            continue

        scores = factor_score.score_series(series)
        percent = _factor_percentiles(scores)
        days_used += 1

        # ---- A：rev20 原始值降序 ----
        ranked_a = [
            (code, scores[code].rev20)
            for code in scores
            if scores[code].rev20 is not None
        ]
        for code in _rank_top(ranked_a, top_n):
            signals["A"].append(Signal(code=code, signal_date=day, meta={"score": scores[code].rev20}))

        # ---- B：rev20 分位 + (−amp20) 分位，固定 50/50 ----
        ranked_b = [
            (code, 0.5 * percent[code]["rev20"] + 0.5 * percent[code]["amp20"])
            for code in scores
        ]
        for code in _rank_top(ranked_b, top_n):
            signals["B"].append(Signal(code=code, signal_date=day, meta={}))

        # ---- C：生产三因子分 ----
        ranked_c = [(code, scores[code].score) for code in scores]
        top_c = _rank_top(ranked_c, top_n)
        for code in top_c:
            signals["C"].append(Signal(code=code, signal_date=day, meta={"score": scores[code].score}))

        # ---- D：同 C 评分，先按历史成交额分层 ----
        liq_ranked = sorted(liquidity.items(), key=lambda item: (-item[1], item[0]))
        for key, ratio in (("D50", 0.50), ("D25", 0.25)):
            keep = {code for code, _v in liq_ranked[: max(top_n, int(len(liq_ranked) * ratio))]}
            ranked_d = [(code, score) for code, score in ranked_c if code in keep]
            for code in _rank_top(ranked_d, top_n):
                signals[key].append(Signal(code=code, signal_date=day, meta={}))

    logger.info("生成信号：%d 个有效信号日", days_used)
    return signals


def _aggregate_year(result) -> dict[int, dict[int, dict]]:
    """按信号年份聚合（只用于历史时间分段，不称样本外验证）。"""
    by_year: dict[int, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    for sample in result.samples:
        if sample.status != "ok" or sample.gross is None:
            continue
        bucket = by_year[sample.signal_date.year][sample.horizon]
        bucket.append((sample.gross, sample.net, sample.excess))
    out: dict[int, dict[int, dict]] = {}
    for year, horizons in by_year.items():
        out[year] = {}
        for horizon, rows in horizons.items():
            gross = np.array([row[0] for row in rows], dtype=float)
            net = np.array([row[1] for row in rows if row[1] is not None], dtype=float)
            excess = np.array(
                [row[2] for row in rows if row[2] is not None], dtype=float
            )
            out[year][horizon] = {
                "n": len(rows),
                "gross": float(gross.mean()),
                "net": float(net.mean()) if net.size else float("nan"),
                "excess": float(excess.mean()) if excess.size else float("nan"),
                "win": float((gross > 0).mean()),
            }
    return out


def _report(result, cost: float) -> None:
    stat = result.horizons.get(5)
    print()
    print(f"== {result.strategy}  (cost={cost:.0f}bps, entry={result.entry_mode}) ==")
    counts = stat.counts if stat else {}
    print(
        f"   观察样本 {stat.observation_samples if stat else 0}"
        f" / 可成交 {stat.execution_samples if stat else 0}"
        f" / 买不到 {counts.get('entry_limit_up', 0)}"
        f" / 跌停退出 {counts.get('exit_limit_down', 0)}"
        f" / pending {counts.get('pending', 0)}"
        f" / 无行情 {counts.get('no_quote', 0)}"
    )
    if stat is None:
        return
    print(
        f"   5日 毛均值 {stat.mean:+.2f}%  净均值 {stat.net_mean:+.2f}%  "
        f"中位 {stat.median:+.2f}%  胜率 {stat.pos_pct:.1f}%  "
        f"超额 {stat.excess_mean:+.2f}%  MAE {stat.mae_mean:+.2f}%"
    )
    for horizon in HORIZONS:
        item = result.horizons.get(horizon)
        if item is None:
            continue
        print(
            f"   {horizon:>2}日: 毛 {item.mean:+.2f}% 净 {item.net_mean:+.2f}% "
            f"超额 {item.excess_mean:+.2f}% 胜率 {item.pos_pct:.1f}% n={item.execution_samples}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="低吸反转策略信号层研究")
    parser.add_argument("--db", default=str(HISTORY_DB), help="只读研究库路径")
    parser.add_argument("--start", default="", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default="", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=TOP_N, help="每日取前 N 只")
    parser.add_argument(
        "--costs", default=",".join(str(int(c)) for c in COSTS), help="成本档（bps，逗号分隔）"
    )
    parser.add_argument("--detail", action="store_true", help="输出年度分段")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    db_path = Path(args.db)
    if not db_path.is_file():
        raise SystemExit(f"没有 {db_path}")

    engine = _readonly_engine(db_path)
    with Session(engine) as session:
        latest = session.scalar(select(func.max(StockDaily.trade_date)))
        earliest = session.scalar(select(func.min(StockDaily.trade_date)))
        if latest is None:
            raise SystemExit("研究库是空的")
        start = date.fromisoformat(args.start) if args.start else earliest
        end = date.fromisoformat(args.end) if args.end else latest
        logger.info("研究区间 %s ~ %s（只读 %s）", start, end, db_path.name)

        matrix = load_matrix(session, start, end)
        if not matrix.calendar:
            raise SystemExit("区间内没有行情")
        logger.info(
            "矩阵：%d 个交易日 × %d 只票", len(matrix.calendar), len(matrix.codes)
        )

        signals = build_signals(matrix, top_n=args.top)
        benchmark = Benchmark(session, matrix, "market")
        costs = [float(item) for item in args.costs.split(",") if item.strip()]

        for strategy in STRATEGIES:
            batch = signals[strategy.key]
            if not batch:
                print(f"\n== {strategy.label} == 无信号")
                continue
            for cost in costs:
                result = run_signals(
                    session,
                    batch,
                    strategy=strategy.label,
                    params={
                        "top": args.top,
                        "factors": list(strategy.factors),
                        "liquidity": strategy.liquidity,
                    },
                    horizons=HORIZONS,
                    entry_mode="next_open",
                    cost_bps=cost,
                    matrix=matrix,
                    benchmark=benchmark,
                )
                _report(result, cost)
                if args.detail and cost == costs[0]:
                    years = _aggregate_year(result)
                    for year in sorted(years):
                        row = years[year].get(5)
                        if not row:
                            continue
                        print(
                            f"   [{year}] n={row['n']:>6} 毛 {row['gross']:+.2f}% "
                            f"净 {row['net']:+.2f}% 超额 {row['excess']:+.2f}% 胜率 {row['win'] * 100:.1f}%"
                        )
    engine.dispose()
    print()
    print("口径提示：信号层研究（SIGNAL_LEVEL_ONLY），不是组合净值；")
    print("         GROSS_ONLY 之外的成本档来自往返综合假设，非真实费率断言；")
    print("         研究库缺退市股 → SURVIVORSHIP_BIAS；历史 ST 名称可能整段为当前名 → ST_STATUS_PARTIAL。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
