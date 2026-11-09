"""R5 缓存安全加固测试：fail-closed、修订矩阵、快照一致性、并发、身份。"""

from __future__ import annotations

import multiprocessing
import shutil
import threading
import time
from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.models import Base, RpsDaily, SectorMember, StockDaily
from app.services import data_revision, research_screen

DAY = date(2026, 9, 24)


def _engine(path):
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    return engine


def _seed_stock(session: Session, day: date = DAY, close: float = 10.0) -> None:
    session.add(
        StockDaily(
            trade_date=day,
            code="000001",
            name="测试",
            open=close,
            high=close * 1.01,
            low=close * 0.99,
            close=close,
            pct_chg=0.0,
            amount=1e8,
        )
    )
    session.commit()


def _counting(monkeypatch, name: str = "_low_start_rows"):
    calls = {"n": 0}

    def fake(*args, **kwargs):
        calls["n"] += 1
        return {"trade_date": DAY, "rows": [], "coverage": {}, "strategy": {}}

    monkeypatch.setattr(research_screen, name, fake)
    return calls


def _installed_engine(tmp_path, name="rev.db"):
    engine = _engine(tmp_path / name)
    with Session(engine) as session:
        _seed_stock(session)
        data_revision.install(session)
    research_screen.clear_cache()
    return engine


def test_a_same_version_hits_cache(tmp_path, monkeypatch):
    engine = _installed_engine(tmp_path)
    calls = _counting(monkeypatch)
    with Session(engine) as session:
        research_screen.low_start_rows(session, trade_date=DAY)
        research_screen.low_start_rows(session, trade_date=DAY)
    assert calls["n"] == 1


def test_b_parameters_are_isolated(tmp_path, monkeypatch):
    engine = _installed_engine(tmp_path)
    calls = _counting(monkeypatch)
    with Session(engine) as session:
        research_screen.low_start_rows(session, trade_date=DAY, limit=10)
        research_screen.low_start_rows(session, trade_date=DAY, limit=20)
    assert calls["n"] == 2


def test_c_historical_kline_revision_invalidates(tmp_path, monkeypatch):
    engine = _installed_engine(tmp_path)
    calls = _counting(monkeypatch)
    with Session(engine) as session:
        research_screen.low_start_rows(session, trade_date=DAY)
    with Session(engine) as session:
        session.execute(text("UPDATE stock_daily SET close = 11.0 WHERE trade_date = :d"), {"d": DAY})
        session.commit()
    with Session(engine) as session:
        research_screen.low_start_rows(session, trade_date=DAY)
    assert calls["n"] == 2


def test_c2_revision_table_missing_disables_cache(tmp_path, monkeypatch):
    """R5-01：修订表不存在时不得启用缓存。"""
    engine = _engine(tmp_path / "plain.db")
    with Session(engine) as session:
        _seed_stock(session)
    research_screen.clear_cache()
    calls = _counting(monkeypatch)
    with Session(engine) as session:
        assert data_revision.status(session).state == "not_installed"
        research_screen.low_start_rows(session, trade_date=DAY)
        research_screen.low_start_rows(session, trade_date=DAY)
    assert calls["n"] == 2


def test_c3_missing_trigger_disables_cache(tmp_path, monkeypatch):
    """R5-01：缺少必要触发器时不得启用缓存。"""
    engine = _installed_engine(tmp_path)
    with Session(engine) as session:
        session.execute(text("DROP TRIGGER trg_sector_member_ad"))
        session.commit()
        state = data_revision.status(session)
        assert state.state == "not_installed"
        assert "trg_sector_member_ad" in state.reason
    calls = _counting(monkeypatch)
    with Session(engine) as session:
        research_screen.low_start_rows(session, trade_date=DAY)
        research_screen.low_start_rows(session, trade_date=DAY)
    assert calls["n"] == 2


def test_c4_trigger_restored_enables_cache(tmp_path, monkeypatch):
    """R5-01 / 测试矩阵 14：触发器缺失后恢复，缓存重新可用。"""
    engine = _installed_engine(tmp_path)
    with Session(engine) as session:
        session.execute(text("DROP TRIGGER trg_stock_daily_au"))
        session.commit()
        data_revision.install(session)
        assert data_revision.status(session).ready
    calls = _counting(monkeypatch)
    with Session(engine) as session:
        research_screen.low_start_rows(session, trade_date=DAY)
        research_screen.low_start_rows(session, trade_date=DAY)
    assert calls["n"] == 1


