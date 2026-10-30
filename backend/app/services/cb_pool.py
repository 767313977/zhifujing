"""可转债 T+0 观察池：读当日快照 → 过硬门槛 → 打分排序。

**为什么先过硬门槛再打分**：转债的亏损主要来自「踩雷」而不是「选得不够强」——
已公告强赎、剩余规模太小、溢价太高，这三类会把一次本来正确的判断直接打成大亏。
所以这里把「不能碰」的规则写成硬门槛，宁可池子里少几只，也不把雷混进来。

打分口径（权重合计 1.0，缺某一项时按剩余权重归一，不拿 0 分冒充「最差」）：

- 0.30 × 正股当日涨幅分位（转债的定价锚是正股，正股强才有日内空间）
- 0.25 × 换手率分位（有没有量，决定能不能进出）
- 0.20 × (100 − 转股溢价率分位)（溢价越低，跟涨越紧）
- 0.15 × 振幅分位（日内 T 的空间）
- 0.10 × (100 − 剩余规模分位)（盘子小的更活，但已经是硬门槛筛过的）

分位是**当天池内**的中位秩，不是历史分位 —— 转债没有足够长的本地历史，
用历史分位会得出「样本不足」的假精确。
"""

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CbDaily

# ---------------------------------------------------------------- 硬门槛

#: 剩余规模下限（亿元）。再小盘口就薄了，T+0 的滑点会吃掉利润
MIN_REMAIN_SIZE = 3.0
#: 当日成交额下限（元）。5000 万是「能进能出」的经验线
MIN_AMOUNT = 50_000_000.0
#: 价格区间：太低弹性差，太高是纯筹码博弈
MIN_PRICE = 100.0
MAX_PRICE = 150.0
#: 转股溢价率上限（%）。再高就是情绪定价，正股涨它也跟不上
MAX_PREMIUM = 30.0
#: 剩余年限下限（年）。临近到期的债流动性会突然枯竭
MIN_YEARS_LEFT = 1.0
#: 评级下限。低于 AA- 的信用风险不该出现在日内池里
MIN_RATING = "AA-"
#: 上市满几天才算「有历史」。新债前几日没有常规涨跌幅，波动极端
MIN_DAYS_LISTED = 5

