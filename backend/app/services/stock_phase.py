"""个股「阶段判定」的取数壳：`code` → `PhaseVerdict`。

判定本身在 `services/patterns.classify_phase`（与形态选股的「明天盯 / 明天预案」
**共用同一批判据**）；这里只管把库里最近 `PHASE_BARS` 根日线取出来、剔掉没法用的行、
转成 `Bars`，然后交给它。

为什么要单独一个模块：**个股页**（`api/stock.profile`）与**个股分析页**
（`api/analysis.lookup`）都要这一段。两边各写一遍早晚会分叉 —— 比如一处取 60 根、
一处取 130 根，`ma60` / `high_120` 那两步就跟着不一样，而症状只是「同一个票两个页面
的左侧压力不一样」，很难往回找。
"""

import logging
import threading
from collections.abc import Iterable
from datetime import date

from sqlalchemy import Row, func, select
from sqlalchemy.orm import Session

from app.db import session_scope
from app.models import StockDaily, TradeCalendar
from app.services.patterns import (
    Bars,
    OVERSOLD_DEV_PCT,
    OVERSOLD_MA,
    PhaseVerdict,
    build_bars,
    classify_phase,
    oversold_deviation,
    usable_bar,
)
from app.sources.base import chunked

logger = logging.getLogger(__name__)

#: 取多少根日线。`classify_phase` 至少要 25 根，而它里面的 `high_120`（左侧压力）
#: 想看满 120 —— 留一点余量，但别太多（这是一次同步查询，个股页每次都要跑）。
PHASE_BARS = 130

#: 批量取日线时的 IN 分片大小（`load_phases`）。SQLite 单条 SQL 的变量上限现代编译值是
#: 32766，但按 900 分批更稳，也与 `api/patterns.py` 的 `_IN_BATCH` 同口径。
_PHASE_IN_BATCH = 900

#: 交易日历的「日期 → 升序序号」表；进程内缓存一次，见 `_trade_day_index`。
_TRADE_DAY_INDEX: dict[date, int] | None = None

#: 判定结果的进程内缓存：`code → PhaseVerdict | None`（None = 这票没有可用的日线）。
#:
#: 为什么需要：一次「一屏几千只」的列表（形态页点进一个形态、`/hits` 取 3000 只）要把
#: 每只票最近 `PHASE_BARS` 根日线读回来算一遍 —— 合计约 39 万行，实测 **14 秒**。而
#: **同一份日线在同一天里算出来的判定就是同一个结果**，反复点不同形态只是把重叠度很高
#: 的那批票重算几遍。所以按**数据版本**缓存，采集一写库就整体作废，不需要靠 TTL 猜 ——
#: 与 `api/patterns._TRACK_CACHE` 同一个思路（那里缓存键里也带着「最新日线日」）。
#:
#: ⚠️ 版本 =(**最新日线日, 那一天的行数**)，不是只看日期（2026-10-15 修）：同一天里还会
#: **陆续补进当天那根** —— 个股页首次打开时的同步、形态扫描给候选股补的日线都不改
#: 「最新日期」。只看日期的话这些补进来的行会让缓存里的旧判定一直沿用到第二天，而
#: `load_phase`（个股页）不走缓存、照实读库，于是**同一只票在形态列表与个股页显示不同的
#: 阶段**（那个模块开头专门写了「两处必须一致」）。带上行数就能让「补过数据」也触发刷新。
#:
#: ⚠️ 仍然盖不住的一种改动：**原地订正**某只票已有的那行（重采把坏值改好）—— 行数不变。
#: 判定只用最近 130 根，且同一天里重采订正是少见情形，重启服务也会清空；不为它上更重的
#: 版本口径（比如整表校验和，那是每次请求都要扫全表）。
#:
#: ⚠️ 缓存里连 **None 也要存**：否则「本来就没有判定」的那批（次新、已退池、停牌到
#: 没数据）每次请求都会被重新查一遍、重新算一遍。空库时不缓存（同 `_trade_day_index`
#: 的空表处理）—— 免得把「什么都没有」永久钉住。
_PHASE_CACHE: dict[str, PhaseVerdict | None] = {}
_PHASE_CACHE_VERSION: tuple[date, int] | None = None
#: 「超跌」筛选的进程内缓存（`load_oversold`）。与阶段判定**分开存**：值类型不同
#: （这里存「乖离%」或 None），共用一份 dict 会互相覆盖。版本口径与上面完全一样。
#: None = 算不出来（日线不够 20 根 / 没有数据），同样要缓存，否则次新股每次都要重算。
_OVERSOLD_CACHE: dict[str, float | None] = {}
_OVERSOLD_CACHE_VERSION: tuple[date, int] | None = None
#: 接口跑在线程池里，多个请求会同时读写这份 dict；只有「命中读 / 写回」几微秒在锁内。
_PHASE_CACHE_LOCK = threading.Lock()

