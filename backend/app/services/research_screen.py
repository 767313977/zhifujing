"""P0.2 A 类研究指标：趋势 / 低位启动 / 板块状态。

只读本地库，复用：
- `services.patterns.build_bars`（经 `services.trend.load_bars`）的前复权 K 线；
- `rps_daily` 的 20/60/120 分位；
- `sector_daily` / `sector_member`；
- `limit_rules` 的涨跌幅与北交所判断。

所有指标只使用目标交易日及以前的数据；缺失值返回 None + `missing[]`，不填 0。
"""

from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from datetime import date
from typing import Iterable

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import RpsDaily, SectorDaily, SectorMember, StockBasic, StockDaily, StockUniverse, TradeCalendar
from app.services import data_revision, limit_rules, trend
from app.services.patterns import build_bars, usable_bar

TREND_STRATEGY = {
    "id": "trend_template",
    "version": "p0.2-2026-11-09",
    "status": "研究假设：统计稳定性不足，未通过正式策略有效性验证",
}
LOW_START_STRATEGY = {
    "id": "low_start_features",
    "version": "p0.2-2026-11-09",
    "status": "研究特征组合：仅用于观察，未验证预测能力",
}
SECTOR_STATE_STRATEGY = {
    "id": "sector_state",
    "version": "p0.2-2026-11-09",
    "status": "市场表现特征，不代表未来涨跌",
}


def target_date(session: Session, trade_date: date | None = None) -> date:
    if trade_date:
        return trade_date
    value = session.scalar(select(func.max(StockDaily.trade_date)))
    if value is None:
        raise ValueError("库里没有日线")
    return value


def _latest_date(session: Session) -> str | None:
    """库内最新日线日：页面用它提示「所选日期是历史数据」。"""
    value = session.scalar(select(func.max(StockDaily.trade_date)))
    return str(value) if value else None


# ---- 数据覆盖状态（P0.9-01 / P0.9-07）----
#
# 研究接口必须能区分四种「空结果」：
#   - 底层个股日线没有（未来日期 / RPS 孤儿日）        → no_bars
#   - 有日线但当日 RPS 一条都没有（未采集）             → no_rps
#   - 有 RPS 行但 60 日值全为空（旧版本 / 窗口不足）    → no_rps60
#   - RPS60 覆盖明显偏低（横截面不完整、分位不可比）    → partial_rps
# 只有 `ok` 时的空结果才是「筛选后确实没有符合条件的股票」。
DATA_STATUS_OK = "ok"
DATA_STATUS_NO_BARS = "no_bars"
DATA_STATUS_NO_RPS = "no_rps"
DATA_STATUS_NO_RPS60 = "no_rps60"
DATA_STATUS_PARTIAL_RPS = "partial_rps"

#: RPS60 的当日覆盖下限（相对当日个股日线行数）。低于它时横截面分位不可比：
#: RPS 的分母是「当天该窗口有值的票」，缺一大片之后 90 分位已经不是同一个横截面。
RPS_COVERAGE_MIN = 0.9

DATA_STATUS_TEXT = {
    DATA_STATUS_OK: "行情与 RPS 覆盖正常",
    DATA_STATUS_NO_BARS: "所选日期没有任何个股日线（底层行情缺失），无法计算研究指标",
    DATA_STATUS_NO_RPS: "当日个股日线已入库，但没有 RPS 记录（RPS 未采集）",
    DATA_STATUS_NO_RPS60: "当日有 RPS 行，但 60 日值全部为空（旧版本数据或窗口不足）",
    DATA_STATUS_PARTIAL_RPS: "当日 RPS60 覆盖不足，横截面分位不可比，结果仅供参考",
}


def day_coverage(session: Session, day: date) -> dict:
    """某一交易日的「个股日线 / RPS」覆盖（P0.9-01）。

    只用 COUNT 查询，不读全表；`rps_coverage = rps60_rows / daily_rows`。
    不用 `MAX(trade_date)` 判断，因为 RPS 可能比行情更新（孤儿行）或更旧。
    """
    daily_rows = int(
        session.scalar(
            select(func.count()).select_from(StockDaily).where(StockDaily.trade_date == day)
        )
        or 0
    )
    rps_rows = int(
        session.scalar(
            select(func.count()).select_from(RpsDaily).where(RpsDaily.trade_date == day)
        )
        or 0
    )
    rps60_rows = int(
        session.scalar(
            select(func.count())
            .select_from(RpsDaily)
            .where(RpsDaily.trade_date == day, RpsDaily.rps60.is_not(None))
        )
        or 0
    )
    coverage = (rps60_rows / daily_rows) if daily_rows else None
    if daily_rows == 0:
        status = DATA_STATUS_NO_BARS
    elif rps_rows == 0:
        status = DATA_STATUS_NO_RPS
    elif rps60_rows == 0:
        status = DATA_STATUS_NO_RPS60
    elif coverage is not None and coverage < RPS_COVERAGE_MIN:
        status = DATA_STATUS_PARTIAL_RPS
    else:
        status = DATA_STATUS_OK
    return {
        "daily_rows": daily_rows,
        "rps_rows": rps_rows,
        "rps60_rows": rps60_rows,
        "rps_coverage": round(coverage, 4) if coverage is not None else None,
        # RPS 有行、行情没有：孤儿 RPS（日期晚于底层行情末日）
        "orphan_rps": bool(rps_rows and not daily_rows),
        "data_status": status,
        "status_text": DATA_STATUS_TEXT[status],
    }


