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

## 入场价口径（`--entry`）

- `close`（默认）：信号日**收盘价**入场，`close[t+h] / close[t] − 1`。历史结论全是这个口径。
- `next_open`：**次日开盘价**入场，`close[t+h] / open[t+1] − 1`。

默认的收盘入场有系统性偏差：形态在收盘后才成立，现实中最早只能次日开盘成交。
涨停 / 突破类形态上这个偏差是致命的 —— 用收盘价等于「买在条件成立的那根 K 线上」，
凭空吃掉次日的高开。**打板类必须用 `--entry next_open`。**

两处口径的**正常情况**下卖出日不变（仍是信号日之后第 h 个交易日的收盘），只换入场价，
所以「5 日」指的都是「在 t+1 开盘买入、持有到 t+5 收盘」。基准表也换成同一入场口径 ——
否则超额里会混进「全市场平均高开幅度」这个与形态无关的量。

### 2026-10-31 修正：入场日 = 实际成交日，持有期从它起算

原来 `next_open` 取**这只票自己的下一根 K 线开盘价**（停牌时那就是复牌首日），但
`_forward_outcomes` 仍按**信号日**的全局位置算目标日 —— 于是「周四才买到」的样本，
被当成「周一就买到」，持有期整体提前了停牌的天数。现在改成：

- `_entry()` 返回 `Entry(price, index, date)`：`next_open` 的 `date` 是**实际成交日**
  （T+1 停牌就是复牌那天）；拿不到下一根 K 线才返回 None（记「无法成交」）。
- **持有期定义**：`next_open` 时买入日算**第 1 个持有交易日**（周四买 → 5 日收益 =
  第 5 个持有交易日收盘，正常 T+1 时正好还是 `close[t+5]`，历史口径不变）；
  `close` 时买在收盘价、买入日算第 0 天，5 日收益 = `close[t+5]`。两种口径的
  `close[t+h]` 都没被机械改动，改的只是「入场日晚于 T+1」这一类。
- **预定退出日停牌**：不再静默丢样本，而是**顺延到复牌后第一个有行情的交易日收盘**
  （现实中停牌也卖不出去）；报告里记录计划退出日 / 实际退出日 / 实际持有天数。
- **数据末端或长期停牌**：该档记「无法退出」（不计入该档均值），并单独计数。
- **基准按实际入场日聚合**：策略组哪天入场，就拿那一天入场的全池平均作基准 ——
  两组同起点、同终点、同持有期，停牌延迟入场的样本不会与别的窗口比。

⚠️ `close` 口径仍是**理论口径**：形态在收盘后才成立，现实中最早只能次日开盘成交。
它只用于与历史数字对照，**不能**当作可交易收益读。

### `--skip-unbuyable`：剔除买不到的信号

即便改了入场时点，**入场价本身可能根本买不到**：次日开盘就在涨停价上（竞价即封 /
一字板），挂单根本成交不了。不剔的话，那一段涨停收益是纯账面数字，打板策略的回测
会被抬得非常高。

判定用 `limit_rules.limit_pct`（主板 10 / 创业板科创板 20 / 北交所 30）算限幅，
比 `入场价 / 前收 − 1 ≥ 限幅 − 容差`；容差 `0.005 / 前收` 覆盖板价四舍五入到分的
那一分钱。**基准表同口径剔除** —— 买不到的票不该出现在「可替代组合」里，否则超额里
会混进一批你根本没机会买的强势股。注意这也意味着涨停潮的日子里基准偏低、超额偏乐观。

买不到的信号**不占 20 日去重位**：同一波行情里如果几天后能成交，那次仍该算样本，
不能因为第一次买不到就把整段行情丢掉。

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
    python scripts/backtest_patterns.py --pattern limit_surge_flat --history --entry next_open --skip-unbuyable
                                                                                   # 打板类：次日开盘 + 剔一字板

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
import math
import sys
import time
from array import array
from bisect import bisect_left
from collections import Counter, defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.jobs.scan_patterns import (  # noqa: E402
    _load_bars,
    is_wudao_board,
    pattern_board_allowed,
    WUDAO_CAND_MAX_HIGH,
    WUDAO_CAND_MAX_PCT,
    WUDAO_CAND_KEYS,
    WUDAO_CAND_MIN_HIGH,
    WUDAO_CAND_MIN_PCT,
    WUDAO_POOL_KEYS,
)
from app.models import StockDaily  # noqa: E402
from app.services import limit_rules, rps as rps_rules, trend as trend_rules  # noqa: E402
from app.services.patterns import (  # noqa: E402
    BF_MAX_GAP,
    BF_MIN_FLAT,
    BF_MIN_GAIN,
    BF_MIN_VOL,
    LS_LOOKBACK,
    MIN_SCORE,
    PATTERNS,
    Bars,
    build_bars,
    is_st,
    limit_threshold,
    usable_bar,
    zgb1_at,
)
from draft_patterns import DRAFT_PATTERNS  # noqa: E402  （同目录的脚本）

logger = logging.getLogger("backtest")

# 持有期（交易日）。1/3 日是 2026-10-31 按用户的整改要求补的：短持有期才能看出
# 「次日开盘买入」这种口径下的真实分布；5/10/20/60 保留，历史结论与它们对齐。
HORIZONS = (1, 3, 5, 10, 20, 60)
MIN_BARS = 80
# 同一只票这么多交易日内的重复信号只留第一个（见模块说明第 2 条）
DEDUP_DAYS = 20

# 入场价口径（见模块说明「入场价口径」）：`close` 是历史默认，`next_open` 给打板 / 突破类用
ENTRY_CLOSE = "close"
ENTRY_NEXT_OPEN = "next_open"
ENTRY_MODES = (ENTRY_CLOSE, ENTRY_NEXT_OPEN)

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
# 实验形态（**不在生产注册表**）：只在这里能被 `--pattern` 点名。
# 与生产形态共用同一套判定入口（`Pattern.detect`），所以回测结果可直接对比。
_DRAFT_KEYS = {pattern.key for pattern in DRAFT_PATTERNS}
for _draft in DRAFT_PATTERNS:
    _BY_KEY[_draft.key] = _draft


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


@dataclass(frozen=True, slots=True)
class Entry:
    """一次实际入场：价格、在**这只票**序列里的行号、成交交易日。

    2026-10-31 加：原来只有价格，停牌时「信号日」与「成交日」会差好几天，持有期却
    仍从信号日算 —— 这就是那次整改的入口。`date` 是**实际成交日**。
    """

    price: float
    index: int
    date: date


