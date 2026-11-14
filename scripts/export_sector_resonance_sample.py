"""只读导出一份「板块主线共振」样例（S4-C 验收证据）。

用途：
- `--list-sectors` 看某天哪些板块有成员快照（挑样例用）；
- 指定 `--sector-codes` 导出完整 JSON。

硬约束：只以 SQLite `mode=ro&immutable=1` 打开库，不建表、不写库、不发外部请求。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services import sector_resonance  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="backend/data/fupan.db")
    parser.add_argument("--date", default="2026-09-24")
    parser.add_argument("--sector-codes", default="")
    parser.add_argument("--mode", default="OR", choices=["AND", "OR"])
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--list-sectors", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    db = Path(args.db).resolve()
    uri = "sqlite:///file:" + db.as_posix() + "?mode=ro&immutable=1&uri=true"
    engine = create_engine(uri, future=True)
    with Session(engine) as session:
        if args.list_sectors:
            rows = session.execute(
                text(
                    "select m.sector_code, b.name, b.taxonomy, count(*) as c "
                    "from sector_member m join sector_basic b on b.code = m.sector_code "
                    "where m.trade_date = :day "
                    "group by m.sector_code order by c desc limit 30"
                ),
                {"day": args.date},
            ).all()
            print(
                json.dumps(
                    [
                        {
                            "code": row[0],
                            "name": row[1],
                            "taxonomy": row[2],
                            "members": row[3],
                        }
                        for row in rows
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return

        codes = [item for item in args.sector_codes.split(",") if item]
        data = sector_resonance.build(
            session,
            as_of=date.fromisoformat(args.date),
            sector_codes=codes,
            mode=args.mode,
            limit=args.limit,
        )
    payload = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(payload, encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
