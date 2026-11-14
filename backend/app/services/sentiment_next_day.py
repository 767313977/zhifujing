"""市场情绪 × 次日表现（P1-C / S1-B）。

只做**历史描述性统计**：把 D 日收盘时已经能确定的股票集合，与 D+1 的真实日线
表现对齐。不改任何既有指标、不回写 C0、不做收益预测。

分层（严格按时间顺序，绝不从未来反向筛选）：

1. 加载 D 日情绪（`market_sentiment`）；
2. 加载 D 日收盘时已经确定的股票集合（涨停池 / 炸板池 / P0-B 的可信断板）；
3. 用**交易日历**确定 D+1（不是 `D + 1 天`）；
4. 聚合 D+1 的实际表现。

口径约定
--------
- **分组不互斥**：全部涨停包含首板 / 二板 / 三板+；炸板与断板可能重叠（同一只票
  当日炸板、又没晋级）。页面上必须写清这是分组统计。
- **涨停池 / 炸板池必须可信**：只认当日 `collect_log` 为 `ok` 且行数合理的快照，
  与 `services/limit_funnel` 同一套来源质量判定。
- **断板复用 P0-B**：`build_promotion_funnel` 里逐券 `FAILED` 的股票才算断板；
  没有涨停证据的股票**不是**断板股。
- **缺失不补 0**：D+1 行情没到齐时 `avg_pct` / `up_ratio` 一律 None，整行标
  `PENDING`；缺失的个股计入 `missing_count`，不是「涨 0%」。
- **观察 ≠ 可交易**：D+1 日线涨跌幅不代表能在集合竞价买到。涨停买不到、停牌、
  一字板无法成交都不在统计里扣除 —— 响应里的 `notes` 会明确提示。
- 复权口径：`stock_daily` 是不复权价 + 真实涨跌幅（见 `models.StockDaily`），
  所以「开盘涨幅」只用同源的 D 收盘与 D+1 开盘，绝不混用前复权收盘。

性能：股票池、采集日志、情绪行都按窗口**批量**查询；日线按「一天一个 IN 查询」
取（不是逐券 N+1）。
"""

from __future__ import annotations

import logging
import math
from datetime import date
from statistics import median
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    CollectLog,
    LimitPool,
    MarketSentiment,
    StockDaily,
    TradeCalendar,
)
from app.services.limit_funnel import (
    FAILED,
    PARTIAL as FUNNEL_PARTIAL,
    VERIFIED as FUNNEL_VERIFIED,
    FunnelInputs,
    build_promotion_funnel,
    load_funnel_inputs,
    load_funnel_window_inputs,
)
from app.timeutil import today_cn

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 分组

GROUP_ALL_LIMIT = "all_limit_up"
GROUP_FIRST = "first_board"
GROUP_SECOND = "second_board"
GROUP_THIRD_PLUS = "third_plus"
GROUP_BROKEN = "broken"
GROUP_FAILED = "failed_promotion"

GROUPS = (
    GROUP_ALL_LIMIT,
    GROUP_FIRST,
    GROUP_SECOND,
    GROUP_THIRD_PLUS,
    GROUP_BROKEN,
    GROUP_FAILED,
)

GROUP_LABELS = {
    GROUP_ALL_LIMIT: "D 日全部涨停",
    GROUP_FIRST: "D 日首板",
    GROUP_SECOND: "D 日二板",
    GROUP_THIRD_PLUS: "D 日三板及以上",
    GROUP_BROKEN: "D 日炸板",
    GROUP_FAILED: "D 日可信断板",
}

#: 分组之间**有包含/重叠**关系，不是互斥切分。
GROUP_NOTE = "分组为分别统计：全部涨停包含首板/二板/三板+；炸板与断板可能重叠，不是互斥口径。"

# ------------------------------------------------------------------ 质量状态

STATUS_VERIFIED = "VERIFIED"
STATUS_PARTIAL = "PARTIAL"
STATUS_PENDING = "PENDING"
STATUS_UNVERIFIED = "UNVERIFIED"
STATUS_EMPTY = "EMPTY"

