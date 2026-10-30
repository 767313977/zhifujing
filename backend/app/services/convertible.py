"""可转债的派生指标与筛选（2026-10-30 加）。

全部是「读库 + 纯计算」，不联网、不写库，便于单测：

- 双低 = 现价 + 转股溢价率（两者都是 % 口径，不再 ×100）；
- 剩余年限 = (到期日 − 交易日) / 365；
- 到期 YTM：用票息原文 + 到期赎回价做二分求 IRR，票息没补齐就是 None（**不拿 0 顶**）；
- 强赎 / 下修 / 回售计数：近 30 个交易日里 `conv_value` 越过条款阈值的天数。
  ⚠️ 用**转股价值**而不是「正股收盘 ≥ 130% × 转股价」：两者等价（转股价值就是按
  当天生效的转股价算的），而正股日线我们只覆盖 `stock_universe` 里的票，转债正股
  有一半不在里面 —— 用转股价值就不受这个影响；
- 计数窗口只要缺一天，就标成「≥n」而不是精确值（页面同理）。

状态与风险分成两类，**风险只提示、不默认剔除**（筛选条件里可以开 `exclude_st`）：

- 状态标签：已公告强赎 / 承诺不强赎 / 强赎临近 / 下修临近；
- 风险标签：正股 ST、正股 < 2 元、评级 ≤ A+、剩余规模 < 1 亿、成交额 < 1000 万、
  未进转股期、剩余年限 < 0.5 年。
"""

import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CbBasic, CbDaily, TradeCalendar

#: 计数窗口（交易日）
COUNT_WINDOW = 30
#: 强赎 / 下修「临近」的计数门槛（条款是 15/30，这里给一个更早的预警值）
NEAR_COUNT = 10
#: 回售期：到期前这么多自然日进入（常见条款是「最后两个计息年度」）
RESALE_YEARS_BEFORE = 2

#: 评级 → 序。`AA+sti` 这类定向转债按 `AA+` 算
RATING_RANK = {
    "AAA": 7,
    "AA+": 6,
    "AA": 5,
    "AA-": 4,
    "A+": 3,
    "A": 2,
    "A-": 1,
    "BBB+": 0,
    "BBB": -1,
    "BBB-": -2,
}

#: 预设 → 中文名。页面上的 Segmented 直接用这份
PRESETS = {
    "t0": "日内 T+0",
    "double_low": "双低",
    "low_premium": "低溢价",
    "bond_floor": "债底（YTM）",
    "revise": "博下修",
}

#: T+0 打分权重（只有 `t0` 预设用）。缺项时按剩余权重归一
T0_WEIGHTS = {
    "stock_pct": 0.30,
    "turnover": 0.25,
    "premium": 0.20,
    "amplitude": 0.15,
    "size": 0.10,
}


@dataclass(frozen=True, slots=True)
class CbScreenRow:
    bond_code: str
    bond_name: str | None
    stock_code: str | None
    stock_name: str | None
    close: float | None
    pct_chg: float | None
    premium_pct: float | None
    double_low: float | None
    conv_value: float | None
    conv_price: float | None
    ytm_pct: float | None
    remain_size: float | None
    issue_size: float | None
    years_left: float | None
    rating: str | None
    amount: float | None
    turnover: float | None
    stock_close: float | None
    stock_pct_chg: float | None
    redeem_count: int | None
    revise_count: int | None
    resale_count: int | None
    #: 计数窗口是否完整（缺天时页面显示「≥n」）
    count_complete: bool
    redeem_status: str | None
    last_trade_date: date | None
    labels: list[str]
    risks: list[str]
    #: 只有 `t0` 预设会算分，其余预设为 None
    score: float | None


@dataclass(frozen=True, slots=True)
class CbScreen:
    as_of: date | None
    #: 实际生效的筛选条件（原样回给页面，照 `pools` 的做法）
    filters: dict
    items: list[CbScreenRow] = field(default_factory=list)
    #: 过完条件、截断**之前**的条数
    total: int = 0


