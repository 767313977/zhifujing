"""M6.6 Batch 3：G9 样本验收器（纯函数 + 离线重放，不联网）。

职责：
- 因子方向未知时禁止构造复权序列；
- 缺失字段写 NULL 并标记 `FIELD_NOT_PROVIDED`，不填零；
- 公告日期保持日期级，不升级为时间戳；
- 同一证券同一登记日/除权日的多行事件，若无法确认同一方案则标 `CONFLICT_MULTI_PLAN`，不自动求和；
- 跨供应商比价只输出绝对误差与 bp，不在事后挑选阈值。

所有测试数据必须显式标记 SYNTHETIC。

用法：

    python scripts/m66_g9_validator.py --offline-replay
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.trend_engine import multisource as ms  # noqa: E402

M64_STAGING = ROOT / "backend/data/m64_staging/batch_20261115T152701Z"
REPORT_DIR = ROOT / "reports/trend_engine/m66"

QUALITY_STATUSES = {"VERIFIED", "PARTIAL", "UNKNOWN", "CONFLICT", "REJECTED", "SYNTHETIC"}
CUMULATIVE_TYPES = {"CUMULATIVE_BACKWARD", "CUMULATIVE_FORWARD"}
FACTOR_TYPES = CUMULATIVE_TYPES | {"SINGLE_EVENT", "UNKNOWN"}

EVENT_NUMERIC_FIELDS = (
    "cash_per_share_pretax",
    "cash_per_share_posttax",
    "stock_dividend_ratio",
    "transfer_ratio",
    "rights_ratio",
    "rights_price",
    "rights_shares",
)


class AdjustedConstructionRejected(ValueError):
    """因子方向/类型不明时禁止构造复权序列。"""


def validate_factor(factor: float | None, factor_type: str, factor_base_date: str | None) -> dict:
    if factor_type not in FACTOR_TYPES:
        raise ValueError(f"未知 factor_type：{factor_type}")
    if factor is None:
        return {
            "quality_status": "UNKNOWN",
            "reconstructable": False,
            "flags": ["FIELD_NOT_PROVIDED"],
            "reason": "复权因子缺失",
        }
    if factor_type not in CUMULATIVE_TYPES:
        return {
            "quality_status": "UNKNOWN",
            "reconstructable": False,
            "flags": ["FACTOR_SEMANTICS_UNKNOWN"],
            "reason": "因子方向/累计规则未书面确认",
        }
    if not factor_base_date:
        return {
            "quality_status": "PARTIAL",
            "reconstructable": True,
            "flags": ["FACTOR_BASE_DATE_MISSING"],
            "reason": "方向已知但基准日缺失",
        }
    return {"quality_status": "VERIFIED", "reconstructable": True, "flags": [], "reason": ""}


def reconstruct_adjusted(raw_close: float, factor: float | None, factor_type: str) -> float:
    verdict = validate_factor(factor, factor_type, None)
    if not verdict["reconstructable"]:
        raise AdjustedConstructionRejected(verdict["reason"])
    return raw_close * float(factor)


def normalize_event(row: dict) -> dict:
    """缺失字段写 None 并记录 FIELD_NOT_PROVIDED，不用 0 代替。"""
    flags: list[str] = []
    normalized: dict[str, Any] = {
        "code": str(row.get("code") or "").strip(),
        "event_type": row.get("event_type") or "UNKNOWN",
        "plan_id": row.get("plan_id"),
        "record_date": row.get("record_date"),
        "ex_date": row.get("ex_date"),
        "pay_date": row.get("pay_date"),
        "share_listing_date": row.get("share_listing_date"),
        "available_at": ms.normalize_available_at(row.get("available_at")),
        "provider": row.get("provider"),
        "source_record_id": row.get("source_record_id"),
    }
    announcement, precision = ms.normalize_announcement(row.get("announcement_date"))
    normalized["announcement_date"] = announcement
    normalized["announcement_precision"] = precision
    if precision != "date":
        flags.append("ANNOUNCEMENT_DATE_MISSING")

    for field in EVENT_NUMERIC_FIELDS:
        value = row.get(field)
        normalized[field] = value
        if value is None:
            flags.append(f"FIELD_NOT_PROVIDED:{field}")
    normalized["flags"] = flags
    normalized["quality_status"] = "PARTIAL" if flags else "VERIFIED"
    return normalized


def aggregate_events(rows: list[dict]) -> list[dict]:
    """按事件键聚合；多行且无法确认同一方案时标冲突，不自动求和。"""
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        event = row if "flags" in row else normalize_event(row)
        key = (event["code"], event["record_date"], event["ex_date"])
        groups.setdefault(key, []).append(event)

    result = []
    for key, events in groups.items():
        plans = {event.get("plan_id") for event in events}
        distinct_plans = {plan for plan in plans if plan}
        conflicted = len(events) > 1 and (len(distinct_plans) > 1 or not distinct_plans)
        aggregated = {
            "code": key[0],
            "record_date": key[1],
            "ex_date": key[2],
            "rows": len(events),
            "plans": sorted(distinct_plans),
            "quality_status": "CONFLICT" if conflicted else "VERIFIED",
            "flags": ["CONFLICT_MULTI_PLAN"] if conflicted else [],
        }
        for field in EVENT_NUMERIC_FIELDS:
            values = [event.get(field) for event in events]
            provided = [value for value in values if value is not None]
            if conflicted:
                aggregated[field] = None
            elif provided:
                aggregated[field] = sum(provided)
            else:
                aggregated[field] = None
        result.append(aggregated)
    return sorted(result, key=lambda item: (item["code"], str(item["ex_date"])))


def compare_providers(rows_a: list[dict], rows_b: list[dict], key_fields: tuple[str, ...], value_field: str) -> list[dict]:
    """跨供应商比价：只给绝对误差与相对 bp，不做阈值裁决。"""
    def index(rows: list[dict]) -> dict:
        return {tuple(row.get(field) for field in key_fields): row.get(value_field) for row in rows}

    left, right = index(rows_a), index(rows_b)
    diffs = []
    for key in sorted(set(left) & set(right), key=lambda item: tuple(str(part) for part in item)):
        a, b = left[key], right[key]
        if a is None or b is None:
            diffs.append({"key": key, "a": a, "b": b, "abs_diff": None, "rel_bp": None, "status": "MISSING"})
            continue
        abs_diff = abs(float(a) - float(b))
        rel_bp = (abs_diff / abs(float(b)) * 10000) if float(b) else None
        diffs.append(
            {
                "key": key,
                "a": a,
                "b": b,
                "abs_diff": round(abs_diff, 8),
                "rel_bp": round(rel_bp, 4) if rel_bp is not None else None,
                "status": "COMPARED",
            }
        )
    return diffs


def offline_replay() -> dict:
    """用 M6.4 已落盘证据做只读重放：方向未书面确认 → 不可重建 adjusted。"""
    factor_check = json.loads((ROOT / "reports/trend_engine/m64/factor_check.json").read_text(encoding="utf-8"))
    verdict = validate_factor(8.8814, "UNKNOWN", None)
    payload = {
        "batch": "m66-batch3-g9-offline-replay",
        "source": "reports/trend_engine/m64/factor_check.json",
        "m64_rows": factor_check.get("rows"),
        "m64_max_relative_diff": factor_check.get("max_relative_diff"),
        "m64_relative_within_5bp": factor_check.get("relative_within_5bp"),
        "m64_verdict": factor_check.get("verdict"),
        "m66_factor_verdict": verdict,
        "reconstructable": verdict["reconstructable"],
        "conclusion": "因子数值关系可复算，但方向未书面确认 → G9-PRICE 仍 PARTIAL",
        "status": "OFFLINE_REPLAY_OF_EXISTING_EVIDENCE",
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "g9_offline_replay.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="G9 验收器（离线，不联网）")
    parser.add_argument("--offline-replay", action="store_true")
    args = parser.parse_args()
    if args.offline_replay:
        print(json.dumps(offline_replay(), ensure_ascii=False, indent=2))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
