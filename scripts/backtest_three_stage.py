"""候选形态的回测：三段式（缓涨→急涨→缩量整理→突破）与突破后缩量横盘。

**这是一次性研究脚本，不属于站点本身。** 它要回答的是「这个模式值不值得做成
一个正式形态」，而不是先做一个好看的标签挂到页面上 —— 用户举的那只票
（强达电路 301628，2026-08）是**从结果倒推**出来的，照抄它的特征去选股，很容易
选出一堆「长得像、后来却没涨」的票。那是事后偏差，不是形态有效。

所以这里必须同时做三件事：

1. **只用判定日及之前的数据**判形态（没有未来函数）—— 判定函数拿到的
   `bars` 里虽然有多余的后续数据，但 `find_*` 只读 `<= t` 的部分；
2. **同一只票 20 个交易日内的重复信号去重**，否则一波行情会被当成几个独立样本；
3. **和同一天全市场的平均收益比**（横截面基准）—— 否则「涨了 8%」可能只是
   那几天大盘在涨，跟形态没关系。

零 iFinD 配额：全部用库里已有的日线在本地算。

用法::

    python scripts/backtest_three_stage.py                              # 三段式
    python scripts/backtest_three_stage.py --samples 30
    python scripts/backtest_three_stage.py --pattern breakout_flat      # 突破后横盘
    # 阈值都能从命令行覆盖，方便一次试几组看分布
    python scripts/backtest_three_stage.py --surge-vol 1.8 --flat-range 0.13
"""

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.jobs.scan_patterns import _load_bars  # noqa: E402
from app.models import StockDaily, TradeCalendar  # noqa: E402
# 直接导入生产的形态函数（私有名也照导）：回测必须与线上判定**逐字一致**，
# 另写一份判定逻辑迟早会跟生产偏离，那时候回测结论就是假的
from app.services.patterns import (  # noqa: E402
    BF_MAX_GAP,
    BF_MIN_FLAT,
    BF_MIN_GAIN,
    BF_MIN_VOL,
    LS_LIMIT_PCT,
    LS_LOOKBACK,
    ON_BREAK_VOL,
    RS_MIN_BARS,
    RS_WEIGHTS,
    Bars,
    _breakout_flat,
    _limit_surge_flat,
    _oneil_breakout,
    _three_stage,
    build_bars,
)

logger = logging.getLogger("backtest")

# ---------------------------------------------------------------- 回测口径

# ⚠️ 2026-09-19 起，三段式与涨停爆量横盘都**直接调 `app.services.patterns`
# 里的生产函数**，阈值以那边为准（`THREE_*` / `LS_*`），本文件不再各留一套。
# 原先这里有一组草案阈值（`SLOW_*` / `SURGE_*` / `FLAT_*` / `BREAK_*`），
# 与生产代码各写各的 —— 漂移之后回测出来的超额就说明不了线上形态的效果
# （实测把三段式的 10 日超额高估了一倍），已删除。
HORIZONS = (5, 10, 20, 60)
MIN_BARS = 80

# ---------------------------------------------------------------- 突破后缩量横盘
# 这个候选已经**补进生产**（`patterns._breakout_flat`），阈值以那边为准 ——
# 本文件只留一个廉价预筛 + 转发调用，免得又走上「脚本与生产各写一套」的老路。
# 形态说明与回测数字见 `patterns.py` 顶部 `BF_*` 那段。


def prescreen_breakout_flat(bars: Bars, t: int) -> bool:
    """预筛：近 `BF_MAX_GAP` 个交易日里出现过「放量上涨」的日子。

    这只是必要条件（横盘那一段还没查），目的是先把 (票, 日) 组合砍掉九成 ——
    实测不预筛的话，这个形态要慢 6 倍以上。
    """
    if t < 70:
        return False
    close, volume = bars.close, bars.volume
    for b_idx in range(max(t - BF_MAX_GAP, 65), t - BF_MIN_FLAT + 1):
        prev = float(close[b_idx - 1])
        if prev <= 0:
            continue
        if float(close[b_idx]) / prev - 1 < BF_MIN_GAIN:
            continue
        prev_vol = float(volume[b_idx - 5 : b_idx].mean())
        if prev_vol > 0 and float(volume[b_idx]) / prev_vol >= BF_MIN_VOL:
            return True
    return False


