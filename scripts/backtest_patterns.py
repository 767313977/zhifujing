"""形态回测：调生产判定函数，算「同一天全市场」为基准的超额收益。

**这是一次性研究脚本，不属于站点本身。** 它要回答的是「这个形态值不值得做成
一个正式形态」，而不是先做一个好看的标签挂到页面上 —— 用户举的那只票
（强达电路 301628，2026-08）是**从结果倒推**出来的，照抄它的特征去选股，很容易
选出一堆「长得像、后来却没涨」的票。那是事后偏差，不是形态有效。

所以这里必须同时做三件事：

1. **只用判定日及之前的数据**判形态（没有未来函数）—— 判定函数拿到的 `bars` 里
   虽然带着后续数据，但判定只读 `<= t` 的部分（见 `_upto`）；
2. **同一只票 20 个交易日内的重复信号去重**，否则一波行情会被当成几个独立样本；
3. **和同一天全市场的平均收益比**（横截面基准）—— 否则「涨了 8%」可能只是
   那几天大盘在涨，跟形态没关系。

零 iFinD 配额：全部用库里已有的日线在本地算。

## 数据来源：两个库

| 数据源 | 窗口 | 用在 |
| --- | --- | --- |
| `stock_daily`（主库，iFinD 口径） | 260 个交易日 | 默认。与线上扫描喂进去的数据**完全一致** |
| `backend/data/history.db`（`--history`） | 由 `--bars` 决定，默认 1300 根 | 长历史回测（5 年 ≈ 1200 根） |

history.db 由 `scripts/backfill_history.py` 用腾讯补出来（零配额）。**为什么长历史不写主库**：
采集任务 `prune()` 只留最近 400 个交易日，写进去会被下一次采集删掉。

判定函数、入库门槛（`MIN_SCORE`）、去重与基准口径**三种模式完全相同**，变的只是
喂进去的历史长度 —— 也正因如此，「长历史那一版」的结论可以直接和其他版本对比。

## 覆盖范围与用法

判定一律**从 `app.services.patterns.PATTERNS` 取生产函数**，所以注册表里任何形态
都能回测，不必在这里再写一遍逻辑。历史上这里另有一套草案实现，与生产漂移之后
把三段式的 10 日超额高估了整整一倍 —— 而且那种错误**从结果里看不出来**，
所以草案已删除，只留「调生产函数」这一条口径。

文件名是 2026-09-25 从 `backtest_three_stage.py` 改过来的：旧名字只覆盖三段式等
4 个形态，现在要跑全部 46 个，名字跟着覆盖面走。

    python scripts/backtest_patterns.py --pattern v_bottom            # 单个（短窗口）
    python scripts/backtest_patterns.py --pattern all-new             # 26 个新形态
    python scripts/backtest_patterns.py --pattern all                 # 注册表里全部
    python scripts/backtest_patterns.py --pattern all-new --history   # 换成 5 年长历史
    python scripts/backtest_patterns.py --pattern all-new --history --stocks 800   # 限样本试跑

多目标时**一遍扫描**跑完所有形态（序列只切一次），输出「形态 × 持有期」汇总表；
单个形态则额外打印按月分布与命中明细，方便逐个人工核对。

## 内存与耗时（长历史下这两条要当回事）

5 年 × 全池 × 26 形态会产生**上百万条**信号。所以：
- 信号**逐条累加**进 `Tally`（分布用 `array('d')`，8 字节/条），不存 dict 对象
- 基准按「日期 → 当日全市场平均」预先压成一个数，而不是把全市场每天的收益都留着
（照旧写法峰值能到 GB 级。）全池 × 5 年 × 26 形态大约十几分钟到半小时。
"""

import argparse
import logging
import sys
import time
from array import array
from collections import Counter, defaultdict, deque
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.jobs.scan_patterns import _load_bars  # noqa: E402
from app.models import StockDaily  # noqa: E402
from app.services.patterns import (  # noqa: E402
    BF_MAX_GAP,
    BF_MIN_FLAT,
    BF_MIN_GAIN,
    BF_MIN_VOL,
    LS_LIMIT_PCT,
    LS_LOOKBACK,
    MIN_SCORE,
    ON_BREAK_VOL,
    PATTERNS,
    RS_MIN_BARS,
    RS_WEIGHTS,
    Bars,
    build_bars,
)

