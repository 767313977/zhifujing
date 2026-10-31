"""ETF 筛选与派生指标（2026-10-31 加，ETF-2）。

全部是「读库 + 纯计算」，不联网、不写库，便于单测：

- 价格序列一律**按 `pct_chg` 复利**（与站内 `stock_daily` 同一口径），不直接除 `close`
  —— 红利 ETF 除息那天 `close` 会跳一下，直接相除会算成大跌；
- 20 日均额用 `etf_bar.amount`（流动性门槛看均额，不看单日成交额）；
- RPS 只在**去重后的股票类 ETF** 里算：沪深300 有二十几只，不去重会把排名挤歪；
- 净申赎直接调 `etf_flow`（含货基剔除、折算日跳过），本模块不自己差分；
- 规模 = `etf_share.shares × close`；份额缺失就是 None（**不拿份额 0 顶**）。

价格口径的一个实测依据：2026-10-31 用通达信盘后包对 510100（XD 除息）与 159583
（1 拆 2）核对过，`prev_close` 是交易所调整后的前收，所以 `close / prev_close − 1`
当天分别是 +0.43% / +2.83%，按它复利不会出现假跌。
"""

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import EtfBar, EtfBasic, EtfCategory, EtfShare
from app.services import etf_flow

#: 指标窗口（交易日）。250 ≈ 一年，够算「距 250 日高点的回撤」
WINDOW = 250
#: 涨幅窗口
RETURN_WINDOWS = (5, 20, 60, 120)
#: RPS 窗口
RPS_WINDOWS = (20, 60, 120)
#: 窗口里有效行占比低于它就给 None（照 `rps.MIN_COVERAGE`）
MIN_COVERAGE = 0.9
#: 均额 / 波动率窗口
AVG_DAYS = 20
#: 创 N 日新高
NEW_HIGH_DAYS = 60
#: 资金窗口
FLOW_WINDOWS = (5, 20)

#: 预设 → 默认条件。页面切预设时清空覆盖条件，回到这里的口径
PRESETS = {
    "momentum": "动量轮动",
    "breakout": "趋势突破",
    "inflow": "资金逆势流入",
    "defensive": "红利低波",
    "t0": "T+0 品种",
}

_PRESET_FILTERS: dict[str, dict] = {
    "momentum": {
        "fund_type": "股票",
        "scale_min": 2.0,
        "amount_min": 50_000_000.0,
        "above_ma20": True,
        "sort": "rps60",
    },
    "breakout": {"new_high": True, "sort": "amount"},
    "inflow": {"inflow_min": 5.0, "ret20_max": 0.0, "sort": "flow20_ratio"},
    "defensive": {"categories": ("红利策略", "银行"), "sort": "volatility"},
    "t0": {"t0": True, "exclude_qdii": True, "sort": "amount"},
}

#: 排序键 → 取值函数。None 一律排最后（页面上显示「—」的那些）
_SORTS: dict[str, tuple] = {
    "rps60": (lambda row: row.rps60, True),
    "rps20": (lambda row: row.rps20, True),
    "rps120": (lambda row: row.rps120, True),
    "ret20": (lambda row: row.ret20, True),
    "ret60": (lambda row: row.ret60, True),
    "amount": (lambda row: row.amount, True),
    "flow20_ratio": (lambda row: row.flow20_ratio, True),
    "scale": (lambda row: row.scale, True),
    "volatility": (lambda row: row.volatility, False),
}


@dataclass(frozen=True, slots=True)
class EtfRow:
    code: str
    name: str | None
    category: str
    fund_type: str | None
    t0: bool
    close: float | None
    pct_chg: float | None
    ret5: float | None
    ret20: float | None
    ret60: float | None
    ret120: float | None
    rps20: float | None
    rps60: float | None
    rps120: float | None
    ma20: float | None
    ma60: float | None
    #: 距 250 日高点的回撤（%，≤ 0）
    drawdown: float | None
    volatility: float | None
    scale: float | None
    amount: float | None
    avg_amount: float | None
    flow5: float | None
    flow20: float | None
    #: 20 日净申赎 ÷ 规模（%）
    flow20_ratio: float | None
    #: 同类 ETF 只数（去重后 >1 时页面显示「同类 N 只」）
    same_count: int
    new_high: bool
    labels: list[str]
    risks: list[str]


