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
from datetime import date, time
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
from app.services.trading_days import is_trade_day, previous_trade_day
from app.timeutil import now_cn, today_cn

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
POST_OPEN_PENDING = "PENDING"

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
    "FRAME_0926_PARTIAL": "0926 帧部分可信（存在已知质量损失）",
    "FRAME_0931_PARTIAL": "0931 帧部分可信（存在已知质量损失）",
    "FRAME_0926_LAST_ATTEMPT_FAILED": "0926 最近一次尝试失败（此前有效报价仍在）",
    "FRAME_0931_LAST_ATTEMPT_FAILED": "0931 最近一次尝试失败（此前有效报价仍在）",
    "FRAME_0926_FILTERED": "0926 帧存在被时间过滤的报价",
    "FRAME_0931_FILTERED": "0931 帧存在被时间过滤的报价",
    "FRAME_0926_WRITE_GAP": "0926 帧有效报价未全部写入",
    "FRAME_0931_WRITE_GAP": "0931 帧有效报价未全部写入",
    "POST_OPEN_MISSING": "缺少 0931 开盘后价格",
    "POST_OPEN_PENDING": "9:31 尚未到达，开盘承接待观察",
    "NO_POST_OPEN_QUOTE": "该股没有 0931 开盘后报价",
    "COVERAGE_GAP": "存在未解释的覆盖缺口",
    "SOURCE_TIME_UNKNOWN": "源报价时间缺失，无法确认来源时间",
    "STALE_SOURCE_QUOTE": "源报价时间不在计划区间",
    "SOURCE_QUOTE_OUT_OF_WINDOW": "源报价时间晚于计划帧，已丢弃",
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
    """`{scheduled_frame: payload}`。

    同一帧重复执行时，质量结论以**最后一次真正持久化过行情**的日志为准
    （S1-A1：`ok` / `partial`，或纯重复请求 `noop`）。失败 / skipped 的尝试
    可能出现在成功之后，它们不会改动库里的行情，因此不得把已经存在的合法行情
    判成无效 —— 但必须保留在诊断字段里（`last_attempt_*`），供页面区分
    「上一次有效报价」与「最近一次尝试失败」。
    """
    rows = session.scalars(
        select(CollectLog)
        .where(CollectLog.trade_date == day, CollectLog.task == FRAME_TASK)
        .order_by(CollectLog.id.desc())
    ).all()
    latest_any: dict[str, dict] = {}
    latest_effective: dict[str, dict] = {}
    latest_noop: dict[str, dict] = {}
    for row in rows:
        try:
            payload = json.loads(row.message or "{}")
        except (TypeError, ValueError):
            payload = {}
        frame = payload.get("scheduled_frame")
        if not frame:
            continue
        payload["status"] = row.status
        payload["log_id"] = row.id
        frame = str(frame)
        if frame not in latest_any:
            latest_any[frame] = payload
        status = str(row.status or "").lower()
        if status in ("ok", "partial"):
            if frame not in latest_effective:
                latest_effective[frame] = payload
        elif payload.get("noop") and frame not in latest_noop:
            latest_noop[frame] = payload
    merged: dict[str, dict] = {}
    for frame, latest in latest_any.items():
        durable = latest_effective.get(frame) or latest_noop.get(frame)
        chosen = dict(durable) if durable is not None else dict(latest)
        chosen["last_attempt_status"] = latest.get("status")
        chosen["last_attempt_reason"] = latest.get("reason")
        chosen["last_attempt_started_at"] = latest.get("actual_started_at")
        chosen["last_attempt_log_id"] = latest.get("log_id")
        chosen["last_attempt_failed"] = (
            str(latest.get("status") or "").lower() in ("failed", "skipped")
            and not latest.get("noop")
        )
        chosen["last_effective_status"] = (durable or {}).get("status")
        merged[frame] = chosen
    return merged


