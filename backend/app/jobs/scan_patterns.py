"""全市场形态扫描：读日线 → 复权 → 逐形态判定 → 落 `pattern_hit`。

**这一步对「通用形态」不花 iFinD 配额** —— 形态全在本地算，实测 3032 只 0.7 秒。
辉宾对齐悟道时，会对**池外候选**按需补近端日线（见 `_ensure_wudao_kline`），
那一小段才吃配额；候选**已有 ≥22 根连续 K 线、且最新一根就是当天**则 0 次调用
（两个条件缺一不可 —— 只有根数够但不含当天，就是拿旧 K 线出今天的信号，见 8.64）。

## 为什么整段重算而不是增量

形态看的是「最近 N 根 K 线的形状」，而 N 最长 249。今天补上一根新 K 线，
昨天那些票的形态可能全都变了（也可能没变）。增量更新得知道「哪些票的形状
被新数据影响了」，而这个问题没有便宜的解。整段重算是 0.7 秒，不值得为它做增量。

## 为什么先删后插

同一天重复扫描必须幂等 —— 手工补扫、定时任务撞车都会发生。所以按
`trade_date` 整段替换，而不是 upsert：**形态是会消失的**。昨天命中「平台突破」
的票今天可能已经跌回平台里，upsert 会把旧命中永久留在表里，榜单越看越假。

## 悟道各池的范围（2026-10-09 定稿）

**板块：六个池子都只收创业板 + 科创板**（`is_wudao_board`，2026-10-09 去掉主板；
边界变化的来历见那个元组的注释）。

**金叉大阳**（2026-10-10 加，用户给的通达信公式）**板块同上**（见 `WUDAO_BOARD_KEYS`），
候选池不限 —— 它的回测就是在 wudao 池上做的。它不是悟道家族的池子，所以**不进
`pattern_track.POOL_KEYS`**（成绩单不取它，那六个是「悟道之路」专属）。

**候选池（「今天出过线」）只对「明天盯 / 明天预案」用**，其余四个池子**不限候选池**
—— 这是按每个形态「它的票今天长得什么样」定的，别再统一收窄（10-08 收过一次，代价见下）：

| 形态 | 板块 | 候选池 | 为什么 |
| --- | --- | --- | --- |
| 明天盯 / 明天预案 | 创业板 + 科创板 | **「今天出过线」的票**（当日冲高幅度 ≥ 4.5%，见 `_candidate_codes`） | 这两个形态只看「今天 / 昨天有没有冲上去」 |
| 洗完可盯 / 华宝早期 / 洗后可盯 / 缩量洗盘中 | 创业板 + 科创板 | **不限**（板块过滤之后全收） | 它们的票今天往往**很安静**（洗盘、连阳初期，涨幅只有 1~3%）。拿「今天强势」当候选等于把它们全筛掉 —— 10-08 收窄后「缩量洗盘中」从 633 掉到 3、华宝早期从 190 掉到 6，丢的就是这批 |

- 候选**零请求**：直接读本地 `stock_daily` 当天的「最高价 / 昨收 − 1」，不再打东财快照
  或同花顺（原型的 `ak.stock_zh_a_spot_em()` 是 56 次翻页；我们原先的两条备源也各有
  配额与可用性问题，而且只给一页 100 行 —— 10-08 的「301636 被切在第 50 名」就是这么来的）。
  判定本来就用这份日线，池子与判定同源，没有口径差。
- 排序**按当日冲高幅度降序**（原先按「来源档 → 代码」，等于抽签：一只票会不会进池
  取决于它的代码大小）。
- 板块 = **创业板（300/301/302）+ 科创板（688/689）**：2026-09-29 一度只创业板、10-08 先
  放开主板、同日再放开科创板、**10-09 把主板去掉**。北交所（30cm）任何时候都不进池子。
- 池外候选缺 K 线时补近端（见 `_ensure_wudao_kline`：东财 → 腾讯 → iFinD）。
"""

import logging
import time
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import delete, func, select

from app.config import Settings, get_settings
from app.db import session_scope, upsert_many
from app.jobs.collect_universe import load_codes
from app.models import (
    CollectLog,
    PatternHit,
    StockBasic,
    StockDaily,
    StockUniverse,
    TradeCalendar,
)
# 东财/腾讯两条兜底线的抓取逻辑都抽到了 `sources/` —— 本地回测往回补多年历史
# （scripts/backfill_history.py）也要用它们。口径（不复权价 + 真实涨跌幅）只能有一份。
from app.sources.eastmoney import fetch_daily as _fetch_daily_eastmoney
from app.sources.tencent import fetch_daily as _fetch_tencent_daily
from app.services.patterns import (
    MIN_SCORE,
    WUDAO_MIN_BARS,
    Bars,
    build_bars,
    evaluate,
    is_st,
    usable_bar,
)
# 悟道六池的 key。借用成绩单那边那份（`pattern_track.POOL_KEYS` 就是这六个池子）——
# 板块闸门要卡的就是它们（外加金叉大阳，见 `WUDAO_BOARD_KEYS`），别各写一份、
# 免得加池子时漏掉一处。
from app.services.pattern_track import POOL_KEYS as WUDAO_POOL_KEYS
from app.sources.ifind import IfindError

logger = logging.getLogger(__name__)

# 扫描时每只票取多少根 K 线。要够 249 日新高用（最长窗口 + 1），再留一点余量
SCAN_BARS = 260

