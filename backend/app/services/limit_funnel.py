"""涨停晋级漏斗：按昨日连板档统计「晋级 / 炸板 / 失败 / 不可验证」。

口径与 `/api/limit/promotion` 的 `promoted` 保持一致（今日 `consecutive == 昨日档 + 1`），
但把「今日数据是否可信」单独拆出来。核心原则：

- 没有可信的今日涨停池，就不能把「不在涨停池」判成失败；
- 没有可信的今日炸板池，就不能把「不在炸板池」当成没炸板；
- 没有今日日线，就不能把「不在两个池」判成收盘未涨停；
- 任何一处不确定都进 `UNVERIFIABLE`，并带独立原因，绝不填 0 或猜成失败。

本模块只做只读查询与纯计算，不写数据库、不访问网络。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    CollectLog,
    LimitPool,
    LimitReason,
    StockBasic,
    StockConcept,
    StockDaily,
    TradeCalendar,
)

PROMOTED = "PROMOTED"
BROKEN = "BROKEN"
FAILED = "FAILED"
UNVERIFIABLE = "UNVERIFIABLE"

VERIFIED = "VERIFIED"
PARTIAL = "PARTIAL"
UNVERIFIED = "UNVERIFIED"
NOT_COLLECTED = "NOT_COLLECTED"

#: 数据源状态（`CollectLog.status` 的另一层语义）
SOURCE_OK = "ok"
SOURCE_PARTIAL = "partial"
SOURCE_FAILED = "failed"
SOURCE_SKIPPED = "skipped"
SOURCE_EMPTY = "empty"
SOURCE_UNKNOWN = "unknown"
SOURCE_NOT_COLLECTED = "not_collected"

REASON_TEXT = {
    "PREVIOUS_UP_POOL_MISSING": "昨日涨停池无数据",
    "PREVIOUS_UP_POOL_UNVERIFIED": "昨日涨停池采集状态不可信",
    "TODAY_UP_POOL_MISSING": "今日涨停池无数据",
    "TODAY_UP_POOL_UNVERIFIED": "今日涨停池采集状态不可信",
    "TODAY_BROKEN_POOL_MISSING": "今日炸板池无数据",
    "TODAY_BROKEN_POOL_UNVERIFIED": "今日炸板池采集状态不可信",
    "DAILY_QUOTE_MISSING": "今日无有效日线",
    "POOL_CONFLICT": "同一股票同时出现在涨停与炸板池",
    "CONSECUTIVE_MISMATCH": "今日连板高度与昨日不衔接",
    "COLLECTION_FAILED": "采集失败",
    "COLLECTION_SKIPPED": "采集被跳过",
    "SOURCE_STATUS_UNKNOWN": "缺少采集状态证据",
    "NO_PREVIOUS_TRADE_DAY": "没有上一交易日",
}

_TRUSTED = {SOURCE_OK}


@dataclass(frozen=True)
class SourceQuality:
    """一个数据源在某一天的质量。只有 `ok` 可作为「完整」证据。"""

    status: str
    rows: int = 0
    log_status: str | None = None

    @property
    def trusted(self) -> bool:
        return self.status in _TRUSTED


@dataclass
class FunnelInputs:
    """一次漏斗计算所需的全部输入（批量查询结果，可 synthetic 构造）。"""

    trade_date: date
    prev_date: date | None
    prev_up: dict[str, dict] = field(default_factory=dict)
    today_up: dict[str, dict] = field(default_factory=dict)
    today_broken: dict[str, dict] = field(default_factory=dict)
    today_daily: dict[str, dict] = field(default_factory=dict)
    stock_basic: dict[str, dict] = field(default_factory=dict)
    boards: dict[str, str] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    prev_up_quality: SourceQuality = SourceQuality(SOURCE_UNKNOWN)
    today_up_quality: SourceQuality = SourceQuality(SOURCE_UNKNOWN)
    today_broken_quality: SourceQuality = SourceQuality(SOURCE_UNKNOWN)


@dataclass
class FunnelQuality:
    status: str
    reasons: list[str]
    prev_up: str
    today_up: str
    today_broken: str
    today_daily: str

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "reasons": list(self.reasons),
            "prev_up": self.prev_up,
            "today_up": self.today_up,
            "today_broken": self.today_broken,
            "today_daily": self.today_daily,
        }


def _source_quality(log: CollectLog | None, rows: int) -> SourceQuality:
    if log is None:
        # 有行但没日志：历史数据可能来自早期版本，不能算「已验证」
        return SourceQuality(SOURCE_UNKNOWN if rows else SOURCE_NOT_COLLECTED, rows)
    status = (log.status or SOURCE_UNKNOWN).lower()
    if status == SOURCE_OK:
        return SourceQuality(SOURCE_OK, rows, status)
    if status in (SOURCE_PARTIAL, SOURCE_FAILED, SOURCE_SKIPPED, SOURCE_EMPTY):
        return SourceQuality(status, rows, status)
    return SourceQuality(SOURCE_UNKNOWN, rows, status)


def _source_reason(prefix: str, quality: SourceQuality) -> str:
    if quality.status == SOURCE_NOT_COLLECTED:
        return f"{prefix}_MISSING"
    if quality.status == SOURCE_FAILED:
        return "COLLECTION_FAILED"
    if quality.status == SOURCE_SKIPPED:
        return "COLLECTION_SKIPPED"
    if quality.status in (SOURCE_UNKNOWN, SOURCE_PARTIAL, SOURCE_EMPTY):
        return "SOURCE_STATUS_UNKNOWN"
    return f"{prefix}_MISSING"


def _reason_text(code: str | None) -> str | None:
    if not code:
        return None
    return REASON_TEXT.get(code, code)


def assess_funnel_quality(inputs: FunnelInputs) -> FunnelQuality:
    """先判数据源，再判股票。日期级状态只表达「源是否可信」。"""
    reasons: list[str] = []
    status = VERIFIED

    if not inputs.prev_up_quality.trusted:
        if inputs.prev_up_quality.status == SOURCE_NOT_COLLECTED:
            status = NOT_COLLECTED
            reasons.append("PREVIOUS_UP_POOL_MISSING")
        else:
            status = UNVERIFIED
            reasons.append("PREVIOUS_UP_POOL_UNVERIFIED")
    else:
        if not inputs.today_up_quality.trusted:
            status = UNVERIFIED
            reasons.append(_source_reason("TODAY_UP_POOL", inputs.today_up_quality))
        if not inputs.today_broken_quality.trusted:
            if status == VERIFIED:
                status = PARTIAL
            reasons.append(_source_reason("TODAY_BROKEN_POOL", inputs.today_broken_quality))

    return FunnelQuality(
        status=status,
        reasons=_dedupe(reasons),
        prev_up=inputs.prev_up_quality.status,
        today_up=inputs.today_up_quality.status,
        today_broken=inputs.today_broken_quality.status,
        today_daily=SOURCE_OK if inputs.today_daily else SOURCE_NOT_COLLECTED,
    )


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def classify_promotion(
    code: str,
    previous_level: int,
    inputs: FunnelInputs,
) -> dict:
    """单只股票的互斥状态。昨日池不可信时一律不可验证，但仍保留观察行。"""
    if not inputs.prev_up_quality.trusted:
        return _unverifiable(code, previous_level, "PREVIOUS_UP_POOL_UNVERIFIED", None)

    up = inputs.today_up.get(code)
    broken = inputs.today_broken.get(code)

    if up is not None and broken is not None:
        return _unverifiable(code, previous_level, "POOL_CONFLICT", "up+broken")

    if not inputs.today_up_quality.trusted:
        return _unverifiable(
            code,
            previous_level,
            _source_reason("TODAY_UP_POOL", inputs.today_up_quality),
            "broken" if broken is not None else None,
        )

    if up is not None:
        consecutive = up.get("consecutive")
        if consecutive is None or int(consecutive) != previous_level + 1:
            return _unverifiable(
                code, previous_level, "CONSECUTIVE_MISMATCH", "up", today_consecutive=consecutive
            )
        return _stock_row(code, previous_level, PROMOTED, "up", inputs)

    if not inputs.today_broken_quality.trusted:
        return _unverifiable(
            code,
            previous_level,
            _source_reason("TODAY_BROKEN_POOL", inputs.today_broken_quality),
            None,
        )

    if broken is not None:
        return _stock_row(code, previous_level, BROKEN, "broken", inputs)

    if code not in inputs.today_daily:
        return _unverifiable(code, previous_level, "DAILY_QUOTE_MISSING", None)

    return _stock_row(code, previous_level, FAILED, None, inputs)


def _today_values(code: str, pool_type: str | None, inputs: FunnelInputs) -> tuple[float | None, float | None]:
    row = inputs.today_up.get(code) if pool_type == "up" else inputs.today_broken.get(code)
    daily = inputs.today_daily.get(code)
    if row is not None:
        return row.get("pct_chg"), row.get("price") if row.get("price") is not None else (
            daily.get("close") if daily else None
        )
    if daily is not None:
        return daily.get("pct_chg"), daily.get("close")
    return None, None


def _stock_row(
    code: str,
    previous_level: int,
    status: str,
    pool_type: str | None,
    inputs: FunnelInputs,
    today_consecutive: int | None = None,
) -> dict:
    row = inputs.today_up.get(code) if pool_type == "up" else (
        inputs.today_broken.get(code) if pool_type == "broken" else None
    )
    daily = inputs.today_daily.get(code)
    basic = inputs.stock_basic.get(code, {})
    pct_chg, close = _today_values(code, pool_type, inputs)
    consecutive = today_consecutive
    if consecutive is None and row is not None:
        consecutive = row.get("consecutive")
    return {
        "code": code,
        "name": (row or {}).get("name") or basic.get("name"),
        "previous_level": previous_level,
        "today_consecutive": consecutive,
        "status": status,
        "reason_code": None,
        "reason_text": None,
        "today_pool_type": pool_type,
        "pct_chg": pct_chg,
        "close": close,
        "board": inputs.boards.get(code),
        "industry": basic.get("industry"),
        "first_seal_time": (row or {}).get("first_seal_time"),
        "open_times": (row or {}).get("open_times"),
        "seal_amount": (row or {}).get("seal_amount"),
        "quote_date": inputs.trade_date if (row is not None or daily is not None) else None,
        "data_quality": VERIFIED,
    }


def _unverifiable(
    code: str,
    previous_level: int,
    reason_code: str,
    pool_type: str | None,
    today_consecutive: int | None = None,
) -> dict:
    return {
        "code": code,
        "name": None,
        "previous_level": previous_level,
        "today_consecutive": today_consecutive,
        "status": UNVERIFIABLE,
        "reason_code": reason_code,
        "reason_text": _reason_text(reason_code),
        "today_pool_type": pool_type,
        "pct_chg": None,
        "close": None,
        "board": None,
        "industry": None,
        "first_seal_time": None,
        "open_times": None,
        "seal_amount": None,
        "quote_date": None,
        "data_quality": UNVERIFIED,
    }


def build_promotion_funnel(
    inputs: FunnelInputs, levels: list[int]
) -> tuple[list[dict], FunnelQuality]:
    """生成每一层的家数与逐券结果；昨日池不可信时返回空层 + 日期级原因。"""
    quality = assess_funnel_quality(inputs)
    if not inputs.prev_up_quality.trusted and not inputs.prev_up:
        # 连观察基数都没有：不要构造一个看似真实的 0 base
        return [], quality

    buckets: dict[int, list[str]] = {}
    for code, row in inputs.prev_up.items():
        level = int(row.get("consecutive") or 1)
        buckets.setdefault(level, []).append(code)

    output: list[dict] = []
    per_stock_reasons: list[str] = []
    for level in sorted({int(item) for item in levels}):
        codes = sorted(buckets.get(level, []))
        stocks = [classify_promotion(code, level, inputs) for code in codes]
        promoted = sum(1 for item in stocks if item["status"] == PROMOTED)
        broken = sum(1 for item in stocks if item["status"] == BROKEN)
        failed = sum(1 for item in stocks if item["status"] == FAILED)
        unverifiable = sum(1 for item in stocks if item["status"] == UNVERIFIABLE)
        base = len(stocks)
        valid_base = promoted + broken + failed
        per_stock_reasons.extend(
            item["reason_code"] for item in stocks if item.get("reason_code")
        )
        output.append(
            {
                "level": level,
                "label": f"{level}进{level + 1}",
                "base": base,
                "promoted": promoted,
                "broken": broken,
                "failed": failed,
                "unverifiable": unverifiable,
                "valid_base": valid_base,
                "legacy_rate": _rate(promoted, base),
                "verified_rate": _rate(promoted, valid_base),
                "stocks": stocks,
            }
        )

    if quality.status == VERIFIED and any(
        level["unverifiable"] > 0 for level in output
    ):
        # 源都可信、但存在逐券冲突/连板异常/缺日线：整日只能算部分可验证
        quality.status = PARTIAL
    quality.reasons = _dedupe(quality.reasons + per_stock_reasons)
    validate_funnel_invariants(output)
    return output, quality


def _rate(promoted: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round(promoted / total * 100, 1)


def validate_funnel_invariants(levels: list[dict]) -> None:
    for level in levels:
        assert level["base"] == (
            level["promoted"]
            + level["broken"]
            + level["failed"]
            + level["unverifiable"]
        ), "base 必须等于四类之和"
        assert level["valid_base"] == (
            level["promoted"] + level["broken"] + level["failed"]
        ), "valid_base 必须剔除不可验证"
        codes = [item["code"] for item in level["stocks"]]
        assert len(codes) == len(set(codes)), "同一层出现重复股票"
        assert len(codes) == level["base"], "逐券数量必须等于 base"
        if level["base"] == 0:
            assert level["legacy_rate"] is None and level["verified_rate"] is None
        if level["valid_base"] == 0:
            assert level["verified_rate"] is None


def _latest_logs(
    session: Session, days: list[date]
) -> dict[tuple[date, str], CollectLog]:
    if not days:
        return {}
    rows = session.scalars(
        select(CollectLog)
        .where(
            CollectLog.trade_date.in_(days),
            CollectLog.task.in_(("pool_up", "pool_broken")),
        )
        .order_by(CollectLog.id.desc())
    ).all()
    latest: dict[tuple[date, str], CollectLog] = {}
    for row in rows:
        if row.trade_date is not None:
            latest.setdefault((row.trade_date, row.task), row)
    return latest


def _pool_rows(session: Session, day: date, pool_type: str) -> list[LimitPool]:
    return list(
        session.scalars(
            select(LimitPool).where(
                LimitPool.trade_date == day, LimitPool.pool_type == pool_type
            )
        )
    )


def load_funnel_inputs(
    session: Session, prev_date: date | None, trade_date: date
) -> FunnelInputs:
    """批量读取一次，不在分类函数里访问数据库。"""
    prev_up_rows = _pool_rows(session, prev_date, "up") if prev_date else []
    today_up_rows = _pool_rows(session, trade_date, "up")
    today_broken_rows = _pool_rows(session, trade_date, "broken")

    prev_up = {
        row.code: {
            "consecutive": row.consecutive,
            "name": row.name,
            "pct_chg": row.pct_chg,
            "price": row.price,
        }
        for row in prev_up_rows
    }
    today_up = {
        row.code: {
            "consecutive": row.consecutive,
            "name": row.name,
            "pct_chg": row.pct_chg,
            "price": row.price,
            "first_seal_time": row.first_seal_time,
            "open_times": row.open_times,
            "seal_amount": row.seal_amount,
        }
        for row in today_up_rows
    }
    today_broken = {
        row.code: {
            "consecutive": row.consecutive,
            "name": row.name,
            "pct_chg": row.pct_chg,
            "price": row.price,
            "first_seal_time": row.first_seal_time,
            "open_times": row.open_times,
            "seal_amount": row.seal_amount,
        }
        for row in today_broken_rows
    }

    codes = set(prev_up) | set(today_up) | set(today_broken)
    daily: dict[str, dict] = {}
    basics: dict[str, dict] = {}
    if codes:
        daily = {
            code: {"close": close, "pct_chg": pct_chg}
            for code, close, pct_chg in session.execute(
                select(StockDaily.code, StockDaily.close, StockDaily.pct_chg).where(
                    StockDaily.trade_date == trade_date,
                    StockDaily.code.in_(codes),
                    StockDaily.close.is_not(None),
                )
            ).all()
        }
        basics = {
            code: {"name": name, "industry": industry}
            for code, name, industry in session.execute(
                select(StockBasic.code, StockBasic.name, StockBasic.industry).where(
                    StockBasic.code.in_(codes)
                )
            ).all()
        }

    boards: dict[str, str] = {}
    reasons: dict[str, str] = {}
    if today_up:
        boards = {
            code: concept
            for code, concept in session.execute(
                select(StockConcept.code, StockConcept.concept)
                .where(
                    StockConcept.trade_date == trade_date,
                    StockConcept.code.in_(set(today_up)),
                )
                .order_by(StockConcept.code, StockConcept.concept)
            ).all()
        }
        reasons = dict(
            session.execute(
                select(LimitReason.code, LimitReason.reason).where(
                    LimitReason.trade_date == trade_date,
                    LimitReason.code.in_(set(today_up)),
                )
            ).all()
        )

    logs = _latest_logs(session, [day for day in (prev_date, trade_date) if day])
    prev_quality = _source_quality(
        logs.get((prev_date, "pool_up")) if prev_date else None, len(prev_up_rows)
    )
    up_quality = _source_quality(logs.get((trade_date, "pool_up")), len(today_up_rows))
    broken_quality = _source_quality(
        logs.get((trade_date, "pool_broken")), len(today_broken_rows)
    )

    return FunnelInputs(
        trade_date=trade_date,
        prev_date=prev_date,
        prev_up=prev_up,
        today_up=today_up,
        today_broken=today_broken,
        today_daily=daily,
        stock_basic=basics,
        boards=boards,
        reasons=reasons,
        prev_up_quality=prev_quality,
        today_up_quality=up_quality,
        today_broken_quality=broken_quality,
    )


def _day_status(
    prev_quality: SourceQuality,
    up_quality: SourceQuality,
    broken_quality: SourceQuality,
    prev_count: int,
) -> str:
    if not prev_quality.trusted:
        return NOT_COLLECTED if prev_quality.status == SOURCE_NOT_COLLECTED else UNVERIFIED
    if not up_quality.trusted:
        return UNVERIFIED
    if not broken_quality.trusted:
        return PARTIAL
    if prev_count == 0:
        return VERIFIED
    return VERIFIED


def list_funnel_dates(session: Session, days: int = 255) -> list[dict]:
    """日期可用性摘要：只查池家数与采集状态，不携带逐券行。

    ⚠️ `quality_status` 是**池数据**的可信度（Up/Broken 采集日志），不是整日
    逐券分类的最终质量；逐券是否还有缺日线/冲突，由 `/promotion-funnel` 那天
    实际计算后给出。两者可能不同，日期列表不冒充完整结论。
    """
    anchor = session.scalar(
        select(func.max(LimitPool.trade_date)).where(LimitPool.pool_type == "up")
    )
    if anchor is None:
        return []
    calendar = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= anchor)
            .order_by(TradeCalendar.trade_date)
        )
    )
    calendar_index = {day: index for index, day in enumerate(calendar)}
    counts_by_type = {
        (day, pool_type): count
        for day, pool_type, count in session.execute(
            select(LimitPool.trade_date, LimitPool.pool_type, func.count())
            .where(LimitPool.pool_type.in_(("up", "broken")))
            .group_by(LimitPool.trade_date, LimitPool.pool_type)
        ).all()
    }
    counts = {
        day: count
        for (day, pool_type), count in counts_by_type.items()
        if pool_type == "up"
    }
    target_dates = sorted(counts)[-days:]
    log_days = set(target_dates)
    log_days.update(
        calendar[calendar_index[day] - 1]
        for day in target_dates
        if day in calendar_index and calendar_index[day] > 0
    )
    logs = _latest_logs(session, sorted(log_days))

    output: list[dict] = []
    for day in target_dates:
        index = calendar_index.get(day)
        prev_day = calendar[index - 1] if index is not None and index > 0 else None
        prev_count = counts.get(prev_day, 0)
        prev_quality = _source_quality(
            logs.get((prev_day, "pool_up")) if prev_day else None, prev_count
        )
        up_quality = _source_quality(logs.get((day, "pool_up")), counts.get(day, 0))
        broken_quality = _source_quality(
            logs.get((day, "pool_broken")), counts_by_type.get((day, "broken"), 0)
        )
        status = _day_status(prev_quality, up_quality, broken_quality, prev_count)
        output.append(
            {
                "trade_date": day,
                "prev_date": prev_day,
                "has_previous_pool": prev_count > 0,
                "has_today_pool": counts.get(day, 0) > 0,
                "quality_status": status,
                "can_calculate_verified_rate": (
                    prev_quality.trusted
                    and prev_count > 0
                    and (up_quality.trusted or broken_quality.trusted)
                ),
            }
        )
    return output