def find_breakout_flat(
    bars: Bars, t: int, counter: dict[int, int] | None = None, rs: float = 0.0
) -> dict | None:
    """直接调生产代码的 `_breakout_flat`（判定是黑盒，没有分步漏斗）。"""
    signal = _breakout_flat(_upto(bars, t + 1, rs))
    if signal is None:
        return None
    return {**signal.detail, "score": round(signal.score, 1)}


# ---------------------------------------------------------------- 涨停爆量横盘


def _upto(bars: Bars, end: int, rs: float = 0.0) -> Bars:
    """截到第 `end` 根（不含）为止。

    判定函数只认「序列的最后一天就是今天」，所以回测到哪天就得把序列切到哪天 ——
    直接传整段等于把后面的行情喂进去，那就是未来函数。

    `rs` 必须由调用方按**那一天**的横截面排名传进来（见 `build_rs_table`），
    不能从 `bars` 上继承 —— 用整段数据算出来的 RS 同样是未来函数。
    """
    return Bars(
        dates=bars.dates[:end],
        open=bars.open[:end],
        high=bars.high[:end],
        low=bars.low[:end],
        close=bars.close[:end],
        volume=bars.volume[:end],
        amount=bars.amount[:end],
        pct_chg=bars.pct_chg[:end],
        rs=rs,
    )


def prescreen_limit_surge(bars: Bars, t: int) -> bool:
    """廉价预筛：最近 `LS_LOOKBACK` 天里出现过涨停。

    没有它，这个形态要对全市场每个 (票, 日) 跑一遍完整判定 —— 实测慢 6 倍，
    而其中绝大多数连涨停都没有，根本不可能命中。
    """
    if t < 40:
        return False
    start = max(0, t + 1 - LS_LOOKBACK)
    return bool((bars.pct_chg[start : t + 1] >= LS_LIMIT_PCT).any())


def find_limit_surge(
    bars: Bars, t: int, counter: dict[int, int] | None = None, rs: float = 0.0
) -> dict | None:
    """直接调生产代码的 `_limit_surge_flat`（判定是黑盒，没有分步漏斗）。"""
    signal = _limit_surge_flat(_upto(bars, t + 1, rs))
    if signal is None:
        return None
    return {**signal.detail, "score": round(signal.score, 1)}


def prescreen(bars: Bars, t: int) -> bool:
    """廉价预筛：今天收盘站上了前 4 个交易日的最高价。

    生产判定要求 `close[t] > 整理段最高价`，而整理段最短 4 天 ——
    所以「越过前 4 日最高价」是它的**必要条件**，不满足就一定不命中。
    只这一条就能砍掉九成以上的 (票, 日) 组合。
    """
    if t < 30:
        return False
    return bool(bars.close[t] > float(bars.high[t - 4 : t].max()))


def find_signal(
    bars: Bars, t: int, counter: dict[int, int] | None = None, rs: float = 0.0
) -> dict | None:
    """直接调生产代码的 `_three_stage`，保证回测与线上判定逐字一致。

    这个脚本原先自带一套草案实现（阈值各写各的），一旦两边漂移，回测出来的
    超额就说明不了线上形态的效果 —— 实测重跑后信号数从 36 掉到 15、
    10 日超额从 +4.9% 掉到 +0.4%。草案那套已经删掉，只留这一条口径。
    """
    signal = _three_stage(_upto(bars, t + 1))
    if signal is None:
        return None
    return {**signal.detail, "score": round(signal.score, 1)}


# ---------------------------------------------------------------- 欧奈尔突破