logger = logging.getLogger("backtest")

HORIZONS = (5, 10, 20, 60)
MIN_BARS = 80
# 同一只票这么多交易日内的重复信号只留第一个（见模块说明第 2 条）
DEDUP_DAYS = 20

HISTORY_DB = BACKEND / "data" / "history.db"
# 长历史的默认窗口：5 年约 1220 根，取 1300 留点余量
DEFAULT_HISTORY_BARS = 1300

# 2026-09-25 按用户清单补的 26 个形态。单独列出来是为了能一条命令跑完这一批
NEW_KEYS = (
    "ma_squeeze",
    "ascending_channel",
    "ma_golden_cross",
    "weekly_bull",
    "downtrend_breakout",
    "gap_up_breakout",
    "gap_fill",
    "box_breakout",
    "volume_dry_bottom",
    "vp_divergence",
    "shrink_limit_up",
    "volume_stall",
    "volume_pile",
    "v_bottom",
    "round_bottom",
    "triple_bottom",
    "rectangle_box",
    "rising_wedge",
    "falling_wedge",
    "diamond",
    "morning_star",
    "bullish_engulfing",
    "hammer",
    "inverted_hammer",
    "yang_wrap_yin",
    "three_white_soldiers",
)

_BY_KEY = {pattern.key: pattern for pattern in PATTERNS}


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


def _as_hit(signal) -> dict | None:
    """Signal → 命中明细字典；分数不到入库门槛的按未命中处理。

    回测必须与线上用同一个 `MIN_SCORE` —— 少了这一条，会把一堆线上根本不会显示的
    弱信号算进统计（结论会偏乐观）。这也是这里用生产常量、而不是另定一个的原因。
    """
    if signal is None or signal.score < MIN_SCORE:
        return None
    return {**signal.detail, "score": round(signal.score, 1)}


# ---------------------------------------------------------------- 廉价预筛
#
# 预筛只是**必要条件**，用来先把 (票, 日) 组合砍掉九成以上；命中与否一律由生产函数
# 说了算。没有预筛的形态就走全量扫 —— 2026-09-25 补的 26 个都属这一类：它们的
# 必要条件不容易用一两行写对，硬写反而容易把真信号筛掉（预筛写错的后果是
# 「回测说没有超额」，而不是报错）。


def prescreen_three_stage(bars: Bars, t: int) -> bool:
    """今天收盘站上了前 4 个交易日的最高价。

    生产判定要求 `close[t] > 整理段最高价`，而整理段最短 4 天 —— 所以这是必要条件。
    """
    if t < 30:
        return False
    return bool(bars.close[t] > float(bars.high[t - 4 : t].max()))


def prescreen_breakout_flat(bars: Bars, t: int) -> bool:
    """近 `BF_MAX_GAP` 个交易日里出现过「放量上涨」的日子（横盘段还没查）。"""
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


def prescreen_limit_surge(bars: Bars, t: int) -> bool:
    """最近 `LS_LOOKBACK` 天里出现过涨停。"""
    if t < 40:
        return False
    start = max(0, t + 1 - LS_LOOKBACK)
    return bool((bars.pct_chg[start : t + 1] >= LS_LIMIT_PCT).any())


def prescreen_oneil(bars: Bars, t: int) -> bool:
    """今天放量、且站在 50 日均线上方（两条都是生产判定的必要条件）。"""
    if t < 160:
        return False
    volume = bars.volume
    avg = float(volume[t - 50 : t].mean())
    if avg <= 0 or float(volume[t]) / avg < ON_BREAK_VOL:
        return False
    return float(bars.close[t]) >= float(bars.close[t - 50 : t].mean())


PRESCREENS = {
    "three_stage": prescreen_three_stage,
    "breakout_flat": prescreen_breakout_flat,
    "limit_surge_flat": prescreen_limit_surge,
    "oneil_breakout": prescreen_oneil,
}