# ---------------------------------------------------------------- 纯计算


def rating_rank(rating: str | None) -> int | None:
    if not rating:
        return None
    return RATING_RANK.get(str(rating).strip().upper().removesuffix("STI"))


def years_left(maturity: date | None, day: date) -> float | None:
    if maturity is None:
        return None
    return round((maturity - day).days / 365, 2)


def double_low(close: float | None, premium_pct: float | None) -> float | None:
    """双低 = 现价 + 转股溢价率（%）。两个口径都是 %，不再 ×100。"""
    if close is None or premium_pct is None:
        return None
    return round(close + premium_pct, 2)


_COUPON_RE = re.compile(r"第[一二三四五六七八九十]年[^0-9]{0,6}(\d+(?:\.\d+)?)\s*%")
_COUPON_ORDER = "一二三四五六七八九十"


def parse_coupons(text: str | None) -> list[float]:
    """从利率说明里解析出逐年票息（%）。

    实测原文（上银转债）：「本次发行可转债票面利率:第一年为0.30%、第二年为0.80%、
    第三年为1.50%、第四年为2.80%、第五年为3.50%、第六年为4.00%。」

    解析不出来就返回空列表 —— 调用方据此把 YTM 记成 None，**不拿 0 顶**。
    """
    if not text:
        return []
    found: dict[int, float] = {}
    for match in _COUPON_RE.finditer(text):
        year_char = match.group(0)[1]
        index = _COUPON_ORDER.find(year_char)
        if index >= 0:
            found[index] = float(match.group(1))
    if not found:
        return []
    # 只认从第 1 年连续排下来的，缺年份就整条丢掉（半份票息算出来的 YTM 是错的）
    size = max(found) + 1
    if len(found) != size:
        return []
    return [found[index] for index in range(size)]


def ytm(
    price: float | None,
    day: date,
    maturity: date | None,
    coupons: list[float],
    call_price: float | None,
) -> float | None:
    """到期 YTM（%，年化）。现价视作全价，先不考虑税与手续费。

    现金流：剩余的每一期票息（面值 100 × 票息率），最后一期用**到期赎回价**
    （它含最后一期利息，所以不再叠加票息）。用二分法求 IRR。
    """
    if price is None or price <= 0 or maturity is None or call_price is None:
        return None
    if not coupons:
        return None
    years = (maturity - day).days / 365
    if years <= 0:
        return None
    periods = max(1, math.ceil(years))
    rates = coupons[-periods:]
    if len(rates) != periods:
        return None
    amounts: list[float] = [100 * rate / 100 for rate in rates[:-1]]
    times: list[float] = [years - (periods - 1 - index) for index in range(periods - 1)]
    amounts.append(call_price)
    times.append(years)

    def npv(rate: float) -> float:
        return sum(amount / (1 + rate) ** time for amount, time in zip(amounts, times)) - price

    low, high = -0.99, 1.0
    f_low, f_high = npv(low), npv(high)
    if f_low * f_high > 0:
        return None
    for _ in range(80):
        mid = (low + high) / 2
        value = npv(mid)
        if abs(value) < 1e-6:
            return round(mid * 100, 2)
        if value * f_low < 0:
            high = mid
        else:
            low, f_low = mid, value
    return round((low + high) / 2 * 100, 2)


# ---------------------------------------------------------------- 计数窗口


def _window_days(session: Session, day: date, size: int = COUNT_WINDOW) -> list[date]:
    """`day` 及之前最近 `size` 个交易日（由近及远）。取不到那么长就给实际有的。"""
    return list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= day)
            .order_by(TradeCalendar.trade_date.desc())
            .limit(size)
        )
    )


@dataclass(frozen=True, slots=True)
class Counts:
    redeem: int
    revise: int
    resale: int | None
    #: 窗口里的交易日是否一个不缺（缺了就只能说「≥n」）
    complete: bool


