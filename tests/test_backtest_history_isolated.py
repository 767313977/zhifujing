"""B3 整改回归：`history.db` 回测不得隐式访问其他数据库。

背景（B3 第一节/第二节）：
- 旧实现 `_load_history()` 末尾无条件调用 `_overlay_names_from_main()`，
  后者用 `session_scope()` 连**主库** `fupan.db`；主库不存在时 SQLite 还会新建空文件。
- 整改后：名称覆盖必须由调用方通过 `overlay_names_db` 显式传入，且只读打开。

本文件用临时库验证这四件事，全部不触碰真实 `backend/data/` 下的任何库。
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# backtest_patterns 在模块级 `from draft_patterns import ...`（同目录脚本），
# 直接 `python scripts/backtest_patterns.py` 运行时脚本目录天然在 sys.path 上，
# 测试里要显式补上。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts import backtest_patterns as bp  # noqa: E402


def _make_history_db(path: Path, rows: list[tuple]) -> None:
    """建一个最小 `stock_daily`（列与回测查询一致）。"""
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_daily (
                trade_date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                pct_chg REAL,
                volume REAL,
                amount REAL,
                PRIMARY KEY (trade_date, code)
            )
            """
        )
        conn.executemany(
            "INSERT OR REPLACE INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?)", rows
        )


def _bar(day: str, code: str, name: str, close: float = 10.0) -> tuple:
    return (day, code, name, close, close * 1.02, close * 0.98, close, 0.5, 1e6, 1e7)


@pytest.fixture()
def history_db(tmp_path: Path) -> Path:
    db = tmp_path / "history.db"
    _make_history_db(db, [
        _bar("2024-01-02", "000001", "平安银行"),
        _bar("2024-01-03", "000001", "平安银行"),
    ])
    return db


# ---- 1. 默认不访问主库 -------------------------------------------------------

def test_load_history_never_calls_main_session(history_db, monkeypatch):
    """默认路径下不得调用 `session_scope`（主库连接）。"""
    def _boom(*args, **kwargs):
        raise AssertionError("_load_history 默认不应访问主库（session_scope）")

    monkeypatch.setattr(bp, "session_scope", _boom)

    engine = bp._readonly_engine(history_db)
    try:
        latest, grouped = bp._load_history(engine, ["000001"], 10)
    finally:
        engine.dispose()

    assert latest == date(2024, 1, 3)
    assert set(grouped) == {"000001"}
    assert [r["name"] for r in grouped["000001"]] == ["平安银行", "平安银行"]


def test_load_history_creates_no_extra_file(history_db, tmp_path):
    """只给 history.db 时，目录里不得多出任何文件（尤其不能新建空库）。"""
    before = {p.name for p in tmp_path.iterdir()}
    engine = bp._readonly_engine(history_db)
    try:
        bp._load_history(engine, ["000001"], 10)
    finally:
        engine.dispose()
    after = {p.name for p in tmp_path.iterdir()}
    assert after == before, f"出现了多余文件：{after - before}"


# ---- 2. 只读连接 -------------------------------------------------------------

def test_readonly_engine_is_mode_ro(history_db):
    engine = bp._readonly_engine(history_db)
    try:
        assert "mode=ro" in str(engine.url)
    finally:
        engine.dispose()


def test_readonly_engine_cannot_write(history_db):
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    engine = bp._readonly_engine(history_db)
    try:
        with pytest.raises(OperationalError):
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM stock_daily"))
    finally:
        engine.dispose()


# ---- 3. 名称覆盖必须显式且库必须已存在 ---------------------------------------

def test_overlay_missing_db_exits_without_creating(tmp_path):
    missing = tmp_path / "nope.db"
    grouped = {"000001": [{"date": date(2024, 1, 2), "name": "旧名"}]}

    with pytest.raises(SystemExit):
        bp._overlay_names_from_main(
            grouped, date(2024, 1, 1), date(2024, 1, 3), db_path=missing
        )

    assert not missing.exists(), "不得因路径不存在而新建数据库文件"


def test_overlay_explicit_db_overrides_names(tmp_path):
    overlay = tmp_path / "names.db"
    _make_history_db(overlay, [
        _bar("2024-01-02", "000001", "ST某某"),
        _bar("2024-01-03", "000001", "平安银行"),
    ])
    original_bytes = overlay.read_bytes()

    grouped = {
        "000001": [
            {"date": date(2024, 1, 2), "name": "平安银行"},
            {"date": date(2024, 1, 3), "name": "平安银行"},
        ]
    }
    changed = bp._overlay_names_from_main(
        grouped, date(2024, 1, 1), date(2024, 1, 3), db_path=overlay
    )

    assert changed == 1
    assert grouped["000001"][0]["name"] == "ST某某"
    assert grouped["000001"][1]["name"] == "平安银行"
    # 只读：覆盖源库字节不变
    assert overlay.read_bytes() == original_bytes


def test_load_history_with_overlay_applies_names(history_db, tmp_path):
    overlay = tmp_path / "names.db"
    _make_history_db(overlay, [_bar("2024-01-02", "000001", "ST某某")])

    engine = bp._readonly_engine(history_db)
    try:
        _latest, grouped = bp._load_history(
            engine, ["000001"], 10, overlay_names_db=overlay
        )
    finally:
        engine.dispose()

    assert grouped["000001"][0]["name"] == "ST某某"
    assert grouped["000001"][1]["name"] == "平安银行"