def _last_attempt_warnings(logs: dict[str, dict]) -> list[str]:
    """最近一次尝试失败、但此前有效数据仍在：只是诊断提示，不降级日期质量。"""
    warnings: list[str] = []
    for frame, label in ((RESULT_FRAME, "0926"), (POST_OPEN_FRAME, "0931")):
        log = logs.get(frame)
        if log and log.get("last_attempt_failed") and log.get("last_effective_status"):
            warnings.append(f"FRAME_{label}_LAST_ATTEMPT_FAILED")
    return warnings


def _frame_issue_reasons(log: dict | None, label: str) -> list[str]:
    """一帧的已知质量损失。`ok` 才可能没有原因；`partial` 永远有原因。"""
    if log is None:
        return [f"FRAME_LOG_MISSING_{label}"]
    reasons: list[str] = []
    status = _log_status(log)
    if status == "partial":
        reasons.append(f"FRAME_{label}_PARTIAL")
    elif status != "ok":
        reasons.append(f"FRAME_{label}_NOT_OK")
    if int(log.get("failed_batches") or 0) > 0:
        reasons.append("BATCH_FAILED")
    if any(
        int(log.get(f"dropped_{kind}_quote") or 0) > 0
        for kind in ("old", "stale", "newer", "unknown")
    ):
        reasons.append("STALE_QUOTE")
    unknown_count = int(
        log.get("source_time_unknown_count", log.get("source_time_unknown") or 0)
    )
    if unknown_count > 0:
        reasons.append("SOURCE_TIME_UNKNOWN")
    requested = int(log.get("requested_codes") or 0)
    returned = int(log.get("returned_codes") or 0)
    valid = int(log.get("valid_codes") or 0)
    written = int(log.get("written_codes") or 0)
    already = int(log.get("already_covered_codes") or 0)
    if requested > returned:
        reasons.append("COVERAGE_GAP")
    if returned > valid:
        reasons.append(f"FRAME_{label}_FILTERED")
    if written + already < valid:
        reasons.append(f"FRAME_{label}_WRITE_GAP")
    if written > int(log.get("quote_times_count") or 0):
        reasons.append("SOURCE_TIME_UNKNOWN")
    return reasons