# ---- 结果缓存（P0.3 + R5 加固）----
#
# 低位启动是**全市场 250 日扫描**（5,261 只 ≈ 130 万行），冷查询约 6 秒。盘后研究是
# 按交易日看同一份结果，重复打开页面没有理由再算一遍。
#
# 安全边界（R5-01）：
# - 只有 `data_revision.status()` 判定 ready 才启用缓存；
# - 修订表/触发器缺失、版本读取失败、库身份不可确定 → **本次请求直接查库，不读不写缓存**；
# - 数据库本身读失败 → 异常向上抛，绝不用旧缓存冒充当前数据；
# - 缓存键 = 库身份 + 交易日 + 参数 + 各数据域修订版本。
#
# 快照一致性（R5-03）：计算前后各读一次版本，不一致就重算（最多 3 轮），
# 仍不稳定则本次不写缓存 —— 避免把旧数据标成新版本长期命中。
_CACHE: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
_CACHE_TTL_SECONDS = 600.0
_CACHE_MAX_ENTRIES = 16
_VERIFY_ROUNDS = 3
_INFLIGHT_TIMEOUT = 60.0

#: 同一缓存键的并发请求合并（single-flight）：只有一个线程扫全市场，其余等它。
_INFLIGHT: dict[tuple, "threading.Event"] = {}
_INFLIGHT_LOCK = threading.Lock()

#: 进程内诊断计数（重启归零）。只用于确认「本次请求是否命中缓存」，
#: 不参与缓存正确性判断。
_CACHE_STATS = {"hits": 0, "computes": 0, "cache_disabled": 0, "busy": 0}


def cache_stats() -> dict:
    return dict(_CACHE_STATS)


#: 全市场冷扫描（low-start）的并发上限。实测 4 个不同交易日并发时 RSS 接近 3 GB、
#: 单次延迟被 IO 竞争拉到 40 秒以上；这里限流 + 有界等待，超时明确返回 503，
#: 不让请求无限堆积（也不通过缩小股票池来掩盖问题）。
_LOW_START_CONCURRENCY = 2
_LOW_START_WAIT_SECONDS = 20.0
_LOW_START_SLOTS = threading.BoundedSemaphore(_LOW_START_CONCURRENCY)


class ResearchBusy(RuntimeError):
    """研究计算繁忙（并发已满且等待超时），由 API 映射为 503。"""


def _limited_low_start(session: Session, day: date, **kwargs) -> dict:
    if not _LOW_START_SLOTS.acquire(timeout=_LOW_START_WAIT_SECONDS):
        _CACHE_STATS["busy"] += 1
        raise ResearchBusy("研究计算繁忙，请稍后重试")
    try:
        return _low_start_rows(session, day, **kwargs)
    finally:
        _LOW_START_SLOTS.release()


def _cache_key(name: str, day: date, params: tuple, state) -> tuple:
    return (name, str(day), params, state.identity, state.version)


def _cache_get(key: tuple) -> dict | None:
    hit = _CACHE.get(key)
    if hit is None:
        return None
    if time.monotonic() - hit[0] >= _CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    _CACHE.move_to_end(key)
    return copy.deepcopy(hit[1])


def _cache_put(key: tuple, value: dict) -> None:
    _CACHE[key] = (time.monotonic(), copy.deepcopy(value))
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX_ENTRIES:
        _CACHE.popitem(last=False)


def _compute_verified(session: Session, name: str, day: date, params: tuple, compute, state) -> dict:
    """计算并校验版本；版本在计算期间变化则重算，最多 `_VERIFY_ROUNDS` 轮。"""
    for _ in range(_VERIFY_ROUNDS):
        _CACHE_STATS["computes"] += 1
        value = compute()
        after = data_revision.status(session)
        if not after.ready:
            # 版本机制在计算期间失效：本次结果照常返回，但不写缓存
            data_revision.log_unavailable(after)
            return value
        if after.identity == state.identity and after.version == state.version:
            _cache_put(_cache_key(name, day, params, after), value)
            return value
        state = after
    return value


def _cached(session: Session, name: str, day: date, params: tuple, compute):
    state = data_revision.status(session)
    if not state.ready:
        # fail closed：不读旧缓存、不写新缓存，直接查库
        _CACHE_STATS["cache_disabled"] += 1
        data_revision.log_unavailable(state)
        return compute()
    key = _cache_key(name, day, params, state)
    hit = _cache_get(key)
    if hit is not None:
        _CACHE_STATS["hits"] += 1
        return hit

    while True:
        with _INFLIGHT_LOCK:
            event = _INFLIGHT.get(key)
            leader = event is None
            if leader:
                event = threading.Event()
                _INFLIGHT[key] = event
        if leader:
            break
        # 等待同键的 leader；超时或 leader 失败时自己尝试
        event.wait(_INFLIGHT_TIMEOUT)
        hit = _cache_get(key)
        if hit is not None:
            return hit

    try:
        return _compute_verified(session, name, day, params, compute, state)
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(key, None)
        event.set()


def clear_cache() -> None:
    """测试/脚本用：清空研究结果缓存。"""
    _CACHE.clear()
    with _INFLIGHT_LOCK:
        _INFLIGHT.clear()


def _trading_days_before(session: Session, day: date, count: int) -> list[date]:
    return list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date < day)
            .order_by(TradeCalendar.trade_date.desc())
            .limit(count)
        )
    )


def _rps(session: Session, day: date, codes: Iterable[str]) -> dict[str, RpsDaily]:
    wanted = list(codes)
    if not wanted:
        return {}
    return {
        row.code: row
        for row in session.scalars(
            select(RpsDaily).where(RpsDaily.trade_date == day, RpsDaily.code.in_(wanted))
        )
    }


def _names(session: Session, day: date, codes: Iterable[str]) -> dict[str, str | None]:
    return dict(
        session.execute(
            select(StockDaily.code, StockDaily.name).where(
                StockDaily.trade_date == day, StockDaily.code.in_(list(codes))
            )
        ).all()
    )


def _industries(session: Session, codes: Iterable[str]) -> dict[str, str | None]:
    return dict(
        session.execute(
            select(StockBasic.code, StockBasic.industry).where(StockBasic.code.in_(list(codes)))
        ).all()
    )