# ---- 4. ST 判定：未知名称不得当作「非 ST」以外的结论 --------------------------

def test_st_rows_marks_constant_names(history_db):
    """同一票全历史只有一个名称且带 ST → 必须计入 `constant_st`（无法区分时段）。"""
    db = history_db
    _make_history_db(db, [
        _bar("2024-01-02", "000002", "ST某某"),
        _bar("2024-01-03", "000002", "ST某某"),
    ])
    engine = bp._readonly_engine(db)
    try:
        _latest, grouped = bp._load_history(engine, ["000001", "000002"], 10)
    finally:
        engine.dispose()

    st_days, stats = bp._st_rows(grouped)
    assert "000002" in st_days
    assert stats["constant_st"] == 1


# ---- 5. 股票池默认取自 history.db，不连主库 ----------------------------------

def test_codes_from_history_uses_only_that_db(history_db, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("_codes_from_history 不应访问主库")

    monkeypatch.setattr(bp, "session_scope", _boom)
    codes = bp._codes_from_history(history_db)
    assert codes == ["000001"]


def test_codes_from_history_does_not_create_file(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(Exception):
        bp._codes_from_history(missing)
    assert not missing.exists()


# ---- 6. 完整 CLI 路径：只读隔离 + RPS 全池分母 -------------------------------

def _make_uptrend_db(path: Path, codes: int = 20, days: int = 80) -> None:
    """单调上涨的合成行情：new_high 每天都会命中。"""
    rows = []
    for c in range(codes):
        code = f"{c:06d}"
        close = 10.0
        for d in range(days):
            day = f"2024-{1 + d // 28:02d}-{1 + d % 28:02d}"
            close *= 1.01
            rows.append(
                (day, code, "测试", close * 0.99, close * 1.005, close * 0.985,
                 close, 1.0, 1e6, 1e7)
            )
    _make_history_db(path, rows)


@pytest.fixture()
def cli_db(tmp_path: Path) -> Path:
    db = tmp_path / "history.db"
    _make_uptrend_db(db)
    return db


def test_cli_history_only_opens_history_db(cli_db, tmp_path, monkeypatch, capsys):
    """完整 CLI 路径：--history 下只允许打开指定的 history.db。"""
    opened: list[str] = []
    real_create_engine = bp.create_engine

    def spy_create_engine(url, *args, **kwargs):
        opened.append(str(url))
        return real_create_engine(url, *args, **kwargs)

    def _boom(*args, **kwargs):
        raise AssertionError("--history 下不应访问主库（session_scope）")

    monkeypatch.setattr(bp, "create_engine", spy_create_engine)
    monkeypatch.setattr(bp, "session_scope", _boom)
    monkeypatch.setattr(bp, "HISTORY_DB", cli_db)

    before = {p.name for p in tmp_path.iterdir()}
    rc = bp.main([
        "--history", "--pattern", "new_high",
        "--stocks", "5", "--bars", "80",
        "--filter-split", "rps50_90",
    ])
    capsys.readouterr()

    # 0 命中时脚本返回 1（既有行为）；本用例只验证数据源隔离
    assert rc in (0, 1)
    # 只打开过 history.db 一个库
    assert opened, "至少应打开一次 history.db"
    for url in opened:
        assert "history.db" in url, f"打开了非预期数据库：{url}"
        assert "mode=ro" in url, f"未使用只读连接：{url}"
        for forbidden in ("fupan.db", "b1_research.db", "p13_audit"):
            assert forbidden not in url, f"打开了受限数据库：{url}"
    # 没有新建任何文件
    after = {p.name for p in tmp_path.iterdir()}
    assert after == before, f"出现了新文件：{after - before}"


def test_cli_passes_full_pool_rps_to_filter(cli_db, monkeypatch, capsys):
    """`--stocks 5` 只缩小形态评价集合，RPS 分母仍必须是全池 20 只。"""
    captured: dict[str, int] = {}
    real_filter_context = bp._filter_context

    def spy_filter_context(names, bars, all_dates, *, rps_bars=None):
        captured["research"] = len(bars)
        captured["rps_pool"] = len(rps_bars) if rps_bars is not None else -1
        return real_filter_context(names, bars, all_dates, rps_bars=rps_bars)

    monkeypatch.setattr(bp, "filter_context_func", spy_filter_context, raising=False)
    monkeypatch.setattr(bp, "_filter_context", spy_filter_context)
    monkeypatch.setattr(bp, "HISTORY_DB", cli_db)

    rc = bp.main([
        "--history", "--pattern", "new_high",
        "--stocks", "5", "--bars", "80",
        "--filter-split", "rps50_90",
    ])
    capsys.readouterr()

    # 0 命中时脚本返回 1（既有行为），与分母接线无关
    assert rc in (0, 1)
    assert captured["research"] == 5, f"形态评价集合应为 5，实际 {captured}"
    assert captured["rps_pool"] == 20, f"RPS 分母应为全池 20，实际 {captured}"