def build_rs_table(bars_by_code: dict[str, Bars]) -> dict[object, dict[str, float]]:
    """逐日 RS 表：`{交易日: {代码: RS 评级}}`，给回测用。

    **必须按天预计算，不能拿整段数据算一次** —— RS 是横截面排名，判定第 t 天时
    只能用「截至 t 的全市场表现」来排；用整段数据算出来的 RS 是未来函数，
    会让回测结果虚高，而且**从结果里看不出来**（这正是最危险的一类错误）。

    预计算而不是每个 t 重排一遍：回测要遍历全市场 × 全时段约 6 万个时点，
    逐点重排的代价太高。这里是先算好每只票每天的加权涨幅，再逐日排序。
    """
    raw: dict[object, dict[str, float]] = defaultdict(dict)
    for code, bars in bars_by_code.items():
        close = bars.close
        size = len(close)
        for t in range(RS_MIN_BARS - 1, size):
            latest = float(close[t])
            if latest <= 0:
                continue
            piece = 0.0
            weight_sum = 0.0
            for window, weight in RS_WEIGHTS:
                if t < window:
                    continue  # 这一年窗口还拿不到，跳过并归一化（与线上同一套逻辑）
                base = float(close[t - window])
                if base <= 0:
                    continue
                piece += weight * (latest / base - 1)
                weight_sum += weight
            if weight_sum > 0:
                raw[bars.dates[t]][code] = piece / weight_sum

    table: dict[object, dict[str, float]] = {}
    for day, values in raw.items():
        order = sorted(values, key=lambda item: values[item])
        total = len(order)
        table[day] = {
            code: round(rank / max(total - 1, 1) * 98) + 1
            for rank, code in enumerate(order)
        }
    return table


def prescreen_oneil(bars: Bars, t: int) -> bool:
    """廉价预筛：今天放量、且站在 50 日均线上方。

    这两条都是生产判定的必要条件（放量门槛、MA50），先过一遍能砍掉九成以上的时点。
    """
    if t < 160:
        return False
    volume = bars.volume
    avg = float(volume[t - 50 : t].mean())
    if avg <= 0 or float(volume[t]) / avg < ON_BREAK_VOL:
        return False
    return float(bars.close[t]) >= float(bars.close[t - 50 : t].mean())


def find_oneil(
    bars: Bars, t: int, counter: dict[int, int] | None = None, rs: float = 0.0
) -> dict | None:
    """直接调生产代码的 `_oneil_breakout`。

    `rs` 必须是**那一天**的横截面评级 —— 传 0（默认）的话生产函数会直接跳过，
    回测会得到「一个信号都没有」，别把它当成「这形态不行」。
    """
    signal = _oneil_breakout(_upto(bars, t + 1, rs))
    if signal is None:
        return None
    return {**signal.detail, "score": round(signal.score, 1), "rs": rs}