def _boards(session: Session, day: date, codes: Iterable[str]) -> dict[str, list[str]]:
    rows = session.execute(
        select(SectorMember.code, SectorMember.sector_code).where(
            SectorMember.trade_date == day, SectorMember.code.in_(list(codes))
        )
    ).all()
    out: dict[str, list[str]] = {}
    for code, board in rows:
        out.setdefault(code, []).append(board)
    return out


def _ret(bars, window: int) -> float | None:
    pct = bars.pct_chg[-window:]
    if len(pct) < max(2, int(window * 0.9)):
        return None
    return round(float((np.prod(1 + pct / 100) - 1) * 100), 2)


def _ma(values: np.ndarray, window: int) -> float | None:
    if len(values) < window:
        return None
    return float(values[-window:].mean())


def _slope(values: np.ndarray, window: int, days: int = 5) -> float | None:
    ma = _ma(values, window)
    if ma is None or len(values) < window + days:
        return None
    old = float(values[-(window + days) : -days].mean())
    return round((ma / old - 1) * 100, 2) if old else None


def _trend_days(bars) -> int:
    close = bars.close
    ma20 = _ma(close, 20)
    if ma20 is None:
        return 0
    count = 0
    for i in range(len(close) - 1, 19, -1):
        if close[i] > close[i - 19 : i + 1].mean():
            count += 1
        else:
            break
    return count


def _amount_ratio(bars, window: int = 20) -> float | None:
    amount = bars.amount
    if len(amount) < window + 1:
        return None
    base = float(np.nanmean(amount[-window - 1 : -1]))
    return round(float(amount[-1] / base), 2) if base > 0 else None


def trend_rows(
    session: Session,
    *,
    trade_date: date | None = None,
    min_rps60: float = 60.0,
    min_trend_days: int = 5,
    limit: int = 100,
    exclude_st: bool = True,
    exclude_bse: bool = True,
) -> dict:
    day = target_date(session, trade_date)
    params = (min_rps60, min_trend_days, limit, exclude_st, exclude_bse)
    result = _cached(
        session,
        "trend",
        day,
        params,
        lambda: _trend_rows(
            session,
            day,
            min_rps60=min_rps60,
            min_trend_days=min_trend_days,
            limit=limit,
            exclude_st=exclude_st,
            exclude_bse=exclude_bse,
        ),
    )
    result["data_latest"] = _latest_date(session)
    return result


def _trend_rows(
    session: Session,
    day: date,
    *,
    min_rps60: float = 60.0,
    min_trend_days: int = 5,
    limit: int = 100,
    exclude_st: bool = True,
    exclude_bse: bool = True,
) -> dict:
    # 先判覆盖，再看候选（P0.9-01）：不能因为 candidates 为空就说「没有符合条件的票」，
    # 也不能反过来把「RPS 没采」当成「市场没有强势股」。
    cover = day_coverage(session, day)
    if cover["data_status"] != DATA_STATUS_OK:
        return {
            "trade_date": day,
            "rows": [],
            "coverage": {"candidates": 0, **cover},
            "strategy": TREND_STRATEGY,
        }
    candidates = list(
        session.scalars(
            select(RpsDaily.code)
            .where(RpsDaily.trade_date == day, RpsDaily.rps60.is_not(None), RpsDaily.rps60 >= min_rps60)
            .order_by(RpsDaily.rps60.desc())
            .limit(max(limit * 8, 400))
        )
    )
    if not candidates:
        return {
            "trade_date": day,
            "rows": [],
            "coverage": {
                "candidates": 0,
                **cover,
                "note": f"当日数据完整，但没有 RPS60 ≥ {min_rps60:g} 的股票",
            },
            "strategy": TREND_STRATEGY,
        }
    names = _names(session, day, candidates)
    industries = _industries(session, candidates)
    boards = _boards(session, day, candidates)
    rps = _rps(session, day, candidates)
    bars_by_code = trend.load_bars(session, candidates, day)
    rows = []
    for code in candidates:
        bars = bars_by_code.get(code)
        name = names.get(code)
        rps_row = rps.get(code)
        missing: list[str] = []
        if bars is None or len(bars) < 130:
            missing.append("日线不足 130 根")
        if rps_row is None or rps_row.rps60 is None:
            missing.append("RPS60 缺失")
        if exclude_st and name and "ST" in name.upper():
            continue
        if exclude_bse and limit_rules.is_bse(code):
            continue
        if missing:
            rows.append(
                {
                    "code": code,
                    "name": name,
                    "industry": industries.get(code),
                    "trade_date": str(day),
                    "state": "数据不足",
                    "missing": missing,
                    "boards": boards.get(code, []),
                    # 日线不足时 RPS 仍然有效：保留它，排序与展示才不会把这类票
                    # 一律沉到列表末尾（否则页面上根本看不到「数据不足」样本）。
                    "rps20": rps_row.rps20 if rps_row else None,
                    "rps60": rps_row.rps60 if rps_row else None,
                    "rps120": rps_row.rps120 if rps_row else None,
                    "strategy": TREND_STRATEGY,
                }
            )
            continue
        close = bars.close
        ma20 = _ma(close, 20)
        ma60 = _ma(close, 60)
        ma120 = _ma(close, 120)
        s20 = _slope(close, 20)
        s60 = _slope(close, 60)
        s120 = _slope(close, 120)
        trend_days = _trend_days(bars)
        off_high = round(float((close[-1] / np.max(bars.high[-60:]) - 1) * 100), 2)
        amount_ratio = _amount_ratio(bars)
        reasons = []
        unmet = []
        if ma20 and ma60 and close[-1] > ma20 > ma60:
            reasons.append(f"收盘 {close[-1]:.2f} > MA20 {ma20:.2f} > MA60 {ma60:.2f}")
        else:
            unmet.append("未形成 收盘 > MA20 > MA60")
        if ma120 and close[-1] > ma120:
            reasons.append(f"收盘高于 MA120 {ma120:.2f}")
        else:
            unmet.append("收盘低于 MA120")
        if s20 is not None and s20 > 0:
            reasons.append(f"MA20 5 日斜率 +{s20:.2f}%")
        else:
            unmet.append("MA20 斜率未转正")
        if trend_days >= min_trend_days:
            reasons.append(f"趋势持续 {trend_days} 天")
        else:
            unmet.append(f"趋势持续仅 {trend_days} 天")
        if amount_ratio is not None and amount_ratio >= 0.8:
            reasons.append(f"成交额/20 日均值 {amount_ratio:.2f}")
        else:
            unmet.append("成交额低于 20 日均值 0.8 倍")
        if off_high < -25:
            unmet.append(f"距 60 日高点 {off_high:.1f}%")
        state = "技术特征符合" if len(unmet) <= 2 else "条件未满足"
        if close[-1] < (ma60 or close[-1]) and (s60 or 0) < 0:
            state = "当前条件失效"
        rows.append(
            {
                "code": code,
                "name": name,
                "industry": industries.get(code),
                "trade_date": str(day),
                "close": round(float(close[-1]), 2),
                "ma20": round(ma20, 2) if ma20 else None,
                "ma60": round(ma60, 2) if ma60 else None,
                "ma120": round(ma120, 2) if ma120 else None,
                "ma20_slope": s20,
                "ma60_slope": s60,
                "ma120_slope": s120,
                "trend_days": trend_days,
                "ret20": _ret(bars, 20),
                "ret60": _ret(bars, 60),
                "ret120": _ret(bars, 120),
                "rps20": rps_row.rps20,
                "rps60": rps_row.rps60,
                "rps120": rps_row.rps120,
                "amount_ratio20": amount_ratio,
                "off_high60": off_high,
                "boards": boards.get(code, []),
                "reasons": reasons,
                "unmet": unmet,
                "missing": [],
                "state": state,
                "strategy": TREND_STRATEGY,
            }
        )
    rows.sort(key=lambda row: (-(row.get("rps60") or -1), -(row.get("trend_days") or 0), row["code"]))
    return {
        "trade_date": day,
        "rows": rows[:limit],
        "coverage": {
            "candidates": len(candidates),
            "with_boards": sum(1 for code in candidates if boards.get(code)),
            "bars": len(bars_by_code),
            **cover,
        },
        "strategy": TREND_STRATEGY,
    }