@dataclass(frozen=True, slots=True)
class EtfScreen:
    as_of: date | None
    filters: dict
    items: list[EtfRow] = field(default_factory=list)
    total: int = 0


@dataclass(frozen=True, slots=True)
class _Raw:
    """逐只算完、还没去重的中间结果。"""

    code: str
    name: str | None
    norm: str
    category: str
    fund_type: str | None
    t0: bool
    close: float
    pct_chg: float | None
    amount: float | None
    avg_amount: float | None
    scale: float | None
    returns: dict[int, float | None]
    ma20: float | None
    ma60: float | None
    drawdown: float | None
    volatility: float | None
    flow5: float | None
    flow20: float | None
    flow20_ratio: float | None
    bars: int
    new_high: bool


def _compound(values: list[float | None], size: int) -> float | None:
    """最近 `size` 个交易日的复利涨幅（%）。有效行不足 90% 返回 None。"""
    window = values[-size:]
    if len(window) < size or sum(1 for value in window if value is not None) < size * MIN_COVERAGE:
        return None
    total = 1.0
    for value in window:
        if value is None:
            continue
        total *= 1 + value / 100
    return round((total - 1) * 100, 2)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _volatility(values: list[float | None]) -> float | None:
    window = [value for value in values[-AVG_DAYS:] if value is not None]
    if len(window) < AVG_DAYS * MIN_COVERAGE:
        return None
    mean = sum(window) / len(window)
    variance = sum((value - mean) ** 2 for value in window) / (len(window) - 1)
    return round(math.sqrt(variance) * math.sqrt(250), 2)


def _trade_days(session: Session, end: date, size: int = WINDOW) -> list[date]:
    rows = session.scalars(
        select(EtfBar.trade_date)
        .where(EtfBar.trade_date <= end)
        .distinct()
        .order_by(EtfBar.trade_date.desc())
        .limit(size)
    )
    return sorted(rows)


def _share_rows(session: Session, day: date) -> dict[str, EtfShare]:
    """每只 ETF 截至 `day` 最近一份份额快照。"""
    latest = (
        select(EtfShare.code, func.max(EtfShare.trade_date).label("latest"))
        .where(EtfShare.trade_date <= day)
        .group_by(EtfShare.code)
        .subquery()
    )
    rows = session.scalars(
        select(EtfShare).join(
            latest,
            (EtfShare.code == latest.c.code) & (EtfShare.trade_date == latest.c.latest),
        )
    )
    return {row.code: row for row in rows}


def _load(session: Session, day: date) -> tuple[list[date], dict[str, list[tuple]]]:
    """取窗口内全部 ETF 日线，按代码分组（升序）。"""
    days = _trade_days(session, day)
    if not days:
        return [], {}
    rows = session.execute(
        select(
            EtfBar.code,
            EtfBar.trade_date,
            EtfBar.close,
            EtfBar.pct_chg,
            EtfBar.amount,
        ).where(EtfBar.trade_date.in_(days))
    ).all()
    grouped: dict[str, list[tuple]] = defaultdict(list)
    for code, trade_date, close, pct_chg, amount in rows:
        grouped[str(code)].append((trade_date, close, pct_chg, amount))
    for values in grouped.values():
        values.sort(key=lambda item: item[0])
    return days, grouped