#: 算判定要取的列。用 **Core 列**而不是 ORM 实体：实体每行都要过一遍 SQLAlchemy 的
#: 属性仪表（一屏十几万行时这部分能占掉约 1/4 的耗时，见 `_compute_verdicts`）；
#: `_verdict_from_rows` 只按属性名读这几项，两种入参都能吃。
_BAR_COLS = (
    StockDaily.code,
    StockDaily.trade_date,
    StockDaily.open,
    StockDaily.high,
    StockDaily.low,
    StockDaily.close,
    StockDaily.volume,
    StockDaily.amount,
    StockDaily.pct_chg,
)


def _trade_day_index(refresh: bool = False) -> dict[date, int]:
    """全量交易日历的「交易日 → 升序序号」表（进程内缓存一份）。

    `load_phases` 会对一屏几百上千只票**逐只**调 `load_phase`，若每次都查一遍日历，
    查询数直接翻倍；而日历几乎不变，缓存一份最省。`refresh=True` 强制重查：日历表是
    **预置**的（跑到年底），进程若跨过预置边界就会变旧 —— 那时还拿旧表判「相邻」，
    最新那根会因查不到而被误截，所以 `load_phase` 发现「最新一根不在表里」时会重查。
    空表（还没建日历）不缓存 —— 免得建库之前的那次调用把空表永久钉住。
    """
    global _TRADE_DAY_INDEX
    if _TRADE_DAY_INDEX is not None and not refresh:
        return _TRADE_DAY_INDEX
    with session_scope() as session:
        days = list(session.scalars(select(TradeCalendar.trade_date)))
    index = {day: i for i, day in enumerate(sorted(days))}
    if index:
        _TRADE_DAY_INDEX = index
    return index


def trim_contiguous_tail(records: list[dict], trade_day_index: dict[date, int]) -> list[dict]:
    """按交易日历，只保留「从最新一天往前连续」的那一段日线。

    `records` 按日期升序；`trade_day_index` 是「交易日 → 它在日历里的升序序号」
    （相邻交易日序号差 1）。返回截断后的尾部（原列表的切片，不复制元素）。

    ## 为什么必须截断

    形态与阶段判定都把**相邻两行**当成**相邻交易日**来复利、找摆动点。池外票
    （每天只补当天那根，见 `jobs.scan_patterns._load_bars`）以及任何缺口的序列，
    中间缺的那几天会被当成没发生过：复利滚错、摆动点挪位 —— 据此判出来的
    「长窗口形态」与「左侧压力」是**拿空洞拼出来的假形态**。

    做法：从最后一行往前扫，相邻两行在日历上不是相邻交易日（中间夹着交易日）就从
    那里截断，**丢弃更早的部分**。

    ## 代价（有意为之）

    可用历史会**变短**：有缺口的票，窗口缩到最近那段连续区间（极端情况下只剩一两根，
    直接不参与判定）。于是形态命中与阶段判定都**更保守** —— 宁可因为样本不够不判，
    也不拿空洞拼出来的假形态。这里不补洞（补洞要靠来源，本地无从补起）。

    连续性用**行本身**判（含停牌日那种「只有收盘价」的残行）：残行证明「那个交易日
    在库里是有行的」，停牌不该把整段历史误截掉；之后 `usable_bar` 再把残行剔掉。
    调用方**先取够数据再截断**（查询窗口不能因为截断而变小），免得本来够的根数不够用。
    """
    cut = 0
    for i in range(len(records) - 1, 0, -1):
        prev_index = trade_day_index.get(records[i - 1]["date"])
        cur_index = trade_day_index.get(records[i]["date"])
        # 相邻交易日序号正好差 1；查不到（不在日历里）也按断裂处理，宁可保守
        if prev_index is not None and cur_index is not None and cur_index - prev_index == 1:
            continue
        cut = i
        break
    return records[cut:]