def low_start_rows(
    session: Session,
    *,
    trade_date: date | None = None,
    limit: int = 100,
    max_position: float = 40.0,
    min_platform_days: int = 15,
    max_platform_height: float = 18.0,
    max_vol_contraction: float = 0.9,
    exclude_st: bool = True,
    exclude_bse: bool = True,
) -> dict:
    day = target_date(session, trade_date)
    params = (
        limit,
        max_position,
        min_platform_days,
        max_platform_height,
        max_vol_contraction,
        exclude_st,
        exclude_bse,
    )
    result = _cached(
        session,
        "low_start",
        day,
        params,
        lambda: _limited_low_start(
            session,
            day,
            limit=limit,
            max_position=max_position,
            min_platform_days=min_platform_days,
            max_platform_height=max_platform_height,
            max_vol_contraction=max_vol_contraction,
            exclude_st=exclude_st,
            exclude_bse=exclude_bse,
        ),
    )
    result["data_latest"] = _latest_date(session)
    return result


def _low_start_rows(
    session: Session,
    day: date,
    *,
    limit: int = 100,
    max_position: float = 40.0,
    min_platform_days: int = 15,
    max_platform_height: float = 18.0,
    max_vol_contraction: float = 0.9,
    exclude_st: bool = True,
    exclude_bse: bool = True,
) -> dict:
    cover = day_coverage(session, day)
    if cover["data_status"] == DATA_STATUS_NO_BARS:
        # 没有底层日线（未来日期 / RPS 孤儿日）：不扫全市场，直接返回明确的覆盖状态。
        return {
            "trade_date": day,
            "rows": [],
            "coverage": {"candidates": 0, "bars": 0, **cover},
            "strategy": LOW_START_STRATEGY,
        }
    day_rps_rows = cover["rps_rows"]
    codes = list(session.scalars(select(StockUniverse.code).limit(6000)))
    if not codes:
        return {
            "trade_date": day,
            "rows": [],
            "coverage": {"candidates": 0, **cover},
            "strategy": LOW_START_STRATEGY,
        }
    names = _names(session, day, codes)
    industries = _industries(session, codes)
    rps = _rps(session, day, codes)

    # 流式扫描（P0.5-03）：按 (code, trade_date) 有序、每 400 只一批读取 250 日窗口，
    # 逐只构建复权 K 线并计算，算完即丢。既避免「全市场 130 万行 + 5,000 份 Bars」同时
    # 驻留，也不做逐股票查询。复权仍走 `patterns.build_bars`，窗口仍为
    # `TREND_LOOKBACK+10` 个交易日，指标函数与旧实现完全相同。
    window_days = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= day)
            .order_by(TradeCalendar.trade_date.desc())
            .limit(trend.TREND_LOOKBACK + 10)
        )
    )
    if not window_days:
        return {
            "trade_date": day,
            "rows": [],
            "coverage": {"candidates": len(codes), "bars": 0, **cover},
            "strategy": LOW_START_STRATEGY,
        }
    start = min(window_days)
    rows: list[dict] = []
    built = 0
    rps_missing = 0

    def flush(code: str | None, items: list[dict]) -> None:
        nonlocal built, rps_missing
        if code is None or len(items) < trend.TREND_LOOKBACK:
            return
        built += 1
        name = names.get(code)
        if exclude_st and name and "ST" in name.upper():
            return
        if exclude_bse and limit_rules.is_bse(code):
            return
        bars = build_bars(items, code)
        close, high, low = bars.close, bars.high, bars.low
        window = min(250, len(close))
        lo = float(np.min(low[-window:]))
        hi = float(np.max(high[-window:]))
        pos = float(round((close[-1] - lo) / (hi - lo) * 100, 1)) if hi > lo else None
        if pos is None or pos > max_position:
            return
        platform = min(40, len(close) - 21)
        if platform < min_platform_days:
            return
        ph = float(np.max(high[-platform - 1 : -1]))
        pl = float(np.min(low[-platform - 1 : -1]))
        height = round((ph / pl - 1) * 100, 1) if pl > 0 else None
        if height is None or height > max_platform_height:
            return
        # 波动收敛看的是**突破日之前**的平台期：把今天算进去会因突破当日的放量长阳
        # 抬高 vol20，反而把刚刚启动的票筛掉。
        vol20 = float(np.std(bars.pct_chg[-21:-1]))
        vol60 = float(np.std(bars.pct_chg[-61:-1]))
        contraction = round(vol20 / vol60, 2) if vol60 > 0 else 0.0
        if contraction > max_vol_contraction:
            return
        amount_ratio = _amount_ratio(bars)
        ma20 = _ma(close, 20)
        s20 = _slope(close, 20)
        breakout = close[-1] > ph * 1.001
        rps_row = rps.get(code)
        rps20 = rps_row.rps20 if rps_row else None
        # RPS 缺失必须写在行上（P0.9-02）：低位启动本身**不依赖** RPS，
        # 所以缺失不能让这只票被剔除，但也不能悄悄留一个空的 rps20 加 missing=[]。
        # 三种原因要分开：整日没采 / 当日没有该股记录 / 有记录但 20 日窗口不足。
        if day_rps_rows == 0:
            rps_status = "day_missing"
            missing = ["RPS20 缺失（当日 RPS 未采集）"]
        elif rps_row is None:
            rps_status = "no_row"
            missing = ["RPS20 缺失（当日无该股 RPS 记录）"]
        elif rps_row.rps20 is None:
            # 能走到这里的票都有 250 根可用日线（`flush` 的门槛），所以不存在
            # 「上市不足 20 日」—— 只可能是窗口内停牌/缺数据，覆盖不到 90%。
            rps_status = "window"
            missing = ["RPS20 缺失（20 日窗口覆盖不足：停牌或数据缺失）"]
        else:
            rps_status = "value"
            missing = []
        if missing:
            rps_missing += 1
        if breakout and amount_ratio and amount_ratio >= 1.3 and (s20 or 0) > 0:
            state = "突破确认"
        elif breakout:
            state = "启动候选"
        elif (s20 or 0) > 0 or (amount_ratio or 0) >= 1.2:
            state = "启动候选"
        else:
            state = "低位观察"
        rows.append(
            {
                "code": code,
                "name": name,
                "industry": industries.get(code),
                "trade_date": str(day),
                "close": round(float(close[-1]), 2),
                "pos_pct": pos,
                "platform_days": platform,
                "platform_height": height,
                "platform_high": round(ph, 2),
                "vol_contraction": contraction,
                "amount_ratio20": amount_ratio,
                "ma20": round(ma20, 2) if ma20 else None,
                "ma20_slope": s20,
                "rps20": rps20,
                "rps_status": rps_status,
                "state": state,
                "reasons": [
                    f"区间位置 {pos:.1f}%",
                    f"平台 {platform} 天、振幅 {height:.1f}%",
                    f"波动收敛 {contraction:.2f}",
                ],
                "missing": missing,
                "strategy": LOW_START_STRATEGY,
            }
        )

    current: str | None = None
    items: list[dict] = []
    for offset in range(0, len(codes), 400):
        chunk = codes[offset : offset + 400]
        result = session.execute(
            select(
                StockDaily.code,
                StockDaily.trade_date,
                StockDaily.open,
                StockDaily.high,
                StockDaily.low,
                StockDaily.close,
                StockDaily.volume,
                StockDaily.amount,
                StockDaily.pct_chg,
            )
            .where(
                StockDaily.code.in_(chunk),
                StockDaily.trade_date >= start,
                StockDaily.trade_date <= day,
            )
            .order_by(StockDaily.code, StockDaily.trade_date)
        )
        for code, trade_day, open_, high, low, close, volume, amount, pct in result:
            if code != current:
                flush(current, items)
                current = code
                items = []
            item = {
                "date": trade_day,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
                "pct_chg": pct,
                "name": None,
            }
            # 停牌残行（只有收盘价）不能当一根 K 线，与扫描 / 回测同口径
            if usable_bar(item):
                items.append(item)
    flush(current, items)

    rows.sort(key=lambda row: (row["state"] != "突破确认", row["pos_pct"], -(row.get("amount_ratio20") or 0)))
    return {
        "trade_date": day,
        "rows": rows[:limit],
        "coverage": {
            "candidates": len(codes),
            "bars": built,
            # 未满 250 根可用日线的票（新股 / 历史不足）：与 RPS 缺失是两回事
            "excluded_short_bars": max(0, len(codes) - built),
            "rps_missing_rows": rps_missing,
            **cover,
        },
        "strategy": LOW_START_STRATEGY,
    }


