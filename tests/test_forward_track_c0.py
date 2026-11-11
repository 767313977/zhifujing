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
    prefixes: tuple[str, ...] = ("00",),
    drop_prefix_last_day: str | None = None,
) -> date:
    """单调漂移的合成行情，返回最新交易日。

    `future_days` 让日历比行情多铺几天，模拟「次日仍是交易日」；
    `thin_last_day` 让最后一晚只剩 N 只票，模拟采集未完成；
    `prefixes` 按顺序轮流分配代码段（默认全深主板），用于验证分市场覆盖；
    `drop_prefix_last_day` 让最后一晚整段缺失某市场。
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
            prefix = prefixes[c % len(prefixes)]
            bucket = c // len(prefixes)
            code = f"{prefix}{bucket:04d}"
            close = 10.0 + c
            for d in range(days):
                day = (START + timedelta(days=d)).isoformat()
                drift = -0.004 + 0.0003 * c
                close *= 1 + drift
                if thin_last_day is not None and d == days - 1 and c >= thin_last_day:
                    continue
                if (
                    drop_prefix_last_day is not None
                    and d == days - 1
                    and code.startswith(drop_prefix_last_day)
                ):
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


def _db_codes(db: Path) -> list[str]:
    with sqlite3.connect(db) as conn:
        return [row[0] for row in conn.execute("select distinct code from stock_daily order by code")]


def _write_universe(db: Path, eligible: list[str], *, version: str = "b1-test") -> Path:
    """在库旁边写一份 B1 清单（测试用）。"""
    codes = _db_codes(db)
    payload = "\n".join(f"{code}|测试|{ft._board_of(code)}" for code in codes)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    manifest = {
        "version": version,
        "data_cutoff": "2099-12-31",
        "universe_sha256": digest,
        "counts": {
            "total": len(codes),
            "forward_eligible": len(eligible),
            "excluded_unverified": len(codes) - len(eligible),
        },
        "securities": [
            {
                "code": code,
                "name": "测试",
                "board": ft._board_of(code),
                "forward_eligible": code in set(eligible),
            }
            for code in codes
        ],
    }
    path = db.with_name("universe.json")
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture()
def env(tmp_path):
    db = tmp_path / "history.db"
    latest = _build_db(db)
    _write_universe(db, _db_codes(db))
    out = tmp_path / "forward"
    return db, out, latest


def _run(db: Path, out: Path, *argv) -> int:
    universe = db.with_name("universe.json")
    if not universe.is_file():
        _write_universe(db, _db_codes(db))
    return ft.main(
        ["--db", str(db), "--out", str(out), "--universe", str(universe), *argv]
    )


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


# ---- forward 闸门 ---------------------------------------------------------


def test_forward_snapshot_passes_all_gates(env, capsys):
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


def test_forward_rejects_before_close(env, capsys):
    db, out, latest = env
    assert _run(db, out, "snapshot", "--mode", "forward", "--now", _now(latest, 14, 0)) == 3
    assert not list(out.glob("signals_*.jsonl"))
    assert "早于 D 日 15:30" in capsys.readouterr().err


def test_forward_rejects_stale_data(env, capsys):
    db, out, latest = env
    # 今天已经是 10 天后，库里最新还是老日期
    assert _run(db, out, "snapshot", "--mode", "forward",
                "--now", _now(latest + timedelta(days=10), 16, 0)) == 3
    assert not list(out.glob("signals_*.jsonl"))
    assert "拒绝正式前瞻冻结" in capsys.readouterr().err


def test_forward_rejects_after_next_open(env, capsys):
    db, out, latest = env
    # D 日收盘后但已跨过次一交易日 09:15：不再是「开盘前」
    assert _run(db, out, "snapshot", "--mode", "forward",
                "--date", latest.isoformat(),
                "--now", _now(latest + timedelta(days=1), 10, 0)) == 3
    assert "开盘前" in capsys.readouterr().err


def test_forward_rejects_incomplete_day(tmp_path, capsys):
    db = tmp_path / "history.db"
    latest = _build_db(db, thin_last_day=5)  # 最后一天只有 5 只
    out = tmp_path / "forward"
    code = _run(db, out, "snapshot", "--mode", "forward", "--now", _now(latest, 15, 45))
    assert code == 3
    assert not list(out.glob("signals_*.jsonl"))
    assert "覆盖" in capsys.readouterr().err


def test_forward_rejects_without_trade_calendar(tmp_path, capsys):
    """没有获准的交易日历 = 无法确认 D 是交易日 → 拒绝启动。"""
    db = tmp_path / "history.db"
    latest = _build_db(db, calendar=False)
    out = tmp_path / "forward"
    code = _run(db, out, "snapshot", "--mode", "forward", "--now", _now(latest, 15, 45))
    assert code == 3
    assert not list(out.glob("signals_*.jsonl"))
    assert "交易日历" in capsys.readouterr().err


def test_forward_writes_nothing_on_reject(env, capsys):
    db, out, latest = env
    _run(db, out, "snapshot", "--mode", "forward", "--now", _now(latest, 10, 0))
    assert not out.exists() or not any(out.iterdir())
    capsys.readouterr()


def test_forward_accepts_late_data_same_evening(env, capsys):
    """D 日 22:00 数据才落完 —— 仍在「15:30 ~ 次日 09:15」窗口内，允许冻结。"""
    db, out, latest = env
    assert _run(db, out, "snapshot", "--mode", "forward",
                "--now", _now(latest, 22, 0)) == 0
    capsys.readouterr()


def test_forward_accepts_late_data_before_next_open(env, capsys):
    """跨夜：次日 08:00 仍早于 09:15，合法窗口未结束，允许冻结。"""
    db, out, latest = env
    code = _run(
        db, out, "snapshot", "--mode", "forward",
        "--date", latest.isoformat(),
        "--now", _now(latest + timedelta(days=1), 8, 0),
    )
    assert code == 0
    capsys.readouterr()


def test_board_classification():
    cases = {
        "600000": "沪主板",
        "688001": "科创板",
        "000001": "深主板",
        "300001": "创业板",
        "830001": "北交所",
        "430001": "北交所",
        "920001": "北交所",
        "900001": "其他",
    }
    for code, board in cases.items():
        assert ft._board_of(code) == board, code


def test_forward_rejects_board_dropped_silently(tmp_path, capsys):
    """总行数下降不足以证明范围完整：北交所整段消失必须单独拦下。"""
    db = tmp_path / "history.db"
    latest = _build_db(
        db, codes=90, prefixes=("60", "00", "83"), drop_prefix_last_day="83"
    )
    out = tmp_path / "forward"
    code = _run(db, out, "snapshot", "--mode", "forward", "--now", _now(latest, 15, 45))
    assert code == 3
    err = capsys.readouterr().err
    assert "北交所" in err
    assert "静默丢弃" in err
    assert not list(out.glob("signals_*.jsonl"))


def test_forward_universe_excludes_unverified(tmp_path, capsys):
    """B1：身份未确认的证券不进入前瞻候选，即使它当天有行情。"""
    db = tmp_path / "history.db"
    latest = _build_db(db, codes=100, prefixes=("60", "00", "83"))
    all_codes = _db_codes(db)
    eligible = [code for code in all_codes if not code.startswith("83")]
    _write_universe(db, eligible)
    out = tmp_path / "forward"

    assert _run(db, out, "snapshot", "--mode", "forward",
                "--now", _now(latest, 15, 45)) == 0
    jsonl, meta_path = ft._paths(out, latest, "forward")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["universe"]["applied"] is True
    assert meta["universe"]["eligible"] == len(eligible)
    assert meta["universe"]["excluded_unverified"] == len(all_codes) - len(eligible)
    rows = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    assert rows and all(not row["stock_code"].startswith("83") for row in rows)
    capsys.readouterr()


def test_replay_ignores_universe(tmp_path, capsys):
    """重放复现历史口径：不受 B1 清单影响，否则与历史 C0 不再可比。"""
    db = tmp_path / "history.db"
    latest = _build_db(db, codes=100, prefixes=("60", "00", "83"))
    _write_universe(db, [code for code in _db_codes(db) if not code.startswith("83")])
    out = tmp_path / "forward"

    assert _run(db, out, "snapshot", "--mode", "replay") == 0
    _jsonl, meta_path = ft._paths(out, latest, "replay")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["universe"] is None
    assert meta["universe_count"] == 100
    capsys.readouterr()


def test_forward_refuses_without_universe(tmp_path, capsys):
    """没有清单不得凭空造一个池（尤其不能靠 startswith('920') 放行）。"""
    db = tmp_path / "history.db"
    latest = _build_db(db)
    out = tmp_path / "forward"
    missing = tmp_path / "missing_universe.json"
    with pytest.raises(SystemExit) as error:
        ft.main([
            "--db", str(db), "--out", str(out), "--universe", str(missing),
            "snapshot", "--mode", "forward", "--now", _now(latest, 15, 45),
        ])
    assert "B1 证券清单" in str(error.value)
    assert not out.exists() or not list(out.glob("signals_*.jsonl"))
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


# ---- 场景 1~4：崩溃点 -------------------------------------------------------


def test_crash_before_tmp_written(tmp_path, monkeypatch):
    """场景 1：连临时文件都没写出来就崩溃 → 目录里什么都没有。"""
    jsonl = tmp_path / "signals_2024-03-30.jsonl"
    meta = tmp_path / "signals_2024-03-30.meta.json"

    def boom(*_args, **_kwargs):
        raise RuntimeError("进程在写临时文件前被杀")

    monkeypatch.setattr(ft, "_write_tmp", boom)
    with pytest.raises(RuntimeError):
        ft._publish_pair(b"a\n", b"{}", jsonl, meta)
    assert not jsonl.exists() and not meta.exists()
    assert not list(tmp_path.iterdir())


def test_crash_after_tmp_before_publish(tmp_path, monkeypatch):
    """场景 2：临时文件写完、发布前崩溃 → 只剩临时文件，且不会被当成快照。"""
    jsonl = tmp_path / "signals_2024-03-30.jsonl"
    meta = tmp_path / "signals_2024-03-30.meta.json"

    def boom(*_args, **_kwargs):
        raise RuntimeError("进程在发布前被杀")

    monkeypatch.setattr(ft, "_publish", boom)
    with pytest.raises(RuntimeError):
        ft._publish_pair(b"a\n", b"{}", jsonl, meta)
    assert not jsonl.exists() and not meta.exists()
    assert not list(tmp_path.glob(".*tmp*"))
    assert ft.inspect_snapshot(tmp_path, date(2024, 3, 30), "forward").state == ft.STATE_NONE


def test_crash_after_jsonl_publish_before_meta(tmp_path):
    """场景 3：JSONL 已独占发布、meta 未发布（真实崩溃，无回滚）→ INCOMPLETE。"""
    day = date(2024, 3, 30)
    jsonl, meta = ft._paths(tmp_path, day, "forward")
    tmp = ft._write_tmp(jsonl, b'{"a":1}\n')
    assert ft._publish(tmp, jsonl) is True
    tmp.unlink(missing_ok=True)

    state = ft.inspect_snapshot(tmp_path, day, "forward")
    assert state.state == ft.STATE_INCOMPLETE
    assert "meta 缺失" in state.detail


def test_crash_during_meta_write(tmp_path, monkeypatch):
    """场景 4：meta 临时文件写到一半崩溃 → 已有 JSONL 也不留下。"""
    jsonl = tmp_path / "signals_2024-03-30.jsonl"
    meta = tmp_path / "signals_2024-03-30.meta.json"
    calls = {"n": 0}
    real = ft._write_tmp

    def flaky(final: Path, payload: bytes) -> Path:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("meta 落盘失败")
        return real(final, payload)

    monkeypatch.setattr(ft, "_write_tmp", flaky)
    with pytest.raises(OSError):
        ft._publish_pair(b"a\n", b"{}", jsonl, meta)
    assert not jsonl.exists() and not meta.exists()
    assert not list(tmp_path.glob(".*tmp*"))


# ---- 场景 3/5/6：状态机与拒绝覆盖 -------------------------------------------


def test_state_none_then_complete(tmp_path, capsys):
    day = date(2024, 3, 30)
    assert ft.inspect_snapshot(tmp_path, day, "forward").state == ft.STATE_NONE
    jsonl, meta = ft._paths(tmp_path, day, "forward")
    payload = b'{"mode":"FORWARD_FROZEN","signal_date":"2024-03-30"}\n'
    info = {
        "mode": "FORWARD_FROZEN",
        "signal_date": day.isoformat(),
        "row_count": 1,
        "content_sha256": hashlib.sha256(payload).hexdigest(),
    }
    assert ft._publish_pair(
        payload,
        json.dumps(info, ensure_ascii=False).encode("utf-8"),
        jsonl,
        meta,
    )
    assert ft.inspect_snapshot(tmp_path, day, "forward").state == ft.STATE_COMPLETE
    capsys.readouterr()


def test_snapshot_refuses_incomplete_state(tmp_path, capsys):
    """只剩 JSONL（孤儿）时，snapshot 不得补写 meta 也不得覆盖。"""
    db = tmp_path / "history.db"
    latest = _build_db(db)
    out = tmp_path / "forward"
    out.mkdir()
    day = latest
    jsonl, _meta = ft._paths(out, day, "replay")
    jsonl.write_bytes(b'{"a":1}\n')

    code = _run(db, out, "snapshot", "--mode", "replay")
    assert code == 4
    text = capsys.readouterr().err
    assert "INCOMPLETE_PUBLISH" in text
    assert "不会自动删除" in text
    assert "不要手工补写 meta" in text
    assert jsonl.read_bytes() == b'{"a":1}\n'  # 原样保留
    assert not _meta.exists()


def test_evaluate_refuses_incomplete_state(tmp_path, capsys):
    db = tmp_path / "history.db"
    latest = _build_db(db)
    out = tmp_path / "forward"
    out.mkdir()
    jsonl, _meta = ft._paths(out, latest, "forward")
    jsonl.write_bytes(b'{"a":1}\n')
    with pytest.raises(SystemExit) as error:
        _run(db, out, "evaluate", "--date", latest.isoformat(), "--mode", "forward")
    assert "INCOMPLETE_PUBLISH" in str(error.value)
    capsys.readouterr()


def test_snapshot_refuses_corrupted_state(tmp_path, capsys):
    db = tmp_path / "history.db"
    latest = _build_db(db)
    out = tmp_path / "forward"
    assert _run(db, out, "snapshot", "--mode", "forward",
                "--now", _now(latest, 15, 45)) == 0
    path = out / f"signals_{latest.isoformat()}.jsonl"
    path.write_bytes(path.read_bytes() + b'{"tampered":1}\n')
    capsys.readouterr()

    code = _run(db, out, "snapshot", "--mode", "forward",
                "--now", _now(latest, 15, 45))
    assert code == 5
    text = capsys.readouterr().err
    assert "CORRUPTED_SNAPSHOT" in text
    assert path.read_bytes().endswith(b'{"tampered":1}\n')  # 未被覆盖


def test_evaluate_refuses_corrupted_state(tmp_path, capsys):
    db = tmp_path / "history.db"
    latest = _build_db(db)
    out = tmp_path / "forward"
    assert _run(db, out, "snapshot", "--mode", "forward",
                "--now", _now(latest, 15, 45)) == 0
    path = out / f"signals_{latest.isoformat()}.jsonl"
    path.write_bytes(path.read_bytes() + b'{"tampered":1}\n')
    with pytest.raises(SystemExit) as error:
        _run(db, out, "evaluate", "--date", latest.isoformat())
    assert "CORRUPTED_SNAPSHOT" in str(error.value)
    capsys.readouterr()


def test_row_level_mismatch_is_corrupted(tmp_path, capsys):
    """同时改写 JSONL 与 meta 使哈希自洽，但行内 signal_date 与文件不符 → 仍判损坏。"""
    db = tmp_path / "history.db"
    latest = _build_db(db)
    out = tmp_path / "forward"
    assert _run(db, out, "snapshot", "--mode", "forward",
                "--now", _now(latest, 15, 45)) == 0
    jsonl, meta_path = ft._paths(out, latest, "forward")
    rows = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    rows[0]["signal_date"] = "2020-01-01"
    payload = "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n"
    jsonl.write_bytes(payload.encode("utf-8"))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["content_sha256"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        _run(db, out, "evaluate", "--date", latest.isoformat())
    assert "CORRUPTED_SNAPSHOT" in str(error.value)
    capsys.readouterr()


def test_forged_pair_passes_local_check(tmp_path, capsys):
    """场景 8：能力边界 —— JSONL 与 meta 同时被替换时，本地哈希无法发现。"""
    db = tmp_path / "history.db"
    latest = _build_db(db)
    out = tmp_path / "forward"
    assert _run(db, out, "snapshot", "--mode", "forward",
                "--now", _now(latest, 15, 45)) == 0
    jsonl, meta_path = ft._paths(out, latest, "forward")

    rows = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    rows.reverse()  # 伪造一份「顺序不同」的候选，内容仍然合法
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
    payload = "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n"
    jsonl.write_bytes(payload.encode("utf-8"))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["content_sha256"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    # 哈希自洽 ⇒ 本地检查判定为 COMPLETE。这正是它不能证明「未被篡改」的地方。
    assert ft.inspect_snapshot(out, latest, "forward").state == ft.STATE_COMPLETE
    capsys.readouterr()


def test_list_marks_incomplete_entries(tmp_path, capsys):
    db = tmp_path / "history.db"
    latest = _build_db(db)
    out = tmp_path / "forward"
    out.mkdir()
    jsonl, _meta = ft._paths(out, latest, "forward")
    jsonl.write_bytes(b'{"a":1}\n')
    assert _run(db, out, "list") == 0
    text = capsys.readouterr().out
    assert "INCOMPLETE_PUBLISH" in text


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
    assert _run(db, out, "snapshot", "--mode", "replay",
                "--now", _now(latest, 15, 45)) == 0
    assert _run(db, out, "evaluate", "--date", latest.isoformat(), "--mode", "replay") == 0
    text = capsys.readouterr().out
    assert "pending" in text
    assert "不计入任何收益" in text


def test_evaluate_counts_matured_samples(tmp_path, capsys):
    db = tmp_path / "history.db"
    latest = _build_db(db, days=90)
    out = tmp_path / "forward"
    signal_day = latest - timedelta(days=20)
    _write_snapshot(out, signal_day, [f"{c:06d}" for c in range(5)], mode="REPLAY_ONLY")

    assert _run(db, out, "evaluate", "--date", signal_day.isoformat(), "--mode", "replay") == 0
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


def test_protocol_records_universe_risk_flags():
    """B1v 已恢复历史全部证券，不能再标「北交所被截断」。"""
    assert "SURVIVORSHIP_BIAS" in ft.PROTOCOL["risk_flags"]
    assert "ST_STATUS_PARTIAL" in ft.PROTOCOL["risk_flags"]
    assert "UNIVERSE_TRUNCATED_BSE" not in ft.PROTOCOL["risk_flags"]
    assert ft.STRATEGY_VERSION.startswith("c0-2026-11-11")
    assert "B1v" in ft.PROTOCOL["universe_definition"]