QUALITY_REASON_TEXT = {
    "UP_POOL_MISSING": "D 日涨停池未采集",
    "UP_POOL_UNVERIFIED": "D 日涨停池采集状态不可信",
    "UP_POOL_EMPTY": "D 日涨停池日志成功但实际 0 行",
    "BROKEN_POOL_MISSING": "D 日炸板池未采集",
    "BROKEN_POOL_UNVERIFIED": "D 日炸板池采集状态不可信",
    "PREV_UP_POOL_MISSING": "D-1 涨停池未采集，无法判定断板",
    "PREV_UP_POOL_UNVERIFIED": "D-1 涨停池不可信，无法判定断板",
    "RELAY_UNVERIFIED": "P0-B 连板漏斗整体不可信，断板集合不可用",
    "NO_SAMPLES": "该分组当日没有样本",
    "OUTCOME_PENDING": "D+1 行情尚未到齐，待观察",
    "OUTCOME_PARTIAL": "D+1 有部分样本缺少行情",
    "OUTCOME_LIMIT_POOL_UNVERIFIED": "D+1 涨停池不可信，无法判断是否再次涨停",
    "METRIC_MISSING": "D 日情绪指标缺失，无法分档（不等于 0）",
}

# ------------------------------------------------------------------ 情绪分档
#
# **版本化固定阈值**：先定档位再看结果，绝不依据 D+1 表现反向优化。
# 改档位必须升版本号，历史对比才能说清用的是哪一套。

THRESHOLD_VERSION = "sentiment-bands-v1"

#: 多日聚合口径版本（S2 起）。v1 的 `avg_pct` 是「每日均值的等权平均」，
#: v2 改成逐券 pooled：每个**证券-交易日**算一个观测。旧口径保留在
#: `day_equal_weight_avg_pct`，两个字段同时返回，谁也不能冒充谁。
AGGREGATION_VERSION = "next-day-aggregation-v2"

METRICS: dict[str, tuple[tuple[float | None, float | None, str], ...]] = {
    "limit_up_count": (
        (None, 30, "<30 家"),
        (30, 60, "30–60 家"),
        (60, 100, "60–100 家"),
        (100, None, "≥100 家"),
    ),
    "broken_rate": (
        (None, 20, "<20%"),
        (20, 35, "20–35%"),
        (35, None, "≥35%"),
    ),
    "max_consecutive": (
        (None, 3, "≤2 板"),
        (3, 5, "3–4 板"),
        (5, None, "≥5 板"),
    ),
    "advance_ratio": (
        (None, 30, "<30%"),
        (30, 50, "30–50%"),
        (50, 70, "50–70%"),
        (70, None, "≥70%"),
    ),
}

METRIC_LABELS = {
    "limit_up_count": "涨停家数",
    "broken_rate": "炸板率",
    "max_consecutive": "最高连板",
    "advance_ratio": "上涨家数占比",
}

NOTES = [
    "统计为历史描述性关联，不构成收益预测或因果结论。",
    "D+1 日线涨跌幅是市场观察值，不代表真实可交易收益：涨停买不到、停牌、"
    "一字板无法成交等情况未从样本中扣除。",
    "D+1 行情未到齐的日期标记为 PENDING，不按 0% 计入均值。",
    "分组之间不互斥（全部涨停包含首板/二板/三板+；炸板与断板可能重叠）。",
    "断板集合复用 P0-B 的可信分类：只有昨日涨停且今日既未涨停也未炸板、"
    "且有当日日线的股票才算断板。",
    "总体均值/中位数为逐券 pooled 口径：每个证券-交易日算一个观测，"
    "样本多的交易日权重自然更大；`day_equal_weight_avg_pct` 是按交易日等权的"
    "旧口径，两者都返回、不混用。",
    "再次涨停只统计「有 D+1 日线且落在 D+1 涨停池」的样本，分母为有效样本数；"
    "缺日线的样本计入缺失，不进入分子，也不按未涨停处理。",
]