MEMBER_STATUS_TEXT = {
    "complete": "成分股完整（manifest 与来源 Count 一致）",
    "partial": "部分覆盖：来源 Count 大于实际取到的去重数",
    "source_empty": "来源当日没有成分股",
    "failed": "采集失败",
    "unsupported": "来源不支持该日期",
    "unsupported_history": "来源不支持该日期",
    "unverified": "来源没给 Count，无法对账",
    "not_collected": "尚未采集",
    "mismatch": "manifest 标记完整，但库内成员数与来源 Count 不一致",
    "missing_manifest": "没有 manifest 记录，完整性未知",
    "no_members": "当日没有成分股记录",
}

#: manifest 里算「完整」的状态值（与 `scripts/backfill_sector_members.py` 一致）
MEMBER_COMPLETE = "complete"


def _member_manifest(session: Session, day: date) -> dict[str, dict] | None:
    """读取当日的成分股回补 manifest。

    表不存在（生产尚未安装回补链路）时返回 `None` —— 调用方据此标「完整性未知」，
    **不能**因为 `sector_member` 表里有行就认定完整。
    """
    exists = session.connection().exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sector_member_backfill'"
    ).first()
    if not exists:
        return None
    rows = session.connection().exec_driver_sql(
        "SELECT sector_code, status, reported, unique_count, written"
        " FROM sector_member_backfill WHERE trade_date = ?",
        (day.isoformat(),),
    ).all()
    return {
        row[0]: {
            "status": row[1],
            "reported": row[2],
            "unique": row[3],
            "written": row[4],
        }
        for row in rows
    }


