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
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    AuctionSnapshot,
    Base,
    CollectLog,
    LimitPool,
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
        db.commit()
    engine.dispose()


@pytest.fixture()
def audit_db(tmp_path):
    path = tmp_path / "audit.db"
    _build_db(path)
    return path


def test_baseline_audit_has_no_unexpected(audit_db):
    result = audit.run(audit_db, D1, D1, 1)
    assert result["matrix_version"] == "quality-consistency-matrix-v2"
    assert result["summary"]["unexpected_disagreements"] == 0
    assert result["summary"]["total_checks"] >= 4
    sampling = result["sampling"]
    assert sampling["strategy"].startswith("等间隔抽样")
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
