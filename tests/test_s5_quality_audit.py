"""S5-E：质量对账器的严格矩阵与负向 fixture。

覆盖：
- 严格状态映射矩阵（不再有「PARTIAL 时任何非 SOURCE_OK 都算过」的宽松条件）；
- 故意制造跨服务矛盾 → `unexpected` 非空、退出码非零；
- 抽样信息（策略 / 已抽日期 / 未覆盖日期 / 是否全历史）必须输出；
- 拒绝写模式数据库。

全部使用临时 SQLite，不连接真实库、不发外部请求。
"""

import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    AuctionSnapshot,
    Base,
    CollectLog,
    LimitPool,
    MarketSentiment,
    TradeCalendar,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_s5_quality_consistency.py"


def _load_audit():
    spec = importlib.util.spec_from_file_location("s5_quality_audit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


audit = _load_audit()

D1 = date(2026, 11, 3)
D2 = date(2026, 11, 4)
D3 = date(2026, 11, 5)


def _frame_payload(frame: str) -> str:
    return json.dumps(
        {
            "scheduled_frame": frame,
            "requested_codes": 1,
            "returned_codes": 1,
            "valid_codes": 1,
            "written_codes": 1,
            "quote_times_count": 1,
            "quote_times": {"600000": f"09:{frame[2:]}:00"},
        }
    )


def _build_db(path: Path, days=(D1,)) -> None:
    engine = create_engine(f"sqlite:///{path.as_posix()}", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([TradeCalendar(trade_date=day) for day in days])
        db.add(
            AuctionSnapshot(
                trade_date=D1,
                code="600000",
                price=10.0,
                samples=[["0926", 10.0, 1000.0]],
                post_open_price=10.2,
            )
        )
        db.add(
            CollectLog(
                trade_date=D1,
                task="auction_frame",
                status="ok",
                rows=1,
                message=_frame_payload("0926"),
            )
        )
        db.add(
            CollectLog(
                trade_date=D1,
                task="auction_frame",
                status="ok",
                rows=1,
                message=_frame_payload("0931"),
            )
        )
        db.add(
            LimitPool(
                trade_date=D1,
                code="600000",
                pool_type="up",
                name="合成银行",
                consecutive=1,
            )
        )
        # D 日样本池可信：三池都有成功日志；情绪样本才能拿到 600000
        db.add_all(
            [
                CollectLog(trade_date=D1, task=task, status="ok", rows=rows)
                for task, rows in (
                    ("pool_up", 1),
                    ("pool_broken", 0),
                    ("pool_down", 0),
                )
            ]
        )
        db.add(
            MarketSentiment(
                trade_date=D1,
                limit_up_count=1,
                broken_rate=0.0,
                max_consecutive=1,
                up_count=1,
                down_count=0,
            )
        )
        db.commit()
    engine.dispose()


@pytest.fixture()
def audit_db(tmp_path):
    path = tmp_path / "audit.db"
    _build_db(path)
    return path


def test_baseline_audit_has_no_unexpected(audit_db):
    result = audit.run(audit_db, D1, D1, 1)
    assert result["matrix_version"] == "quality-consistency-matrix-v3"
    assert result["summary"]["unexpected_disagreements"] == 0
    assert result["summary"]["not_comparable"] == 0
    assert result["summary"]["total_checks"] >= 4
    sampling = result["sampling"]
    assert sampling["strategy"].startswith("等距抽样")
    assert sampling["sampled_days"] == [D1.isoformat()]
    assert sampling["uncovered_days"] == []
    assert sampling["is_full_history"] is True
    assert "抽样对账不等于全历史完整验收" in sampling["disclaimer"]
    # 矩阵必须写进输出，便于复核
    assert "VERIFIED_SOURCE" in result["matrix"]["auction"]
    assert "ok" in result["matrix"]["pool"]["VERIFIED_SOURCE"]


def test_sampling_reports_uncovered_days(audit_db):
    path = audit_db.with_name("audit3.db")
    _build_db(path, days=(D1, D2, D3))
    result = audit.run(path, D1, D3, 2)
    sampling = result["sampling"]
    assert sampling["available_trade_days"] == 3
    assert len(sampling["sampled_days"]) == 2
    assert len(sampling["uncovered_days"]) == 1
    assert sampling["is_full_history"] is False
    assert sampling["coverage_ratio"] == round(2 / 3, 4)


def test_forced_cross_service_contradiction_is_unexpected_and_exits_nonzero(
    audit_db, tmp_path, monkeypatch, capsys
):
    """负向 fixture：故意让竞价服务与 overview 结论冲突 → unexpected + 退出码 2。"""
    monkeypatch.setattr(
        audit.auction_anomaly,
        "build_anomaly",
        lambda *_args, **_kwargs: {"quality": {"status": "INCOMPLETE"}},
    )
    out = tmp_path / "matrix.json"
    md = tmp_path / "report.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_s5_quality_consistency.py",
            "--db",
            str(audit_db),
            "--start",
            D1.isoformat(),
            "--end",
            D1.isoformat(),
            "--out",
            str(out),
            "--md",
            str(md),
            "--sample",
            "1",
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        audit.main()
    assert excinfo.value.code == 2

    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["summary"]["unexpected_disagreements"] == 1
    item = result["unexpected"][0]
    assert item["name"] == "overview.auction vs anomaly.quality"
    assert item["reason_code"] is None
    assert "INCOMPLETE" in item["detail"]
    text = md.read_text(encoding="utf-8")
    assert "未解释差异" in text
    assert "INCOMPLETE" in text
    capsys.readouterr()


def test_readonly_guard_rejects_write_mode(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_s5_quality_consistency.py",
            "--db",
            "backend/data/b1_research.db?mode=rw",
            "--start",
            D1.isoformat(),
            "--end",
            D1.isoformat(),
            "--out",
            str(tmp_path / "nope.json"),
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        audit.main()
    assert "拒绝写模式数据库" in str(excinfo.value)
    capsys.readouterr()


def test_explained_requires_reason_code():
    ok = audit._check("x", True)
    assert ok["explained"] is False and ok["reason_code"] is None
    bad = audit._check("x", False, detail="d")
    assert bad["explained"] is False and bad["reason_code"] is None
    explained = audit._check("x", False, detail="d", reason_code="WHY")
    assert explained["explained"] is True
    assert explained["reason_code"] == "WHY"


def _codes(count: int) -> list[str]:
    return [f"{600000 + index:06d}" for index in range(count)]


def _row(
    *,
    status="VERIFIED",
    sample=10,
    valid=10,
    missing=0,
    unverifiable=0,
    reasons=None,
    missing_codes=None,
    outcome=D2.isoformat(),
):
    sample_codes = _codes(sample)
    missing_codes = list(missing_codes or [])
    return {
        "outcome_date": outcome,
        "data_quality_status": status,
        "sample_count": sample,
        "valid_count": valid,
        "missing_count": missing,
        "unverifiable_count": unverifiable,
        "limit_up_again_count": 0 if unverifiable == 0 else None,
        "limit_up_again_ratio": 0.0 if unverifiable == 0 else None,
        "sample_codes": sample_codes,
        "valid_codes": [code for code in sample_codes if code not in missing_codes],
        "missing_codes": missing_codes,
        "data_quality_reasons": list(reasons or []),
    }


def _items(
    *,
    up_status=audit.dq.STATUS_VERIFIED,
    up_trusted=True,
    broken_status=audit.dq.STATUS_VERIFIED,
    broken_trusted=True,
    metric_status=audit.dq.STATUS_VERIFIED,
):
    return {
        "limit_pool": {
            "source": "limit_pool",
            "status": up_status,
            "pool_up": {"status": up_status, "trusted": up_trusted},
            "pool_broken": {"status": broken_status, "trusted": broken_trusted},
        },
        "market_sentiment": {"source": "market_sentiment", "status": metric_status},
    }


def _sentiment_checks(
    path: Path, monkeypatch, items, row, *, daily_status=None, d1_items=None
):
    monkeypatch.setattr(
        audit.snd, "build_next_day", lambda *_args, **_kwargs: {"series": [row]}
    )
    if daily_status is not None:
        monkeypatch.setattr(audit, "_daily_status", lambda *_args, **_kwargs: daily_status)
    if d1_items is not None:
        monkeypatch.setattr(audit, "_overview_items", lambda *_args, **_kwargs: d1_items)
    engine = create_engine(f"sqlite:///{path.as_posix()}", future=True)
    try:
        with Session(engine) as session:
            return audit._sentiment_checks(session, D1, items)
    finally:
        engine.dispose()


def _by_prefix(checks, prefix):
    return [check for check in checks if check["name"].startswith(prefix)]


def test_sample_pool_missing_is_not_attributed_to_d1_daily(audit_db, monkeypatch):
    """D 日样本池缺失导致 sample=0 时，只报上游依赖不可用，不比对 D+1 日线。"""
    row = _row(
        status="UNVERIFIED",
        sample=0,
        valid=0,
        reasons=["UP_POOL_MISSING", "METRIC_MISSING"],
    )
    items = _items(
        up_status=audit.dq.STATUS_NOT_COLLECTED,
        up_trusted=False,
        metric_status=audit.dq.STATUS_NOT_COLLECTED,
    )
    checks = _sentiment_checks(audit_db, monkeypatch, items, row, daily_status="VERIFIED_SOURCE")
    sample_check = _by_prefix(checks, "sentiment.sample_pool(D up)")[0]
    assert sample_check["result"] == "NOT_COMPARABLE"
    assert sample_check["reason_code"] == "SENTIMENT_SAMPLE_POOL_DEPENDENCY_UNRESOLVED"
    metric_check = _by_prefix(checks, "sentiment.metric")[0]
    assert metric_check["result"] == "NOT_COMPARABLE"
    assert metric_check["reason_code"] == "SENTIMENT_METRIC_DEPENDENCY_UNRESOLVED"
    # 没有样本就不能拿 D+1 日线来比较
    assert not _by_prefix(checks, "sentiment.d1_coverage")


def test_sample_pool_and_d1_daily_are_reported_separately(audit_db, monkeypatch):
    row = _row(status="PARTIAL", sample=10, valid=10, missing=0, reasons=["UP_POOL_UNVERIFIED"])
    items = _items(up_status=audit.dq.STATUS_UNVERIFIABLE, up_trusted=False)
    checks = _sentiment_checks(
        audit_db, monkeypatch, items, row, daily_status=audit.dq.STATUS_PARTIAL
    )
    sample_check = _by_prefix(checks, "sentiment.sample_pool(D up)")[0]
    assert sample_check["result"] == "NOT_COMPARABLE"
    coverage = _by_prefix(checks, "sentiment.d1_coverage")[0]
    assert coverage["result"] == "EXPLAINED"
    assert coverage["reason_code"] == "SENTIMENT_SAMPLE_SUBSET_DAILY_PARTIAL"


def test_d1_limit_pool_unverifiable_keeps_daily_coverage_pass(audit_db, monkeypatch):
    """D+1 涨停池不可信：日线覆盖 PASS，再次涨停指标单独 NOT_COMPARABLE。"""
    row = _row(
        status="PARTIAL",
        sample=10,
        valid=10,
        missing=0,
        unverifiable=10,
        reasons=["OUTCOME_LIMIT_POOL_UNVERIFIED"],
    )
    d1_items = _items(up_status=audit.dq.STATUS_UNVERIFIABLE, up_trusted=False)
    checks = _sentiment_checks(
        audit_db,
        monkeypatch,
        _items(),
        row,
        daily_status=audit.dq.STATUS_VERIFIED,
        d1_items=d1_items,
    )
    coverage = _by_prefix(checks, "sentiment.d1_coverage")[0]
    assert coverage["result"] == "PASS"
    limit_check = _by_prefix(checks, "sentiment.limit_up_again")[0]
    assert limit_check["result"] == "NOT_COMPARABLE"
    assert limit_check["reason_code"] == "OUTCOME_LIMIT_POOL_DEPENDENCY_UNRESOLVED"
    assert limit_check["diagnostics"]["unverifiable_count"] == 10


def test_missing_sample_row_is_not_comparable_and_lists_codes(audit_db, monkeypatch):
    """无 D+1 日线行且无停牌/交易资格证据：NOT_COMPARABLE，但必须定位到代码。"""
    row = _row(status="PARTIAL", sample=10, valid=9, missing=1, missing_codes=["600003"])
    checks = _sentiment_checks(
        audit_db, monkeypatch, _items(), row, daily_status=audit.dq.STATUS_VERIFIED
    )
    coverage = _by_prefix(checks, "sentiment.d1_coverage")[0]
    assert coverage["result"] == "NOT_COMPARABLE"
    assert coverage["reason_code"] == "D1_DAILY_ROW_MISSING_WITHOUT_TRADABILITY_EVIDENCE"
    assert coverage["diagnostics"]["missing_codes"] == ["600003"]
    assert coverage["diagnostics"]["missing_row_codes"] == ["600003"]
    assert coverage["diagnostics"]["missing_pct_codes"] == []


def test_missing_quote_without_tradability_evidence_is_not_comparable(audit_db, monkeypatch):
    row = _row(status="PARTIAL", sample=10, valid=9, missing=1, missing_codes=["600003"])
    checks = _sentiment_checks(
        audit_db, monkeypatch, _items(), row, daily_status=audit.dq.STATUS_PARTIAL
    )
    coverage = _by_prefix(checks, "sentiment.d1_coverage")[0]
    assert coverage["result"] == "NOT_COMPARABLE"
    assert coverage["reason_code"] == "D1_DAILY_ROW_MISSING_WITHOUT_TRADABILITY_EVIDENCE"
    # 不得擅自解释为停牌
    assert coverage["diagnostics"]["tradability_evidence"] is None


def test_null_pct_chg_is_reported_as_missing_pct_codes(audit_db, monkeypatch):
    engine = create_engine(f"sqlite:///{audit_db.as_posix()}", future=True)
    with Session(engine) as session:
        from app.models import StockDaily

        session.add(StockDaily(trade_date=D2, code="600003", open=10.0, close=10.0, pct_chg=None))
        session.commit()
    engine.dispose()
    row = _row(status="PARTIAL", sample=10, valid=9, missing=1, missing_codes=["600003"])
    checks = _sentiment_checks(
        audit_db, monkeypatch, _items(), row, daily_status=audit.dq.STATUS_PARTIAL
    )
    coverage = _by_prefix(checks, "sentiment.d1_coverage")[0]
    assert coverage["diagnostics"]["missing_row_codes"] == []
    assert coverage["diagnostics"]["missing_pct_codes"] == ["600003"]
    assert coverage["result"] == "NOT_COMPARABLE"
    assert coverage["reason_code"] == "D1_PCT_CHG_NULL_WITHOUT_TRADABILITY_EVIDENCE"


def test_null_pct_with_verified_daily_is_unexpected(audit_db, monkeypatch):
    """行存在、收盘价存在但 pct_chg 为空，与「全市场日线已验证」矛盾 → UNEXPECTED。"""
    engine = create_engine(f"sqlite:///{audit_db.as_posix()}", future=True)
    with Session(engine) as session:
        from app.models import StockDaily

        session.add(StockDaily(trade_date=D2, code="600003", open=10.0, close=10.0, pct_chg=None))
        session.commit()
    engine.dispose()
    row = _row(status="PARTIAL", sample=10, valid=9, missing=1, missing_codes=["600003"])
    checks = _sentiment_checks(
        audit_db, monkeypatch, _items(), row, daily_status=audit.dq.STATUS_VERIFIED
    )
    coverage = _by_prefix(checks, "sentiment.d1_coverage")[0]
    assert coverage["result"] == "UNEXPECTED"
    assert coverage["diagnostics"]["missing_pct_codes"] == ["600003"]


def test_daily_source_unavailable_is_not_comparable(audit_db, monkeypatch):
    row = _row(status="VERIFIED", sample=10, valid=10)
    for daily_status in (
        audit.dq.STATUS_SOURCE_ERROR,
        audit.dq.STATUS_NOT_COLLECTED,
        audit.dq.STATUS_UNVERIFIABLE,
    ):
        checks = _sentiment_checks(audit_db, monkeypatch, _items(), row, daily_status=daily_status)
        coverage = _by_prefix(checks, "sentiment.d1_coverage")[0]
        assert coverage["result"] == "NOT_COMPARABLE"
        assert coverage["reason_code"] == "D1_DAILY_SOURCE_UNAVAILABLE"


@pytest.mark.parametrize(
    ("metric_status", "expected"),
    [
        (audit.dq.STATUS_VERIFIED, "UNEXPECTED"),
        (audit.dq.STATUS_NOT_COLLECTED, "NOT_COMPARABLE"),
    ],
)
def test_metric_missing_is_checked_against_d_market_sentiment(
    audit_db, monkeypatch, metric_status, expected
):
    row = _row(status="PARTIAL", sample=10, valid=10, reasons=["METRIC_MISSING"])
    checks = _sentiment_checks(
        audit_db,
        monkeypatch,
        _items(metric_status=metric_status),
        row,
        daily_status=audit.dq.STATUS_VERIFIED,
    )
    metric_check = _by_prefix(checks, "sentiment.metric")[0]
    assert metric_check["result"] == expected


def test_not_comparable_keeps_cli_fail_closed(audit_db, tmp_path, monkeypatch, capsys):
    """NOT_COMPARABLE 不得计入 PASS，CLI 必须保持非零退出码。"""
    row = _row(status="PARTIAL", sample=10, valid=9, missing=1, missing_codes=["600003"])
    monkeypatch.setattr(
        audit.snd, "build_next_day", lambda *_args, **_kwargs: {"series": [row]}
    )
    monkeypatch.setattr(
        audit, "_daily_status", lambda *_args, **_kwargs: audit.dq.STATUS_PARTIAL
    )
    code, result = _run_cli(monkeypatch, audit_db, tmp_path)
    capsys.readouterr()
    assert code == 2
    assert result["summary"]["not_comparable"] >= 1
    item = next(i for i in result["not_comparable"] if i["name"].startswith("sentiment.d1_coverage"))
    assert item["diagnostics"]["missing_codes"] == ["600003"]
    assert "missing_row_codes" in item["detail"]
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "不可比 / 依赖未解决" in md
    assert "missing_row_codes" in md


def _run_cli(monkeypatch, db_path, out_dir, sample=1):
    out = out_dir / "matrix.json"
    md = out_dir / "report.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_s5_quality_consistency.py",
            "--db",
            str(db_path),
            "--start",
            D1.isoformat(),
            "--end",
            D1.isoformat(),
            "--out",
            str(out),
            "--md",
            str(md),
            "--sample",
            str(sample),
        ],
    )
    try:
        audit.main()
        code = 0
    except SystemExit as exc:  # CLI 用非零退出码标记未解释差异
        code = exc.code
    return code, json.loads(out.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "daily_status",
    [audit.dq.STATUS_SOURCE_ERROR, audit.dq.STATUS_NOT_COLLECTED],
)
def test_unverified_daily_unavailable_never_passes(audit_db, monkeypatch, daily_status):
    """SOURCE_ERROR / NOT_COLLECTED 时，即使 sentiment=VERIFIED 也不能通过。"""
    row = _row(status="VERIFIED", sample=10, valid=10)
    checks = _sentiment_checks(audit_db, monkeypatch, _items(), row, daily_status=daily_status)
    coverage = _by_prefix(checks, "sentiment.d1_coverage")[0]
    assert coverage["result"] == "NOT_COMPARABLE"


def test_theme_source_error_is_not_na(audit_db, tmp_path, monkeypatch, capsys):
    """sector_member 来源报错且当日无成员时，不得因缺 sector_code 直接判 N/A。"""
    real_sector_items = audit.dq._sector_member_items

    def _broken(*_args, **_kwargs):
        items = real_sector_items(*_args, **_kwargs)
        for item in items.values():
            item["status"] = audit.dq.STATUS_SOURCE_ERROR
        return items

    monkeypatch.setattr(audit.dq, "_sector_member_items", _broken)
    code, result = _run_cli(monkeypatch, audit_db, tmp_path)
    capsys.readouterr()
    assert code == 2
    names = [item["name"] for item in result["unexpected"]]
    assert "overview.sector_member vs theme-search" in names


@pytest.mark.parametrize(
    ("total", "sample"),
    [(21, 15), (29, 15), (21, 10), (15, 15), (15, 20), (1, 1)],
)
def test_sampling_covers_full_interval(total, sample):
    """等距抽样必须唯一、递增、数量正确，并在 sample>=2 时覆盖首尾。"""
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(total)]
    picked = audit._sample_days(days, sample)
    assert len(picked) == min(sample, total)
    assert picked == sorted(set(picked))
    if sample >= total:
        assert picked == days
    elif sample >= 2:
        assert picked[0] == days[0]
        assert picked[-1] == days[-1]


