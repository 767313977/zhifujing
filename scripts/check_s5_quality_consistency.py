"""S5-B：历史数据质量只读对账器（不写库、不外呼）。

用途：把 `/api/quality/overview` 的结论与各业务服务（竞价异动、晋级漏斗、
题材交叉、情绪次日）逐日对照，输出机器可读的契约矩阵与差异清单。

运行示例：

```powershell
.venv/Scripts/python.exe scripts/check_s5_quality_consistency.py `
  --db backend/data/b1_research.db --start 2026-09-01 --end 2026-09-30 `
  --out reports/s5_quality/contract_matrix.json
```

硬边界：
- 数据库一律 `mode=ro&immutable=1` 打开；拒绝 `?mode=rw` / `?mode=rwc`；
- 只 SELECT，不建表、不补采、不写缓存；
- 不调用任何 `sources/*`，因此不会发起外部请求；
- 差异分为 `explained`（已在质量协议中定义的已知差异）与 `unexpected`。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services import auction_anomaly  # noqa: E402
from app.services import data_quality as dq  # noqa: E402
from app.services import limit_funnel  # noqa: E402
from app.services import sentiment_next_day as snd  # noqa: E402
from app.services import theme_intersection as ti  # noqa: E402
from app.services.trading_days import previous_trade_day  # noqa: E402


def _readonly_uri(path: Path) -> str:
    resolved = path.resolve()
    if not resolved.exists():
        raise SystemExit(f"数据库不存在：{resolved}")
    return f"sqlite:///file:{resolved.as_posix()}?mode=ro&immutable=1&uri=true"


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001 - 只用于报告
        return None


def _check(name: str, ok: bool, *, detail: str | None = None, explained: bool = False) -> dict:
    return {
        "name": name,
        "ok": bool(ok),
        "explained": explained,
        "detail": detail,
    }


def _auction_checks(session: Session, day: date, item: dict) -> list[dict]:
    anomaly = auction_anomaly.build_anomaly(session, day, "all", "auction_amount", "desc", 1)
    anomaly_status = anomaly["quality"]["status"]
    status = item["status"]
    if status == dq.STATUS_VERIFIED:
        ok = anomaly_status == "VALID"
    elif status == dq.STATUS_NOT_COLLECTED:
        ok = anomaly_status == "NOT_COLLECTED"
    elif status == dq.STATUS_UNVERIFIABLE:
        ok = anomaly_status == "SOURCE_UNAVAILABLE"
    else:
        ok = anomaly_status in ("INCOMPLETE", "VALID")
    return [
        _check(
            "overview.auction vs anomaly.quality",
            ok,
            detail=f"overview={status}/{item.get('auction_result_status')}+"
            f"{item.get('post_open_status')}; anomaly={anomaly_status}",
            explained=(
                not ok
                and status == dq.STATUS_PARTIAL
                and anomaly_status == "INCOMPLETE"
            ),
        )
    ]


def _pool_checks(session: Session, day: date, item: dict) -> list[dict]:
    prev = previous_trade_day(day, session)
    inputs = limit_funnel.load_funnel_inputs(session, prev, day)
    funnel_status = inputs.today_up_quality.status
    status = item["status"]
    if status == dq.STATUS_VERIFIED:
        ok = funnel_status == limit_funnel.SOURCE_OK
    elif status == dq.STATUS_NOT_COLLECTED:
        ok = funnel_status == limit_funnel.SOURCE_NOT_COLLECTED
    elif status == dq.STATUS_PARTIAL:
        # 质量台保留「最近尝试失败前的有效快照」，漏斗按最新日志严格判定 → 允许不同
        ok = funnel_status != limit_funnel.SOURCE_OK
    else:
        ok = funnel_status != limit_funnel.SOURCE_OK
    return [
        _check(
            "overview.limit_pool.up vs funnel.today_up",
            ok,
            detail=f"overview={status}; funnel={funnel_status}",
            explained=(not ok and status == dq.STATUS_PARTIAL),
        )
    ]


def _theme_checks(session: Session, day: date, item: dict) -> list[dict]:
    sector_code = session.execute(
        text("SELECT sector_code FROM sector_member WHERE trade_date = :day LIMIT 1"),
        {"day": day},
    ).scalar()
    if not sector_code:
        return [_check("overview.sector_member vs theme-search", True, detail="N/A 无成员快照")]
    themes = ti.search(
        session, sector_codes=[sector_code], mode="AND", market="all", as_of=day, limit=1, offset=0
    )
    has_proof = bool(themes.get("coverage", {}).get("has_completion_proof"))
    ok = has_proof is False and item["completion_proof"] is False
    return [
        _check(
            "overview.sector_member vs theme-search",
            ok,
            detail=f"quality.completion_proof={item['completion_proof']}; "
            f"theme.has_completion_proof={has_proof}",
        )
    ]


def _sentiment_checks(session: Session, day: date, items: dict) -> list[dict]:
    series = snd.build_next_day(session, days=1, end_date=day)
    row = (series.get("series") or [None])[0]
    if not row:
        return [_check("overview.stock_daily vs sentiment-next-day", True, detail="N/A 无样本")]
    d1 = row.get("outcome_date")
    daily_status = _daily_status(session, d1) if d1 else None
    sentiment_status = row.get("data_quality_status")
    missing = int(row.get("missing_count") or 0)
    # 情绪次日只抽样其分组股票；全市场日线 PARTIAL（个别行缺字段）不构成矛盾，
    # 但「D+1 日线整体未采集」或「自称 VERIFIED 却有缺失样本」必须判矛盾。
    if sentiment_status == "VERIFIED" and d1:
        sample_ok = missing == 0
        daily_ok = daily_status in (dq.STATUS_VERIFIED, dq.STATUS_PARTIAL)
        ok = sample_ok and daily_ok
        # 仅当「抽样内无缺失、只是全市场个别行缺字段」时算已解释差异
        explained = (not ok) and sample_ok and daily_status == dq.STATUS_PARTIAL
    else:
        ok, explained = True, False
    return [
        _check(
            "overview.stock_daily vs sentiment-next-day",
            ok,
            detail=f"next_day={d1}; daily={daily_status}; sentiment={sentiment_status}; "
            f"missing={missing}",
            explained=(not ok and explained),
        )
    ]


def _daily_status(session: Session, day: date) -> str:
    items = dq.overview(session, target_date=day, days=30)["items"]
    return {item["source"]: item for item in items}["stock_daily"]["status"]


def run(db_path: Path, start: date, end: date, sample: int) -> dict:
    uri = _readonly_uri(db_path)
    engine = create_engine(uri, future=True)
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    started = time.perf_counter()
    checks: list[dict] = []
    with Session(engine) as session:
        days = [
            date.fromisoformat(str(row))
            for row in session.execute(
                text(
                    "SELECT trade_date FROM trade_calendar "
                    "WHERE trade_date BETWEEN :start AND :end ORDER BY trade_date"
                ),
                {"start": start, "end": end},
            ).scalars()
        ]
        if not days:
            raise SystemExit("区间内没有交易日")
        step = max(1, len(days) // max(1, sample))
        selected = days[::step][:sample]
        for day in selected:
            overview = dq.overview(session, target_date=day, days=30)
            items = {item["source"]: item for item in overview["items"]}
            day_checks = []
            day_checks.extend(_auction_checks(session, day, items["auction_snapshot"]))
            day_checks.extend(_pool_checks(session, day, items["limit_pool"]))
            day_checks.extend(_theme_checks(session, day, items["sector_member"]))
            day_checks.extend(_sentiment_checks(session, day, items))
            checks.append({"trade_date": day.isoformat(), "checks": day_checks})
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

    unexpected = [
        {"trade_date": day["trade_date"], **check}
        for day in checks
        for check in day["checks"]
        if not check["ok"] and not check["explained"]
    ]
    explained = [
        {"trade_date": day["trade_date"], **check}
        for day in checks
        for check in day["checks"]
        if not check["ok"] and check["explained"]
    ]
    return {
        "quality_version": dq.QUALITY_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "database": db_path.name,
        "mode": "ro&immutable=1",
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "sampled_days": len(checks),
        "sql_statements": len(statements),
        "elapsed_ms": elapsed_ms,
        "summary": {
            "total_checks": sum(len(day["checks"]) for day in checks),
            "passed": sum(1 for day in checks for check in day["checks"] if check["ok"]),
            "explained_disagreements": len(explained),
            "unexpected_disagreements": len(unexpected),
        },
        "days": checks,
        "explained": explained,
        "unexpected": unexpected,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="S5 数据质量只读对账（不写库）")
    parser.add_argument("--db", required=True, help="只读数据库文件路径")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--out", required=True, help="输出 JSON 路径")
    parser.add_argument("--sample", type=int, default=15, help="抽样交易日数（默认 15）")
    args = parser.parse_args()

    raw = args.db.lower()
    if "mode=rw" in raw or "mode=rwc" in raw:
        raise SystemExit("拒绝写模式数据库；只接受只读副本")
    if args.sample < 1 or args.sample > 255:
        raise SystemExit("--sample 必须在 1~255")

    result = run(Path(args.db), args.start, args.end, args.sample)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"days={result['sampled_days']} checks={result['summary']['total_checks']} "
        f"unexpected={result['summary']['unexpected_disagreements']} "
        f"sql={result['sql_statements']} elapsed_ms={result['elapsed_ms']}"
    )
    if result["summary"]["unexpected_disagreements"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
