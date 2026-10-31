"""ETF 次日验证与动量轮动回测（2026-10-31 加，ETF-4）。

**筛选口径一律走 `etf_screen`**（`to_rows` / `select` / `PRESETS`），这里只负责
「把历史一天一天重放一遍、算持有期收益、和基准比」—— 两处阈值不能各写一份，
形态回测当年就是两套口径分叉后把超额高估了一倍（见 `etf_screen` 的注释）。

三条硬口径（指令文件 ETF-4 的「必须避开的坑」）：

1. **不能用后来才有的信息**：每个信号日只用**截至那天**的日线 / 份额快照算指标；
   同指数去重的代表按**当时**的 20 日均额选；基金类型与分类来自 `etf_basic` /
   `etf_category`（按名称判定，当天就知道）。
2. **次日收盘起算**：信号在收盘后才成立，现实里最早只能**次日收盘**买入，
   持有 `hold` 个交易日后按收盘卖出（`t+1` 收盘 → `t+1+hold` 收盘）。
   已退市 / 停牌的 ETF 留在样本里：入场取它**下一份**日线，出场取 `exit` 之前
   **最后一份**日线，不做补偿（与 `backtest_patterns` 的停牌处理同一思路）。
3. **`inflow` 预设的起点受限**：净申赎要份额快照（2026-09-21 才有），
   20 日窗口再往后 20 个份额日，所以可验证样本比其它预设少得多 —— 页面写明。
4. **规模门槛在份额快照之前失效**：更早的交易日算不出规模（`etf_share` 没有数据），
   历史重放那段时间只保留 20 日均额门槛（`etf_screen.select(skip_scale=True)`）；
   拿后来的份额去倒推就是前视偏差，宁可少一道门槛。

基准两个：**510300（沪深300ETF）** 与「**去重后全体等权**」，入场 / 出场日与被测
策略相同。收益单位是 %，累计是逐期复利。

性能：`etf_bar` 一次性读全量（约 40 万行），逐日指标用**前缀和**算（涨幅 = pct 的
log 累加、MA = 复权价的窗口均值）。线上实测 120 天首次约 10 秒（数据集 4 秒 +
逐日算 7 秒），**逐日行缓存**之后换预设 / 只数 / 持有期约 1 秒；最终结果在
`api/etf.py` 按「参数 + 最新日线日 + 最新份额日」再缓存 10 分钟。
"""

import statistics
from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from math import exp, log, sqrt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import EtfBar, EtfBasic, EtfCategory, EtfShare
from app.services import etf_flow, etf_screen
from app.services.etf_screen import _Raw

#: 基准：沪深300ETF（规模 / 成交额最大的宽基底仓）
BENCH_CODE = "510300"
#: 默认回看多少个交易日（一年 ≈ 250，但指标窗口要 250，信号日只能取在后面）
DEFAULT_DAYS = 120
MAX_DAYS = 240
MAX_TOP = 20
#: 信号日之前至少要有这么多根日线，否则 RPS / MA / 新高都算不出来
MIN_SIGNAL_BARS = etf_screen.NEW_HIGH_DAYS
#: 均额 / 净申赎窗口（与 `etf_screen` 一致）
AVG_DAYS = etf_screen.AVG_DAYS
MIN_COVERAGE = etf_screen.MIN_COVERAGE
#: 回测成本：单边万 0.5（ETF 免印花税）
DEFAULT_COST = 0.00005


@dataclass(frozen=True, slots=True)
class Pick:
    code: str
    name: str | None
    ret: float | None


@dataclass(frozen=True, slots=True)
class Day:
    trade_date: date
    entry_date: date | None
    exit_date: date | None
    ret: float | None
    hs300_ret: float | None
    universe_ret: float | None
    picks: list[Pick]


@dataclass(frozen=True, slots=True)
class Track:
    preset: str
    top: int
    hold: int
    days: int
    as_of: date | None
    note: str | None
    rows: list[Day]
    curve: list[dict]
    stats: dict


@dataclass(frozen=True, slots=True)
class Rotation:
    preset: str
    top: int
    step: int
    cost: float
    periods: list[dict]
    stats: dict
    bench: dict