@dataclass(frozen=True, slots=True)
class Exit:
    """某一档持有期的出场结果（见模块说明「入场日 = 实际成交日」）。

    - `planned`：按交易日轴算出来的计划退出日；
    - `actual`：实际退出日 —— 计划日停牌时**顺延到复牌后第一个有行情的交易日**；
    - `held`：实际持有交易日数（`next_open` 时买入日算第 1 天；`close` 时算第 0 天）；
    - `rolled`：实际退出日晚于计划退出日（顺延过）；
    - `value` 为 None：**退不出来**（数据末端 / 长期停牌），不计入该档均值。
    """

    horizon: int
    value: float | None
    planned: date | None
    actual: date | None
    held: int | None
    rolled: bool


def _entry(bars: Bars, t: int, mode: str) -> Entry | None:
    """`t` 日信号的实际入场（价格 + 成交日）；拿不到可成交的开盘价时返回 None。

    - `close`：信号日收盘价，成交日就是信号日（**理论口径** —— 形态在收盘后才成立，
      现实中最早只能次日开盘成交，见模块说明 P0-3）；
    - `next_open`：**这只票自己的下一根 K 线开盘价**。T+1 停牌时那根就是复牌首日，
      也就是「最早能成交的价格」；成交日取那根 K 线的日期，不一定等于 T+1。
    """
    if mode == ENTRY_CLOSE:
        return Entry(float(bars.close[t]), t, bars.dates[t])
    if t + 1 >= len(bars):
        return None
    return Entry(float(bars.open[t + 1]), t + 1, bars.dates[t + 1])


def _entry_price(bars: Bars, t: int, mode: str) -> float | None:
    """只要价格的薄封装（历史调用点与单测用）。"""
    entry = _entry(bars, t, mode)
    return None if entry is None else entry.price


#: 板价四舍五入到分带来的相对误差上限（`0.005 元 / 前收`），再大就封顶半个百分点。
#: 复权价上换不成绝对价差，所以只能用相对值；取偏大的一侧 = 宁可多丢一个信号。
_OPEN_LIMIT_TOL_CAP = 0.005


def _unbuyable(bars: Bars, t: int, mode: str, limit_pct: float) -> bool:
    """入场价是不是**已经等于涨停价** —— 这种价买不到（竞价即封 / 一字板）。

    - `close` 口径看**信号日收盘**是否封在涨停价上（收盘那一下也买不到）；
    - `next_open` 口径看**次日开盘**是否已在涨停价上。

    限幅按代码取（`limit_rules.limit_pct`：主板 10 / 创业板科创板 20 / 北交所 30），
    不在这里另写一张板块表 —— 那个口径已经有两处漂移过（见 `limit_rules` 模块说明）。

    容差 `0.005 / 前收` 覆盖板价取整到分的那一分钱（例：前收 3.00、板价 3.30，
    开盘 3.30 就是买不到）。宁可多丢一个信号，也不虚增收益。
    """
    if mode == ENTRY_CLOSE:
        if t == 0:
            return False
        prev_close, price = float(bars.close[t - 1]), float(bars.close[t])
    else:
        if t + 1 >= len(bars):
            return True
        prev_close, price = float(bars.close[t]), float(bars.open[t + 1])
    if prev_close <= 0 or price <= 0:
        return True
    tol = min(_OPEN_LIMIT_TOL_CAP, 0.005 / prev_close)
    return price / prev_close - 1 >= limit_pct / 100.0 - tol


def _exit_point(
    all_dates: list[date], date_index: dict, target_g: int
) -> tuple[int, int] | None:
    """目标交易日 `target_g` 的 `(该股行号, 全局位置)`；该日停牌则顺延到之后第一个有行的交易日。

    顺延到交易日轴末尾仍然没有行情 → None（**退不出来**：数据末端或长期停牌）。
    正常「目标日有行」在第一次循环就命中，顺延几乎不会走。
    """
    n = len(all_dates)
    g = max(target_g, 0)
    while g < n:
        j = date_index.get(all_dates[g])
        if j is not None:
            return j, g
        g += 1
    return None


def _forward_outcomes(
    bars: Bars,
    date_index: dict,
    all_dates: list,
    entry: Entry,
    *,
    g_entry: int,
    hold_from_entry: bool,
) -> list[Exit]:
    """从**实际入场日**算 `HORIZONS` 各档收益（`close[实际退出日] / 入场价 − 1`）。

    **2026-10-10 从「按行」改为「按交易日」**：`stock_daily` 没有停牌日的行，原来的
    `close[t + h]` 取的是第 h **行**而不是第 h 个**交易日**，停牌空洞会把实际持有期
    拉长；而线上 `pattern_track._curves_from` 按 `TradeCalendar` 定位、不受空洞影响。

    **2026-10-31 再修：起点改成实际入场日**（原来固定用信号日 `g`，停牌延迟入场时
    持有期整体提前）。持有期定义见模块说明：

    - `hold_from_entry=True`（`next_open`）：买入日算**第 1 个持有交易日**，
      目标日 = `g_entry + h - 1`。正常 T+1 入场时正好是 `close[t+h]`，历史口径不变；
    - `hold_from_entry=False`（`close`）：买在收盘价、买入日算第 0 天，
      目标日 = `g_entry + h`，与历史完全一致。

    计划退出日停牌 → 顺延到复牌后第一个有行的交易日（`Exit.rolled=True`）；
    一直找不到 → `value=None`（**无法退出**，该档不计样本），由调用方计数，不再静默丢弃。
    """
    outcomes: list[Exit] = []
    total = len(all_dates)
    for h in HORIZONS:
        target_g = g_entry + h - (1 if hold_from_entry else 0)
        planned = all_dates[target_g] if 0 <= target_g < total else None
        point = _exit_point(all_dates, date_index, target_g)
        if point is None:
            outcomes.append(Exit(h, None, planned, None, None, False))
            continue
        j, g = point
        held = g - g_entry + (1 if hold_from_entry else 0)
        outcomes.append(
            Exit(
                h,
                float(bars.close[j]) / entry.price - 1,
                planned,
                all_dates[g],
                held,
                g != target_g,
            )
        )
    return outcomes


