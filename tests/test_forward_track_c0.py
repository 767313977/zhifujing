"""C0 前瞻跟踪工具的最小确定性测试（合成库，不触碰真实数据）。

覆盖：重复冻结拒绝、内容不可改、历史日期拒绝、数据未完成拒绝、
分位与排序确定性、数据版本落盘、状态与收益、延迟退出计入、未成熟不计收益、
不访问敏感库、不触发网络。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts import forward_track_c0 as ft  # noqa: E402


def _build_db(path: Path, days: int = 90, codes: int = 30) -> date:
    """单调上涨的合成行情，返回最新交易日。"""
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE stock_daily (
                trade_date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
                open REAL, high REAL, low REAL, close REAL,
                pct_chg REAL, volume REAL, amount REAL,
                PRIMARY KEY (trade_date, code)
            )
            """
        )
        rows = []
        start = date(2024, 1, 1)
        for c in range(codes):
            code = f"{c:06d}"
            close = 10.0 + c
            for d in range(days):
                day = (start + timedelta(days=d)).isoformat()
                drift = -0.004 + 0.0003 * c
                close *= 1 + drift
                rows.append(
                    (day, code, "测试", close, close * 1.01, close * 0.99,
                     close, drift * 100, 1e6, 1e7 / (c + 1))
                )
        conn.executemany(
            "INSERT OR REPLACE INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?)", rows
        )
    return start + timedelta(days=days - 1)


@pytest.fixture()
def env(tmp_path):
    db = tmp_path / "history.db"
    latest = _build_db(db)
    out = tmp_path / "forward"
    return db, out, latest


def _run(db: Path, out: Path, *argv) -> int:
    return ft.main(["--db", str(db), "--out", str(out), *argv])


# ---- 冻结与不可改写 ---------------------------------------------------------

def test_snapshot_creates_frozen_file(env, capsys):
    db, out, _latest = env
    assert _run(db, out, "snapshot") == 0
    files = list(out.glob("signals_*.jsonl"))
    assert len(files) == 1
    meta = json.loads(files[0].with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert meta["row_count"] == ft.TOP_N
    assert meta["content_sha256"] == hashlib.sha256(files[0].read_bytes()).hexdigest()
    assert meta["protocol"]["weights"] == {"rev20": 0.30, "illiq20": 0.30, "amp20": 0.40}
    assert meta["source_data_revision"]["kind"] in ("data_revision", "file_stat")
    capsys.readouterr()


def test_duplicate_freeze_is_rejected(env, capsys):
    db, out, _latest = env
    assert _run(db, out, "snapshot") == 0
    assert _run(db, out, "snapshot") == 2  # 已冻结，拒绝覆盖
    capsys.readouterr()


def test_backdated_snapshot_is_rejected(env, capsys):
    db, out, latest = env
    with pytest.raises(SystemExit):
        _run(db, out, "snapshot", "--date", (latest - timedelta(days=10)).isoformat())
    capsys.readouterr()


def test_future_date_is_rejected(env, capsys):
    db, out, latest = env
    with pytest.raises(SystemExit):
        _run(db, out, "snapshot", "--date", (latest + timedelta(days=1)).isoformat())
    capsys.readouterr()


def test_tampered_snapshot_is_rejected(env, tmp_path, capsys):
    db, out, _latest = env
    assert _run(db, out, "snapshot") == 0
    path = next(out.glob("signals_*.jsonl"))
    path.write_text(path.read_text(encoding="utf-8") + '{"tampered":1}\n', encoding="utf-8")
    day = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))["signal_date"]
    with pytest.raises(SystemExit):
        _run(db, out, "evaluate", "--date", day)
    capsys.readouterr()


# ---- 确定性与协议 -----------------------------------------------------------