def assess_day_quality(
    rows: list[AuctionSnapshot],
    logs: dict[str, dict],
    *,
    post_open_pending: bool = False,
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
            "warnings": [],
            "frames": [],
            "auction_count": 0,
            "result_count": 0,
            "post_open_count": 0,
            "auction_coverage_ratio": None,
            "auction_coverage_status": None,
            "post_open_coverage_ratio": None,
            "post_open_coverage_status": None,
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
            for frame, label in ((RESULT_FRAME, "0926"), (POST_OPEN_FRAME, "0931")):
                reasons.extend(_frame_issue_reasons(logs.get(frame), label))
        return {
            "status": status,
            "reasons": _dedupe(reasons),
            "warnings": _last_attempt_warnings(logs),
            "frames": _frame_details(logs),
            "auction_count": 0,
            "result_count": 0,
            "post_open_count": 0,
            "auction_coverage_ratio": (logs.get(RESULT_FRAME) or {}).get("coverage_ratio"),
            "auction_coverage_status": (logs.get(RESULT_FRAME) or {}).get("coverage_status"),
            "post_open_coverage_ratio": (logs.get(POST_OPEN_FRAME) or {}).get("coverage_ratio"),
            "post_open_coverage_status": (logs.get(POST_OPEN_FRAME) or {}).get("coverage_status"),
        }

    if not has_0926:
        statuses = {_log_status(payload) for payload in logs.values()}
        if statuses and statuses <= {"failed"}:
            status = SOURCE_UNAVAILABLE
        else:
            status = INCOMPLETE
        reasons.append("FRAME_MISSING_0926")
        reasons.extend(_frame_issue_reasons(logs.get(RESULT_FRAME), "0926"))
    else:
        reasons.extend(_frame_issue_reasons(logs.get(RESULT_FRAME), "0926"))
        if not has_0931:
            if post_open_pending and logs.get(POST_OPEN_FRAME) is None:
                reasons.append("POST_OPEN_PENDING")
            else:
                reasons.append("POST_OPEN_MISSING")
                reasons.extend(_frame_issue_reasons(logs.get(POST_OPEN_FRAME), "0931"))
        else:
            reasons.extend(_frame_issue_reasons(logs.get(POST_OPEN_FRAME), "0931"))
        status = INCOMPLETE if reasons else VALID

    return {
        "status": status,
        "reasons": _dedupe(reasons),
        "warnings": _last_attempt_warnings(logs),
        "frames": _frame_details(logs),
        "auction_count": len(rows),
        "result_count": sum(
            1
            for row in rows
            if _frame_value(row.samples, RESULT_FRAME, SAMPLE_PRICE) is not None
        ),
        "post_open_count": sum(1 for row in rows if row.post_open_price is not None),
        "auction_coverage_ratio": (logs.get(RESULT_FRAME) or {}).get("coverage_ratio"),
        "auction_coverage_status": (logs.get(RESULT_FRAME) or {}).get("coverage_status"),
        "post_open_coverage_ratio": (logs.get(POST_OPEN_FRAME) or {}).get("coverage_ratio"),
        "post_open_coverage_status": (logs.get(POST_OPEN_FRAME) or {}).get("coverage_status"),
    }


def _frame_details(logs: dict[str, dict]) -> list[dict]:
    return [logs[frame] for frame in sorted(logs)]


def _code_source_known(log: dict | None, code: str) -> bool:
    """该证券是否有**逐券**源时间证据；帧级汇总时间不算。"""
    if not log:
        return False
    return code in (log.get("quote_times") or {})


def assess_stock_quality(
    row: AuctionSnapshot | None,
    logs: dict[str, dict],
    code: str,
    *,
    post_open_pending: bool = False,
) -> tuple[str, str, str, list[str]]:
    """逐券质量：竞价终值与开盘承接各自独立，再合成整体状态。"""
    log926 = logs.get(RESULT_FRAME)
    log931 = logs.get(POST_OPEN_FRAME)
    stale_codes = set((log926 or {}).get("dropped_stale_codes") or [])
    newer_codes = set((log926 or {}).get("dropped_newer_codes") or [])

    # ---------- A. 竞价终值 ----------
    if row is None:
        if code in stale_codes:
            auction_status, auction_reasons = STALE_QUOTE, ["STALE_SOURCE_QUOTE"]
        elif code in newer_codes:
            auction_status, auction_reasons = STALE_QUOTE, ["SOURCE_QUOTE_OUT_OF_WINDOW"]
        else:
            auction_status, auction_reasons = NO_QUOTE, ["NO_AUCTION_ROW"]
    else:
        price = _frame_value(row.samples, RESULT_FRAME, SAMPLE_PRICE)
        if price is None:
            if any(
                _frame_value(row.samples, frame, SAMPLE_PRICE) is not None
                for frame in RESULT_FRAMES
            ):
                auction_status, auction_reasons = FRAME_MISSING, ["NO_0926_QUOTE"]
            else:
                auction_status, auction_reasons = NO_QUOTE, ["NO_AUCTION_QUOTE"]
        elif code in stale_codes:
            auction_status, auction_reasons = STALE_QUOTE, ["STALE_SOURCE_QUOTE"]
        elif code in newer_codes:
            auction_status, auction_reasons = STALE_QUOTE, ["SOURCE_QUOTE_OUT_OF_WINDOW"]
        elif log926 is None:
            auction_status, auction_reasons = (
                SOURCE_TIME_UNKNOWN,
                ["FRAME_LOG_MISSING_0926", "SOURCE_TIME_UNKNOWN"],
            )
        elif _log_status(log926) == "partial":
            auction_status, auction_reasons = (
                INCOMPLETE_STOCK,
                ["FRAME_0926_PARTIAL"],
            )
        elif _log_status(log926) != "ok":
            auction_status, auction_reasons = (
                INCOMPLETE_STOCK,
                ["FRAME_0926_NOT_OK"],
            )
        elif not _code_source_known(log926, code):
            auction_status, auction_reasons = (
                SOURCE_TIME_UNKNOWN,
                ["SOURCE_TIME_UNKNOWN"],
            )
        elif _frame_value(row.samples, RESULT_FRAME, SAMPLE_AMOUNT) is None:
            auction_status, auction_reasons = (
                INCOMPLETE_STOCK,
                ["AUCTION_AMOUNT_MISSING"],
            )
        else:
            auction_status, auction_reasons = STOCK_VALID, []

    # ---------- B. 开盘承接 ----------
    post_price = row.post_open_price if row is not None else None
    if post_price is None:
        if log931 is None and post_open_pending:
            post_status, post_reasons = POST_OPEN_PENDING, ["POST_OPEN_PENDING"]
        elif log931 is None:
            post_status, post_reasons = FRAME_MISSING, ["FRAME_LOG_MISSING_0931"]
        else:
            post_status, post_reasons = NO_QUOTE, ["NO_POST_OPEN_QUOTE"]
    elif log931 is None:
        post_status, post_reasons = (
            SOURCE_TIME_UNKNOWN,
            ["FRAME_LOG_MISSING_0931", "SOURCE_TIME_UNKNOWN"],
        )
    elif _log_status(log931) == "partial":
        post_status, post_reasons = INCOMPLETE_STOCK, ["FRAME_0931_PARTIAL"]
    elif _log_status(log931) != "ok":
        post_status, post_reasons = INCOMPLETE_STOCK, ["FRAME_0931_NOT_OK"]
    elif not _code_source_known(log931, code):
        post_status, post_reasons = SOURCE_TIME_UNKNOWN, ["SOURCE_TIME_UNKNOWN"]
    else:
        post_status, post_reasons = STOCK_VALID, []

    # ---------- C. 合成整体状态 ----------
    if auction_status != STOCK_VALID:
        overall = auction_status
    elif post_status == STOCK_VALID:
        overall = STOCK_VALID
    elif post_status == POST_OPEN_PENDING:
        overall = POST_OPEN_PENDING
    else:
        overall = INCOMPLETE_STOCK
    return auction_status, post_status, overall, _dedupe(auction_reasons + post_reasons)


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
    session: Session, codes: list[str], prev: date | None
) -> dict[str, str]:
    """只取**上一交易日及更早**的题材快照。

    P2-01：观察日当天的 `stock_concept` 可能是盘后生成的，不能当成 09:25 前已知。
    因此这里只用 `prev`，缺失就返回空，不用当天标签或行业替代。
    """
    if not codes or prev is None:
        return {}
    return {
        code: concept
        for code, concept in session.execute(
            select(StockConcept.code, StockConcept.concept)
            .where(
                StockConcept.trade_date == prev,
                StockConcept.code.in_(codes),
            )
            .order_by(StockConcept.code, StockConcept.concept)
        ).all()
    }


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