# 采集日志里的一类任务名
PATTERN_TASK = "patterns"

# 扫某一天之前，要求那一天在库里至少覆盖池子的这个比例（见 `_require_bars`）。
# 90% 是「明显没采全」的底线：正常日子缺失约 0.3%（停牌/次新），
# 而 2026-09-24 那种抽风是 9% —— 卡在 90% 能拦住整片缺，又不至于因为几只停牌票就不让扫。
_MIN_DAY_COVERAGE = 0.9

# ---- 悟道各池的口径。2026-10-09 定稿，改动前先读模块顶部的表 ----

# **只有这两个形态另限候选池**（要过板块关的形态见 `WUDAO_BOARD_KEYS` 与模块顶部）
WUDAO_CAND_KEYS = frozenset({"wudao_sample", "wudao_start"})

# 候选窗口（按**当日冲高幅度** = 最高价 / 昨收 − 1，单位 %）：
# 下界 = 「今天出过线」的下限；上界挡掉首日无涨跌幅限制的新股（实测能到 +653%）。
# 收盘涨幅那一档只用来挡掉「冲高很高、收盘大跌」的（那种票既做不了样板也做不了启动）。
WUDAO_CAND_MIN_HIGH = 4.5
WUDAO_CAND_MAX_HIGH = 22.0
WUDAO_CAND_MIN_PCT = -3.0
WUDAO_CAND_MAX_PCT = 20.5

# 候选**不设条数上限**（2026-10-08 放过一版 300 只的「保护」，随后删掉）：候选是按冲高
# 幅度降序取的，任何上限都从**尾部**切 —— 而尾部正是「冲高 7~12%、收盘回落」那一档，
# 也就是样板本尊。原型那个「前 50」就是这么把干净样板挡在门外的。窗口本身就是过滤，
# 多出来的只让 `_ensure_wudao_kline` 多查几次本地日线（它是按需补，不是逐只请求）。

# 池外候选补多少交易日日线（日历日粗算 ×1.5 在 sync_stock 内）
_WUDAO_SYNC_DAYS = 60


class _NoBars(Exception):
    """这一级**没有这只票的数据** —— 与「这条源挂了」是两回事。

    退市股、还没上市的代码、以及腾讯那边根本没有的代码都属于这一类。实测（2026-09-24）
    300038 数知退、300060 都是这种：东财/腾讯都没有它们的近端日线，iFinD 却会给一行
    只有收盘价、其余全空的行。**「没有数据」不该记进熔断计数** —— 几个退市股就能凑够
    3 次、把一整条源整轮关掉，那才是真的亏。
    """


# 悟道六池的板块：**创业板 + 科创板**（2026-10-09 用户要求把主板去掉，只剩这两块）。
#
# 2026-09-29 一度只创业板、10-08 先放开主板、同日再放开科创板，10-09 又收回主板 ——
# 现在的边界就是**创业板（300/301/302）+ 科创板（688/689）**，六个池子都用它。
#
# 为什么停在「20cm 的两块」：本套阈值是按 20cm 校准的 ——「太热」那两条（收盘涨幅 ≥12% /
# 冲高 ≥18%）与「封板排除」（收盘涨幅 ≥9.5% + 收在最高 + 几乎无上影）在主板（10cm）上
# 天生打不到（涨停才 +10%），去了主板等于用 10cm 的票去套 20cm 的规则。
#
# ⚠️ 北交所（4/8 开头，30cm）**明确不收**：它的涨跌幅与这两块都不同，要收得同时改
# 「太热」阈值与封板判据。
# ⚠️ 原型 `is_chinext` 只认 300/301，我们多留了 302（创业板新号段，如 302132 中航成飞）。
_WUDAO_BOARD_PREFIXES = (
    "300",
    "301",
    "302",
    "688",
    "689",
)


def is_wudao_board(code: str) -> bool:
    """创业板 + 科创板。「悟道六池」只收这两块（不含主板、不含北交所）。

    ⚠️ 名字故意不叫 `is_chinext` —— 这条边界改过好几轮（2026-09-29 只创业板、10-08 加了
    主板、同日又加科创板、10-09 把主板去掉），叫 `is_chinext` 会骗人。
    调用点：候选池构建（`_candidate_codes`）与落库闸门（`_in_wudao_pool`，六个池子都过）。
    """
    c = str(code or "").strip().zfill(6)
    return c.startswith(_WUDAO_BOARD_PREFIXES)


def already_scanned(trade_date: date) -> bool:
    """该交易日是否已经扫过。

    只用来在日志上留个记号并避免同一轮里重复打印，**不承担正确性职责** ——
    重扫是幂等的、只要 0.7 秒、还零配额，所以「多扫一次」没有代价。
    """
    return _scanned_marker(trade_date)


def _scanned_marker(trade_date: date) -> bool:
    with session_scope() as session:
        return bool(
            session.scalar(
                select(CollectLog.trade_date)
                .where(
                    CollectLog.trade_date == trade_date,
                    CollectLog.task == PATTERN_TASK,
                    CollectLog.status == "ok",
                )
                .limit(1)
            )
        )


def _record(
    trade_date: date, status: str, rows: int, message: str | None, cost: float | None = None
) -> None:
    with session_scope() as session:
        session.add(
            CollectLog(
                trade_date=trade_date,
                task=PATTERN_TASK,
                status=status,
                rows=rows,
                message=message,
                cost_seconds=cost,
            )
        )


