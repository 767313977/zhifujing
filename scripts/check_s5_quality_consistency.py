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

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.models import StockDaily  # noqa: E402
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
    name: str,
    ok: bool,
    *,
    detail: str | None = None,
    reason_code: str | None = None,
    comparable: bool = True,
    diagnostics: dict | None = None,
) -> dict:
    """一条对账结果。

    `result` 四分类（R3-P2-02）：
    - `PASS`：按协议一致；
    - `EXPLAINED`：不一致但有**具体、已批准**的 reason code；
    - `NOT_COMPARABLE`：缺少判断所需的依赖 / 历史资格证据，不计 PASS，也不自动解释；
    - `UNEXPECTED`：其余不一致。

    `NOT_COMPARABLE` 优先于 reason code：它表示「这条检查根本无法完成」，
    不能拿一个原因码把它洗成 EXPLAINED。
    """
    if ok:
        result = "PASS"
    elif not comparable:
        result = "NOT_COMPARABLE"
    elif reason_code:
        result = "EXPLAINED"
    else:
        result = "UNEXPECTED"
    return {
        "name": name,
        "ok": bool(ok),
        "explained": result == "EXPLAINED",
        "comparable": bool(comparable),
        "result": result,
        "reason_code": None if ok else reason_code,
        "detail": detail,
        "diagnostics": diagnostics,
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
        # v3 起不再用综合 `data_quality_status` 与 D+1 日线直接比较：
        # 情绪状态混合了 D 日样本池、D+1 日线、D+1 涨停池与 D 日情绪指标四个依赖，
        # 分别由 `sentiment.sample_pool` / `d1_coverage` / `limit_up_again` / `metric` 检查。
        # 此表仅保留历史 v2 对照，不再参与判定。
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


def _overview_items(session: Session, day: date) -> dict[str, dict]:
    return {
        item["source"]: item for item in dq.overview(session, target_date=day, days=30)["items"]
    }


def _dimension(item: dict | None, name: str) -> dict:
    return (item or {}).get(name) or {}


def _sample_pool_checks(
    session: Session, day: date, items: dict, reasons: list[str], sample: int
) -> list[dict]:
    """C2.1：D 日样本池依赖，单独于 D+1 日线判定。"""
    checks: list[dict] = []
    for prefix, dimension, label in (
        ("UP_POOL", "pool_up", "up"),
        ("BROKEN_POOL", "pool_broken", "broken"),
    ):
        matched = [reason for reason in reasons if reason.startswith(prefix)]
        if not matched:
            continue
        dim = _dimension(items.get("limit_pool"), dimension)
        trusted = bool(dim.get("trusted"))
        checks.append(
            _check(
                f"sentiment.sample_pool(D {label}) vs overview.limit_pool(D)",
                False,
                comparable=trusted,
                reason_code=None if trusted else "SENTIMENT_SAMPLE_POOL_DEPENDENCY_UNRESOLVED",
                detail=f"reasons={matched}; sample={sample}; "
                f"overview.{dimension}={dim.get('status')} trusted={trusted}",
                diagnostics={
                    "reasons": matched,
                    "sample_count": sample,
                    "overview_status": dim.get("status"),
                    "overview_trusted": trusted,
                },
            )
        )
    prev_matched = [
        reason for reason in reasons if reason.startswith(("PREV_UP_POOL", "RELAY"))
    ]
    if prev_matched:
        prev = previous_trade_day(day, session)
        prev_items = _overview_items(session, prev) if prev else {}
        dim = _dimension(prev_items.get("limit_pool"), "pool_up")
        trusted = bool(dim.get("trusted"))
        checks.append(
            _check(
                "sentiment.sample_pool(D-1 up) vs overview.limit_pool(D-1)",
                False,
                comparable=trusted,
                reason_code=None if trusted else "SENTIMENT_SAMPLE_POOL_DEPENDENCY_UNRESOLVED",
                detail=f"reasons={prev_matched}; sample={sample}; "
                f"overview.pool_up={dim.get('status')} trusted={trusted}",
                diagnostics={
                    "reasons": prev_matched,
                    "sample_count": sample,
                    "overview_status": dim.get("status"),
                    "overview_trusted": trusted,
                    "dependency_date": prev.isoformat() if prev else None,
                },
            )
        )
    if not checks:
        dim = _dimension(items.get("limit_pool"), "pool_up")
        checks.append(
            _check(
                "sentiment.sample_pool(D) vs overview.limit_pool(D)",
                True,
                detail=f"sample={sample}; overview.pool_up={dim.get('status')} "
                f"trusted={dim.get('trusted')}",
                diagnostics={
                    "sample_count": sample,
                    "overview_status": dim.get("status"),
                    "overview_trusted": bool(dim.get("trusted")),
                },
            )
        )
    return checks


def _d1_coverage_check(session: Session, day: date, d1: date, row: dict) -> dict:
    """C2.2：D+1 日线覆盖率，必须输出缺失股票代码级别的诊断。"""
    name = "sentiment.d1_coverage vs overview.stock_daily(D+1)"
    daily_status = _daily_status(session, d1)
    sample = int(row.get("sample_count") or 0)
    valid = int(row.get("valid_count") or 0)
    missing = int(row.get("missing_count") or 0)
    sample_codes = list(row.get("sample_codes") or [])
    missing_codes = sorted(row.get("missing_codes") or [])
    row_codes: set[str] = set()
    pct_by_code: dict[str, float | None] = {}
    if missing_codes:
        rows = session.execute(
            select(StockDaily.code, StockDaily.pct_chg).where(
                StockDaily.trade_date == d1, StockDaily.code.in_(missing_codes)
            )
        ).all()
        row_codes = {code for code, _ in rows}
        pct_by_code = {code: pct for code, pct in rows}
    missing_row_codes = [code for code in missing_codes if code not in row_codes]
    missing_pct_codes = [
        code for code in missing_codes if code in row_codes and pct_by_code.get(code) is None
    ]
    diagnostics = {
        "sample_date": day.isoformat(),
        "outcome_date": d1.isoformat(),
        "sample_count": sample,
        "valid_count": valid,
        "missing_count": missing,
        "sample_codes": sample_codes,
        "valid_codes": list(row.get("valid_codes") or []),
        "missing_codes": missing_codes,
        "missing_row_codes": missing_row_codes,
        "missing_pct_codes": missing_pct_codes,
        "sentiment_status": row.get("data_quality_status"),
        "daily_status": daily_status,
        "data_quality_reasons": list(row.get("data_quality_reasons") or []),
        # 本地没有历史停牌 / 可交易资格数据：不得擅自解释为停牌
        "tradability_evidence": None,
    }
    detail = (
        f"D+1={d1}; daily={daily_status}; sentiment={row.get('data_quality_status')}; "
        f"sample={sample}; valid={valid}; missing={missing}; "
        f"missing_row_codes={missing_row_codes}; missing_pct_codes={missing_pct_codes}"
    )
    if daily_status in (
        dq.STATUS_SOURCE_ERROR,
        dq.STATUS_UNVERIFIABLE,
        dq.STATUS_NOT_COLLECTED,
    ):
        return _check(
            name,
            False,
            comparable=False,
            reason_code="D1_DAILY_SOURCE_UNAVAILABLE",
            detail=detail,
            diagnostics=diagnostics,
        )
    if missing == 0 and valid == sample:
        if daily_status == dq.STATUS_PARTIAL:
            return _check(
                name,
                False,
                reason_code="SENTIMENT_SAMPLE_SUBSET_DAILY_PARTIAL",
                detail=detail,
                diagnostics=diagnostics,
            )
        return _check(name, True, detail=detail, diagnostics=diagnostics)
    # 完全没有 D+1 日线行：本地没有历史停牌 / 可交易资格数据，无法判断是
    # 「当日不应交易」还是「采集缺失」——不得擅自解释为停牌，也不得直接判采集错误。
    if missing_row_codes:
        return _check(
            name,
            False,
            comparable=False,
            reason_code="D1_DAILY_ROW_MISSING_WITHOUT_TRADABILITY_EVIDENCE",
            detail=detail,
            diagnostics=diagnostics,
        )
    if missing_pct_codes:
        if daily_status == dq.STATUS_VERIFIED:
            # 行存在、收盘价也在，但 pct_chg 为空；全市场日线却声称已验证 → 矛盾
            return _check(name, False, detail=detail, diagnostics=diagnostics)
        return _check(
            name,
            False,
            comparable=False,
            reason_code="D1_PCT_CHG_NULL_WITHOUT_TRADABILITY_EVIDENCE",
            detail=detail,
            diagnostics=diagnostics,
        )
    return _check(
        name,
        False,
        comparable=False,
        reason_code="SENTIMENT_SAMPLE_QUOTES_UNVERIFIABLE",
        detail=detail,
        diagnostics=diagnostics,
    )


def _sentiment_checks(session: Session, day: date, items: dict) -> list[dict]:
    """R3-P2-02：按依赖维度拆开情绪质量，不再用一个综合状态对照 D+1 日线。"""
    series = snd.build_next_day(
        session, days=1, end_date=day, include_code_details=True
    )
    row = (series.get("series") or [None])[0]
    if not row:
        return [
            _check(
                "sentiment.sample_pool(D) vs overview.limit_pool(D)",
                True,
                detail="N/A 无样本行",
            )
        ]

    reasons = list(row.get("data_quality_reasons") or [])
    sample = int(row.get("sample_count") or 0)
    unverifiable = int(row.get("unverifiable_count") or 0)
    d1 = row.get("outcome_date")
    if isinstance(d1, str):
        d1 = date.fromisoformat(d1)

    checks = _sample_pool_checks(session, day, items, reasons, sample)

    # C2.4：D 日情绪指标缺失要对 D 日 market_sentiment 负责，而不是 D+1 日线
    if "METRIC_MISSING" in reasons:
        metric_status = (items.get("market_sentiment") or {}).get("status")
        trusted = metric_status == dq.STATUS_VERIFIED
        checks.append(
            _check(
                "sentiment.metric(D) vs overview.market_sentiment(D)",
                False,
                comparable=trusted,
                reason_code=None if trusted else "SENTIMENT_METRIC_DEPENDENCY_UNRESOLVED",
                detail=f"METRIC_MISSING; overview.market_sentiment={metric_status}",
                diagnostics={
                    "reasons": reasons,
                    "overview_status": metric_status,
                    "overview_trusted": trusted,
                },
            )
        )

    # C2.2：D+1 日线覆盖（sample=0 时无法评估，交由样本池检查）
    if sample > 0 and d1:
        checks.append(_d1_coverage_check(session, day, d1, row))

    # C2.3：D+1 涨停池只影响「是否再次涨停」，不影响日线覆盖
    if "OUTCOME_LIMIT_POOL_UNVERIFIED" in reasons or unverifiable > 0:
        up = _dimension(
            _overview_items(session, d1).get("limit_pool") if d1 else None, "pool_up"
        )
        trusted = bool(up.get("trusted"))
        checks.append(
            _check(
                "sentiment.limit_up_again vs overview.limit_pool(D+1)",
                False,
                comparable=trusted,
                reason_code=None if trusted else "OUTCOME_LIMIT_POOL_DEPENDENCY_UNRESOLVED",
                detail=f"unverifiable={unverifiable}; D+1 up={up.get('status')} "
                f"trusted={trusted}; limit_up_again="
                f"{row.get('limit_up_again_count')}",
                diagnostics={
                    "unverifiable_count": unverifiable,
                    "limit_up_again_count": row.get("limit_up_again_count"),
                    "limit_up_again_ratio": row.get("limit_up_again_ratio"),
                    "reasons": reasons,
                    "overview_status": up.get("status"),
                    "overview_trusted": trusted,
                },
            )
        )
    return checks


def _daily_status(session: Session, day: date) -> str:
    return _overview_items(session, day)["stock_daily"]["status"]


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
        if check["result"] == "UNEXPECTED"
    ]
    explained = [
        {"trade_date": day["trade_date"], **check}
        for day in checks
        for check in day["checks"]
        if check["result"] == "EXPLAINED"
    ]
    not_comparable = [
        {"trade_date": day["trade_date"], **check}
        for day in checks
        for check in day["checks"]
        if check["result"] == "NOT_COMPARABLE"
    ]
    return {
        "quality_version": dq.QUALITY_VERSION,
        "matrix_version": "quality-consistency-matrix-v3",
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
        # NOT_COMPARABLE / 未解决上游依赖同样 fail closed
        "exit_code": 2 if (unexpected or not_comparable) else 0,
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
            "passed": sum(
                1 for day in checks for check in day["checks"] if check["result"] == "PASS"
            ),
            "explained_disagreements": len(explained),
            "not_comparable": len(not_comparable),
            "unexpected_disagreements": len(unexpected),
        },
        "days": checks,
        "explained": explained,
        "not_comparable": not_comparable,
        "unexpected": unexpected,
    }