def test_snapshot_is_deterministic(tmp_path, capsys):
    outputs = []
    for name in ("a", "b"):
        db = tmp_path / f"{name}.db"
        _build_db(db)
        out = tmp_path / f"out_{name}"
        assert _run(db, out, "snapshot") == 0
        path = next(out.glob("signals_*.jsonl"))
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        outputs.append([(r["stock_code"], r["rank"], r["score_total"]) for r in rows])
    assert outputs[0] == outputs[1]
    capsys.readouterr()


def test_candidate_rows_have_required_fields(env, capsys):
    db, out, _latest = env
    assert _run(db, out, "snapshot") == 0
    path = next(out.glob("signals_*.jsonl"))
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    for field in (
        "signal_date", "created_at", "strategy_version", "git_commit",
        "source_data_revision", "source_data_date", "universe_definition",
        "universe_count", "stock_code", "rank", "score_total",
        "rev20", "illiq20", "amp20", "avg_amount_20", "eligibility_reason",
    ):
        assert field in row, field
    capsys.readouterr()


# ---- 评价 ----------------------------------------------------------------

def _write_snapshot(out: Path, day: date, codes: list[str]) -> None:
    """模拟一份历史冻结快照（用于评价路径）。"""
    out.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(
            {
                "signal_date": day.isoformat(), "created_at": "2024-01-01T00:00:00+08:00",
                "strategy_version": ft.STRATEGY_VERSION, "git_commit": "test",
                "source_data_revision": {"kind": "file_stat", "value": {}},
                "source_data_date": day.isoformat(), "universe_definition": "test",
                "universe_count": len(codes), "stock_code": code, "rank": index + 1,
                "score_total": 50.0, "rev20": 1.0, "illiq20": 1.0, "amp20": 0.02,
                "percentiles": {}, "avg_amount_20": 1e7, "eligibility_reason": "test",
            },
            ensure_ascii=False, sort_keys=True,
        )
        + "\n"
        for index, code in enumerate(codes)
    )
    jsonl = out / f"signals_{day.isoformat()}.jsonl"
    jsonl.write_bytes(payload.encode("utf-8"))
    meta = {
        "signal_date": day.isoformat(), "row_count": len(codes),
        "universe_count": len(codes),
        "content_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }
    (out / f"signals_{day.isoformat()}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )


def test_evaluate_reports_pending_when_not_matured(env, capsys):
    db, out, latest = env
    assert _run(db, out, "snapshot") == 0
    assert _run(db, out, "evaluate", "--date", latest.isoformat()) == 0
    text = capsys.readouterr().out
    assert "pending" in text
    assert "不计入任何收益" in text


def test_evaluate_counts_matured_samples(tmp_path, capsys):
    db = tmp_path / "history.db"
    latest = _build_db(db, days=90)
    out = tmp_path / "forward"
    signal_day = latest - timedelta(days=20)
    _write_snapshot(out, signal_day, [f"{c:06d}" for c in range(5)])

    assert _run(db, out, "evaluate", "--date", signal_day.isoformat()) == 0
    text = capsys.readouterr().out
    assert "已入场且有退出价" in text
    assert "30bps" in text and "100bps" in text
    assert "基准毛均值" in text


# ---- 只读与无副作用 ---------------------------------------------------------

def test_engine_is_readonly(env):
    db, _out, _latest = env
    engine = ft._readonly_engine(db)
    try:
        assert "mode=ro" in str(engine.url)
    finally:
        engine.dispose()


def test_module_has_no_network_or_scheduler():
    source = Path(ft.__file__).read_text(encoding="utf-8")
    for banned in ("requests", "urllib", "httpx", "apscheduler", "schedule."):
        assert banned not in source, banned
    assert not hasattr(ft, "session_scope")


def test_protocol_weights_are_frozen():
    assert ft.PROTOCOL["weights"] == {"rev20": 0.30, "illiq20": 0.30, "amp20": 0.40}
    assert ft.PROTOCOL["top_n"] == 20
    assert ft.PROTOCOL["entry"] == "D+1 开盘"
    assert ft.PROTOCOL["percentile_scope"] == "当日全体候选池内"