@dataclass(slots=True)
class _Ser:
    """一只 ETF 的日线序列（`close` 为 None 的行已剔除）与各指标的前缀和。"""

    pos: list[int]
    close: list[float]
    pct: list[float | None]
    amount: list[float | None]
    #: log(1+pct/100) 的前缀和，`L[k]` = 前 k 根（严格说是 pct[0..k-1]）之和
    L: list[float]
    #: `E[k] = exp(L[k])`，用于复权价：`adj_i(j) = close[i] * E[j+1] / E[i+1]`
    E: list[float]
    Epre: list[float]
    retcnt: list[int]
    amtpre: list[float]
    amtcnt: list[int]
    pctpre: list[float]
    pct2pre: list[float]
    pctcnt: list[int]
    #: 创 60 日新高（用复权价判，与 `etf_screen` 一致）
    hi60: list[bool]
    #: 距 250 日高点回撤（%，≤ 0）
    ddown: list[float | None]


@dataclass(slots=True)
class _Dataset:
    days: list[date]
    series: dict[str, _Ser]
    basics: dict[str, EtfBasic]
    categories: dict[str, str]
    #: 每只 ETF 的份额快照（日期升序）
    share_days: dict[str, list[date]]
    share_values: dict[str, list[float]]
    nets: dict[date, dict[str, float]]
    net_keys: list[date]
    #: 最早一份份额快照日；之前算不出规模（历史重放要跳过规模门槛，见 `track`）
    first_share_day: date | None
    #: 逐日「去重后全量行」缓存：每天约 1000 行，与预设 / top / hold 都无关，
    #: 换参数时直接复用（首次 10 秒 → 之后约 1 秒）。上限见 `_ROWS_CACHE_DAYS`
    rows_cache: dict[int, list] = field(default_factory=dict)


def _rolling_max(values: list[float], size: int) -> list[float]:
    """`out[i] = max(values[max(0, i-size+1) .. i])`（单调队列，O(n)）。"""
    out = [0.0] * len(values)
    queue: deque[int] = deque()
    for index, value in enumerate(values):
        while queue and values[queue[-1]] <= value:
            queue.pop()
        queue.append(index)
        while queue[0] <= index - size:
            queue.popleft()
        out[index] = values[queue[0]]
    return out


def _build_series(
    rows: list[tuple[date, float | None, float | None, float | None]],
    index_of: dict[date, int],
) -> _Ser | None:
    pos: list[int] = []
    close: list[float] = []
    pct: list[float | None] = []
    amount: list[float | None] = []
    for trade_date, value, pct_chg, turnover in rows:
        if value is None:
            continue
        pos.append(index_of[trade_date])
        close.append(float(value))
        pct.append(pct_chg)
        amount.append(turnover)
    size = len(pos)
    if size == 0:
        return None

    logs = [0.0] * (size + 1)
    retcnt = [0] * (size + 1)
    for step in range(1, size + 1):
        value = pct[step - 1]
        if value is None:
            logs[step] = logs[step - 1]
            retcnt[step] = retcnt[step - 1]
        else:
            factor = 1 + value / 100
            # 理论上涨跌幅 > -100%，保险起见兜住 0 与负数（脏数据不该让 log 崩）
            logs[step] = logs[step - 1] + (log(factor) if factor > 0 else 0.0)
            retcnt[step] = retcnt[step - 1] + 1
    expo = [exp(value) for value in logs]
    epre = [0.0] * (size + 1)
    amtpre = [0.0] * (size + 1)
    amtcnt = [0] * (size + 1)
    pctpre = [0.0] * (size + 1)
    pct2pre = [0.0] * (size + 1)
    pctcnt = [0] * (size + 1)
    for step in range(1, size + 1):
        epre[step] = epre[step - 1] + expo[step]
        value = amount[step - 1]
        amtpre[step] = amtpre[step - 1] + (value or 0.0)
        amtcnt[step] = amtcnt[step - 1] + (1 if value is not None else 0)
        pct_value = pct[step - 1]
        if pct_value is None:
            pctpre[step] = pctpre[step - 1]
            pct2pre[step] = pct2pre[step - 1]
            pctcnt[step] = pctcnt[step - 1]
        else:
            pctpre[step] = pctpre[step - 1] + pct_value
            pct2pre[step] = pct2pre[step - 1] + pct_value * pct_value
            pctcnt[step] = pctcnt[step - 1] + 1

    high60 = _rolling_max(logs, etf_screen.NEW_HIGH_DAYS)
    high250 = _rolling_max(logs, etf_screen.WINDOW)
    hi60 = [
        (step + 1 >= etf_screen.NEW_HIGH_DAYS) and logs[step + 1] >= high60[step + 1]
        for step in range(size)
    ]
    ddown: list[float | None] = []
    for step in range(size):
        top = high250[step + 1]
        ddown.append(round((expo[step + 1] / exp(top) - 1) * 100, 2))
    return _Ser(
        pos=pos,
        close=close,
        pct=pct,
        amount=amount,
        L=logs,
        E=expo,
        Epre=epre,
        retcnt=retcnt,
        amtpre=amtpre,
        amtcnt=amtcnt,
        pctpre=pctpre,
        pct2pre=pct2pre,
        pctcnt=pctcnt,
        hi60=hi60,
        ddown=ddown,
    )