def _bars_from_rows(rows: Iterable[StockDaily | Row]) -> Bars | None:
    """把一只票的一段日线（**顺序不限**，内部按交易日升序）整理成 `Bars`。

    `rows` 既可以是 ORM 的 `StockDaily`（个股页 `load_phase`），也可以是 Core 查出来的
    `Row`（列表页 `_compute_verdicts`，见那里为什么不用实体）—— 只按属性名读九个字段，
    两条路径都吃。

    阶段判定与「超跌」筛选共用这一段：同一只票在个股页与列表页必须给出同一个结论，
    共用同一段取数/清洗是唯一不会漂移的办法（见模块开头）。
    """
    records = [
        {
            "date": item.trade_date,
            "open": item.open,
            "high": item.high,
            "low": item.low,
            "close": item.close,
            "volume": item.volume,
            "amount": item.amount,
            "pct_chg": item.pct_chg,
        }
        for item in sorted(rows, key=lambda item: item.trade_date)
    ]
    # 先按交易日历截成「最近一段连续」（有缺口的票窗口会缩水，见 `trim_contiguous_tail`
    # 的代价说明），再剔残行 —— 顺序见那个函数的 docstring。
    index = _trade_day_index()
    if records and records[-1]["date"] not in index:
        # 缓存日历可能已过期（跨过预置边界）：最新一根查不到会被误判成断裂，重查一次
        index = _trade_day_index(refresh=True)
    records = trim_contiguous_tail(records, index)
    # 只留能当一根 K 线用的行：iFinD 对停牌日会给一行「只有收盘价、其余全空」的残行，
    # `build_bars` 里 `float(None)` 会直接抛 500。判据抽成了公共的 `usable_bar`
    # （2026-10-10），与 `scan_patterns._load_bars` / `backtest_patterns._load_history`
    # 同一口径（与 `api/stock._adjusted` 也是同一套）。
    usable = [record for record in records if usable_bar(record)]
    if not usable:
        return None
    return build_bars(usable)


def _verdict_from_rows(rows: Iterable[StockDaily | Row]) -> PhaseVerdict | None:
    """把一只票的一段日线算成阶段判定；没有可用日线时返回 None。"""
    bars = _bars_from_rows(rows)
    return None if bars is None else classify_phase(bars)


def load_phase(session: Session, code: str) -> PhaseVerdict | None:
    """取最近的日线算阶段判定。根数不够（次新 / 刚缓存）返回 None，调用方不显示这块。"""
    rows = list(
        session.scalars(
            select(StockDaily)
            .where(StockDaily.code == code)
            .order_by(StockDaily.trade_date.desc())
            .limit(PHASE_BARS)
        )
    )
    return _verdict_from_rows(rows)


def _recent_rows(
    session: Session, codes: list[str], bars: int = PHASE_BARS
) -> dict[str, list[Row]]:
    """按 code 各取最近 `bars` 根日线（Core 列，窗口函数分片）。

    阶段判定与「超跌」筛选共用这一段取数 —— 两处的数据窗口、分批大小、排序口径必须
    一致，否则同一只票会在两个筛选条件里给出互相矛盾的结论。

    `bars` 默认是阶段判定的 130 根；超跌只需要 MA20，传 25 根能把全市场取数从
    65 万行降到 10 万行（见 `load_oversold`）。
    """
    grouped: dict[str, list[Row]] = {}
    for batch in chunked(codes, _PHASE_IN_BATCH):
        recent = (
            select(
                *_BAR_COLS,
                func.row_number()
                .over(
                    partition_by=StockDaily.code,
                    order_by=StockDaily.trade_date.desc(),
                )
                .label("rn"),
            )
            .where(StockDaily.code.in_(batch))
            .subquery()
        )
        for row in session.execute(select(recent).where(recent.c.rn <= bars)):
            grouped.setdefault(row.code, []).append(row)
    return grouped


