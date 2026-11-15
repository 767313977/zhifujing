"""Trend Engine 参数与版本。

所有阈值集中在这里，且都以 `frozen dataclass` 暴露，便于回测在**训练窗口内**做参数搜索。
默认值来自任务书与项目既有实测（见 `reports/trend_engine/00-architecture-audit.md`）：

- RPS 只做低权重分项（默认 3/100，可在 0~5 之间配置），**不做硬过滤**；
- 板块默认 0 分，可在 0~5 之间配置，**不做硬过滤**（B1 报告：板块叠加为负超额）；
- 模型 A / B 的基础分项各自归一化，两个模型独立评分，不合成单一总分。

⚠️ 改这里任何一个数都要同时升 `CALC_VERSION`（或模型版本），否则历史快照会被误认为同一口径。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

#: 指标层版本：窗口 / 复权 / 分母口径变化时必须升
CALC_VERSION = "trend-engine-1.0.0"

MODEL_A_NAME = "trend_breakout"
MODEL_B_NAME = "trend_continuation"
MODEL_A_VERSION = "A-1.0.0"
MODEL_B_VERSION = "B-1.0.0"

#: RPS / 板块分项的可配置上限（任务书第九节）
SATELLITE_MAX_POINTS = 5.0


@dataclass(frozen=True, slots=True)
class IndicatorConfig:
    """指标窗口与最小数据要求。"""

    #: 均线族。MA250 需要 250 根，缺就返回 None（不拿短窗口冒充长期均线）
    ma_periods: tuple[int, ...] = (5, 10, 20, 30, 60, 120, 250)
    #: 斜率：MA 在 `slope_days` 个交易日内的变化率（%）
    slope_days: int = 5
    #: 长斜率：10 日变化率（%），与 5 日一起提供，避免只看一周噪声
    slope_long_days: int = 10
    #: ATR 窗口。项目既有 `patterns._low_atr_growth` 用简单均值（非 Wilder），这里保持一致
    atr_window: int = 14
    atr_long_window: int = 20
    #: 量能分母窗口：5 日成交量、20 日成交额（均**不含当日**）
    volume_window: int = 5
    amount_window: int = 20
    #: 收益率窗口（交易日）。短中长各留一档，回测/页面共用
    ret_windows: tuple[int, ...] = (5, 10, 20, 40, 60, 120, 250)
    #: 结构判定至少需要的中期窗口（MA60 + 斜率余量）
    min_bars: int = 130
    #: 均线走平阈值：5 日变化率绝对值低于它算「走平」（%）
    slope_flat_pct: float = 0.2
    #: 均线「粘合」阈值：MA5/10/20/60 的极差 / 均值 低于它算收敛
    convergence_pct: float = 3.0
    #: 长周期高点回溯（250 个交易日 ≈ 一年）
    high_lookback: int = 250


@dataclass(frozen=True, slots=True)
class PlatformConfig:
    """平台/箱体参数。平台窗口**不含当日**（PIT 硬要求）。"""

    #: 平台观察窗口（交易日），实际可用长度不足 `min_days` 视为数据不足
    days: int = 30
    min_days: int = 20
    #: 平台宽度上限（%）：(平台高 − 平台低) / 平台低 × 100
    max_width_pct: float = 18.0
    #: 理想宽度（%）：不超过它拿满「平台质量」分
    ideal_width_pct: float = 8.0
    #: 突破后允许高出平台上沿的上限（%）。超过就不是「突破」而是追高
    max_breakout_excess_pct: float = 8.0
    #: 突破所需的最小幅度（%），0 表示收盘只要高于平台上沿即可
    min_breakout_buffer_pct: float = 0.0


@dataclass(frozen=True, slots=True)
class ModelAConfig:
    """趋势启动模型（Model A）参数。"""

    indicator: IndicatorConfig = field(default_factory=IndicatorConfig)
    platform: PlatformConfig = field(default_factory=PlatformConfig)
    #: 流动性：前 20 日日均成交额下限（元）。可按市场阶段覆盖，不写死在代码里
    min_amount20: float = 5.0e7
    #: 突破日成交额 / 前 20 日均额。分母不含当日；同时要求 5 日均额不过度萎缩
    min_breakout_amount_ratio: float = 1.5
    ideal_breakout_amount_ratio: float = 3.0
    #: 上限：单日异常放量（>8 倍）不加分，避免一笔大宗/乌龙指刷出高分
    max_breakout_amount_ratio: float = 8.0
    #: 收盘在当日振幅中的位置（0~1），越高说明尾盘越强
    min_close_position: float = 0.6
    #: 收盘相对 MA20 的最大正乖离（%）：超过视为过度加速，直接排除
    max_extension_above_ma20_pct: float = 15.0
    #: 平台期内均线收敛要求：收敛程度分项的最低门槛（%）
    max_platform_convergence_pct: float = 8.0
    #: 波动率收缩：突破前 ATR% 相对 60 日前 ATR% 的上限
    max_atr_contraction_ratio: float = 1.0
    #: 假突破风险：突破日上影线占振幅的比例上限
    max_upper_shadow_pct: float = 50.0
    #: 评分卫星项（任务书第九节）
    rps_points: float = 3.0
    sector_points: float = 0.0
    #: 基础分项权重（会在扣除卫星项后按比例归一化到 100）
    base_weights: tuple[tuple[str, float], ...] = (
        ("structure", 40.0),
        ("breakout", 30.0),
        ("volume", 30.0),
    )


@dataclass(frozen=True, slots=True)
class PullbackConfig:
    """回调结构参数（Model B）。"""

    #: 观察最近多少个交易日内的回撤。必须覆盖「反弹段 + 回调段 + 回调前的峰」，
    #: 否则反弹创新高后，峰会被窗口截断、回撤被低估（实测 40 天会把 4.1% 算成 3.0%）
    lookback_days: int = 60
    #: 阶段回撤合理区间（%）。低于下限说明还没回调，高于上限说明结构可能已坏
    min_depth_pct: float = 3.0
    max_depth_pct: float = 12.0
    #: ATR 标准化回撤区间：回撤深度 / ATR% 。低波动股不能按固定 3%~12% 卡
    min_atr_multiple: float = 0.8
    max_atr_multiple: float = 3.0
    #: 回调期成交量 / 上涨期成交量 的上限（缩量回踩）
    max_volume_shrink_ratio: float = 0.8
    #: 回调低点距 MA20 的容忍度（%）：跌破 MA20 超过这个比例不算「获得支撑」
    max_below_ma20_pct: float = 3.0
    #: 再次转强所需的最小放量倍数（相对回调期均量）
    min_rebound_amount_ratio: float = 1.2


@dataclass(frozen=True, slots=True)
class ModelBConfig:
    """趋势延续模型（Model B）参数。Model B 不复用 Model A 的突破评分。"""

    indicator: IndicatorConfig = field(default_factory=IndicatorConfig)
    pullback: PullbackConfig = field(default_factory=PullbackConfig)
    min_amount20: float = 5.0e7
    #: 中期趋势至少持续多少个交易日（收盘 > MA20）
    min_trend_days: int = 20
    #: MA20 / MA60 均线走平阈值沿用 `IndicatorConfig.slope_flat_pct`
    #: MA60 向下属于「例外」：允许入选但必须单独标记，不能靠降低阈值隐藏
    allow_ma60_down_exception: bool = True
    #: RPS 只做分项（默认 3 分）
    rps_points: float = 3.0
    sector_points: float = 0.0
    base_weights: tuple[tuple[str, float], ...] = (
        ("structure", 40.0),
        ("pullback", 30.0),
        ("volume", 30.0),
    )


@dataclass(frozen=True, slots=True)
class SectorConfig:
    """板块强度参数。"""

    #: 两个窗口必须与 rps.ret / mid_rank 同口径
    windows: tuple[int, ...] = (20, 60)
    #: 覆盖率闸门：窗口内有效交易日占比低于它 → 该档记 None
    min_coverage: float = 0.9
    #: 强势板块阈值（RPS20）
    strong_rps: float = 80.0
    #: 板块内「强势股」的个股 RPS60 阈值
    member_strong_rps: float = 80.0
    #: 持续性：最近多少个交易日 RPS20 ≥ 80 视为连续强势
    persistence_days: int = 3
    #: 排名变化回看交易日
    rank_change_days: int = 5


@dataclass(frozen=True, slots=True)
class MarketConfig:
    """市场环境参数。缺数据只能返回 UNKNOWN / 降级，不允许猜。"""

    index_code: str = "000001.SH"
    ma_fast: int = 20
    ma_slow: int = 60
    #: BULL：指数在 MA20 上方且 MA20 > MA60
    #: BEAR：指数在 MA60 下方且 MA60 斜率向下
    #: EXTREME_RISK：波动率 / 跌停 / 恐慌的任一硬条件
    extreme_qvix_pct: float = 35.0
    extreme_down_ratio: float = 0.8
    extreme_limit_down: int = 60
    #: 至少需要多少个可用输入才给结论（1 = 仅指数；低于它返回 UNKNOWN）
    min_inputs: int = 2


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """风险引擎参数。NORMAL / WARNING / BROKEN 多条件确认。"""

    #: 跌破 MA20 超过这个比例算 WARNING 候选（%）
    ma20_break_pct: float = 1.0
    #: 跌破 MA60 超过这个比例才算结构破坏候选（%）
    ma60_break_pct: float = 2.0
    #: BROKEN 至少需要的确认条件数（禁止单日跌破即 BROKEN）
    broken_min_conditions: int = 2
    #: 连续收盘低于关键位多少天才算确认
    confirm_days: int = 2
    #: 结构低点回溯窗口
    structure_low_days: int = 40
    #: ATR 风险参考倍数
    atr_stop_multiple: float = 2.0
    #: 建议复核间隔（交易日）
    review_days: int = 5
    #: RPS 持续下降的判定：最近 N 日下降幅度（分位）
    rps_drop_points: float = 10.0
    rps_drop_days: int = 5


def _validate_points(name: str, value: float) -> None:
    if not 0 <= value <= SATELLITE_MAX_POINTS:
        raise ValueError(f"{name} 必须在 0~{SATELLITE_MAX_POINTS:g} 之间，收到 {value}")


def effective_weights(base: tuple[tuple[str, float], ...], rps_points: float, sector_points: float) -> dict[str, float]:
    """把基础分项归一化到「100 − RPS − 板块」，并返回完整权重表。

    例：基础 40/30/30、RPS=3、板块=0 → structure 38.8 / breakout 29.1 / volume 29.1 / rps 3.0。
    这样总分恒为 100，且卫星项权重变化不会偷偷放大其它分项。
    """
    _validate_points("rps_points", rps_points)
    _validate_points("sector_points", sector_points)
    total_base = sum(value for _key, value in base)
    if total_base <= 0:
        raise ValueError("基础权重必须为正")
    budget = 100.0 - rps_points - sector_points
    weights = {key: value / total_base * budget for key, value in base}
    weights["rps"] = rps_points
    weights["sector"] = sector_points
    return weights


def model_a_config(**overrides) -> ModelAConfig:
    """测试/回测用：在默认配置上覆盖少量字段。"""
    return replace(ModelAConfig(), **overrides)


def model_b_config(**overrides) -> ModelBConfig:
    return replace(ModelBConfig(), **overrides)
