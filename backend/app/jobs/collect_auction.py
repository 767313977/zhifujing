"""集合竞价采样：交易日 9:15–9:26 每分钟抓一次全市场快照 → `auction_snapshot`。

数据源是**腾讯批量行情**（`sources/tencent.fetch_spot`），零 iFinD 配额。

两个采样窗口，作用不同：
- **9:15–9:26**（`RESULT_*`）：竞价**结果** —— 竞价价 / 昨收 / 竞价涨幅 / 竞价额 /
  竞价量 / 换手 / 市值 / 涨跌停价，同时逐分钟追加 `samples`（竞价走势）。停在 9:26
  是为了拿竞价终值、又不把开盘后的连续竞价掺进来。
- **9:31**（`RATIO_SAMPLE`）：**只补 `volume_ratio`（量比）与开盘后现价**。腾讯
  **开盘前不填量比**（实测 9:26 前恒为 0，开盘后才有值），所以单独再采一帧；这一帧
  **不改**竞价价格/金额，也不往 `samples` 里追加。

## 时间边界（P0-A-01，2026-11-11）

`_frame_plan()` 只承认**计划分钟本身**，以及调度器 `misfire_grace_time=90` 造成的
**最多 90 秒迟到**：

- 09:14 → 不采样；
- 09:15–09:26 → 对应分钟；
- 09:27:00–09:27:30 → 迟到候选 0926；但只有源报价时间能证明仍是 0926 区间才写，
  否则整帧跳过（绝不把 09:27 的连续交易前快照记成 0926 终值）；
- 09:28–09:30、10:00、11:30、15:00、夜间 → 不采样；
- 09:31 → 0931；
- 09:32:00–09:32:30 → 迟到候选 0931，同样必须由源报价时间证明。

`actual_started_at`、`scheduled_frame`、`source_quote_time` 三者分开记录。计划帧号
本身**不构成**「已拿到该时刻行情」的证据。

## 帧级质量日志（P0-A-02）

每个实际执行的计划帧写一条 `collect_log`，任务名固定 `auction_frame`，`message`
是稳定 JSON（字段见 `_log_payload`）。它**不进入** `has_collected` / 收盘补采 /
额度统计 / C0 版本：这些路径都只认各自的任务名，`auction_frame` 不在其中。

⚠️ 与收盘采集**解耦**：不进 `collect_guard`，不启动补采（竞价没有历史可补）。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select

from app.db import session_scope, upsert_many
from app.models import AuctionSnapshot, CollectLog, StockBasic
from app.services.trading_days import is_trade_day
from app.sources.tencent import fetch_spot
from app.timeutil import CST, now_cn

logger = logging.getLogger(__name__)

# 「竞价结果」列的采样窗口（HHMM）。9:15 集合竞价开始、9:25 出开盘价，9:26 收最后一帧。
RESULT_FIRST = "0915"
RESULT_LAST = "0926"

# 只补量比的那一帧（见模块说明）。腾讯开盘前不填量比，9:26 前取到恒为 0。
RATIO_SAMPLE = "0931"

#: 采样窗口内**计划**采的那几刻（HHMM，升序）。见 `_frame_plan`。
PLANNED_MINUTES = [f"09{minute:02d}" for minute in range(15, 27)] + [RATIO_SAMPLE]

#: 调度器 `misfire_grace_time=90` 允许的最大迟到秒数。
LATE_GRACE_SECONDS = 90

FRAME_TASK = "auction_frame"
FRAME_KIND_RESULT = "result"
FRAME_KIND_POST_OPEN = "post_open"

#: 丢弃代码列表在日志里最多保留这么多只，避免 JSON 膨胀；计数始终是全量。
_DROPPED_CODE_LIMIT = 200


@dataclass(frozen=True)
class FramePlan:
    """一次实际执行对应哪个**计划帧**，以及是否属于迟到执行。"""

    frame: str
    kind: str
    late: bool
    actual_started_at: datetime


@dataclass
class QuoteFilterResult:
    """源报价按时间窗过滤后的统计。"""

    quotes: list[dict] = field(default_factory=list)
    dropped_old: int = 0
    dropped_stale: int = 0
    dropped_newer: int = 0
    dropped_unknown: int = 0
    source_time_unknown: int = 0
    source_times: list[str] = field(default_factory=list)
    dropped_old_codes: list[str] = field(default_factory=list)
    dropped_stale_codes: list[str] = field(default_factory=list)
    dropped_newer_codes: list[str] = field(default_factory=list)
    dropped_unknown_codes: list[str] = field(default_factory=list)
    source_time_unknown_codes: list[str] = field(default_factory=list)


def _as_cst(moment: datetime) -> datetime:
    """注入的时刻统一按 Asia/Shanghai 解释；带其它时区的先转换。"""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=CST)
    return moment.astimezone(CST)


def _frame_minutes(frame: str) -> int:
    return int(frame[:2]) * 60 + int(frame[2:])


def _frame_kind(frame: str) -> str:
    return FRAME_KIND_POST_OPEN if frame == RATIO_SAMPLE else FRAME_KIND_RESULT


def _frame_plan(moment: datetime) -> FramePlan | None:
    """实际时刻 → 计划帧。

    先匹配**整分钟**，再匹配「最多迟到 90 秒」的上一计划帧。这样 09:16:30 不会被
    错配成 0915 的迟到帧，09:27:31 之后也不会再回落到 0926。
    """
    local = _as_cst(moment)
    total_minutes = local.hour * 60 + local.minute
    seconds = local.second + local.microsecond / 1_000_000

    for frame in PLANNED_MINUTES:
        if total_minutes == _frame_minutes(frame):
            return FramePlan(frame, _frame_kind(frame), False, local)

    late_candidates: list[tuple[int, str]] = []
    for frame in PLANNED_MINUTES:
        delta = (total_minutes - _frame_minutes(frame)) * 60 + seconds
        if 0 < delta <= LATE_GRACE_SECONDS:
            late_candidates.append((_frame_minutes(frame), frame))
    if not late_candidates:
        return None
    frame = max(late_candidates)[1]
    return FramePlan(frame, _frame_kind(frame), True, local)


def _frame_hhmm(moment: datetime) -> str | None:
    """这一趟**计划**采哪一分钟；窗口外返回 None（保留给旧调用方/测试）。"""
    plan = _frame_plan(moment)
    return plan.frame if plan else None


def _quote_time_parts(raw: object) -> tuple[date | None, int | None]:
    """解析腾讯 `quote_time` → (行情日期, 当日分钟数)。

    腾讯一般给 `YYYYMMDDHHMMSS`；也兼容 `YYYYMMDDHHMM` 与只有日期的 `YYYYMMDD`。
    只有日期、没有时间的行返回 `(day, None)` —— 能证明日期，不能证明分钟。
    """
    if not raw:
        return None, None
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    try:
        if len(digits) >= 14:
            parsed = datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
            return parsed.date(), parsed.hour * 60 + parsed.minute
        if len(digits) == 12:
            parsed = datetime.strptime(digits, "%Y%m%d%H%M")
            return parsed.date(), parsed.hour * 60 + parsed.minute
        if len(digits) == 8:
            parsed = datetime.strptime(digits, "%Y%m%d")
            return parsed.date(), None
    except ValueError:
        return None, None
    return None, None


def _is_today_quote(item: dict, day: date) -> bool:
    """这一行的**行情日期**是不是 `day`（时间未知时放行，由帧级规则再判）。"""
    quote_day, _ = _quote_time_parts(item.get("quote_time"))
    return quote_day is None or quote_day == day


def _positive(value: float | None) -> float | None:
    """价格类的清洗：**≤0 一律当缺值**。"""
    if value is None or value <= 0:
        return None
    return value


def now_cst() -> datetime:
    """当前北京时间 —— 统一走 `timeutil.now_cn()`（固定 +8，不看容器时区）。"""
    return now_cn()


def _universe() -> list[str]:
    """全市场代码（`stock_basic` 覆盖全 A，约 5500 只）。"""
    with session_scope() as session:
        return [code for code in session.scalars(select(StockBasic.code)) if code]


def _filter_quotes(
    quotes: list[dict], day: date, plan: FramePlan
) -> QuoteFilterResult:
    """按「行情日期 + 行情分钟」过滤源报价。

    结果帧允许源报价比计划帧早 1 分钟（09:26 那帧经常拿到 09:25:59 的竞价终值），
    但不允许**晚于**计划帧 —— 那正是 09:27 污染 0926 的路径。
    """
    result = QuoteFilterResult()
    target = _frame_minutes(plan.frame)
    for item in quotes:
        code = item.get("code") or ""
        quote_day, quote_minutes = _quote_time_parts(item.get("quote_time"))
        raw_time = str(item.get("quote_time") or "").strip()
        if raw_time:
            result.source_times.append(raw_time)

        if quote_day is not None and quote_day != day:
            result.dropped_old += 1
            result.dropped_old_codes.append(code)
            continue

        if quote_minutes is None:
            if plan.late:
                # 迟到帧必须能证明源报价仍处于正确分钟；缺时间就整帧不写
                result.dropped_unknown += 1
                result.dropped_unknown_codes.append(code)
            else:
                result.source_time_unknown += 1
                result.source_time_unknown_codes.append(code)
                result.quotes.append(item)
            continue

        if plan.kind == FRAME_KIND_RESULT:
            if quote_minutes > target:
                result.dropped_newer += 1
                result.dropped_newer_codes.append(code)
                continue
            if quote_minutes < target - 1:
                result.dropped_stale += 1
                result.dropped_stale_codes.append(code)
                continue
        else:  # 0931：只接受 0931 这一分钟，早一分钟或晚一分钟都不算
            if quote_minutes > target:
                result.dropped_newer += 1
                result.dropped_newer_codes.append(code)
                continue
            if quote_minutes < target:
                result.dropped_stale += 1
                result.dropped_stale_codes.append(code)
                continue

        result.quotes.append(item)
    return result


def _late_frame_proven(result: QuoteFilterResult) -> bool:
    """迟到帧是否完整可证明：任何旧日期 / 早到 / 晚到 / 缺时间都判不成立。"""
    return not (
        result.dropped_old
        or result.dropped_stale
        or result.dropped_newer
        or result.dropped_unknown
    )


def _limited(codes: list[str]) -> list[str]:
    return codes[:_DROPPED_CODE_LIMIT]


def _log_payload(
    *,
    day: date,
    plan: FramePlan | None,
    status: str,
    reason: str | None,
    started_at: datetime,
    elapsed_ms: int,
    requested: int,
    returned: int,
    valid: int,
    written: int,
    stats: dict,
    filtered: QuoteFilterResult | None,
    extra: dict | None = None,
) -> dict:
    filtered = filtered or QuoteFilterResult()
    times = sorted(filtered.source_times)
    payload = {
        "trade_date": day.isoformat(),
        "scheduled_frame": plan.frame if plan else None,
        "frame_kind": plan.kind if plan else None,
        "late": bool(plan.late) if plan else False,
        "actual_started_at": started_at.isoformat(),
        "source_quote_time": times[-1] if times else None,
        "source_quote_time_min": times[0] if times else None,
        "requested_codes": requested,
        "returned_codes": returned,
        "valid_codes": valid,
        "dropped_old_quote": filtered.dropped_old,
        "dropped_stale_quote": filtered.dropped_stale,
        "dropped_newer_quote": filtered.dropped_newer,
        "dropped_unknown_quote": filtered.dropped_unknown,
        "source_time_unknown": filtered.source_time_unknown,
        "failed_batches": int(stats.get("failed", 0)),
        "total_batches": int(stats.get("requests", 0)),
        "written_codes": written,
        "status": status,
        "reason": reason,
        "elapsed_ms": elapsed_ms,
        "dropped_old_codes": _limited(filtered.dropped_old_codes),
        "dropped_stale_codes": _limited(filtered.dropped_stale_codes),
        "dropped_newer_codes": _limited(filtered.dropped_newer_codes),
        "dropped_unknown_codes": _limited(filtered.dropped_unknown_codes),
        "source_time_unknown_codes": _limited(filtered.source_time_unknown_codes),
    }
    if extra:
        payload.update(extra)
    return payload


def _log_row(
    *,
    day: date,
    status: str,
    written: int,
    elapsed_ms: int,
    payload: dict,
) -> CollectLog:
    return CollectLog(
        trade_date=day,
        task=FRAME_TASK,
        status=status,
        rows=written,
        message=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        cost_seconds=elapsed_ms / 1000,
    )


def _safe_log(**kwargs) -> None:
    """失败路径的独立日志事务；日志写不进去只能记 logger，不能反过来算成功。"""
    try:
        with session_scope() as session:
            session.add(_log_row(**kwargs))
    except Exception:  # noqa: BLE001 - 日志失败不能吞掉原始失败结论
        logger.exception("竞价帧日志写入失败（%s）", kwargs.get("payload", {}).get("scheduled_frame"))


def _finish(
    *,
    day: date,
    plan: FramePlan,
    started_at: datetime,
    started_monotonic: float,
    status: str,
    reason: str | None,
    requested: int,
    returned: int,
    valid: int,
    written: int,
    stats: dict,
    filtered: QuoteFilterResult | None = None,
    extra: dict | None = None,
) -> int:
    """统一记录帧日志并返回写入只数（0 表示该帧未写入行情）。"""
    elapsed_ms = int((time.monotonic() - started_monotonic) * 1000)
    payload = _log_payload(
        day=day,
        plan=plan,
        status=status,
        reason=reason,
        started_at=started_at,
        elapsed_ms=elapsed_ms,
        requested=requested,
        returned=returned,
        valid=valid,
        written=written,
        stats=stats,
        filtered=filtered,
        extra=extra,
    )
    _safe_log(day=day, status=status, written=written, elapsed_ms=elapsed_ms, payload=payload)
    return written if status in ("ok", "partial") else 0


def collect_auction(now: datetime | None = None) -> int:
    """跑一次采样，返回写入的股票数（0 表示跳过 / 失败 / 无数据）。

    `now` 可注入（测试用）；缺省取当前北京时间。所有判断都按 Asia/Shanghai。
    """
    started_monotonic = time.monotonic()
    started_at = _as_cst(now) if now is not None else now_cn()
    day = started_at.date()
    plan = _frame_plan(started_at)
    if plan is None:
        # 不在任何计划帧（10:00 / 11:30 / 15:00 / 夜间）：不写行情，也不写日志。
        return 0

    stats: dict = {}
    if not is_trade_day(day):
        return _finish(
            day=day,
            plan=plan,
            started_at=started_at,
            started_monotonic=started_monotonic,
            status="skipped",
            reason="NON_TRADE_DAY",
            requested=0,
            returned=0,
            valid=0,
            written=0,
            stats=stats,
        )

    codes = _universe()
    if not codes:
        logger.warning("竞价采样：stock_basic 为空，跳过（先跑一轮采集建池）")
        return _finish(
            day=day,
            plan=plan,
            started_at=started_at,
            started_monotonic=started_monotonic,
            status="skipped",
            reason="UNIVERSE_EMPTY",
            requested=0,
            returned=0,
            valid=0,
            written=0,
            stats=stats,
        )

    try:
        quotes = fetch_spot(codes, stats=stats)
    except Exception as exc:  # noqa: BLE001 - 单分钟的失败不该影响下一分钟
        logger.warning("竞价采样：抓取失败 %s %s", day, plan.frame, exc_info=True)
        return _finish(
            day=day,
            plan=plan,
            started_at=started_at,
            started_monotonic=started_monotonic,
            status="failed",
            reason="FETCH_EXCEPTION",
            requested=len(codes),
            returned=0,
            valid=0,
            written=0,
            stats=stats,
            extra={"error": f"{type(exc).__name__}: {exc}"[:300]},
        )
    if not quotes:
        logger.warning("竞价采样：%s %s 返回 0 行", day, plan.frame)
        return _finish(
            day=day,
            plan=plan,
            started_at=started_at,
            started_monotonic=started_monotonic,
            status="failed",
            reason="SOURCE_EMPTY",
            requested=len(codes),
            returned=0,
            valid=0,
            written=0,
            stats=stats,
        )

    filtered = _filter_quotes(quotes, day, plan)
    if plan.late and not _late_frame_proven(filtered):
        # 迟到帧无法证明源报价仍在计划分钟：整帧跳过，不写任何污染数据
        return _finish(
            day=day,
            plan=plan,
            started_at=started_at,
            started_monotonic=started_monotonic,
            status="skipped",
            reason="LATE_FRAME_UNPROVEN",
            requested=len(codes),
            returned=len(quotes),
            valid=0,
            written=0,
            stats=stats,
            filtered=filtered,
        )

    if not filtered.quotes:
        reason = (
            "STALE_OR_OLD_QUOTE"
            if (filtered.dropped_old or filtered.dropped_stale or filtered.dropped_newer)
            else "NO_VALID_QUOTE"
        )
        logger.warning(
            "竞价采样：%s %s 没有可用报价（旧 %d / 早 %d / 晚 %d / 缺时间 %d）",
            day,
            plan.frame,
            filtered.dropped_old,
            filtered.dropped_stale,
            filtered.dropped_newer,
            filtered.dropped_unknown,
        )
        return _finish(
            day=day,
            plan=plan,
            started_at=started_at,
            started_monotonic=started_monotonic,
            status="failed",
            reason=reason,
            requested=len(codes),
            returned=len(quotes),
            valid=0,
            written=0,
            stats=stats,
            filtered=filtered,
        )

    rows: list[dict] = []
    applicable = len(filtered.quotes)
    try:
        with session_scope() as session:
            if plan.kind == FRAME_KIND_POST_OPEN:
                # 9:31 只刷「已存在的行」的量比与开盘后现价；不插只有两列的脏行
                known = set(
                    session.scalars(
                        select(AuctionSnapshot.code).where(
                            AuctionSnapshot.trade_date == day
                        )
                    )
                )
                applicable = sum(1 for item in filtered.quotes if item["code"] in known)
                for item in filtered.quotes:
                    if item["code"] not in known:
                        continue
                    volume_ratio = item["volume_ratio"]
                    if volume_ratio is not None and volume_ratio <= 0:
                        volume_ratio = None
                    post_open_price = _positive(item["price"])
                    if volume_ratio is None and post_open_price is None:
                        continue
                    rows.append(
                        {
                            "trade_date": day,
                            "code": item["code"],
                            "volume_ratio": volume_ratio,
                            "post_open_price": post_open_price,
                        }
                    )
            else:
                existing = {
                    code: samples
                    for code, samples in session.execute(
                        select(AuctionSnapshot.code, AuctionSnapshot.samples).where(
                            AuctionSnapshot.trade_date == day
                        )
                    ).all()
                }
                for item in filtered.quotes:
                    code = item["code"]
                    price = _positive(item["price"])
                    pre_close = item["pre_close"]
                    samples = list(existing.get(code) or [])
                    samples = [frame for frame in samples if frame[0] != plan.frame]
                    samples.append(
                        [
                            plan.frame,
                            price,
                            item["amount"],
                            item["committee_diff"],
                            item["bid1_price"],
                            item["bid1_volume"],
                            item["ask1_price"],
                            item["ask1_volume"],
                        ]
                    )
                    rows.append(
                        {
                            "trade_date": day,
                            "code": code,
                            "name": item["name"],
                            "price": price,
                            "pre_close": pre_close,
                            "pct_chg": (
                                round((price / pre_close - 1) * 100, 2)
                                if price is not None and pre_close
                                else None
                            ),
                            "open": _positive(item["open"]),
                            "amount": item["amount"],
                            "volume": item["volume"],
                            "turnover": item["turnover"],
                            "volume_ratio": None,
                            "float_mv": item["float_mv"],
                            "total_mv": item["total_mv"],
                            "limit_up": _positive(item["limit_up"]),
                            "limit_down": _positive(item["limit_down"]),
                            "samples": samples,
                        }
                    )

            if not rows:
                # 事务里没写任何行情：不要顺带提交一条成功日志
                raise _EmptyFrame("NO_EXISTING_AUCTION_ROWS")

            partial = bool(
                stats.get("failed")
                or filtered.dropped_old
                or filtered.dropped_stale
                or filtered.dropped_newer
                or filtered.dropped_unknown
                or filtered.source_time_unknown
                or len(rows) != applicable
            )
            status = "partial" if partial else "ok"
            reason = None
            if stats.get("failed"):
                reason = "BATCH_FAILED"
            elif filtered.dropped_old or filtered.dropped_stale or filtered.dropped_newer:
                reason = "STALE_OR_OLD_QUOTE"
            elif filtered.dropped_unknown:
                reason = "LATE_FRAME_UNPROVEN"
            elif filtered.source_time_unknown:
                reason = "SOURCE_TIME_UNKNOWN"
            elif len(rows) != applicable:
                reason = "PARTIAL_SOURCE"

            elapsed_ms = int((time.monotonic() - started_monotonic) * 1000)
            payload = _log_payload(
                day=day,
                plan=plan,
                status=status,
                reason=reason,
                started_at=started_at,
                elapsed_ms=elapsed_ms,
                requested=len(known) if plan.kind == FRAME_KIND_POST_OPEN else len(codes),
                returned=len(quotes),
                valid=applicable,
                written=len(rows),
                stats=stats,
                filtered=filtered,
            )
            upsert_many(session, AuctionSnapshot, rows)
            # 与行情同一个事务：日志写不进去就整体回滚，避免「行情成功、质量证据缺失」
            session.add(
                _log_row(
                    day=day,
                    status=status,
                    written=len(rows),
                    elapsed_ms=elapsed_ms,
                    payload=payload,
                )
            )
    except _EmptyFrame as exc:
        return _finish(
            day=day,
            plan=plan,
            started_at=started_at,
            started_monotonic=started_monotonic,
            status="failed",
            reason=str(exc),
            requested=len(codes),
            returned=len(quotes),
            valid=applicable,
            written=0,
            stats=stats,
            filtered=filtered,
        )
    except Exception as exc:  # noqa: BLE001 - 单帧写库失败只影响这一帧
        logger.warning("竞价采样：写库失败 %s %s", day, plan.frame, exc_info=True)
        return _finish(
            day=day,
            plan=plan,
            started_at=started_at,
            started_monotonic=started_monotonic,
            status="failed",
            reason="WRITE_EXCEPTION",
            requested=len(codes),
            returned=len(quotes),
            valid=applicable,
            written=0,
            stats=stats,
            filtered=filtered,
            extra={"error": f"{type(exc).__name__}: {exc}"[:300]},
        )

    elapsed = time.monotonic() - started_monotonic
    logger.info(
        "竞价采样 %s %s：%d 只 / 请求 %d 只（丢批 %d/%d 批），耗时 %.1f 秒%s",
        day,
        plan.frame,
        len(rows),
        len(codes),
        stats.get("failed", 0),
        stats.get("requests", 0),
        elapsed,
        "（迟到帧，已由源报价时间证明）" if plan.late else "",
    )
    if elapsed > 60:
        logger.warning(
            "竞价采样 %s %s 耗时 %.1f 秒（> 60 秒），下一分钟那一帧可能被跳过",
            day,
            plan.frame,
            elapsed,
        )
    return len(rows)


class _EmptyFrame(Exception):
    """事务内发现本帧没有可写行情。"""