def _forward_values(
    bars: Bars,
    date_index: dict,
    all_dates: list,
    entry: Entry,
    *,
    g_entry: int,
    hold_from_entry: bool,
) -> list[float | None]:
    """`_forward_outcomes` 的**轻量版**（只给基准表用：全池 × 全历史 × 6 档）。

    基准只累加均值，不需要计划/实际退出日与持有天数；这里省掉 `Exit` 对象的分配，
    逻辑与 `_forward_outcomes` 一字不差（同一套目标日 + 顺延规则）。
    """
    values: list[float | None] = []
    for h in HORIZONS:
        point = _exit_point(
            all_dates, date_index, g_entry + h - (1 if hold_from_entry else 0)
        )
        values.append(None if point is None else float(bars.close[point[0]]) / entry.price - 1)
    return values


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
    """最近 `LS_LOOKBACK` 天里出现过涨停（按该股的涨跌幅限制判，见 `limit_threshold`）。"""
    if t < 40:
        return False
    start = max(0, t + 1 - LS_LOOKBACK)
    return bool((bars.pct_chg[start : t + 1] >= limit_threshold(bars.code)).any())


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
    "rps_any_90": "RPS 三线任一≥90",
    "trend_template": "趋势模板(Minervini)",
}
FILTER_CHECKS: dict[str, Callable[[str, Bars, int, dict], bool]] = {}

# 每个过滤条件要用到哪几个 RPS 窗口（`--filter-split` 里一个都没选就不预计算）
RPS_CONDITIONS: dict[str, tuple[int, ...]] = {
    "rps50_90": (50,),
    "rps120_85": (120,),
    "rps_dual": (50, 120),
    # 陶博士「一线红」：RPS50 / 120 / 250 任一 ≥90 即可
    "rps_any_90": (50, 120, 250),
    # 趋势模板第 8 条「RS ≥ 70」直接用 250 日分位（与站内 RPS 同口径）
    "trend_template": (250,),
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
    bars_by_code: dict[str, Bars], windows: set[int], all_dates: list[date]
) -> dict[int, dict[date, dict[str, float]]]:
    """`{窗口: {交易日: {代码: 0~100 百分位}}}`——**与线上 `services.rps` 同一套口径**。

    2026-10-24（选股池-8）改成调 `app.services.rps` 的纯函数：

    - 涨幅用 `pct_chg` **复利**（原来是 `close[i]/close[i-N]`，除权日会假跌）；
    - 窗口按**交易日**截（`all_dates` = 全市场日期并集），不是「最近 N 行」；
    - 分位用**中位秩**（原来是 `rank/(n-1)`，并列值会被读成极端）。

    性能：每只票先算一条 `log1p` 前缀和，窗口涨幅 = `exp(差值) − 1`。直接按窗口重乘
    的话，全池 × 5 年 × 3 窗口约 1800 万次乘 250 项，跑不完。
    """
    positions = {day: index for index, day in enumerate(all_dates)}
    raw: dict[int, dict[date, dict[str, float]]] = {
        window: defaultdict(dict) for window in windows
    }
    for code, bars in bars_by_code.items():
        gs = [positions[day] for day in bars.dates]
        logs = [0.0]
        for pct in bars.pct_chg.tolist():
            logs.append(logs[-1] + math.log1p(pct / 100.0))
        for i, g in enumerate(gs):
            for window in windows:
                start_g = g - window + 1
                if start_g < 0:
                    continue
                lo = bisect_left(gs, start_g)
                if i - lo + 1 < int(window * rps_rules.MIN_COVERAGE):
                    continue
                value = round((math.exp(logs[i + 1] - logs[lo]) - 1) * 100, 2)
                raw[window][bars.dates[i]][code] = value

    tables: dict[int, dict[date, dict[str, float]]] = {}
    for window, days in raw.items():
        table: dict[date, dict[str, float]] = {}
        for day, values in days.items():
            ordered = sorted(values.values())
            table[day] = {
                code: rps_rules.mid_rank(ordered, value)
                for code, value in values.items()
            }
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


def _filter_rps_any_90(code: str, bars: Bars, i: int, ctx: dict) -> bool:
    """「一线红」：RPS50 / RPS120 / RPS250 任意一条 ≥90。

    陶博士的用法是把它叠在形态上（尤其是口袋支点），只看「有没有一条线是红的」。
    """
    day = bars.dates[i]
    for window in (50, 120, 250):
        value = _rps_lookup(ctx, window, code, day)
        if value is not None and value >= RPS_TOP:
            return True
    return False


FILTER_CHECKS["rps_any_90"] = _filter_rps_any_90


#: 趋势模板第 8 条的门槛（RS ≥ 70）。判据本体在 `app.services.trend`（页面与回测共用）。
TREND_RS_MIN = trend_rules.TREND_RS_MIN


def _filter_trend_template(code: str, bars: Bars, i: int, ctx: dict) -> bool:
    """Minervini 趋势模板（8 条）—— 严格转发到 `app.services.trend.passes`。

    2026-10-24 从本文件挪进 `services/`：选股池的「趋势模板」开关要用同一条判据，
    两处各写一份必然漂移（`patterns.is_st` 那次就是教训）。
    """
    return trend_rules.passes(bars, i, _rps_lookup(ctx, 250, code, bars.dates[i]))


FILTER_CHECKS["trend_template"] = _filter_trend_template


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
        ctx["rps"] = _rps_context(bars_by_code, windows, all_dates)
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

    __slots__ = (
        "returns",
        "bench",
        "counts",
        "months",
        "samples",
        "total",
        "delayed",
        "no_entry",
        "exit_rolled",
        "exit_missing",
        "samples_rolled",
        "samples_missing",
    )

    def __init__(self, horizons: int = len(HORIZONS)) -> None:
        self.returns = [array("d") for _ in range(horizons)]
        self.bench = [0.0] * horizons
        self.counts = [0] * horizons
        self.months: Counter[str] = Counter()
        self.samples: deque[dict] = deque(maxlen=40)
        #: 加入统计的信号条数（与「某档有效样本」是两回事：退不出来 / 数据不够的档会少）
        self.total = 0
        #: 入场日晚于正常 T+1（停牌后复牌才买到）的条数
        self.delayed = 0
        #: 出了信号但拿不到入场价（无法成交）的条数 —— 由调用方累加
        self.no_entry = 0
        #: 各档「计划退出日停牌、顺延成交」与「退不出来」的条数
        self.exit_rolled = [0] * horizons
        self.exit_missing = [0] * horizons
        #: 至少有一档顺延 / 至少有一档退不出来的信号条数
        self.samples_rolled = 0
        self.samples_missing = 0

    def add(
        self,
        outcomes: list[Exit],
        bench_means: list[float],
        month: str,
        sample: dict | None = None,
        *,
        delayed: bool = False,
    ) -> None:
        # `value is None` = 该档退不出来（数据末端 / 长期停牌），**该档不计样本**：
        # 原来是静默丢掉，现在单独计数并在报告里交代（见 `Exit`）。
        rolled_any = missing_any = False
        for index, outcome in enumerate(outcomes):
            if outcome.value is None:
                self.exit_missing[index] += 1
                missing_any = True
                continue
            self.returns[index].append(outcome.value)
            self.bench[index] += bench_means[index]
            self.counts[index] += 1
            if outcome.rolled:
                self.exit_rolled[index] += 1
                rolled_any = True
        self.total += 1
        self.months[month] += 1
        if delayed:
            self.delayed += 1
        if rolled_any:
            self.samples_rolled += 1
        if missing_any:
            self.samples_missing += 1
        if sample is not None:
            self.samples.append(sample)

    # -- 报告用 ----------------------------------------------------------

    @property
    def size(self) -> int:
        return self.total

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


