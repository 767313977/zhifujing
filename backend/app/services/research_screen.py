"""P0.2 A 类研究指标：趋势 / 低位启动 / 板块状态。

只读本地库，复用：
- `services.patterns.build_bars`（经 `services.trend.load_bars`）的前复权 K 线；
- `rps_daily` 的 20/60/120 分位；
- `sector_daily` / `sector_member`；
- `limit_rules` 的涨跌幅与北交所判断。

所有指标只使用目标交易日及以前的数据；缺失值返回 None + `missing[]`，不填 0。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from datetime import date
from typing import Iterable

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import RpsDaily, SectorDaily, SectorMember, StockBasic, StockDaily, StockUniverse, TradeCalendar
from app.services import limit_rules, trend

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


# ---- 结果缓存（P0.3）----
#
# 低位启动是**全市场 250 日扫描**（5,261 只 ≈ 130 万行），冷查询约 7 秒。盘后研究是
# 按交易日看同一份结果，重复打开页面没有理由再算一遍。
#
# 缓存键必须同时包含：交易日、策略参数、**数据版本**（日线/RPS/板块的最新日期）。
# 回补或重采把数据推进一天后，键会变，不会读到旧结果。
#
# 只缓存文件库：单元测试用 `sqlite://` 内存库，各测试日期/参数相同，若也走缓存会
# 串味。TTL 10 分钟是兜底（同一天内的数据订正不会改 `max(trade_date)`）。
_CACHE: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
_CACHE_TTL_SECONDS = 600.0
_CACHE_MAX_ENTRIES = 16


def _cache_key(session: Session, name: str, day: date, params: tuple) -> tuple | None:
    url = str(session.get_bind().url)
    if url in ("sqlite://", "sqlite:///") or ":memory:" in url:
        return None
    version = (
        session.scalar(select(func.max(StockDaily.trade_date))),
        session.scalar(select(func.max(RpsDaily.trade_date))),
        session.scalar(select(func.max(SectorDaily.trade_date))),
    )
    return (url, name, str(day), version, params)


def _cached(session: Session, name: str, day: date, params: tuple, compute):
    key = _cache_key(session, name, day, params)
    if key is None:
        return compute()
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit is not None and now - hit[0] < _CACHE_TTL_SECONDS:
        _CACHE.move_to_end(key)
        return hit[1]
    value = compute()
    _CACHE[key] = (now, value)
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX_ENTRIES:
        _CACHE.popitem(last=False)
    return value


def clear_cache() -> None:
    """测试/脚本用：清空研究结果缓存。"""
    _CACHE.clear()


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
    return _cached(
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
    candidates = list(
        session.scalars(
            select(RpsDaily.code)
            .where(RpsDaily.trade_date == day, RpsDaily.rps60.is_not(None), RpsDaily.rps60 >= min_rps60)
            .order_by(RpsDaily.rps60.desc())
            .limit(max(limit * 8, 400))
        )
    )
    if not candidates:
        return {"trade_date": day, "rows": [], "coverage": {"candidates": 0}, "strategy": TREND_STRATEGY}
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
    return _cached(
        session,
        "low_start",
        day,
        params,
        lambda: _low_start_rows(
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
    codes = list(session.scalars(select(StockUniverse.code).limit(6000)))
    if not codes:
        return {"trade_date": day, "rows": [], "coverage": {"candidates": 0}, "strategy": LOW_START_STRATEGY}
    names = _names(session, day, codes)
    industries = _industries(session, codes)
    rps = _rps(session, day, codes)
    bars_by_code = trend.load_bars(session, codes, day)
    rows = []
    for code in codes:
        bars = bars_by_code.get(code)
        name = names.get(code)
        if bars is None or len(bars) < 200:
            continue
        if exclude_st and name and "ST" in name.upper():
            continue
        if exclude_bse and limit_rules.is_bse(code):
            continue
        close, high, low = bars.close, bars.high, bars.low
        window = min(250, len(close))
        lo = float(np.min(low[-window:]))
        hi = float(np.max(high[-window:]))
        pos = float(round((close[-1] - lo) / (hi - lo) * 100, 1)) if hi > lo else None
        if pos is None or pos > max_position:
            continue
        platform = min(40, len(close) - 21)
        if platform < min_platform_days:
            continue
        ph = float(np.max(high[-platform - 1 : -1]))
        pl = float(np.min(low[-platform - 1 : -1]))
        height = round((ph / pl - 1) * 100, 1) if pl > 0 else None
        if height is None or height > max_platform_height:
            continue
        # 波动收敛看的是**突破日之前**的平台期：把今天算进去会因突破当日的放量长阳
        # 抬高 vol20，反而把刚刚启动的票筛掉。
        vol20 = float(np.std(bars.pct_chg[-21:-1]))
        vol60 = float(np.std(bars.pct_chg[-61:-1]))
        contraction = round(vol20 / vol60, 2) if vol60 > 0 else 0.0
        if contraction > max_vol_contraction:
            continue
        amount_ratio = _amount_ratio(bars)
        ma20 = _ma(close, 20)
        s20 = _slope(close, 20)
        breakout = close[-1] > ph * 1.001
        rps_row = rps.get(code)
        rps20 = rps_row.rps20 if rps_row else None
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
                "state": state,
                "reasons": [
                    f"区间位置 {pos:.1f}%",
                    f"平台 {platform} 天、振幅 {height:.1f}%",
                    f"波动收敛 {contraction:.2f}",
                ],
                "missing": [],
                "strategy": LOW_START_STRATEGY,
            }
        )
    rows.sort(key=lambda row: (row["state"] != "突破确认", row["pos_pct"], -(row.get("amount_ratio20") or 0)))
    return {
        "trade_date": day,
        "rows": rows[:limit],
        "coverage": {"candidates": len(codes), "bars": len(bars_by_code)},
        "strategy": LOW_START_STRATEGY,
    }


def sector_states(
    session: Session,
    *,
    trade_date: date | None = None,
    taxonomy: str = "kph_selected",
    limit: int = 100,
) -> dict:
    requested = target_date(session, trade_date)
    params = (taxonomy, limit)
    return _cached(
        session,
        "sector_state",
        requested,
        params,
        lambda: _sector_states(session, requested, taxonomy=taxonomy, limit=limit),
    )


def _sector_states(
    session: Session,
    requested: date,
    *,
    taxonomy: str = "kph_selected",
    limit: int = 100,
) -> dict:
    available = session.scalar(
        select(func.max(SectorDaily.trade_date)).where(
            SectorDaily.taxonomy == taxonomy, SectorDaily.trade_date <= requested
        )
    )
    if available is None:
        return {
            "trade_date": str(requested),
            "rows": [],
            "coverage": {"days": 0, "status": "数据不足"},
            "strategy": SECTOR_STATE_STRATEGY,
        }
    day = available
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
            "rows": [],
            "coverage": {"days": len(days), "status": "数据不足"},
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
    out = []
    for board, series in by_board.items():
        r20 = rps20.get(board)
        if r20 is None:
            continue
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
        pcts = [p for p in breadth.get(board, []) if p is not None]
        up_ratio = round(sum(1 for p in pcts if p > 0) / len(pcts) * 100, 1) if pcts else None
        prev5 = rps20_by_day.get(days[-6], {}).get(board) if len(days) >= 6 else None
        if r20 >= 90 and (prev5 is None or prev5 < 80):
            state = "新晋强势"
        elif r20 >= 80 and strong_days >= 3:
            state = "持续走强"
        elif r20 >= 60:
            state = (
                "强势整理"
                if abs(ret[5].get(board, 0)) <= 3 and (amount_ratio or 1) < 1
                else "强度回落"
            )
        elif r20 <= 20:
            state = "持续弱势"
        else:
            state = "中性"
        if up_ratio is None:
            state = "数据不足"
        top = sorted(core.get(board, []), reverse=True)[:5]
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
                "core": [{"code": code, "pct_chg": pct} for pct, code in top],
                "missing": [] if up_ratio is not None else ["sector_member 当日缺失"],
                "strategy": SECTOR_STATE_STRATEGY,
            }
        )
    out.sort(key=lambda row: (-(row["rps20"] or -1), row["sector_code"]))
    return {
        "trade_date": str(day),
        "requested_date": str(requested),
        "rows": out[:limit],
        "coverage": {
            "days": len(days),
            "boards": len(by_board),
            "with_members": len(breadth),
            "note": "sector_member 仅覆盖已回补板块；未覆盖板块的广度显示数据不足",
        },
        "strategy": SECTOR_STATE_STRATEGY,
    }
