"""M6.6 Batch 1：生成 iFinD 工单发送包（不发送）。

从 M6.5 的 `ifind_vendor_question_matrix.csv` 派生 28 条问题清单，补充：
- required_evidence（该问题必须拿到什么才算答复）
- reply_status（初值 OPEN）
- gate_impact（影响哪个子门禁）

只写 `reports/trend_engine/m66/ifind_ticket_package/`，不发起任何请求。

用法：

    python scripts/m66_build_ifind_package.py
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "reports/trend_engine/m65/ifind_vendor_question_matrix.csv"
PACKAGE = ROOT / "reports/trend_engine/m66/ifind_ticket_package"

REQUIRED_EVIDENCE = {
    "A复权": "官方字段字典条目 + 同一样本原始 JSON（含请求参数）+ 因子与复权价计算示例",
    "B公司行为": "事件明细接口文档 + 一条真实事件的原生字段响应 + 字段单位与含税口径",
    "C板块": "接口文档 + 指定历史交易日截面响应样例 + 板块代码映射表",
    "D许可": "书面许可/合同条款原文或链接 + 配额计量说明 + 报价口径",
}

GATE_IMPACT = {
    "G9_price": "G9-PRICE",
    "G9_accounting": "G9-CA",
    "G9_pit": "G9-PIT",
    "G3": "G3",
    "G2": "G2",
    "ALL": "G9/G3",
}

FIELD_TEMPLATE_FIELDS = [
    ("adjust_type", "复权类型（RAW/FORWARD/BACKWARD）", "枚举", "A复权"),
    ("price_type", "价格类型字段名", "字符串", "A复权"),
    ("factor_name", "复权因子官方字段名", "字符串", "A复权"),
    ("factor_direction", "因子方向（前/后/单次/累计）", "枚举", "A复权"),
    ("factor_base_date", "因子基准日", "日期", "A复权"),
    ("factor_formula", "因子累计与价格还原公式", "公式文本", "A复权"),
    ("factor_revision_policy", "历史因子是否回溯修订", "文本", "A复权"),
    ("cash_per_share_pretax", "每股派现（税前）", "元/股", "B公司行为"),
    ("cash_per_share_posttax", "每股派现（税后）", "元/股", "B公司行为"),
    ("stock_dividend_ratio", "每股送股比例", "股/股", "B公司行为"),
    ("transfer_ratio", "每股转增比例", "股/股", "B公司行为"),
    ("rights_ratio", "配股比例", "股/股", "B公司行为"),
    ("rights_price", "配股价", "元/股", "B公司行为"),
    ("announcement_timestamp", "公告时间（是否到时分秒）", "datetime", "B公司行为"),
    ("record_date", "股权登记日", "日期", "B公司行为"),
    ("ex_date", "除权除息日", "日期", "B公司行为"),
    ("pay_date", "派息/到账日", "日期", "B公司行为"),
    ("share_listing_date", "红股上市日", "日期", "B公司行为"),
    ("plan_status", "方案状态枚举", "枚举", "B公司行为"),
    ("revision_flag", "修订/取消标识", "布尔/文本", "B公司行为"),
    ("sector_membership_api", "历史成分接口名", "字符串", "C板块"),
    ("sector_snapshot_date_param", "历史截面日期参数", "字符串", "C板块"),
    ("sector_in_out_fields", "纳入/剔除日期字段", "字符串", "C板块"),
    ("sector_code_mapping", "板块代码映射表", "文件/链接", "C板块"),
    ("storage_license", "本地持久化许可", "文本", "D许可"),
    ("derived_metrics_license", "衍生指标许可", "文本", "D许可"),
    ("website_license", "网站展示许可", "文本", "D许可"),
    ("quota_metering", "配额计量口径", "文本", "D许可"),
]


def main() -> int:
    PACKAGE.mkdir(parents=True, exist_ok=True)
    rows = list(csv.DictReader(SOURCE.open(encoding="utf-8")))
    if len(rows) != 28:
        raise SystemExit(f"M6.5 问题数不是 28，而是 {len(rows)}，先核对来源")

    out_rows = []
    for row in rows:
        section = row["section"]
        gate = row["blocking_gate"]
        out_rows.append(
            {
                "question_id": row["question_id"],
                "section": section,
                "topic": row["topic"],
                "question": row["question"],
                "blocking_gate": gate,
                "gate_impact": GATE_IMPACT.get(gate, "UNKNOWN"),
                "current_evidence": row["current_evidence"],
                "required_evidence": REQUIRED_EVIDENCE.get(section, "官方书面答复"),
                "expected_answer": row["expected_answer"],
                "priority": row["priority"],
                "reply_status": "OPEN",
                "reply_ref": "",
                "updated_at": "",
            }
        )

    target = PACKAGE / "ifind_28_questions.csv"
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(out_rows[0]))
        writer.writeheader()
        writer.writerows(out_rows)

    with (PACKAGE / "field_dictionary_template.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["field_code", "field_meaning", "unit_or_type", "section", "supplier_answer", "evidence_ref"])
        for code, meaning, unit, section in FIELD_TEMPLATE_FIELDS:
            writer.writerow([code, meaning, unit, section, "", ""])

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(SOURCE.relative_to(ROOT)),
        "questions": len(out_rows),
        "sections": sorted({row["section"] for row in out_rows}),
        "status": "DRAFT_READY_NOT_SENT",
        "contains_credentials": False,
        "contains_personal_data": False,
    }
    (PACKAGE / "package_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"questions={len(out_rows)} -> {target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