def _breadth_status(
    manifest_installed: bool, entry: dict | None, actual_rows: int
) -> tuple[str, str]:
    """把 manifest 状态翻译成「广度可不可信」（P0.9-05）。

    返回 `(status, reason)`；只有 `status == "complete"` 才允许用成分股计算广度。
    """
    if actual_rows == 0 and entry is None:
        return "no_members", MEMBER_STATUS_TEXT["no_members"]
    if not manifest_installed:
        return "missing_manifest", "库内没有 manifest 表，无法认定成分股完整"
    if entry is None:
        return "missing_manifest", "该板块当日没有 manifest 记录，完整性未知"

    status = (entry.get("status") or "").lower()
    if status == MEMBER_COMPLETE:
        expected = entry.get("unique")
        if expected is None:
            expected = entry.get("reported")
        if expected is None:
            return "unverified", "manifest 标记完整但没有来源 Count，无法对账"
        if actual_rows != int(expected):
            return (
                "mismatch",
                f"manifest 标记完整，但库内成员 {actual_rows} 行 ≠ 来源 {int(expected)}",
            )
        return "complete", ""

    mapped = status or "unknown"
    return mapped, MEMBER_STATUS_TEXT.get(mapped, f"manifest 状态 {mapped}，完整性未知")


def _fallback_info(session: Session, requested: date, actual: date) -> dict:
    """板块数据的日期回退说明（P0.9-03）。"""
    if actual == requested:
        return {
            "applied": False,
            "requested_date": str(requested),
            "actual_data_date": str(actual),
            "gap_trading_days": 0,
            "severity": "none",
            "reason": None,
        }
    gap = int(
        session.scalar(
            select(func.count())
            .select_from(TradeCalendar)
            .where(TradeCalendar.trade_date > actual, TradeCalendar.trade_date <= requested)
        )
        or 0
    )
    severity = "high" if gap >= 5 else "warn"
    reason = f"所选日期 {requested} 没有板块日线，已回退到最近可用交易日 {actual}"
    if gap >= 5:
        reason += f"（相差 {gap} 个交易日，结果不代表所选日期的板块状态）"
    return {
        "applied": True,
        "requested_date": str(requested),
        "actual_data_date": str(actual),
        "gap_trading_days": gap,
        "severity": severity,
        "reason": reason,
    }


def sector_states(
    session: Session,
    *,
    trade_date: date | None = None,
    taxonomy: str = "kph_selected",
    limit: int = 100,
    allow_fallback: bool = True,
) -> dict:
    requested = target_date(session, trade_date)
    params = (taxonomy, limit, bool(allow_fallback))
    result = _cached(
        session,
        "sector_state",
        requested,
        params,
        lambda: _sector_states(
            session,
            requested,
            taxonomy=taxonomy,
            limit=limit,
            allow_fallback=allow_fallback,
        ),
    )
    result["data_latest"] = _latest_date(session)
    return result