def _load_bars(
    trade_date: date, codes: list[str] | set[str], *, label: str = "股票池"
) -> dict[str, list[dict]]:
    """取出最近 `SCAN_BARS` 个交易日内指定代码的日线，按代码分组。

    一定要显式给代码名单，不能把 `stock_daily` 里的票全拿来扫：库里还有一批
    **池外的涨停股**（见 8.22.4，每天只给它们补当天那一根）。它们的序列是断的 ——
    连着两天涨停才有两行相邻，其余日子是空的 —— 而形态引擎会把**相邻的行**
    当成**相邻的交易日**，等于喂进去一条带空洞的 K 线，正是这套引擎最怕的输入。

    辉宾池外候选必须先经 `_ensure_wudao_kline` 补成连续近端，再放进这份名单。
    """
    codes = list(codes)
    if not codes:
        raise IfindError(f"{label}为空，先建池或补候选")

    with session_scope() as session:
        dates = list(
            session.scalars(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date <= trade_date)
                .order_by(TradeCalendar.trade_date.desc())
                .limit(SCAN_BARS)
            )
        )
        if not dates:
            raise IfindError(f"{trade_date} 之前没有交易日历数据")
        start = min(dates)

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
                StockDaily.trade_date <= trade_date,
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
        # 缺任一项（尤其停牌残行「只有收盘价、开高低为空」）都不能当一根 K 线：
        # `build_bars` 里 `float(None)` 会直接抛，整轮扫描失败。口径与 `load_phase` 一致。
        if not usable_bar(record):
            continue
        grouped[code].append(record)
    return grouped


def _candidate_codes(trade_date: date) -> set[str]:
    """「明天盯 / 明天预案」的候选：**创业板 + 科创板**里**今天出过线**的票。

    口径 = 当日**冲高幅度**（最高价 / 昨收 − 1）落在 `WUDAO_CAND_MIN_HIGH ~ MAX_HIGH`，
    且收盘涨幅在 `MIN_PCT ~ MAX_PCT`（挡掉「冲高很高、收盘大跌」与首日不限幅的新股）。
    窗口内**全部收**（不设条数上限，理由见常量那段注释），板块见 `is_wudao_board`。

    **零请求**：直接读本地 `stock_daily` —— 判定用的就是这份日线，池子与判定同源。
    原先这里要走「东财快照 → 同花顺」两条外部源（原型是 `stock_zh_a_spot_em()`、56 次
    翻页），既有配额与可用性问题、又只给一页约 100 行 —— 2026-10-08 那次「301636 正好被
    切在第 50 名」就是这么来的；而且排序按「来源档 → 代码」，等于抽签。见模块顶部那张表。
    """
    with session_scope() as session:
        rows = session.execute(
            select(StockDaily.code, StockDaily.pct_chg, StockDaily.high, StockDaily.close)
            .where(StockDaily.trade_date == trade_date, StockDaily.pct_chg.is_not(None))
        ).all()

    scored: list[tuple[float, str]] = []
    for code, pct, high, close in rows:
        c = str(code).zfill(6)
        if not is_wudao_board(c):
            continue
        pct = float(pct)
        if not (WUDAO_CAND_MIN_PCT <= pct <= WUDAO_CAND_MAX_PCT):
            continue
        # 昨收由当日收盘与官方涨跌幅反推（比再查一根 K 线便宜，且与 pct_chg 同源）
        prev_close = float(close) / (1.0 + pct / 100.0)
        if prev_close <= 0 or high is None:
            continue
        high_pct = (float(high) / prev_close - 1.0) * 100.0
        if not (WUDAO_CAND_MIN_HIGH <= high_pct <= WUDAO_CAND_MAX_HIGH):
            continue
        scored.append((high_pct, c))

    scored.sort(key=lambda item: (-item[0], item[1]))
    codes = {c for _, c in scored}
    logger.info(
        "悟道候选（创业板+科创板 · 按冲高幅度降序）：窗口内 %d 只", len(codes)
    )
    return codes


#: 要过「只出创业板 + 科创板」这道板块关的形态。
#:
#: **刻意与 `WUDAO_POOL_KEYS` 分开**：那个元组还兼着「悟道成绩单取数」（见
#: `pattern_track.POOL_KEYS`），而这里只管板块关 —— 金叉大阳不是悟道家族的池子。
#:
#: 金叉大阳（2026-10-10 用户给的通达信公式）回测就是在 wudao 池上做的：4% 大阳在
#: 20cm 板和 10cm 主板不是同一件事，拿全市场跑会得到另一批票、数字也不代表生产
#: （见 `scripts/backtest_patterns.py --board` 的说明），所以把板块锁死。
WUDAO_BOARD_KEYS: tuple[str, ...] = (*WUDAO_POOL_KEYS, "ma_cross_big_yang")