def _counts(
    session: Session,
    day: date,
    codes: set[str],
    basics: dict[str, CbBasic],
) -> dict[str, Counts]:
    """近 30 日的强赎 / 下修 / 回售计数。

    只查一次库：把窗口内所有券的 `conv_value` 拉回来，在内存里按券聚合。
    """
    days = _window_days(session, day)
    if not days or not codes:
        return {}
    rows = session.execute(
        select(CbDaily.bond_code, CbDaily.trade_date, CbDaily.conv_value).where(
            CbDaily.trade_date.in_(days)
        )
    ).all()
    values: dict[str, dict[date, float]] = {}
    for code, trade_date, conv_value in rows:
        if conv_value is None:
            continue
        values.setdefault(str(code), {})[trade_date] = float(conv_value)

    size = len(days)
    result: dict[str, Counts] = {}
    for code in codes:
        series = values.get(code, {})
        basic = basics.get(code)
        redeem_pct = (basic.redeem_trigger_pct if basic else None) or 130.0
        revise_pct = (basic.revise_trigger_pct if basic else None) or 85.0
        resale_pct = basic.resale_trigger_pct if basic else None
        redeem = sum(1 for value in series.values() if value >= redeem_pct)
        revise = sum(1 for value in series.values() if value <= revise_pct)
        resale: int | None = None
        if resale_pct is not None and basic and basic.maturity_date:
            in_resale = basic.maturity_date - timedelta(days=365 * RESALE_YEARS_BEFORE)
            if day >= in_resale:
                resale = sum(1 for value in series.values() if value <= resale_pct)
        result[code] = Counts(
            redeem=redeem,
            revise=revise,
            resale=resale,
            complete=len(series) >= size,
        )
    return result


# ---------------------------------------------------------------- 标签


def _labels(
    basic: CbBasic | None,
    daily: CbDaily,
    counts: Counts | None,
) -> tuple[list[str], list[str]]:
    """状态标签与风险标签。状态只提示「可能出事」，风险只提示「格外小心」。"""
    labels: list[str] = []
    risks: list[str] = []

    status = daily.redeem_status or ""
    if "强赎" in status and "不强赎" not in status:
        labels.append("已公告强赎")
    elif "不强赎" in status:
        labels.append("承诺不强赎")
    if counts is not None and counts.complete:
        if counts.redeem >= NEAR_COUNT and "已公告强赎" not in labels:
            labels.append(f"强赎临近 {counts.redeem}/30")
        if counts.revise >= NEAR_COUNT:
            labels.append(f"下修临近 {counts.revise}/30")

    name = f"{basic.stock_name if basic else ''}{basic.bond_name if basic else ''}".upper()
    if "ST" in name or "退" in name:
        risks.append("正股 ST")
    if daily.stock_close is not None and daily.stock_close < 2:
        risks.append("正股 < 2 元")
    rank = rating_rank(basic.rating if basic else None)
    if rank is not None and rank <= RATING_RANK["A+"]:
        risks.append("评级 ≤ A+")
    if daily.remain_size is not None and daily.remain_size < 1:
        risks.append("剩余 < 1 亿")
    if daily.amount is not None and daily.amount < 10_000_000:
        risks.append("成交额 < 1000 万")
    if basic and basic.convert_start_date and basic.convert_start_date > daily.trade_date:
        risks.append("未进转股期")
    years = years_left(basic.maturity_date if basic else None, daily.trade_date)
    if years is not None and years < 0.5:
        risks.append("剩余 < 0.5 年")
    return labels, risks


# ---------------------------------------------------------------- 筛选


def _median_rank(values: list[float], value: float) -> float:
    """中位秩分位（0~100）。与恐贪那套口径一致。"""
    if not values:
        return 50.0
    below = sum(1 for item in values if item < value)
    equal = sum(1 for item in values if item == value)
    return (below + equal / 2) / len(values) * 100