def _render_diagnostics(item: dict) -> str:
    """把关键诊断字段压成一行，确保 Markdown 也能定位代码与原因。"""
    diag = item.get("diagnostics") or {}
    keys = (
        "sample_count",
        "valid_count",
        "missing_count",
        "missing_codes",
        "missing_row_codes",
        "missing_pct_codes",
        "unverifiable_count",
        "data_quality_reasons",
        "daily_status",
        "overview_status",
        "overview_trusted",
    )
    compact = {key: diag[key] for key in keys if diag.get(key) is not None}
    if not compact:
        return ""
    return " · `" + json.dumps(compact, ensure_ascii=False) + "`"


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
        f"- 不可比 / 依赖未解决：{summary['not_comparable']}",
        f"- 未解释差异：{summary['unexpected_disagreements']}",
        f"- SQL：{result['sql_statements']}；耗时：{result['elapsed_ms']} ms",
        "",
        "## 不可比 / 依赖未解决（不计 PASS）",
        "",
    ]
    if result["not_comparable"]:
        for item in result["not_comparable"]:
            lines.append(
                f"- {item['trade_date']} · {item['name']} · "
                f"{item['reason_code']} · {item['detail']}"
                f"{_render_diagnostics(item)}"
            )
    else:
        lines.append("- 无")
    lines.extend(["", "## 已解释差异", ""])
    if result["explained"]:
        for item in result["explained"]:
            lines.append(
                f"- {item['trade_date']} · {item['name']} · "
                f"{item['reason_code']} · {item['detail']}"
                f"{_render_diagnostics(item)}"
            )
    else:
        lines.append("- 无")
    lines.extend(["", "## 未解释差异", ""])
    if result["unexpected"]:
        for item in result["unexpected"]:
            lines.append(
                f"- {item['trade_date']} · {item['name']} · {item['detail']}"
                f"{_render_diagnostics(item)}"
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
    if result["summary"]["unexpected_disagreements"] or result["summary"]["not_comparable"]:
        raise SystemExit(result["exit_code"])


if __name__ == "__main__":
    main()
