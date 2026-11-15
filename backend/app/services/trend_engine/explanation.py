"""解释层：把模型结果整理成「入选原因 / 排除原因 / 风险提示 / 关键价位」。

只做格式化与去重，不产生任何新的判断。所有文本都必须能追溯到具体指标值，
页面不允许只显示一个黑盒分数。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Explanation:
    reasons: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    #: 关键价位（突破价 / 支撑价 / 失效参考价），单位与 `bars` 一致（前复权价）
    levels: dict[str, float] = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        return not self.exclusions


def pct(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:+.{digits}f}%"


def num(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def ratio(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}×"


def collect(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """按顺序合并并去重（保留首次出现）。"""
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for item in group:
            text = (item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(text)
    return tuple(out)
