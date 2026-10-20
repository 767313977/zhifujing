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

## ⚠️ 已知偏差（样本与基准都**偏乐观**，别当成生产效果）

有两处**用现有数据修不掉**的前视 / 生存偏差 —— 真正的修法需要「历史成分股 + 逐日 ST
状态」的快照，现在没有。**不要**为了修而伪造历史池：

1. **股票池取的是「现在」的池子**（`load_codes()`，见 `main()`；`--board wudao` 只是
   在上面再按板块过滤）。**退市 / 出池的票整段缺席** —— 样本与基准里都没有它们。
2. **ST 拿「最后一根 K 线的名字」判、且整段剔除**（见 `main()` 里 `st_dropped` 那段）。
   于是**后天才变 ST 的票，连它变 ST 之前那段下跌也一起被剔掉**。

两处方向一致：都**少了「后来死掉 / 被 ST」的那批票**，所以样本与基准**双双偏乐观**。
此外回测**不含生产那条候选池过滤**（生产「明天盯 / 明天预案」只在「当日出过线」的候选里选，
见 `scan_patterns._candidate_codes`；回测只卡板块），口径更宽 → 命中更多。

结论：这些数字只能用来**在同一口径下比形态之间的相对强弱**，不能当作「上生产能赚这么多」。

## 持有期口径：按交易日（2026-10-10 从「按行」改为「按交易日」）

`stock_daily` 里没有停牌日的行，于是原来的 `close[t + h]` 取的是**第 h 行**，
不是「第 h 个交易日」—— 停牌空洞会把实际持有期拉长（例：000010 2026-04-23，
按行 5 日 −25.68% vs 按日历 −21.89%；9-28 窗口内 5092 只里 406 只 ≥100 根的池内票
存在日期空洞）。线上 `pattern_track._curves_from` 是按 `TradeCalendar` 定位、**不受**
空洞影响，所以回测必须与它同口径：用数据自身的日期并集建一条交易日轴，按全局位置
判「第 h 个交易日」。这是**口径变更** —— 文档里 2026-10-10 之前的回测数字仍是旧口径
（见 `docs/plans/2026-09-17-fupan-site-design.md` 末尾那一节）。

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
4 个形态，现在要跑注册表里全部（2026-09-27 起是 44 个），名字跟着覆盖面走。

    python scripts/backtest_patterns.py --pattern v_bottom            # 单个（短窗口）
    python scripts/backtest_patterns.py --pattern all-new             # 18 个新形态
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
from collections.abc import Callable
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
    PATTERNS,
    Bars,
    build_bars,
    is_st,
    usable_bar,
)

logger = logging.getLogger("backtest")

HORIZONS = (5, 10, 20, 60)
MIN_BARS = 80
# 同一只票这么多交易日内的重复信号只留第一个（见模块说明第 2 条）
DEDUP_DAYS = 20

HISTORY_DB = BACKEND / "data" / "history.db"
# 长历史的默认窗口：5 年约 1220 根，取 1300 留点余量
DEFAULT_HISTORY_BARS = 1300

# 2026-09-25 按用户清单补的那一批（原 26 个，量堆已于 2026-09-27 删除）。
# 单独列出来是为了能一条命令跑完这一批
NEW_KEYS = (
    "ma_squeeze",
    "ma_golden_cross",
    # 「上升通道 / 周线多头」2026-10-15 撤下（用户要求），不在这批里了
    "downtrend_breakout",
    "gap_up_breakout",
    "gap_fill",
    "box_breakout",
    "volume_dry_bottom",
    # 「量价底背离」2026-10-18 撤下（用户要求），不在这批里了
    "shrink_limit_up",
    "volume_stall",
    "v_bottom",
    "round_bottom",
    "triple_bottom",
    "rising_wedge",
    # 「矩形箱体」「下降楔形」2026-10-18 撤下（用户要求），不在这批里了
    "diamond",
    # 「启明星 / 锤子线 / 倒锤子线」2026-10-14 撤下（噪声），不在这批里了；
    # 同日「看涨吞没 / 阳包阴」按同一条线（信号量 ≥1000 且 |10 日超额| < 0.5%）也撤下
    "three_white_soldiers",
)

_BY_KEY = {pattern.key: pattern for pattern in PATTERNS}


