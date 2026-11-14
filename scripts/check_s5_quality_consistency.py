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
import sqlite3
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


def _git_state() -> dict:
    """记录运行时的完整 HEAD、分支与工作区是否干净（便于证据回溯）。"""
    state: dict = {"sha": None, "branch": None, "dirty": None}
    try:
        state["sha"] = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        state["branch"] = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        state["dirty"] = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    except Exception:  # noqa: BLE001 - 只用于报告
        pass
    return state


def _node_version() -> str | None:
    try:
        return subprocess.run(
            ["node", "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - 只用于报告
        return None


def _check(
    name: str, ok: bool, *, detail: str | None = None, reason_code: str | None = None
) -> dict:
    """一条对账结果。

    严格口径：不匹配要归为 `explained`，**必须**给出具体 reason code；
    没有原因码的一律进 `unexpected`（不能靠宽松条件静默通过）。
    """
    return {
        "name": name,
        "ok": bool(ok),
        "explained": bool(not ok and reason_code),
        "reason_code": None if ok else reason_code,
        "detail": detail,
    }


#: 严格状态映射矩阵：overview 状态 → 其他服务允许的状态集合。
#: 不在此表里的组合一律 unexpected；`SOURCE_ERROR` 只能由 overview 自己声明，
#: 下游服务没有等价状态，因此必须用 `OVERVIEW_SOURCE_ERROR_DECLARED` 解释。
STATE_MATRIX: dict[str, dict[str, set[str]]] = {
    "auction": {
        dq.STATUS_VERIFIED: {"VALID"},
        dq.STATUS_PARTIAL: {"INCOMPLETE"},
        dq.STATUS_UNVERIFIABLE: {"SOURCE_UNAVAILABLE"},
        dq.STATUS_NOT_COLLECTED: {"NOT_COLLECTED"},
        dq.STATUS_SOURCE_ERROR: set(),
    },
    "pool": {
        dq.STATUS_VERIFIED: {limit_funnel.SOURCE_OK},
        dq.STATUS_PARTIAL: {
            limit_funnel.SOURCE_PARTIAL,
            limit_funnel.SOURCE_FAILED,
            limit_funnel.SOURCE_SKIPPED,
            limit_funnel.SOURCE_EMPTY,
        },
        dq.STATUS_UNVERIFIABLE: {
            limit_funnel.SOURCE_FAILED,
            limit_funnel.SOURCE_SKIPPED,
            limit_funnel.SOURCE_EMPTY,
            limit_funnel.SOURCE_UNKNOWN,
            limit_funnel.SOURCE_NOT_COLLECTED,
        },
        dq.STATUS_NOT_COLLECTED: {limit_funnel.SOURCE_NOT_COLLECTED},
        dq.STATUS_SOURCE_ERROR: set(),
    },
    "sentiment": {
        # 左侧是 D+1 `stock_daily` overview 状态，右侧是 sentiment_next_day 的
        # `data_quality_status` 词表（VERIFIED / PARTIAL / UNVERIFIED / PENDING）。
        # 唯一允许的「子集差异」不在此表：daily PARTIAL + sentiment VERIFIED 且
        # 抽样内 missing==0 / 有有效样本时，由 `SENTIMENT_SAMPLE_SUBSET_DAILY_PARTIAL`
        # 明确解释；其余组合一律 unexpected。
        dq.STATUS_VERIFIED: {"VERIFIED", "EMPTY"},
        dq.STATUS_PARTIAL: {"PARTIAL"},
        dq.STATUS_UNVERIFIABLE: {"UNVERIFIED"},
        dq.STATUS_NOT_COLLECTED: {"PENDING"},
        dq.STATUS_SOURCE_ERROR: set(),
    },
}


def _matrix_match(kind: str, overview_status: str, peer_status: str | None) -> bool:
    allowed = STATE_MATRIX[kind].get(overview_status)
    return peer_status is not None and allowed is not None and peer_status in allowed


def _auction_checks(session: Session, day: date, item: dict) -> list[dict]:
    anomaly = auction_anomaly.build_anomaly(session, day, "all", "auction_amount", "desc", 1)
    anomaly_status = anomaly["quality"]["status"]
    status = item["status"]
    ok = _matrix_match("auction", status, anomaly_status)
    reason = None
    if not ok and status == dq.STATUS_SOURCE_ERROR:
        reason = "OVERVIEW_SOURCE_ERROR_DECLARED"
    return [
        _check(
            "overview.auction vs anomaly.quality",
            ok,
            detail=f"overview={status}/{item.get('auction_result_status')}+"
            f"{item.get('post_open_status')}; anomaly={anomaly_status}",
            reason_code=reason,
        )
    ]


def _pool_checks(session: Session, day: date, item: dict) -> list[dict]:
    prev = previous_trade_day(day, session)
    inputs = limit_funnel.load_funnel_inputs(session, prev, day)
    funnel_status = inputs.today_up_quality.status
    status = item["status"]
    ok = _matrix_match("pool", status, funnel_status)
    reason = None
    if not ok and status == dq.STATUS_SOURCE_ERROR:
        reason = "OVERVIEW_SOURCE_ERROR_DECLARED"
    return [
        _check(
            "overview.limit_pool.up vs funnel.today_up",
            ok,
            detail=f"overview={status}; funnel={funnel_status}",
            reason_code=reason,
        )
    ]


def _theme_checks(session: Session, day: date, item: dict) -> list[dict]:
    status = item["status"]
    sector_code = session.execute(
        text("SELECT sector_code FROM sector_member WHERE trade_date = :day LIMIT 1"),
        {"day": day},
    ).scalar()
    if not sector_code:
        # N/A 只有在「来源查询成功且目标日确实没有成员快照」时才成立；
        # 来源自身报错（SOURCE_ERROR）时不得用 N/A 掩盖。
        if status in (dq.STATUS_NOT_COLLECTED, dq.STATUS_NOT_TRADE_DAY):
            return [
                _check(
                    "overview.sector_member vs theme-search",
                    True,
                    detail=f"N/A：来源状态={status} 且当日无成员快照",
                )
            ]
        return [
            _check(
                "overview.sector_member vs theme-search",
                False,
                detail=f"来源状态={status} 但当日无成员快照；N/A 不成立",
            )
        ]
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
    if not d1:
        # 区间末尾没有 D+1 交易日：没有可对照的日线日期，属于不可比而非矛盾。
        return [
            _check(
                "overview.stock_daily vs sentiment-next-day",
                True,
                detail="N/A 无 D+1 交易日，无法对照日线状态",
            )
        ]
    daily_status = _daily_status(session, d1)
    sentiment_status = row.get("data_quality_status")
    missing = int(row.get("missing_count") or 0)
    valid = int(row.get("valid_count") or 0)
    sample = int(row.get("sample_count") or 0)
    # 左侧是 D+1 全市场日线状态，右侧是情绪服务的抽样状态；两端不再是同一变量。
    # 「样本子集完整」是唯一允许的跨服务差异，且必须同时满足：
    # - 全市场日线 PARTIAL（个别股票缺字段）；
    # - 情绪侧抽查样本无缺失、有有效样本；
    # 任何一项不满足都必须进 unexpected。
    reason = None
    if daily_status == dq.STATUS_PARTIAL and sentiment_status == "VERIFIED":
        ok = False
        if missing == 0 and valid > 0 and sample > 0:
            reason = "SENTIMENT_SAMPLE_SUBSET_DAILY_PARTIAL"
    else:
        ok = _matrix_match("sentiment", daily_status, sentiment_status)
    return [
        _check(
            "overview.stock_daily vs sentiment-next-day",
            ok,
            detail=f"next_day={d1}; daily={daily_status}; sentiment={sentiment_status}; "
            f"sample={sample}; valid={valid}; missing={missing}",
            reason_code=reason,
        )
    ]


def _daily_status(session: Session, day: date) -> str:
    items = dq.overview(session, target_date=day, days=30)["items"]
    return {item["source"]: item for item in items}["stock_daily"]["status"]


def _sample_days(days: list[date], sample: int) -> list[date]:
    """覆盖整个区间的等距抽样。

    - `sample >= len(days)`：直接全量；
    - `sample == 1`：取区间首个交易日；
    - 其余：按 `(n-1)/(sample-1)` 均匀取索引，保证唯一、递增且覆盖首尾。
    """
    if sample >= len(days):
        return list(days)
    if sample == 1:
        return [days[0]]
    span = len(days) - 1
    indexes = [round(i * span / (sample - 1)) for i in range(sample)]
    return [days[index] for index in indexes]


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
        selected = _sample_days(days, sample)
        sampled_iso = [item.isoformat() for item in selected]
        sampled_set = set(sampled_iso)
        uncovered_iso = [item.isoformat() for item in days if item.isoformat() not in sampled_set]
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
        "matrix_version": "quality-consistency-matrix-v2",
        "matrix": {
            kind: {status: sorted(allowed) for status, allowed in mapping.items()}
            for kind, mapping in STATE_MATRIX.items()
        },
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "git": _git_state(),
        "command": " ".join(sys.argv),
        "environment": {
            "python": sys.version.split()[0],
            "node": _node_version(),
            "sqlite": sqlite3.sqlite_version,
        },
        "exit_code": 2 if unexpected else 0,
        "database": db_path.name,
        "mode": "ro&immutable=1",
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "sampled_days": len(checks),
        "sampling": {
            "strategy": (
                "等距抽样：--sample >= 区间交易日数时全量；否则按 (n-1)/(sample-1) "
                "均匀取索引，样本唯一递增并覆盖区间首尾"
            ),
            "requested_sample": sample,
            "available_trade_days": len(days),
            "sampled_days": sampled_iso,
            "uncovered_days": uncovered_iso,
            "coverage_ratio": round(len(checks) / len(days), 4),
            "is_full_history": len(checks) == len(days),
            "disclaimer": "抽样对账不等于全历史完整验收；未覆盖日期见 uncovered_days",
        },
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


def _render_markdown(result: dict) -> str:
    summary = result["summary"]
    sampling = result.get("sampling") or {}
    lines = [
        "# S5 数据质量对账（只读、抽样）",
        "",
        f"- 生成时间：{result['generated_at']}",
        f"- Git：`{(result.get('git') or {}).get('sha') or 'unknown'}`"
        f"（分支 `{(result.get('git') or {}).get('branch')}`，"
        f"工作区 dirty={(result.get('git') or {}).get('dirty')}）",
        f"- 命令：`{result.get('command')}`",
        f"- 环境：Python {result['environment']['python']} / "
        f"Node {result['environment']['node'] or 'N/A'} / "
        f"SQLite {result['environment']['sqlite']}",
        f"- 退出码：{result.get('exit_code')}",
        f"- 数据库：`{result['database']}`（{result['mode']}）",
        f"- 区间：{result['range']['start']} ~ {result['range']['end']}",
        f"- 矩阵版本：`{result.get('matrix_version')}`",
        "",
        "## 抽样口径",
        "",
        f"- 策略：{sampling.get('strategy')}",
        f"- 请求样本：{sampling.get('requested_sample')}；区间交易日："
        f"{sampling.get('available_trade_days')}；实际抽样：{result['sampled_days']}",
        f"- 已抽日期：{'、'.join(sampling.get('sampled_days') or []) or '无'}",
        f"- 覆盖率：{sampling.get('coverage_ratio')}；是否全历史：{sampling.get('is_full_history')}",
        f"- 说明：{sampling.get('disclaimer')}",
        "",
        "## 结果",
        "",
        f"- 检查总数：{summary['total_checks']}",
        f"- 通过：{summary['passed']}",
        f"- 已解释差异：{summary['explained_disagreements']}",
        f"- 未解释差异：{summary['unexpected_disagreements']}",
        f"- SQL：{result['sql_statements']}；耗时：{result['elapsed_ms']} ms",
        "",
        "## 未解释差异",
        "",
    ]
    if result["unexpected"]:
        for item in result["unexpected"]:
            lines.append(
                f"- {item['trade_date']} · {item['name']} · {item['detail']}"
            )
    else:
        lines.append("- 无（仅限本次抽样覆盖的日期）")
    lines.extend(["", "## 未覆盖日期（未参与本次对账）", ""])
    uncovered = sampling.get("uncovered_days") or []
    lines.append("- " + ("、".join(uncovered) if uncovered else "无（本次覆盖了区间内全部交易日）"))
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="S5 数据质量只读对账（不写库）")
    parser.add_argument("--db", required=True, help="只读数据库文件路径")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--out", required=True, help="输出 JSON 路径")
    parser.add_argument("--md", help="可选：同时输出 Markdown 摘要")
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
    if args.md:
        md_path = Path(args.md)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(_render_markdown(result), encoding="utf-8")
    print(
        f"days={result['sampled_days']} checks={result['summary']['total_checks']} "
        f"unexpected={result['summary']['unexpected_disagreements']} "
        f"sql={result['sql_statements']} elapsed_ms={result['elapsed_ms']}"
    )
    if result["summary"]["unexpected_disagreements"]:
        raise SystemExit(result["exit_code"])


if __name__ == "__main__":
    main()