def _sector_states(
    session: Session,
    requested: date,
    *,
    taxonomy: str = "kph_selected",
    limit: int = 100,
    allow_fallback: bool = True,
) -> dict:
    available = session.scalar(
        select(func.max(SectorDaily.trade_date)).where(
            SectorDaily.taxonomy == taxonomy, SectorDaily.trade_date <= requested
        )
    )
    if available is None:
        return {
            "trade_date": str(requested),
            "requested_date": str(requested),
            "actual_data_date": None,
            "fallback": {
                "applied": False,
                "requested_date": str(requested),
                "actual_data_date": None,
                "gap_trading_days": None,
                "severity": "high",
                "reason": "库里没有该分类的板块日线",
            },
            "rows": [],
            "coverage": {"days": 0, "status": "数据不足", "data_status": "no_data"},
            "strategy": SECTOR_STATE_STRATEGY,
        }
    day = available
    fallback = _fallback_info(session, requested, day)
    if fallback["applied"] and not allow_fallback:
        # 严格模式（P0.9-03）：宁可返回「不可用」，也不把旧日期当成请求日的结果。
        return {
            "trade_date": str(day),
            "requested_date": str(requested),
            "actual_data_date": str(day),
            "fallback": fallback,
            "rows": [],
            "coverage": {
                "days": 0,
                "status": "数据不足",
                "data_status": "requested_date_unavailable",
                "note": fallback["reason"],
            },
            "strategy": SECTOR_STATE_STRATEGY,
        }
    data_status = "fallback" if fallback["applied"] else "ok"
    days = list(
        session.scalars(
            select(SectorDaily.trade_date)
            .where(SectorDaily.taxonomy == taxonomy, SectorDaily.trade_date <= day)
            .group_by(SectorDaily.trade_date)
            .order_by(SectorDaily.trade_date.desc())
            .limit(61)
        )
    )
    days = sorted(days)
    if len(days) < 21:
        return {
            "trade_date": str(day),
            "requested_date": str(requested),
            "actual_data_date": str(day),
            "fallback": fallback,
            "rows": [],
            "coverage": {
                "days": len(days),
                "status": "数据不足",
                "data_status": "insufficient_history",
                "note": f"板块日线只有 {len(days)} 个交易日，不足 20 日 RPS 所需窗口",
            },
            "strategy": SECTOR_STATE_STRATEGY,
        }
    rows = session.execute(
        select(SectorDaily.trade_date, SectorDaily.sector_code, SectorDaily.name, SectorDaily.pct_chg, SectorDaily.amount)
        .where(SectorDaily.taxonomy == taxonomy, SectorDaily.trade_date.in_(days))
        .order_by(SectorDaily.trade_date)
    ).all()
    by_board: dict[str, dict[date, tuple[float | None, float | None]]] = {}
    board_names: dict[str, str] = {}
    for when, code, name, pct, amount in rows:
        by_board.setdefault(code, {})[when] = (pct, amount)
        if name:
            board_names[code] = name
    ret20_day: dict[date, dict[str, float]] = {}
    for board, series in by_board.items():
        ordered = sorted(series)
        for index, when in enumerate(ordered):
            if index < 19:
                continue
            value = 1.0
            for item in ordered[index - 19 : index + 1]:
                pct = series[item][0]
                if pct is not None:
                    value *= 1 + pct / 100
            ret20_day.setdefault(when, {})[board] = (value - 1) * 100
    rps20_by_day: dict[date, dict[str, float]] = {}
    for when, values in ret20_day.items():
        if len(values) < 5:
            continue
        ordered = sorted(values.items(), key=lambda item: item[1])
        rps20_by_day[when] = {
            board: (index + 0.5) / len(ordered) * 100 for index, (board, _) in enumerate(ordered)
        }
    ret: dict[int, dict[str, float]] = {5: {}, 10: {}, 20: {}, 60: {}}
    for board, series in by_board.items():
        ordered = sorted(series)
        for window in (5, 10, 20, 60):
            if len(ordered) < window:
                continue
            value = 1.0
            for when in ordered[-window:]:
                pct = series[when][0]
                if pct is not None:
                    value *= 1 + pct / 100
            ret[window][board] = (value - 1) * 100
    rps20 = rps20_by_day.get(day, {})
    member_counts = dict(
        session.execute(
            select(SectorMember.sector_code, func.count()).where(
                SectorMember.trade_date == day
            ).group_by(SectorMember.sector_code)
        ).all()
    )
    manifest = _member_manifest(session, day)
    members = session.execute(
        select(SectorMember.code, SectorMember.sector_code, SectorMember.pct_chg).where(
            SectorMember.trade_date == day
        )
    ).all()
    breadth = {}
    core = {}
    for code, board, pct in members:
        if board not in by_board:
            continue
        breadth.setdefault(board, []).append(pct)
        if pct is not None:
            core.setdefault(board, []).append((pct, code))

    # ---- 集合关系（P0.9-04）----
    # 每个统计都落在明确的分母上，且满足：
    #   boards_in_day = boards_with_rps20 + excluded_no_rps20 + excluded_short_history
    #   matched = boards_with_rps20（受 limit 截断后 returned ≤ matched）
    day_boards = {board for board, series in by_board.items() if day in series}
    window_boards = set(by_board)
    hist_boards = {board for board, series in by_board.items() if len(series) >= 20}
    hist_in_day = hist_boards & day_boards
    rps_boards = set(rps20)

    out = []
    breadth_counts = {MEMBER_COMPLETE: 0, "mismatch": 0, "partial": 0, "unknown": 0}
    for board in rps_boards:
        series = by_board[board]
        r20 = rps20[board]
        amount_series = [v[1] for v in series.values() if v[1] is not None]
        amount_ratio = None
        if len(amount_series) >= 21:
            base = float(np.mean(amount_series[-21:-1]))
            amount_ratio = round(float(amount_series[-1] / base), 2) if base > 0 else None
        strong_days = 0
        for when in sorted(rps20_by_day, reverse=True):
            if when > day:
                continue
            if rps20_by_day[when].get(board, 0) >= 80:
                strong_days += 1
            else:
                break

        # 广度（依赖 sector_member）：只有 manifest 完整且与来源 Count 一致才可信。
        # 日线类指标（ret*/rps20/amount_ratio）不受 manifest 影响，照常返回。
        actual_rows = int(member_counts.get(board, 0))
        breadth_status, breadth_reason = _breadth_status(
            manifest is not None, (manifest or {}).get(board), actual_rows
        )
        trusted = breadth_status == MEMBER_COMPLETE
        pcts = [p for p in breadth.get(board, []) if p is not None]
        up_ratio = (
            round(sum(1 for p in pcts if p > 0) / len(pcts) * 100, 1)
            if trusted and pcts
            else None
        )
        top = sorted(core.get(board, []), reverse=True)[:5] if trusted else []

        prev5 = rps20_by_day.get(days[-6], {}).get(board) if len(days) >= 6 else None
        if r20 >= 90 and (prev5 is None or prev5 < 80):
            state_daily = "新晋强势"
        elif r20 >= 80 and strong_days >= 3:
            state_daily = "持续走强"
        elif r20 >= 60:
            state_daily = (
                "强势整理"
                if abs(ret[5].get(board, 0)) <= 3 and (amount_ratio or 1) < 1
                else "强度回落"
            )
        elif r20 <= 20:
            state_daily = "持续弱势"
        else:
            state_daily = "中性"
        state = state_daily if up_ratio is not None else "数据不足"
        if breadth_status == MEMBER_COMPLETE:
            breadth_counts[MEMBER_COMPLETE] += 1
        elif breadth_status == "mismatch":
            breadth_counts["mismatch"] += 1
        elif breadth_status == "partial":
            breadth_counts["partial"] += 1
        else:
            breadth_counts["unknown"] += 1
        out.append(
            {
                "sector_code": board,
                "name": board_names.get(board, board),
                "trade_date": str(day),
                "ret5": round(ret[5].get(board), 2) if board in ret[5] else None,
                "ret10": round(ret[10].get(board), 2) if board in ret[10] else None,
                "ret20": round(ret[20].get(board), 2) if board in ret[20] else None,
                "ret60": round(ret[60].get(board), 2) if board in ret[60] else None,
                "rps20": round(r20, 1),
                "amount_ratio20": amount_ratio,
                "up_ratio": up_ratio,
                "strong_days": strong_days,
                "state": state,
                # 只用板块日线得出的强弱（不受成分股 manifest 影响），
                # 这样「广度不可用」不会连板块日线层面的强弱一起被抹掉。
                "state_daily": state_daily,
                "breadth_status": breadth_status,
                "core": [{"code": code, "pct_chg": pct} for pct, code in top],
                "missing": [] if up_ratio is not None else [f"成分股广度不可用：{breadth_reason}"],
                "strategy": SECTOR_STATE_STRATEGY,
            }
        )
    out.sort(key=lambda row: (-(row["rps20"] or -1), row["sector_code"]))
    returned = out[:limit]
    return {
        "trade_date": str(day),
        "requested_date": str(requested),
        "actual_data_date": str(day),
        "fallback": fallback,
        "rows": returned,
        "coverage": {
            "days": len(days),
            "status": "ok" if data_status in {"ok", "fallback"} else data_status,
            "data_status": data_status,
            # --- 集合关系（P0.9-04）---
            "boards_in_day": len(day_boards),
            "boards_with_history": len(hist_in_day),
            "boards_with_rps20": len(rps_boards),
            "excluded_short_history": len(day_boards - hist_boards),
            "excluded_no_rps20": len(hist_in_day - rps_boards),
            "matched": len(out),
            "returned": len(returned),
            "truncated": len(out) - len(returned),
            "window_only_boards": len(window_boards - day_boards),
            # --- 兼容旧字段 ---
            "boards": len(window_boards),
            "with_members": len(member_counts),
            # --- manifest / 广度（P0.9-05）---
            "manifest_installed": manifest is not None,
            "manifest_boards": len(manifest or {}),
            "breadth_complete": breadth_counts[MEMBER_COMPLETE],
            "breadth_mismatch": breadth_counts["mismatch"],
            "breadth_partial": breadth_counts["partial"],
            "breadth_unknown": breadth_counts["unknown"],
            "note": (
                "板块日线指标（Ret/RPS/成交额）不依赖成分股；广度（上涨占比/核心股）"
                "只在 manifest 完整且与来源 Count 一致时展示。"
            ),
        },
        "strategy": SECTOR_STATE_STRATEGY,
    }