def _in_wudao_pool(
    pattern: str, code: str, cands: set[str], huabao_skip: set[str]
) -> bool:
    """这条悟道信号允不允许落库。

    **六个池子（悟道家族）＋ 金叉大阳都先过板块关**：`is_wudao_board` = 创业板 + 科创板
    （2026-10-09 用户要求去掉主板，见那个元组的注释；金叉大阳见 `WUDAO_BOARD_KEYS`）。

    板块过了之后再看两道：
    - 「明天盯 / 明天预案」另外**限候选池**（今天冲高 ≥ `WUDAO_CAND_MIN_HIGH` 的票）；
    - 「洗完可盯 / 华宝早期 / 洗后可盯 / 缩量洗盘中」**不限候选池**（它们的票今天往往很安静，
      用「今天强势」当候选等于把它们全筛掉，见模块顶部那张表），
      华宝早期另外排掉同一只票上已经命中的样板/启动。
    """
    if pattern in WUDAO_BOARD_KEYS and not is_wudao_board(code):
        return False
    if pattern in WUDAO_CAND_KEYS:
        return code in cands
    if pattern == "huabao_early":
        return code not in huabao_skip
    return True


def _bar_counts(codes: set[str], trade_date: date, *, lookback: int = 40) -> dict[str, int]:
    """近端 lookback 个交易日内，各代码已有多少根日线。"""
    if not codes:
        return {}
    with session_scope() as session:
        dates = list(
            session.scalars(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date <= trade_date)
                .order_by(TradeCalendar.trade_date.desc())
                .limit(lookback)
            )
        )
        if not dates:
            return {}
        start = min(dates)
        rows = session.execute(
            select(StockDaily.code, func.count())
            .where(
                StockDaily.code.in_(codes),
                StockDaily.trade_date >= start,
                StockDaily.trade_date <= trade_date,
            )
            .group_by(StockDaily.code)
        ).all()
    return {str(code).zfill(6): int(n) for code, n in rows}


def _last_bars(codes: set[str]) -> dict[str, date]:
    """各代码在库里的**最后一根 K 线是哪天**（不限窗口）。

    用来判断这份日线是不是**停在过去** —— 池外候选、以及「当天那根没采到的池内票」，
    都会表现为「有几十根 K 线、但最新的那根不是今天」，见 8.64。

    ⚠️ 只数 `pct_chg` 非空的行，与 `_load_bars` 的口径一致：iFinD 对停牌日会给一行
    「只有收盘价、其余全空」的残行（`sync_stock` 那条路照写不误），它算不了一根 K 线，
    却会把「最新一根」顶到当天、让这类票逃过补采。
    """
    if not codes:
        return {}
    with session_scope() as session:
        rows = session.execute(
            select(StockDaily.code, func.max(StockDaily.trade_date))
            .where(StockDaily.code.in_(codes), StockDaily.pct_chg.is_not(None))
            .group_by(StockDaily.code)
        ).all()
    return {str(code).zfill(6): day for code, day in rows}


def _sync_stock_eastmoney(code: str, *, days: int = _WUDAO_SYNC_DAYS) -> int:
    """池外致富候选的日线兜底：东财不复权 K 线 → `stock_daily`。

    iFinD 401 / 配额紧张时走这条。只写近端 `days` 个日历日，够量比窗口即可。

    抓取与口径都在 `app.sources.eastmoney`（本地回测往回补历史也用它，见
    `scripts/backfill_history.py`）—— 口径只有一份，别在这里再写一遍。
    """
    rows = _fetch_daily_eastmoney(code, days=days)
    if not rows:
        # 「没有数据」（退市股、未上市代码）与「源挂了」是两回事，见 `_NoBars`
        raise _NoBars("东财无数据")
    with session_scope() as session:
        return upsert_many(session, StockDaily, rows)


def _sync_stock_tencent(code: str, *, days: int = _WUDAO_SYNC_DAYS) -> int:
    """池外候选补日线（第二级兜底）：腾讯 K 线 → `stock_daily`。**不占 iFinD 配额**。

    为什么加这一级：云端连不上东财直连（8.51.3），那一级必然失败，于是每天几十只候选
    全落到 iFinD 兜底（实测 2026-09-24：48 只 = 48 次调用）。腾讯这条线**云端可达**
    （2026-09-24 在服务器上实测过），把它插进链条就把那几十次配额全省下来。

    **口径对过账**（本机 + 云端各跑一遍 `scripts/probe_akshare_tx.py`）：腾讯「不复权」的
    开高低收 / 成交量(股) / 成交额(元) 与库里 iFinD 写的值**逐位相同**（成交量有 14 股的
    舍入差，腾讯是整百股）；唯一的单位差是 `turnover` —— 腾讯给**比例** 0.0015、
    本站库里是**百分数** 0.15，故 ×100。

    **涨跌幅必须由前复权序列算，不能用不复权收盘价比**：除权日不复权价会跳空，
    比出来是一个假的暴跌。腾讯的 `qfq` 相邻收盘之比才是真实收益率（与 iFinD 的
    `涨跌幅` 对账：48 行、差异 >0.02 的 0 行）。

    抓取与口径（不复权价 + 由前复权推涨跌幅 + `turnover` ×100）都在 `app.sources.tencent`
    —— 本地补多年历史（`scripts/backfill_history.py`）用的是同一个函数。
    这一段只负责查名字与写库，口径只能有一份。
    """
    end = date.today()
    # 多留两周：前复权序列的**第一行**没有前值、算不出涨跌幅，那一行会被丢掉，
    # 多取的这段保证丢完之后仍在 `days` 个交易日以上
    start = end - timedelta(days=int(days * 1.5) + 14)
    rows = _fetch_tencent_daily(code, start=start, end=end)
    if not rows:
        raise _NoBars(f"腾讯无 {code} 日线")

    with session_scope() as session:
        name = session.scalar(
            select(StockBasic.name).where(StockBasic.code == str(code).zfill(6))
        )
    for row in rows:
        row["name"] = name or str(code).zfill(6)
    with session_scope() as session:
        return upsert_many(session, StockDaily, rows)