#: 数据集缓存（`etf_bar` 全量 + 份额 + 净申赎）：`(引擎, 最新日线日, 最新份额日, 时间, 数据)`。
#: 一次约 1~2 秒，而验证面板换参数会连着请求几次（照 `etf_screen._RAW_CACHE`）。
_DATA_CACHE: tuple[object, date | None, date | None, datetime, _Dataset | None] | None = None
_DATA_TTL = timedelta(minutes=10)


def _dataset(session: Session) -> _Dataset | None:
    global _DATA_CACHE
    day = session.scalar(select(func.max(EtfBar.trade_date)))
    share_day = session.scalar(select(func.max(EtfShare.trade_date)))
    bind = session.get_bind()
    now = datetime.now()
    cached = _DATA_CACHE
    if (
        cached is not None
        and cached[0] is bind
        and cached[1] == day
        and cached[2] == share_day
        and now - cached[3] < _DATA_TTL
    ):
        return cached[4]
    data = _load(session) if day is not None else None
    _DATA_CACHE = (bind, day, share_day, now, data)
    return data


def _load(session: Session) -> _Dataset:
    days = list(
        session.scalars(select(EtfBar.trade_date).distinct().order_by(EtfBar.trade_date))
    )
    index_of = {value: index for index, value in enumerate(days)}
    grouped: dict[str, list[tuple]] = defaultdict(list)
    for code, trade_date, close, pct_chg, amount in session.execute(
        select(EtfBar.code, EtfBar.trade_date, EtfBar.close, EtfBar.pct_chg, EtfBar.amount)
    ).all():
        grouped[str(code)].append((trade_date, close, pct_chg, amount))
    series: dict[str, _Ser] = {}
    for code, rows in grouped.items():
        rows.sort(key=lambda item: item[0])
        value = _build_series(rows, index_of)
        if value is not None:
            series[code] = value

    basics = {row.code: row for row in session.scalars(select(EtfBasic))}
    categories = {row.code: row.category for row in session.scalars(select(EtfCategory))}

    shares: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for code, trade_date, value in session.execute(
        select(EtfShare.code, EtfShare.trade_date, EtfShare.shares)
    ).all():
        if value is None:
            continue
        shares[str(code)].append((trade_date, float(value)))
    share_days: dict[str, list[date]] = {}
    share_values: dict[str, list[float]] = {}
    for code, rows in shares.items():
        rows.sort(key=lambda item: item[0])
        share_days[code] = [item[0] for item in rows]
        share_values[code] = [item[1] for item in rows]

    # 只取覆盖达标的份额日（碎片快照当基准会让第二天几乎所有券配不上，
    # 与 `etf_screen` 同一口径，见 `etf_flow.covered_days`）
    snapshot_days = etf_flow.covered_days(session)
    pairs = [
        (snapshot_days[index], snapshot_days[index - 1])
        for index in range(1, len(snapshot_days))
    ]
    money = etf_flow.money_fund_codes(session)
    nets = etf_flow.code_nets(session, pairs, money)
    return _Dataset(
        days=days,
        series=series,
        basics=basics,
        categories=categories,
        share_days=share_days,
        share_values=share_values,
        nets=nets,
        net_keys=[day for day, _ in pairs],
        first_share_day=snapshot_days[0] if snapshot_days else None,
    )


def _ret(series: _Ser, index: int, size: int) -> float | None:
    start = index + 1 - size
    if start < 0:
        return None
    if series.retcnt[index + 1] - series.retcnt[start] < size * MIN_COVERAGE:
        return None
    return round((exp(series.L[index + 1] - series.L[start]) - 1) * 100, 2)


def _ma(series: _Ser, index: int, size: int) -> float | None:
    if index + 1 < size:
        return None
    window = series.Epre[index + 1] - series.Epre[index + 1 - size]
    return series.close[index] * window / (size * series.E[index + 1])