def _compute_verdicts(session: Session, codes: list[str]) -> dict[str, PhaseVerdict | None]:
    """按 code 取最近 `PHASE_BARS` 根日线算判定；**没有日线 / 算不出来的记 None**。

    None 是「这只票没有判定」的显式记号：缓存要能记住「算过了、就是没有」，否则每天
    都会被同一批次新 / 已退池 / 停牌到没数据的票反复重查重算（见 `_PHASE_CACHE`）。

    两条关键实现（都是 2026-10-15 实测调出来的）：

    1. **用窗口函数，每只票只取 `PHASE_BARS` 根**。SQLite 的 `LIMIT` 是整条语句级的，
       写不出「每只票各取 130 根」，所以按 `code` 分区、`trade_date` 降序编号，只留
       编号 ≤ `PHASE_BARS` —— 走 `ix_stock_daily_code_date`，与 `load_phase` 的
       `limit(PHASE_BARS)` 同一个窗口。原先（2026-10-12 那版）是按批把整段历史读回来、
       到 Python 里才切前 130 根：3000 只票要多搬进 ORM 几十万行，实测 7 秒起步。
       ⚠️ `code.in_(batch)` 必须写在**窗口那一层**：放到外层去过滤的话，编号是先把
       全市场编号完再筛，这批票的 rn 早就不 ≤ `PHASE_BARS`，只会剩下零星几行。
    2. **取 Core 列而不是 ORM 实体**（`_BAR_COLS`）：实体每行都要过一遍 SQLAlchemy 的
       属性仪表，一屏十几万行时占掉约 1/4 耗时（实测 1145 只：5.1 秒 → 3.9 秒），
       判定结果逐只比对完全一致。
    """
    grouped = _recent_rows(session, codes)
    out: dict[str, PhaseVerdict | None] = {}
    for code in codes:
        rows = grouped.get(code)
        out[code] = _verdict_from_rows(rows) if rows else None
    return out


def _phase_version(session: Session) -> tuple[date, int] | None:
    """判定缓存的数据版本 = (**最新日线日**, 那一天的行数)；库还是空的时返回 None。

    两个查询都走 `stock_daily` 的主键索引（`trade_date` 是首列）：`max` 是取索引尾、
    计数是那一天的等值区间（约 5300 行），都不到 1 毫秒 —— 相对它省下的十几秒可以忽略。

    为什么要带「那一天的行数」而不是只看日期：同一天里还会陆续补进当天那根（个股页
    首次打开时的同步、形态扫描给候选股补的日线），日期不变、**行数会变**。详见
    `_PHASE_CACHE` 那段说明。
    """
    latest = session.scalar(select(func.max(StockDaily.trade_date)))
    if latest is None:
        return None
    rows_on_latest = session.scalar(
        select(func.count()).select_from(StockDaily).where(StockDaily.trade_date == latest)
    )
    return latest, int(rows_on_latest or 0)