# ---------------------------------------------------------------- 数据装载


def _load_history(engine, codes: list[str], bars: int) -> tuple[date, dict[str, list[dict]]]:
    """从 `history.db` 装载：每只票最近 `bars` 根日线。

    窗口**按数据自身的交易日**切，不用交易日历 —— 这个库只有 `stock_daily` 一张表，
    没有日历表（见 backfill_history 的说明）。线上那条 `_load_bars` 走日历是因为
    它要卡 `SCAN_BARS`，回测要的恰恰是「比线上更长」，所以在这里单独实现。
    """
    with Session(engine) as session:
        dates = list(
            session.scalars(
                select(StockDaily.trade_date)
                .group_by(StockDaily.trade_date)
                .order_by(StockDaily.trade_date.desc())
                .limit(bars)
            )
        )
        if not dates:
            raise SystemExit(f"{HISTORY_DB} 里没有数据，先跑 scripts/backfill_history.py")
        start = min(dates)
        latest = max(dates)
        rows = session.execute(
            select(
                StockDaily.code,
                StockDaily.name,
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
                StockDaily.trade_date >= start,
                StockDaily.trade_date <= latest,
                StockDaily.code.in_(codes),
            )
            .order_by(StockDaily.code, StockDaily.trade_date)
        ).all()

    grouped: dict[str, list[dict]] = defaultdict(list)
    for code, name, day, open_, high, low, close, volume, amount, pct in rows:
        # 涨跌幅为空的行在采集时就已经滤掉了，这里再挡一道：少了它 `build_bars`
        # 会把停牌日当成 0% 涨跌，前复权序列直接失真
        if close is None or pct is None:
            continue
        grouped[code].append(
            {
                "date": day,
                "name": name,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
                "pct_chg": pct,
            }
        )
    return latest, dict(grouped)


# ---------------------------------------------------------------- RS 表


def build_rs_table(bars_by_code: dict[str, Bars]) -> dict[object, dict[str, float]]:
    """逐日 RS 表：`{交易日: {代码: RS 评级}}`。

    **必须按天预计算，不能拿整段数据算一次** —— RS 是横截面排名，判定第 t 天时
    只能用「截至 t 的全市场表现」来排；用整段数据算出来的 RS 是未来函数，
    会让回测结果虚高，而且从结果里看不出来（这正是最危险的一类错误）。

    预计算而不是每个 t 重排一遍：回测要遍历全市场 × 全时段几百万个时点，
    逐点重排代价太高。这里是先算好每只票每天的加权涨幅，再逐日排序。
    """
    raw: dict[object, dict[str, float]] = defaultdict(dict)
    for code, bars in bars_by_code.items():
        close = bars.close
        for t in range(RS_MIN_BARS - 1, len(close)):
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
            code: round(rank / max(total - 1, 1) * 98) + 1 for rank, code in enumerate(order)
        }
    return table


# ---------------------------------------------------------------- 累加器


class Tally:
    """一个形态的累加统计。

    为什么不把每条信号存成一个 dict：5 年 × 全池 × 26 形态会产生**上百万条**信号，
    逐条存 dict 要几百 MB 到 GB；而报告只需要「分布 + 同日基准均值 + 按月计数 +
    几条样本」。所以：
    - 分布用 `array('d')` 存（8 字节/条，不是 Python 对象）
    - 基准只累加「当日全市场平均」的和，不保留日期
    """

    __slots__ = ("returns", "bench", "counts", "months", "samples")

    def __init__(self, horizons: int = len(HORIZONS)) -> None:
        self.returns = [array("d") for _ in range(horizons)]
        self.bench = [0.0] * horizons
        self.counts = [0] * horizons
        self.months: Counter[str] = Counter()
        self.samples: deque[dict] = deque(maxlen=40)

    def add(
        self,
        outcomes: list[float],
        bench_means: list[float],
        month: str,
        sample: dict | None = None,
    ) -> None:
        for index, value in enumerate(outcomes):
            self.returns[index].append(value)
            self.bench[index] += bench_means[index]
            self.counts[index] += 1
        self.months[month] += 1
        if sample is not None:
            self.samples.append(sample)

    # -- 报告用 ----------------------------------------------------------

    @property
    def size(self) -> int:
        return self.counts[0] if self.counts else 0

    def values(self, index: int) -> np.ndarray:
        """第 index 个持有期的收益数组（只读视图，不复制）。"""
        return np.frombuffer(self.returns[index], dtype=np.float64)

    def bench_mean(self, index: int) -> float:
        count = self.counts[index]
        return self.bench[index] / count if count else 0.0

    def stats(self, index: int) -> tuple[float, float, float, float, float]:
        values = self.values(index)
        if not values.size:
            return (0.0, 0.0, 0.0, 0.0, 0.0)
        return (
            float(values.mean()),
            float(np.median(values)),
            float(np.mean(values > 0)),
            float(values.max()),
            float(values.min()),
        )