def _t0_score(rows: list[dict]) -> None:
    """就地给 `t0` 预设的行打分（分位在**当天池内**算，缺项按剩余权重归一）。"""
    def column(key: str) -> list[float]:
        return [float(row[key]) for row in rows if row.get(key) is not None]

    stock_values = column("stock_pct_chg")
    turnover_values = column("turnover")
    premium_values = column("premium_pct")
    amplitude_values = column("amplitude")
    size_values = column("remain_size")

    for row in rows:
        parts: dict[str, float] = {}
        if row["stock_pct_chg"] is not None:
            parts["stock_pct"] = _median_rank(stock_values, row["stock_pct_chg"])
        if row["turnover"] is not None:
            parts["turnover"] = _median_rank(turnover_values, row["turnover"])
        if row["premium_pct"] is not None:
            parts["premium"] = 100 - _median_rank(premium_values, row["premium_pct"])
        if row["amplitude"] is not None:
            parts["amplitude"] = _median_rank(amplitude_values, row["amplitude"])
        if row["remain_size"] is not None:
            parts["size"] = 100 - _median_rank(size_values, row["remain_size"])
        weight = sum(T0_WEIGHTS[key] for key in parts)
        row["score"] = (
            round(sum(T0_WEIGHTS[key] * value for key, value in parts.items()) / weight, 1)
            if weight
            else None
        )


#: 各预设的默认条件（页面可以覆盖；覆盖后的实际值会原样回给页面）
_PRESET_FILTERS: dict[str, dict] = {
    "t0": {
        "price_min": 100.0,
        "price_max": 150.0,
        "premium_max": 30.0,
        "size_min": 3.0,
        "years_min": 1.0,
        "rating_min": "AA-",
        "amount_min": 50_000_000.0,
        "exclude_redeem": True,
        "exclude_st": True,
        "exclude_not_convertible": False,
        "min_listed_days": 5,
        "sort": "score",
    },
    "double_low": {
        "exclude_redeem": True,
        "exclude_st": True,
        "sort": "double_low",
    },
    "low_premium": {
        "price_max": 130.0,
        "exclude_redeem": True,
        "sort": "premium",
    },
    "bond_floor": {
        "ytm_min": 0.0,
        "exclude_redeem": True,
        "sort": "ytm",
    },
    "revise": {
        "revise_min": NEAR_COUNT,
        "years_max": 3.0,
        "exclude_redeem": True,
        "sort": "revise",
    },
}

#: 排序键 → (字段, 是否升序)
_SORTS = {
    "score": ("score", False),
    "double_low": ("double_low", True),
    "premium": ("premium_pct", True),
    "ytm": ("ytm_pct", False),
    "revise": ("revise_count", False),
    "amount": ("amount", False),
    "price": ("close", False),
    "size": ("remain_size", False),
    "years": ("years_left", False),
}


def _passes(row: dict, filters: dict) -> bool:
    """逐条判断。缺数据（None）**不通过需要该字段的条件** —— 宁缺勿滥。"""
    checks = (
        ("price_min", "close", lambda value, limit: value >= limit),
        ("price_max", "close", lambda value, limit: value <= limit),
        ("premium_max", "premium_pct", lambda value, limit: value <= limit),
        ("size_min", "remain_size", lambda value, limit: value >= limit),
        ("size_max", "remain_size", lambda value, limit: value <= limit),
        ("years_min", "years_left", lambda value, limit: value >= limit),
        ("years_max", "years_left", lambda value, limit: value <= limit),
        ("amount_min", "amount", lambda value, limit: value >= limit),
        ("ytm_min", "ytm_pct", lambda value, limit: value >= limit),
        ("revise_min", "revise_count", lambda value, limit: value >= limit),
    )
    for key, field_name, compare in checks:
        limit = filters.get(key)
        if limit is None:
            continue
        value = row.get(field_name)
        if value is None or not compare(value, limit):
            return False

    rating_min = filters.get("rating_min")
    if rating_min:
        floor = rating_rank(rating_min)
        value = rating_rank(row.get("rating"))
        if floor is None or value is None or value < floor:
            return False

    if filters.get("exclude_redeem") and "已公告强赎" in row["labels"]:
        return False
    if filters.get("exclude_st") and "正股 ST" in row["risks"]:
        return False
    if filters.get("exclude_not_convertible") and "未进转股期" in row["risks"]:
        return False
    min_listed = filters.get("min_listed_days")
    if min_listed and row.get("_listed_days") is not None and row["_listed_days"] < min_listed:
        return False
    return True


