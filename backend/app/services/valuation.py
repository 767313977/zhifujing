"""核心指数的「近 10 年估值分位」：读库 → 算分位 → 给页面用。

口径（页面必须照原样写出来，否则会被拿去和别的站逐位对比）：

- 窗口：**近 10 年**（3653 天），按该指数**自己最后一个采样日**往前推；
- 采样：来源是周频，所以窗口里约 500 个点，不是 2500 个；
- 分位：复用恐贪那套**中位秩**（`fear_greed.percentile_score`），0 = 历史最低，
  100 = 历史最高；样本不足 20 个点时返回 None，不给 0；
- 只有 PE / PB。公开源没有指数级 10 年 PS 历史，这一项刻意留空（见 `models.IndexValuation`）。

「A 股估值温度」= 这些核心指数 **PE 分位的中位数** —— 与单看沪深300 相比，
不容易被一只大盘股的权重结构带偏。
"""

import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import IndexValuation
from app.services.fear_greed import percentile_score

#: 分位窗口：近 10 年（自然日）。闰年不影响口径表述，直接写 3653 天。
WINDOW_DAYS = 3653

#: 核心指数清单：(站内代码, 展示名, 数据源代码)。
#:
#: 站内代码与 `index_daily` / `INDEX_ORDER` 同一套（000300.SH 这种），方便以后
#: 把估值和点位画在一张图上。蛋卷只覆盖这 8 个 —— **上证指数不在里面**
#: （它的估值接口不存在），别把「没有」当成「漏采」。
CORE_INDICES: tuple[tuple[str, str, str], ...] = (
    ("000300.SH", "沪深300", "SH000300"),
    ("000016.SH", "上证50", "SH000016"),
    ("000905.SH", "中证500", "SH000905"),
    ("000852.SH", "中证1000", "SH000852"),
    ("399001.SZ", "深证成指", "SZ399001"),
    ("399006.SZ", "创业板指", "SZ399006"),
    ("000688.SH", "科创50", "SH000688"),
    ("000922.CSI", "中证红利", "SH000922"),
)

#: PE 分位 → 温度。区间取「左闭右开」，最后一档兜到 100（含 100）。
TONE_BANDS: tuple[tuple[float, str], ...] = (
    (10.0, "极冷"),
    (30.0, "偏冷"),
    (70.0, "正常"),
    (90.0, "偏热"),
    (100.01, "极热"),
)


def tone_of(percentile: float | None) -> str | None:
    """分位 → 温度档。None 原样返回 None（页面显示「—」，不猜）。"""
    if percentile is None:
        return None
    for upper, label in TONE_BANDS:
        if percentile < upper:
            return label
    return TONE_BANDS[-1][1]


@dataclass(frozen=True, slots=True)
class IndexValuationRow:
    code: str
    name: str
    as_of: date | None
    pe_ttm: float | None
    pe_pct: float | None
    pb: float | None
    pb_pct: float | None
    tone: str | None


@dataclass(frozen=True, slots=True)
class ValuationSummary:
    as_of: date | None
    window_days: int
    items: list[IndexValuationRow]
    market_pct: float | None
    market_tone: str | None


@dataclass(frozen=True, slots=True)
class ValuationPoint:
    day: date
    pe_ttm: float | None
    pb: float | None


@dataclass(frozen=True, slots=True)
class ValuationSeries:
    code: str
    name: str
    window_days: int
    points: list[ValuationPoint]


def _load(session: Session) -> dict[str, list[IndexValuation]]:
    """一次把全部核心指数的序列读回来（约 8 × 516 行，单查询够用）。"""
    rows = list(
        session.scalars(
            select(IndexValuation)
            .where(IndexValuation.index_code.in_([code for code, _, _ in CORE_INDICES]))
            .order_by(IndexValuation.index_code, IndexValuation.trade_date)
        )
    )
    grouped: dict[str, list[IndexValuation]] = {}
    for row in rows:
        grouped.setdefault(row.index_code, []).append(row)
    return grouped


def _window(
    rows: list[IndexValuation], attr: str
) -> tuple[float | None, float | None]:
    """返回（最新值, 近 10 年分位）。没有数据时是 (None, None)。"""
    if not rows:
        return None, None
    latest = rows[-1]
    current = getattr(latest, attr)
    if current is None:
        return None, None
    since = latest.trade_date - timedelta(days=WINDOW_DAYS)
    values = [
        float(value)
        for row in rows
        if row.trade_date >= since
        for value in [getattr(row, attr)]
        if value is not None
    ]
    return float(current), percentile_score(values, float(current))


def summary(session: Session) -> ValuationSummary:
    """全部核心指数的当前估值 + 分位，外加 A 股估值温度。"""
    grouped = _load(session)
    items: list[IndexValuationRow] = []
    for code, name, _ in CORE_INDICES:
        rows = grouped.get(code) or []
        pe, pe_pct = _window(rows, "pe_ttm")
        pb, pb_pct = _window(rows, "pb")
        items.append(
            IndexValuationRow(
                code=code,
                name=name,
                as_of=rows[-1].trade_date if rows else None,
                pe_ttm=pe,
                pe_pct=pe_pct,
                pb=pb,
                pb_pct=pb_pct,
                tone=tone_of(pe_pct),
            )
        )

    pe_percentiles = [item.pe_pct for item in items if item.pe_pct is not None]
    market_pct = round(statistics.median(pe_percentiles), 1) if pe_percentiles else None
    as_of = max((item.as_of for item in items if item.as_of), default=None)
    return ValuationSummary(
        as_of=as_of,
        window_days=WINDOW_DAYS,
        items=items,
        market_pct=market_pct,
        market_tone=tone_of(market_pct),
    )


def series(session: Session, code: str) -> ValuationSeries | None:
    """单个指数的近 10 年序列（给详情曲线用）。不认识的代码返回 None。"""
    name = next((item[1] for item in CORE_INDICES if item[0] == code), None)
    if name is None:
        return None
    rows = list(
        session.scalars(
            select(IndexValuation)
            .where(IndexValuation.index_code == code)
            .order_by(IndexValuation.trade_date)
        )
    )
    if not rows:
        return ValuationSeries(code=code, name=name, window_days=WINDOW_DAYS, points=[])
    since = rows[-1].trade_date - timedelta(days=WINDOW_DAYS)
    return ValuationSeries(
        code=code,
        name=name,
        window_days=WINDOW_DAYS,
        points=[
            ValuationPoint(day=row.trade_date, pe_ttm=row.pe_ttm, pb=row.pb)
            for row in rows
            if row.trade_date >= since
        ],
    )