def _st_rows(grouped: dict[str, list[dict]]) -> tuple[dict[str, set[date]], dict]:
    """按**交易日**判 ST：`{代码: {该日为 ST 的日期}}` + 一份统计。

    2026-10-31 整改：原来用「最后一根 K 线的名称」把整段历史剔掉 —— 那会把
    「后天才变 ST」的票在变 ST 之前那段也一起剔掉，也把「现在是 ST」当成「过去一直
    是 ST」。现在逐行看 `stock_daily.name`：**某天是 ST 就只有那天不参与**，摘帽后
    符合条件可以重新进来。

    ⚠️ 历史名称的可靠性有限：`history.db` 是后来回补的，同一只票所有行可能都写着
    **当前**名称。这种情况无法还原逐日状态 —— 只能统计出来（`constant_st`）并在
    报告里披露，**不伪造**。
    """
    st_days: dict[str, set[date]] = {}
    no_name = 0
    constant_st = 0
    for code, records in grouped.items():
        names = {str(record.get("name") or "").strip() for record in records}
        no_name += sum(1 for record in records if not str(record.get("name") or "").strip())
        days = {record["date"] for record in records if is_st(record.get("name"))}
        if days:
            st_days[code] = days
            # 全历史名称只有一个值、且带 ST → 无法区分「一直 ST」与「后来才 ST」
            if len(names) == 1:
                constant_st += 1
    return st_days, {
        "codes": len(st_days),
        "rows": sum(len(days) for days in st_days.values()),
        "no_name": no_name,
        "constant_st": constant_st,
    }


def _candidate_sets(bars_by_code: dict[str, Bars]) -> dict[date, set[str]]:
    """复现生产 `scan_patterns._candidate_codes`：创业板 + 科创板里「今天出过线」的票。

    口径与生产逐条一致（当日冲高 4.5%~22%、收盘涨跌 −3%~20.5%、板块 300/301/302/688/689），
    只用**当天及之前**的数据，不碰未来。只有「明天盯 / 明天预案」用它，其余四个池子
    不限候选池（理由见 `scan_patterns` 模块顶部那张表）。
    """
    out: dict[date, set[str]] = defaultdict(set)
    for code, bars in bars_by_code.items():
        if not is_wudao_board(code):
            continue
        for t in range(1, len(bars)):
            pct = float(bars.pct_chg[t])
            if not (WUDAO_CAND_MIN_PCT <= pct <= WUDAO_CAND_MAX_PCT):
                continue
            close = float(bars.close[t])
            high = float(bars.high[t])
            denominator = 1.0 + pct / 100.0
            if denominator <= 0 or close <= 0 or high <= 0:
                continue
            # 与生产同一个反推口径（昨收 = 今收 / (1+涨跌幅)），不另查一根 K 线
            prev_close = close / denominator
            if prev_close <= 0:
                continue
            high_pct = (high / prev_close - 1.0) * 100.0
            if WUDAO_CAND_MIN_HIGH <= high_pct <= WUDAO_CAND_MAX_HIGH:
                out[bars.dates[t]].add(code)
    return out