def _avg_amount(series: _Ser, index: int, size: int = AVG_DAYS) -> float | None:
    if index + 1 < size:
        return None
    total = series.amtpre[index + 1] - series.amtpre[index + 1 - size]
    count = series.amtcnt[index + 1] - series.amtcnt[index + 1 - size]
    return total / count if count else None


def _volatility(series: _Ser, index: int, size: int = AVG_DAYS) -> float | None:
    if index + 1 < size:
        return None
    count = series.pctcnt[index + 1] - series.pctcnt[index + 1 - size]
    if count < size * MIN_COVERAGE:
        return None
    total = series.pctpre[index + 1] - series.pctpre[index + 1 - size]
    total2 = series.pct2pre[index + 1] - series.pct2pre[index + 1 - size]
    mean = total / count
    variance = (total2 - count * mean * mean) / (count - 1)
    return round(sqrt(max(variance, 0.0)) * sqrt(250), 2)


def _raws_at(data: _Dataset, index: int) -> list[_Raw]:
    """截至 `index` 那一天（含）算一遍逐只原始指标，供 `etf_screen` 的预设重放。"""
    day = data.days[index]
    end = bisect_right(data.net_keys, day)
    flow_keys = data.net_keys[max(0, end - max(etf_screen.FLOW_WINDOWS)) : end]
    raws: list[_Raw] = []
    for code, series in data.series.items():
        pos = bisect_left(series.pos, index)
        if pos >= len(series.pos) or series.pos[pos] != index:
            continue
        basic = data.basics.get(code)
        close = series.close[pos]
        amounts = series.amount

        # 规模：截至当天最近一份份额快照（缺就 None，不拿 0 顶）
        shares_now: float | None = None
        snapshot_days = data.share_days.get(code)
        if snapshot_days:
            slot = bisect_right(snapshot_days, day) - 1
            if slot >= 0:
                shares_now = data.share_values[code][slot] or None
        scale = shares_now * close / 1e8 if shares_now is not None else None

        series_flow = [data.nets.get(key, {}).get(code) for key in flow_keys]
        flows: dict[int, float | None] = {}
        for window in etf_screen.FLOW_WINDOWS:
            available = [value for value in series_flow[-window:] if value is not None]
            flows[window] = (
                sum(available) if len(available) >= window * MIN_COVERAGE else None
            )
        flow20 = flows[20]
        scale_yuan = scale * 1e8 if scale else None
        flow_ratio = (
            round(flow20 / scale_yuan * 100, 2)
            if flow20 is not None and scale_yuan
            else None
        )
        raws.append(
            _Raw(
                code=code,
                name=basic.name if basic else None,
                norm=(basic.norm_name or basic.name or code) if basic else code,
                category=data.categories.get(code) or "其他",
                fund_type=basic.fund_type if basic else None,
                t0=bool(basic.t0) if basic else False,
                close=close,
                pct_chg=series.pct[pos],
                amount=amounts[pos],
                avg_amount=_avg_amount(series, pos),
                scale=scale,
                returns={
                    window: _ret(series, pos, window) for window in etf_screen.RETURN_WINDOWS
                },
                ma20=_ma(series, pos, AVG_DAYS),
                ma60=_ma(series, pos, 60),
                drawdown=series.ddown[pos],
                volatility=_volatility(series, pos),
                flow5=flows[5],
                flow20=flow20,
                flow20_ratio=flow_ratio,
                bars=len(series.pos),
                new_high=series.hi60[pos],
            )
        )
    return raws


#: 逐日行缓存最多留多少个交易日（一天约 1000 行，几十 MB；超过就整份清掉）
_ROWS_CACHE_DAYS = 130


def _rows_at(data: _Dataset, index: int) -> list:
    """某一天**去重后**的全量行（带缓存，与预设 / top / hold 都无关）。

    换预设 / 只数 / 持有期时省掉重算（实测 120 天首次约 10 秒、之后约 1 秒）。
    """
    cached = data.rows_cache.get(index)
    if cached is not None:
        return cached
    rows = etf_screen.to_rows(_raws_at(data, index), dedup=True)
    if len(data.rows_cache) >= _ROWS_CACHE_DAYS:
        data.rows_cache.clear()
    data.rows_cache[index] = rows
    return rows