def test_c5_unverifiable_status_disables_cache(tmp_path, monkeypatch):
    """R5-01：版本读取失败 → fail closed。"""
    engine = _installed_engine(tmp_path)
    monkeypatch.setattr(
        research_screen.data_revision,
        "status",
        lambda session: data_revision.CacheState("unverifiable", "读取失败"),
    )
    calls = _counting(monkeypatch)
    with Session(engine) as session:
        research_screen.low_start_rows(session, trade_date=DAY)
        research_screen.low_start_rows(session, trade_date=DAY)
    assert calls["n"] == 2


def test_d_sector_member_revision_invalidates(tmp_path, monkeypatch):
    engine = _installed_engine(tmp_path)
    calls = _counting(monkeypatch, "_sector_states")
    with Session(engine) as session:
        research_screen.sector_states(session, trade_date=DAY)
    with Session(engine) as session:
        session.add(SectorMember(trade_date=DAY, sector_code="A", code="000001", pct_chg=1.0))
        session.commit()
    with Session(engine) as session:
        research_screen.sector_states(session, trade_date=DAY)
    assert calls["n"] == 2


def test_e_rps_revision_invalidates(tmp_path, monkeypatch):
    engine = _installed_engine(tmp_path)
    calls = _counting(monkeypatch, "_trend_rows")
    with Session(engine) as session:
        research_screen.trend_rows(session, trade_date=DAY)
    with Session(engine) as session:
        session.add(RpsDaily(trade_date=DAY, code="000001", rps60=88))
        session.commit()
    with Session(engine) as session:
        research_screen.trend_rows(session, trade_date=DAY)
    assert calls["n"] == 2


def test_f_manifest_status_change_bumps_revision(tmp_path):
    engine = _engine(tmp_path / "manifest.db")
    with Session(engine) as session:
        session.execute(
            text(
                "CREATE TABLE sector_member_backfill (trade_date TEXT, sector_code TEXT, "
                "board_name TEXT, taxonomy TEXT, status TEXT, reported INTEGER, unique_count INTEGER, "
                "written INTEGER, attempts INTEGER, last_error TEXT, PRIMARY KEY(trade_date, sector_code, taxonomy))"
            )
        )
        session.execute(
            text(
                "INSERT INTO sector_member_backfill VALUES "
                "('2026-09-24','A','板块A','kph_selected','complete',10,10,10,1,NULL)"
            )
        )
        session.commit()
        data_revision.install(session)
    with Session(engine) as session:
        before = data_revision.status(session).version
    with Session(engine) as session:
        session.execute(text("UPDATE sector_member_backfill SET status='partial'"))
        session.commit()
    with Session(engine) as session:
        after = data_revision.status(session).version
    assert before != after


def test_g_rollback_does_not_advance_revision(tmp_path):
    engine = _installed_engine(tmp_path)
    with Session(engine) as session:
        before = data_revision.status(session).version
    with Session(engine) as session:
        session.add(
            StockDaily(
                trade_date=DAY,
                code="000002",
                name="回滚",
                open=10,
                high=10,
                low=10,
                close=10,
                pct_chg=0.0,
                amount=1e8,
            )
        )
        session.flush()
        session.rollback()
    with Session(engine) as session:
        after = data_revision.status(session).version
    assert before == after


def test_h_different_files_are_isolated(tmp_path, monkeypatch):
    first = _installed_engine(tmp_path, "a.db")
    second = _installed_engine(tmp_path, "b.db")
    with Session(first) as session:
        id_a = data_revision.status(session).identity
    with Session(second) as session:
        id_b = data_revision.status(session).identity
    assert id_a != id_b
    calls = _counting(monkeypatch)
    with Session(first) as session:
        research_screen.low_start_rows(session, trade_date=DAY)
    with Session(second) as session:
        research_screen.low_start_rows(session, trade_date=DAY)
    assert calls["n"] == 2


def test_i_replaced_database_file_gets_new_identity(tmp_path):
    path = tmp_path / "swap.db"
    engine = _installed_engine(tmp_path, "swap.db")
    with Session(engine) as session:
        first = data_revision.status(session).identity
    engine.dispose()
    path.unlink()
    engine = _installed_engine(tmp_path, "swap.db")
    with Session(engine) as session:
        second = data_revision.status(session).identity
    assert first != second


def test_i2_same_uuid_different_path_is_isolated(tmp_path):
    """同一 db_uuid 的两个副本放在不同路径，不能共用缓存身份。"""
    first = _installed_engine(tmp_path, "origin.db")
    with Session(first) as session:
        uuid_a = data_revision.status(session).identity[2]
    first.dispose()
    shutil.copy2(tmp_path / "origin.db", tmp_path / "copy.db")
    copy = create_engine(f"sqlite:///{tmp_path / 'copy.db'}", future=True)
    with Session(copy) as session:
        state = data_revision.status(session)
    assert state.ready
    assert state.identity[2] == uuid_a
    assert state.identity[1][0] != str((tmp_path / "origin.db").resolve())