def _stats(values: list[float]) -> tuple[float, float, float, float, float]:
    array = np.asarray(values, dtype=float)
    return (
        float(array.mean()),
        float(np.median(array)),
        float(np.mean(array > 0)),
        float(array.max()),
        float(array.min()),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="全市场形态回测（调生产函数，与线上判定逐字一致）"
    )
    parser.add_argument("--samples", type=int, default=15, help="打印多少条命中明细")
    parser.add_argument(
        "--pattern",
        choices=("three_stage", "breakout_flat", "limit_surge_flat", "oneil_breakout"),
        default="three_stage",
        help="跑哪个形态：三段式 / 突破后横盘 / 涨停爆量横盘 / 欧奈尔突破",
    )
    args = parser.parse_args()

    # 四个形态都直接调生产函数（阈值都在 app/services/patterns.py），
    # 所以不提供命令行覆盖 —— 要试参数就改那边，改完在这里回测即验证
    if args.pattern == "breakout_flat":
        screen, detect = prescreen_breakout_flat, find_breakout_flat
    elif args.pattern == "limit_surge_flat":
        screen, detect = prescreen_limit_surge, find_limit_surge
    elif args.pattern == "oneil_breakout":
        screen, detect = prescreen_oneil, find_oneil
    else:
        screen, detect = prescreen, find_signal
    # 判定都走生产函数，是黑盒，给不出分步漏斗
    stage_names, stage_order = {}, []
    # 欧奈尔要 RS，而 RS 需要 200 根 K 线预热；库里只有 250 个交易日，
    # 再留 60 日前瞻就一天都不剩（实测刚跑时 rs_table 只覆盖 52 天、命中 0 个）——
    # 所以这个形态的回测只用短前瞻
    horizons = (5, 10, 20) if args.pattern == "oneil_breakout" else HORIZONS

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = get_settings()
    with session_scope() as session:
        # 截止日必须取 **stock_daily 的最大日期**，不能取交易日历的最大日期 ——
        # 日历表会预置到年底，拿它当截止日会让 `_load_bars` 的 260 日窗口整体后移，
        # 把最早那几个月的数据切掉（实测少了三个月，信号全挤在 4-6 月，
        # 看着像「这个形态只在特定行情下成立」，其实是窗口的问题）
        latest = session.scalar(select(func.max(StockDaily.trade_date)))
    if latest is None:
        raise SystemExit("stock_daily 是空的，先跑 collect_kline")

    grouped = _load_bars(latest, settings)
    bars_by_code = {
        code: build_bars(records)
        for code, records in grouped.items()
        if len(records) >= MIN_BARS
    }
    logger.info("载入 %d 只票的日线（截至 %s）", len(bars_by_code), latest)

    # 逐日 RS 表。只有依赖 RS 的形态（欧奈尔）用得上，但建它很便宜（一次排序），
    # 而**必须按天算** —— 用整段数据算出来的 RS 是未来函数（见 build_rs_table）
    rs_table = build_rs_table(bars_by_code)
    logger.info("RS 表：%d 个交易日", len(rs_table))

    # 基准：每个交易日 → 全市场在该日之后 N 日的平均收益。
    # 必须按**同一天**比，否则「形态命中组涨了 6%」可能只是那段时间大盘在涨。
    bench: dict[int, dict[object, list[float]]] = {h: defaultdict(list) for h in horizons}
    for bars in bars_by_code.values():
        n = len(bars)
        for i in range(n - max(horizons)):
            entry = float(bars.close[i])
            if entry <= 0:
                continue
            for h in horizons:
                bench[h][bars.dates[i]].append(float(bars.close[i + h]) / entry - 1)

    signals: list[dict] = []
    scanned = 0
    funnel: dict[int, int] = defaultdict(int)
    last_hit: dict[str, int] = {}
    for code, bars in bars_by_code.items():
        n = len(bars)
        for t in range(MIN_BARS, n - max(horizons)):
            if not screen(bars, t):
                continue
            scanned += 1
            # RS 传**那一天**的；不在表里就是 0，依赖 RS 的形态会自行跳过
            rs = rs_table.get(bars.dates[t], {}).get(code, 0.0)
            hit = detect(bars, t, funnel, rs)
            if hit is None:
                continue
            entry = float(bars.close[t])
            if entry <= 0:
                continue
            # 同一只票 20 个交易日内只留第一个信号：一波行情里连着几天都满足条件，
            # 会被当成几个「独立样本」，把统计显著性算得虚高
            if t - last_hit.get(code, -10**6) < 20:
                continue
            last_hit[code] = t
            signals.append(
                {
                    "code": code,
                    "name": grouped[code][-1].get("name") or "",
                    "date": bars.dates[t],
                    "hit": hit,
                    "outcomes": {
                        h: float(bars.close[t + h]) / entry - 1 for h in horizons
                    },
                }
            )

    logger.info("预筛通过 %d 个 (票, 日) 组合，命中 %d 个信号", scanned, len(signals))

    # 没有分步漏斗的形态（涨停爆量横盘直接复用生产函数）整段跳过
    if stage_order:
        print()
        print("--- 逐条条件的通过情况：看是哪一环在卡（每列互斥，取最深层）---")
        for level in stage_order:
            print(f"  [{level}] {funnel.get(level, 0):>6} 个   {stage_names[level]}")
        print()

    if not signals:
        logger.warning("一个都没命中 —— 条件太严，先放宽 SURGE_MIN_GAIN 或 FLAT_MAX_VOL")
        return 1

    print()
    print("=" * 92)
    print(
        f"{'持有':>6} {'样本':>6} {'均值':>9} {'中位':>9} {'胜率':>7} "
        f"{'基准均值':>10} {'超额':>9} {'最好':>9} {'最差':>9}"
    )
    print("-" * 92)
    for h in horizons:
        values = [s["outcomes"][h] for s in signals]
        mean, median, win, best, worst = _stats(values)
        base_values = [
            float(np.mean(bench[h][s["date"]]))
            for s in signals
            if bench[h].get(s["date"])
        ]
        base = float(np.mean(base_values)) if base_values else 0.0
        print(
            f"{h:>4}日 {len(values):>6} {mean * 100:>8.2f}% {median * 100:>8.2f}% "
            f"{win * 100:>6.1f}% {base * 100:>9.2f}% {(mean - base) * 100:>8.2f}% "
            f"{best * 100:>8.1f}% {worst * 100:>8.1f}%"
        )
    print("=" * 92)

    # 分档：两个形态的关键变量不同 —— 三段式看「缩到多少」，横盘看「回撤多少」
    print()
    if args.pattern == "breakout_flat":
        print("--- 按「横盘期回撤」分档（10 日收益）---")
        buckets = ((0.0, 0.015), (0.015, 0.03), (0.03, 0.05))
        bucket_key = "giveback"
    elif args.pattern == "limit_surge_flat":
        print("--- 按「横盘段振幅」分档（10 日收益）---")
        buckets = ((0.0, 0.035), (0.035, 0.06), (0.06, 0.12))
        bucket_key = "flat_range"
    elif args.pattern == "oneil_breakout":
        print("--- 按「突破日量比」分档（10 日收益）---")
        buckets = ((1.4, 2.0), (2.0, 3.0), (3.0, 20.0))
        bucket_key = "vol_ratio"
    else:
        print("--- 按「整理段缩量到急涨段均量的比例」分档（10 日收益）---")
        buckets = ((0.0, 0.3), (0.3, 0.45), (0.45, 0.6))
        bucket_key = "shrink"
    for low, high in buckets:
        group = [s for s in signals if low <= s["hit"][bucket_key] < high]
        if not group:
            continue
        values = [s["outcomes"][10] for s in group]
        mean, median, win, _, _ = _stats(values)
        print(
            f"  {low:.2%}~{high:.2%}: {len(group):>5} 个  均值 {mean * 100:>6.2f}%  "
            f"中位 {median * 100:>6.2f}%  胜率 {win * 100:>5.1f}%"
        )

    # 信号在时间上均匀吗？如果全挤在某一两个月，说明它可能只在特定市场环境下
    # 成立 —— 那这个「平均超额」就不是长期能力，而是那段时间的行情给的
    by_month: dict[str, int] = defaultdict(int)
    for s in signals:
        by_month[str(s["date"])[:7]] += 1
    print()
    print("--- 信号按月分布 ---")
    print("  " + "   ".join(f"{month}: {count}" for month, count in sorted(by_month.items())))

    print()
    print(f"--- 命中明细（最近 {args.samples} 条，便于逐个人工核对）---")
    for s in sorted(signals, key=lambda item: item["date"])[-args.samples :]:
        hit = s["hit"]
        out = s["outcomes"]
        head = f"  {s['date']} {s['code']} {str(s.get('name') or ''):<6} "
        tail = (
            f"  → 5日{out[5] * 100:>+6.1f}% 10日{out[10] * 100:>+6.1f}% "
            f"20日{out[20] * 100:>+6.1f}%"
        )
        if args.pattern == "breakout_flat":
            body = (
                f"突破{hit['break_gain'] * 100:>5.1f}%(量{hit['vol_ratio']:.1f}x) "
                f"横盘{hit['flat_days']:>2}日(幅{hit['flat_range'] * 100:>4.1f}%) "
                f"回撤{hit['giveback'] * 100:>4.1f}% 缩到{hit['shrink'] * 100:>3.0f}% "
                f"位置{hit['position'] * 100:>3.0f}%"
            )
        elif args.pattern == "limit_surge_flat":
            body = (
                f"涨停量{hit['limit_vol']:>4.1f}x 上冲{hit['surge_gain'] * 100:>5.1f}% "
                f"横盘{hit['flat_days']:>2}日(幅{hit['flat_range'] * 100:>4.1f}%) "
                f"缩到{hit['shrink'] * 100:>3.0f}% 守住{hit['keep'] * 100:>3.0f}%"
            )
        elif args.pattern == "oneil_breakout":
            body = (
                f"RS{hit['rs']:>3.0f} 量比{hit['vol_ratio']:>4.2f} "
                f"基底{hit['flat_days']:>3}日(回撤{hit['flat_range'] * 100:>4.1f}%) "
                f"距一年新高{hit['from_high'] * 100:>5.1f}% "
                f"突破幅{hit['excess'] * 100:>4.1f}%"
            )
        else:
            body = (
                f"缓涨{hit['slow_gain'] * 100:>5.1f}%(阳{hit['slow_bull'] * 100:.0f}%) "
                f"急涨{hit['surge_gain'] * 100:>5.1f}%/{hit['surge_days']}日(量{hit['surge_vol']:.1f}x) "
                f"整理{hit['flat_days']:>3}日(幅{hit['flat_range'] * 100:>4.1f}%) "
                f"缩到{hit['shrink'] * 100:>4.0f}% 突破量{hit['break_vol']:.1f}x"
            )
        print(head + body + tail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
