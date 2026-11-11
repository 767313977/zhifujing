"""C0 前瞻跟踪工具的最小确定性测试（合成库，不触碰真实数据）。

覆盖本轮验收要求：
- 数据过期时拒绝正式快照；收盘前拒绝；数据未完成拒绝；无交易日历拒绝；
- 历史重放不得标记为前瞻；重放不得被前瞻评价；
- 候选池 / 因子值 / 分位 / 评分 / 排名与历史回测 C0 完全一致；
- 同日并发创建只成功一次；写入中途失败不产生有效快照；
- 信号与元数据完整性；不访问敏感库、不触发网络与交易。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from app.services.research_backtest import load_matrix  # noqa: E402
from scripts import backtest_reversal as bt  # noqa: E402
from scripts import forward_track_c0 as ft  # noqa: E402

START = date(2024, 1, 1)


def _build_db(
    path: Path,
    days: int = 90,
    codes: int = 30,
    *,
    calendar: bool = True,
    future_days: int = 5,
    thin_last_day: int | None = None,
) -> date:
    """单调漂移的合成行情，返回最新交易日。

    `future_days` 让日历比行情多铺几天，模拟「次日仍是交易日」；
    `thin_last_day` 让最后一晚只剩 N 只票，模拟采集未完成。
    """
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
        if calendar:
            conn.execute("CREATE TABLE trade_calendar (trade_date TEXT PRIMARY KEY)")
        rows = []
        for c in range(codes):
            code = f"{c:06d}"
            close = 10.0 + c
            for d in range(days):
                day = (START + timedelta(days=d)).isoformat()
                drift = -0.004 + 0.0003 * c
                close *= 1 + drift
                if thin_last_day is not None and d == days - 1 and c >= thin_last_day:
                    continue
                rows.append(
                    (day, code, "测试", close, close * 1.01, close * 0.99,
                     close, drift * 100, 1e6, 1e7 / (c + 1))
                )
        conn.executemany(
            "INSERT OR REPLACE INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?)", rows
        )
        if calendar:
            days_all = [
                (START + timedelta(days=d)).isoformat()
                for d in range(days + future_days)
            ]
            conn.executemany(
                "INSERT OR REPLACE INTO trade_calendar VALUES (?)",
                [(item,) for item in days_all],
            )
    return START + timedelta(days=days - 1)


@pytest.fixture()
def env(tmp_path):
    db = tmp_path / "history.db"
    latest = _build_db(db)
    out = tmp_path / "forward"
    return db, out, latest


def _run(db: Path, out: Path, *argv) -> int:
    return ft.main(["--db", str(db), "--out", str(out), *argv])


def _now(day: date, hour: int, minute: int) -> str:
    return f"{day.isoformat()}T{hour:02d}:{minute:02d}:00+08:00"


# ---- 历史重放 ---------------------------------------------------------------


def test_replay_snapshot_is_labeled_replay_only(env, capsys):
    db, out, latest = env
    assert _run(db, out, "snapshot", "--mode", "replay") == 0
    path = out / f"replay_signals_{latest.isoformat()}.jsonl"
    assert path.is_file()
    meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert meta["mode"] == "REPLAY_ONLY"
    assert meta["counts_for_forward_performance"] is False
    assert meta["row_count"] == ft.TOP_N
    assert meta["content_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "replay_note" in meta
    text = capsys.readouterr().out
    assert "REPLAY_ONLY" in text


def test_replay_may_use_historical_date(env, capsys):
    db, out, latest = env
    older = (latest - timedelta(days=10)).isoformat()
    assert _run(db, out, "snapshot", "--mode", "replay", "--date", older) == 0
    assert (out / f"replay_signals_{older}.jsonl").is_file()
    capsys.readouterr()


def test_replay_cannot_be_evaluated_as_forward(env, capsys):
    db, out, latest = env
    assert _run(db, out, "snapshot", "--mode", "replay") == 0
    with pytest.raises(SystemExit):
        _run(db, out, "evaluate", "--date", latest.isoformat(), "--mode", "forward")
    capsys.readouterr()


def test_replay_does_not_create_forward_file(env, capsys):
    db, out, latest = env
    assert _run(db, out, "snapshot", "--mode", "replay") == 0
    assert not (out / f"signals_{latest.isoformat()}.jsonl").exists()
    capsys.readouterr()


# ---- live 闸门 --------------------------------------------------------------


def test_live_snapshot_passes_all_gates(env, capsys):
    db, out, latest = env
    code = _run(db, out, "snapshot", "--mode", "forward", "--now", _now(latest, 15, 45))
    assert code == 0
    path = out / f"signals_{latest.isoformat()}.jsonl"
    meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert meta["mode"] == "FORWARD_FROZEN"
    assert meta["counts_for_forward_performance"] is True
    assert meta["completeness"]["rows"] == 30
    assert meta["completeness"]["coverage_ratio"] == pytest.approx(1.0)
    assert meta["completeness"]["factor_missing_ratio"] == pytest.approx(0.0)
    capsys.readouterr()


def test_live_rejects_before_close(env, capsys):
    db, out, latest = env
    assert _run(db, out, "snapshot", "--mode", "forward", "--now", _now(latest, 14, 0)) == 3
    assert not list(out.glob("signals_*.jsonl"))
    assert "早于 D 日 15:30" in capsys.readouterr().err


def test_live_rejects_stale_data(env, capsys):
    db, out, latest = env
    # 今天已经是 10 天后，库里最新还是老日期
    assert _run(db, out, "snapshot", "--mode", "forward",
                "--now", _now(latest + timedelta(days=10), 16, 0)) == 3
    assert not list(out.glob("signals_*.jsonl"))
    assert "拒绝正式前瞻冻结" in capsys.readouterr().err


def test_live_rejects_after_next_open(env, capsys):
    db, out, latest = env
    # D 日收盘后但已跨过次一交易日 09:15：不再是「开盘前」
    assert _run(db, out, "snapshot", "--mode", "forward",
                "--date", latest.isoformat(),
                "--now", _now(latest + timedelta(days=1), 10, 0)) == 3
    assert "开盘前" in capsys.readouterr().err


def test_live_rejects_incomplete_day(tmp_path, capsys):
    db = tmp_path / "history.db"
    latest = _build_db(db, thin_last_day=5)  # 最后一天只有 5 只
    out = tmp_path / "forward"
    code = _run(db, out, "snapshot", "--mode", "forward", "--now", _now(latest, 15, 45))
    assert code == 3
    assert not list(out.glob("signals_*.jsonl"))
    assert "覆盖" in capsys.readouterr().err


def test_live_rejects_without_trade_calendar(tmp_path, capsys):
    """没有获准的交易日历 = 无法确认 D 是交易日 → 拒绝启动。"""
    db = tmp_path / "history.db"
    latest = _build_db(db, calendar=False)
    out = tmp_path / "forward"
    code = _run(db, out, "snapshot", "--mode", "forward", "--now", _now(latest, 15, 45))
    assert code == 3
    assert not list(out.glob("signals_*.jsonl"))
    assert "交易日历" in capsys.readouterr().err


def test_live_writes_nothing_on_reject(env, capsys):
    db, out, latest = env
    _run(db, out, "snapshot", "--mode", "forward", "--now", _now(latest, 10, 0))
    assert not out.exists() or not any(out.iterdir())
    capsys.readouterr()


# ---- 并发与原子性 -----------------------------------------------------------


def test_concurrent_publish_only_one_succeeds(tmp_path):
    jsonl = tmp_path / "signals_2024-03-30.jsonl"
    meta = tmp_path / "signals_2024-03-30.meta.json"
    first = ft._publish_pair(b"a\n", b"{}", jsonl, meta)
    second = ft._publish_pair(b"b\n", b"{}", jsonl, meta)
    assert first is True
    assert second is False
    assert jsonl.read_bytes() == b"a\n"  # 绝不被第二次覆盖


def test_partial_write_leaves_no_valid_snapshot(tmp_path, monkeypatch):
    jsonl = tmp_path / "signals_2024-03-30.jsonl"
    meta = tmp_path / "signals_2024-03-30.meta.json"
    real = ft._publish
    calls = {"n": 0}

    def flaky(tmp: Path, final: Path) -> bool:
        calls["n"] += 1
        if calls["n"] == 2:  # meta 发布失败
            raise OSError("磁盘写入失败")
        return real(tmp, final)

    monkeypatch.setattr(ft, "_publish", flaky)
    with pytest.raises(OSError):
        ft._publish_pair(b"a\n", b"{}", jsonl, meta)
    assert not jsonl.exists()
    assert not meta.exists()
    assert not list(tmp_path.glob(".*tmp*"))


# ---- 一致性与协议 -----------------------------------------------------------


def test_candidate_pool_matches_historical_c0(env):
    """同一份行情、同一参数下，前瞻候选与历史 C0 必须逐项一致。"""
    db, _out, latest = env
    engine = ft._readonly_engine(db)
    try:
        with Session(engine) as session:
            matrix = load_matrix(session, START, latest)
    finally:
        engine.dispose()

    details: dict = {}
    signals = bt.build_signals(
        matrix, start=latest, end=latest, top_n=ft.TOP_N, details=details
    )
    forward = ft.build_candidates(matrix, latest, ft.TOP_N)

    historical_codes = [item.code for item in signals["C0"]]
    forward_codes = [code for code, _score in forward["ranked"]]
    assert historical_codes == forward_codes
    assert len(historical_codes) == ft.TOP_N

    day_detail = details[latest]
    for code in historical_codes:
        assert day_detail["scores"][code] == forward["scores"][code]
        for key in ("rev20", "illiq20", "amp20"):
            assert day_detail["percentiles"][code][key] == pytest.approx(
                forward["percentiles"][code][key]
            )
        assert day_detail["liquidity"].get(code) == pytest.approx(
            forward["liquidity"].get(code)
        )


def test_candidate_universe_includes_st_and_bse(env):
    """协议声明「含 ST、含北交所、不套流动性门槛」—— 代码层不得有额外过滤。"""
    db, _out, latest = env
    engine = ft._readonly_engine(db)
    try:
        with Session(engine) as session:
            matrix = load_matrix(session, START, latest)
    finally:
        engine.dispose()
    candidates = ft.build_candidates(matrix, latest, 1000)
    # 合成库里 30 只全部有行情 → 全部进入池，没有任何额外筛除
    assert len(candidates["pool"]) == 30
    assert set(candidates["pool"]) == {f"{c:06d}" for c in range(30)}


def test_snapshot_is_deterministic(tmp_path, capsys):
    outputs = []
    for name in ("a", "b"):
        db = tmp_path / f"{name}.db"
        latest = _build_db(db)
        out = tmp_path / f"out_{name}"
        assert _run(db, out, "snapshot", "--mode", "forward",
                    "--now", _now(latest, 15, 45)) == 0
        path = out / f"signals_{latest.isoformat()}.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        outputs.append([(r["stock_code"], r["rank"], r["score_total"]) for r in rows])
    assert outputs[0] == outputs[1]
    capsys.readouterr()


def test_candidate_rows_have_required_fields(env, capsys):
    db, out, latest = env
    assert _run(db, out, "snapshot", "--mode", "forward",
                "--now", _now(latest, 15, 45)) == 0
    path = out / f"signals_{latest.isoformat()}.jsonl"
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    for field in (
        "mode", "signal_date", "created_at", "strategy_version", "git_commit",
        "source_data_revision", "source_data_date", "universe_definition",
        "universe_count", "stock_code", "rank", "score_total",
        "rev20", "illiq20", "amp20", "percentiles", "avg_amount_20",
        "eligibility_reason",
    ):
        assert field in row, field
    assert row["mode"] == "FORWARD_FROZEN"
    capsys.readouterr()


# ---- 完整性与版本 -----------------------------------------------------------


def test_tampered_snapshot_is_rejected(env, capsys):
    db, out, latest = env
    assert _run(db, out, "snapshot", "--mode", "forward",
                "--now", _now(latest, 15, 45)) == 0
    path = out / f"signals_{latest.isoformat()}.jsonl"
    path.write_bytes(path.read_bytes() + b'{"tampered":1}\n')
    with pytest.raises(SystemExit):
        _run(db, out, "evaluate", "--date", latest.isoformat())
    capsys.readouterr()


def test_version_mismatch_is_rejected(env, capsys):
    db, out, latest = env
    assert _run(db, out, "snapshot", "--mode", "forward",
                "--now", _now(latest, 15, 45)) == 0
    meta_path = out / f"signals_{latest.isoformat()}.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["strategy_version"] = "c0-old"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        _run(db, out, "evaluate", "--date", latest.isoformat())
    assert "策略版本" in str(error.value)
    capsys.readouterr()


def test_duplicate_snapshot_is_rejected(env, capsys):
    db, out, latest = env
    now = _now(latest, 15, 45)
    assert _run(db, out, "snapshot", "--mode", "forward", "--now", now) == 0
    assert _run(db, out, "snapshot", "--mode", "forward", "--now", now) == 2
    capsys.readouterr()


# ---- 评价 ------------------------------------------------------------------


def _write_snapshot(out: Path, day: date, codes: list[str], *, mode: str = "FORWARD_FROZEN") -> None:
    out.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(
            {
                "mode": mode,
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
    stem = "signals" if mode == "FORWARD_FROZEN" else "replay_signals"
    jsonl = out / f"{stem}_{day.isoformat()}.jsonl"
    jsonl.write_bytes(payload.encode("utf-8"))
    meta = {
        "mode": mode,
        "counts_for_forward_performance": mode == "FORWARD_FROZEN",
        "signal_date": day.isoformat(), "row_count": len(codes),
        "strategy_version": ft.STRATEGY_VERSION,
        "universe_count": len(codes),
        "completeness": {"rows": 30},
        "content_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }
    (out / f"{stem}_{day.isoformat()}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )


def test_evaluate_reports_pending_when_not_matured(env, capsys):
    db, out, latest = env
    assert _run(db, out, "snapshot", "--mode", "forward",
                "--now", _now(latest, 15, 45)) == 0
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


def test_module_has_no_network_scheduler_or_orders():
    source = Path(ft.__file__).read_text(encoding="utf-8")
    for banned in ("requests", "urllib", "httpx", "apscheduler", "schedule.",
                   "place_order", "broker", "trade_account"):
        assert banned not in source, banned
    assert not hasattr(ft, "session_scope")


def test_protocol_weights_are_frozen():
    assert ft.PROTOCOL["weights"] == {"rev20": 0.30, "illiq20": 0.30, "amp20": 0.40}
    assert ft.PROTOCOL["top_n"] == 20
    assert ft.PROTOCOL["entry"] == "D+1 开盘"
    assert ft.PROTOCOL["percentile_scope"] == "当日全体候选池内"
    assert "ST" in ft.PROTOCOL["universe_definition"]
    assert "北交所" in ft.PROTOCOL["universe_definition"]
