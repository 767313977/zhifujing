"""S6.2：显式初始化 User DB（只建 10 张用户表）。

应用启动**不会**自动创建用户库（fail closed）；部署时由本脚本显式执行。
默认从环境变量读取路径（`USER_DB_PATH`，缺省 `DATA_DIR/user.db`）。

用法：
    python scripts/s6_init_user_db.py --dry-run
    python scripts/s6_init_user_db.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import get_settings  # noqa: E402
from app.db import init_user_db, user_schema_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正建表；默认 dry-run")
    parser.add_argument("--dry-run", action="store_true", help="显式 dry-run（默认行为）")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    args = parser.parse_args()
    if args.apply and args.dry_run:
        raise SystemExit("--apply 与 --dry-run 不能同时使用")

    settings = get_settings()
    target = settings.user_db_path
    before = user_schema_report()
    payload: dict = {
        "command": "python scripts/s6_init_user_db.py",
        "mode": "apply" if args.apply else "dry-run",
        "user_db": str(target),
        "exists_before": target.exists(),
        "missing_before": before["missing"],
    }

    if not args.apply:
        payload["action"] = "would_create_tables"
        payload["tables"] = before["tables"]
    else:
        result = init_user_db()
        after = user_schema_report()
        payload["action"] = "created"
        payload["tables"] = result["tables"]
        payload["ok"] = after["ok"]
        payload["missing_after"] = after["missing"]
        if not after["ok"]:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
