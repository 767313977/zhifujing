"""双价格域：信号价（复权）与执行价（原始）的强类型隔离。

设计原则（M6.3）：

1. **执行价只用 raw**：成交、涨跌停、现金、持仓市值、费用 → `ExecutionBar`。
2. **信号价只用 adjusted**：MA、ATR、结构高低点、收益、波动 → `SignalBar`。
3. 复权因子缺失 / 非法 / 未验证时，`SignalBar` **构造失败**，不得静默退化为 raw。
4. 类型不共享 `close` 之类模糊字段，调用方无法「拿错一个价格域」。

⚠️ 当前数据现状：库内没有复权因子与公司行为表（见 `reports/trend_engine/m63/01-dual-price-contract.md`）。
因此生产数据上 `build_price_bar` 只会产出 `adjustment_status=UNKNOWN`，`to_signal_bar` 必失败。
本模块的公式仅在显式传入因子时使用，且 `method` 必须由调用方声明来源；未核验的因子不得传入。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

AdjustmentMethod = Literal["FORWARD", "BACKWARD", "NONE", "UNKNOWN"]
QualityStatus = Literal["OK", "PARTIAL", "UNKNOWN", "INVALID_DATA", "SUSPECT"]

ADJUSTMENT_OK = "OK"
ADJUSTMENT_UNKNOWN = "UNKNOWN"
ADJUSTMENT_INVALID = "INVALID_DATA"


class PriceDomainError(ValueError):
    """价格域混用或价格数据不可用于该域时抛出。"""


@dataclass(frozen=True, slots=True)
class PriceBar:
    code: str
    trade_date: date
    raw_open: Decimal
    raw_high: Decimal
    raw_low: Decimal
    raw_close: Decimal
    adjusted_open: Decimal | None
    adjusted_high: Decimal | None
    adjusted_low: Decimal | None
    adjusted_close: Decimal | None
    adj_factor: Decimal | None
    adjustment_method: AdjustmentMethod
    adjustment_reference_date: date | None
    adjustment_status: str
    source: str
    source_version: str
    available_at: datetime | None
    ingested_at: datetime | None
    corporate_action_revision_id: str | None
    quality_status: QualityStatus


@dataclass(frozen=True, slots=True)
class SignalBar:
    """只含复权价；构造成功即代表信号域可用。"""

    code: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adj_factor: Decimal
    adjustment_method: AdjustmentMethod
    adjustment_reference_date: date | None
    quality_status: QualityStatus


@dataclass(frozen=True, slots=True)
class ExecutionBar:
    """只含原始价；成交与会计必须使用本类型。"""

    code: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    quality_status: QualityStatus


def _as_decimal(value: float | int | str | Decimal | None, field: str) -> Decimal:
    if value is None:
        raise PriceDomainError(f"{field} 缺失")
    try:
        result = Decimal(repr(float(value)))
    except (TypeError, ValueError) as exc:
        raise PriceDomainError(f"{field} 非法：{value!r}") from exc
    if not result.is_finite():
        raise PriceDomainError(f"{field} 非有限值：{value!r}")
    return result


def _validate_ohlc(open_: Decimal, high: Decimal, low: Decimal, close: Decimal) -> None:
    for name, value in (("open", open_), ("high", high), ("low", low), ("close", close)):
        if value <= 0:
            raise PriceDomainError(f"raw_{name} 必须为正数，收到 {value}")
    if high < low:
        raise PriceDomainError(f"high({high}) < low({low})")
    if not (low <= open_ <= high and low <= close <= high):
        raise PriceDomainError("open/close 必须落在 [low, high] 内")


def build_price_bar(
    *,
    code: str,
    trade_date: date,
    raw_open: float | Decimal,
    raw_high: float | Decimal,
    raw_low: float | Decimal,
    raw_close: float | Decimal,
    adj_factor: Decimal | None = None,
    adjustment_method: AdjustmentMethod = "UNKNOWN",
    adjustment_reference_date: date | None = None,
    source: str = "b1_research.stock_daily",
    source_version: str = "unknown",
    available_at: datetime | None = None,
    ingested_at: datetime | None = None,
    corporate_action_revision_id: str | None = None,
) -> PriceBar:
    """构建一根双价格域 bar；缺因子时 adjusted_* 为 None 且状态为 UNKNOWN。"""
    open_ = _as_decimal(raw_open, "raw_open")
    high = _as_decimal(raw_high, "raw_high")
    low = _as_decimal(raw_low, "raw_low")
    close = _as_decimal(raw_close, "raw_close")
    _validate_ohlc(open_, high, low, close)

    adjusted: tuple[Decimal | None, ...] = (None, None, None, None)
    status: str = ADJUSTMENT_UNKNOWN
    factor: Decimal | None = None
    method: AdjustmentMethod = adjustment_method

    if adj_factor is not None:
        factor = _as_decimal(adj_factor, "adj_factor")
        if factor <= 0:
            status = ADJUSTMENT_INVALID
            method = "UNKNOWN"
        elif adjustment_method in ("FORWARD", "BACKWARD"):
            adjusted = (open_ * factor, high * factor, low * factor, close * factor)
            status = ADJUSTMENT_OK
        else:
            status = ADJUSTMENT_UNKNOWN
            method = "UNKNOWN"

    quality: QualityStatus = "OK" if status == ADJUSTMENT_OK else "PARTIAL"
    if status == ADJUSTMENT_INVALID:
        quality = "INVALID_DATA"
    return PriceBar(
        code=code,
        trade_date=trade_date,
        raw_open=open_,
        raw_high=high,
        raw_low=low,
        raw_close=close,
        adjusted_open=adjusted[0],
        adjusted_high=adjusted[1],
        adjusted_low=adjusted[2],
        adjusted_close=adjusted[3],
        adj_factor=factor,
        adjustment_method=method,
        adjustment_reference_date=adjustment_reference_date,
        adjustment_status=status,
        source=source,
        source_version=source_version,
        available_at=available_at,
        ingested_at=ingested_at,
        corporate_action_revision_id=corporate_action_revision_id,
        quality_status=quality,
    )


def to_signal_bar(bar: PriceBar) -> SignalBar:
    """转信号域；因子缺失 / 非法 / 未声明方法时拒绝。"""
    if bar.adjustment_status != ADJUSTMENT_OK or bar.adjusted_close is None:
        raise PriceDomainError(
            f"{bar.code} {bar.trade_date} 信号价不可用：status={bar.adjustment_status}, method={bar.adjustment_method}"
        )
    assert bar.adjusted_open is not None and bar.adjusted_high is not None and bar.adjusted_low is not None
    return SignalBar(
        code=bar.code,
        trade_date=bar.trade_date,
        open=bar.adjusted_open,
        high=bar.adjusted_high,
        low=bar.adjusted_low,
        close=bar.adjusted_close,
        adj_factor=bar.adj_factor,  # type: ignore[arg-type]
        adjustment_method=bar.adjustment_method,
        adjustment_reference_date=bar.adjustment_reference_date,
        quality_status=bar.quality_status,
    )


def to_execution_bar(bar: PriceBar) -> ExecutionBar:
    """转执行域；只返回 raw，不受因子影响。"""
    if bar.quality_status == "INVALID_DATA":
        raise PriceDomainError(f"{bar.code} {bar.trade_date} raw 价格非法")
    return ExecutionBar(
        code=bar.code,
        trade_date=bar.trade_date,
        open=bar.raw_open,
        high=bar.raw_high,
        low=bar.raw_low,
        close=bar.raw_close,
        quality_status=bar.quality_status,
    )


def adjusted_series_is_continuous(
    previous_adjusted_close: Decimal | None, current_adjusted_close: Decimal | None, *, tolerance_pct: float = 0.5
) -> bool:
    """除权日核验：复权序列不应出现超过容差的跳变。"""
    if previous_adjusted_close is None or current_adjusted_close is None:
        return False
    if previous_adjusted_close <= 0:
        return False
    gap = abs(current_adjusted_close / previous_adjusted_close - 1) * 100
    return gap <= tolerance_pct
