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

from sqlalchemy import exists, func, select
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
from app.timeutil import today_cn

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
    "UP_POOL_EMPTY_WITH_OK_LOG": "涨停池日志成功但实际 0 行（疑似来源空表）",
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


def _source_quality(
    log: CollectLog | None, rows: int, *, allow_empty_ok: bool = False
) -> SourceQuality:
    if log is None:
        # 有行但没日志：历史数据可能来自早期版本，不能算「已验证」
        return SourceQuality(SOURCE_UNKNOWN if rows else SOURCE_NOT_COLLECTED, rows)
    status = (log.status or SOURCE_UNKNOWN).lower()
    if status == SOURCE_OK:
        if rows == 0 and not allow_empty_ok:
            # 涨停池 ok+0 不符合采集器约定（空表应记 failed）；炸板池 ok+0 合法
            return SourceQuality(SOURCE_EMPTY, 0, status)
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
    if quality.status == SOURCE_EMPTY and prefix in ("TODAY_UP_POOL", "PREVIOUS_UP_POOL"):
        return "UP_POOL_EMPTY_WITH_OK_LOG"
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
            reasons.append(
                "UP_POOL_EMPTY_WITH_OK_LOG"
                if inputs.prev_up_quality.status == SOURCE_EMPTY
                else "PREVIOUS_UP_POOL_UNVERIFIED"
            )
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
        legacy_rate, verified_rate = compute_rates(
            promoted=promoted,
            base=base,
            valid_base=valid_base,
            previous_up_trusted=inputs.prev_up_quality.trusted,
            today_up_trusted=inputs.today_up_quality.trusted,
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
                "legacy_rate": legacy_rate,
                "verified_rate": verified_rate,
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


def compute_rates(
    *,
    promoted: int,
    base: int,
    valid_base: int,
    previous_up_trusted: bool,
    today_up_trusted: bool,
) -> tuple[float | None, float | None]:
    """旧口径 / 可验证口径。

    旧口径只有在「昨日池可信 + 今日涨停池可信」时才有意义：今日池不可信时
    `promoted=0` 只代表「没观察到」，不能写成 0%。可验证口径只看有效分母。
    全部样本都不可验证（`valid_base=0`）时两种比率都只能是 null —— 没有可信
    结果，不等于「确认无人晋级」。
    """
    legacy_rate = (
        _rate(promoted, base)
        if base > 0
        and valid_base > 0
        and previous_up_trusted
        and today_up_trusted
        else None
    )
    verified_rate = _rate(promoted, valid_base) if valid_base > 0 else None
    return legacy_rate, verified_rate


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
            assert (
                level["legacy_rate"] is None and level["verified_rate"] is None
            ), "全部不可验证时两种成功率都必须为 null"


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
        logs.get((trade_date, "pool_broken")),
        len(today_broken_rows),
        allow_empty_ok=True,
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


def load_funnel_window_inputs(
    session: Session,
    days: list[date],
    calendar: list[date] | None = None,
) -> dict[date, FunnelInputs]:
    """S3-B：一次批量读取多日漏斗输入，避免逐日 N+1。

    与 `load_funnel_inputs` 的逐日结果必须**完全等价**（同样的键、同样的值、
    同样的 SourceQuality）。区别只在查询次数：池 / 日线 / 基础资料 / 题材 /
    涨停原因 / 采集日志各按窗口批量取，`days=250` 也从 ~2255 条 SQL 降到几十条。

    `calendar` 传入完整交易日序列时不再查 `trade_calendar`；不传则只查
    `<= max(days)` 的日期，足够找到每天的上一交易日。
    """
    wanted = sorted(set(days))
    if not wanted:
        return {}
    if calendar is None:
        calendar = list(
            session.scalars(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date <= wanted[-1])
                .order_by(TradeCalendar.trade_date)
            )
        )
    position = {day: index for index, day in enumerate(calendar)}
    prev_by_day = {
        day: (calendar[position[day] - 1] if day in position and position[day] > 0 else None)
        for day in wanted
    }
    needed_days = set(wanted)
    needed_days.update(day for day in prev_by_day.values() if day is not None)

    up_by_day: dict[date, dict[str, dict]] = {}
    broken_by_day: dict[date, dict[str, dict]] = {}
    for row in session.execute(
        select(
            LimitPool.trade_date,
            LimitPool.code,
            LimitPool.pool_type,
            LimitPool.consecutive,
            LimitPool.name,
            LimitPool.pct_chg,
            LimitPool.price,
            LimitPool.first_seal_time,
            LimitPool.open_times,
            LimitPool.seal_amount,
        ).where(
            LimitPool.trade_date.in_(needed_days),
            LimitPool.pool_type.in_(("up", "broken")),
        )
    ).all():
        (
            trade_day,
            code,
            pool_type,
            consecutive,
            name,
            pct_chg,
            price,
            first_seal_time,
            open_times,
            seal_amount,
        ) = row
        payload = {
            "consecutive": consecutive,
            "name": name,
            "pct_chg": pct_chg,
            "price": price,
            "first_seal_time": first_seal_time,
            "open_times": open_times,
            "seal_amount": seal_amount,
        }
        if pool_type == "up":
            up_by_day.setdefault(trade_day, {})[code] = payload
        else:
            broken_by_day.setdefault(trade_day, {})[code] = payload

    codes_all = sorted(
        {code for rows in up_by_day.values() for code in rows}
        | {code for rows in broken_by_day.values() for code in rows}
    )

    def chunks(values: list[str] | list[date], size: int = 800) -> list[list]:
        return [values[index : index + size] for index in range(0, len(values), size)]

    # 每天真正需要日线的股票 = 昨日涨停 ∪ 今日涨停 ∪ 今日炸板。
    # 不能按「全窗口所有代码 × 全窗口所有日期」取，那会扫出近百万行（实测 4.6s）。
    relevant_by_day = {
        day: set(up_by_day.get(prev_by_day[day], {}) if prev_by_day[day] else {})
        | set(up_by_day.get(day, {}))
        | set(broken_by_day.get(day, {}))
        for day in wanted
    }

    daily_by_day: dict[date, dict[str, dict]] = {}
    basics: dict[str, dict] = {}
    for day_chunk in chunks(wanted, 10):
        code_union = sorted(set().union(*(relevant_by_day[day] for day in day_chunk)))
        for code_chunk in chunks(code_union):
            for trade_day, code, close, pct_chg in session.execute(
                select(
                    StockDaily.trade_date,
                    StockDaily.code,
                    StockDaily.close,
                    StockDaily.pct_chg,
                ).where(
                    StockDaily.trade_date.in_(day_chunk),
                    StockDaily.code.in_(code_chunk),
                    StockDaily.close.is_not(None),
                )
            ).all():
                if code not in relevant_by_day.get(trade_day, ()):
                    continue
                daily_by_day.setdefault(trade_day, {})[code] = {
                    "close": close,
                    "pct_chg": pct_chg,
                }
    for code_chunk in chunks(codes_all):
        for code, name, industry in session.execute(
            select(StockBasic.code, StockBasic.name, StockBasic.industry).where(
                StockBasic.code.in_(code_chunk)
            )
        ).all():
            basics[code] = {"name": name, "industry": industry}

    up_codes = sorted({code for rows in up_by_day.values() for code in rows})
    boards_by_day: dict[date, dict[str, str]] = {}
    reasons_by_day: dict[date, dict[str, str]] = {}
    for chunk in chunks(up_codes):
        for trade_day, code, concept in session.execute(
            select(StockConcept.trade_date, StockConcept.code, StockConcept.concept)
            .where(
                StockConcept.trade_date.in_(wanted),
                StockConcept.code.in_(chunk),
            )
            .order_by(StockConcept.code, StockConcept.concept)
        ).all():
            # 与原逐日实现一致：同一代码多个 concept 时，最后一条生效
            boards_by_day.setdefault(trade_day, {})[code] = concept
        for trade_day, code, reason in session.execute(
            select(LimitReason.trade_date, LimitReason.code, LimitReason.reason).where(
                LimitReason.trade_date.in_(wanted),
                LimitReason.code.in_(chunk),
            )
        ).all():
            reasons_by_day.setdefault(trade_day, {})[code] = reason

    logs = _latest_logs(session, sorted(needed_days))
    output: dict[date, FunnelInputs] = {}
    for day in wanted:
        prev = prev_by_day[day]
        prev_up_rows = up_by_day.get(prev, {}) if prev else {}
        today_up = up_by_day.get(day, {})
        today_broken = broken_by_day.get(day, {})
        relevant = set(prev_up_rows) | set(today_up) | set(today_broken)
        today_daily = {
            code: value
            for code, value in daily_by_day.get(day, {}).items()
            if code in relevant
        }
        # 原实现的 prev_up 只带 4 个字段，这里保持完全一致
        prev_up = {
            code: {
                "consecutive": row["consecutive"],
                "name": row["name"],
                "pct_chg": row["pct_chg"],
                "price": row["price"],
            }
            for code, row in prev_up_rows.items()
        }
        prev_quality = _source_quality(
            logs.get((prev, "pool_up")) if prev else None, len(prev_up_rows)
        )
        up_quality = _source_quality(logs.get((day, "pool_up")), len(today_up))
        broken_quality = _source_quality(
            logs.get((day, "pool_broken")),
            len(today_broken),
            allow_empty_ok=True,
        )
        output[day] = FunnelInputs(
            trade_date=day,
            prev_date=prev,
            prev_up=prev_up,
            today_up=today_up,
            today_broken=today_broken,
            today_daily=today_daily,
            stock_basic={
                code: basics[code] for code in relevant if code in basics
            },
            boards=boards_by_day.get(day, {}),
            reasons=reasons_by_day.get(day, {}),
            prev_up_quality=prev_quality,
            today_up_quality=up_quality,
            today_broken_quality=broken_quality,
        )
    return output


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