def _forward(series: _Ser | None, entry: int, exit_index: int) -> float | None:
    """从 `entry`（含）之后第一份日线收盘，持有到 `exit_index`（含）之前最后一份。

    `entry` 是「次日」的全局索引：停牌 / 已退市时取它**之后**第一份日线（最早能成交
    的价格），出场取 `exit_index` 之前**最后一份**（退市按最后一天结算）。
    """
    if series is None:
        return None
    begin = bisect_left(series.pos, entry)
    end = bisect_right(series.pos, exit_index) - 1
    if begin >= len(series.pos) or end <= begin:
        return None
    return round((exp(series.L[end + 1] - series.L[begin + 1]) - 1) * 100, 2)


def _stat(values: list[float]) -> dict:
    if not values:
        return {
            "samples": 0,
            "mean": None,
            "median": None,
            "win_rate": None,
            "total": None,
            "best": None,
            "worst": None,
        }
    total = 1.0
    for value in values:
        total *= 1 + value / 100
    return {
        "samples": len(values),
        "mean": round(sum(values) / len(values), 2),
        "median": round(statistics.median(values), 2),
        "win_rate": round(sum(1 for value in values if value > 0) / len(values) * 100, 1),
        "total": round((total - 1) * 100, 2),
        "best": round(max(values), 2),
        "worst": round(min(values), 2),
    }


def _cumulative(values: list[float | None]) -> list[float | None]:
    """逐期复利累计（None 的期跳过，累计器不动 —— 与 `etf_flow._cumulative` 同语义）。"""
    result: list[float | None] = []
    acc = 1.0
    for value in values:
        if value is None:
            result.append(None)
            continue
        acc *= 1 + value / 100
        result.append(round((acc - 1) * 100, 2))
    return result


def track(
    session: Session,
    preset: str = "momentum",
    top: int = 5,
    hold: int = 5,
    days: int = DEFAULT_DAYS,
) -> Track:
    """近 `days` 个交易日按 `preset` 每天选前 `top` 只，次日收盘起持有 `hold` 天。"""
    if preset not in etf_screen.PRESETS:
        raise ValueError(f"未知预设：{preset}")
    data = _dataset(session)
    if data is None or len(data.days) < 2:
        return Track(preset, top, hold, days, None, "还没有 ETF 日线", [], [], {})

    last = len(data.days) - 1
    start = max(MIN_SIGNAL_BARS, last - max(1, days) + 1)
    rows: list[Day] = []
    pending = 0
    empty = 0
    no_scale_days = 0
    for index in range(start, last + 1):
        if index + 1 + hold > last:
            pending += 1
            continue
        # 份额快照之前没有规模数据：那段时间跳过规模门槛（不拿后来的份额倒推）
        skip_scale = data.first_share_day is None or data.days[index] < data.first_share_day
        universe = _rows_at(data, index)
        if not universe:
            empty += 1
            continue
        picked = etf_screen.select_rows(universe, preset, top, skip_scale=skip_scale)
        if not picked:
            empty += 1
            continue
        if skip_scale:
            no_scale_days += 1
        forward = {
            row.code: _forward(data.series.get(row.code), index + 1, index + 1 + hold)
            for row in universe
        }
        picks = [Pick(code=row.code, name=row.name, ret=forward.get(row.code)) for row in picked]
        values = [pick.ret for pick in picks if pick.ret is not None]
        universe_values = [value for value in forward.values() if value is not None]
        rows.append(
            Day(
                trade_date=data.days[index],
                entry_date=data.days[index + 1],
                exit_date=data.days[index + 1 + hold],
                ret=round(sum(values) / len(values), 2) if values else None,
                hs300_ret=_forward(data.series.get(BENCH_CODE), index + 1, index + 1 + hold),
                universe_ret=(
                    round(sum(universe_values) / len(universe_values), 2)
                    if universe_values
                    else None
                ),
                picks=picks,
            )
        )

    strategy = [row.ret for row in rows]
    hs300 = [row.hs300_ret for row in rows]
    universe = [row.universe_ret for row in rows]
    strategy_curve = _cumulative(strategy)
    hs300_curve = _cumulative(hs300)
    universe_curve = _cumulative(universe)
    curve = [
        {
            "trade_date": row.trade_date,
            "strategy": strategy_curve[index],
            "hs300": hs300_curve[index],
            "universe": universe_curve[index],
        }
        for index, row in enumerate(rows)
    ]

    notes: list[str] = []
    if preset == "inflow":
        notes.append(
            "资金流入依赖净申赎，份额 2026-09-21 才有、再等 20 个交易日，"
            "可验证样本比其它预设少一截"
        )
    if no_scale_days:
        notes.append(
            f"{no_scale_days} 个交易日早于份额快照（{data.first_share_day}），"
            "那段时间没有规模数据，只按 20 日均额筛（不拿后来的份额倒推）"
        )
    if pending:
        notes.append(f"最近 {pending} 个交易日还没满 {hold} 日持有期，未计入")
    if empty:
        notes.append(f"{empty} 个交易日没有符合条件（或当天无日线）的标的，未计入")
    if not rows:
        notes.append("样本还不够：至少要有 1 个完整持有期")
    return Track(
        preset=preset,
        top=top,
        hold=hold,
        days=days,
        as_of=data.days[-1],
        note="；".join(notes) or None,
        rows=rows,
        curve=curve,
        stats={
            "strategy": _stat([value for value in strategy if value is not None]),
            "hs300": _stat([value for value in hs300 if value is not None]),
            "universe": _stat([value for value in universe if value is not None]),
        },
    )