def _sync_note(sync_info: dict) -> str:
    """采集日志里「补日线用了哪几级」那截说明；都没干活就不写。

    分来源列出来是为了**事后能看出哪一级在扛** —— 腾讯这一级的价值正是让 iFinD 归零，
    没有这个数就只能去翻 logger 才知道它到底生效没有。
    """
    parts = []
    if sync_info.get("via_eastmoney"):
        parts.append(f"东财 {sync_info['via_eastmoney']} 只")
    if sync_info.get("via_tencent"):
        parts.append(f"腾讯 {sync_info['via_tencent']} 只")
    if sync_info.get("via_ifind"):
        parts.append(f"iFinD {sync_info['via_ifind']} 只")
    if sync_info.get("skipped_quota"):
        parts.append(f"因 iFinD 上限跳过 {sync_info['skipped_quota']} 只")
    return (" / 补日线 " + "、".join(parts)) if parts else ""


def _ensure_wudao_kline(
    codes: set[str], trade_date: date, settings: Settings | None = None
) -> dict:
    """候选池外那几只补日线：东财 → 腾讯 → iFinD（三级，都免费在前、吃配额在后）。

    **什么时候要补**（两个判据，缺一不可）：近端根数不足 `WUDAO_MIN_BARS`，
    **或者最后一根 K 线不是 `trade_date` 那天**。后一条是 8.64 补上的：
    池外候选第一次补完就再也不刷新了（根数早就够），于是它们会一直拿几天前的
    K 线出「今天的信号」。

    ⚠️ **后一条只在「扫的就是库里最新的那个交易日」时才算**（2026-10-09 加）：
    它的本意是「别拿几天前的 K 线出**今天**的信号」，而**历史重扫**（用现在的判定把
    过去的日子重算一遍，见 §8.87.4）时，候选的最后一根本来就晚于那天 —— 照原判据
    会把**每一个候选**都当成「停在过去」，几百只票逐只去补（补回来的还都是库里已有的行）。
    所以历史重扫只保留「根数不够」那一条。

    **三道闸门**（后两道 2026-09-24 加，为同时省时间与配额；阈值见 `Settings` 的注释）：

    - **东财熔断**：连续失败 `wudao_em_breaker_failures` 次就本轮不再试它。
      云端连不上东财直连（1.7 / 8.51.3 记过），而这里是**逐只**补 —— 不熔断的话每只都要
      等一次约 10 秒的超时。实测 09-24：48 只候选白等约 8 分钟，且最终全部落到 iFinD。
    - **腾讯熔断**：同一个道理 —— 这条线也有可能整轮不通（接口变动 / 该机器不可达），
      不熔断就是 48 只 × 两次请求 × 超时。
    - **iFinD 上限**：每轮最多补 `wudao_ifind_fallback_max` 只，超出的**本轮不补**
      （当天就没有这些候选的形态信号）。这是刻意的「配额 ↔ 覆盖」取舍，不静默：
      跳过的只数进 `skipped_quota`，日志与采集日志里都会写出来。
      **有了腾讯这一级，它其实很少再触发** —— 只有在腾讯也整轮不通时才轮得到它顶上来。
    """
    settings = settings or get_settings()
    counts = _bar_counts(codes, trade_date)
    last = _last_bars(codes)
    # 两个判据：**根数不够**（池外新股，从没补过）与**停在过去**（补过、之后没再刷）。
    # 少了后一条就是 8.64 那个 bug：池外候选第一次补完就再也不刷新了，
    # 于是 09-24 那天用 09-22 的 K 线出信号，命中列表上的「最新价」是两天前的。
    # ⚠️ 后一条只在「扫最新交易日」时才算（历史重扫见 docstring）：
    with session_scope() as session:
        data_latest = session.scalar(select(func.max(StockDaily.trade_date)))
    fresh_scan = data_latest is not None and trade_date >= data_latest
    need = {
        c
        for c in codes
        if counts.get(c, 0) < WUDAO_MIN_BARS or (fresh_scan and last.get(c) != trade_date)
    }
    if not need:
        return {
            "synced": 0,
            "failed": 0,
            "needed": 0,
            "via_eastmoney": 0,
            "via_tencent": 0,
            "via_ifind": 0,
            "skipped_quota": 0,
        }

    return _sync_codes_three_levels(need, settings, label="悟道候选")