AUCTION_HIDDEN_STATUSES = {NO_QUOTE, FRAME_MISSING, STALE_QUOTE, SOURCE_TIME_UNKNOWN}


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
    concept_as_of: str | None,
    prev_status: str | None,
    prev_board: int | None,
    previous_amount: float | None,
    post_open_pending: bool = False,
) -> dict[str, Any]:
    auction_status, post_status, overall_status, reasons = assess_stock_quality(
        snapshot, logs, code, post_open_pending=post_open_pending
    )
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

    # 竞价终值可信才展示；INCOMPLETE（金额缺失 / 帧 partial）仍保留价格，但带质量提示
    auction_price = None if auction_status in AUCTION_HIDDEN_STATUSES else price
    auction_amount = (
        None if auction_status in AUCTION_HIDDEN_STATUSES else amount
    )
    auction_pct = _auction_pct(auction_price, pre_close)
    if auction_price is not None and auction_pct is None:
        reasons = _dedupe(reasons + ["PRE_CLOSE_MISSING"])

    post_open_price = snapshot.post_open_price if snapshot is not None else None
    # 只有承接质量 VALID 才给「开盘后涨幅」；SOURCE_TIME_UNKNOWN / PENDING / 缺失都为 null
    post_open_pct = (
        _post_open_pct(post_open_price, pre_close)
        if post_status == STOCK_VALID
        else None
    )
    if previous_amount is None or previous_amount <= 0:
        if auction_amount is not None:
            reasons = _dedupe(reasons + ["YESTERDAY_AMOUNT_MISSING"])
        amount_ratio = None
    else:
        amount_ratio = (
            round(auction_amount / previous_amount * 100, 2)
            if auction_amount is not None
            else None
        )

    # 两个时点的价格都必须各自 VALID，才给「9:25→9:31 涨幅变化」
    pct_change_after_auction = (
        round(post_open_pct - auction_pct, 2)
        if (
            auction_status == STOCK_VALID
            and post_status == STOCK_VALID
            and post_open_pct is not None
            and auction_pct is not None
        )
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
        "concept_as_of": concept_as_of,
        "prev_limit_status": prev_status,
        "prev_board_height": prev_board,
        "auction_price": auction_price,
        "auction_pct": auction_pct,
        "auction_amount": auction_amount,
        "yesterday_amount": previous_amount,
        "auction_amount_ratio": amount_ratio,
        "post_open_price": post_open_price,
        "post_open_pct": post_open_pct,
        "pct_change_after_auction": pct_change_after_auction,
        "auction_frame": RESULT_FRAME if auction_price is not None else None,
        "post_open_frame": POST_OPEN_FRAME if post_open_price is not None else None,
        "auction_quality_status": auction_status,
        "post_open_quality_status": post_status,
        "data_quality_status": overall_status,
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
    post_open_pending: bool = False,
) -> list[dict[str, Any]]:
    if codes is None:
        codes = sorted(snapshots)
    meta = _stock_meta(session, codes)
    concepts = _concept_map(session, codes, prev)
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
        concept = concepts.get(code)
        rows.append(
            _row_payload(
                trade_date=trade_date,
                prev_date=prev,
                code=code,
                snapshot=snapshots.get(code),
                logs=logs,
                name=name,
                industry=industry,
                concept=concept,
                concept_as_of=prev.isoformat() if concept and prev else None,
                prev_status=prev_status,
                prev_board=prev_board,
                previous_amount=amounts.get(code),
                post_open_pending=post_open_pending,
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
    now = now_cn()
    post_open_pending = (
        day == now.date()
        and now.time() < time(9, 31)
        and is_trade_day(day, session)
    )

    all_snapshots = _snapshot_map(session, day, None)
    logs = _frame_logs(session, day)
    day_quality = assess_day_quality(
        list(all_snapshots.values()), logs, post_open_pending=post_open_pending
    )

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
            post_open_pending=post_open_pending,
        )
        rows_by_group[name] = rows
        auction_valid = sum(
            1 for row in rows if row["auction_quality_status"] == STOCK_VALID
        )
        post_valid = sum(
            1 for row in rows if row["post_open_quality_status"] == STOCK_VALID
        )
        fully_valid = sum(
            1 for row in rows if row["data_quality_status"] == STOCK_VALID
        )
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
                "valid": fully_valid,
                "auction_valid_count": auction_valid,
                "post_open_valid_count": post_valid,
                "fully_valid_count": fully_valid,
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