def screen(session: Session, day: date | None = None, **overrides) -> CbScreen:
    """按预设 + 覆盖参数筛一遍，返回行与**实际生效**的条件。

    参数为 None 的项**不覆盖**预设默认值；显式传 `False` / `0` 会覆盖（例如
    `exclude_redeem=False`）。这是页面「关掉某个开关」与「没填」的区别。
    """
    preset = overrides.pop("preset", None) or "t0"
    filters = dict(_PRESET_FILTERS.get(preset, _PRESET_FILTERS["t0"]))
    for key, value in overrides.items():
        if value is not None:
            filters[key] = value
    filters["preset"] = preset

    if day is None:
        day = session.scalar(select(CbDaily.trade_date).order_by(CbDaily.trade_date.desc()))
    if day is None:
        return CbScreen(as_of=None, filters=filters)

    dailies = list(session.scalars(select(CbDaily).where(CbDaily.trade_date == day)))
    if not dailies:
        return CbScreen(as_of=None, filters=filters)

    basics = {
        row.bond_code: row for row in session.scalars(select(CbBasic))
    }
    counts = _counts(session, day, {row.bond_code for row in dailies}, basics)

    rows: list[dict] = []
    for daily in dailies:
        basic = basics.get(daily.bond_code)
        labels, risks = _labels(basic, daily, counts.get(daily.bond_code))
        counter = counts.get(daily.bond_code)
        rows.append(
            {
                "bond_code": daily.bond_code,
                "bond_name": basic.bond_name if basic else None,
                "stock_code": basic.stock_code if basic else None,
                "stock_name": basic.stock_name if basic else None,
                "close": daily.close,
                "pct_chg": daily.pct_chg,
                "premium_pct": daily.premium_pct,
                "double_low": double_low(daily.close, daily.premium_pct),
                "conv_value": daily.conv_value,
                "conv_price": daily.conv_price,
                "ytm_pct": daily.ytm_pct,
                "remain_size": daily.remain_size,
                "issue_size": basic.issue_size if basic else None,
                "years_left": years_left(basic.maturity_date if basic else None, day),
                "rating": basic.rating if basic else None,
                "amount": daily.amount,
                "turnover": daily.turnover,
                "amplitude": daily.amplitude,
                "stock_close": daily.stock_close,
                "stock_pct_chg": daily.stock_pct_chg,
                "redeem_count": counter.redeem if counter else 0,
                "revise_count": counter.revise if counter else 0,
                "resale_count": counter.resale if counter else None,
                "count_complete": bool(counter and counter.complete),
                "redeem_status": daily.redeem_status,
                "last_trade_date": daily.last_trade_date,
                "labels": labels,
                "risks": risks,
                "score": None,
                "_listed_days": (day - basic.list_date).days if basic and basic.list_date else None,
            }
        )

    passed = [row for row in rows if _passes(row, filters)]
    if preset == "t0":
        _t0_score(passed)

    key, ascending = _SORTS.get(filters.get("sort") or "score", _SORTS["score"])

    def sort_value(row: dict):
        """缺数据一律排最后，不随升降序翻转（与前端 `lib/sort.ts` 同一口径）。"""
        value = row.get(key)
        if value is None:
            return (1, 0.0)
        return (0, float(value) if ascending else -float(value))

    passed.sort(key=sort_value)

    limit = int(filters.get("limit") or 200)
    total = len(passed)
    items = [_row_out(row) for row in passed[:limit]]
    return CbScreen(as_of=day, filters=filters, items=items, total=total)


