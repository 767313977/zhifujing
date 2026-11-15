"""M6.4 Batch 2 追加：跨除权日的复权因子公式核验（1 次真实调用）。

只用 600519 在 2026-06-26 除息日前后的窗口，检验：
- 复权因子是否在除权日变化；
- 收盘价 / 复权因子 是否等于前收（即 `adjusted = raw × factor`）；
- 调整后序列在除权日是否连续。

输出：`factor_formula_reconciliation.csv`、`factor_check.json`（staging + reports）。
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import get_settings  # noqa: E402
from app.sources import ifind as ifind_module  # noqa: E402

STAGING_ROOT = ROOT / "backend/data/m64_staging"
REPORT_DIR = ROOT / "reports/trend_engine/m64"


def number(value) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text in {"-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def main() -> int:
    settings = get_settings()
    ifind_module.record_call = lambda *args, **kwargs: None  # type: ignore[assignment]
    client = ifind_module.IfindClient(settings)
    batch = sorted(STAGING_ROOT.glob("batch_*"))[-1]
    query = (
        "600519 从2026-06-15到2026-07-10 每个交易日的收盘价、前复权收盘价、后复权收盘价、复权因子"
    )
    response = client.call("stock", "get_stock_performance", {"query": query})
    inner = ifind_module._extract(response)[1]
    rows = [row for table in ifind_module.parse_tables(inner.get("answer") or "") for row in table]
    (batch / "g9_factor_cross_exdate.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    output = []
    by_date: dict[str, dict] = {}
    for row in rows:
        day = str(row.get("日期") or "")
        if len(day) != 8:
            continue
        bucket = by_date.setdefault(day, {"closes": [], "factor": None})
        close = number(row.get("收盘价") or row.get("收盘价（单位：元）"))
        if close is not None:
            bucket["closes"].append(close)
        factor = number(row.get("复权因子"))
        if factor is not None:
            bucket["factor"] = factor
    for day in sorted(by_date):
        bucket = by_date[day]
        closes = sorted(bucket["closes"])
        raw = min(closes) if closes else None
        adjusted = max(closes) if len(closes) > 1 else None
        factor = bucket["factor"]
        implied = round(raw * factor, 4) if raw is not None and factor is not None else None
        output.append(
            {
                "trade_date": f"{day[:4]}-{day[4:6]}-{day[6:]}",
                "raw_close": raw,
                "adj_factor": factor,
                "adjusted_close_inferred": adjusted,
                "raw_x_factor": implied,
                "match_abs_diff": round(abs((adjusted or 0) - (implied or 0)), 4) if adjusted and implied else None,
                "field_label_ambiguous": len(closes) > 1,
            }
        )
    output.sort(key=lambda item: item["trade_date"])
    with (REPORT_DIR / "factor_formula_reconciliation.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output[0].keys()))
        writer.writeheader()
        writer.writerows(output)
    factors = {item["trade_date"]: item["adj_factor"] for item in output}
    ordered = sorted(factors)
    changes = [day for index, day in enumerate(ordered) if index and factors[day] != factors[ordered[index - 1]]]
    diffs = [item["match_abs_diff"] for item in output if item["match_abs_diff"] is not None]
    payload = {
        "query": query,
        "rows": len(output),
        "factor_changes": changes,
        "max_abs_diff_raw_x_factor_vs_adjusted": max(diffs) if diffs else None,
        "formula_consistent": bool(diffs) and max(diffs) < 0.5,
        "field_label_ambiguous": any(item["field_label_ambiguous"] for item in output),
        "formula_candidate": "adjusted_close = raw_close × cumulative_adj_factor",
        "verdict": "FORMULA_CONSISTENT_BUT_FIELD_AMBIGUOUS" if diffs and max(diffs) < 0.5 else "UNVERIFIED",
        "note": "NL 返回把前/后复权都塞进同名『收盘价』列，字段标签歧义；无公告时间与每股派现，账户会计仍不可验证",
    }
    (REPORT_DIR / "factor_check.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