def _sync_codes_three_levels(need: set[str], settings: Settings, *, label: str) -> dict:
    """把一批票的近端日线补齐：**东财 → 腾讯 → iFinD**（免费在前、吃配额在后）。

    从 `_ensure_wudao_kline` 抽出来给两处共用：那里补的是「扫描目标日的悟道候选」，
    `backfill_top_kline` 补的是「形态命中评分前 N 只」—— **判据不同、补法相同**。

    **三道闸门**（为同时省时间与配额；阈值见 `Settings` 的注释）：

    - **东财熔断**：连续失败 `wudao_em_breaker_failures` 次就本轮不再试它。
      云端连不上东财直连（1.7 / 8.51.3 记过），而这里是**逐只**补 —— 不熔断的话每只都要
      等一次约 10 秒的超时。实测 09-24：48 只候选白等约 8 分钟，且最终全部落到 iFinD。
    - **腾讯熔断**：同一个道理 —— 这条线也有可能整轮不通（接口变动 / 该机器不可达）。
    - **iFinD 上限**：每轮最多补 `wudao_ifind_fallback_max` 只，超出的**本轮不补**
      （当天就没有这些候选的形态信号）。这是刻意的「配额 ↔ 覆盖」取舍，不静默：
      跳过的只数进 `skipped_quota`，日志与采集日志里都会写出来。
      **有了腾讯这一级，它其实很少再触发** —— 只有在腾讯也整轮不通时才轮得到它顶上来。
    """
    from app.jobs.collect_daily import DailyCollector
    from app.sources.ifind import IfindError

    collector = DailyCollector()
    synced = 0
    failed = 0
    via_em = 0
    via_tencent = 0
    via_ifind = 0
    skipped_quota = 0
    skip_ifind = False
    em_failures = 0
    em_dead = False
    tx_failures = 0
    tx_dead = False
    for code in sorted(need):
        ok = False
        if not em_dead:
            try:
                wrote = _sync_stock_eastmoney(code, days=_WUDAO_SYNC_DAYS)
                synced += 1
                via_em += 1
                ok = True
                logger.info("补日线(东财) %s → %d 行", code, wrote)
            except _NoBars as exc:
                # 这只票这一级没有 —— 换下一级，不算这条源挂了
                logger.info("补日线(东财) %s 无数据，换下一级：%s", code, exc)
            except Exception as exc:  # noqa: BLE001
                em_failures += 1
                if em_failures >= settings.wudao_em_breaker_failures:
                    # 熔断：后面几十只不再一只只等超时。只报一次，别刷屏
                    em_dead = True
                    logger.warning(
                        "补日线(东财) 连续失败 %d 次（最近一只 %s：%s），"
                        "本轮剩余%s不再试东财，改由腾讯 / iFinD 兜底",
                        em_failures,
                        code,
                        exc,
                        label,
                    )
                else:
                    logger.warning("补日线(东财) %s 失败：%s", code, exc)
        if not ok and not tx_dead:
            try:
                wrote = _sync_stock_tencent(code, days=_WUDAO_SYNC_DAYS)
                synced += 1
                via_tencent += 1
                ok = True
                logger.info("补日线(腾讯) %s → %d 行", code, wrote)
            except _NoBars as exc:
                logger.info("补日线(腾讯) %s 无数据，换下一级：%s", code, exc)
            except Exception as exc:  # noqa: BLE001
                tx_failures += 1
                if tx_failures >= settings.wudao_tx_breaker_failures:
                    tx_dead = True
                    logger.warning(
                        "补日线(腾讯) 连续失败 %d 次（最近一只 %s：%s），"
                        "本轮剩余%s不再试腾讯，改由 iFinD 兜底（受上限约束）",
                        tx_failures,
                        code,
                        exc,
                        label,
                    )
                else:
                    logger.warning("补日线(腾讯) %s 失败：%s", code, exc)
        if not ok and not skip_ifind:
            if via_ifind >= settings.wudao_ifind_fallback_max:
                # 到上限了：本轮不补它。不记 failed —— 这不是失败，是刻意的取舍
                skipped_quota += 1
                continue
            try:
                wrote = collector.sync_stock(code, days=_WUDAO_SYNC_DAYS)
                synced += 1
                via_ifind += 1
                ok = True
                logger.info("补日线(iFinD) %s → %d 行", code, wrote)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "401" in msg or isinstance(exc, IfindError):
                    skip_ifind = True
                    logger.warning("iFinD 不可用，本轮不再重试：%s", exc)
                else:
                    logger.warning("补日线(iFinD) %s 失败：%s", code, exc)
        if not ok:
            failed += 1
    if skipped_quota:
        logger.warning(
            "补日线：%d 只%s因 iFinD 兜底到上限（%d 只）本轮未补 —— "
            "想让覆盖更全就调大 `WUDAO_IFIND_FALLBACK_MAX`",
            skipped_quota,
            label,
            settings.wudao_ifind_fallback_max,
        )
    return {
        "synced": synced,
        "failed": failed,
        "needed": len(need),
        "via_eastmoney": via_em,
        "via_tencent": via_tencent,
        "via_ifind": via_ifind,
        "skipped_quota": skipped_quota,
    }


def _latest_hit_date() -> date | None:
    """库里最新的 `pattern_hit` 日期 —— 与形态页默认视图取的那天一致。"""
    with session_scope() as session:
        return session.scalar(select(func.max(PatternHit.trade_date)))