def load_phases(session: Session, codes: Iterable[str]) -> dict[str, PhaseVerdict]:
    """一次算一批票的阶段判定（形态选股命中列表那种「一屏多只票」的场景）。

    返回 `{code: 判定}`；**日线不够 25 根的票不在结果里**，调用方按「没有」处理。

    2026-10-12 由「逐只 `load_phase`」改成**批量取数**：以前 `/hits` 点进一个形态看全部
    时最多 3000 只 → 3000 条 SQL（N+1）。现在按 `_PHASE_IN_BATCH` 分批把日线一次取回、
    按 code 分组，再交给与 `load_phase` **完全同一个** `_verdict_from_rows` 来算 ——
    判定口径一字未改，查询数从 3000 降到个位数。

    2026-10-15 两次收尾（那版批量取数**漏了每只票的根数限制**，一屏 3000 只时整条
    `/hits` 要 43 秒）：

    - 取数改成窗口函数 + Core 列（见 `_compute_verdicts`）：整条 `/hits` 取 3000 只
      **43 秒 → 18.8 秒**（其中判定本身就是 14 秒）；
    - 结果按**数据版本**缓存（`_PHASE_CACHE`）—— 形态之间的票重叠度很高，同一天里
      第二次点开几乎是白算，那 14 秒随后降到几百毫秒。
    """
    global _PHASE_CACHE, _PHASE_CACHE_VERSION

    ordered = list(codes)
    if not ordered:
        return {}

    # 数据版本 = (最新日线日, 那天行数)：采集写库或当天补进新行都会让它变，缓存整体作废
    version = _phase_version(session)
    cached: dict[str, PhaseVerdict | None]
    if version is None:
        # 空库（还没采到日线）：照算，但一个字都不缓存（同 `_trade_day_index` 的空表处理）
        cached = {}
        pending = ordered
    else:
        with _PHASE_CACHE_LOCK:
            if version != _PHASE_CACHE_VERSION:
                _PHASE_CACHE, _PHASE_CACHE_VERSION = {}, version
            cached = {code: _PHASE_CACHE[code] for code in ordered if code in _PHASE_CACHE}
        pending = [code for code in ordered if code not in cached]

    if pending:
        fresh = _compute_verdicts(session, pending)
        cached.update(fresh)
        if version is not None:
            with _PHASE_CACHE_LOCK:
                # 算这一批的期间，采集可能刚好写库（版本已经变了）：那一批是按旧日线算的，
                # 写进去会让下一天之前的请求都读到旧判定 —— 直接丢掉，宁可下次重算
                if _PHASE_CACHE_VERSION == version:
                    _PHASE_CACHE.update(fresh)

    # 保持入参顺序（调用方按 code 逐个 `.get`，顺序不影响，但别在这里无谓改口径）
    return {code: cached[code] for code in ordered if cached.get(code) is not None}


def load_oversold(session: Session, codes: Iterable[str]) -> dict[str, float]:
    """这批票里当天「超跌」的那些 → `{code: 乖离%}`（负值，越负跌得越狠）。

    口径取自用户提供的 `日线做T.hxf`（见 `patterns.is_oversold`），与阶段判定共用同一段
    取数与清洗（`_recent_rows` + `_bars_from_rows`）—— 两处对同一只票必须看到同一根 K。
    它回答的是「离 20 日线够不够远」，与 `classify_phase` 的 9 档互不影响。

    返回**乖离值**而不只是 code 集合：筛选结果要按「跌得有多狠」排序，阈值附近（−15%）
    和深跌（−30%）在列表里的先后差得很远。非超跌的票不出现在结果里。

    缓存与 `load_phases` 同一套版本口径，但用**独立的 dict**：两边的值类型不同
    （一档标签一档乖离），混在一起会互相覆盖。
    """
    global _OVERSOLD_CACHE, _OVERSOLD_CACHE_VERSION

    ordered = list(codes)
    if not ordered:
        return {}

    version = _phase_version(session)
    cached: dict[str, float | None]
    if version is None:
        cached = {}
        pending = ordered
    else:
        with _PHASE_CACHE_LOCK:
            if version != _OVERSOLD_CACHE_VERSION:
                _OVERSOLD_CACHE, _OVERSOLD_CACHE_VERSION = {}, version
            cached = {code: _OVERSOLD_CACHE[code] for code in ordered if code in _OVERSOLD_CACHE}
        pending = [code for code in ordered if code not in cached]

    if pending:
        # 只需要 MA20（20 根），不必按阶段判定的 130 根取 —— 全市场 5000 只时这是
        # 65 万行 vs 10 万行的差别（实测 20 秒 vs 3 秒）
        grouped = _recent_rows(session, pending, bars=OVERSOLD_MA + 5)
        fresh: dict[str, float | None] = {}
        for code in pending:
            rows = grouped.get(code)
            bars = _bars_from_rows(rows) if rows else None
            fresh[code] = oversold_deviation(bars) if bars is not None else None
        cached.update(fresh)
        # 同 `load_phases`：算的期间数据版本变了就丢掉这批（宁可下次重算）
        if version is not None:
            with _PHASE_CACHE_LOCK:
                if _OVERSOLD_CACHE_VERSION == version:
                    _OVERSOLD_CACHE.update(fresh)

    # 缓存里存**全部**乖离（超跌与否都存）—— 阈值以后要调时不必重算；过滤放在这里做，
    # 与 `patterns.is_oversold` 同一个阈值常量（OVERSOLD_DEV_PCT）
    return {
        code: dev
        for code in ordered
        if (dev := cached.get(code)) is not None and dev < OVERSOLD_DEV_PCT
    }
