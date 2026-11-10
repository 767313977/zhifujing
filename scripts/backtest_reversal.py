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
import random
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
COSTS = (10.0, 30.0, 50.0, 100.0, 200.0)


@dataclass(frozen=True)
class Strategy:
    key: str
    label: str
    mode: str  # raw_rev / rev_amp / trio / trio_no_illiq / trio_shuffled / trio_then_filter
    liquidity: str = "all"  # all / top50 / top25


STRATEGIES: tuple[Strategy, ...] = (
    Strategy("A", "A rev20 单因子", "raw_rev"),
    Strategy("B", "B rev20+低振幅", "rev_amp"),
    Strategy("C0", "C0 三因子（生产）", "trio"),
    Strategy("C1", "C1 去 illiq（归一化）", "trio_no_illiq"),
    Strategy("C2", "C2 illiq 打乱（诊断）", "trio_shuffled"),
    Strategy("L1_50", "L1 全池评分→前50%过滤", "trio", "top50"),
    Strategy("L1_25", "L1 全池评分→前25%过滤", "trio", "top25"),
    Strategy("L2_50", "L2 前50%过滤→重算分位", "trio_then_filter", "top50"),
    Strategy("L2_25", "L2 前25%过滤→重算分位", "trio_then_filter", "top25"),
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


def _shuffled_illiq(percent: dict[str, dict[str, float]], day: date) -> dict[str, float]:
    """同一天内对 illiq 分位做确定性置换（C2 诊断用）。

    分布完全不变，只破坏「分位 — 个股」的对应关系。种子取日期序数，保证可复现。
    """
    codes = list(percent)
    values = [percent[code]["illiq20"] for code in codes]
    order = list(range(len(codes)))
    random.Random(day.toordinal()).shuffle(order)
    return {codes[i]: values[order[i]] for i in range(len(codes))}


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
        codes_today = list(scores)
        shuffled = _shuffled_illiq(percent, day)

        liq_ranked = sorted(liquidity.items(), key=lambda item: (-item[1], item[0]))

        def liq_keep(ratio: float) -> set[str]:
            size = max(top_n, int(len(liq_ranked) * ratio))
            return {code for code, _value in liq_ranked[:size]}

        for strategy in STRATEGIES:
            mode = strategy.mode
            if mode == "raw_rev":
                ranked = [
                    (code, scores[code].rev20)
                    for code in codes_today
                    if scores[code].rev20 is not None
                ]
            elif mode == "rev_amp":
                ranked = [
                    (code, 0.5 * percent[code]["rev20"] + 0.5 * percent[code]["amp20"])
                    for code in codes_today
                ]
            elif mode == "trio":
                ranked = [(code, scores[code].score) for code in codes_today]
            elif mode == "trio_no_illiq":
                ranked = [
                    (
                        code,
                        (0.30 / 0.70) * percent[code]["rev20"]
                        + (0.40 / 0.70) * percent[code]["amp20"],
                    )
                    for code in codes_today
                ]
            elif mode == "trio_shuffled":
                ranked = [
                    (
                        code,
                        0.30 * percent[code]["rev20"]
                        + 0.30 * shuffled[code]
                        + 0.40 * percent[code]["amp20"],
                    )
                    for code in codes_today
                ]
            elif mode == "trio_then_filter":
                keep = liq_keep(0.50 if strategy.liquidity == "top50" else 0.25)
                subset = {code: scores[code] for code in codes_today if code in keep}
                if len(subset) < top_n:
                    continue
                sub_percent = _factor_percentiles(subset)
                ranked = [
                    (
                        code,
                        0.30 * sub_percent[code]["rev20"]
                        + 0.30 * sub_percent[code]["illiq20"]
                        + 0.40 * sub_percent[code]["amp20"],
                    )
                    for code in subset
                ]
            else:  # pragma: no cover - 配置错误直接暴露
                raise ValueError(f"未知策略模式：{mode}")

            # L1：先在全池算分，再按流动性保留（不重新算分位）
            if strategy.liquidity != "all" and mode != "trio_then_filter":
                keep = liq_keep(0.50 if strategy.liquidity == "top50" else 0.25)
                ranked = [(code, score) for code, score in ranked if code in keep]

            for code in _rank_top(ranked, top_n):
                signals[strategy.key].append(
                    Signal(code=code, signal_date=day, meta={})
                )

    logger.info("生成信号：%d 个有效信号日", days_used)
    return signals


def _net(gross: float, cost: float) -> float:
    """与 `research_backtest` 完全相同的净收益公式（cost 为一次往返总成本）。"""
    return ((1 + gross / 100) * (1 - cost / 10000) - 1) * 100


def _ok_samples(result, horizon: int) -> list:
    """正常完成（`ok`）：入场与退出都按预定交易日成交。"""
    return [
        sample
        for sample in result.samples
        if sample.horizon == horizon and sample.status == "ok" and sample.gross is not None
    ]


def _entry_samples(result, horizon: int) -> list:
    """**已入场且有可确定退出价**：`ok` + `exit_limit_down`。

    `exit_limit_down` 的 `exit_price/gross` 是延迟到首个可卖日后的真实价格，
    不是「理论上卖不掉」的跌停价 —— 因此可以计入收益，只是不算预定 5 日退出。
    """
    return [
        sample
        for sample in result.samples
        if sample.horizon == horizon
        and sample.status in ("ok", "exit_limit_down")
        and sample.gross is not None
    ]


def _unresolved_samples(result, horizon: int) -> list:
    """入场成功但**始终没有可确定退出价**（`no_exit`）。`pending` 不算失败，单独统计。"""
    return [
        sample
        for sample in result.samples
        if sample.horizon == horizon and sample.status == "no_exit"
    ]


def _state_groups(result, horizon: int) -> dict[str, int]:
    """互斥终态分组（按引擎真实 `STATUSES` 归类，名称不改写）。"""
    groups: dict[str, int] = defaultdict(int)
    for sample in result.samples:
        if sample.horizon != horizon:
            continue
        groups[sample.status] += 1
    return dict(groups)


def _entry_delayed_count(result, horizon: int) -> int:
    return sum(
        1
        for sample in result.samples
        if sample.horizon == horizon
        and sample.status in ("ok", "exit_limit_down")
        and sample.exit_delay > 0
    )


def _net_excess(samples, cost: float) -> float | None:
    values = [
        _net(sample.gross, cost) - _net(sample.benchmark, cost)
        for sample in samples
        if sample.benchmark is not None
    ]
    if not values:
        return None
    return float(np.mean(values))


def _daily_mean(result, horizon: int = 5) -> dict[date, float]:
    acc: dict[date, list[float]] = defaultdict(list)
    for sample in _ok_samples(result, horizon):
        acc[sample.signal_date].append(sample.gross)
    return {day: float(np.mean(values)) for day, values in acc.items()}


def _nw_t(values: list[float], lag: int) -> float:
    arr = np.asarray(values, dtype=float)
    n = arr.size
    if n < 3:
        return float("nan")
    mean = float(arr.mean())
    dev = arr - mean
    var = float(dev @ dev) / n
    for offset in range(1, min(lag, n - 1) + 1):
        var += 2.0 * (1.0 - offset / (lag + 1.0)) * float(dev[offset:] @ dev[:-offset]) / n
    if var <= 0:
        return float("nan")
    return mean / (var / n) ** 0.5


def _paired(a: dict[date, float], b: dict[date, float]) -> dict:
    days = sorted(set(a) & set(b))
    diffs = [a[day] - b[day] for day in days]
    if not diffs:
        return {"days": 0}
    t_stat = _nw_t(diffs, 4)
    mean = float(np.mean(diffs))
    se = abs(mean / t_stat) if t_stat and np.isfinite(t_stat) and t_stat != 0 else float("nan")
    return {
        "days": len(days),
        "mean": mean,
        "t": t_stat,
        "low": mean - 1.96 * se if np.isfinite(se) else float("nan"),
        "high": mean + 1.96 * se if np.isfinite(se) else float("nan"),
    }


def _print_summary(results: dict, costs: list[float]) -> None:
    header = f"{'策略':<24}{'可成交':>8}{'5日毛':>9}" + "".join(f"{int(c):>8}" for c in costs)
    header += f"{'毛超额':>9}{'净超额30':>10}{'中位':>9}{'胜率':>8}{'MAE':>9}"
    print()
    print("五组策略 · 5 日持有 · D+1 开盘入场 · 毛/净均为**仅正常完成样本**(cond) · 成本列为净收益(%)")
    print("-" * len(header))
    print(header)
    print("-" * len(header))
    for key, result in results.items():
        stat = result.horizons.get(5)
        if stat is None:
            continue
        samples = _ok_samples(result, 5)
        cells = []
        for cost in costs:
            values = [_net(sample.gross, cost) for sample in samples]
            cells.append(float(np.mean(values)) if values else float("nan"))
        net_ex = _net_excess(samples, 30.0)
        print(
            f"{result.strategy:<24}{stat.execution_samples:>8}{stat.mean:>8.2f}%"
            + "".join(f"{value:>7.2f}%" for value in cells)
            + f"{stat.excess_mean:>8.2f}%"
            + (f"{net_ex:>9.2f}%" if net_ex is not None else f"{'N/A':>10}")
            + f"{stat.median:>8.2f}%{stat.pos_pct:>7.1f}%{stat.mae_mean:>8.2f}%"
        )
    print("-" * len(header))


def _print_status(results: dict) -> None:
    print()
    print("互斥终态计数（5 日；名称直接取引擎 `STATUSES`，不改写）")
    print("-" * 108)
    keys = ("ok", "exit_limit_down", "no_exit", "pending", "unbuyable",
            "suspended", "no_quote", "price_invalid", "new_listing")
    print(f"{'策略':<24}" + "".join(f"{k:>15}" for k in keys) + f"{'合计':>9}")
    for result in results.values():
        groups = _state_groups(result, 5)
        total = sum(groups.values())
        print(
            f"{result.strategy:<24}"
            + "".join(f"{groups.get(k, 0):>15}" for k in keys)
            + f"{total:>9}"
        )
    print("不变量：ok+exit_limit_down+no_exit+pending+其余 = 全部信号；")
    print("        exit_limit_down 是「入场成功、退出延迟到首个可卖日」，已有真实退出价；")
    print("        no_exit 是「入场成功但延迟上限内仍无有效价格」，pending 是「期末未到退出日」。")


def _print_exit_sensitivity(results: dict) -> None:
    print()
    print("退出状态敏感性（5 日；cond = 仅正常完成，all = 含延迟退出，压力 = 未解决按固定亏损计）")
    print("-" * 118)
    print(
        f"{'策略':<24}{'ok_n':>8}{'+延迟_n':>10}{'cond毛':>9}{'all毛':>9}"
        f"{'all净30':>10}{'未解决':>9}{'占比':>8}{'压力-10%':>10}{'压力-20%':>10}"
    )
    for result in results.values():
        ok = _ok_samples(result, 5)
        entry = _entry_samples(result, 5)
        unresolved = _unresolved_samples(result, 5)
        if not entry:
            continue
        cond_gross = float(np.mean([s.gross for s in ok])) if ok else float("nan")
        all_gross = float(np.mean([s.gross for s in entry]))
        all_net = float(np.mean([_net(s.gross, 30.0) for s in entry]))
        base = sum(s.gross for s in entry)
        n_unres = len(unresolved)
        share = n_unres / (len(entry) + n_unres) * 100 if n_unres else 0.0
        stress = []
        for loss in (10.0, 20.0):
            total = len(entry) + n_unres
            stress.append((base - loss * n_unres) / total if total else float("nan"))
        print(
            f"{result.strategy:<24}{len(ok):>8}{len(entry):>10}"
            f"{cond_gross:>8.2f}%{all_gross:>8.2f}%{all_net:>9.2f}%"
            f"{n_unres:>9}{share:>7.1f}%" + "".join(f"{v:>9.2f}%" for v in stress)
        )
    print("压力两列**不是历史成交结果**，只是「若未解决样本亏 10% / 20%」的敏感性。")


def _print_pairs(results: dict) -> None:
    pairs = [("C0", "C1"), ("C0", "C2"), ("L1_50", "L2_50"), ("L1_25", "L2_25")]
    print()
    print("同信号日配对差（5 日毛收益；人群 = 已入场且有退出价；HAC lag=4）")
    print("-" * 108)
    print(
        f"{'比较':<16}{'共同日':>8}{'A样本':>8}{'B样本':>8}"
        f"{'均值差':>10}{'t':>8}{'95%CI低':>10}{'95%CI高':>10}"
    )
    for left, right in pairs:
        if left not in results or right not in results:
            continue
        a, b = results[left], results[right]
        stat = _paired(_daily_entry_mean(a), _daily_entry_mean(b))
        if not stat["days"]:
            print(f"{left + ' - ' + right:<16}{0:>8}")
            continue
        print(
            f"{left + ' - ' + right:<16}{stat['days']:>8}"
            f"{len(_entry_samples(a, 5)):>8}{len(_entry_samples(b, 5)):>8}"
            f"{stat['mean']:>9.3f}{stat['t']:>8.2f}{stat['low']:>10.3f}{stat['high']:>10.3f}"
        )


def _daily_entry_mean(result, horizon: int = 5) -> dict[date, float]:
    acc: dict[date, list[float]] = defaultdict(list)
    for sample in _entry_samples(result, horizon):
        acc[sample.signal_date].append(sample.gross)
    return {day: float(np.mean(values)) for day, values in acc.items()}


def _print_years(results: dict) -> None:
    print()
    print("历史时间分段（5 日毛超额，%）：2021 年自 9 月起，不是完整自然年")
    print("-" * 96)
    years = sorted({s.signal_date.year for r in results.values() for s in r.samples})
    header = f"{'策略':<24}" + "".join(f"{year:>12}" for year in years) + f"{'有效日':>9}"
    print(header)
    for result in results.values():
        cells = []
        dayset: dict[int, set] = defaultdict(set)
        for sample in _entry_samples(result, 5):
            dayset[sample.signal_date.year].add(sample.signal_date)
        for year in years:
            rows = [
                sample
                for sample in _entry_samples(result, 5)
                if sample.signal_date.year == year and sample.excess is not None
            ]
            cells.append(f"{np.mean([r.excess for r in rows]):>11.2f}" if rows else f"{'—':>12}")
        total_days = sum(len(v) for v in dayset.values())
        print(f"{result.strategy:<24}" + "".join(cells) + f"{total_days:>9}")


def _print_pairs(results: dict) -> None:
    pairs = [("C0", "C1"), ("C0", "C2"), ("L1_50", "L2_50"), ("L1_25", "L2_25")]
    print()
    print("同信号日配对差（5 日毛收益，A − B；HAC lag=4）")
    print("-" * 96)
    print(f"{'比较':<16}{'有效日':>8}{'均值差':>10}{'t':>8}{'95%CI低':>10}{'95%CI高':>10}")
    for left, right in pairs:
        if left not in results or right not in results:
            continue
        stat = _paired(_daily_mean(results[left]), _daily_mean(results[right]))
        if not stat["days"]:
            print(f"{left + ' - ' + right:<16}{0:>8}")
            continue
        print(
            f"{left + ' - ' + right:<16}{stat['days']:>8}{stat['mean']:>9.3f}"
            f"{stat['t']:>8.2f}{stat['low']:>10.3f}{stat['high']:>10.3f}"
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

        # 只跑 cost=0：毛收益与 net 无关，净收益用与引擎**完全相同**的公式逐样本重算，
        # 避免为 5 个成本档重复遍历 2 万余条信号（结果等价，见 `_net`）。
        results = {}
        for strategy in STRATEGIES:
            batch = signals[strategy.key]
            if not batch:
                print(f"\n== {strategy.label} == 无信号")
                continue
            results[strategy.key] = run_signals(
                session,
                batch,
                strategy=strategy.label,
                params={"top": args.top, "mode": strategy.mode, "liquidity": strategy.liquidity},
                horizons=HORIZONS,
                entry_mode="next_open",
                cost_bps=0.0,
                matrix=matrix,
                benchmark=benchmark,
            )
    engine.dispose()

    print()
    print(f"研究区间 {start} ~ {end}；每日 Top {args.top}；持有 {list(HORIZONS)} 日")
    _print_summary(results, costs)
    _print_status(results)
    _print_exit_sensitivity(results)
    if args.detail:
        _print_years(results)
        _print_pairs(results)
    print()
    print("口径提示：信号层研究（SIGNAL_LEVEL_ONLY），不是组合净值；")
    print("         gross 不含费用；excess = gross − 基准毛收益（基准不扣费）；")
    print("         net 与 net_excess 用同一 cost 逐样本计算；cost 是往返综合假设，非真实费率；")
    print("         研究库缺退市股 → SURVIVORSHIP_BIAS；历史 ST 名称可能整段为当前名 → ST_STATUS_PARTIAL。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