def _row_out(row: dict) -> CbScreenRow:
    return CbScreenRow(
        bond_code=row["bond_code"],
        bond_name=row["bond_name"],
        stock_code=row["stock_code"],
        stock_name=row["stock_name"],
        close=row["close"],
        pct_chg=row["pct_chg"],
        premium_pct=row["premium_pct"],
        double_low=row["double_low"],
        conv_value=row["conv_value"],
        conv_price=row["conv_price"],
        ytm_pct=row["ytm_pct"],
        remain_size=row["remain_size"],
        issue_size=row["issue_size"],
        years_left=row["years_left"],
        rating=row["rating"],
        amount=row["amount"],
        turnover=row["turnover"],
        stock_close=row["stock_close"],
        stock_pct_chg=row["stock_pct_chg"],
        redeem_count=row["redeem_count"],
        revise_count=row["revise_count"],
        resale_count=row["resale_count"],
        count_complete=row["count_complete"],
        redeem_status=row["redeem_status"],
        last_trade_date=row["last_trade_date"],
        labels=row["labels"],
        risks=row["risks"],
        score=row["score"],
    )


@dataclass(frozen=True, slots=True)
class RadarRow:
    bond_code: str
    bond_name: str | None
    stock_code: str | None
    stock_name: str | None
    close: float | None
    premium_pct: float | None
    redeem_status: str | None
    last_trade_date: date | None
    #: 距最后交易日的自然日数（负数 = 已经过了）
    days_left: int | None
    redeem_count: int
    count_complete: bool


def radar(session: Session, day: date | None = None) -> tuple[date | None, list[RadarRow]]:
    """强赎雷达：已公告的按最后交易日升序，临近的按计数降序。

    这是**防踩坑**的一栏，不是选股：强赎公告之后按约 100 元被赎回，没转股的人
    会在几天里亏掉 20%~30%。
    """
    if day is None:
        day = session.scalar(select(CbDaily.trade_date).order_by(CbDaily.trade_date.desc()))
    if day is None:
        return None, []
    dailies = list(session.scalars(select(CbDaily).where(CbDaily.trade_date == day)))
    basics = {row.bond_code: row for row in session.scalars(select(CbBasic))}
    counts = _counts(session, day, {row.bond_code for row in dailies}, basics)

    rows: list[RadarRow] = []
    for daily in dailies:
        counter = counts.get(daily.bond_code)
        status = daily.redeem_status or ""
        announced = "强赎" in status and "不强赎" not in status
        near = bool(counter and counter.complete and counter.redeem >= NEAR_COUNT)
        if not announced and not near and "不强赎" not in status:
            continue
        basic = basics.get(daily.bond_code)
        days = (daily.last_trade_date - day).days if daily.last_trade_date else None
        rows.append(
            RadarRow(
                bond_code=daily.bond_code,
                bond_name=basic.bond_name if basic else None,
                stock_code=basic.stock_code if basic else None,
                stock_name=basic.stock_name if basic else None,
                close=daily.close,
                premium_pct=daily.premium_pct,
                redeem_status=daily.redeem_status,
                last_trade_date=daily.last_trade_date,
                days_left=days,
                redeem_count=counter.redeem if counter else 0,
                count_complete=bool(counter and counter.complete),
            )
        )

    def sort_key(row: RadarRow):
        announced = bool(
            row.redeem_status
            and "强赎" in row.redeem_status
            and "不强赎" not in row.redeem_status
        )
        # 已公告：按最后交易日升序（越紧急越前）；其余按强赎计数降序
        if announced:
            return (0, row.last_trade_date or date.max, 0)
        return (1, date.max, -row.redeem_count)

    rows.sort(key=sort_key)
    return day, rows
