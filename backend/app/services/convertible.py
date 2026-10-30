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
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CbBasic, CbDaily, TradeCalendar

#: 计数窗口（交易日）
COUNT_WINDOW = 30
#: 强赎 / 下修「临近」的计数门槛（条款是 15/30，这里给一个更早的预警值）
NEAR_COUNT = 10
#: 集思录没给「所需天数」时标签用的分母（常见条款是 15/30，拿 30 当分母会显得还早）
DEFAULT_REDEEM_DAYS = 15
#: 回售期：到期前这么多自然日进入（常见条款是「最后两个计息年度」）
RESALE_YEARS_BEFORE = 2
#: 回售触发比例的默认档位（常见条款是「30 日均价 ≤ 转股价 70%」）。
#: `cb_basic.resale_trigger_pct` 覆盖率很低（条款补采队列只挑缺票息 / 缺到期赎回价的券，
#: 早已补齐的券轮不到，见 `jobs/collect_cb._fill_terms`），缺了按 70 算，
#: 否则几乎所有券都没有回售计数。银行转债没这条款，仍按 None 处理。
DEFAULT_RESALE_PCT = 70.0

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
    #: 强赎计数是否精确（集思录的强赎天计数优先；本地窗口不足时只能给「≥n」）
    redeem_complete: bool
    #: 下修计数是否精确（本地窗口不足时只能给「≥n」）
    revise_complete: bool
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
    #: 当天快照的行情来源。不是 `sina+em+jsl` 就说明走了兜底（页面要挂横幅）
    source: str | None = None


# ---------------------------------------------------------------- 纯计算


def rating_rank(rating: str | None) -> int | None:
    if not rating:
        return None
    return RATING_RANK.get(str(rating).strip().upper().removesuffix("STI"))


def redeem_kind(status: str | None) -> str | None:
    """集思录强赎状态 → 四分类。

    ⚠️ 「已满足强赎条件」里也有「强赎」两个字，但它是**公司还没表态**，
    很多后来会公告不强赎 —— 不能当成已公告强赎去剔除（2026-10-30 复查 P1-1）。
    akshare 实际会给出：已公告强赎 / 公告要强赎 / 公告不强赎 / 已满足强赎条件。
    """
    text = status or ""
    if "不强赎" in text:
        return "no_redeem"
    if "已满足" in text:
        return "met"
    if "强赎" in text:
        return "announced"
    return None


def years_left(maturity: date | None, day: date) -> float | None:
    if maturity is None:
        return None
    return round((maturity - day).days / 365, 2)


def double_low(close: float | None, premium_pct: float | None) -> float | None:
    """双低 = 现价 + 转股溢价率（%）。两个口径都是 %，不再 ×100。"""
    if close is None or premium_pct is None:
        return None
    return round(close + premium_pct, 2)


_COUPON_RE = re.compile(r"第[一二三四五六七八九十1-9]\s*年[^0-9]{0,6}(\d+(?:\.\d+)?)\s*%")
_COUPON_ORDER = "一二三四五六七八九十"


def parse_coupons(text: str | None) -> list[float]:
    """从利率说明里解析出逐年票息（%）。

    实测原文（上银转债）：「本次发行可转债票面利率:第一年为0.30%、第二年为0.80%、
    第三年为1.50%、第四年为2.80%、第五年为3.50%、第六年为4.00%。」

    两种写法都要认：中文序数（第一年 / 第二年）与阿拉伯数字（第1年 / 第2年）——
    只认前者会让一部分券静默拿不到 YTM（复查 P2-6）。

    解析不出来就返回空列表 —— 调用方据此把 YTM 记成 None，**不拿 0 顶**。
    """
    if not text:
        return []
    found: dict[int, float] = {}
    for match in _COUPON_RE.finditer(text):
        year_char = match.group(0)[1]
        if year_char.isdigit():
            index = int(year_char) - 1
        else:
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