def _pool_state(
    latest_log: CollectLog | None, rows: int, *, allow_empty: bool = False
) -> tuple[bool, str | None]:
    """池数据是否可信。与 `limit_funnel._source_quality` 同一口径的轻量版本。"""
    if latest_log is None:
        return False, "MISSING"
    status = (latest_log.status or "").lower()
    if status != "ok":
        return False, "UNVERIFIED"
    if rows == 0 and not allow_empty:
        return False, "EMPTY"
    return True, None


def _latest_logs(session: Session, days: list[date]) -> dict[tuple[date, str], CollectLog]:
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


def _band(value: float | None, metric: str) -> tuple[str, float | None, float | None] | None:
    if value is None:
        return None
    for lower, upper, label in METRICS[metric]:
        if lower is None and upper is None:
            continue
        if lower is None and value < upper:  # type: ignore[operator]
            return label, lower, upper
        if upper is None and value >= lower:  # type: ignore[operator]
            return label, lower, upper
        if lower is not None and upper is not None and lower <= value < upper:
            return label, lower, upper
    return None


def _metric_value(sentiment: MarketSentiment | None, metric: str) -> float | None:
    if sentiment is None:
        return None
    if metric == "limit_up_count":
        return float(sentiment.limit_up_count) if sentiment.limit_up_count is not None else None
    if metric == "broken_rate":
        return sentiment.broken_rate
    if metric == "max_consecutive":
        return float(sentiment.max_consecutive) if sentiment.max_consecutive is not None else None
    if metric == "advance_ratio":
        up, down = sentiment.up_count, sentiment.down_count
        if up is None or down is None or up + down <= 0:
            return None
        return round(up / (up + down) * 100, 2)
    raise ValueError(f"未知情绪指标：{metric}")


