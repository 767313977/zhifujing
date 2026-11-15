"""Trend Engine M6：统一历史回测与验证 CLI（**只读**，不写任何数据库）。

复用 `app.services.research_backtest` 的成交模拟内核（T+1 开盘、停牌顺延、涨停不可买、
退出跌停标记、成本、基准），本脚本只负责：

1. 从只读 SQLite 生成 Model A / Model B 的**历史信号**（PIT：只用当日及以前数据）；
2. 按参数变体重跑排序与筛选（分数由已存的分项重算，不重复扫描）；
3. 输出训练 / 验证 / 样本外、Walk-forward、参数敏感性、市场环境分层、尾部依赖；
4. 生成 JSON + Markdown 报告。

用法：

    python scripts/backtest_trend_engine.py --db backend/data/b1_research.db \
        --start 2025-10-09 --end 2026-10-09 --days 240 --top 20 \
        --cost-bps 15 --report-dir reports/trend_engine/m6

边界：
- `history.db` 只有 `stock_daily`，没有 RPS / 板块 / 指数 → 只能跑 A/B 的受限版本，
  且必须标注「现存股票样本」；本脚本不跨库拼接样本。
- 数据不足时对应指标写 `null` 并标 `UNKNOWN`，不估算、不外推。
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
BACKEND = ROOT / "backend"
for path in (str(BACKEND), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.models import (  # noqa: E402
    BreadthDaily,
    IndexDaily,
    RpsDaily,
    SectorDaily,
    SectorMember,
    StockDaily,
    TradeCalendar,
)
from app.services import limit_rules, research_backtest as rb  # noqa: E402
from app.services.patterns import build_bars_from_arrays, is_st  # noqa: E402
from app.services.trend_engine import (  # noqa: E402
    breakout_model,
    config as te_config,
    continuation_model,
    indicators,
    market_regime,
    risk_engine,
    scoring,
    sector_strength,
)

logger = logging.getLogger("trend-backtest")


# ---------------------------------------------------------------- 只读会话


def open_readonly(db_path: Path) -> Session:
    """`mode=ro` + `query_only`，从连接层保证不会写库。"""
    uri = f"sqlite:///file:{db_path.as_posix()}?mode=ro&uri=true"
    engine = create_engine(uri, future=True)
    connection = engine.connect()
    connection.exec_driver_sql("PRAGMA query_only=ON")
    return Session(bind=connection)


# ---------------------------------------------------------------- 数据矩阵


def has_table(session: Session, name: str) -> bool:
    """`history.db` 这类精简库没有 RPS / 板块 / 指数表，必须显式降级而不是报错。"""
    return bool(
        session.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"),
            {"name": name},
        ).first()
    )


class RpsMatrix:
    """(交易日, 代码) 的 RPS 分位矩阵，避免逐票逐日查库。"""

    def __init__(self, calendar: list[date], codes: list[str]) -> None:
        self.calendar = calendar
        self.codes = codes
        self.date_pos = {day: index for index, day in enumerate(calendar)}
        self.code_pos = {code: index for index, code in enumerate(codes)}
        shape = (len(calendar), len(codes))
        self.rps20 = np.full(shape, np.nan)
        self.rps60 = np.full(shape, np.nan)
        self.rps120 = np.full(shape, np.nan)
        self.rps250 = np.full(shape, np.nan)

    def values(self, day: date, code: str) -> dict[str, float | None] | None:
        i = self.date_pos.get(day)
        j = self.code_pos.get(code)
        if i is None or j is None:
            return None
        out = {
            "rps20": self.rps20[i, j],
            "rps60": self.rps60[i, j],
            "rps120": self.rps120[i, j],
            "rps250": self.rps250[i, j],
        }
        if all(not np.isfinite(value) for value in out.values()):
            return None
        return {
            key: (None if not np.isfinite(value) else float(value))
            for key, value in out.items()
        }


def load_rps_matrix(session: Session, calendar: list[date], codes: list[str]) -> RpsMatrix:
    matrix = RpsMatrix(calendar, codes)
    if not calendar or not codes or not has_table(session, "rps_daily"):
        return matrix
    start, end = calendar[0], calendar[-1]
    rows = session.execute(
        select(
            RpsDaily.trade_date,
            RpsDaily.code,
            RpsDaily.rps20,
            RpsDaily.rps60,
            RpsDaily.rps120,
            RpsDaily.rps250,
        )
        .where(RpsDaily.trade_date >= start, RpsDaily.trade_date <= end)
        .execution_options(yield_per=20000)
    )
    for when, code, r20, r60, r120, r250 in rows:
        i = matrix.date_pos.get(when)
        j = matrix.code_pos.get(code)
        if i is None or j is None:
            continue
        for array, value in (
            (matrix.rps20, r20),
            (matrix.rps60, r60),
            (matrix.rps120, r120),
            (matrix.rps250, r250),
        ):
            if value is not None:
                array[i, j] = float(value)
    return matrix


def _rolling_max_prev(values: np.ndarray, window: int) -> np.ndarray:
    """index i 的值 = max(values[i-window:i])（**不含当日**）。"""
    size = values.size
    out = np.full(size, np.nan)
    if size < window + 1:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(values, window + 1)
    out[window:] = windows[:, :window].max(axis=1)
    return out


@dataclass
class CodeSeries:
    code: str
    bars: object
    ma20: np.ndarray
    ma60: np.ndarray
    trend_days: np.ndarray
    amount_mean20_prev: np.ndarray
    prior_high: dict[int, np.ndarray]


def build_series(matrix: rb.BarMatrix, window: int) -> dict[str, CodeSeries]:
    """把矩阵里每只票的可用行压成前复权 `Bars`，并预算候选前置条件。"""
    out: dict[str, CodeSeries] = {}
    calendar = matrix.calendar
    for j, code in enumerate(matrix.codes):
        mask = (
            matrix.present[:, j]
            & np.isfinite(matrix.open[:, j])
            & np.isfinite(matrix.high[:, j])
            & np.isfinite(matrix.low[:, j])
            & np.isfinite(matrix.close[:, j])
            & np.isfinite(matrix.amount[:, j])
            & np.isfinite(matrix.volume[:, j])
        )
        positions = np.nonzero(mask)[0]
        if positions.size < 130:
            continue
        dates = [calendar[pos] for pos in positions]
        pct = np.nan_to_num(matrix.pct[positions, j], nan=0.0)
        try:
            bars = build_bars_from_arrays(
                dates=dates,
                open_=matrix.open[positions, j],
                high=matrix.high[positions, j],
                low=matrix.low[positions, j],
                close=matrix.close[positions, j],
                volume=matrix.volume[positions, j],
                amount=matrix.amount[positions, j],
                pct_chg=pct,
                code=code,
            )
        except ValueError:
            continue
        ma20 = indicators.rolling_mean(bars.close, 20)
        ma60 = indicators.rolling_mean(bars.close, 60)
        # 连续站上 MA20 的天数（与 `indicators.trend_days_above` 同一判据），用于廉价前置筛
        trend_days = np.zeros(len(bars), dtype=int)
        above = np.isfinite(ma20) & (bars.close > ma20)
        for index in range(len(bars)):
            trend_days[index] = trend_days[index - 1] + 1 if index and above[index] else (1 if above[index] else 0)
        amount_mean = indicators.rolling_mean(bars.amount, 20)
        amount_prev = np.concatenate(([np.nan], amount_mean[:-1]))
        prior_high = {
            span: _rolling_max_prev(bars.high, span) for span in (20, 30, 40, 60)
        }
        out[code] = CodeSeries(
            code=code,
            bars=bars,
            ma20=ma20,
            ma60=ma60,
            trend_days=trend_days,
            amount_mean20_prev=amount_prev,
            prior_high=prior_high,
        )
    return out


def slice_bars(bars, index: int):
    """取 [0, index] 的切片（视图），供模型按历史日判定。"""
    return type(bars)(
        dates=bars.dates[: index + 1],
        open=bars.open[: index + 1],
        high=bars.high[: index + 1],
        low=bars.low[: index + 1],
        close=bars.close[: index + 1],
        volume=bars.volume[: index + 1],
        amount=bars.amount[: index + 1],
        pct_chg=bars.pct_chg[: index + 1],
        code=bars.code,
    )


# ---------------------------------------------------------------- 扫描


def _features(result, rps_values, sector_score) -> dict:
    """把模型结果压成归因用特征（只存数值/短标签，不存对象）。"""
    structure = getattr(result, "structure", None)
    mom = getattr(result, "momentum", None)
    volume = getattr(result, "volume", None)
    pullback = getattr(result, "pullback", None)
    ret = (mom.ret if mom else {}) or {}
    rps = (mom.rps if mom else {}) or {}
    ma = (structure.ma if structure else {}) or {}
    slope = (structure.slope if structure else {}) or {}
    return {
        "ret5": ret.get(5),
        "ret10": ret.get(10),
        "ret20": ret.get(20),
        "ret60": ret.get(60),
        "rps20": rps.get("rps20"),
        "rps60": rps.get("rps60"),
        "rps120": rps.get("rps120"),
        "rps250": rps.get("rps250"),
        "ma20": ma.get(20),
        "ma60": ma.get(60),
        "ma120": ma.get(120),
        "slope20": slope.get(20),
        "slope60": slope.get(60),
        "distance_ma20": getattr(structure, "distance_ma20_pct", None),
        "trend_days": getattr(structure, "trend_days", None),
        "convergence": getattr(structure, "convergence_pct", None),
        "alignment": getattr(structure, "alignment", None),
        "amount_ratio20": getattr(volume, "amount_ratio20", None),
        "vol_ratio5": getattr(volume, "vol_ratio5", None),
        "up_down_vol": getattr(volume, "up_down_volume_ratio20", None),
        "close_position": getattr(volume, "close_position", None),
        "divergence": getattr(volume, "divergence", None),
        "sector_score": sector_score,
        "platform_width": getattr(result, "platform_width_pct", None),
        "breakout_pct": getattr(result, "breakout_pct", None),
        "pullback_depth": getattr(pullback, "depth_pct", None),
        "pullback_atr": getattr(pullback, "atr_multiple", None),
        "pullback_shrink": getattr(pullback, "volume_shrink_ratio", None),
        "trend_strength": getattr(result, "trend_strength", None),
    }


def scan(
    session: Session,
    matrix: rb.BarMatrix,
    rps_matrix: RpsMatrix,
    series: dict[str, CodeSeries],
    scan_days: set[date],
    *,
    sector_map: dict[date, dict[str, float]] | None = None,
    config_a: te_config.ModelAConfig | None = None,
    config_b: te_config.ModelBConfig | None = None,
    collect_risk: bool = False,
    collect_features: bool = False,
) -> dict[str, list[dict]]:
    """逐票逐日判定，返回 `{model: [{day, code, parts, risk_state}]}`。

    只保留 `eligible=True` 的记录；分项存下来，后续换权重不必重扫。
    """
    config_a = config_a or te_config.ModelAConfig()
    config_b = config_b or te_config.ModelBConfig()
    calendar = matrix.calendar
    results: dict[str, list[dict]] = {"A": [], "B": []}

    for code, item in series.items():
        bars = item.bars
        if limit_rules.is_bse(code):
            continue
        for i, day in enumerate(bars.dates):
            if day not in scan_days or i < 129:
                continue
            name = matrix.name[matrix.date_pos[day], matrix.code_pos[code]]
            if is_st(name):
                continue
            rps_values = rps_matrix.values(day, code)
            sector_score = None
            if sector_map is not None:
                sector_score = sector_map.get(day, {}).get(code)

            # ---- Model A 前置条件（廉价向量化筛）----
            if (
                np.isfinite(item.ma20[i])
                and np.isfinite(item.prior_high[30][i])
                and np.isfinite(item.amount_mean20_prev[i])
                and bars.close[i] > item.ma20[i]
                and bars.close[i] > item.prior_high[30][i]
                and bars.amount[i] > item.amount_mean20_prev[i] * 1.1
            ):
                sliced = slice_bars(bars, i)
                result = breakout_model.evaluate(
                    sliced,
                    code=code,
                    name=name,
                    day=day,
                    rps_values=rps_values,
                    sector_score=sector_score,
                    trade_days=calendar[: matrix.date_pos[day] + 1],
                    config=config_a,
                )
                if result.eligible:
                    record = {
                        "day": day,
                        "code": code,
                        "model": "A",
                        "parts": dict(result.parts),
                    }
                    if collect_risk:
                        record["risk_state"] = risk_engine.evaluate(
                            sliced, model="A", include_since=False
                        ).state
                    if collect_features:
                        record["features"] = _features(result, rps_values, sector_score)
                    results["A"].append(record)

            # ---- Model B 前置条件 ----
            if (
                np.isfinite(item.ma20[i])
                and np.isfinite(item.ma60[i])
                and item.ma20[i] > item.ma60[i]
                and item.trend_days[i] >= config_b.min_trend_days
                and bars.close[i] > item.ma20[i] * 0.97
                and bars.close[i] < item.ma20[i] * 1.15
                and i >= 60
                and bars.close[i] > bars.close[i - 60]
            ):
                sliced = slice_bars(bars, i)
                result = continuation_model.evaluate(
                    sliced,
                    code=code,
                    name=name,
                    day=day,
                    rps_values=rps_values,
                    sector_score=sector_score,
                    config=config_b,
                )
                if result.eligible:
                    record = {
                        "day": day,
                        "code": code,
                        "model": "B",
                        "parts": dict(result.parts),
                    }
                    if collect_risk:
                        record["risk_state"] = risk_engine.evaluate(
                            sliced, model="B", include_since=False
                        ).state
                    if collect_features:
                        record["features"] = _features(result, rps_values, sector_score)
                    results["B"].append(record)
    return results


# ---------------------------------------------------------------- 信号与评分


def score_of(parts: dict, config, rps_points: float, sector_points: float) -> float:
    weights = te_config.effective_weights(config.base_weights, rps_points, sector_points)
    total, _missing = scoring.weighted_total(parts, weights)
    return total


def build_signals(
    records: list[dict],
    *,
    configs: dict,
    rps_points: float,
    sector_points: float,
    top: int,
    min_score: float,
    start: date | None = None,
    end: date | None = None,
) -> list[rb.Signal]:
    grouped: dict[date, dict[str, float]] = defaultdict(dict)
    for record in records:
        day = record["day"]
        if start and day < start:
            continue
        if end and day > end:
            continue
        cfg = configs.get(record.get("model", "A"), configs.get("A"))
        if cfg is None:
            continue
        score = score_of(record["parts"], cfg, rps_points, sector_points)
        if score < min_score:
            continue
        code = record["code"]
        # 组合模式下同一只票可能同时被 A/B 选中：同日同票只能算一次，取较高分
        if code not in grouped[day] or score > grouped[day][code]:
            grouped[day][code] = score
    signals: list[rb.Signal] = []
    for day in sorted(grouped):
        picked = sorted(grouped[day].items(), key=lambda item: (-item[1], item[0]))[:top]
        for code, score in picked:
            signals.append(rb.Signal(code=code, signal_date=day, meta={"score": score}))
    return signals


# ---------------------------------------------------------------- 统计


def _execution(samples, horizon: int):
    return [
        sample
        for sample in samples
        if sample.horizon == horizon and sample.status == "ok" and sample.gross is not None
    ]


def _profit_factor(values: list[float]) -> float | None:
    gains = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    if losses <= 0:
        return None
    return round(gains / losses, 2)


def _max_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    cumulative = np.cumsum(values)
    peak = np.maximum.accumulate(cumulative)
    return round(float((cumulative - peak).min()), 2)


def _max_consecutive_losses(values: list[float]) -> int:
    worst = current = 0
    for value in values:
        if value <= 0:
            current += 1
            worst = max(worst, current)
        else:
            current = 0
    return worst


def _tail(samples) -> dict:
    values = sorted((sample.gross for sample in samples), reverse=True)
    if not values:
        return {"top5_share": None, "mean_excluding_top5pct": None}
    count = max(1, int(len(values) * 0.05))
    total = sum(values)
    top = sum(values[:count])
    return {
        "top5_count": count,
        "top5_share": round(top / total, 3) if total > 0 else None,
        "total_gross": round(total, 2),
        "mean_excluding_top5pct": round(float(np.mean(values[count:])), 2) if len(values) > count else None,
    }


def _bootstrap(samples, horizon: int, iterations: int, seed: int) -> dict:
    grouped: dict[date, list[float]] = defaultdict(list)
    for sample in _execution(samples, horizon):
        if sample.excess is not None:
            grouped[sample.signal_date].append(sample.excess)
    days = sorted(grouped)
    if len(days) < 2:
        return {"days": len(days), "mean": None, "ci_low": None, "ci_high": None, "note": "UNKNOWN: 有效日期不足"}
    rng = random.Random(seed)
    means = []
    for _ in range(iterations):
        picked = [rng.choice(days) for _ in days]
        values = [value for day in picked for value in grouped[day]]
        if values:
            means.append(float(np.mean(values)))
    means.sort()
    return {
        "days": len(days),
        "iterations": iterations,
        "mean": round(float(np.mean(means)), 3),
        "ci_low": round(means[int(len(means) * 0.025)], 3),
        "ci_high": round(means[int(len(means) * 0.975)], 3),
    }


def summarize(
    result: rb.BacktestResult,
    *,
    horizons: tuple[int, ...],
    regime_map: dict[date, str],
    bootstrap: int,
    seed: int,
) -> dict:
    payload = {
        "strategy": result.strategy,
        "params": result.params,
        "start": str(result.start) if result.start else None,
        "end": str(result.end) if result.end else None,
        "entry_mode": result.entry_mode,
        "cost_bps": result.cost_bps,
        "signals": result.data_coverage.get("signal_count"),
        "data_coverage": result.data_coverage,
        "warnings": result.warnings,
        "horizons": {},
        "regimes": {},
        "bootstrap": {},
    }
    for horizon in horizons:
        stat = result.horizons.get(horizon)
        if stat is None:
            continue
        execution = _execution(result.samples, horizon)
        gross = [sample.gross for sample in execution]
        holding = [
            (sample.exit_date - sample.entry_date).days
            for sample in execution
            if sample.entry_date and sample.exit_date
        ]
        payload["horizons"][horizon] = {
            "observation_samples": stat.observation_samples,
            "execution_samples": stat.execution_samples,
            "counts": stat.counts,
            "mean": stat.mean,
            "median": stat.median,
            "pos_pct": stat.pos_pct,
            "excess_mean": stat.excess_mean,
            "excess_median": stat.excess_median,
            "beat_pct": stat.beat_pct,
            "net_mean": stat.net_mean,
            "mae_mean": stat.mae_mean,
            "distribution": stat.distribution,
            "profit_factor": _profit_factor(gross),
            "max_drawdown_points": _max_drawdown(gross),
            "max_consecutive_losses": _max_consecutive_losses(gross),
            "holding_days_mean": round(float(np.mean(holding)), 1) if holding else None,
            "tail": _tail(execution),
        }
        payload["bootstrap"][horizon] = _bootstrap(result.samples, horizon, bootstrap, seed)

        buckets: dict[str, list] = defaultdict(list)
        for sample in execution:
            buckets[regime_map.get(sample.signal_date, "UNKNOWN")].append(sample)
        payload["regimes"][horizon] = {
            name: {
                "samples": len(items),
                "mean": round(float(np.mean([s.gross for s in items])), 2) if items else None,
                "excess_mean": round(
                    float(np.mean([s.excess for s in items if s.excess is not None])), 2
                )
                if any(s.excess is not None for s in items)
                else None,
                "pos_pct": round(
                    float(np.mean([1 if s.gross > 0 else 0 for s in items]) * 100), 1
                )
                if items
                else None,
            }
            for name, items in buckets.items()
        }
    return payload


# ---------------------------------------------------------------- 市场环境


def build_regime_map(session: Session, calendar: list[date], index_code: str) -> dict[date, str]:
    """指数口径的环境分类（breadth 为空时降级为「仅指数」，报告中标注）。"""
    if not has_table(session, "index_daily"):
        return {}
    rows = session.execute(
        select(IndexDaily.trade_date, IndexDaily.close)
        .where(IndexDaily.code == index_code)
        .order_by(IndexDaily.trade_date)
    ).all()
    series = [(when, float(close)) for when, close in rows if close is not None]
    closes = np.asarray([value for _when, value in series]) if series else np.asarray([])
    ma20 = indicators.rolling_mean(closes, 20) if closes.size else np.asarray([])
    ma60 = indicators.rolling_mean(closes, 60) if closes.size else np.asarray([])
    out: dict[date, str] = {}
    for index, (day, close) in enumerate(series):
        if day not in calendar:
            continue
        inputs = {
            "close": close,
            "ma20": float(ma20[index]) if np.isfinite(ma20[index]) else None,
            "ma60": float(ma60[index]) if np.isfinite(ma60[index]) else None,
            "ma20_slope": indicators.slope_pct(closes[: index + 1], 20, 5),
            "ma60_slope": indicators.slope_pct(closes[: index + 1], 60, 5),
        }
        regime, _reasons = market_regime.classify(inputs)
        out[day] = regime if regime != market_regime.UNKNOWN else "UNKNOWN"
    return out


# ---------------------------------------------------------------- 板块映射


def build_sector_map(
    session: Session, days: list[date], taxonomy: str
) -> dict[date, dict[str, float]]:
    """`{交易日: {个股: 所属最强板块 RPS20}}`。只在有 `sector_member` 的日期可用。"""
    out: dict[date, dict[str, float]] = {}
    if not has_table(session, "sector_member"):
        return out
    for day in days:
        has_members = session.scalar(
            select(func.count()).select_from(SectorMember).where(SectorMember.trade_date == day)
        )
        if not has_members:
            continue
        rps = sector_strength.evaluate(
            session, trade_date=day, taxonomy=taxonomy, limit=1000
        )
        board_score = {
            row["sector_code"]: row["rps20"]
            for row in rps.rows
            if row.get("rps20") is not None
        }
        if not board_score:
            continue
        rows = session.execute(
            select(SectorMember.code, SectorMember.sector_code).where(
                SectorMember.trade_date == day
            )
        ).all()
        mapping: dict[str, float] = {}
        for code, board in rows:
            value = board_score.get(board)
            if value is None:
                continue
            if code not in mapping or value > mapping[code]:
                mapping[code] = value
        out[day] = mapping
    return out


# ---------------------------------------------------------------- 主流程


def _split(days: list[date], train: float, valid: float) -> tuple[list[date], list[date], list[date]]:
    size = len(days)
    a = int(size * train)
    b = int(size * (train + valid))
    return days[:a], days[a:b], days[b:]


def _run(
    session,
    records,
    *,
    label,
    config,
    rps_points,
    sector_points,
    top,
    min_score,
    start,
    end,
    horizons,
    cost_bps,
    matrix,
    benchmark,
    regime_map,
    bootstrap,
    seed,
) -> dict:
    signals = build_signals(
        records,
        configs={"A": config, "B": config},
        rps_points=rps_points,
        sector_points=sector_points,
        top=top,
        min_score=min_score,
        start=start,
        end=end,
    )
    result = rb.run_signals(
        session,
        signals,
        strategy=label,
        params={
            "top": top,
            "min_score": min_score,
            "rps_points": rps_points,
            "sector_points": sector_points,
        },
        horizons=horizons,
        entry_mode="next_open",
        cost_bps=cost_bps,
        matrix=matrix,
        benchmark=benchmark,
    )
    return summarize(
        result, horizons=horizons, regime_map=regime_map, bootstrap=bootstrap, seed=seed
    )


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _int_keys(payload):
    """JSON 里 `horizons` 的键会变成字符串 "20"；渲染前转回 int，避免表格全空。"""
    if isinstance(payload, dict):
        out = {}
        for key, value in payload.items():
            new_key = int(key) if isinstance(key, str) and key.isdigit() else key
            out[new_key] = _int_keys(value)
        return out
    if isinstance(payload, list):
        return [_int_keys(item) for item in payload]
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Trend Engine M6 统一回测（只读）")
    parser.add_argument("--db", required=True)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--days", type=int, default=240, help="扫描窗口（交易日）")
    parser.add_argument("--warmup", type=int, default=140, help="矩阵预热交易日")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--min-score", type=float, default=60.0)
    parser.add_argument("--rps-points", type=float, default=3.0)
    parser.add_argument("--sector-points", type=float, default=0.0)
    parser.add_argument("--taxonomy", default="kph_selected")
    parser.add_argument("--cost-bps", type=float, default=15.0)
    parser.add_argument("--horizons", default="5,10,20,40,60")
    parser.add_argument("--bootstrap", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--train", type=float, default=0.6)
    parser.add_argument("--valid", type=float, default=0.2)
    parser.add_argument("--report-dir", default="reports/trend_engine/m6")
    parser.add_argument("--models", default="A,B")
    parser.add_argument("--collect-sector", action="store_true")
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="不重跑回测，只从 m6_summary.json 重新生成 Markdown 报告",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    report_dir = Path(args.report_dir)
    if args.render_only:
        summary = report_dir / "m6_summary.json"
        if not summary.exists():
            raise SystemExit(f"缺少 {summary}")
        report = _int_keys(json.loads(summary.read_text(encoding="utf-8")))
        _write_markdown(report_dir / "m6_report.md", report)
        print(f"报告已重新生成：{report_dir / 'm6_report.md'}")
        return 0

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        raise SystemExit(f"数据库不存在：{db_path}")
    session = open_readonly(db_path)
    horizons = tuple(sorted({int(item) for item in args.horizons.split(",") if item.strip()}))

    db_end = session.scalar(select(func.max(StockDaily.trade_date)))
    if db_end is None:
        raise SystemExit("库里没有日线")
    end = args.end or db_end
    calendar = (
        list(
            session.scalars(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date <= end)
                .order_by(TradeCalendar.trade_date)
            )
        )
        if has_table(session, "trade_calendar")
        else []
    )
    if not calendar:
        # history.db 没有交易日历 → 退化到行情日期并集（报告必须标注）
        calendar = sorted(
            set(session.scalars(select(StockDaily.trade_date).where(StockDaily.trade_date <= end)))
        )
        logger.warning("没有 trade_calendar，退化到行情日期并集")
    scan_days = calendar[-args.days :]
    start = args.start or scan_days[0]
    scan_days = [day for day in scan_days if day >= start]
    if not scan_days:
        raise SystemExit("扫描窗口为空")
    matrix_start = calendar[max(0, calendar.index(scan_days[0]) - args.warmup)]

    logger.info("加载矩阵 %s ~ %s", matrix_start, end)
    matrix = rb.load_matrix(session, matrix_start, end)
    rps_matrix = load_rps_matrix(session, matrix.calendar, matrix.codes)
    logger.info("构建前复权序列：%d 只", len(matrix.codes))
    series = build_series(matrix, 60)
    logger.info("可用序列：%d 只", len(series))

    sector_map = None
    if args.collect_sector:
        logger.info("加载板块映射…")
        sector_map = build_sector_map(session, scan_days, args.taxonomy)

    logger.info("扫描信号…")
    records = scan(
        session,
        matrix,
        rps_matrix,
        series,
        set(scan_days),
        sector_map=sector_map,
    )
    logger.info("信号：A=%d B=%d", len(records["A"]), len(records["B"]))

    regime_map = build_regime_map(session, matrix.calendar, "000001.SH")
    benchmark = rb.Benchmark(session, matrix, "market")
    config_a = te_config.ModelAConfig(rps_points=args.rps_points, sector_points=args.sector_points)
    config_b = te_config.ModelBConfig(rps_points=args.rps_points, sector_points=args.sector_points)

    train_days, valid_days, test_days = _split(scan_days, args.train, args.valid)
    splits = {"train": train_days, "validation": valid_days, "test": test_days}
    models = [item.strip().upper() for item in args.models.split(",") if item.strip()]

    report: dict = {
        "db": str(db_path),
        "data_end": str(db_end),
        "scan_start": str(scan_days[0]),
        "scan_end": str(scan_days[-1]),
        "scan_days": len(scan_days),
        "cost_bps": args.cost_bps,
        "horizons": list(horizons),
        "splits": {name: [str(days[0]), str(days[-1])] if days else None for name, days in splits.items()},
        "models": {},
        "sensitivity": {},
        "walk_forward": {},
        "risk_engine": {"status": "NOT_VALIDATED", "reason": "统一内核只支持固定持有期；风险退出需扩展成交模拟"},
    }

    def run(label, cfg, model_key, start_day, end_day, **override):
        params = {
            "rps_points": args.rps_points,
            "sector_points": args.sector_points,
            "top": args.top,
            "min_score": args.min_score,
        }
        params.update(override)
        return _run(
            session,
            records[model_key],
            label=label,
            config=cfg,
            rps_points=params["rps_points"],
            sector_points=params["sector_points"],
            top=params["top"],
            min_score=params["min_score"],
            start=start_day,
            end=end_day,
            horizons=horizons,
            cost_bps=args.cost_bps,
            matrix=matrix,
            benchmark=benchmark,
            regime_map=regime_map,
            bootstrap=args.bootstrap,
            seed=args.seed,
        )

    combined_records = records["A"] + records["B"]

    for model_key, cfg, label in (
        ("A", config_a, "ModelA"),
        ("B", config_b, "ModelB"),
    ):
        if model_key not in models:
            continue
        payload = {
            "full": run(f"{label}-full", cfg, model_key, scan_days[0], scan_days[-1]),
            "splits": {
                name: (
                    run(f"{label}-{name}", cfg, model_key, days[0], days[-1])
                    if days
                    else None
                )
                for name, days in splits.items()
            },
        }
        report["models"][model_key] = payload
        _write(report_dir / f"m6_model_{model_key.lower()}.json", payload)

    if "A" in models and "B" in models:
        # 组合信号 = A ∪ B（同日同票只算一次），两个模型各用自己的权重
        combined_signals = build_signals(
            combined_records,
            configs={"A": config_a, "B": config_b},
            rps_points=args.rps_points,
            sector_points=args.sector_points,
            top=args.top,
            min_score=args.min_score,
            start=scan_days[0],
            end=scan_days[-1],
        )
        result = rb.run_signals(
            session,
            combined_signals,
            strategy="Combined-full",
            params={"top": args.top, "min_score": args.min_score},
            horizons=horizons,
            entry_mode="next_open",
            cost_bps=args.cost_bps,
            matrix=matrix,
            benchmark=benchmark,
        )
        report["models"]["combined"] = summarize(
            result, horizons=horizons, regime_map=regime_map, bootstrap=args.bootstrap, seed=args.seed
        )
        _write(report_dir / "m6_combined.json", report["models"]["combined"])

    # ---- 参数敏感性：RPS / 板块 / 成本 / Top / 阈值 ----
    sensitivity = {}
    for label, cfg, key in (("A", config_a, "A"), ("B", config_b, "B")):
        if key not in models:
            continue
        node = {}
        node["rps_0"] = run(f"{label}-rps0", cfg, key, scan_days[0], scan_days[-1], rps_points=0.0)
        node["rps_5"] = run(f"{label}-rps5", cfg, key, scan_days[0], scan_days[-1], rps_points=5.0)
        node["top10"] = run(f"{label}-top10", cfg, key, scan_days[0], scan_days[-1], top=10)
        node["top30"] = run(f"{label}-top30", cfg, key, scan_days[0], scan_days[-1], top=30)
        node["score50"] = run(f"{label}-score50", cfg, key, scan_days[0], scan_days[-1], min_score=50.0)
        node["score70"] = run(f"{label}-score70", cfg, key, scan_days[0], scan_days[-1], min_score=70.0)
        if sector_map:
            sector_days = sorted(sector_map)
            # 板块数据只有 60 天：只在同一窗口内做「关闭 vs 开启」对照，避免缺数据日拉低分数
            node["sector_window"] = [str(sector_days[0]), str(sector_days[-1])]
            node["baseline_same_window"] = run(
                f"{label}-sector-baseline", cfg, key, sector_days[0], sector_days[-1]
            )
            node["sector_5"] = run(
                f"{label}-sector5", cfg, key, sector_days[0], sector_days[-1],
                sector_points=5.0,
            )
        else:
            node["sector_5"] = {"status": "UNKNOWN", "reason": "未采集板块映射（--collect-sector 未开启）"}
        sensitivity[key] = node
    report["sensitivity"] = sensitivity
    _write(report_dir / "m6_parameter_sensitivity.json", sensitivity)

    # ---- Walk-forward：用前一块选参数，在下一块评估 ----
    wf = {}
    blocks = [splits["train"], splits["validation"], splits["test"]]
    for model_key, cfg, label in (("A", config_a, "ModelA"), ("B", config_b, "ModelB")):
        if model_key not in models:
            continue
        folds = []
        for index in range(1, len(blocks)):
            train_block = blocks[index - 1]
            test_block = blocks[index]
            if not train_block or not test_block:
                continue
            grid = [(20, 60.0), (20, 70.0), (10, 60.0)]
            scored = []
            for top, minimum in grid:
                candidate = run(
                    f"{label}-wf-pick", cfg, model_key, train_block[0], train_block[-1],
                    top=top, min_score=minimum,
                )
                stats = candidate["horizons"].get(20) or {}
                scored.append((stats.get("excess_mean"), top, minimum))
            scored = [item for item in scored if item[0] is not None]
            if not scored:
                folds.append({"status": "UNKNOWN", "reason": "训练块没有可成交样本"})
                continue
            best = max(scored, key=lambda item: item[0])
            tested = run(
                f"{label}-wf-test", cfg, model_key, test_block[0], test_block[-1],
                top=best[1], min_score=best[2],
            )
            folds.append(
                {
                    "train_block": [str(train_block[0]), str(train_block[-1])],
                    "test_block": [str(test_block[0]), str(test_block[-1])],
                    "selected": {"top": best[1], "min_score": best[2], "train_excess_20d": best[0]},
                    "test": tested["horizons"].get(20),
                }
            )
        wf[model_key] = folds
    report["walk_forward"] = wf
    _write(report_dir / "m6_walk_forward.json", wf)

    _write(report_dir / "m6_regime.json", {
        model: payload.get("full", {}).get("regimes", {})
        for model, payload in report["models"].items()
    })
    _write(report_dir / "m6_tail.json", {
        model: {
            horizon: (stats.get("tail") or {})
            for horizon, stats in (payload.get("horizons") or {}).items()
        }
        for model, payload in report["models"].items()
    })

    quality = {
        "db": str(db_path),
        "stock_daily_end": str(db_end),
        "rps_days": (
            session.scalar(select(func.count(func.distinct(RpsDaily.trade_date))))
            if has_table(session, "rps_daily")
            else None
        ),
        "rps_end": (
            str(session.scalar(select(func.max(RpsDaily.trade_date))))
            if has_table(session, "rps_daily")
            else None
        ),
        "index_end": (
            str(session.scalar(select(func.max(IndexDaily.trade_date))))
            if has_table(session, "index_daily")
            else None
        ),
        "sector_end": (
            str(session.scalar(select(func.max(SectorDaily.trade_date))))
            if has_table(session, "sector_daily")
            else None
        ),
        "breadth_rows": (
            session.scalar(select(func.count()).select_from(BreadthDaily))
            if has_table(session, "breadth_daily")
            else None
        ),
        "notes": [
            "所有样本来自现存股票池，history.db 无退市股 → 幸存者偏差",
            "RPS 历史混版本风险：跨日研究以当前版本重算结果为准",
            "breadth 为空时市场环境仅按指数判定，报告中标注为指数口径",
        ],
    }
    _write(report_dir / "m6_data_quality.json", quality)

    report["data_quality"] = quality
    _write(report_dir / "m6_summary.json", report)
    _write_markdown(report_dir / "m6_report.md", report)
    print(f"报告已写入 {report_dir}")
    return 0


def _write_markdown(path: Path, report: dict) -> None:
    lines: list[str] = []
    lines.append("# Trend Engine M6 回测报告\n")
    lines.append(f"- 数据库：`{report['db']}`")
    lines.append(f"- 扫描区间：{report['scan_start']} ~ {report['scan_end']}（{report['scan_days']} 个交易日）")
    lines.append(f"- 成本：{report['cost_bps']} bps（一次往返）｜持有期：{report['horizons']}")
    lines.append(f"- 训练/验证/样本外：{report['splits']}\n")

    def table(payload: dict) -> None:
        header = "| 持有期 | 可成交 | 均值 | 中位 | 超额 | 跑赢% | 扣费均值 | PF | 最大回撤(点) | 连亏 | 尾部前5%占比 |"
        lines.append(header)
        lines.append("| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for horizon, stats in (payload.get("horizons") or {}).items():
            tail = stats.get("tail") or {}
            lines.append(
                f"| {horizon} | {stats.get('execution_samples')} | {stats.get('mean')} | "
                f"{stats.get('median')} | {stats.get('excess_mean')} | {stats.get('beat_pct')} | "
                f"{stats.get('net_mean')} | {stats.get('profit_factor')} | "
                f"{stats.get('max_drawdown_points')} | {stats.get('max_consecutive_losses')} | "
                f"{tail.get('top5_share')} |"
            )
        lines.append("")

    for key, payload in report["models"].items():
        lines.append(f"## {key}\n")
        if key == "combined":
            table(payload)
            continue
        lines.append("### 全区间\n")
        table(payload["full"])
        lines.append("### 分段\n")
        for name, split in (payload.get("splits") or {}).items():
            if not split:
                continue
            lines.append(f"**{name}**")
            table(split)
        lines.append("### 市场环境（20 日）\n")
        regimes = ((payload["full"].get("regimes") or {}).get(20) or {})
        lines.append("| 环境 | 样本 | 均值 | 超额 | 胜率 |")
        lines.append("| --- | ---: | ---: | ---: | ---: |")
        for name, stats in regimes.items():
            lines.append(
                f"| {name} | {stats.get('samples')} | {stats.get('mean')} | "
                f"{stats.get('excess_mean')} | {stats.get('pos_pct')} |"
            )
        lines.append("")
        lines.append("### 日期聚类 bootstrap（20 日超额）\n")
        lines.append(str((payload["full"].get("bootstrap") or {}).get(20)) + "\n")

    lines.append("## 参数敏感性（20 日超额均值）\n")
    lines.append("| 模型 | 变体 | 可成交 | 超额 | 均值 |")
    lines.append("| --- | --- | ---: | ---: | ---: |")
    for model, node in (report.get("sensitivity") or {}).items():
        for name, payload in node.items():
            if not isinstance(payload, dict) or "horizons" not in payload:
                lines.append(f"| {model} | {name} | — | UNKNOWN | — |")
                continue
            stats = payload["horizons"].get(20) or {}
            lines.append(
                f"| {model} | {name} | {stats.get('execution_samples')} | "
                f"{stats.get('excess_mean')} | {stats.get('mean')} |"
            )
    lines.append("")

    lines.append("## Walk-forward\n")
    for model, folds in (report.get("walk_forward") or {}).items():
        lines.append(f"**{model}**")
        for fold in folds:
            if "selected" not in fold:
                lines.append(f"- {fold.get('status')}: {fold.get('reason')}")
                continue
            test = fold.get("test") or {}
            lines.append(
                f"- 训练 {fold['train_block']} 选 top={fold['selected']['top']} "
                f"score≥{fold['selected']['min_score']}（训练 20 日超额 {fold['selected']['train_excess_20d']}）"
                f" → 测试 {fold['test_block']}：样本 {test.get('execution_samples')}，"
                f"超额 {test.get('excess_mean')}，均值 {test.get('mean')}"
            )
    lines.append("")

    lines.append("## 风险引擎\n")
    risk = report.get("risk_engine") or {}
    lines.append(f"- 状态：{risk.get('status')}｜原因：{risk.get('reason')}\n")

    lines.append("## 最终结论（自动生成，未通过证据的一律 UNKNOWN）\n")
    def q(model_key: str, horizon: int = 20):
        node = report["models"].get(model_key) or {}
        payload = node.get("full") if model_key != "combined" else node
        if not payload:
            return None
        return (payload.get("horizons") or {}).get(horizon) or {}

    a20, b20, c20 = q("A") or {}, q("B") or {}, q("combined") or {}
    def verdict(stats):
        if not stats or stats.get("execution_samples") is None or stats.get("excess_mean") is None:
            return "UNKNOWN"
        if stats["execution_samples"] < 200:
            return "UNKNOWN（样本不足 200）"
        return "有正向证据" if stats["excess_mean"] > 0 else "无正向证据"

    lines.append(f"- Model A 是否具备独立研究价值：{verdict(a20)}（20 日超额 {a20.get('excess_mean')}）")
    lines.append(f"- Model B 是否具备独立研究价值：{verdict(b20)}（20 日超额 {b20.get('excess_mean')}）")
    if c20 and a20 and b20 and None not in (c20.get("excess_mean"), a20.get("excess_mean"), b20.get("excess_mean")):
        better = c20["excess_mean"] > max(a20["excess_mean"], b20["excess_mean"])
        lines.append(f"- A+B 是否优于单模型：{'是' if better else '否'}"
                     f"（组合 {c20['excess_mean']} vs A {a20['excess_mean']} / B {b20['excess_mean']}）")
    else:
        lines.append("- A+B 是否优于单模型：UNKNOWN")

    def sensitivity_delta(model: str, left: str, right: str):
        node = (report.get("sensitivity") or {}).get(model) or {}
        a = node.get(left)
        b = node.get(right)
        if not isinstance(a, dict) or not isinstance(b, dict):
            return None
        sa = (a.get("horizons") or {}).get(20) or {}
        sb = (b.get("horizons") or {}).get(20) or {}
        if sa.get("excess_mean") is None or sb.get("excess_mean") is None:
            return None
        return round(sb["excess_mean"] - sa["excess_mean"], 2)

    materiality = 0.2  # 小于 0.2pp 视为「无实质增益」，避免把噪声写成结论

    def factor_verdict(delta: float | None, samples: int | None) -> str:
        if delta is None or samples is None or samples < 200:
            return "UNKNOWN（样本不足 200）"
        if abs(delta) < materiality:
            return f"无实质增益（{delta:+.2f}pp，<{materiality}pp）"
        return "是" if delta > 0 else "否"

    for model in ("A", "B"):
        node = (report.get("sensitivity") or {}).get(model) or {}
        rps_samples = ((node.get("rps_5") or {}).get("horizons") or {}).get(20, {}).get("execution_samples")
        delta = sensitivity_delta(model, "rps_0", "rps_5")
        lines.append(
            f"- 模型 {model} 的 RPS 是否带来正向增益：{factor_verdict(delta, rps_samples)}"
            + ("" if delta is None else f"（5 分 vs 0 分，20 日超额差 {delta:+.2f}pp）")
        )
        sector_samples = ((node.get("sector_5") or {}).get("horizons") or {}).get(20, {}).get("execution_samples")
        delta_sector = sensitivity_delta(model, "baseline_same_window", "sector_5")
        lines.append(
            f"- 模型 {model} 的板块因子是否带来正向增益：{factor_verdict(delta_sector, sector_samples)}"
            + ("" if delta_sector is None else f"（5 分 vs 0 分，同窗口 20 日超额差 {delta_sector:+.2f}pp）")
        )

    # 环境：优先用 Model A 的 20 日分层
    a_full = (report["models"].get("A") or {}).get("full") or {}
    regimes = (a_full.get("regimes") or {}).get(20) or {}
    if regimes:
        best = max(regimes.items(), key=lambda item: (item[1].get("excess_mean") if item[1].get("excess_mean") is not None else -999))
        worst = min(regimes.items(), key=lambda item: (item[1].get("excess_mean") if item[1].get("excess_mean") is not None else 999))
        lines.append(
            f"- 最适合的市况（Model A 20 日分层）：{best[0]}"
            f"（20 日超额 {best[1].get('excess_mean')}，样本 {best[1].get('samples')}）"
        )
        lines.append(
            f"- 最容易亏损的市况（Model A 20 日分层）：{worst[0]}"
            f"（20 日超额 {worst[1].get('excess_mean')}，样本 {worst[1].get('samples')}）"
        )
    else:
        lines.append("- 最适合 / 最容易亏损的市况：UNKNOWN（环境映射缺失）")

    tails = (report.get("models", {}).get("A") or {}).get("full", {}).get("horizons", {})
    shares = [
        (stats.get("tail") or {})
        for stats in tails.values()
        if (stats.get("tail") or {}).get("top5_share") is not None
    ]
    if shares:
        peak_item = max(shares, key=lambda item: item["top5_share"])
        peak = peak_item["top5_share"]
        total = peak_item.get("total_gross")
        lines.append(
            f"- 是否存在严重尾部依赖：{'是' if peak > 0.5 else '否'}"
            f"（最大前 5% 收益 = 总收益的 {peak:.2f} 倍，即 {peak * 100:.0f}%；"
            f"该档总收益 {total}）"
        )
    else:
        lines.append("- 是否存在严重尾部依赖：UNKNOWN（总收益不为正时无法计算占比）")

    lines.append(f"- 风险引擎是否降低最大回撤：{risk.get('status')}")
    oos = (report["models"].get("A", {}).get("splits", {}) or {}).get("test") or {}
    oos20 = (oos.get("horizons") or {}).get(20) or {}
    ready = (
        oos20.get("execution_samples", 0) >= 200
        and (oos20.get("excess_mean") or 0) > 0
        and (oos.get("bootstrap", {}).get(20, {}) or {}).get("ci_low", -1) > 0
    )
    lines.append(f"- 是否具备进入 M7 的条件：{'是' if ready else '否'}"
                 f"（样本外 20 日超额 {oos20.get('excess_mean')}，"
                 f"bootstrap 下界 {((oos.get('bootstrap') or {}).get(20) or {}).get('ci_low')}）")

    lines.append("\n> 所有结论只代表本库样本；`history.db` 无退市股、RPS 历史混版本、"
                 "breadth 为空 —— 不得外推为未来收益承诺。\n")
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
