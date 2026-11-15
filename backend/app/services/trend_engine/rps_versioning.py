"""RPS 版本化与重算内核（纯函数 + 只读判定）。

M6.3 要求：

1. 每个 RPS 结果必须能追溯到版本、窗口、股票池、复权方法、输入截止日；
2. 不同版本禁止拼接；孤儿日 / 混版本日拒绝读取；
3. 允许在 sandbox 中重算新版本，但**不得覆盖**旧 `rps_daily`。

本模块只提供纯计算与判定，不查库、不写库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np

WINDOWS = (20, 50, 60, 120, 250)


@dataclass(frozen=True, slots=True)
class RpsVersion:
    version_id: str
    algo: str
    windows: tuple[int, ...]
    universe_rule: str
    st_rule: str
    new_listing_rule: str
    suspension_rule: str
    rank_method: str
    input_start: date
    input_cutoff: date
    db_sha256: str
    adjustment_method: str
    code_sha: str
    created_at: str
    quality_status: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "version_id": self.version_id,
            "algo": self.algo,
            "windows": list(self.windows),
            "universe_rule": self.universe_rule,
            "st_rule": self.st_rule,
            "new_listing_rule": self.new_listing_rule,
            "suspension_rule": self.suspension_rule,
            "rank_method": self.rank_method,
            "input_start": str(self.input_start),
            "input_cutoff": str(self.input_cutoff),
            "db_sha256": self.db_sha256,
            "adjustment_method": self.adjustment_method,
            "code_sha": self.code_sha,
            "created_at": self.created_at,
            "quality_status": self.quality_status,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class RpsDayQuality:
    trade_date: date
    stock_rows: int
    rps_rows: int
    ret20_rows: int
    ret120_rows: int
    status: str
    reason: str

    @property
    def usable(self) -> bool:
        return self.status in {"OK", "PARTIAL"}


def classify_legacy_day(
    *,
    trade_date: date,
    stock_rows: int,
    rps_rows: int,
    ret20_rows: int,
    ret120_rows: int,
    stock_last_date: date,
) -> RpsDayQuality:
    """判定旧 `rps_daily` 某日是否可用于研究。"""
    if rps_rows == 0:
        return RpsDayQuality(trade_date, stock_rows, rps_rows, ret20_rows, ret120_rows, "MISSING", "无 RPS 数据")
    if trade_date > stock_last_date:
        return RpsDayQuality(
            trade_date, stock_rows, rps_rows, ret20_rows, ret120_rows, "SUSPECT", "孤儿日期：晚于个股日线末日"
        )
    if stock_rows == 0:
        return RpsDayQuality(
            trade_date, stock_rows, rps_rows, ret20_rows, ret120_rows, "SUSPECT", "孤儿 RPS：当日无个股日线"
        )
    if rps_rows and ret20_rows == 0 and ret120_rows > 0:
        return RpsDayQuality(
            trade_date, stock_rows, rps_rows, ret20_rows, ret120_rows, "SUSPECT", "混版本：ret20 全空但 ret120 有值"
        )
    if ret20_rows / max(stock_rows, 1) < 0.8:
        return RpsDayQuality(
            trade_date, stock_rows, rps_rows, ret20_rows, ret120_rows, "PARTIAL", "ret20 覆盖率 <80%"
        )
    return RpsDayQuality(trade_date, stock_rows, rps_rows, ret20_rows, ret120_rows, "OK", "覆盖正常")


def percentile_rank(values: np.ndarray) -> np.ndarray:
    """平均秩百分位（0~100），并列取平均秩，与常见 RPS 口径一致。"""
    size = values.size
    if size == 0:
        return np.asarray([], dtype=float)
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(size, dtype=float)
    ranks[order] = np.arange(1, size + 1, dtype=float)
    # 并列取平均秩
    sorted_values = values[order]
    start = 0
    while start < size:
        end = start + 1
        while end < size and sorted_values[end] == sorted_values[start]:
            end += 1
        if end - start > 1:
            ranks[order[start:end]] = (start + 1 + end) / 2.0
        start = end
    if size == 1:
        return np.asarray([100.0])
    return (ranks - 1) / (size - 1) * 100


def returns_for_window(closes: np.ndarray, window: int) -> np.ndarray:
    """按交易行数计算窗口收益率（百分数）；不足窗口为 NaN。"""
    out = np.full(closes.size, np.nan)
    if closes.size > window:
        out[window:] = (closes[window:] / closes[:-window] - 1) * 100
    return out


def cross_section_ranks(returns_by_code: dict[str, float]) -> dict[str, float]:
    """对单日横截面做百分位排名；空输入返回空。"""
    codes = list(returns_by_code)
    if not codes:
        return {}
    values = np.asarray([returns_by_code[code] for code in codes], dtype=float)
    ranks = percentile_rank(values)
    return {code: float(ranks[index]) for index, code in enumerate(codes)}


def version_stamp(version_id: str, created_at: datetime | None = None) -> str:
    return (created_at or datetime.utcnow()).isoformat(timespec="seconds")