def _is_bank_bond(basic: CbBasic | None) -> bool:
    """银行转债没有回售条款（东财 `RESALE_TRIG_PRICE` 给的是 `"-"`）。"""
    return ((basic.stock_name if basic else None) or "").endswith("银行")


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
        if resale_pct is None and basic is not None and not _is_bank_bond(basic):
            resale_pct = DEFAULT_RESALE_PCT
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
    kind = redeem_kind(status)
    if kind == "announced":
        labels.append("已公告强赎")
    elif kind == "met":
        # 只满足条件、公司还没表态：单独一个标签，**不算强赎风险**
        labels.append("已满足强赎条件")
    elif kind == "no_redeem":
        labels.append("承诺不强赎")

    if counts is not None:
        # 集思录的「强赎天计数」是从条款生效起算的完整计数，本地自算要凑满 30 个交易日
        # 才有意义（12 月中旬之前都缺），所以优先用它（复查 P1-4）
        redeem = daily.redeem_days if daily.redeem_days is not None else counts.redeem
        redeem_complete = daily.redeem_days is not None or counts.complete
        # 分母是「触发所需天数」（通常 15），不是 30 —— 12/30 与 12/15 的紧迫感完全不同
        need = daily.redeem_days_required or DEFAULT_REDEEM_DAYS
        if redeem >= NEAR_COUNT and kind not in ("announced", "met"):
            labels.append(
                f"强赎临近 {redeem}/{need}" if redeem_complete else f"强赎临近 ≥{redeem}/{need}"
            )
        if counts.revise >= NEAR_COUNT:
            labels.append(
                f"下修临近 {counts.revise}/30"
                if counts.complete
                else f"下修临近 ≥{counts.revise}/30"
            )

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
                "redeem_count": (
                    daily.redeem_days
                    if daily.redeem_days is not None
                    else (counter.redeem if counter else 0)
                ),
                "revise_count": counter.revise if counter else 0,
                "resale_count": counter.resale if counter else None,
                # 强赎计数优先用集思录（从条款生效起算，第一天就完整）；下修只能自算
                "redeem_complete": bool(
                    daily.redeem_days is not None or (counter and counter.complete)
                ),
                "revise_complete": bool(counter and counter.complete),
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
    # 同一天可能先走兜底、后来新浪恢复，两种来源混在一起 —— 取**出现最多**的那个，
    # 而不是第一行的（2026-10-30 复查）
    sources = Counter(row.source for row in dailies if row.source)
    source = sources.most_common(1)[0][0] if sources else None
    return CbScreen(
        as_of=day,
        filters=filters,
        items=items,
        total=total,
        source=source,
    )


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
        redeem_complete=row["redeem_complete"],
        revise_complete=row["revise_complete"],
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
    redeem_complete: bool


