"""只读导出 S4-A / S4-B 的真实历史证据（不写库、不发外部请求）。

- 3 个真实个股观察日案例：三桶计数、池状态、题材时点、D+1 后验；
- 2 个真实交易日的数据质量快照（含缺日志 / 部分覆盖）。

输出：`reports/s4_pit/real_cases.json`（默认）。
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

from app.services import data_quality as dq  # noqa: E402
from app.services import stock_research_context as ctx  # noqa: E402


def _pick_codes(session: Session, day: date) -> list[str]:
    rows = session.execute(
        text(
            "select code from limit_pool where trade_date = :day "
            "and pool_type = 'up' order by code limit 3"
        ),
        {"day": day},
    ).all()
    codes = [row[0] for row in rows]
    if len(codes) < 3:
        extra = session.execute(
            text("select code from stock_basic order by code limit :limit"),
            {"limit": 3 - len(codes)},
        ).all()
        codes += [row[0] for row in extra if row[0] not in codes]
    return codes[:3]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="backend/data/fupan.db")
    parser.add_argument("--day", default="2026-09-24")
    parser.add_argument("--out", default="reports/s4_pit/real_cases.json")
    args = parser.parse_args()

    db = Path(args.db).resolve()
    uri = "sqlite:///file:" + db.as_posix() + "?mode=ro&immutable=1&uri=true"
    engine = create_engine(uri, future=True)
    day = date.fromisoformat(args.day)

    with Session(engine) as session:
        codes = _pick_codes(session, day)
        cases = []
        for code in codes:
            data = ctx.build(session, code=code, as_of=day)
            cases.append(
                {
                    "code": code,
                    "name": data["name"],
                    "as_of": str(data["resolved_trade_date"]),
                    "cutoff": data["cutoff"],
                    "evidence_version": data["evidence_version"],
                    "evidence_counts": data["evidence_counts"],
                    "known": {
                        "pool_status": data["at_cutoff_known"]["pool_status"],
                        "board_height": data["at_cutoff_known"]["board_height"],
                        "daily_pct": (data["at_cutoff_known"]["daily"] or {}).get("pct_chg"),
                        "known_themes": [
                            item["name"] for item in data["at_cutoff_known"]["themes"]
                        ],
                        "known_concepts": [
                            item["name"] for item in data["at_cutoff_known"]["concepts"]
                        ],
                    },
                    "unknown": {
                        "themes": [
                            {"name": item["name"], "known_by": item["known_by"]}
                            for item in data["unknown_or_later"]["themes"]
                        ],
                        "concepts": [
                            {"name": item["name"], "known_by": item["known_by"]}
                            for item in data["unknown_or_later"]["concepts"]
                        ],
                        "reasons": data["unknown_or_later"]["reasons"],
                    },
                    "later_outcome": data["later_outcome"],
                    "historical_signal_eligible": data["historical_signal_eligible"],
                    "quality": data["quality"],
                }
            )

        quality = []
        for target in (day, date(2026, 9, 23)):
            overview = dq.overview(session, target_date=target, days=30)
            quality.append(
                {
                    "as_of": str(overview["as_of"]),
                    "database": overview["database"],
                    "items": [
                        {
                            "source": item["source"],
                            "status": item["status"],
                            "reason_codes": item["reason_codes"],
                            "observed_rows": item["observed_rows"],
                            "observed_sectors": item["observed_sectors"],
                            "expected_sectors": item["expected_sectors"],
                            "completion_proof": item["completion_proof"],
                            "latest_observation_date": (
                                str(item["latest_observation_date"])
                                if item["latest_observation_date"]
                                else None
                            ),
                            "latest_valid_snapshot": (
                                str(item["latest_valid_snapshot"])
                                if item["latest_valid_snapshot"]
                                else None
                            ),
                            "latest_attempt": item["latest_attempt"],
                        }
                        for item in overview["items"]
                    ],
                }
            )

    payload = {
        "database": db.name,
        "readonly": True,
        "pit_cases": cases,
        "quality_snapshots": quality,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"cases": len(cases), "quality": len(quality), "out": str(out)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