def test_sampling_selection_and_uncovered_are_disjoint_and_complete(audit_db, tmp_path):
    """selected ∪ uncovered = 全部交易日，且二者交集为空。"""
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(21)]
    path = tmp_path / "audit21.db"
    _build_db(path, days=tuple(days))
    result = audit.run(path, days[0], days[-1], 15)
    sampling = result["sampling"]
    sampled = sampling["sampled_days"]
    uncovered = sampling["uncovered_days"]
    assert len(sampled) == 15
    assert set(sampled) & set(uncovered) == set()
    assert set(sampled) | set(uncovered) == {day.isoformat() for day in days}
    assert sampled[0] == days[0].isoformat()
    assert sampled[-1] == days[-1].isoformat()
    assert sampling["is_full_history"] is False


@pytest.mark.parametrize("sample", ["0", "256"])
def test_invalid_sample_is_rejected(audit_db, tmp_path, monkeypatch, capsys, sample):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_s5_quality_consistency.py",
            "--db",
            str(audit_db),
            "--start",
            D1.isoformat(),
            "--end",
            D1.isoformat(),
            "--out",
            str(tmp_path / "nope.json"),
            "--sample",
            sample,
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        audit.main()
    assert "--sample 必须在 1~255" in str(excinfo.value)
    capsys.readouterr()