#: 评级 → 序（只用于比较，不参与打分）。带 `sti` 后缀（定向转债）的先剥掉
_RATING_RANK = {
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

#: 打分权重（见模块说明）
WEIGHTS = {
    "stock_pct": 0.30,
    "turnover": 0.25,
    "premium": 0.20,
    "amplitude": 0.15,
    "size": 0.10,
}


@dataclass(frozen=True, slots=True)
class CbRow:
    """观察池的一行。字段与页面列一一对应。"""

    code: str
    name: str | None
    close: float | None
    pct_chg: float | None
    amplitude: float | None
    amount: float | None
    turnover_rate: float | None
    premium_rt: float | None
    convert_value: float | None
    remain_size: float | None
    issue_size: float | None
    rating: str | None
    redeem_status: str | None
    years_left: float | None
    stock_code: str | None
    stock_name: str | None
    stock_pct_chg: float | None
    #: 收盘价 + 转股溢价率（双低），越低越「便宜」
    double_low: float | None
    score: float


@dataclass(frozen=True, slots=True)
class CbGate:
    """一条硬门槛剔掉了多少只。页面用它解释「为什么池子只剩这么点」。"""

    label: str
    count: int


@dataclass(frozen=True, slots=True)
class ExcludedRow:
    code: str
    name: str | None
    close: float | None
    amount: float | None
    premium_rt: float | None
    reason: str


@dataclass(frozen=True, slots=True)
class CbPool:
    trade_date: date | None
    total: int
    rows: list[CbRow] = field(default_factory=list)
    excluded: list[ExcludedRow] = field(default_factory=list)
    gates: list[CbGate] = field(default_factory=list)


def _median_rank(values: list[float], value: float) -> float:
    """`value` 在 `values` 里的中位秩（0~100）。与恐贪那套口径一致。"""
    if not values:
        return 50.0
    below = sum(1 for item in values if item < value)
    equal = sum(1 for item in values if item == value)
    return (below + equal / 2) / len(values) * 100


def rating_rank(rating: str | None) -> int | None:
    """评级 → 序。`AA+sti` 这类定向转债按 `AA+` 算；认不出的返回 None。"""
    if not rating:
        return None
    text = str(rating).strip().upper().removesuffix("STI")
    return _RATING_RANK.get(text)


def reject_reason(row: CbDaily, day: date) -> str | None:
    """这只债为什么不能进池。返回 None = 通过。**顺序即优先级**（页面只显示第一条）。"""
    status = row.redeem_status or ""
    if "强赎" in status and "不强赎" not in status:
        return "强赎风险"
    if row.close is None:
        return "没有行情"
    if row.close < MIN_PRICE:
        return f"价格<{MIN_PRICE:.0f}"
    if row.close > MAX_PRICE:
        return f"价格>{MAX_PRICE:.0f}"
    if row.premium_rt is None:
        return "缺溢价率"
    if row.premium_rt > MAX_PREMIUM:
        return f"溢价>{MAX_PREMIUM:.0f}%"
    if row.amount is None or row.amount < MIN_AMOUNT:
        return f"成交额<{MIN_AMOUNT / 1e8:.2f}亿"
    if row.remain_size is None:
        # 集思录没有这条记录时**不拿发行规模顶替**：两者差的就是「还剩多少没转股」，
        # 拿发行规模冒充会把 0.5 亿的小盘债当成 10 亿的大盘债放进来
        return "剩余规模未知"
    if row.remain_size < MIN_REMAIN_SIZE:
        return f"剩余<{MIN_REMAIN_SIZE:.0f}亿"
    years = years_left(row, day)
    if years is not None and years < MIN_YEARS_LEFT:
        return f"剩余期限<{MIN_YEARS_LEFT:.0f}年"
    rank = rating_rank(row.rating)
    floor = rating_rank(MIN_RATING)
    if rank is None:
        return "缺评级"
    if floor is not None and rank < floor:
        return f"评级低于{MIN_RATING}"
    name = f"{row.stock_name or ''}{row.name or ''}".upper()
    if "ST" in name or "退" in name:
        return "ST / 退市风险"
    if row.list_date is not None and (day - row.list_date).days < MIN_DAYS_LISTED:
        return "新债上市未满5日"
    return None


def years_left(row: CbDaily, day: date) -> float | None:
    """剩余年限（年）。没有到期日就是 None，不猜。"""
    if row.maturity_date is None:
        return None
    return round((row.maturity_date - day).days / 365.25, 2)


def _score(rows: list[CbDaily], day: date) -> list[CbRow]:
    """对**已通过门槛**的行打分。分位在池内算，缺项按剩余权重归一。"""
    stock_values = [float(r.stock_pct_chg) for r in rows if r.stock_pct_chg is not None]
    turnover_values = [float(r.turnover_rate) for r in rows if r.turnover_rate is not None]
    premium_values = [float(r.premium_rt) for r in rows if r.premium_rt is not None]
    amplitude_values = [float(r.amplitude) for r in rows if r.amplitude is not None]
    size_values = [float(r.remain_size) for r in rows if r.remain_size is not None]

    scored: list[CbRow] = []
    for row in rows:
        parts: dict[str, float] = {}
        if row.stock_pct_chg is not None:
            parts["stock_pct"] = _median_rank(stock_values, float(row.stock_pct_chg))
        if row.turnover_rate is not None:
            parts["turnover"] = _median_rank(turnover_values, float(row.turnover_rate))
        if row.premium_rt is not None:
            parts["premium"] = 100 - _median_rank(premium_values, float(row.premium_rt))
        if row.amplitude is not None:
            parts["amplitude"] = _median_rank(amplitude_values, float(row.amplitude))
        if row.remain_size is not None:
            parts["size"] = 100 - _median_rank(size_values, float(row.remain_size))

        weight_sum = sum(WEIGHTS[key] for key in parts)
        score = (
            sum(WEIGHTS[key] * value for key, value in parts.items()) / weight_sum
            if weight_sum
            else 0.0
        )

        double_low = None
        if row.close is not None and row.premium_rt is not None:
            double_low = round(row.close + float(row.premium_rt), 2)

        scored.append(
            CbRow(
                code=row.code,
                name=row.name,
                close=row.close,
                pct_chg=row.pct_chg,
                amplitude=row.amplitude,
                amount=row.amount,
                turnover_rate=row.turnover_rate,
                premium_rt=row.premium_rt,
                convert_value=row.convert_value,
                remain_size=row.remain_size,
                issue_size=row.issue_size,
                rating=row.rating,
                redeem_status=row.redeem_status,
                years_left=years_left(row, day),
                stock_code=row.stock_code,
                stock_name=row.stock_name,
                stock_pct_chg=row.stock_pct_chg,
                double_low=double_low,
                score=round(score, 1),
            )
        )

    # 同分时成交额大的在前：同样的分数，能进出的那只更有意义
    scored.sort(key=lambda item: (-item.score, -(item.amount or 0)))
    return scored


def build(session: Session, trade_date: date | None = None) -> CbPool:
    """读某天的快照并算观察池。不传日期 = 最近一次快照。"""
    day = trade_date
    if day is None:
        day = session.scalar(select(func.max(CbDaily.trade_date)))
    if day is None:
        return CbPool(trade_date=None, total=0)

    snapshot = list(session.scalars(select(CbDaily).where(CbDaily.trade_date == day)))
    if not snapshot:
        return CbPool(trade_date=None, total=0)

    passed: list[CbDaily] = []
    excluded: list[ExcludedRow] = []
    gate_counts: dict[str, int] = {}
    for row in snapshot:
        reason = reject_reason(row, day)
        if reason is None:
            passed.append(row)
            continue
        gate_counts[reason] = gate_counts.get(reason, 0) + 1
        excluded.append(
            ExcludedRow(
                code=row.code,
                name=row.name,
                close=row.close,
                amount=row.amount,
                premium_rt=row.premium_rt,
                reason=reason,
            )
        )

    excluded.sort(key=lambda item: -(item.amount or 0))
    gates = [CbGate(label=label, count=count) for label, count in sorted(
        gate_counts.items(), key=lambda pair: -pair[1]
    )]
    return CbPool(
        trade_date=day,
        total=len(snapshot),
        rows=_score(passed, day),
        excluded=excluded,
        gates=gates,
    )