def backfill_top_kline(
    day: date | None = None, *, limit: int = 50, settings: Settings | None = None
) -> dict:
    """把「形态选股」评分前 `limit` 只的日线**补到最近交易日**（默认 50，与页面同一口径）。

    2026-10-09 用户要求「每天把形态选股的 50 支票数据补齐」。形态页那张 K 线读的就是
    `stock_daily` —— 某只的最后一根若早于最近交易日，图上**最新几天就是断的**。

    与 `_ensure_wudao_kline` 有两点不同（所以单独一个函数、不并进去）：

    - 补的是**页面真正会显示的那 50 只**（按票归并取最高分，`_backfill_hit_dde` 同口径），
      不是扫描的候选池；
    - 补到的是**最近交易日**（`_latest_trade_date()`，取自全本交易日历），不是扫描目标日
      —— 所以扫描停在旧日期时（本机库常年落后于线上）也照样往上补。

    `day=None` 时用库里最新的 `pattern_hit` 日期（与页面默认视图一致）。走免费源为主
    （东财 → 腾讯），iFinD 只在两级都不可用时兜底，所以**常态零 iFinD 配额**。
    """
    settings = settings or get_settings()
    target = day or _latest_hit_date()
    if target is None:
        return {"codes": 0, "pending": 0, "want": None}
    codes = _top_hit_codes(target, limit)
    if not codes:
        return {"codes": 0, "pending": 0, "want": None}

    want = _latest_trade_date()
    last = _last_bars(set(codes))
    # 缺的判据只有一条：**最后一根 < 最近交易日**（根数在这里不是问题 —— 能进前 50
    # 的票本来就够它命中的那个形态用了）。
    need = {code for code in codes if last.get(code) is None or last[code] < want}
    if not need:
        return {"codes": len(codes), "pending": 0, "want": want.isoformat()}
    info = _sync_codes_three_levels(need, settings, label=f"{target} 命中前 {limit}")
    return {"codes": len(codes), "pending": len(need), "want": want.isoformat(), **info}


def _top_hit_codes(day: date, limit: int) -> list[str]:
    """某日命中里按股票归并、评分降序的前 `limit` 只 —— 与页面同一口径。

    直接用 `collect_dde.top_hit_codes`（`_backfill_hit_dde` 那一步也用它，
    口径只有一份；两边选出的集合实测完全相同，见那个函数的 docstring）。
    """
    from app.jobs.collect_dde import top_hit_codes

    return top_hit_codes(day, limit)


def scan(
    trade_date: date | None = None,
    settings: Settings | None = None,
    *,
    min_score: float = MIN_SCORE,
) -> dict:
    """扫描一个交易日的全市场形态，整段替换该日的命中记录。"""
    settings = settings or get_settings()
    started = time.monotonic()

    target = trade_date or _latest_trade_date()
    _require_bars(target)

    universe = load_codes()
    if not universe:
        raise IfindError("股票池为空，先建池（UniverseCollector.collect）")

    # 「明天盯 / 明天预案」的候选（创业板 + 科创板里今天出过线的票）—— 这两个形态
    # 在板块之外**另限候选池**；其余四个池子只过板块关、不限候选池（理由见模块顶部那张表）。
    wudao_cands = _candidate_codes(target)
    extras = {c for c in wudao_cands if c not in set(universe)}
    sync_info = (
        _ensure_wudao_kline(wudao_cands, target, settings)
        if wudao_cands
        else {
            "synced": 0,
            "failed": 0,
            "needed": 0,
            "via_eastmoney": 0,
            "via_tencent": 0,
            "via_ifind": 0,
            "skipped_quota": 0,
        }
    )

    scan_codes = set(universe) | extras
    grouped = _load_bars(target, scan_codes)
    if not grouped:
        raise IfindError(f"{target} 没有日线数据，先跑 collect_kline")

    # **日线停在过去的票一律不出信号**。它的「最新」K 线可能是几天前、甚至几周前的，
    # 而命中列表那一列写的是「最新价」—— 混进去就是给用户看旧价（8.64 那次：
    # 列表显示 33.86 / +3.71%，而当天真实收盘是 35.39 / +4.98%）。
    # 上面 `_ensure_wudao_kline` 已经把**候选**里这种票补过一轮，剩下的补不到
    # （当天没采到的池内票、以及真没成交的停牌票），只能不出。
    stale = {code for code, records in grouped.items() if records[-1]["date"] != target}
    for code in stale:
        grouped.pop(code, None)
    if stale:
        logger.warning(
            "%d 只票的日线停在 %s 之前，本轮不出它们的信号（先补日线："
            "`collect_kline` 或 `_ensure_wudao_kline`）",
            len(stale),
            target,
        )

    # **ST 票一律不参与**（2026-09-28 用户要求「形态选股剔除 st 票」）。
    # 放在「日线停在过去」之后、`build_bars` 之前：判据取的是**最新那根 K 线的名字**，
    # 也就是命中列表上要显示的那个名字 —— 用别处的名字（池子里的、`stock_basic` 的）
    # 会有对不上的可能。
    # 致富候选那一支也走这里，所以 ST 涨停股同样进不来（实测 09-24 那天有 111 条）。
    st_codes = {code for code, records in grouped.items() if is_st(records[-1].get("name"))}
    for code in st_codes:
        grouped.pop(code, None)
    if st_codes:
        logger.info("%s：剔除 %d 只 ST 票（形态选股不收 ST）", target, len(st_codes))

    rows: list[dict] = []
    skipped = 0
    dropped_wudao = 0
    by_pattern: dict[str, int] = defaultdict(int)
    bars_map: dict[str, Bars] = {}
    for code, records in grouped.items():
        # 致富只要约 22 根；其它形态内部仍按各自 MIN_BARS 自行跳过
        if len(records) < WUDAO_MIN_BARS:
            skipped += 1
            continue
        bars_map[code] = build_bars(records)

    # 先把每只票的形态全算出来，再决定哪条能落库 —— 华宝早期要排掉**同一只票**上已经
    # 命中的样板/启动（原型 `_early_card_from` 开头就把 start/sample/diverge/dump 退回），
    # 不先算完就没有这个信息。我们只搬了 sample/start 两个阶段，diverge/dump 没有。
    code_sigs: dict[str, list] = {}
    huabao_skip: set[str] = set()
    for code, bars in bars_map.items():
        sigs = list(evaluate(bars, min_score=min_score))
        code_sigs[code] = sigs
        if any(s.pattern in ("wudao_sample", "wudao_start") for s in sigs):
            huabao_skip.add(code)

    for code, sigs in code_sigs.items():
        last = grouped[code][-1]
        for signal in sigs:
            # 「明天盯 / 明天预案」只收候选名单里的票（其余形态一律放行）
            if not _in_wudao_pool(signal.pattern, code, wudao_cands, huabao_skip):
                dropped_wudao += 1
                continue
            by_pattern[signal.pattern] += 1
            rows.append(
                {
                    "trade_date": target,
                    "code": code,
                    "pattern": signal.pattern,
                    "name": last["name"],
                    "score": round(signal.score, 1),
                    "close": last["close"],
                    "pct_chg": last["pct_chg"],
                    "amount": last["amount"],
                    "key_levels": signal.key_levels,
                    "detail": signal.detail,
                }
            )

    with session_scope() as session:
        session.execute(delete(PatternHit).where(PatternHit.trade_date == target))
        written = upsert_many(session, PatternHit, rows)

    cost = round(time.monotonic() - started, 2)
    pool_brief = f"候选 创{len(wudao_cands)}"
    logger.info(
        "形态扫描完成：%s，%d 只票 → %d 条命中"
        "（跳过 %d / 悟道剔池外 %d / %s / 日线停在过去 %d / 剔除 ST %d / 补日线 %s），"
        "用时 %ss",
        target,
        len(grouped),
        written,
        skipped,
        dropped_wudao,
        pool_brief,
        len(stale),
        len(st_codes),
        sync_info,
        cost,
    )
    _record(
        target,
        "ok",
        written,
        f"{len(grouped)} 只 / {written} 条命中 / {pool_brief}"
        + (f" / 日线停在过去剔除 {len(stale)} 只" if stale else "")
        + (f" / 剔除 ST {len(st_codes)} 只" if st_codes else "")
        + _sync_note(sync_info),
        cost,
    )
    return {
        "status": "ok",
        "trade_date": target.isoformat(),
        "codes": len(grouped),
        "skipped": skipped,
        # 日线停在 target 之前、本轮没出信号的只数（>0 就说明当天的日线没采全）
        "stale": len(stale),
        # 因带 ST 被剔除的只数（2026-09-28 起的口径）
        "st_dropped": len(st_codes),
        "wudao_cands": len(wudao_cands),
        "wudao_extras": len(extras),
        "wudao_sync": sync_info,
        "wudao_dropped": dropped_wudao,
        "rows": written,
        "by_pattern": dict(by_pattern),
        "cost_seconds": cost,
    }


