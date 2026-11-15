"""候选池筛选与排序：**不补足数量**，不给缺失/被排除的票凑排名。

池子命名与任务书一致：
`trend_start_pool` / `trend_continuation_pool` / `trend_watch_pool` /
`trend_warning_pool` / `trend_broken_pool`。

本模块不写库：它只接收模型结果，返回排序后的列表与池子归属，落库由 M7 的后台任务负责。
"""

from __future__ import annotations

from app.services.trend_engine import config as cfg_module

START_POOL = "trend_start_pool"
CONTINUATION_POOL = "trend_continuation_pool"
WATCH_POOL = "trend_watch_pool"
WARNING_POOL = "trend_warning_pool"
BROKEN_POOL = "trend_broken_pool"

#: 模型 A 分层（与 `breakout_model.TIERS` 一致）
START_TIERS = (
    (80.0, "重点启动候选"),
    (70.0, "启动观察"),
    (60.0, "等待确认"),
    (0.0, "不进入核心池"),
)

#: 模型 B 分层
CONTINUATION_TIERS = (
    (85.0, "核心趋势观察"),
    (75.0, "趋势延续候选"),
    (65.0, "等待确认"),
    (0.0, "不进入核心池"),
)


def top(
    results,
    *,
    limit: int = 20,
    min_score: float = 0.0,
    eligible_only: bool = True,
) -> list:
    """按分数降序取前 `limit` 只。合格票不足时返回多少算多少，**不补足**。"""
    rows = [item for item in results if (item.eligible or not eligible_only)]
    rows = [item for item in rows if item.score >= min_score]
    rows.sort(key=lambda item: (-item.score, item.code))
    return rows[:limit]


def start_pool(score: float, eligible: bool) -> str | None:
    if not eligible:
        return None
    if score >= 70:
        return START_POOL
    if score >= 60:
        return WATCH_POOL
    return None


def continuation_pool(score: float, eligible: bool) -> str | None:
    if not eligible:
        return None
    if score >= 75:
        return CONTINUATION_POOL
    if score >= 65:
        return WATCH_POOL
    return None


def risk_pool(state: str) -> str | None:
    """风险状态 → 池子。NORMAL 不进任何风险池。"""
    if state == "WARNING":
        return WARNING_POOL
    if state == "BROKEN":
        return BROKEN_POOL
    return None


def weights_for(config: cfg_module.ModelAConfig | cfg_module.ModelBConfig) -> dict[str, float]:
    """便于报告/页面展示：当前生效的权重表（含 RPS / 板块）。"""
    return cfg_module.effective_weights(
        config.base_weights, config.rps_points, config.sector_points
    )