def _upto(bars: Bars, end: int) -> Bars:
    """截到第 `end` 根（不含）为止。

    判定函数只认「序列的最后一天就是今天」，所以回测到哪天就得把序列切到哪天 ——
    直接传整段等于把后面的行情喂进去，那就是未来函数。
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
        # 代码要一起带过去：「知行超级B1」的 `宽幅板` 按代码前缀判 20cm，
        # 丢了它整条判据就退化成「只看近 200 日有没有 >15% 的单日涨幅」
        code=bars.code,
    )


def _as_hit(signal) -> dict | None:
    """Signal → 命中明细字典；分数不到入库门槛的按未命中处理。

    回测必须与线上用同一个 `MIN_SCORE` —— 少了这一条，会把一堆线上根本不会显示的
    弱信号算进统计（结论会偏乐观）。这也是这里用生产常量、而不是另定一个的原因。
    """
    if signal is None or signal.score < MIN_SCORE:
        return None
    return {**signal.detail, "score": round(signal.score, 1)}


def _forward_outcomes(
    bars: Bars,
    date_index: dict,
    g: int,
    all_dates: list,
    entry: float,
) -> list[float | None]:
    """按**交易日**对齐算 `HORIZONS` 各档收益（`close[目标日] / entry − 1`）。

    **2026-10-10 从「按行」改为「按交易日」**，理由：`stock_daily` 没有停牌日的行，
    原来的 `close[t + h]` 取到的是**第 h 行**而不是「第 h 个交易日」，停牌空洞会把
    实际持有期拉长（例：000010 2026-04-23 按行 5 日 −25.68% vs 按日历 −21.89%）；
    而线上 `pattern_track._curves_from` 是按 `TradeCalendar` 定位、不受空洞影响，
    回测必须与它同口径。

    「第 h 个交易日」用数据自身的日期并集（`all_dates`）来定位：`g` 是命中日在
    `all_dates` 里的全局位置，`all_dates[g + h]` 就是目标交易日；再用该股的
    `date_index` 把目标交易日换回这只票的行号。目标日**该股停牌（没行）**时该档返回
    `None` —— 线上对这种票也是「这一档不算样本」，而不是拿一个假的 0% 顶进去。
    """
    outcomes: list[float | None] = []
    for h in HORIZONS:
        target = g + h
        if target >= len(all_dates):
            outcomes.append(None)
            continue
        j = date_index.get(all_dates[target])
        outcomes.append(None if j is None else float(bars.close[j]) / entry - 1)
    return outcomes


def _fmt_pct(value: float | None) -> str:
    """命中明细里的一档收益；该档该股停牌（None）时显示「—」。"""
    return "—" if value is None else f"{value * 100:>+6.1f}%"


# ---------------------------------------------------------------- 廉价预筛
#
# 预筛只是**必要条件**，用来先把 (票, 日) 组合砍掉九成以上；命中与否一律由生产函数
# 说了算。没有预筛的形态就走全量扫 —— 2026-09-25 补的那一批都属这一类：它们的
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


PRESCREENS = {
    "three_stage": prescreen_three_stage,
    "breakout_flat": prescreen_breakout_flat,
    "limit_surge_flat": prescreen_limit_surge,
}


# ---------------------------------------------------------------- K 线确认实验
#
# 2026-10-18 的清单 #3：把 10-14 删除的那批「单 K 蜡烛形态」当成**已有形态命中日的
# 确认条件**，先用回测看有没有增益 —— 不再单独注册形态。
#
# 三条判定从删除前那一版逐字取回（`git show c826e34:backend/app/services/patterns.py`），
# 阈值也保持一致。**只在这个脚本里用**，不进 `services/patterns.py` 的注册表。
CANDLE_BODY_MAX = 0.35  # 小实体：实体 / 全日振幅 的上限
CANDLE_SHADOW_MULT = 2.0  # 长影线至少是实体的多少倍
CANDLE_SHORT_SHADOW = 0.3  # 另一侧影线占振幅的上限
CANDLE_PRIOR_DAYS = 5
CANDLE_PRIOR_DROP = 0.03  # 反转类蜡烛要在跌势之后出现才有意义
ENGULF_MIN_BODY = 1.3
ENGULF_MIN_CLOSE_POS = 0.6
STAR_BIG_BODY = 0.015
STAR_SMALL_BODY = 0.5


def _candle_parts(bars: Bars, index: int = -1) -> tuple[float, ...]:
    """`(开, 高, 低, 收, 实体, 上影, 下影, 振幅)`。"""
    open_, high, low, close = (
        float(bars.open[index]),
        float(bars.high[index]),
        float(bars.low[index]),
        float(bars.close[index]),
    )
    return (
        open_,
        high,
        low,
        close,
        abs(close - open_),
        high - max(open_, close),
        min(open_, close) - low,
        high - low,
    )


def _prior_drop(bars: Bars, days: int = CANDLE_PRIOR_DAYS) -> float:
    """`days` 个交易日前的收盘到**昨收**的跌幅（正数表示下跌）。"""
    if len(bars) < days + 2:
        return 0.0
    base = float(bars.close[-1 - days])
    if base <= 0:
        return 0.0
    return float(base / bars.close[-2] - 1)


def _engulf_shape(bars: Bars) -> tuple[float, float] | None:
    """阳包阴 / 看涨吞没共用的两根 K 判据。"""
    if len(bars) < 3:
        return None
    prev_open, _, _, prev_close, prev_body, _, _, prev_span = _candle_parts(bars, -2)
    open_, _, low, close, body, _, _, span = _candle_parts(bars)
    if prev_close >= prev_open or close <= open_:
        return None
    if prev_body <= 0 or prev_span <= 0 or span <= 0:
        return None
    if not (open_ <= prev_close and close >= prev_open):
        return None
    ratio = body / prev_body
    if ratio < ENGULF_MIN_BODY:
        return None
    close_pos = (close - low) / span
    if close_pos < ENGULF_MIN_CLOSE_POS:
        return None
    return ratio, close_pos


def has_bullish_engulfing(bars: Bars) -> bool:
    """看涨吞没：吞没形状 + 出现在近 5 日回调之后。"""
    if len(bars) < CANDLE_PRIOR_DAYS + 3:
        return False
    if _prior_drop(bars) < CANDLE_PRIOR_DROP:
        return False
    return _engulf_shape(bars) is not None


def has_hammer(bars: Bars) -> bool:
    """锤子线：长下影（≥2 倍实体）、上影极短、小实体，且出现在下跌之后。"""
    if len(bars) < CANDLE_PRIOR_DAYS + 2:
        return False
    _, _, _, _, body, upper, lower, span = _candle_parts(bars)
    if span <= 0 or body <= 0:
        return False
    if body > span * CANDLE_BODY_MAX or upper > span * CANDLE_SHORT_SHADOW:
        return False
    if lower < body * CANDLE_SHADOW_MULT:
        return False
    return _prior_drop(bars) >= CANDLE_PRIOR_DROP


def has_morning_star(bars: Bars) -> bool:
    """启明星：大阴 → 小实体（探底）→ 大阳，右侧收盘收复左侧实体一半以上。"""
    if len(bars) < CANDLE_PRIOR_DAYS + 4:
        return False
    open1, _, _, close1, body1, _, _, span1 = _candle_parts(bars, -3)
    _, _, _, _, body2, _, _, span2 = _candle_parts(bars, -2)
    open3, _, _, close3, body3, _, _, span3 = _candle_parts(bars)
    if close1 >= open1 or body1 <= 0 or span1 <= 0:
        return False
    if body1 / close1 < STAR_BIG_BODY:
        return False
    if span2 <= 0 or body2 > body1 * STAR_SMALL_BODY:
        return False
    if close3 <= open3 or body3 <= 0 or span3 <= 0:
        return False
    if body3 / close3 < STAR_BIG_BODY * 0.6:
        return False
    return close3 >= (open1 + close1) / 2


CANDLE_CHECKS = {
    "bullish_engulfing": has_bullish_engulfing,
    "hammer": has_hammer,
    "morning_star": has_morning_star,
}
CANDLE_LABELS = {
    "bullish_engulfing": "看涨吞没",
    "hammer": "锤子线",
    "morning_star": "启明星",
}


# ---------------------------------------------------------------- 过滤条件对照
#
# 2026-10-19 清单「指令 0」：把「命中日是否同时满足某个过滤条件」拆成过滤组 / 基础组
# 对照，与 K 线确认实验同一套骨架。注册表签名统一为：
#
#     check(code: str, bars: Bars, i: int, ctx: dict) -> bool
#
# - `bars` 是整段序列、`i` 是**命中日**下标；判定只能读 `<= i` 的部分，不许看未来。
# - `ctx` 放横截面预计算（RPS 排名 / 行业涨幅），单票条件用不到时是 `{}`。
# - **只有带 `--filter-split` 才建累加器、才预计算**，平时零额外开销。
FILTER_LABELS: dict[str, str] = {
    "ma60_up": "MA60 向上",
    "rps50_90": "RPS50≥90",
    "rps120_85": "RPS120≥85",
    "rps_dual": "RPS50≥90 且 RPS120≥85",
}
FILTER_CHECKS: dict[str, Callable[[str, Bars, int, dict], bool]] = {}

# 每个过滤条件要用到哪几个 RPS 窗口（`--filter-split` 里一个都没选就不预计算）
RPS_CONDITIONS: dict[str, tuple[int, ...]] = {
    "rps50_90": (50,),
    "rps120_85": (120,),
    "rps_dual": (50, 120),
}


def _filter_ma60_up(code: str, bars: Bars, i: int, ctx: dict) -> bool:
    """占位过滤：收盘 > MA60，且 MA60 比 5 个交易日前高。

    它只用来验证 `--filter-split` 框架能跑通，本身不是候选策略 ——
    真要用，得先看报告里能不能过决策线。
    """
    if i < 64:
        return False
    close = bars.close
    ma_now = float(np.mean(close[i - 59 : i + 1]))
    ma_prev = float(np.mean(close[i - 64 : i - 4]))
    return float(close[i]) > ma_now and ma_now > ma_prev


FILTER_CHECKS["ma60_up"] = _filter_ma60_up


# ---------------------------------------------------------------- RPS 相对强度
#
# 欧奈尔相对强度：**N 日涨幅在当天横截面上的百分位**（0~100）。
# 口径（2026-10-19 清单「指令 1」）：
# - 分母是**参与本次回测的那批票**（与基准同池），停牌当天没有行 → 不参与排名；
# - 只看当天及以前：涨幅用 `close[t] / close[t-N] - 1`，t-N 不足的直接跳过；
# - 按日一次性排序，不在每条信号上现算。

RPS_TOP = 90.0


def _rps_context(
    bars_by_code: dict[str, Bars], windows: set[int]
) -> dict[int, dict[date, dict[str, float]]]:
    """`{窗口: {交易日: {代码: 0~100 百分位}}}`。

    同一只票在窗口内停牌时缺行，`close[i] / close[i-N]` 按**行**对齐（与
    MA60 等指标同口径），不跨空缺补交易日。
    """
    by_date: dict[int, dict[date, dict[str, float]]] = {
        window: defaultdict(dict) for window in windows
    }
    for code, bars in bars_by_code.items():
        close = bars.close
        for window in windows:
            if len(bars) <= window:
                continue
            gains = close[window:] / close[:-window] - 1.0
            for offset, gain in enumerate(gains.tolist()):
                by_date[window][bars.dates[window + offset]][code] = gain

    tables: dict[int, dict[date, dict[str, float]]] = {}
    for window, days in by_date.items():
        table: dict[date, dict[str, float]] = {}
        for day, gains in days.items():
            if not gains:
                continue
            codes = list(gains)
            values = np.fromiter((gains[code] for code in codes), dtype=float)
            order = np.argsort(values)
            ranks = np.empty(len(values), dtype=float)
            ranks[order] = np.arange(len(values), dtype=float)
            # 全是同一涨幅（或只有一只票）时，denom 兜住 0 除
            percent = ranks / max(len(values) - 1, 1) * 100.0
            table[day] = dict(zip(codes, percent.tolist()))
        tables[window] = table
    return tables


def _rps_lookup(ctx: dict, window: int, code: str, day: date) -> float | None:
    table = ctx.get("rps", {}).get(window)
    if not table:
        return None
    return table.get(day, {}).get(code)


def _filter_rps50_90(code: str, bars: Bars, i: int, ctx: dict) -> bool:
    value = _rps_lookup(ctx, 50, code, bars.dates[i])
    return value is not None and value >= RPS_TOP


def _filter_rps120_85(code: str, bars: Bars, i: int, ctx: dict) -> bool:
    value = _rps_lookup(ctx, 120, code, bars.dates[i])
    return value is not None and value >= 85.0


def _filter_rps_dual(code: str, bars: Bars, i: int, ctx: dict) -> bool:
    day = bars.dates[i]
    rps50 = _rps_lookup(ctx, 50, code, day)
    rps120 = _rps_lookup(ctx, 120, code, day)
    return (
        rps50 is not None
        and rps120 is not None
        and rps50 >= RPS_TOP
        and rps120 >= 85.0
    )


FILTER_CHECKS["rps50_90"] = _filter_rps50_90
FILTER_CHECKS["rps120_85"] = _filter_rps120_85
FILTER_CHECKS["rps_dual"] = _filter_rps_dual


def _filter_context(
    names: list[str], bars_by_code: dict[str, Bars], all_dates: list[date]
) -> dict:
    """过滤条件需要的横截面预计算（**只算被点名的条件**）。"""
    ctx: dict = {}
    windows: set[int] = set()
    for name in names:
        windows.update(RPS_CONDITIONS.get(name, ()))
    if windows:
        # 额外表（RPS 单独回测）固定用 RPS50，只选 rps120_85 时也要补上
        windows.add(50)
        logger.info("RPS 预计算：窗口 %s", ", ".join(str(w) for w in sorted(windows)))
        ctx["rps"] = _rps_context(bars_by_code, windows)
    return ctx


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
        record = {
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
        # 与 `_load_bars` 同口径：缺任一项（尤其停牌残行）都不能当一根 K 线，
        # 否则 `build_bars` 的 `float(None)` 会抛
        if not usable_bar(record):
            continue
        grouped[code].append(record)
    return latest, dict(grouped)


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
        outcomes: list[float | None],
        bench_means: list[float],
        month: str,
        sample: dict | None = None,
    ) -> None:
        # 某一档为 None = 该股在那一档的目标交易日停牌（没行），**该档不计样本** ——
        # 与线上 `pattern_track`「两端都要有行」同口径（2026-10-10）。
        for index, value in enumerate(outcomes):
            if value is None:
                continue
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
        # 致富那两个（明天预案 / 明天盯）**不进全量回测**：它们的候选池是悟道同口径的
        # 强势小池（见 scan_patterns._candidate_codes），拿全市场跑出来的是
        # 另一个分布，结论没有意义
        keys = [p.key for p in PATTERNS if p.group != "致富"]
    else:
        # 支持逗号分隔的一串（`--pattern a,b,c`）：只重算几个形态时用它 ——
        # 序列只切一遍，多带几个形态几乎不额外花时间，比跑 N 次省掉 N-1 次全池加载
        keys = [item.strip() for item in name.split(",") if item.strip()]
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
        help="形态 key（可逗号分隔传多个）/ all-new（18 个新形态）/ all（除致富外全部）",
    )
    parser.add_argument("--samples", type=int, default=15, help="单个形态时打印多少条命中明细")
    parser.add_argument("--stocks", type=int, default=0, help="只用前 N 只票（0 = 全部）")
    parser.add_argument(
        "--board",
        default="all",
        choices=("all", "wudao", "prod"),
        help=(
            "**要与生产对齐就跑 prod**（= 线上落库闸门 `scan_patterns.is_pattern_board`："
            "主板 + 创业板 + 科创板，不含北交所；2026-10-14 起）；"
            "wudao = 只创业板 + 科创板（`is_wudao_board`，悟道六池那一套，它们的阈值按 "
            "20cm 校准；`patterns.py` 各段旧回测表就是这个口径）；"
            "all = 池子里全部（含北交所）"
        ),
    )
    parser.add_argument("--history", action="store_true", help="用长历史库 history.db")
    parser.add_argument(
        "--candle-split",
        action="store_true",
        help=(
            "K 线确认实验：把每个形态的信号按「命中当天是否同时出现看涨吞没 / 锤子线 / "
            "启明星」分组，输出两组的 5/10/20 日超额与胜率之差"
        ),
    )
    parser.add_argument(
        "--filter-split",
        default="",
        help=(
            "过滤条件对照：逗号分隔（如 ma60_up、rps50_90）。把每个形态的信号按"
            "「命中当天该条件是否成立」分成过滤组 / 基础组，输出两组之差。"
            f"可选：{', '.join(FILTER_CHECKS)}"
        ),
    )
    parser.add_argument(
        "--bars", type=int, default=DEFAULT_HISTORY_BARS, help=f"长历史窗口（默认 {DEFAULT_HISTORY_BARS} 根）"
    )
    args = parser.parse_args()

    filter_names = [name.strip() for name in args.filter_split.split(",") if name.strip()]
    unknown_filters = [name for name in filter_names if name not in FILTER_CHECKS]
    if unknown_filters:
        raise SystemExit(
            f"未知的过滤条件：{', '.join(unknown_filters)}"
            f"（可选：{', '.join(FILTER_CHECKS)}）"
        )

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from app.jobs.collect_universe import load_codes

    codes = load_codes()
    if args.board == "wudao":
        from app.jobs.scan_patterns import is_wudao_board

        codes = [code for code in codes if is_wudao_board(code)]
        logger.info("只扫创业板 + 科创板：%d 只（与悟道六池同板块口径）", len(codes))
    elif args.board == "prod":
        from app.jobs.scan_patterns import is_pattern_board

        codes = [code for code in codes if is_pattern_board(code)]
        logger.info("按线上落库闸门：%d 只（主板 + 创业板 + 科创板，不含北交所）", len(codes))
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

    # **ST 一律不参与**，判据与线上扫描**同一个函数**（`patterns.is_st`）——
    # 各写一份的话，回测数字就不再代表生产（8.68 那批形态就是栽在这上面）。
    # ⚠️ 必须在这里剔（`grouped` 一份），**不能只剔样本**：下面那张基准表是拿
    # 同一份逐票序列算出来的「同一天全市场平均」，样本剔了 ST 而基准不剔，
    # 两组就不是对照了。
    st_dropped = [code for code, records in grouped.items() if is_st(records[-1].get("name"))]
    for code in st_dropped:
        grouped.pop(code, None)
    if st_dropped:
        logger.info("剔除 %d 只 ST 票（与线上扫描同口径）", len(st_dropped))

    bars_by_code = {
        code: build_bars(records, code)
        for code, records in grouped.items()
        if len(records) >= MIN_BARS
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

    # K 线确认实验：每个 (形态, 蜡烛名) 一份累加器。只有 `--candle-split` 时才建，
    # 平时零额外开销（判定函数要读开高低收，不是免费的）。
    candle_tallies: dict[tuple[str, str], Tally] = {}
    if args.candle_split:
        candle_tallies = {
            (key, candle): Tally() for key in targets for candle in CANDLE_CHECKS
        }
        logger.info("K 线确认实验：%d 个形态 × %d 种蜡烛", len(targets), len(CANDLE_CHECKS))

    # 过滤条件对照：过滤组 / 基础组（**不满足**该条件的信号）各一份累加器。
    # 两组互补，样本加起来就是该形态的全部信号。
    filter_tallies: dict[tuple[str, str], Tally] = {}
    filter_base_tallies: dict[tuple[str, str], Tally] = {}
    filter_ctx: dict = {}
    if filter_names:
        filter_tallies = {
            (key, name): Tally() for key in targets for name in filter_names
        }
        filter_base_tallies = {
            (key, name): Tally() for key in targets for name in filter_names
        }
        logger.info(
            "过滤条件对照：%d 个形态 × %d 个条件（%s）",
            len(targets),
            len(filter_names),
            ", ".join(filter_names),
        )

    # RPS 单独回测（清单「指令 1」要求）：把「每天 RPS50 ≥ 90」当成一个虚拟形态，
    # 与真实形态用同一套 20 日去重与同日横截面基准。
    rps_extra: Tally | None = None
    last_rps: dict[str, int] = {}
    if any(name in RPS_CONDITIONS for name in filter_names):
        rps_extra = Tally()
        logger.info("RPS 单独回测：每天 RPS50 ≥ 90，按 %d 日去重", DEDUP_DAYS)

    # **交易日轴**：取数据自身的日期并集。持有期一律按**交易日**对齐（见 `_forward_outcomes`
    # 与模块顶部「持有期口径」一节）—— 停牌日的行在库里是缺的，只有一条全局日期轴才能
    # 把「第 h 个交易日」和「第 h 行」区分开。
    all_dates = sorted({day for bars in bars_by_code.values() for day in bars.dates})
    global_pos = {day: i for i, day in enumerate(all_dates)}
    logger.info("交易日轴：%d 个交易日（%s ~ %s）", len(all_dates), all_dates[0], all_dates[-1])

    # 过滤条件的横截面预计算要在交易日轴之后做（RPS / 行业涨幅都按「日」组织）
    if filter_names:
        filter_ctx = _filter_context(filter_names, bars_by_code, all_dates)

    # 基准：每个交易日 → 该日之后**第 h 个交易日**的平均收益。
    #
    # ⚠️ **分母就是参与本次回测的那批票**（下面这份 `bars_by_code`）—— 所以
    # `--board wudao` 时基准是「**创业板 + 科创板等权**」而不是全市场，超额里**不含**
    # 板块 beta。（2026-10-14 更正：这里原来写的是「全市场平均」，措辞与代码不符，
    # 曾让人误判成「超额混着板块 beta、只能比相对强弱」。）
    #
    # 必须按**同一天**比，否则「形态命中组涨了 6%」可能只是那段时间大盘在涨。
    #
    # 这里只累加 sum 与 count（而不是把每天的收益都存成一个列表）：5 年 × 全池会攒下
    # 上千万个浮点数，存列表峰值能吃掉好几个 GB，而报告只需要那个均值。
    # 口径与命中样本一致：按交易日对齐，目标日停牌的票不计入该日基准。
    bench_sum: dict[int, dict[object, float]] = {h: defaultdict(float) for h in HORIZONS}
    bench_count: dict[int, dict[object, int]] = {h: defaultdict(int) for h in HORIZONS}
    for bars in bars_by_code.values():
        date_index = {day: i for i, day in enumerate(bars.dates)}
        for i, day in enumerate(bars.dates):
            entry = float(bars.close[i])
            if entry <= 0:
                continue
            g = global_pos[day]
            for h, value in zip(
                HORIZONS, _forward_outcomes(bars, date_index, g, all_dates, entry)
            ):
                if value is None:
                    continue
                bench_sum[h][day] += value
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
        date_index = {day: i for i, day in enumerate(bars.dates)}
        for t in range(MIN_BARS, n):
            day = bars.dates[t]
            g = global_pos[day]
            if g + max(HORIZONS) >= len(all_dates):
                # 命中日之后不足 `max(HORIZONS)` 个交易日了。日期升序，可以直接停这一只票。
                break
            if rps_extra is not None:
                rps50 = _rps_lookup(filter_ctx, 50, code, day)
                if (
                    rps50 is not None
                    and rps50 >= RPS_TOP
                    and t - last_rps.get(code, -10**6) >= DEDUP_DAYS
                ):
                    entry_rps = float(bars.close[t])
                    if entry_rps > 0:
                        last_rps[code] = t
                        rps_extra.add(
                            _forward_outcomes(bars, date_index, g, all_dates, entry_rps),
                            [bench[h].get(day, 0.0) for h in HORIZONS],
                            str(day)[:7],
                        )
            sample: Bars | None = None
            # 同一个 (票, 交易日) 的蜡烛形态只判一次：多个形态同日命中时共用
            candle_flags: tuple[str, ...] | None = None
            for key, target in targets.items():
                screen = target["screen"]
                if screen is not None and not screen(bars, t):
                    continue
                if sample is None:
                    sample = _upto(bars, t + 1)
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
                outcomes = _forward_outcomes(bars, date_index, g, all_dates, entry)
                tallies[key].add(
                    outcomes,
                    [bench[h].get(day, 0.0) for h in HORIZONS],
                    str(day)[:7],
                    {
                        "code": code,
                        "name": grouped[code][-1].get("name") or "",
                        "date": day,
                        "hit": hit,
                        "outcomes": dict(zip(HORIZONS, outcomes)),
                    },
                )
                if candle_tallies:
                    if candle_flags is None:
                        candle_flags = tuple(
                            name
                            for name, detect in CANDLE_CHECKS.items()
                            if detect(sample)
                        )
                    for candle in candle_flags:
                        candle_tallies[(key, candle)].add(
                            outcomes,
                            [bench[h].get(day, 0.0) for h in HORIZONS],
                            str(day)[:7],
                        )
                if filter_tallies:
                    for name in filter_names:
                        bucket = (
                            filter_tallies
                            if FILTER_CHECKS[name](code, bars, t, filter_ctx)
                            else filter_base_tallies
                        )
                        bucket[(key, name)].add(
                            outcomes,
                            [bench[h].get(day, 0.0) for h in HORIZONS],
                            str(day)[:7],
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
    if candle_tallies:
        _report_candle_split(tallies, candle_tallies)
    if filter_tallies:
        _report_filter_split(filter_tallies, filter_base_tallies)
    if rps_extra is not None and rps_extra.size:
        _report_rps_standalone(rps_extra)
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
                f"  → 5日{_fmt_pct(out.get(5))} 10日{_fmt_pct(out.get(10))} "
                f"20日{_fmt_pct(out.get(20))}"
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


def _report_candle_split(
    tallies: dict[str, Tally], split: dict[tuple[str, str], Tally]
) -> None:
    """K 线确认实验表：确认组 vs 同形态全部信号（基础组）。

    决策线（清单 #3）：某个「形态 × K 线确认」组合的 10 日超额比基础组高 ≥1pp、
    且确认组样本 ≥200，才考虑在命中明细里加 `candle_confirm` 标签；
    否则这一项结束，把结论写进 `patterns.py` 的注释，免得以后又想做一遍。
    """
    index10 = HORIZONS.index(10)
    rows = []
    for (key, candle), tally in split.items():
        base = tallies.get(key)
        if base is None or not base.size or tally.size == 0:
            continue
        # 基础组 10 日样本都没有时，差值没有意义（该形态还没走到 10 日）
        if base.counts[index10] == 0:
            continue
        base_excess = base.stats(index10)[0] - base.bench_mean(index10)
        excess = tally.stats(index10)[0] - tally.bench_mean(index10)
        rows.append(
            {
                "key": key,
                "candle": candle,
                "samples": tally.size,
                "excess5": tally.stats(0)[0] - tally.bench_mean(0),
                "excess10": excess,
                "excess20": tally.stats(2)[0] - tally.bench_mean(2),
                "win10": tally.stats(index10)[2],
                "diff": (excess - base_excess) * 100,
                "win_diff": (tally.stats(index10)[2] - base.stats(index10)[2]) * 100,
            }
        )

    print()
    print("=" * 118)
    print("K 线确认实验：同一形态的信号按「命中当天是否同时出现该蜡烛形态」分组")
    print(
        "决策线：10 日超额比基础组高 ≥1pp 且确认组样本 ≥200 → 才考虑加 candle_confirm 标签"
    )
    print("-" * 118)
    if not rows:
        print("没有任何组合（基础组或确认组没有样本）")
        print("=" * 118)
        return

    rows.sort(key=lambda item: item["diff"], reverse=True)
    shown = [item for item in rows if item["samples"] >= 30]
    hidden = len(rows) - len(shown)
    print(
        f"{'分组':<10}{'形态':<16}{'K线确认':<10}{'样本':>6}"
        f"{'5日超额':>10}{'10日超额':>10}{'20日超额':>10}"
        f"{'10日胜率':>9}{'胜率差':>9}{'超额差':>9}  结论"
    )
    print("-" * 118)
    winners = []
    for item in shown:
        meta = _BY_KEY[item["key"]]
        label = CANDLE_LABELS[item["candle"]]
        hit = item["diff"] >= 1.0 and item["samples"] >= 200
        note = "✅ 达标" if hit else ("样本不足" if item["samples"] < 200 and item["diff"] >= 1.0 else "")
        if hit:
            winners.append(f"{meta.name} × {label}")
        print(
            f"{meta.group:<10}{meta.name:<16}{label:<10}{item['samples']:>6}"
            f"{item['excess5'] * 100:>9.2f}%{item['excess10'] * 100:>9.2f}%"
            f"{item['excess20'] * 100:>9.2f}%{item['win10'] * 100:>8.1f}%"
            f"{item['win_diff']:>+8.1f}{item['diff']:>+8.2f}  {note}"
        )
    print("=" * 118)
    print(f"（只显示确认组样本 ≥30 的组合；另有 {hidden} 个组合样本不足 30 未列出）")
    print("（超额差 = 确认组 10 日超额 − 基础组 10 日超额，单位 pp；胜率差同）")
    print()
    if winners:
        print("达到决策线的组合：" + "、".join(winners))
    else:
        print(
            "结论：没有任何「形态 × K 线确认」组合达到「10 日超额 +1pp 且样本 ≥200」——"
            "本次实验结束，不新增 candle_confirm 标签、也不恢复独立蜡烛形态。"
        )


def _report_filter_split(
    matched: dict[tuple[str, str], Tally],
    base: dict[tuple[str, str], Tally],
) -> None:
    """过滤条件对照表：满足条件的「过滤组」vs 不满足的同形态信号「基础组」。

    两组**互补**：样本相加 = 该形态的全部信号。这样比「过滤组 vs 全部信号」
    更能看出条件本身的增量 —— 后者会把过滤组自己也算进对照组。

    决策线（2026-10-19 清单）：过滤组样本 ≥200 且 10 日超额比基础组高 ≥1pp，
    才算这个条件对这个形态有效；长历史方向一致才上线。
    """
    index10 = HORIZONS.index(10)
    rows = []
    for (key, name), tally in matched.items():
        reference = base.get((key, name))
        if reference is None or not tally.size or not reference.size:
            continue
        # 任一组还没走到 10 日，差值没有意义
        if tally.counts[index10] == 0 or reference.counts[index10] == 0:
            continue
        excess = tally.stats(index10)[0] - tally.bench_mean(index10)
        base_excess = reference.stats(index10)[0] - reference.bench_mean(index10)
        rows.append(
            {
                "key": key,
                "filter": name,
                "samples": tally.size,
                "base_samples": reference.size,
                "excess5": tally.stats(0)[0] - tally.bench_mean(0),
                "excess10": excess,
                "excess20": tally.stats(2)[0] - tally.bench_mean(2),
                # 60 日要命中后再走 60 个交易日，窗口末端那批信号没有样本 ——
                # 为 0 时打印 `—`，不能当「超额 0」看
                "excess60": (
                    tally.stats(3)[0] - tally.bench_mean(3)
                    if tally.counts[3]
                    else None
                ),
                "win10": tally.stats(index10)[2],
                "median10": tally.stats(index10)[1],
                "base_win10": reference.stats(index10)[2],
                "diff": (excess - base_excess) * 100,
                "win_diff": (tally.stats(index10)[2] - reference.stats(index10)[2]) * 100,
            }
        )

    print()
    print("=" * 150)
    print("过滤条件对照：同一形态的信号按「命中当天该过滤条件是否成立」分组")
    print(
        "决策线：过滤组 10 日超额比基础组高 ≥1pp 且过滤组样本 ≥200 → 该条件对这个形态有效"
    )
    print("-" * 150)
    if not rows:
        print("没有任何组合（过滤组或基础组没有样本）")
        print("=" * 150)
        return

    rows.sort(key=lambda item: item["diff"], reverse=True)
    shown = [item for item in rows if item["samples"] >= 30]
    hidden = len(rows) - len(shown)
    print(
        f"{'分组':<10}{'形态':<16}{'过滤条件':<14}{'过滤组':>7}{'基础组':>7}"
        f"{'5日超额':>10}{'10日超额':>10}{'20日超额':>10}{'60日超额':>10}"
        f"{'10日胜率':>9}{'10日中位':>9}{'胜率差':>9}{'超额差':>9}  结论"
    )
    print("-" * 150)
    winners = []
    for item in shown:
        meta = _BY_KEY[item["key"]]
        label = FILTER_LABELS.get(item["filter"], item["filter"])
        hit = item["diff"] >= 1.0 and item["samples"] >= 200
        note = "✅ 达标" if hit else ("样本不足" if item["diff"] >= 1.0 else "")
        if hit:
            winners.append(f"{meta.name} × {label}")
        excess60 = (
            "         —" if item["excess60"] is None else f"{item['excess60'] * 100:>9.2f}%"
        )
        print(
            f"{meta.group:<10}{meta.name:<16}{label:<14}"
            f"{item['samples']:>7}{item['base_samples']:>7}"
            f"{item['excess5'] * 100:>9.2f}%{item['excess10'] * 100:>9.2f}%"
            f"{item['excess20'] * 100:>9.2f}%{excess60}"
            f"{item['win10'] * 100:>8.1f}%{item['median10'] * 100:>8.2f}%"
            f"{item['win_diff']:>+8.1f}{item['diff']:>+8.2f}  {note}"
        )
    print("=" * 150)
    print(f"（只显示过滤组样本 ≥30 的组合；另有 {hidden} 个组合样本不足 30 未列出）")
    print("（超额差 = 过滤组 10 日超额 − 基础组 10 日超额，单位 pp；胜率差同）")
    print()
    if winners:
        print("达到决策线的组合：" + "、".join(winners))
    else:
        print(
            "结论：没有任何「形态 × 过滤条件」组合达到「10 日超额 +1pp 且过滤组样本 ≥200」。"
        )


def _report_rps_standalone(tally: Tally) -> None:
    """RPS50 ≥ 90 单独（不叠任何形态）的表现。

    清单「指令 1」的附加要求：先知道相对强度本身有没有用，再谈它当过滤条件。
    口径与形态完全一致（20 日去重、同日横截面基准、按交易日持有期）。
    """
    index10 = HORIZONS.index(10)
    print()
    print("=" * 104)
    print("RPS50 ≥ 90 单独（不叠任何形态）：每天横截面涨幅前 10% 的票")
    print("-" * 104)
    header = f"{'信号数':>7}"
    for horizon in HORIZONS:
        header += f"{f'{horizon}日超额':>12}"
    header += f"{'10日胜率':>10}{'10日中位':>10}"
    print(header)
    print("-" * 104)
    line = f"{tally.size:>7}"
    for index in range(len(HORIZONS)):
        if tally.counts[index]:
            excess = tally.stats(index)[0] - tally.bench_mean(index)
            line += f"{excess * 100:>11.2f}%"
        else:
            line += f"{'—':>12}"
    line += f"{tally.stats(index10)[2] * 100:>9.1f}%"
    line += f"{tally.stats(index10)[1] * 100:>9.2f}%"
    print(line)
    print("=" * 104)
    print("（超额 = 命中组平均收益 − 同一天全市场平均收益；与形态表同一套基准）")


if __name__ == "__main__":
    sys.exit(main())