def _benchmark(
    bars_by_code: dict[str, Bars],
    grouped: dict[str, list[dict]],
    all_dates: list[date],
    global_pos: dict[date, int],
    mode: str,
    *,
    skip_unbuyable: bool,
    st_days: dict[str, set[date]],
) -> tuple[dict[int, dict[date, float]], dict[int, dict[date, float]], dict]:
    """按**实际入场日**聚合的横截面基准：全池一份、悟道池（创业板+科创板）一份。

    2026-10-31 改：原来按**信号日** `day` 聚合 —— 样本因停牌晚两天入场时，基准却是
    「信号日入场」那批票的收益，两组不是同一段时间。现在策略组哪天入场就拿哪天入场的
    全池平均作基准（同起点、同终点、同持有期）。

    口径与样本完全一致：ST 按交易日剔、`--skip-unbuyable` 同口径剔、入场日晚于 T+1
    的票按**它的实际入场日**计入对应那一天，不并到信号日。
    """
    hold_from_entry = mode == ENTRY_NEXT_OPEN
    sums = {h: defaultdict(float) for h in HORIZONS}
    counts = {h: defaultdict(int) for h in HORIZONS}
    wsums = {h: defaultdict(float) for h in HORIZONS}
    wcounts = {h: defaultdict(int) for h in HORIZONS}
    stats: Counter[str] = Counter()

    for code, bars in bars_by_code.items():
        date_index = {day: i for i, day in enumerate(bars.dates)}
        code_st = st_days.get(code, set())
        wudao = is_wudao_board(code)
        for i, day in enumerate(bars.dates):
            if day in code_st:
                stats["st"] += 1
                continue
            entry = _entry(bars, i, mode)
            if entry is None:
                stats["no_entry"] += 1
                continue
            if entry.date in code_st:
                # 信号日正常、成交日已经是 ST（极端但会出现）：同样不参与
                stats["st"] += 1
                continue
            if skip_unbuyable and _unbuyable(
                bars, i, mode, limit_rules.limit_pct(code, grouped[code][i].get("name"))
            ):
                stats["unbuyable"] += 1
                continue
            g_entry = global_pos[entry.date]
            if g_entry > global_pos[day] + (0 if mode == ENTRY_CLOSE else 1):
                stats["delayed"] += 1
            values = _forward_values(
                bars,
                date_index,
                all_dates,
                entry,
                g_entry=g_entry,
                hold_from_entry=hold_from_entry,
            )
            for index, value in enumerate(values):
                if value is None:
                    continue
                horizon = HORIZONS[index]
                sums[horizon][entry.date] += value
                counts[horizon][entry.date] += 1
                if wudao:
                    wsums[horizon][entry.date] += value
                    wcounts[horizon][entry.date] += 1

    def _means(total: dict, number: dict) -> dict[int, dict[date, float]]:
        return {
            horizon: {
                day: total[horizon][day] / count
                for day, count in number[horizon].items()
                if count
            }
            for horizon in HORIZONS
        }

    return _means(sums, counts), _means(wsums, wcounts), dict(stats)


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
            "prod = 线上落库闸门（主板 + 创业板 + 科创板，不含北交所），且**按形态**"
            "再套生产边界：悟道六池 + 金叉大阳仍只收创业板 + 科创板"
            "（`pattern_board_allowed`）；"
            "wudao = 全部形态都只扫创业板 + 科创板（`is_wudao_board`，20cm 口径，"
            "`patterns.py` 各段旧回测表就是这个口径）；"
            "all = 研究口径：池子里全部（含北交所），**不等于生产口径**，只用于形态间相对比较"
        ),
    )
    parser.add_argument(
        "--production-parity",
        action="store_true",
        help=(
            "尽量复现生产扫描的全部可获得条件：按形态分板块边界、ST 按交易日剔、"
            "「明天盯 / 明天预案」限当日候选池、华宝早期排掉同票已命中的样板/启动。"
            "总市值 2000 亿那道**无法复现**（库里没有历史市值），会单独披露")
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
        "--entry",
        default=ENTRY_CLOSE,
        choices=ENTRY_MODES,
        help=(
            "入场价口径：close = 信号日收盘（默认，历史数字都是这个口径）；"
            "next_open = 次日开盘（打板 / 突破类必须用，信号收盘后才成立）"
        ),
    )
    parser.add_argument(
        "--skip-unbuyable",
        action="store_true",
        help=(
            "剔除入场价已经在涨停价上的信号（竞价即封 / 一字板，开盘价买不到）。"
            "基准表同口径剔除。打板类必须开，否则涨停那一段收益是假的"
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
    if args.entry == ENTRY_NEXT_OPEN:
        logger.info(
            "入场口径：**次日开盘**（close[t+h] / open[t+1] − 1），与默认的收盘入场不可直接比较"
        )
    if args.skip_unbuyable:
        logger.info("剔除买不到的信号：入场价 = 涨停价（竞价即封 / 一字板），基准表同口径")

    from app.jobs.collect_universe import load_codes

    codes = load_codes()
    if args.board == "wudao":
        codes = [code for code in codes if is_wudao_board(code)]
        logger.info("只扫创业板 + 科创板：%d 只（与悟道六池同板块口径）", len(codes))
    elif args.board == "prod":
        # 只保留落库闸门允许的代码；**形态级**的再收窄（悟道六池 + 金叉大阳仍只收 20cm）
        # 在逐条命中的循环里做 —— 两处用同一个 `pattern_board_allowed`。
        codes = [code for code in codes if pattern_board_allowed("", code)]
        logger.info("按线上落库闸门：%d 只（主板 + 创业板 + 科创板，不含北交所）", len(codes))
    else:
        logger.warning(
            "--board all 是**研究口径**（含北交所、不限形态），数字不能当生产效果读；"
            "要与生产对照请加 --board prod --production-parity"
        )
    if args.production_parity and args.board != "prod":
        logger.warning(
            "--production-parity 已开但 --board=%s：候选池按生产口径只收创业板 + 科创板，"
            "板块仍按 %s —— 想完整对齐请同时给 --board prod",
            args.board,
            args.board,
        )
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

    # 数据缺口要单独说：没有日线的票**不是**「没有信号」，而是这批票根本没进回测。
    missing = set(codes) - set(grouped)
    if missing:
        logger.warning(
            "%d 只票在库/窗口里没有任何可用日线（数据缺口，不是「没有信号」）：%s%s",
            len(missing),
            ", ".join(sorted(missing)[:8]),
            " …" if len(missing) > 8 else "",
        )
    short = sum(1 for records in grouped.values() if len(records) < MIN_BARS)
    if short:
        logger.warning("%d 只票的历史不足 %d 根，不参与判定", short, MIN_BARS)

    # **ST 按交易日剔**（2026-10-31 整改）。判据仍是线上同一个 `patterns.is_st`，
    # 但作用域从「整段」改成「那一天」：某天名称带 ST 就只有那天不参与，摘帽后可以
    # 重新进来。样本与基准共用这一份 `st_days`，两组仍是对照关系。
    # ⚠️ 历史名称可能整段都是当前名称（`history.db` 是回补的），那种情况无法还原，
    # 只能统计并在日志/报告里披露 —— 不伪造逐日状态。
    st_days, st_stats = _st_rows(grouped)
    logger.info(
        "ST 按交易日剔除：%d 只票 / %d 个(票,日)（名称缺失 %d 行；"
        "%d 只票全历史名称恒为当前名、无法区分历史 ST 状态）",
        st_stats["codes"],
        st_stats["rows"],
        st_stats["no_name"],
        st_stats["constant_st"],
    )

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
    if _DRAFT_KEYS & set(targets):
        logger.info(
            "★ 其中 %d 个是**实验形态**（未注册进生产）：%s",
            len(_DRAFT_KEYS & set(targets)),
            ", ".join(sorted(_DRAFT_KEYS & set(targets))),
        )

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

    # 基准：**按实际入场日**聚合的横截面平均（策略组哪天入场，就比哪一天入场的基准）。
    #
    # ⚠️ **分母就是参与本次回测的那批票**（`bars_by_code`）—— `--board wudao` 时基准是
    # 「创业板 + 科创板等权」。另算一份「悟道池」基准（只有 20cm 那两块），供悟道六池与
    # 金叉大阳在生产口径下使用：它们的生产边界本来就只有那两块。
    #
    # 2026-10-31 改：原来按**信号日**聚合，样本因停牌晚两天入场时，基准仍是「信号日入场」
    # 那批票的收益 —— 两组不是同一段时间。现在按下标 `entry.date` 存，策略与基准
    # 同起点、同终点、同持有期；停牌延迟入场的票计入**它自己的入场日**，不并到信号日。
    #
    # 只累加 sum 与 count（不保留每天的收益列表）：5 年 × 全池会攒下上千万个浮点数。
    bench_all, bench_wudao, bench_stats = _benchmark(
        bars_by_code,
        grouped,
        all_dates,
        global_pos,
        args.entry,
        skip_unbuyable=args.skip_unbuyable,
        st_days=st_days,
    )
    logger.info(
        "基准表：%d 个入场日（全池）/ %d 个（创业板+科创板）；"
        "基准侧 ST 剔除 %d 个(票,日)、延迟入场 %d 个、无法成交 %d 个",
        len(bench_all[HORIZONS[0]]),
        len(bench_wudao[HORIZONS[0]]),
        bench_stats.get("st", 0),
        bench_stats.get("delayed", 0),
        bench_stats.get("no_entry", 0),
    )
    # 悟道六池 + 金叉大阳的生产基准是「创业板 + 科创板」那一份
    use_wudao_bench = args.production_parity or args.board in ("prod", "wudao")

    def _bench_table(key: str) -> dict[int, dict[date, float]]:
        return bench_wudao if use_wudao_bench and key in WUDAO_POOL_KEYS else bench_all

    tallies = {key: Tally() for key in targets}
    # 各种「信号被挡掉」的原因分开计数：报告里要交代，否则「信号少」看不出原因
    unbuyable: Counter[str] = Counter()
    no_entry: Counter[str] = Counter()
    st_entry: Counter[str] = Counter()
    board_dropped: Counter[str] = Counter()
    candidate_dropped: Counter[str] = Counter()
    huabao_dropped: Counter[str] = Counter()
    last_hit: dict[tuple[str, str], int] = {}
    started = time.monotonic()
    hold_from_entry = args.entry == ENTRY_NEXT_OPEN
    prod_board_gate = args.board == "prod"
    # 候选池只对「明天盯 / 明天预案」生效；其余四个池子不限（生产就是这样）
    candidates = _candidate_sets(bars_by_code) if args.production_parity else {}

    for done, (code, bars) in enumerate(bars_by_code.items(), start=1):
        n = len(bars)
        date_index = {day: i for i, day in enumerate(bars.dates)}
        code_st = st_days.get(code, set())
        for t in range(MIN_BARS, n):
            day = bars.dates[t]
            g = global_pos[day]
            if g + max(HORIZONS) >= len(all_dates):
                # 命中日之后不足 `max(HORIZONS)` 个交易日了。日期升序，可以直接停这一只票。
                break
            if day in code_st:
                continue
            if rps_extra is not None:
                rps50 = _rps_lookup(filter_ctx, 50, code, day)
                if (
                    rps50 is not None
                    and rps50 >= RPS_TOP
                    and t - last_rps.get(code, -10**6) >= DEDUP_DAYS
                ):
                    entry_rps = _entry(bars, t, args.entry)
                    # 买不到的信号连去重位都不占：后面几天若能成交，仍该算样本
                    if entry_rps is not None and entry_rps.price > 0 and not (
                        args.skip_unbuyable
                        and _unbuyable(
                            bars,
                            t,
                            args.entry,
                            limit_rules.limit_pct(code, grouped[code][t].get("name")),
                        )
                    ):
                        last_rps[code] = t
                        g_entry = global_pos[entry_rps.date]
                        rps_extra.add(
                            _forward_outcomes(
                                bars,
                                date_index,
                                all_dates,
                                entry_rps,
                                g_entry=g_entry,
                                hold_from_entry=hold_from_entry,
                            ),
                            [bench_all[h].get(entry_rps.date, 0.0) for h in HORIZONS],
                            str(day)[:7],
                            delayed=g_entry > g + (1 if hold_from_entry else 0),
                        )
            sample: Bars | None = None
            # 同一个 (票, 交易日) 的蜡烛形态只判一次：多个形态同日命中时共用
            candle_flags: tuple[str, ...] | None = None
            for key, target in targets.items():
                # 板块关：prod 口径下按形态再收一次（悟道六池 + 金叉大阳只收 20cm）
                if prod_board_gate and not pattern_board_allowed(key, code):
                    board_dropped[key] += 1
                    continue
                if (
                    args.production_parity
                    and key in WUDAO_CAND_KEYS
                    and code not in candidates.get(day, ())
                ):
                    candidate_dropped[key] += 1
                    continue
                screen = target["screen"]
                if screen is not None and not screen(bars, t):
                    continue
                if key.startswith("zgnb_"):
                    # 知行B1：指标只依赖 `<= t`，整条序列算一次、按根取值即可
                    # （`patterns.zgb1_at` 与「截断后再判」有对照测试）
                    hit = _as_hit(zgb1_at(bars, key, t))
                    if candle_tallies and sample is None:
                        sample = _upto(bars, t + 1)
                else:
                    if sample is None:
                        sample = _upto(bars, t + 1)
                    hit = _as_hit(_BY_KEY[key].detect(sample))
                if hit is None:
                    continue
                # 同一只票 DEDUP_DAYS 个交易日内只留第一个信号：一波行情里连着几天
                # 都满足条件，会被当成几个「独立样本」，把统计显著性算得虚高
                if t - last_hit.get((key, code), -10**6) < DEDUP_DAYS:
                    continue
                entry = _entry(bars, t, args.entry)
                if entry is None:
                    # 拿不到可成交的开盘价（序列末尾）—— 记「无法成交」，不静默丢
                    no_entry[key] += 1
                    continue
                if entry.date in code_st:
                    # 信号日正常、成交日已是 ST：与基准同规则，剔除并计数
                    st_entry[key] += 1
                    continue
                # 买不到的信号不占去重位：20 日窗口内后面能成交的那次仍该算
                if args.skip_unbuyable and _unbuyable(
                    bars,
                    t,
                    args.entry,
                    limit_rules.limit_pct(code, grouped[code][t].get("name")),
                ):
                    unbuyable[key] += 1
                    continue
                # 华宝早期：同一只票当天已经出过样板 / 启动就不收（生产 `_signal_allowed`）
                if args.production_parity and key == "huabao_early":
                    flagged = any(
                        _as_hit(_BY_KEY[other].detect(sample)) is not None
                        for other in ("wudao_sample", "wudao_start")
                    )
                    if flagged:
                        huabao_dropped[key] += 1
                        continue
                g_entry = global_pos[entry.date]
                delayed = g_entry > g + (1 if hold_from_entry else 0)
                last_hit[(key, code)] = t
                outcomes = _forward_outcomes(
                    bars,
                    date_index,
                    all_dates,
                    entry,
                    g_entry=g_entry,
                    hold_from_entry=hold_from_entry,
                )
                table = _bench_table(key)
                bench_means = [table[h].get(entry.date, 0.0) for h in HORIZONS]
                tallies[key].add(
                    outcomes,
                    bench_means,
                    str(day)[:7],
                    {
                        "code": code,
                        # 用**当天**的名字（不是最后一根），与 ST 判定同源
                        "name": grouped[code][t].get("name") or "",
                        "date": day,
                        "entry_date": entry.date,
                        "delayed": delayed,
                        "hit": hit,
                        "outcomes": {item.horizon: item.value for item in outcomes},
                        "exits": {item.horizon: item.actual for item in outcomes},
                    },
                    delayed=delayed,
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
                            outcomes, bench_means, str(day)[:7]
                        )
                if filter_tallies:
                    for name in filter_names:
                        bucket = (
                            filter_tallies
                            if FILTER_CHECKS[name](code, bars, t, filter_ctx)
                            else filter_base_tallies
                        )
                        bucket[(key, name)].add(outcomes, bench_means, str(day)[:7])
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
    if unbuyable:
        dropped = sum(unbuyable.values())
        logger.info(
            "其中 %d 条因「入场价 = 涨停价」被剔除（占 %.1f%%：%s）",
            dropped,
            dropped / (total + dropped) * 100,
            ", ".join(f"{key}×{count}" for key, count in unbuyable.most_common()),
        )
    if no_entry:
        logger.warning(
            "%d 条信号拿不到可成交的开盘价（序列末尾，记「无法成交」）：%s",
            sum(no_entry.values()),
            ", ".join(f"{key}×{count}" for key, count in no_entry.most_common()),
        )
    if st_entry:
        logger.info(
            "成交日已是 ST（信号日不是）挡掉 %d 条：%s",
            sum(st_entry.values()),
            ", ".join(f"{key}×{count}" for key, count in st_entry.most_common()),
        )
    if board_dropped:
        logger.info(
            "按形态板块边界挡掉：%s",
            ", ".join(f"{key}×{count}" for key, count in board_dropped.most_common()),
        )
    if candidate_dropped:
        logger.info(
            "不在当日候选池（仅明天盯 / 明天预案）挡掉：%s",
            ", ".join(f"{key}×{count}" for key, count in candidate_dropped.most_common()),
        )
    if huabao_dropped:
        logger.info(
            "华宝早期：同票同日已出样板/启动，挡掉 %d 条", sum(huabao_dropped.values())
        )
    if total == 0:
        logger.warning(
            "一个都没命中 —— 条件太严或数据太短（不是「今天没选出票」，是本次回测没有样本）"
        )
        return 1

    if args.entry == ENTRY_NEXT_OPEN:
        # 口径不同不能和文档里的历史数字混读，报告开头点明
        print(
            "\n★ 入场 = 次日开盘（open[t+1]）；历史结论均为信号日收盘入场，两者不可直接比较。"
            "\n  停牌导致入场晚于 T+1 时，持有期从**实际成交日**起算，计划退出日停牌则顺延到复牌首日收盘。"
        )
    else:
        print(
            "\n★ 入场 = 信号日收盘价（**理论口径**：形态在收盘后才成立，现实中最早只能次日开盘成交）。"
            "\n  这一口径只用于与历史数字对照，不能当作可交易收益；实盘模拟请用 --entry next_open。"
        )
    if args.skip_unbuyable:
        print("★ 已剔除入场价已在涨停价上的信号（买不到），基准表同口径")
    if args.production_parity:
        print(
            "★ 生产口径（--production-parity）：按形态分板块边界 + 当日候选池（仅明天盯/明天预案）"
            " + 华宝早期互斥。**总市值 2000 亿未复现**（库里没有历史市值），见报告末「数据限制」。"
        )

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
    _report_data_limits(args, grouped, st_stats)
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
        f"{'基准均值':>10} {'超额':>9} {'顺延':>6} {'无法退出':>8} {'最好':>9} {'最差':>9}"
    )
    print("-" * 100)
    for index, horizon in enumerate(HORIZONS):
        mean, median, win, best, worst = tally.stats(index)
        base = tally.bench_mean(index)
        print(
            f"{horizon:>4}日 {tally.counts[index]:>6} {mean * 100:>8.2f}% {median * 100:>8.2f}% "
            f"{win * 100:>6.1f}% {base * 100:>9.2f}% {(mean - base) * 100:>8.2f}% "
            f"{tally.exit_rolled[index]:>6} {tally.exit_missing[index]:>8} "
            f"{best * 100:>8.1f}% {worst * 100:>8.1f}%"
        )
    print("=" * 100)

    dropped = tally.no_entry
    print()
    print(
        f"成交与退出：延迟入场 {tally.delayed} 条（停牌后复牌才买到）；"
        f"无法成交 {dropped} 条（占 {dropped / (tally.size + dropped) * 100 if tally.size + dropped else 0:.1f}%）；"
        f"至少一档无法退出 {tally.samples_missing} 条；至少一档顺延 {tally.samples_rolled} 条"
    )
    print("（无法退出 = 数据末端或长期停牌，该档不计样本；顺延 = 计划退出日停牌，按复牌首日收盘退出）")

    print()
    print("--- 信号按月分布（全挤在某一两个月 = 可能只在特定行情下成立）---")
    print("  " + "   ".join(f"{m}: {c}" for m, c in sorted(tally.months.items())))

    if samples > 0:
        print()
        print(f"--- 命中明细（按扫描顺序最后 {samples} 条，便于逐个人工核对）---")
        # 注意别写成 rows[-samples:]：samples=0 时那是 [-0:] 等于整段，会把全部明细打出来
        for item in sorted(tally.samples, key=lambda entry: entry["date"])[-samples:]:
            out = item["outcomes"]
            flag = " ⚠延迟" if item.get("delayed") else ""
            print(
                f"  {item['date']} {item['code']} {str(item.get('name') or ''):<6} "
                f"分数{item['hit'].get('score', 0):>5.1f} 入场{item['entry_date']}{flag}"
                f"  → 1日{_fmt_pct(out.get(1))} 3日{_fmt_pct(out.get(3))} "
                f"5日{_fmt_pct(out.get(5))} 10日{_fmt_pct(out.get(10))} "
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

    idx1, idx3, idx5, idx10, idx20, idx60 = (
        HORIZONS.index(h) for h in (1, 3, 5, 10, 20, 60)
    )

    # 按 10 日超额排序：做这张表的全部意义就是「一眼看出哪些形态值得留」
    rows.sort(
        key=lambda item: (item["excess"][idx10] if item["excess"] else -99), reverse=True
    )

    print()
    print("=" * 132)
    print(
        f"{'分组':<12}{'形态':<18}{'信号':>7}{'1日超额':>10}{'3日超额':>10}"
        f"{'5日超额':>10}{'10日超额':>10}"
        f"{'20日超额':>10}{'60日超额':>10}{'10日胜率':>9}{'10日中位':>10}"
    )
    print("-" * 132)
    for item in rows:
        meta, tally = item["meta"], item["tally"]
        if item["excess"] is None:
            print(
                f"{meta.group:<12}{meta.name:<18}{0:>7}{'—':>10}{'—':>10}{'—':>10}"
                f"{'—':>10}{'—':>10}{'—':>10}{'—':>9}{'—':>10}"
            )
            continue
        excess = item["excess"]
        median10 = tally.stats(idx10)[1]
        win10 = tally.stats(idx10)[2]
        print(
            f"{meta.group:<12}{meta.name:<18}{tally.size:>7}"
            f"{excess[idx1] * 100:>9.2f}%{excess[idx3] * 100:>9.2f}%"
            f"{excess[idx5] * 100:>9.2f}%{excess[idx10] * 100:>9.2f}%"
            f"{excess[idx20] * 100:>9.2f}%"
            f"{excess[idx60] * 100:>9.2f}%{win10 * 100:>8.1f}%{median10 * 100:>9.2f}%"
        )
    print("=" * 132)
    print(
        "（超额 = 命中组平均收益 − 同**实际入场日**全池平均收益，单位 %，未年化；"
        "「同一天」指同一天入场、同一天退出）"
    )

    print()
    print("--- 分档明细（绝对收益，%；基准 = 同实际入场日的全池平均）---")
    for item in rows:
        tally = item["tally"]
        if not tally.size:
            continue
        print(f"  {item['meta'].name}")
        for index, horizon in enumerate(HORIZONS):
            mean, median, win, _best, _worst = tally.stats(index)
            base = tally.bench_mean(index)
            print(
                f"    {horizon:>3}日  样本{tally.counts[index]:>6}  均值{mean * 100:>7.2f}%"
                f"  中位{median * 100:>7.2f}%  胜率{win * 100:>5.1f}%"
                f"  基准{base * 100:>7.2f}%  超额{(mean - base) * 100:>+6.2f}%"
            )

    print()
    print("--- 成交与退出（无法成交 = 拿不到可成交开盘价；无法退出 = 数据末端/长期停牌）---")
    print(
        f"{'形态':<20}{'信号':>7}{'延迟入场':>9}{'无法成交':>9}{'无法成交率':>11}"
        f"{'无法退出':>9}{'退出顺延':>9}"
    )
    print("-" * 76)
    for item in rows:
        tally = item["tally"]
        if not tally.size:
            continue
        rate = tally.no_entry / (tally.size + tally.no_entry) * 100 if tally.size + tally.no_entry else 0.0
        print(
            f"{item['meta'].name:<20}{tally.size:>7}{tally.delayed:>9}{tally.no_entry:>9}"
            f"{rate:>10.1f}%{tally.samples_missing:>9}{tally.samples_rolled:>9}"
        )
    print("=" * 76)

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
    index5 = HORIZONS.index(5)
    index10 = HORIZONS.index(10)
    index20 = HORIZONS.index(20)
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
                "excess5": tally.stats(index5)[0] - tally.bench_mean(index5),
                "excess10": excess,
                "excess20": tally.stats(index20)[0] - tally.bench_mean(index20),
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
    index5 = HORIZONS.index(5)
    index10 = HORIZONS.index(10)
    index20 = HORIZONS.index(20)
    index60 = HORIZONS.index(60)
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
                "excess5": tally.stats(index5)[0] - tally.bench_mean(index5),
                "excess10": excess,
                "excess20": tally.stats(index20)[0] - tally.bench_mean(index20),
                # 60 日要命中后再走 60 个交易日，窗口末端那批信号没有样本 ——
                # 为 0 时打印 `—`，不能当「超额 0」看
                "excess60": (
                    tally.stats(index60)[0] - tally.bench_mean(index60)
                    if tally.counts[index60]
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


def _over_cap_now(grouped: dict[str, list[dict]]) -> set[str]:
    """当前总市值超过生产上限的代码（与本次样本池求交）。拿不到时返回空集。"""
    try:
        from app.jobs.scan_patterns import PATTERN_MAX_TOTAL_MV, _over_cap_codes

        return set(grouped) & _over_cap_codes(PATTERN_MAX_TOTAL_MV)
    except Exception:  # noqa: BLE001  披露用，查不到就不披露这一条
        return set()


def _report_data_limits(args, grouped: dict[str, list[dict]], st_stats: dict) -> None:
    """把**用现有数据修不掉**的偏差逐条列出来（任务书要求：有偏差必须披露）。

    这些不是 bug，是数据限制：列出来是为了避免把「回测跑出来的数字」当成
    「上生产能赚这么多」。任何一条能修的前提都是补数据，而不是改代码。
    """
    print()
    print("=" * 100)
    print("数据限制（决定这些数字能不能当「上生产能赚多少」）")
    print("-" * 100)
    print(
        f"0) 本次口径：board={args.board} / entry={args.entry} / "
        f"production_parity={args.production_parity} / HORIZONS={list(HORIZONS)}"
    )
    print("1) 幸存者偏差：股票池来自 load_codes() —— **当前**在池的票。退市 / 出池的票整段")
    print("   缺席，样本与基准同向偏乐观；未按历史交易日的真实上市状态还原股票池。")
    print("2) 总市值 2000 亿过滤**未复现**：库里没有历史市值（stock_daily 无 total_mv，")
    print("   stock_universe 只有当前快照）。不用今天的市值代替历史市值，也不伪造。")
    over = _over_cap_now(grouped)
    if over:
        print(
            f"   当前超线、且在本次样本池里的票 {len(over)} 只 —— 生产会把它们整段剔除，"
            "回测没有；含这些票的样本/基准数字都偏宽。"
        )
    print(
        f"3) ST 按交易日剔除 {st_stats['rows']} 个(票,日)（{st_stats['codes']} 只票）；"
        f"其中 {st_stats['constant_st']} 只票全历史名称恒为当前名称，"
    )
    print("   无法区分「一直 ST」与「后来才 ST」；名称缺失的行按非 ST 处理。")
    print(f"   名称缺失 {st_stats['no_name']} 行。")
    print("4) 成交假设：入场用当日开盘价、退出用收盘价，**未计**佣金 / 印花税 / 滑点 /")
    print("   冲击成本；涨停买不到只在 --skip-unbuyable 时剔除，跌停卖不出未建模。")
    print("5) 去重：同一只票 20 个交易日内的重复信号只留第一个（只是独立样本的近似）。")
    print("=" * 100)


if __name__ == "__main__":
    sys.exit(main())