def rotation(
    session: Session,
    preset: str = "momentum",
    top: int = 5,
    step: int = 5,
    days: int = MAX_DAYS,
    cost: float = DEFAULT_COST,
) -> Rotation:
    """动量轮动回测：每 `step` 个交易日在**当时**的预设结果里取前 `top` 只等权换仓。

    调仓日 `t` 收盘出信号 → `t+1` 收盘换仓 → 持有到 `t+1+step` 收盘；下一个调仓日
    是 `t+step`，所以两段仓位首尾相接、不重叠。成本按**单边** `cost` 计，换手率 =
    新买入权重 + 卖出的旧权重（全换仓 = 2 → 一来一回付两次万 0.5）；ETF 无印花税。
    """
    if preset not in etf_screen.PRESETS:
        raise ValueError(f"未知预设：{preset}")
    data = _dataset(session)
    if data is None or len(data.days) < step + 2:
        return Rotation(preset, top, step, cost, [], {}, {})
    last = len(data.days) - 1
    start = max(MIN_SIGNAL_BARS, last - max(1, min(days, MAX_DAYS)) + 1)
    periods: list[dict] = []
    previous: set[str] | None = None
    strategy_raw: list[float] = []
    bench_raw: list[float] = []
    hs300_raw: list[float] = []
    for index in range(start, last - step, step):
        # 份额快照之前没有规模数据 → 那段时间跳过规模门槛（与 `track` 同一口径）
        skip_scale = data.first_share_day is None or data.days[index] < data.first_share_day
        universe = _rows_at(data, index)
        if not universe:
            continue
        picked = etf_screen.select_rows(universe, preset, top, skip_scale=skip_scale)
        if not picked:
            continue
        forward = {
            row.code: _forward(data.series.get(row.code), index + 1, index + 1 + step)
            for row in universe
        }
        values = [forward[row.code] for row in picked if forward.get(row.code) is not None]
        if not values:
            continue
        gross = sum(values) / len(values)
        codes = {row.code for row in picked}
        if previous is None:
            turnover = 1.0
        elif previous:
            turnover = len(codes - previous) / len(codes) + len(previous - codes) / len(previous)
        else:
            turnover = 1.0
        net = gross - cost * turnover * 100
        previous = codes
        universe_values = [value for value in forward.values() if value is not None]
        benchmark = sum(universe_values) / len(universe_values) if universe_values else None
        hs300 = _forward(data.series.get(BENCH_CODE), index + 1, index + 1 + step)
        strategy_raw.append(net)
        if benchmark is not None:
            bench_raw.append(benchmark)
        if hs300 is not None:
            hs300_raw.append(hs300)
        periods.append(
            {
                "trade_date": data.days[index],
                "entry_date": data.days[index + 1],
                "exit_date": data.days[index + 1 + step],
                "gross": round(gross, 2),
                "cost": round(cost * turnover * 100, 3),
                "ret": round(net, 2),
                "universe_ret": None if benchmark is None else round(benchmark, 2),
                "hs300_ret": hs300,
                "picks": [row.code for row in picked],
            }
        )
    return Rotation(
        preset=preset,
        top=top,
        step=step,
        cost=cost,
        periods=periods,
        stats=_stat(strategy_raw),
        bench={
            "universe": _stat(bench_raw),
            "hs300": _stat(hs300_raw),
            "excess": _stat(
                [
                    period["ret"] - period["universe_ret"]
                    for period in periods
                    if period["universe_ret"] is not None
                ]
            ),
        },
    )
