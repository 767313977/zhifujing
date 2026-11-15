"""M6.7 N6 / Batch 1：生成 iFinD 28 问 v2 工单包与逐问状态表（不发送）。

从 M6.6 的 `ifind_ticket_package/ifind_28_questions.csv` 派生：
- 保留原编号与问题文本；
- 增加 `closure_gap`（当前缺什么证据）；
- 增加 `reply_status`（沿用枚举，初值 OPEN）与 `reply_ref`、`updated_at`；
- 输出 v2 目录与状态表，不改动 M6.6 原件。

用法：

    python scripts/m67_build_ifind_v2.py
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "reports/trend_engine/m66/ifind_ticket_package/ifind_28_questions.csv"
REPORT_DIR = ROOT / "reports/trend_engine/m67"
V2_DIR = REPORT_DIR / "01-ifind-ticket-package-v2"

CLOSURE_GAP = {
    "A复权": "缺官方字段字典与 adjust_type；前/后复权仅能靠措辞区分",
    "B公司行为": "字段 schema 存在但分红/送转值为空；无公告时间与方案状态",
    "C板块": "sector_data 仅返回当前成分，日期参数被忽略",
    "D许可": "无书面许可；额度无官方用量 API",
}


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    V2_DIR.mkdir(parents=True, exist_ok=True)
    rows = list(csv.DictReader(SOURCE.open(encoding="utf-8")))
    if len(rows) != 28:
        raise SystemExit(f"预期 28 条，实际 {len(rows)}")

    now = datetime.now(timezone.utc).isoformat()
    status_rows = []
    for row in rows:
        status_rows.append(
            {
                "question_id": row["question_id"],
                "section": row["section"],
                "topic": row["topic"],
                "gate_impact": row.get("gate_impact", ""),
                "required_evidence": row.get("required_evidence", ""),
                "closure_gap": CLOSURE_GAP.get(row["section"], ""),
                "reply_status": row.get("reply_status") or "OPEN",
                "reply_ref": row.get("reply_ref", ""),
                "updated_at": row.get("updated_at", "") or now,
            }
        )

    with (V2_DIR / "ifind_28_questions_v2.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(status_rows[0]))
        writer.writeheader()
        writer.writerows(status_rows)

    with (REPORT_DIR / "01-ifind-question-status.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(status_rows[0]))
        writer.writeheader()
        writer.writerows(status_rows)

    manifest = {
        "generated_at": now,
        "source": str(SOURCE.relative_to(ROOT)),
        "questions": len(status_rows),
        "status": "DRAFT_NOT_SENT",
        "contains_credentials": False,
        "contains_local_absolute_path": False,
        "reuses_m66_numbering": True,
        "open_questions": sum(1 for item in status_rows if item["reply_status"] == "OPEN"),
    }
    (V2_DIR / "package_manifest_v2.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (V2_DIR / "README.md").write_text(
        "# iFinD 工单包 v2（M6.7）\n\n"
        "- 状态：`DRAFT_NOT_SENT`（TRAE 未发送，也不会声称已发送）\n"
        "- 编号沿用 M6.6 的 28 条，未合并、未重排\n"
        "- 新增 `closure_gap`：该问题当前缺什么证据\n"
        "- 正文见 `../06-ifind-28-question-ticket-v2.md`\n"
        "- M6.6 原件保持在 `reports/trend_engine/m66/ifind_ticket_package/`，未修改\n",
        encoding="utf-8",
    )
    print(json.dumps({"questions": len(status_rows), "open": manifest["open_questions"], "dir": str(V2_DIR.relative_to(ROOT))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
