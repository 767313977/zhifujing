"""M6.5 Batch 5：导出 staging schema 草案。

从 `app.services.trend_engine.multisource.SCHEMA_SQL` 生成
`reports/trend_engine/m65/m65_staging_schema.sql`，保证代码与草案不漂移。

用法：

    python scripts/m65_write_schema_draft.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.trend_engine import multisource as ms  # noqa: E402

REPORT = ROOT / "reports/trend_engine/m65/m65_staging_schema.sql"


def main() -> int:
    header = (
        "-- M6.5 多供应商 staging schema 草案（自动生成，勿手改）\n"
        "-- 生成器：scripts/m65_write_schema_draft.py\n"
        "-- 落库位置：backend/data/m65_staging/<batch_id>/m65_multisource_staging.db\n"
        "-- 规则：只追加、按 provider 隔离、不写原始研究库\n\n"
    )
    REPORT.write_text(header + ms.SCHEMA_SQL.strip() + "\n", encoding="utf-8")
    print(f"written: {REPORT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