def _raw_rows(session: Session, day: date, days: list[date], grouped: dict) -> list[_Raw]:
    basics = {row.code: row for row in session.scalars(select(EtfBasic))}
    categories = {row.code: row.category for row in session.scalars(select(EtfCategory))}
    shares = _share_rows(session, day)
    money = etf_flow.money_fund_codes(session)

    # 逐只净申赎：只取最近 21 个交易日（5 / 20 日累计够用）
    pairs = [(days[index], days[index - 1]) for index in range(1, len(days))]
    nets = etf_flow.code_nets(session, pairs[-21:], money)

    # 只在「当天有日线」的券里算；`values` 按日期升序，最后一条就是 day
    today_codes = {code for code, values in grouped.items() if values[-1][0] == day}

    # 净申赎逐只取最近 21 个交易日；份额 2026-09-21 才有，缺的日子不进窗口
    recent_days = days[-21:]
    flows: dict[int, dict[str, float | None]] = {window: {} for window in FLOW_WINDOWS}
    for code in today_codes:
        values = [nets.get(flow_day, {}).get(code) for flow_day in recent_days]
        for window in FLOW_WINDOWS:
            available = [value for value in values[-window:] if value is not None]
            # 窗口里有效天数不足就记 None（**不拿 0 顶**）
            flows[window][code] = sum(available) if len(available) >= window else None
    per_code_flow = flows

    result: list[_Raw] = []
    for code in today_codes:
        values = grouped.get(code) or []
        closes = [item[1] for item in values if item[1] is not None]
        pcts = [item[2] for item in values]
        amounts = [item[3] for item in values if item[3] is not None]
        if not closes:
            continue
        latest = values[-1]
        close = float(closes[-1])
        basic = basics.get(code)
        category = categories.get(code) or "其他"
        share = shares.get(code)
        shares_now = float(share.shares) if share and share.shares else None
        scale = shares_now * close / 1e8 if shares_now is not None else None

        ma20 = _mean(closes[-AVG_DAYS:]) if len(closes) >= AVG_DAYS else None
        ma60 = _mean(closes[-60:]) if len(closes) >= 60 else None
        high = max(closes)
        drawdown = round((close / high - 1) * 100, 2) if high else None
        avg_amount = _mean(amounts[-AVG_DAYS:]) if len(amounts) >= AVG_DAYS else None
        flow5 = per_code_flow[5].get(code)
        flow20 = per_code_flow[20].get(code)
        scale_yuan = scale * 1e8 if scale else None
        flow_ratio = (
            round(flow20 / scale_yuan * 100, 2)
            if flow20 is not None and scale_yuan
            else None
        )
        result.append(
            _Raw(
                code=code,
                name=basic.name if basic else None,
                norm=(basic.norm_name or basic.name or code) if basic else code,
                category=category,
                fund_type=basic.fund_type if basic else None,
                t0=bool(basic.t0) if basic else False,
                close=close,
                pct_chg=latest[2],
                amount=latest[3],
                avg_amount=avg_amount,
                scale=scale,
                returns={window: _compound(pcts, window) for window in RETURN_WINDOWS},
                ma20=ma20,
                ma60=ma60,
                drawdown=drawdown,
                volatility=_volatility(pcts),
                flow5=flow5,
                flow20=flow20,
                flow20_ratio=flow_ratio,
                bars=len(values),
                new_high=len(closes) >= NEW_HIGH_DAYS and close >= max(closes[-NEW_HIGH_DAYS:]),
            )
        )
    return result


def _dedup(rows: list[_Raw]) -> tuple[list[_Raw], dict[str, int]]:
    """同指数去重：每组留 20 日均额最大的那只，其余折叠。"""
    groups: dict[str, list[_Raw]] = defaultdict(list)
    for row in rows:
        groups[row.norm].append(row)
    keep: list[_Raw] = []
    counts: dict[str, int] = {}
    for norm, members in groups.items():
        members.sort(key=lambda row: (row.avg_amount is None, -(row.avg_amount or 0)))
        keep.append(members[0])
        counts[norm] = len(members)
    return keep, counts


def _percentile(values: list[float], value: float) -> float:
    below = sum(1 for item in values if item < value)
    equal = sum(1 for item in values if item == value)
    return round((below + equal / 2) / len(values) * 100, 1)


def _rps(rows: list[_Raw]) -> dict[str, dict[int, float | None]]:
    """在**去重后的股票类 ETF** 里算各窗口的涨幅分位（中位秩，0~100）。"""
    universe = [
        row for row in rows if row.fund_type == "股票"
    ] or rows
    result: dict[str, dict[int, float | None]] = {}
    for window in RPS_WINDOWS:
        values = [row.returns[window] for row in universe if row.returns[window] is not None]
        for row in universe:
            value = row.returns[window]
            result.setdefault(row.code, {})[window] = (
                _percentile(values, value)
                if value is not None and len(values) >= 20
                else None
            )
    return result


