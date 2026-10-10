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
from collections.abc import Iterable
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import session_scope
from app.models import StockDaily, TradeCalendar
from app.services.patterns import (
    Bars,
    PhaseVerdict,
    build_bars,
    classify_phase,
    usable_bar,
)

logger = logging.getLogger(__name__)

#: 取多少根日线。`classify_phase` 至少要 25 根，而它里面的 `high_120`（左侧压力）
#: 想看满 120 —— 留一点余量，但别太多（这是一次同步查询，个股页每次都要跑）。
PHASE_BARS = 130

#: 交易日历的「日期 → 升序序号」表；进程内缓存一次，见 `_trade_day_index`。
_TRADE_DAY_INDEX: dict[date, int] | None = None


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
        for item in reversed(rows)
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
    bars: Bars = build_bars(usable)
    return classify_phase(bars)


def load_phases(session: Session, codes: Iterable[str]) -> dict[str, PhaseVerdict]:
    """一次算一批票的阶段判定（形态选股命中列表那种「一屏多只票」的场景）。

    返回 `{code: 判定}`；**日线不够 25 根的票不在结果里**，调用方按「没有」处理。

    内部**逐只复用 `load_phase`**，刻意不另写一条批量 SQL：列表页与个股页对同一只
    票必须给出同一个判定，共用同一条代码路径是唯一不会漂移的办法（本模块开头那段
    说的就是这个坑）。成本随只数线性涨 —— 命中列表默认 50 只；「点进一个形态看全部」
    时最多到 3000 只（`/hits` 的 `limit` 上限），那是用户主动点开的一次操作，秒级
    可以接受。真嫌慢的话，正解是在扫描时把判定一起落进 `pattern_hit`，而不是在这里
    写第二条取数路径。
    """
    out: dict[str, PhaseVerdict] = {}
    for code in codes:
        verdict = load_phase(session, code)
        if verdict is not None:
            out[code] = verdict
    return out
