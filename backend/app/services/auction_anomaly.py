"""竞价异动与数据质量协议（P0-A-03 / P0-A-04 / P0-A-05 / P0-A-06）。

本模块**只读本地库**，不发外部请求、不写数据库。数据库读取与纯函数分开：

- `assess_day_quality()` / `assess_stock_quality()` 是纯函数，便于离线测试；
- `build_anomaly()` 负责一次性批量读取，再调用纯函数组装结果。

## 时间口径

只认 **0926 竞价终值** 与 **0931 开盘后帧**：

- `auction_price` / `auction_amount` 来自 `samples` 里的 `0926` 帧，不拿主表 `price`
  顶替（主表可能在最后几帧被覆盖）；
- `post_open_price` 是 0931 帧写入的列；
- `pct_change_after_auction = post_open_pct - auction_pct`，单位是**百分点**，不是
  成交额变化，也不是价格变化率；
- 缺 0931 时开盘承接为 null，不写 0。

## 质量状态

日期级：`NOT_COLLECTED` / `SOURCE_UNAVAILABLE` / `INCOMPLETE` / `VALID`。

逐券：`VALID` / `NO_QUOTE` / `FRAME_MISSING` / `STALE_QUOTE` /
`SOURCE_TIME_UNKNOWN` / `INCOMPLETE`。

「未采集」「源不可用」「股票无报价」是三种不同的事，不合并成一个状态。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    SAMPLE_AMOUNT,
    SAMPLE_PRICE,
    AuctionSnapshot,
    CollectLog,
    StockBasic,
    StockConcept,
    StockDaily,
)
from app.services.limit_funnel import (
    FAILED,
    NOT_COLLECTED as FUNNEL_NOT_COLLECTED,
    PARTIAL as FUNNEL_PARTIAL,
    UNVERIFIED as FUNNEL_UNVERIFIED,
    VERIFIED as FUNNEL_VERIFIED,
    build_promotion_funnel,
    load_funnel_inputs,
)
from app.services.trading_days import previous_trade_day
from app.timeutil import today_cn

FRAME_TASK = "auction_frame"
RESULT_FRAME = "0926"
POST_OPEN_FRAME = "0931"
RESULT_FRAMES = tuple(f"09{minute:02d}" for minute in range(15, 27))

NOT_COLLECTED = "NOT_COLLECTED"
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
INCOMPLETE = "INCOMPLETE"
VALID = "VALID"

STOCK_VALID = "VALID"
NO_QUOTE = "NO_QUOTE"
FRAME_MISSING = "FRAME_MISSING"
STALE_QUOTE = "STALE_QUOTE"
SOURCE_TIME_UNKNOWN = "SOURCE_TIME_UNKNOWN"
INCOMPLETE_STOCK = "INCOMPLETE"

#: 这些原因会直接让「9:25→9:31 涨幅变化」不可计算：价格本身来源不明 / 越界 / 缺失。
PRICE_BLOCKING_REASONS = {
    "NO_AUCTION_ROW",
    "NO_AUCTION_QUOTE",
    "NO_0926_QUOTE",
    "STALE_SOURCE_QUOTE",
    "SOURCE_TIME_UNKNOWN",
    "FRAME_LOG_MISSING_0926",
    "FRAME_LOG_MISSING_0931",
}

GROUP_ALL = "all"
GROUP_PREV_LIMIT = "prev_limit"
GROUP_PREV_BROKEN = "prev_broken"
GROUP_PREV_FAILED_RELAY = "prev_failed_relay"
GROUPS = (GROUP_ALL, GROUP_PREV_LIMIT, GROUP_PREV_BROKEN, GROUP_PREV_FAILED_RELAY)

GROUP_LABELS = {
    GROUP_ALL: "全部",
    GROUP_PREV_LIMIT: "昨日涨停",
    GROUP_PREV_BROKEN: "昨日炸板",
    GROUP_PREV_FAILED_RELAY: "昨日断板",
}

SORTS = ("auction_amount", "auction_pct", "post_open_change")

REASON_TEXT = {
    "NO_AUCTION_SNAPSHOT": "当日没有竞价快照",
    "NO_AUCTION_ROW": "该股当日没有竞价记录",
    "NO_AUCTION_QUOTE": "该股没有竞价成交价",
    "NO_0926_QUOTE": "缺少 0926 竞价终值",
    "FRAME_MISSING_0926": "缺少 0926 竞价终值帧",
    "FRAME_MISSING_0931": "缺少 0931 开盘后帧",
    "FRAME_LOG_MISSING_0926": "缺少 0926 帧采集证据",
    "FRAME_LOG_MISSING_0931": "缺少 0931 帧采集证据",
    "FRAME_0926_NOT_OK": "0926 帧采集状态异常",
    "FRAME_0931_NOT_OK": "0931 帧采集状态异常",
    "POST_OPEN_MISSING": "缺少 0931 开盘后价格",
    "STALE_SOURCE_QUOTE": "源报价时间不在计划区间",
    "SOURCE_TIME_UNKNOWN": "源报价时间缺失，无法确认来源时间",
    "BATCH_FAILED": "数据源存在失败批次",
    "STALE_QUOTE": "存在被丢弃的旧报价/越界报价",
    "AUCTION_AMOUNT_MISSING": "缺少竞价成交金额",
    "PRE_CLOSE_MISSING": "缺少昨收，无法计算竞价涨幅",
    "YESTERDAY_AMOUNT_MISSING": "缺少昨日全天成交额",
    "PREV_LIMIT_POOL_MISSING": "昨日涨停池未采集",
    "PREV_LIMIT_POOL_EMPTY": "昨日涨停池日志成功但实际 0 行",
    "PREV_LIMIT_POOL_UNVERIFIED": "昨日涨停池采集状态不可信",
    "PREV_BROKEN_POOL_MISSING": "昨日炸板池未采集",
    "PREV_BROKEN_POOL_UNVERIFIED": "昨日炸板池采集状态不可信",
    "PREV_RELAY_UNVERIFIED": "昨日断板分类缺少可信连板失败证据",
    "NO_PREVIOUS_TRADE_DAY": "没有上一交易日",
    "NO_AUCTION_ROWS": "当日没有任何竞价快照",
}


def _frame_value(samples: list | None, hhmm: str, index: int) -> Any:
    """取 `samples` 中某一帧的第 `index` 列；缺失/老格式返回 None。"""
    for frame in samples or []:
        if len(frame) > index and frame[0] == hhmm:
            return frame[index]
    return None


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _log_status(payload: dict) -> str:
    return str(payload.get("status") or "").lower()


def _frame_logs(session: Session, day: date) -> dict[str, dict]:
    """`{scheduled_frame: payload}`；同一帧重复执行时取 **id 最大** 的那条。"""
    rows = session.scalars(
        select(CollectLog)
        .where(CollectLog.trade_date == day, CollectLog.task == FRAME_TASK)
        .order_by(CollectLog.id.desc())
    ).all()
    latest: dict[str, dict] = {}
    for row in rows:
        try:
            payload = json.loads(row.message or "{}")
        except (TypeError, ValueError):
            payload = {}
        frame = payload.get("scheduled_frame")
        if not frame or frame in latest:
            continue
        payload["status"] = row.status
        latest[str(frame)] = payload
    return latest


def assess_day_quality(
    rows: list[AuctionSnapshot], logs: dict[str, dict]
) -> dict[str, Any]:
    """日期级质量。只看**池数据/帧证据**，不逐券展开。"""
    reasons: list[str] = []
    has_rows = bool(rows)
    has_0926 = any(
        _frame_value(row.samples, RESULT_FRAME, SAMPLE_PRICE) is not None
        for row in rows
    )
    has_0931 = any(row.post_open_price is not None for row in rows)

    if not has_rows and not logs:
        return {
            "status": NOT_COLLECTED,
            "reasons": ["NO_AUCTION_SNAPSHOT"],
            "frames": [],
            "auction_count": 0,
            "result_count": 0,
            "post_open_count": 0,
        }

    if not has_rows:
        statuses = {_log_status(payload) for payload in logs.values()}
        if statuses and statuses <= {"skipped"} and all(
            payload.get("reason") == "NON_TRADE_DAY" for payload in logs.values()
        ):
            status = NOT_COLLECTED
            reasons.append("NO_AUCTION_SNAPSHOT")
        elif statuses and statuses <= {"failed"}:
            status = SOURCE_UNAVAILABLE
            reasons.append("NO_AUCTION_QUOTE")
        else:
            status = INCOMPLETE
            reasons.append("NO_AUCTION_SNAPSHOT")
        return {
            "status": status,
            "reasons": _dedupe(reasons),
            "frames": _frame_details(logs),
            "auction_count": 0,
            "result_count": 0,
            "post_open_count": 0,
        }

    if not has_0926:
        statuses = {_log_status(payload) for payload in logs.values()}
        if statuses and statuses <= {"failed"}:
            status = SOURCE_UNAVAILABLE
        else:
            status = INCOMPLETE
        reasons.append("FRAME_MISSING_0926")
    else:
        status = VALID
        log926 = logs.get(RESULT_FRAME)
        if log926 is None:
            reasons.append("FRAME_LOG_MISSING_0926")
        else:
            if _log_status(log926) not in ("ok", "partial"):
                reasons.append("FRAME_0926_NOT_OK")
            if not log926.get("source_quote_time"):
                reasons.append("SOURCE_TIME_UNKNOWN")
            if (
                log926.get("dropped_old_quote")
                or log926.get("dropped_stale_quote")
                or log926.get("dropped_newer_quote")
                or log926.get("dropped_unknown_quote")
            ):
                reasons.append("STALE_QUOTE")

        if not has_0931:
            reasons.append("POST_OPEN_MISSING")
            if logs.get(POST_OPEN_FRAME) is None:
                reasons.append("FRAME_LOG_MISSING_0931")
        else:
            log931 = logs.get(POST_OPEN_FRAME)
            if log931 is None:
                reasons.append("FRAME_LOG_MISSING_0931")
            elif _log_status(log931) not in ("ok", "partial"):
                reasons.append("FRAME_0931_NOT_OK")

        if any(int(payload.get("failed_batches") or 0) > 0 for payload in logs.values()):
            reasons.append("BATCH_FAILED")
        if any(int(payload.get("source_time_unknown") or 0) > 0 for payload in logs.values()):
            reasons.append("SOURCE_TIME_UNKNOWN")
        if reasons:
            status = INCOMPLETE

    return {
        "status": status,
        "reasons": _dedupe(reasons),
        "frames": _frame_details(logs),
        "auction_count": len(rows),
        "result_count": sum(
            1
            for row in rows
            if _frame_value(row.samples, RESULT_FRAME, SAMPLE_PRICE) is not None
        ),
        "post_open_count": sum(1 for row in rows if row.post_open_price is not None),
    }


def _frame_details(logs: dict[str, dict]) -> list[dict]:
    return [logs[frame] for frame in sorted(logs)]


def assess_stock_quality(
    row: AuctionSnapshot | None, logs: dict[str, dict], code: str
) -> tuple[str, list[str]]:
    """逐券质量。先判「有没有 0926」，再判来源时间与金额完整性。"""
    log926 = logs.get(RESULT_FRAME)
    raw926 = log926 or {}
    stale_codes = set(raw926.get("dropped_stale_codes") or []) | set(
        raw926.get("dropped_newer_codes") or []
    )
    unknown_codes = set(raw926.get("source_time_unknown_codes") or [])

    if row is None:
        if code in stale_codes:
            return STALE_QUOTE, ["STALE_SOURCE_QUOTE"]
        return NO_QUOTE, ["NO_AUCTION_ROW"]

    price = _frame_value(row.samples, RESULT_FRAME, SAMPLE_PRICE)
    if price is None:
        if any(
            _frame_value(row.samples, frame, SAMPLE_PRICE) is not None
            for frame in RESULT_FRAMES
        ):
            return FRAME_MISSING, ["NO_0926_QUOTE"]
        return NO_QUOTE, ["NO_AUCTION_QUOTE"]

    reasons: list[str] = []
    status = STOCK_VALID
    if code in stale_codes:
        status = STALE_QUOTE
        reasons.append("STALE_SOURCE_QUOTE")
    if log926 is None:
        status = INCOMPLETE_STOCK if status == STOCK_VALID else status
        reasons.append("FRAME_LOG_MISSING_0926")
    else:
        if _log_status(log926) not in ("ok", "partial"):
            status = INCOMPLETE_STOCK if status == STOCK_VALID else status
            reasons.append("FRAME_0926_NOT_OK")
        if code in unknown_codes or not log926.get("source_quote_time"):
            status = SOURCE_TIME_UNKNOWN if status == STOCK_VALID else status
            reasons.append("SOURCE_TIME_UNKNOWN")

    if _frame_value(row.samples, RESULT_FRAME, SAMPLE_AMOUNT) is None:
        status = INCOMPLETE_STOCK if status == STOCK_VALID else status
        reasons.append("AUCTION_AMOUNT_MISSING")

    if row.post_open_price is None:
        reasons.append("POST_OPEN_MISSING")
    else:
        log931 = logs.get(POST_OPEN_FRAME)
        if log931 is None:
            status = INCOMPLETE_STOCK if status == STOCK_VALID else status
            reasons.append("FRAME_LOG_MISSING_0931")
        else:
            if _log_status(log931) not in ("ok", "partial"):
                status = INCOMPLETE_STOCK if status == STOCK_VALID else status
                reasons.append("FRAME_0931_NOT_OK")
            if not log931.get("source_quote_time"):
                status = SOURCE_TIME_UNKNOWN if status == STOCK_VALID else status
                reasons.append("SOURCE_TIME_UNKNOWN")
    return status, _dedupe(reasons)


def _pool_reason(prefix: str, quality) -> str | None:
    if quality.trusted:
        return None
    if quality.status == "not_collected":
        return f"{prefix}_MISSING"
    if quality.status == "empty":
        return f"{prefix}_EMPTY"
    return f"{prefix}_UNVERIFIED"


def _prev_sets(
    session: Session, prev: date | None
) -> tuple[
    dict[str, int],
    dict[str, int | None],
    dict[str, int],
    set[str],
    list[str],
]:
    """昨日涨停 / 炸板 / 断板代码与质量原因。

    断板只取 `limit_funnel` 里**逐券 FAILED** 的股票；如果昨日连板漏斗整体不可信，
    返回空集并给出原因 —— 绝不把「昨日不在涨停池」的股票都当断板。
    """
    if prev is None:
        return {}, {}, {}, set(), ["NO_PREVIOUS_TRADE_DAY"]

    prev_prev = previous_trade_day(prev, session)
    inputs = load_funnel_inputs(session, prev_prev, prev)
    reasons: list[str] = []

    limit_reason = _pool_reason("PREV_LIMIT_POOL", inputs.today_up_quality)
    broken_reason = _pool_reason("PREV_BROKEN_POOL", inputs.today_broken_quality)
    if limit_reason:
        reasons.append(limit_reason)
    if broken_reason:
        reasons.append(broken_reason)

    up = {
        code: int(row.get("consecutive") or 1)
        for code, row in inputs.today_up.items()
    } if inputs.today_up_quality.trusted else {}
    broken = {
        code: (int(row["consecutive"]) if row.get("consecutive") is not None else None)
        for code, row in inputs.today_broken.items()
    } if inputs.today_broken_quality.trusted else {}

    failed: dict[str, int] = {}
    levels, relay_quality = build_promotion_funnel(inputs, list(range(1, 11)))
    if relay_quality.status in (FUNNEL_VERIFIED, FUNNEL_PARTIAL):
        for level in levels:
            for stock in level["stocks"]:
                if stock["status"] == FAILED:
                    failed[stock["code"]] = int(level["level"])
    elif relay_quality.status in (FUNNEL_NOT_COLLECTED, FUNNEL_UNVERIFIED):
        reasons.append("PREV_RELAY_UNVERIFIED")

    return up, broken, failed, set(up) | set(broken) | set(failed), _dedupe(reasons)


def _snapshot_map(
    session: Session, day: date, codes: list[str] | None
) -> dict[str, AuctionSnapshot]:
    stmt = select(AuctionSnapshot).where(AuctionSnapshot.trade_date == day)
    if codes is not None:
        if not codes:
            return {}
        stmt = stmt.where(AuctionSnapshot.code.in_(codes))
    return {row.code: row for row in session.scalars(stmt)}


def _stock_meta(session: Session, codes: list[str]) -> dict[str, tuple[str | None, str | None]]:
    if not codes:
        return {}
    return {
        code: (name, industry)
        for code, name, industry in session.execute(
            select(StockBasic.code, StockBasic.name, StockBasic.industry).where(
                StockBasic.code.in_(codes)
            )
        ).all()
    }


def _concept_map(
    session: Session, codes: list[str], prev: date | None, day: date
) -> dict[str, str]:
    if not codes:
        return {}
    result: dict[str, str] = {}
    for target in (prev, day):
        if target is None:
            continue
        for code, concept in session.execute(
            select(StockConcept.code, StockConcept.concept)
            .where(
                StockConcept.trade_date == target,
                StockConcept.code.in_(codes),
            )
            .order_by(StockConcept.code, StockConcept.concept)
        ).all():
            result.setdefault(code, concept)
    return result


def _prev_amounts(
    session: Session, prev: date | None, codes: list[str]
) -> dict[str, float]:
    if prev is None or not codes:
        return {}
    return {
        code: amount
        for code, amount in session.execute(
            select(StockDaily.code, StockDaily.amount).where(
                StockDaily.trade_date == prev,
                StockDaily.code.in_(codes),
            )
        ).all()
        if amount is not None
    }


def _auction_pct(price: float | None, pre_close: float | None) -> float | None:
    if price is None or not pre_close or pre_close <= 0:
        return None
    return round((price / pre_close - 1) * 100, 2)


def _post_open_pct(price: float | None, pre_close: float | None) -> float | None:
    return _auction_pct(price, pre_close)


def _row_payload(
    *,
    trade_date: date,
    prev_date: date | None,
    code: str,
    snapshot: AuctionSnapshot | None,
    logs: dict[str, dict],
    name: str | None,
    industry: str | None,
    concept: str | None,
    prev_status: str | None,
    prev_board: int | None,
    previous_amount: float | None,
) -> dict[str, Any]:
    quality_status, reasons = assess_stock_quality(snapshot, logs, code)
    price = (
        _frame_value(snapshot.samples, RESULT_FRAME, SAMPLE_PRICE)
        if snapshot is not None
        else None
    )
    amount = (
        _frame_value(snapshot.samples, RESULT_FRAME, SAMPLE_AMOUNT)
        if snapshot is not None
        else None
    )
    pre_close = snapshot.pre_close if snapshot is not None else None
    auction_pct = _auction_pct(price, pre_close)
    if price is not None and auction_pct is None:
        reasons = _dedupe(reasons + ["PRE_CLOSE_MISSING"])
    post_open_price = snapshot.post_open_price if snapshot is not None else None
    post_open_pct = _post_open_pct(post_open_price, pre_close)
    if previous_amount is None or previous_amount <= 0:
        if amount is not None:
            reasons = _dedupe(reasons + ["YESTERDAY_AMOUNT_MISSING"])
        amount_ratio = None
    else:
        amount_ratio = (
            round(amount / previous_amount * 100, 2) if amount is not None else None
        )

    # 只有两个时点的价格都来源可信，才给「9:25→9:31 涨幅变化」；否则 null
    price_blocked = any(reason in PRICE_BLOCKING_REASONS for reason in reasons)
    pct_change_after_auction = (
        round(post_open_pct - auction_pct, 2)
        if post_open_pct is not None and auction_pct is not None and not price_blocked
        else None
    )

    resolved_name = name or (snapshot.name if snapshot is not None else None)
    return {
        "trade_date": trade_date,
        "prev_trade_date": prev_date,
        "code": code,
        "name": resolved_name,
        "industry": industry,
        "concept": concept,
        "concept_source": "开盘啦" if concept else None,
        "prev_limit_status": prev_status,
        "prev_board_height": prev_board,
        "auction_price": price,
        "auction_pct": auction_pct,
        "auction_amount": amount,
        "yesterday_amount": previous_amount,
        "auction_amount_ratio": amount_ratio,
        "post_open_price": post_open_price,
        "post_open_pct": post_open_pct,
        "pct_change_after_auction": pct_change_after_auction,
        "auction_frame": RESULT_FRAME if price is not None else None,
        "post_open_frame": POST_OPEN_FRAME if post_open_price is not None else None,
        "data_quality_status": quality_status,
        "quality_reasons": _dedupe(reasons),
    }


def _group_rows(
    session: Session,
    *,
    trade_date: date,
    prev: date | None,
    group: str,
    codes: list[str] | None,
    up: dict[str, int],
    broken: dict[str, int | None],
    failed: dict[str, int],
    snapshots: dict[str, AuctionSnapshot],
    logs: dict[str, dict],
) -> list[dict[str, Any]]:
    if codes is None:
        codes = sorted(snapshots)
    meta = _stock_meta(session, codes)
    concepts = _concept_map(session, codes, prev, trade_date)
    amounts = _prev_amounts(session, prev, codes)

    rows: list[dict[str, Any]] = []
    for code in codes:
        if group == GROUP_PREV_LIMIT:
            prev_status, prev_board = "limit_up", up.get(code)
        elif group == GROUP_PREV_BROKEN:
            prev_status, prev_board = "broken", broken.get(code)
        elif group == GROUP_PREV_FAILED_RELAY:
            prev_status, prev_board = "failed_relay", failed.get(code)
        else:
            if code in up:
                prev_status, prev_board = "limit_up", up[code]
            elif code in broken:
                prev_status, prev_board = "broken", broken[code]
            elif code in failed:
                prev_status, prev_board = "failed_relay", failed[code]
            else:
                prev_status, prev_board = None, None
        name, industry = meta.get(code, (None, None))
        rows.append(
            _row_payload(
                trade_date=trade_date,
                prev_date=prev,
                code=code,
                snapshot=snapshots.get(code),
                logs=logs,
                name=name,
                industry=industry,
                concept=concepts.get(code),
                prev_status=prev_status,
                prev_board=prev_board,
                previous_amount=amounts.get(code),
            )
        )
    return rows


def _sort_rows(rows: list[dict], sort: str, order: str) -> list[dict]:
    key = {
        "auction_amount": "auction_amount",
        "auction_pct": "auction_pct",
        "post_open_change": "pct_change_after_auction",
    }[sort]
    valued = [row for row in rows if row.get(key) is not None]
    blank = [row for row in rows if row.get(key) is None]
    valued.sort(key=lambda row: row[key], reverse=order != "asc")
    return valued + blank


def build_anomaly(
    session: Session,
    trade_date: date | None,
    group: str,
    sort: str,
    order: str,
    limit: int,
) -> dict[str, Any]:
    """组装竞价异动结果。所有分组一次算完，只对选中分组截断。"""
    latest = session.scalar(select(AuctionSnapshot.trade_date).order_by(
        AuctionSnapshot.trade_date.desc()
    ).limit(1))
    day = trade_date or latest or today_cn()
    prev = previous_trade_day(day, session) if day <= today_cn() else None

    all_snapshots = _snapshot_map(session, day, None)
    logs = _frame_logs(session, day)
    day_quality = assess_day_quality(list(all_snapshots.values()), logs)

    up, broken, failed, _, prev_reasons = _prev_sets(session, prev)
    codes_by_group = {
        GROUP_ALL: None,
        GROUP_PREV_LIMIT: sorted(up) if up else [],
        GROUP_PREV_BROKEN: sorted(broken) if broken else [],
        GROUP_PREV_FAILED_RELAY: sorted(failed) if failed else [],
    }

    rows_by_group: dict[str, list[dict]] = {}
    groups: list[dict] = []
    for name in GROUPS:
        rows = _group_rows(
            session,
            trade_date=day,
            prev=prev,
            group=name,
            codes=codes_by_group[name],
            up=up,
            broken=broken,
            failed=failed,
            snapshots=all_snapshots,
            logs=logs,
        )
        rows_by_group[name] = rows
        valid = sum(1 for row in rows if row["data_quality_status"] == STOCK_VALID)
        reasons: list[str] = []
        if name != GROUP_ALL:
            reasons.extend(prev_reasons)
            if not rows:
                reasons.append(
                    {
                        GROUP_PREV_LIMIT: "PREV_LIMIT_POOL_MISSING",
                        GROUP_PREV_BROKEN: "PREV_BROKEN_POOL_MISSING",
                        GROUP_PREV_FAILED_RELAY: "PREV_RELAY_UNVERIFIED",
                    }[name]
                )
        groups.append(
            {
                "group": name,
                "label": GROUP_LABELS[name],
                "total": len(rows),
                "valid": valid,
                "quality_status": (
                    INCOMPLETE
                    if reasons and day_quality["status"] == VALID
                    else day_quality["status"]
                ),
                "quality_reasons": _dedupe(reasons),
            }
        )

    selected = _sort_rows(rows_by_group[group], sort, order)
    return {
        "trade_date": day,
        "prev_trade_date": prev,
        "group": group,
        "sort": sort,
        "order": order,
        "limit": limit,
        "total": len(selected),
        "shown": len(selected[:limit]),
        "rows": selected[:limit],
        "groups": groups,
        "quality": day_quality,
    }