def _require_bars(trade_date: date) -> None:
    """确认库里真的有这一天的日线，而且**大部分票都采到了**。

    少了这道校验，扫描会拿**昨天**的 K 线当今天用 —— 结果不是空的，而是「用
    昨天的数据打上今天的日期」，看起来完全正常。这种错误没有任何外部症状，
    只会在事后复盘时发现「那天的信号怎么是用前一天的价算的」。

    ⚠️ 2026-09-24 补：原来只要求「那天有一行」。实测那天 274 只（9%）没采到，
    这个校验照样通过，扫描仍然出了 44 条拿旧 K 线的命中。现在要求覆盖率
    ≥ `_MIN_DAY_COVERAGE`：低于它说明当天没采全，**宁可报错**（当天不出信号），
    也不要出一份掺着旧价的榜单 —— 上面那道「日线停在过去就剔除」只挡得住单只，
    整片缺的时候该让人去看采集，而不是默默筛掉一成票。
    """
    with session_scope() as session:
        latest = session.scalar(select(func.max(StockDaily.trade_date)))
        pool = session.scalar(select(func.count()).select_from(StockUniverse)) or 0
        # 只数**能用**的行（`pct_chg` 非空），与 `_load_bars` 同口径：
        # iFinD 的停牌残行（只有收盘价）会在库里堆着，但它们算不了一根 K 线
        count = (
            session.scalar(
                select(func.count())
                .select_from(StockDaily)
                .where(
                    StockDaily.trade_date == trade_date,
                    StockDaily.pct_chg.is_not(None),
                )
            )
            or 0
        )
    if latest is None:
        raise IfindError("stock_daily 是空的，先跑 collect_kline")
    if latest < trade_date or count == 0:
        raise IfindError(f"{trade_date} 的日线还没采到（库里最新是 {latest}），先跑 collect_kline")
    if pool and count < pool * _MIN_DAY_COVERAGE:
        raise IfindError(
            f"{trade_date} 的日线只采到 {count}/{pool} 只（{count / pool:.0%}），"
            f"低于 {_MIN_DAY_COVERAGE:.0%} —— 当天没采全，先补 collect_kline 再扫"
        )


def _latest_trade_date() -> date:
    with session_scope() as session:
        found = session.scalar(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= date.today())
            .order_by(TradeCalendar.trade_date.desc())
            .limit(1)
        )
    if found is None:
        raise IfindError("交易日历为空，先采集交易日历")
    return found