def _labels(row: _Raw, same_count: int) -> tuple[list[str], list[str]]:
    labels: list[str] = []
    risks: list[str] = []
    if row.t0:
        labels.append("T+0")
    if same_count > 1:
        labels.append(f"同类 {same_count} 只")
    if row.new_high:
        labels.append("创60日新高")
    if row.scale is not None and row.scale < 2:
        risks.append("规模 < 2 亿")
    if row.avg_amount is not None and row.avg_amount < 10_000_000:
        risks.append("均额 < 1000 万")
    if row.bars < 60:
        risks.append("上市不足 60 日")
    if row.flow20 is not None and row.scale and row.scale > 0:
        if row.flow20 / (row.scale * 1e8) <= -0.3:
            risks.append("份额 20 日减少 > 30%")
    return labels, risks


def screen(session: Session, day: date | None = None, **overrides) -> EtfScreen:
    """按预设 + 覆盖条件筛一遍，返回行与**实际生效**的条件。"""
    preset = overrides.pop("preset", None) or "momentum"
    filters = dict(_PRESET_FILTERS.get(preset, _PRESET_FILTERS["momentum"]))
    for key, value in overrides.items():
        if value is not None:
            filters[key] = value
    filters["preset"] = preset

    if day is None:
        day = session.scalar(select(func.max(EtfBar.trade_date)))
    if day is None:
        return EtfScreen(as_of=None, filters=filters)

    days, grouped = _load(session, day)
    raws = _raw_rows(session, day, days, grouped)
    dedup = bool(filters.get("dedup", True))
    if dedup:
        raws, counts = _dedup(raws)
    else:
        raws, counts = raws, {row.norm: 1 for row in raws}
    rps = _rps(raws)

    rows: list[EtfRow] = []
    for raw in raws:
        labels, risks = _labels(raw, counts.get(raw.norm, 1))
        rows.append(
            EtfRow(
                code=raw.code,
                name=raw.name,
                category=raw.category,
                fund_type=raw.fund_type,
                t0=raw.t0,
                close=raw.close,
                pct_chg=raw.pct_chg,
                ret5=raw.returns[5],
                ret20=raw.returns[20],
                ret60=raw.returns[60],
                ret120=raw.returns[120],
                rps20=rps.get(raw.code, {}).get(20),
                rps60=rps.get(raw.code, {}).get(60),
                rps120=rps.get(raw.code, {}).get(120),
                ma20=raw.ma20,
                ma60=raw.ma60,
                drawdown=raw.drawdown,
                volatility=raw.volatility,
                scale=raw.scale,
                amount=raw.amount,
                avg_amount=raw.avg_amount,
                flow5=raw.flow5,
                flow20=raw.flow20,
                flow20_ratio=raw.flow20_ratio,
                same_count=counts.get(raw.norm, 1),
                new_high=raw.new_high,
                labels=labels,
                risks=risks,
            )
        )

    limit = int(filters.get("limit", 200))
    rows = [row for row in rows if _keep(row, filters)]
    rows.sort(key=_sort_key(filters.get("sort") or "rps60"))
    return EtfScreen(as_of=day, filters=filters, items=rows[:limit], total=len(rows))


def _keep(row: EtfRow, filters: dict) -> bool:
    fund_type = filters.get("fund_type")
    if fund_type and row.fund_type != fund_type:
        return False
    if filters.get("t0") and not row.t0:
        return False
    if filters.get("exclude_qdii") and row.fund_type == "QDII":
        return False
    if filters.get("exclude_money") and row.category == "货币现金":
        return False
    if filters.get("exclude_bond") and row.category == "债券":
        return False
    categories = filters.get("categories")
    if categories and row.category not in categories:
        return False
    # 0 视为「不设门槛」（页面清空输入框），别把缺规模的券一起剔掉
    scale_min = filters.get("scale_min")
    if scale_min and (row.scale is None or row.scale < scale_min):
        return False
    amount_min = filters.get("amount_min")
    if amount_min and (row.avg_amount is None or row.avg_amount < amount_min):
        return False
    if filters.get("above_ma20") and (row.ma20 is None or row.close <= row.ma20):
        return False
    if filters.get("new_high"):
        if not row.new_high:
            return False
        if not row.amount or not row.avg_amount or row.amount <= row.avg_amount * 1.5:
            return False
    inflow_min = filters.get("inflow_min")
    if inflow_min is not None and (row.flow20_ratio is None or row.flow20_ratio < inflow_min):
        return False
    ret20_max = filters.get("ret20_max")
    if ret20_max is not None and (row.ret20 is None or row.ret20 > ret20_max):
        return False
    return True