def radar(session: Session, day: date | None = None) -> tuple[date | None, list[RadarRow]]:
    """强赎雷达：已公告的按最后交易日升序，其后是「已满足条件」与「临近」。

    这是**防踩坑**的一栏，不是选股：强赎公告之后按约 100 元被赎回，没转股的人
    会在几天里亏掉 20%~30%。

    「已满足强赎条件」单独排在已公告之后 —— 它是公司还没表态，和「已公告」不是一回事
    （复查 P1-1）。
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
        kind = redeem_kind(daily.redeem_status)
        redeem = daily.redeem_days if daily.redeem_days is not None else (
            counter.redeem if counter else 0
        )
        complete = daily.redeem_days is not None or bool(counter and counter.complete)
        # 窗口不完整时计数是**下界**：已经 ≥10 就一定 ≥10，没有理由不显示（复查 P1-4）
        near = redeem >= NEAR_COUNT and kind not in ("announced", "met")
        if kind is None and not near:
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
                redeem_count=redeem,
                redeem_complete=complete,
            )
        )

    #: 已公告（0）→ 已满足条件（1）→ 临近（2）→ 承诺不强赎（3）
    priority = {"announced": 0, "met": 1, None: 2, "no_redeem": 3}

    def sort_key(row: RadarRow):
        kind = redeem_kind(row.redeem_status)
        rank = priority.get(kind, 3)
        if rank == 0:
            # 已公告：按最后交易日升序（越紧急越前）
            return (0, row.last_trade_date or date.max, 0)
        return (rank, date.max, -row.redeem_count)

    rows.sort(key=sort_key)
    return day, rows


# ---------------------------------------------------------------- 个股联动


@dataclass(frozen=True, slots=True)
class StockBond:
    """个股详情页「存续转债」小卡片的一行（可转债-5）。"""

    bond_code: str
    bond_name: str | None
    close: float | None
    pct_chg: float | None
    premium_pct: float | None
    conv_value: float | None
    redeem_status: str | None
    last_trade_date: date | None
    #: 一句话状态（已公告强赎 / 已满足强赎条件 / 承诺不强赎 / 强赎临近 n/m），没有就 None
    state: str | None


def _state_text(daily: CbDaily) -> str | None:
    """卡片上的一句话状态。口径与筛选页标签一致，只是不展开「下修」。"""
    kind = redeem_kind(daily.redeem_status)
    if kind == "announced":
        return "已公告强赎"
    if kind == "met":
        return "已满足强赎条件"
    if kind == "no_redeem":
        return "承诺不强赎"
    if daily.redeem_days is not None and daily.redeem_days >= NEAR_COUNT:
        need = daily.redeem_days_required or DEFAULT_REDEEM_DAYS
        return f"强赎临近 {daily.redeem_days}/{need}"
    return None


def bonds_of_stock(
    session: Session, stock_code: str, day: date | None = None
) -> list[StockBond]:
    """某只正股**存续**的转债（个股页联动用）。

    「存续」= `cb_basic.delist_date` 为空或不早于快照日 —— 已摘牌的券不往个股页上摆，
    但库里保留（回测要用）。

    快照日缺省取 `cb_daily` 的最新一天：转债快照**只能当天采**，所以这里显示的就是
    最近一次采集的盘后数据，不是实时价。
    """
    if not stock_code:
        return []
    if day is None:
        day = session.scalar(select(func.max(CbDaily.trade_date)))
    basics = [
        row
        for row in session.scalars(
            select(CbBasic).where(CbBasic.stock_code == stock_code).order_by(CbBasic.bond_code)
        )
        if row.delist_date is None or (day is not None and row.delist_date >= day)
    ]
    if not basics:
        return []
    dailies: dict[str, CbDaily] = {}
    if day is not None:
        dailies = {
            row.bond_code: row
            for row in session.scalars(
                select(CbDaily).where(
                    CbDaily.trade_date == day,
                    CbDaily.bond_code.in_([row.bond_code for row in basics]),
                )
            )
        }
    result: list[StockBond] = []
    for basic in basics:
        daily = dailies.get(basic.bond_code)
        result.append(
            StockBond(
                bond_code=basic.bond_code,
                bond_name=basic.bond_name,
                close=daily.close if daily else None,
                pct_chg=daily.pct_chg if daily else None,
                premium_pct=daily.premium_pct if daily else None,
                conv_value=daily.conv_value if daily else None,
                redeem_status=daily.redeem_status if daily else None,
                last_trade_date=daily.last_trade_date if daily else None,
                state=_state_text(daily) if daily else None,
            )
        )
    return result


# ---------------------------------------------------------------- 单券详情


@dataclass(frozen=True, slots=True)
class CbPoint:
    """详情页走势的一个点。"""

    trade_date: date
    close: float | None
    conv_value: float | None
    premium_pct: float | None
    ytm_pct: float | None


@dataclass(frozen=True, slots=True)
class CbDetail:
    """单券详情：条款摘要 + 最近快照 + 走势序列（可转债-3 §3.1）。

    没有事件表（`cb_event` 还没做），所以这里不带事件列表；强赎 / 下修只能看
    `redeem_status` 与各项计数。
    """

    as_of: date | None
    bond_code: str
    bond_name: str | None
    stock_code: str | None
    stock_name: str | None

    # ---- 条款（`cb_basic`）----
    list_date: date | None
    maturity_date: date | None
    convert_start_date: date | None
    issue_size: float | None
    rating: str | None
    maturity_call_price: float | None
    coupon_text: str | None
    redeem_trigger_pct: float | None
    resale_trigger_pct: float | None
    revise_trigger_pct: float | None
    #: 到期 / 强赎完成 / 摘牌日；非空说明这只券已经不在市场上了
    delist_date: date | None

    # ---- 最近一份快照（`cb_daily`）----
    close: float | None
    pct_chg: float | None
    high: float | None
    low: float | None
    amount: float | None
    amplitude: float | None
    turnover: float | None
    stock_close: float | None
    stock_pct_chg: float | None
    conv_price: float | None
    conv_value: float | None
    premium_pct: float | None
    double_low: float | None
    ytm_pct: float | None
    remain_size: float | None
    years_left: float | None
    redeem_status: str | None
    #: 已满足天数（集思录优先，否则本地近 30 日窗口）
    redeem_count: int | None
    #: 触发所需天数（标签分母）
    redeem_required: int | None
    redeem_complete: bool
    revise_count: int | None
    revise_complete: bool
    resale_count: int | None
    last_trade_date: date | None
    #: 距最后交易日的自然日数（负数 = 已过）
    days_left: int | None
    labels: list[str]
    risks: list[str]
    #: 当天快照的行情来源（不是 `sina+em+jsl` 说明走了兜底）
    source: str | None

    #: 走势（升序）。历史只有从上线那天起累积的那些天 —— 快照不可回补
    series: list[CbPoint]


def detail(
    session: Session,
    bond_code: str,
    day: date | None = None,
    days: int = COUNT_WINDOW * 8,
) -> CbDetail | None:
    """单券详情，纯读库。查不到这只券返回 None（接口层转 404）。

    `day` 缺省取该券**最近一份**快照，而不是全市场的最新日期 —— 已摘牌 / 当天停牌的
    券也要能打开详情，页面上的 `as_of` 会写明是哪一天。

    `series` 最多 `days` 个交易日（按 `cb_daily` 自身的日期，不看日历）：转债快照
    只能当天采，所以序列长度就是「上线以来采到几天」，别假装是 250 天。
    """
    basic = session.get(CbBasic, bond_code)
    if basic is None:
        return None
    if day is None:
        daily = session.scalar(
            select(CbDaily)
            .where(CbDaily.bond_code == bond_code)
            .order_by(CbDaily.trade_date.desc())
            .limit(1)
        )
    else:
        daily = session.get(CbDaily, (day, bond_code))
    as_of = daily.trade_date if daily is not None else None

    counts = None
    if as_of is not None:
        counts = _counts(session, as_of, {bond_code}, {bond_code: basic}).get(bond_code)
    labels: list[str] = []
    risks: list[str] = []
    if daily is not None:
        labels, risks = _labels(basic, daily, counts)

    series: list[CbPoint] = []
    if as_of is not None:
        rows = session.scalars(
            select(CbDaily)
            .where(CbDaily.bond_code == bond_code, CbDaily.trade_date <= as_of)
            .order_by(CbDaily.trade_date.desc())
            .limit(days)
        ).all()
        series = [
            CbPoint(
                trade_date=row.trade_date,
                close=row.close,
                conv_value=row.conv_value,
                premium_pct=row.premium_pct,
                ytm_pct=row.ytm_pct,
            )
            for row in reversed(rows)
        ]

    # 计数口径与筛选页一致：集思录的完整计数优先，本地窗口只作兜底
    redeem = None
    redeem_complete = False
    if daily is not None:
        if daily.redeem_days is not None:
            redeem, redeem_complete = daily.redeem_days, True
        elif counts is not None:
            redeem, redeem_complete = counts.redeem, counts.complete

    return CbDetail(
        as_of=as_of,
        bond_code=bond_code,
        bond_name=basic.bond_name,
        stock_code=basic.stock_code,
        stock_name=basic.stock_name,
        list_date=basic.list_date,
        maturity_date=basic.maturity_date,
        convert_start_date=basic.convert_start_date,
        issue_size=basic.issue_size,
        rating=basic.rating,
        maturity_call_price=basic.maturity_call_price,
        coupon_text=basic.coupon_text,
        redeem_trigger_pct=basic.redeem_trigger_pct,
        resale_trigger_pct=basic.resale_trigger_pct,
        revise_trigger_pct=basic.revise_trigger_pct,
        delist_date=basic.delist_date,
        close=daily.close if daily else None,
        pct_chg=daily.pct_chg if daily else None,
        high=daily.high if daily else None,
        low=daily.low if daily else None,
        amount=daily.amount if daily else None,
        amplitude=daily.amplitude if daily else None,
        turnover=daily.turnover if daily else None,
        stock_close=daily.stock_close if daily else None,
        stock_pct_chg=daily.stock_pct_chg if daily else None,
        conv_price=daily.conv_price if daily else None,
        conv_value=daily.conv_value if daily else None,
        premium_pct=daily.premium_pct if daily else None,
        double_low=double_low(
            daily.close if daily else None, daily.premium_pct if daily else None
        ),
        ytm_pct=daily.ytm_pct if daily else None,
        remain_size=daily.remain_size if daily else None,
        years_left=years_left(basic.maturity_date, as_of) if as_of else None,
        redeem_status=daily.redeem_status if daily else None,
        redeem_count=redeem,
        redeem_required=(daily.redeem_days_required if daily else None)
        or (DEFAULT_REDEEM_DAYS if redeem is not None else None),
        redeem_complete=redeem_complete,
        revise_count=counts.revise if counts else None,
        revise_complete=bool(counts.complete) if counts else False,
        resale_count=counts.resale if counts else None,
        last_trade_date=daily.last_trade_date if daily else None,
        days_left=(daily.last_trade_date - as_of).days
        if daily and daily.last_trade_date and as_of
        else None,
        labels=labels,
        risks=risks,
        source=daily.source if daily else None,
        series=series,
    )