def _resolve_targets(name: str) -> dict[str, dict]:
    if name == "all-new":
        keys = list(NEW_KEYS)
    elif name == "all":
        # 致富那两个（今天可买 / 明天盯）**不进全量回测**：它们的候选池是悟道同口径的
        # 强势小池（见 scan_patterns._wudao_candidate_codes），拿全市场跑出来的是
        # 另一个分布，结论没有意义
        keys = [p.key for p in PATTERNS if p.group != "致富"]
    else:
        keys = [name]
    targets = {}
    for key in keys:
        if key not in _BY_KEY:
            raise SystemExit(f"注册表里没有形态 {key!r}；可用：{', '.join(sorted(_BY_KEY))}")
        targets[key] = {"screen": PRESCREENS.get(key)}
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description="形态回测（调生产函数，与线上判定逐字一致）")
    parser.add_argument(
        "--pattern",
        default="three_stage",
        help="形态 key / all-new（26 个新形态）/ all（除致富外全部）",
    )
    parser.add_argument("--samples", type=int, default=15, help="单个形态时打印多少条命中明细")
    parser.add_argument("--stocks", type=int, default=0, help="只用前 N 只票（0 = 全部）")
    parser.add_argument("--history", action="store_true", help="用长历史库 history.db")
    parser.add_argument(
        "--bars", type=int, default=DEFAULT_HISTORY_BARS, help=f"长历史窗口（默认 {DEFAULT_HISTORY_BARS} 根）"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from app.jobs.collect_universe import load_codes

    codes = load_codes()
    if args.stocks:
        codes = codes[: args.stocks]
    if not codes:
        raise SystemExit("股票池为空，先建池")

    if args.history:
        if not HISTORY_DB.is_file():
            raise SystemExit(f"没有 {HISTORY_DB}，先跑 scripts/backfill_history.py")
        engine = create_engine(f"sqlite:///{HISTORY_DB.as_posix()}")
        latest, grouped = _load_history(engine, codes, args.bars)
        logger.info("数据源 history.db（窗口 %d 根），截至 %s", args.bars, latest)
    else:
        with session_scope() as session:
            # 截止日必须取 **stock_daily 的最大日期**，不能取交易日历的最大日期 ——
            # 日历表会预置到年底，拿它当截止日会让 `_load_bars` 的 260 日窗口整体后移，
            # 把最早那几个月切掉（实测少了三个月，信号全挤在 4-6 月，看着像
            # 「这个形态只在特定行情下成立」，其实是窗口的问题）
            latest = session.scalar(select(func.max(StockDaily.trade_date)))
        if latest is None:
            raise SystemExit("stock_daily 是空的，先跑 collect_kline")
        grouped = _load_bars(latest, codes)

    bars_by_code = {
        code: build_bars(records) for code, records in grouped.items() if len(records) >= MIN_BARS
    }
    if not bars_by_code:
        raise SystemExit("没有可用的日线序列")
    lengths = sorted(len(bars) for bars in bars_by_code.values())
    logger.info(
        "载入 %d 只票，每票 K 线 中位 %d 根（最长 %d），截至 %s",
        len(bars_by_code),
        lengths[len(lengths) // 2],
        lengths[-1],
        latest,
    )

    targets = _resolve_targets(args.pattern)
    logger.info("目标形态 %d 个：%s", len(targets), ", ".join(targets))

    rs_table = build_rs_table(bars_by_code)
    logger.info("RS 表：%d 个交易日", len(rs_table))

    # 基准：每个交易日 → 全市场在该日之后 N 日的平均收益。
    # 必须按**同一天**比，否则「形态命中组涨了 6%」可能只是那段时间大盘在涨。
    #
    # 这里只累加 sum 与 count（而不是把每天的收益都存成一个列表）：5 年 × 全池会攒下
    # 上千万个浮点数，存列表峰值能吃掉好几个 GB，而报告只需要那个均值。
    bench_sum: dict[int, dict[object, float]] = {h: defaultdict(float) for h in HORIZONS}
    bench_count: dict[int, dict[object, int]] = {h: defaultdict(int) for h in HORIZONS}
    for bars in bars_by_code.values():
        n = len(bars)
        for i in range(n - max(HORIZONS)):
            entry = float(bars.close[i])
            if entry <= 0:
                continue
            day = bars.dates[i]
            for h in HORIZONS:
                bench_sum[h][day] += float(bars.close[i + h]) / entry - 1
                bench_count[h][day] += 1
    bench: dict[int, dict[object, float]] = {
        h: {day: bench_sum[h][day] / count for day, count in bench_count[h].items() if count}
        for h in HORIZONS
    }
    logger.info("基准表：%d 个交易日", len(bench[HORIZONS[0]]))

    tallies = {key: Tally() for key in targets}
    last_hit: dict[tuple[str, str], int] = {}
    started = time.monotonic()

    for done, (code, bars) in enumerate(bars_by_code.items(), start=1):
        n = len(bars)
        for t in range(MIN_BARS, n - max(HORIZONS)):
            sample: Bars | None = None
            for key, target in targets.items():
                screen = target["screen"]
                if screen is not None and not screen(bars, t):
                    continue
                if sample is None:
                    rs = rs_table.get(bars.dates[t], {}).get(code, 0.0)
                    sample = _upto(bars, t + 1, rs)
                hit = _as_hit(_BY_KEY[key].detect(sample))
                if hit is None:
                    continue
                # 同一只票 DEDUP_DAYS 个交易日内只留第一个信号：一波行情里连着几天
                # 都满足条件，会被当成几个「独立样本」，把统计显著性算得虚高
                if t - last_hit.get((key, code), -10**6) < DEDUP_DAYS:
                    continue
                entry = float(bars.close[t])
                if entry <= 0:
                    continue
                last_hit[(key, code)] = t
                day = bars.dates[t]
                tallies[key].add(
                    [float(bars.close[t + h]) / entry - 1 for h in HORIZONS],
                    [bench[h].get(day, 0.0) for h in HORIZONS],
                    str(day)[:7],
                    {
                        "code": code,
                        "name": grouped[code][-1].get("name") or "",
                        "date": day,
                        "hit": hit,
                        "outcomes": {
                            h: float(bars.close[t + h]) / entry - 1 for h in HORIZONS
                        },
                    },
                )
        if done % 1000 == 0:
            logger.info(
                "  扫过 %d/%d 只，命中 %d 条，用时 %.0fs",
                done,
                len(bars_by_code),
                sum(tally.size for tally in tallies.values()),
                time.monotonic() - started,
            )

    total = sum(tally.size for tally in tallies.values())
    logger.info("命中 %d 条信号，用时 %.0fs", total, time.monotonic() - started)
    if total == 0:
        logger.warning("一个都没命中 —— 条件太严或数据太短")
        return 1

    if len(targets) == 1:
        key = next(iter(targets))
        _report_single(key, tallies[key], args.samples)
    else:
        _report_multi(tallies)
    return 0


# ---------------------------------------------------------------- 报告


def _report_single(key: str, tally: Tally, samples: int) -> None:
    meta = _BY_KEY[key]
    if not tally.size:
        print(f"\n形态：{meta.name}（{key}）—— 没有命中")
        return
    print()
    print("=" * 92)
    print(f"形态：{meta.name}（{key}，组={meta.group}）  信号 {tally.size} 条")
    print("-" * 92)
    print(
        f"{'持有':>6} {'样本':>6} {'均值':>9} {'中位':>9} {'胜率':>7} "
        f"{'基准均值':>10} {'超额':>9} {'最好':>9} {'最差':>9}"
    )
    print("-" * 92)
    for index, horizon in enumerate(HORIZONS):
        mean, median, win, best, worst = tally.stats(index)
        base = tally.bench_mean(index)
        print(
            f"{horizon:>4}日 {tally.counts[index]:>6} {mean * 100:>8.2f}% {median * 100:>8.2f}% "
            f"{win * 100:>6.1f}% {base * 100:>9.2f}% {(mean - base) * 100:>8.2f}% "
            f"{best * 100:>8.1f}% {worst * 100:>8.1f}%"
        )
    print("=" * 92)

    print()
    print("--- 信号按月分布（全挤在某一两个月 = 可能只在特定行情下成立）---")
    print("  " + "   ".join(f"{m}: {c}" for m, c in sorted(tally.months.items())))

    if samples > 0:
        print()
        print(f"--- 命中明细（按扫描顺序最后 {samples} 条，便于逐个人工核对）---")
        # 注意别写成 rows[-samples:]：samples=0 时那是 [-0:] 等于整段，会把全部明细打出来
        for item in sorted(tally.samples, key=lambda entry: entry["date"])[-samples:]:
            out = item["outcomes"]
            print(
                f"  {item['date']} {item['code']} {str(item.get('name') or ''):<6} "
                f"分数{item['hit'].get('score', 0):>5.1f}"
                f"  → 5日{out[5] * 100:>+6.1f}% 10日{out[10] * 100:>+6.1f}% "
                f"20日{out[20] * 100:>+6.1f}%"
            )


def _report_multi(tallies: dict[str, Tally]) -> None:
    rows = []
    for key, tally in tallies.items():
        meta = _BY_KEY[key]
        if not tally.size:
            rows.append({"key": key, "meta": meta, "tally": tally, "excess": None})
            continue
        excess = {
            index: tally.stats(index)[0] - tally.bench_mean(index)
            for index in range(len(HORIZONS))
        }
        rows.append({"key": key, "meta": meta, "tally": tally, "excess": excess})

    # 按 10 日超额排序：做这张表的全部意义就是「一眼看出哪些形态值得留」
    rows.sort(key=lambda item: (item["excess"][1] if item["excess"] else -99), reverse=True)

    print()
    print("=" * 110)
    print(
        f"{'分组':<12}{'形态':<18}{'信号':>7}{'5日超额':>10}{'10日超额':>10}"
        f"{'20日超额':>10}{'60日超额':>10}{'10日胜率':>9}{'10日中位':>10}"
    )
    print("-" * 110)
    for item in rows:
        meta, tally = item["meta"], item["tally"]
        if item["excess"] is None:
            print(f"{meta.group:<12}{meta.name:<18}{0:>7}{'—':>10}{'—':>10}{'—':>10}{'—':>10}{'—':>9}{'—':>10}")
            continue
        excess = item["excess"]
        median10 = tally.stats(1)[1]
        win10 = tally.stats(1)[2]
        print(
            f"{meta.group:<12}{meta.name:<18}{tally.size:>7}"
            f"{excess[0] * 100:>9.2f}%{excess[1] * 100:>9.2f}%{excess[2] * 100:>9.2f}%"
            f"{excess[3] * 100:>9.2f}%{win10 * 100:>8.1f}%{median10 * 100:>9.2f}%"
        )
    print("=" * 110)
    print("（超额 = 命中组平均收益 − 同一天全市场平均收益，单位 %，未年化）")

    print()
    print("--- 各形态信号按月分布（看是不是只在某一两个月成立）---")
    for key, tally in tallies.items():
        if not tally.size:
            continue
        months = "   ".join(f"{m}:{c}" for m, c in sorted(tally.months.items()))
        print(f"  {_BY_KEY[key].name:<18}{months}")


if __name__ == "__main__":
    sys.exit(main())