def _sort_key(sort: str):
    getter, desc = _SORTS.get(sort, _SORTS["rps60"])

    def key(row: EtfRow):
        value = getter(row)
        # None 永远排最后；有值时升序/降序按配置
        return (value is None, -(value or 0) if desc else (value or 0))

    return key


@dataclass(frozen=True, slots=True)
class EtfPoint:
    trade_date: date
    close: float | None
    pct_chg: float | None
    #: 当日份额（份）；2026-09-21 之前没有，就是 None
    shares: float | None


@dataclass(frozen=True, slots=True)
class EtfDetail:
    as_of: date | None
    row: EtfRow
    #: 近 250 日走势（升序）
    series: list[EtfPoint]
    #: 同指数（`norm_name` 相同）的其它只
    peers: list[dict]


def detail(session: Session, code: str, day: date | None = None) -> EtfDetail | None:
    """单只 ETF 详情：最新一行指标 + 250 日走势（价格 + 份额）+ 同类列表。"""
    if day is None:
        day = session.scalar(select(func.max(EtfBar.trade_date)))
    if day is None:
        return None
    data = screen(session, day, preset="momentum", dedup=False, limit=5000)
    row = next((item for item in data.items if item.code == code), None)
    if row is None:
        return None

    bars = session.execute(
        select(EtfBar.trade_date, EtfBar.close, EtfBar.pct_chg)
        .where(EtfBar.code == code, EtfBar.trade_date <= day)
        .order_by(EtfBar.trade_date)
    ).all()
    shares = dict(
        session.execute(
            select(EtfShare.trade_date, EtfShare.shares).where(EtfShare.code == code)
        ).all()
    )
    series = [
        EtfPoint(
            trade_date=trade_date,
            close=close,
            pct_chg=pct_chg,
            shares=shares.get(trade_date),
        )
        for trade_date, close, pct_chg in bars[-WINDOW:]
    ]

    basic = session.get(EtfBasic, code)
    norm = (basic.norm_name or basic.name) if basic else None
    by_code = {item.code: item for item in data.items}
    peers: list[dict] = []
    if norm:
        same = session.scalars(
            select(EtfBasic).where(EtfBasic.norm_name == norm, EtfBasic.code != code)
        )
        for other in same:
            item = by_code.get(other.code)
            if item is None:
                continue
            peers.append(
                {
                    "code": item.code,
                    "name": item.name,
                    "scale": item.scale,
                    "ret60": item.ret60,
                    "rps60": item.rps60,
                    "avg_amount": item.avg_amount,
                }
            )
        peers.sort(key=lambda item: item["avg_amount"] or 0, reverse=True)
    return EtfDetail(as_of=day, row=row, series=series, peers=peers)


def categories(session: Session, day: date | None = None) -> list[dict]:
    """按分类汇总：代表 ETF 的 RPS60 中位数、20 日净申赎合计（行业强弱一览）。

    用「去重 + 全分类」的口径，不要套动量预设的门槛 —— 那会把小行业整类筛没。
    """
    data = screen(
        session,
        day,
        preset="momentum",
        fund_type="",
        scale_min=0,
        amount_min=0,
        above_ma20=False,
        limit=2000,
    )
    grouped: dict[str, list[EtfRow]] = defaultdict(list)
    for row in data.items:
        grouped[row.category].append(row)
    result: list[dict] = []
    for name, rows in grouped.items():
        rps_values = sorted(row.rps60 for row in rows if row.rps60 is not None)
        flows = [row.flow20 for row in rows if row.flow20 is not None]
        median = (
            rps_values[len(rps_values) // 2] if rps_values else None
        )
        result.append(
            {
                "category": name,
                "count": len(rows),
                "rps60_median": median,
                "flow20": round(sum(flows), 2) if flows else None,
            }
        )
    result.sort(key=lambda item: (item["rps60_median"] is None, -(item["rps60_median"] or 0)))
    return result