def _has_valid_sample(
    day: date,
    prev_pool: dict[str, int],
    today_up: dict[str, int],
    today_broken: set[str],
    session: Session,
) -> bool:
    """轻量判断该日是否**至少有一只**可验证样本（与逐券分类同口径）。

    先看昨日基数股里有没有晋级/炸板；两者都没有时，只需一次 `EXISTS` 确认
    是否有日线能判失败。没有 N+1 逐券循环，也不会把「全部缺日线」误标成
    「可计算」。
    """
    unresolved: list[str] = []
    for code, level in prev_pool.items():
        in_up = code in today_up
        in_broken = code in today_broken
        if in_up and in_broken:
            continue  # 双池冲突 → 不可验证
        if in_up:
            if int(today_up[code]) == int(level) + 1:
                return True
            continue  # 连板不衔接 → 不可验证
        if in_broken:
            return True
        unresolved.append(code)
    if not unresolved:
        return False
    return bool(
        session.scalar(
            select(
                exists().where(
                    StockDaily.trade_date == day,
                    StockDaily.code.in_(unresolved),
                    StockDaily.close.is_not(None),
                )
            )
        )
    )


def list_funnel_dates(session: Session, days: int = 255) -> list[dict]:
    """最近 N 个**交易日**的可用性摘要（不是最近 N 个有数据的日期）。

    - 交易日序列来自 `trade_calendar`，缺失池的日期也保留，方便用户点进去看
      「未采集 / 失败 / 质量未知」的明确原因；
    - 起点取已有涨停池或涨停池采集日志的最早日期，不向更早的历史凭空扩；
    - 终点取 `today_cn()`，最新交易日尚未采集时也会出现在列表里；
    - `quality_status` 是**池数据**可信度，不代表逐券全部可验证；逐券是否还有
      缺日线/冲突，由单日漏斗接口实际计算后给出。
    - `can_calculate_verified_rate` 只有在昨日池与今日 up/broken 池都可信、
      昨日有基数，且至少存在一只可验证样本时才为 true。这样 up 失败、
      broken 成功，或全部缺日线时，都不会被误标为「可计算」。
    """
    first_pool = session.scalar(
        select(func.min(LimitPool.trade_date)).where(
            LimitPool.pool_type.in_(("up", "broken"))
        )
    )
    first_log = session.scalar(
        select(func.min(CollectLog.trade_date)).where(
            CollectLog.task.in_(("pool_up", "pool_broken"))
        )
    )
    starts = [day for day in (first_pool, first_log) if day is not None]
    if not starts:
        return []
    start = min(starts)
    end = today_cn()
    if start > end:
        return []

    calendar = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date >= start, TradeCalendar.trade_date <= end)
            .order_by(TradeCalendar.trade_date)
        )
    )
    if not calendar:
        return []
    target_dates = calendar[-days:]
    calendar_index = {day: index for index, day in enumerate(calendar)}

    prev_days = {
        calendar[calendar_index[day] - 1]
        for day in target_dates
        if day in calendar_index and calendar_index[day] > 0
    }
    pool_days = set(target_dates) | prev_days

    # 一次批量取代码：用于判断「至少一只可验证样本」，不返回逐券明细。
    up_codes: dict[date, dict[str, int]] = {}
    broken_codes: dict[date, set[str]] = {}
    for day, pool_type, code, consecutive in session.execute(
        select(
            LimitPool.trade_date,
            LimitPool.pool_type,
            LimitPool.code,
            LimitPool.consecutive,
        ).where(
            LimitPool.trade_date.in_(pool_days),
            LimitPool.pool_type.in_(("up", "broken")),
        )
    ).all():
        if pool_type == "up":
            up_codes.setdefault(day, {})[code] = consecutive or 1
        else:
            broken_codes.setdefault(day, set()).add(code)

    logs = _latest_logs(session, sorted(set(target_dates) | prev_days))

    output: list[dict] = []
    for day in target_dates:
        index = calendar_index.get(day)
        prev_day = calendar[index - 1] if index is not None and index > 0 else None
        prev_pool = up_codes.get(prev_day, {}) if prev_day else {}
        today_up = up_codes.get(day, {})
        today_broken = broken_codes.get(day, set())
        prev_count = len(prev_pool)
        prev_quality = _source_quality(
            logs.get((prev_day, "pool_up")) if prev_day else None, prev_count
        )
        up_quality = _source_quality(logs.get((day, "pool_up")), len(today_up))
        broken_quality = _source_quality(
            logs.get((day, "pool_broken")),
            len(today_broken),
            allow_empty_ok=True,
        )
        status = _day_status(prev_quality, up_quality, broken_quality, prev_count)
        source_ready = (
            prev_quality.trusted
            and prev_count > 0
            and up_quality.trusted
            and broken_quality.trusted
        )
        output.append(
            {
                "trade_date": day,
                "prev_date": prev_day,
                "has_previous_pool": prev_count > 0,
                "has_today_pool": bool(today_up),
                "quality_status": status,
                "can_calculate_verified_rate": (
                    source_ready
                    and _has_valid_sample(
                        day, prev_pool, today_up, today_broken, session
                    )
                ),
            }
        )
    return output