def test_i3_reopen_database_still_hits(tmp_path, monkeypatch):
    """重新打开同一个库：身份与版本不变，缓存继续命中。"""
    path = tmp_path / "reopen.db"
    engine = _installed_engine(tmp_path, "reopen.db")
    calls = _counting(monkeypatch)
    with Session(engine) as session:
        research_screen.low_start_rows(session, trade_date=DAY)
    engine.dispose()
    reopened = create_engine(f"sqlite:///{path}", future=True)
    with Session(reopened) as session:
        research_screen.low_start_rows(session, trade_date=DAY)
    assert calls["n"] == 1


def _child_update(db_path: str) -> None:
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with Session(engine) as session:
        session.execute(
            text("UPDATE stock_daily SET close = 13.0 WHERE trade_date = :d"), {"d": DAY}
        )
        session.commit()


def test_j_other_process_write_invalidates(tmp_path):
    engine = _installed_engine(tmp_path)
    with Session(engine) as session:
        # 与回补库一致：WAL 模式下由另一个进程写入
        session.connection().exec_driver_sql("PRAGMA journal_mode=WAL")
        session.commit()
        before = data_revision.status(session).version
    engine.dispose()
    process = multiprocessing.get_context("spawn").Process(
        target=_child_update, args=(str(tmp_path / "rev.db"),)
    )
    process.start()
    process.join(timeout=60)
    assert process.exitcode == 0
    engine = create_engine(f"sqlite:///{tmp_path / 'rev.db'}", future=True)
    with Session(engine) as session:
        after = data_revision.status(session).version
    assert before != after


def test_k_ttl_and_lru(monkeypatch, tmp_path):
    engine = _installed_engine(tmp_path)
    calls = _counting(monkeypatch)
    monkeypatch.setattr(research_screen, "_CACHE_MAX_ENTRIES", 1)
    with Session(engine) as session:
        research_screen.low_start_rows(session, trade_date=DAY, limit=10)
        research_screen.low_start_rows(session, trade_date=DAY, limit=20)
        research_screen.low_start_rows(session, trade_date=DAY, limit=10)
    assert calls["n"] == 3
    monkeypatch.setattr(research_screen, "_CACHE_TTL_SECONDS", 0.0)
    with Session(engine) as session:
        research_screen.low_start_rows(session, trade_date=DAY, limit=10)
        research_screen.low_start_rows(session, trade_date=DAY, limit=10)
    assert calls["n"] == 5


def test_l_memory_database_is_not_cached(monkeypatch):
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    calls = _counting(monkeypatch)
    with Session(engine) as session:
        research_screen.low_start_rows(session, trade_date=DAY)
        research_screen.low_start_rows(session, trade_date=DAY)
    assert calls["n"] == 2


def test_m_cached_object_is_copied(tmp_path):
    engine = _installed_engine(tmp_path)
    payload = {"trade_date": DAY, "rows": [{"code": "000001"}], "coverage": {}, "strategy": {}}
    with Session(engine) as session:
        research_screen._cached(session, "x", DAY, (), lambda: payload)
        first = research_screen._cached(session, "x", DAY, (), lambda: payload)
        first["rows"].append({"code": "POLLUTED"})
        second = research_screen._cached(session, "x", DAY, (), lambda: payload)
    assert [row["code"] for row in second["rows"]] == ["000001"]


def test_n_write_during_compute_recomputes(tmp_path):
    """R5-03：计算期间另一个连接提交，结果必须重算并带新版本缓存。"""
    engine = _installed_engine(tmp_path)
    values: list[float] = []

    def fake():
        with Session(engine) as session:
            close = session.scalar(
                text("SELECT close FROM stock_daily WHERE code='000001' AND trade_date=:d"),
                {"d": DAY},
            )
        values.append(close)
        if len(values) == 1:
            with Session(engine) as writer:
                writer.execute(
                    text("UPDATE stock_daily SET close = 99 WHERE code='000001' AND trade_date=:d"),
                    {"d": DAY},
                )
                writer.commit()
        return {"trade_date": DAY, "rows": [], "coverage": {}, "strategy": {}, "close": close}

    with Session(engine) as session:
        result = research_screen._cached(session, "snap", DAY, (), fake)
        cached = research_screen._cached(session, "snap", DAY, (), fake)
    assert values == [10.0, 99.0]
    assert result["close"] == 99.0
    assert cached["close"] == 99.0