def data_health(session: Session, *, lookback_days: int = 90, limit: int = 20) -> dict:
    """研究库的数据时点健康度（P0.9-07，只给管理员诊断用）。

    回答两个问题：
    1. 哪些日期的 RPS **没有底层个股日线**（孤儿 RPS，不能用于研究结论）；
    2. 最近 `lookback_days` 个有行情的交易日里，哪些**有行情但没有 RPS**。

    两者都按「逐交易日」统计，不用 `MAX(trade_date)` 代表整段覆盖。
    """
    conn = session.connection()
    bars_latest = session.scalar(select(func.max(StockDaily.trade_date)))
    rps_latest = session.scalar(select(func.max(RpsDaily.trade_date)))

    orphan_count = int(
        conn.exec_driver_sql(
            "SELECT COUNT(*) FROM (SELECT trade_date FROM rps_daily GROUP BY trade_date) r"
            " WHERE NOT EXISTS (SELECT 1 FROM stock_daily d WHERE d.trade_date = r.trade_date)"
        ).scalar()
        or 0
    )
    orphan_dates = [
        {"trade_date": row[0], "rps_rows": row[1], "daily_rows": row[2]}
        for row in conn.exec_driver_sql(
            "SELECT r.trade_date, r.n,"
            " (SELECT COUNT(*) FROM stock_daily d WHERE d.trade_date = r.trade_date) AS bars"
            " FROM (SELECT trade_date, COUNT(*) AS n FROM rps_daily GROUP BY trade_date) r"
            " WHERE (SELECT COUNT(*) FROM stock_daily d WHERE d.trade_date = r.trade_date) = 0"
            " ORDER BY r.trade_date DESC LIMIT ?",
            (int(limit),),
        ).all()
    ]

    missing_rps_count = 0
    missing_rps_dates: list[str] = []
    if bars_latest is not None:
        start = conn.exec_driver_sql(
            "SELECT trade_date FROM trade_calendar WHERE trade_date <= ?"
            " ORDER BY trade_date DESC LIMIT 1 OFFSET ?",
            (bars_latest.isoformat(), int(lookback_days)),
        ).scalar()
        base_sql = (
            " FROM trade_calendar c WHERE c.trade_date <= ? AND c.trade_date >= ?"
            " AND EXISTS (SELECT 1 FROM stock_daily d WHERE d.trade_date = c.trade_date)"
            " AND NOT EXISTS (SELECT 1 FROM rps_daily r WHERE r.trade_date = c.trade_date)"
        )
        params = (bars_latest.isoformat(), start or "0000-00-00")
        missing_rps_count = int(
            conn.exec_driver_sql("SELECT COUNT(*)" + base_sql, params).scalar() or 0
        )
        missing_rps_dates = [
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT c.trade_date" + base_sql + " ORDER BY c.trade_date DESC LIMIT ?",
                (*params, int(limit)),
            ).all()
        ]

    return {
        "bars_latest": str(bars_latest) if bars_latest else None,
        "rps_latest": str(rps_latest) if rps_latest else None,
        # RPS 比行情新：10-21 的 RPS 没有 10-21 的底层行情时就是 True
        "rps_newer_than_bars": bool(bars_latest and rps_latest and rps_latest > bars_latest),
        "orphan_rps_dates_count": orphan_count,
        "orphan_rps_dates": orphan_dates,
        "missing_rps_dates_count": missing_rps_count,
        "missing_rps_dates": missing_rps_dates,
        "lookback_days": int(lookback_days),
    }