def _finite(value: Any) -> float | None:
    """把库里的数值转成有限 float；NaN / Inf / 非数字一律当缺失。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _stats(
    samples: list[str],
    quotes: dict[str, dict],
    *,
    limit_up_codes: set[str] | None,
) -> tuple[dict[str, Any], dict[str, list[float]]]:
    """一组股票在 D+1 的表现。

    `limit_up_codes=None` 表示 D+1 涨停池不可信 —— 再次涨停相关字段全部 null，
    计入 `unverifiable_count`，绝不把「没证据」当成「没涨停」。

    分子分母守恒：再次涨停只在 **有 D+1 日线** 的样本里数（`valid_codes`），
    分母也是 `valid`；缺行情的股票只进 `missing_count`，不会让比例超过 100%。

    第二个返回值是逐券明细（未四舍五入），供跨日 pooled 聚合使用。
    """
    valid_codes: list[str] = []
    pcts: list[float] = []
    opens: list[float] = []
    close_vs_open: list[float] = []
    missing = 0
    for code in samples:
        quote = quotes.get(code)
        pct = _finite(quote.get("pct_chg")) if quote else None
        if pct is None:
            missing += 1
            continue
        valid_codes.append(code)
        pcts.append(pct)
        open_ = _finite(quote.get("open"))
        prev_close = _finite(quote.get("prev_close"))
        close = _finite(quote.get("close"))
        if open_ is not None and prev_close is not None and prev_close > 0:
            opens.append((open_ / prev_close - 1) * 100)
        if open_ is not None and close is not None and open_ > 0:
            close_vs_open.append((close / open_ - 1) * 100)

    valid = len(pcts)
    up = sum(1 for value in pcts if value > 0)
    down = sum(1 for value in pcts if value < 0)
    flat = sum(1 for value in pcts if value == 0)
    again = None
    again_ratio = None
    not_again = None
    unverifiable = 0
    if limit_up_codes is not None:
        again = sum(1 for code in valid_codes if code in limit_up_codes)
        again_ratio = round(again / valid * 100, 2) if valid else None
        not_again = valid - again
    else:
        unverifiable = valid

    stats = {
        "sample_count": len(samples),
        "valid_count": valid,
        "missing_count": missing,
        "avg_pct": round(sum(pcts) / valid, 2) if valid else None,
        "median_pct": round(median(pcts), 2) if valid else None,
        "outcome_up_count": up,
        "outcome_down_count": down,
        "outcome_flat_count": flat,
        "up_ratio": round(up / valid * 100, 2) if valid else None,
        "limit_up_again_count": again,
        "limit_up_again_ratio": again_ratio,
        "not_limit_up_count": not_again,
        "unverifiable_count": unverifiable,
        "avg_open_pct": round(sum(opens) / len(opens), 2) if opens else None,
        "avg_close_vs_open": (
            round(sum(close_vs_open) / len(close_vs_open), 2) if close_vs_open else None
        ),
    }
    valid_set = set(valid_codes)
    values = {
        "pct": pcts,
        "open_pct": opens,
        "close_vs_open": close_vs_open,
        # 审计诊断：只在需要时由调用方暴露，不改变任何统计口径
        "valid_codes": valid_codes,
        "missing_codes": [code for code in samples if code not in valid_set],
    }
    return stats, values


def _day_samples(
    session: Session,
    *,
    day: date,
    prev: date | None,
    group: str,
    up_by_day: dict[date, dict[str, int | None]],
    broken_by_day: dict[date, set[str]],
    pool_ok: dict[tuple[date, str], tuple[bool, str | None]],
    funnel_inputs: FunnelInputs | None = None,
) -> tuple[list[str], list[str]]:
    """D 日收盘时已经确定的样本代码 + 质量原因。"""
    reasons: list[str] = []
    up_ok, up_state = pool_ok.get((day, "pool_up"), (False, "MISSING"))
    broken_ok, broken_state = pool_ok.get((day, "pool_broken"), (False, "MISSING"))

    if group in (GROUP_ALL_LIMIT, GROUP_FIRST, GROUP_SECOND, GROUP_THIRD_PLUS):
        if not up_ok:
            reasons.append(
                "UP_POOL_MISSING"
                if up_state == "MISSING"
                else "UP_POOL_EMPTY"
                if up_state == "EMPTY"
                else "UP_POOL_UNVERIFIED"
            )
            return [], reasons
        codes = list(up_by_day.get(day, {}))
        if group == GROUP_FIRST:
            codes = [code for code in codes if (up_by_day[day][code] or 1) <= 1]
        elif group == GROUP_SECOND:
            codes = [code for code in codes if (up_by_day[day][code] or 1) == 2]
        elif group == GROUP_THIRD_PLUS:
            codes = [code for code in codes if (up_by_day[day][code] or 1) >= 3]
        return sorted(codes), reasons

    if group == GROUP_BROKEN:
        if not broken_ok:
            reasons.append(
                "BROKEN_POOL_MISSING" if broken_state == "MISSING" else "BROKEN_POOL_UNVERIFIED"
            )
            return [], reasons
        return sorted(broken_by_day.get(day, set())), reasons

    # group == GROUP_FAILED：复用 P0-B 的逐券 FAILED 分类
    if prev is None:
        return [], ["PREV_UP_POOL_MISSING"]
    inputs = (
        funnel_inputs
        if funnel_inputs is not None
        else load_funnel_inputs(session, prev, day)
    )
    if not inputs.prev_up_quality.trusted:
        reasons.append(
            "PREV_UP_POOL_MISSING"
            if inputs.prev_up_quality.status == "not_collected"
            else "PREV_UP_POOL_UNVERIFIED"
        )
        return [], reasons
    levels, quality = build_promotion_funnel(inputs, list(range(1, 11)))
    if quality.status not in (FUNNEL_VERIFIED, FUNNEL_PARTIAL):
        reasons.append("RELAY_UNVERIFIED")
        return [], reasons
    codes = [
        stock["code"]
        for level in levels
        for stock in level["stocks"]
        if stock["status"] == FAILED
    ]
    return sorted(set(codes)), reasons


def _calendar(session: Session) -> list[date]:
    return list(
        session.scalars(select(TradeCalendar.trade_date).order_by(TradeCalendar.trade_date))
    )


def build_next_day(
    session: Session,
    *,
    days: int = 20,
    group: str = GROUP_FIRST,
    metric: str = "limit_up_count",
    include_pending: bool = True,
    end_date: date | None = None,
    include_code_details: bool = False,
) -> dict[str, Any]:
    """构建情绪 × 次日表现的结果（只读本地库）。"""
    if group not in GROUPS:
        raise ValueError(f"未知分组：{group}")
    if metric not in METRICS:
        raise ValueError(f"未知情绪指标：{metric}")

    days = max(1, min(int(days), 250))
    calendar = _calendar(session)
    data_as_of = session.scalar(select(func.max(StockDaily.trade_date)))
    if end_date is None:
        end_date = today_cn()
    window = [day for day in calendar if day <= end_date][-days:]
    if not window:
        return _empty(group, metric, days, data_as_of)

    position = {day: index for index, day in enumerate(calendar)}

    def next_day(day: date) -> date | None:
        index = position.get(day)
        if index is None or index + 1 >= len(calendar):
            return None
        return calendar[index + 1]

    needed = set(window)
    for day in window:
        following = next_day(day)
        if following:
            needed.add(following)
        index = position.get(day)
        if index and index > 0:
            needed.add(calendar[index - 1])

    pool_rows = session.execute(
        select(
            LimitPool.trade_date,
            LimitPool.code,
            LimitPool.pool_type,
            LimitPool.consecutive,
        ).where(LimitPool.trade_date.in_(needed))
    ).all()
    up_by_day: dict[date, dict[str, int | None]] = {}
    broken_by_day: dict[date, set[str]] = {}
    for trade_day, code, pool_type, consecutive in pool_rows:
        if pool_type == "up":
            up_by_day.setdefault(trade_day, {})[code] = consecutive
        elif pool_type == "broken":
            broken_by_day.setdefault(trade_day, set()).add(code)

    logs = _latest_logs(session, sorted(needed))
    pool_ok: dict[tuple[date, str], tuple[bool, str | None]] = {}
    for trade_day in needed:
        up_rows = len(up_by_day.get(trade_day, {}))
        broken_rows = len(broken_by_day.get(trade_day, set()))
        pool_ok[(trade_day, "pool_up")] = _pool_state(
            logs.get((trade_day, "pool_up")), up_rows
        )
        pool_ok[(trade_day, "pool_broken")] = _pool_state(
            logs.get((trade_day, "pool_broken")), broken_rows, allow_empty=True
        )

    sentiments = {
        row.trade_date: row
        for row in session.scalars(
            select(MarketSentiment).where(MarketSentiment.trade_date.in_(window))
        )
    }

    series: list[dict[str, Any]] = []
    values_by_day: dict[date, dict[str, list[float]]] = {}
    pending_days: list[str] = []
    unverified_days: list[str] = []
    metric_missing_days: list[str] = []
    # 断板分组：一次批量加载整个窗口，避免每天再打 8 条 SQL（S3-B）
    funnel_window = (
        load_funnel_window_inputs(session, list(window), calendar)
        if group == GROUP_FAILED
        else {}
    )
    for day in window:
        following = next_day(day)
        prev_index = position.get(day, 0) - 1
        prev = calendar[prev_index] if prev_index >= 0 else None
        codes, reasons = _day_samples(
            session,
            day=day,
            prev=prev,
            group=group,
            up_by_day=up_by_day,
            broken_by_day=broken_by_day,
            pool_ok=pool_ok,
            funnel_inputs=funnel_window.get(day),
        )
        sentiment = sentiments.get(day)
        metric_value = _metric_value(sentiment, metric)
        if metric_value is None:
            # 指标缺失不等于 0 档：保留逐券统计，但明确标出不可分档
            reasons.append("METRIC_MISSING")
            metric_missing_days.append(day.isoformat())

        quotes: dict[str, dict] = {}
        outcome_limit_up: set[str] | None = None
        outcome_ready = False
        if following is None:
            reasons.append("OUTCOME_PENDING")
        elif data_as_of is None or following > data_as_of:
            reasons.append("OUTCOME_PENDING")
        else:
            outcome_ready = True

        if outcome_ready and codes:
            # 一天一个 IN 查询：D 日收盘用于开盘涨幅，D+1 用于结果
            daily_rows = session.execute(
                select(
                    StockDaily.code,
                    StockDaily.trade_date,
                    StockDaily.open,
                    StockDaily.close,
                    StockDaily.pct_chg,
                ).where(
                    StockDaily.trade_date.in_([day, following]),
                    StockDaily.code.in_(codes),
                )
            ).all()
            closes: dict[str, float] = {}
            for code, trade_day, _open, close, _pct in daily_rows:
                if trade_day == day and close is not None:
                    closes[code] = close
                elif trade_day == following:
                    quotes[code] = {
                        "open": _open,
                        "close": close,
                        "pct_chg": _pct,
                        "prev_close": closes.get(code),
                    }
            # 上面单条查询里 D 行可能排在 D+1 之后，补一遍 prev_close
            for code, quote in quotes.items():
                if quote["prev_close"] is None and code in closes:
                    quote["prev_close"] = closes[code]

            up_ok, _ = pool_ok.get((following, "pool_up"), (False, "MISSING"))
            if up_ok:
                outcome_limit_up = set(up_by_day.get(following, {}))
            else:
                reasons.append("OUTCOME_LIMIT_POOL_UNVERIFIED")

        stats, values = _stats(codes, quotes, limit_up_codes=outcome_limit_up)
        values_by_day[day] = values

        if stats["sample_count"] == 0:
            if any(reason.startswith(("UP_POOL", "BROKEN_POOL", "PREV_UP", "RELAY")) for reason in reasons):
                status = STATUS_UNVERIFIED
            else:
                status = STATUS_EMPTY
                reasons.append("NO_SAMPLES")
        elif not outcome_ready:
            status = STATUS_PENDING
        elif stats["valid_count"] == 0:
            # D+1 日线已到（data_as_of 覆盖），但这批样本都没有行情：这是
            # 「样本缺失」，不是「待观察」—— 不能因为均值为空就伪装成未成熟。
            status = STATUS_PARTIAL
            reasons.append("OUTCOME_PARTIAL")
        elif stats["missing_count"] > 0 or stats["unverifiable_count"] > 0:
            status = STATUS_PARTIAL
            if stats["missing_count"] > 0:
                reasons.append("OUTCOME_PARTIAL")
        else:
            status = STATUS_VERIFIED

        if status == STATUS_PENDING:
            pending_days.append(day.isoformat())
        elif status == STATUS_UNVERIFIED:
            unverified_days.append(day.isoformat())

        series.append(
            {
                "sample_date": day,
                "outcome_date": following,
                "group": group,
                "sentiment_value": metric_value,
                **{
                    "limit_up_count": sentiment.limit_up_count if sentiment else None,
                    "broken_rate": sentiment.broken_rate if sentiment else None,
                    "max_consecutive": sentiment.max_consecutive if sentiment else None,
                    "up_count": sentiment.up_count if sentiment else None,
                    "down_count": sentiment.down_count if sentiment else None,
                    "advance_ratio": _metric_value(sentiment, "advance_ratio"),
                },
                **stats,
                "data_quality_status": status,
                "data_quality_reasons": _dedupe(reasons),
                "data_as_of": data_as_of,
                **(
                    {
                        # 审计诊断字段（R3-P2-02）：定位缺失股票代码，不参与业务统计
                        "sample_codes": list(codes),
                        "valid_codes": values.get("valid_codes") or [],
                        "missing_codes": values.get("missing_codes") or [],
                    }
                    if include_code_details
                    else {}
                ),
            }
        )

    visible = series
    if not include_pending:
        visible = [row for row in series if row["data_quality_status"] != STATUS_PENDING]

    summary = _summary(visible, values_by_day)
    bands = _bands(visible, metric, values_by_day)
    status_counts: dict[str, int] = {}
    for row in series:
        status = row["data_quality_status"]
        status_counts[status] = status_counts.get(status, 0) + 1

    return {
        "group": group,
        "group_label": GROUP_LABELS[group],
        "metric": metric,
        "metric_label": METRIC_LABELS[metric],
        "days": days,
        "threshold_version": THRESHOLD_VERSION,
        "aggregation_version": AGGREGATION_VERSION,
        "thresholds": [
            {"label": label, "lower": lower, "upper": upper}
            for lower, upper, label in METRICS[metric]
        ],
        "data_as_of": data_as_of,
        "dates": [row["sample_date"] for row in series],
        "series": visible,
        "summary": summary,
        "bands": bands,
        "quality": {
            "status_counts": status_counts,
            "pending_days": pending_days,
            "unverified_days": unverified_days,
            "metric_missing_days": metric_missing_days,
            "group_note": GROUP_NOTE,
        },
        "notes": NOTES,
    }


def _pooled(values_by_day: dict[date, dict[str, list[float]]], rows: list[dict], key: str) -> list[float]:
    """把多天的逐券明细拼成一个池（每个证券-交易日一个观测，不按天去重）。"""
    pooled: list[float] = []
    for row in rows:
        pooled.extend(values_by_day.get(row["sample_date"], {}).get(key, []))
    return pooled


def _summary(
    rows: list[dict], values_by_day: dict[date, dict[str, list[float]]] | None = None
) -> dict[str, Any]:
    """把逐日结果汇总为总体统计（只统计有有效行情的日期）。

    两套均值口径同时返回，绝不是同一个数：
    - `security_weighted_avg_pct` / `avg_pct`：逐券 pooled（分母为证券-交易日观测数）；
    - `day_equal_weight_avg_pct`：每个有统计的交易日等权（S1 的旧口径）。
    `median_pct` 明确为 pooled 中位数，不是「每日中位数的中位数」。
    """
    values_by_day = values_by_day or {}
    valid_rows = [row for row in rows if row["valid_count"] > 0]
    sample_count = sum(row["sample_count"] for row in rows if row["data_quality_status"] != STATUS_PENDING)
    valid_count = sum(row["valid_count"] for row in rows)
    missing_count = sum(row["missing_count"] for row in rows if row["data_quality_status"] != STATUS_PENDING)
    day_means = [row["avg_pct"] for row in valid_rows if row["avg_pct"] is not None]
    pooled_pct = _pooled(values_by_day, valid_rows, "pct")
    up_count = sum(row["outcome_up_count"] for row in valid_rows)
    down_count = sum(row["outcome_down_count"] for row in valid_rows)
    flat_count = sum(row["outcome_flat_count"] for row in valid_rows)
    again_rows = [row for row in valid_rows if row["limit_up_again_count"] is not None]
    again_count = sum(row["limit_up_again_count"] for row in again_rows)
    again_base = sum(row["valid_count"] for row in again_rows)
    pooled_avg = round(sum(pooled_pct) / len(pooled_pct), 2) if pooled_pct else None
    return {
        "day_count": len(rows),
        "stat_day_count": len(valid_rows),
        "sample_count": sample_count,
        "valid_count": valid_count,
        "missing_count": missing_count,
        #: 逐券 pooled 均值（每个证券-交易日一个观测）
        "security_weighted_avg_pct": pooled_avg,
        #: 当日等权均值（旧口径，保留兼容与对照）
        "day_equal_weight_avg_pct": _mean(day_means),
        "avg_pct": pooled_avg,
        "median_pct": round(median(pooled_pct), 2) if pooled_pct else None,
        "up_count": up_count,
        "down_count": down_count,
        "flat_count": flat_count,
        "up_ratio": round(up_count / valid_count * 100, 2) if valid_count else None,
        "limit_up_again_count": again_count if again_rows else None,
        "limit_up_again_ratio": (
            round(again_count / again_base * 100, 2) if again_base else None
        ),
        "not_limit_up_count": (again_base - again_count) if again_rows else None,
        "unverifiable_count": sum(row["unverifiable_count"] for row in valid_rows),
        "avg_open_pct": _pooled_mean(values_by_day, valid_rows, "open_pct"),
        "avg_close_vs_open": _pooled_mean(values_by_day, valid_rows, "close_vs_open"),
    }


def _pooled_mean(
    values_by_day: dict[date, dict[str, list[float]]], rows: list[dict], key: str
) -> float | None:
    values = _pooled(values_by_day, rows, key)
    return round(sum(values) / len(values), 2) if values else None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _bands(
    rows: list[dict],
    metric: str,
    values_by_day: dict[date, dict[str, list[float]]] | None = None,
) -> list[dict[str, Any]]:
    values_by_day = values_by_day or {}
    buckets: dict[str, list[dict]] = {
        label: [] for _lower, _upper, label in METRICS[metric]
    }
    for row in rows:
        found = _band(row["sentiment_value"], metric)
        if found is None:
            continue
        buckets[found[0]].append(row)

    result: list[dict[str, Any]] = []
    for lower, upper, label in METRICS[metric]:
        items = buckets[label]
        valid_rows = [row for row in items if row["valid_count"] > 0]
        valid = sum(row["valid_count"] for row in valid_rows)
        day_means = [row["avg_pct"] for row in valid_rows if row["avg_pct"] is not None]
        pooled_pct = _pooled(values_by_day, valid_rows, "pct")
        pooled_avg = round(sum(pooled_pct) / len(pooled_pct), 2) if pooled_pct else None
        up = sum(row["outcome_up_count"] for row in valid_rows)
        again_rows = [row for row in valid_rows if row["limit_up_again_count"] is not None]
        again_base = sum(row["valid_count"] for row in again_rows)
        again = sum(row["limit_up_again_count"] for row in again_rows)
        result.append(
            {
                "label": label,
                "lower": lower,
                "upper": upper,
                "day_count": len(items),
                "stat_day_count": len(valid_rows),
                "sample_count": sum(row["sample_count"] for row in items),
                "valid_count": valid,
                "security_weighted_avg_pct": pooled_avg,
                "day_equal_weight_avg_pct": _mean(day_means),
                "avg_pct": pooled_avg,
                "median_pct": round(median(pooled_pct), 2) if pooled_pct else None,
                "up_ratio": round(up / valid * 100, 2) if valid else None,
                "limit_up_again_ratio": (
                    round(again / again_base * 100, 2) if again_base else None
                ),
            }
        )
    return result


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _empty(group: str, metric: str, days: int, data_as_of: date | None) -> dict[str, Any]:
    return {
        "group": group,
        "group_label": GROUP_LABELS[group],
        "metric": metric,
        "metric_label": METRIC_LABELS[metric],
        "days": days,
        "threshold_version": THRESHOLD_VERSION,
        "aggregation_version": AGGREGATION_VERSION,
        "thresholds": [
            {"label": label, "lower": lower, "upper": upper}
            for lower, upper, label in METRICS[metric]
        ],
        "data_as_of": data_as_of,
        "dates": [],
        "series": [],
        "summary": _summary([]),
        "bands": [],
        "quality": {
            "status_counts": {},
            "pending_days": [],
            "unverified_days": [],
            "metric_missing_days": [],
            "group_note": GROUP_NOTE,
        },
        "notes": NOTES,
    }


def reason_text(code: str) -> str:
    return QUALITY_REASON_TEXT.get(code, code)