def test_o_single_flight_merges_concurrent_calls(tmp_path):
    engine = _installed_engine(tmp_path)
    calls = {"n": 0}
    lock = threading.Lock()

    def slow():
        with lock:
            calls["n"] += 1
        time.sleep(0.3)
        return {"trade_date": DAY, "rows": [], "coverage": {}, "strategy": {}}

    results: list[dict] = []

    def worker():
        with Session(engine) as session:
            results.append(research_screen._cached(session, "flight", DAY, (), slow))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert calls["n"] == 1
    assert len(results) == 5


def test_p_database_error_propagates_without_stale_cache(tmp_path):
    engine = _installed_engine(tmp_path)

    def boom():
        raise RuntimeError("数据库读取失败")

    with Session(engine) as session:
        with pytest.raises(RuntimeError):
            research_screen._cached(session, "boom", DAY, (), boom)
        value = research_screen._cached(
            session, "boom", DAY, (), lambda: {"trade_date": DAY, "rows": [], "coverage": {}, "strategy": {}}
        )
    assert value["trade_date"] == DAY


def test_q_trigger_advances_per_row(tmp_path):
    """SQLite 触发器逐行推进：一批 N 行写入 → revision 增加 N。"""
    engine = _installed_engine(tmp_path)
    with Session(engine) as session:
        before = dict(data_revision.status(session).version[1]).get("stock_daily", 0)
        for index in range(100):
            session.add(
                StockDaily(
                    trade_date=DAY,
                    code=f"9{index:05d}",
                    name="批量",
                    open=10,
                    high=10,
                    low=10,
                    close=10,
                    pct_chg=0.0,
                    amount=1e8,
                )
            )
        session.commit()
        after = dict(data_revision.status(session).version[1])["stock_daily"]
    assert after - before == 100


def test_install_is_idempotent(tmp_path):
    engine = _installed_engine(tmp_path)
    with Session(engine) as session:
        first = data_revision.install(session)
        second = data_revision.install(session)
    assert first == second


def test_required_triggers_exist(tmp_path):
    engine = _installed_engine(tmp_path)
    with Session(engine) as session:
        names = {
            row[0]
            for row in session.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger'")
            )
        }
    for table in ("stock_daily", "rps_daily", "sector_member", "sector_daily", "stock_universe", "trade_calendar"):
        for suffix in ("ai", "au", "ad"):
            assert f"trg_{table}_{suffix}" in names


def test_cache_stats_count_hit_and_compute(tmp_path, monkeypatch):
    engine = _installed_engine(tmp_path)
    _counting(monkeypatch)
    before = research_screen.cache_stats()
    with Session(engine) as session:
        research_screen.low_start_rows(session, trade_date=DAY)
        research_screen.low_start_rows(session, trade_date=DAY)
    after = research_screen.cache_stats()
    assert after["computes"] - before["computes"] == 1
    assert after["hits"] - before["hits"] == 1


def test_cache_stats_count_disabled(tmp_path, monkeypatch):
    engine = _engine(tmp_path / "plain_stats.db")
    with Session(engine) as session:
        _seed_stock(session)
    _counting(monkeypatch)
    research_screen.clear_cache()
    before = research_screen.cache_stats()
    with Session(engine) as session:
        research_screen.low_start_rows(session, trade_date=DAY)
        research_screen.low_start_rows(session, trade_date=DAY)
    after = research_screen.cache_stats()
    assert after["cache_disabled"] - before["cache_disabled"] == 2
    assert after["computes"] == before["computes"]


def test_leader_failure_releases_slot_and_waiter_recovers(tmp_path, monkeypatch):
    """Leader 抛异常时：等待方重算、槽位与 inflight 状态都释放。"""
    engine = _installed_engine(tmp_path)
    calls = {"n": 0}
    lock = threading.Lock()

    def flaky(session, day, **kwargs):
        with lock:
            calls["n"] += 1
            first = calls["n"] == 1
        if first:
            raise RuntimeError("leader 失败")
        time.sleep(0.1)
        return {"trade_date": DAY, "rows": [], "coverage": {}, "strategy": {}}

    monkeypatch.setattr(research_screen, "_low_start_rows", flaky)
    results: list[dict] = []
    errors: list[str] = []

    def worker():
        try:
            with Session(engine) as session:
                results.append(research_screen.low_start_rows(session, trade_date=DAY))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    # Leader 的请求按语义失败；等待方重算成功，且不会收到 leader 的错误
    assert len(errors) == 1 and "leader 失败" in errors[0]
    assert len(results) == 1
    assert calls["n"] >= 2
    # 槽位全部归还：两次非阻塞获取都应成功
    assert research_screen._LOW_START_SLOTS.acquire(blocking=False)
    assert research_screen._LOW_START_SLOTS.acquire(blocking=False)
    research_screen._LOW_START_SLOTS.release()
    research_screen._LOW_START_SLOTS.release()
    assert not research_screen._INFLIGHT
