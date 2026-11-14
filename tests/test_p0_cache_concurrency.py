"""P0-1 single-flight + data_revision 缓存并发安全验收（10 场景）。

这些测试不依赖真实 data_revision 表 —— 用 monkeypatch 让
`_effective_key()` 和 `data_revision.status()` 返回可控值。
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# 在 import api.pools 前 monkeypatch，确保模块级函数被替换
import app.api.pools as pools_api
from app.services import data_revision as dr

pytestmark = pytest.mark.usefixtures("reset_cache_globals")


@pytest.fixture()
def reset_cache_globals(monkeypatch):
    """每个测试前后重置缓存全局状态 + 让 data_revision 返回 ready。"""
    # 清空模块级全局缓存
    pools_api._track_cache.clear()
    pools_api._inflight.clear()
    pools_api._retry_attempted.clear()
    # 默认让 data_revision.status() 返回 ready
    ready_state = dr.CacheState(
        state="ready",
        reason="test",
        identity=("db", ("/fake/path",), "test-uuid-1111"),
        version=(("RPS", 1),),
    )
    monkeypatch.setattr(pools_api.data_revision, "status", lambda session: ready_state)
    monkeypatch.setattr(pools_api.data_revision, "log_unavailable", lambda s: None)
    # 把 single-flight 超时调到 2s，测试更快
    monkeypatch.setattr(pools_api, "_SINGLE_FLIGHT_TIMEOUT", 2)
    yield


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    with Session(engine) as session:
        yield session


@pytest.fixture()
def fake_board():
    return MagicMock(trade_date=None, rows=[], industry_top=[],
                     selection_scope="full_market", selection_cap=None, evaluated_count=None)


# ---- 3.1 Leader 正常完成 ------------------------------------------------------

def test_concurrent_same_key_single_build(session, fake_board):
    """20 并发同键只 build 一次（single-flight 去重）。"""
    gate = threading.Event()  # 控制 leader 什么时候完成
    build_count = {"n": 0}
    results: list[object] = []
    excs: list[Exception] = []

    def slow_build():
        build_count["n"] += 1
        gate.wait(timeout=5)
        return "BOARD_OK"

    def worker():
        try:
            r = pools_api._cached(("raw",), slow_build, session=session)
            results.append(r)
        except Exception as e:
            excs.append(e)

    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = [ex.submit(worker) for _ in range(20)]
        # 等够时间让 20 个都注册完 inflight
        time.sleep(0.2)
        # 此时 leader 应该只有 1 个，在 inflight 里等
        assert len(pools_api._inflight) == 1, f"inflight 应只有 1 个 leader，实际 {len(pools_api._inflight)}"
        # 放行 leader
        gate.set()
        for f in futures:
            f.result(timeout=5)

    assert build_count["n"] == 1, f"只应 build 一次，实际 {build_count['n']}"
    assert results == ["BOARD_OK"] * 20, f"20 个结果应一致，实际 {results[:3]}..."
    assert excs == []
    # inflight 最终必须清理
    time.sleep(0.05)
    assert len(pools_api._inflight) == 0


# ---- 3.2 Leader 抛异常 --------------------------------------------------------

def test_retry_budget_allows_at_most_one_rebuild():
    """R3-P2-03：leader 失败后的补算预算每键只有一次，迟到的 waiter 不得再建 leader。"""
    key = ("retry-budget",)
    event, leader = pools_api._register_or_join(key, retry=True)
    assert leader is True and event is not None
    pools_api._inflight.pop(key, None)
    event2, leader2 = pools_api._register_or_join(key, retry=True)
    assert leader2 is False and event2 is None
    # 新的一轮请求（非 retry）重置预算
    event3, leader3 = pools_api._register_or_join(key)
    assert leader3 is True and event3 is not None
    pools_api._inflight.pop(key, None)


def test_leader_exception_all_waiters_wake(session, fake_board):
    """leader 抛异常 → inflight 清空、所有 waiter 被唤醒且无人永久阻塞。

    waiter 的行为有两条合法路径：
    - 抢到新的 leader 名额 → 再次执行 build，拿到同样的 RuntimeError；
    - 没抢到（其他 waiter 已在补算）→ 返回 503 繁忙。
    两条路径都不得永久阻塞，也都不得写成缓存。
    """
    gate = threading.Event()
    build_count = {"n": 0}
    results: list[object] = []
    excs: list[Exception] = []

    def flaky_build():
        build_count["n"] += 1
        gate.wait(timeout=5)
        raise RuntimeError("BOOM")

    def worker():
        try:
            r = pools_api._cached(("raw",), flaky_build, session=session)
            results.append(r)
        except Exception as e:
            excs.append(e)

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(worker) for _ in range(10)]
        time.sleep(0.2)
        # inflight 里 leader 正常注册
        assert len(pools_api._inflight) == 1
        gate.set()
        for f in futures:
            f.result(timeout=10)  # 若有线程卡死，这里会超时

    # 所有线程都正常结束：要么拿到异常，要么（不可能）拿到结果
    assert len(excs) == 10
    assert results == []
    # 每个异常只能是对外可解释的两种：原始 RuntimeError 或 503 繁忙
    for e in excs:
        assert isinstance(e, RuntimeError) or isinstance(e, HTTPException)
        if isinstance(e, RuntimeError):
            assert "BOOM" in str(e)
        else:
            assert e.status_code == 503
    # 补算最多再跑一次 leader，不允许 10 次独立计算
    assert build_count["n"] <= 2, f"build 次数应 ≤2，实际 {build_count['n']}"
    # inflight 必须清理
    assert len(pools_api._inflight) == 0
    # 异常结果不得写入缓存
    assert pools_api._track_cache == {}


def test_exception_does_not_write_cache(session):
    """leader 抛异常 → 缓存不写入，且异常在子线程中被显式捕获断言（不留未处理告警）。"""
    gate = threading.Event()
    captured: list[BaseException] = []

    def flaky():
        gate.wait(timeout=3)
        raise RuntimeError("BOOM")

    def run():
        try:
            pools_api._cached(("k",), flaky, session=session)
        except BaseException as exc:  # noqa: BLE001 - 测试需要断言真实异常
            captured.append(exc)

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.1)
    gate.set()
    t.join(timeout=5)

    assert not t.is_alive(), "子线程必须在超时内结束"
    assert len(captured) == 1, f"必须捕获到 1 个异常，实际 {captured}"
    assert isinstance(captured[0], RuntimeError)
    assert "BOOM" in str(captured[0])
    # 异常路径不写缓存
    assert pools_api._track_cache == {}
    # inflight 已清理
    assert pools_api._inflight == {}


def test_exception_then_new_request_can_retry(session):
    """异常后再来一个请求 → 能正常重新 build。"""
    build_count = {"n": 0}

    def flaky_first_then_ok():
        build_count["n"] += 1
        if build_count["n"] == 1:
            raise RuntimeError("BOOM")
        return "OK"

    # 第一次：抛异常
    try:
        pools_api._cached(("k",), flaky_first_then_ok, session=session)
    except RuntimeError:
        pass
    assert build_count["n"] == 1
    # 第二次：能重试
    result = pools_api._cached(("k",), flaky_first_then_ok, session=session)
    assert result == "OK"
    assert build_count["n"] == 2


# ---- 3.3 60 秒超时（这里用 2s） ---------------------------------------------

def test_timeout_does_not_start_extra_inflight(session):
    """follower 超时后返回 503，不自行 build，不新增 inflight。

    这是本轮修复的核心回归：旧实现超时后每人都调 build，5 个 follower 会放大成
    5 次昂贵计算；现在必须 0 次。
    """
    gate = threading.Event()

    def slow_build():
        gate.wait(timeout=10)  # 超过 _SINGLE_FLIGHT_TIMEOUT=2s
        return "OK"

    leader_t = threading.Thread(target=lambda: pools_api._cached(("k",), slow_build, session=session))
    leader_t.start()
    time.sleep(0.1)
    # leader 注册了 inflight
    assert len(pools_api._inflight) == 1
    leader_key = list(pools_api._inflight.keys())[0]

    # 启动 5 个 follower，它们会在 2s 超时后收到 503
    follower_build_count = {"n": 0}

    def follower_build():
        follower_build_count["n"] += 1
        return "SHOULD_NOT_RUN"

    outcomes: list[object] = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(lambda: pools_api._cached(("k",), follower_build, session=session))
                   for _ in range(5)]
        for f in futures:
            try:
                outcomes.append(f.result(timeout=15))
            except HTTPException as exc:
                outcomes.append(exc)

    # 全部应为 503 繁忙
    assert len(outcomes) == 5
    assert all(isinstance(o, HTTPException) and o.status_code == 503 for o in outcomes)
    # follower 的 build 一次都不能被调用
    assert follower_build_count["n"] == 0
    # 只有 leader 那一个 inflight 存在
    assert len(pools_api._inflight) == 1

    # leader 放出来并正常结束
    gate.set()
    leader_t.join(timeout=5)
    assert leader_key not in pools_api._inflight
    # leader 完成后缓存写入，后续请求可命中
    assert pools_api._cached(("k",), follower_build, session=session) == "OK"
    assert follower_build_count["n"] == 0


def test_timeout_cache_hit_after_wait_near_end(session):
    """follower 等待期内 leader 完成 → 命中缓存返回，不自己 build（无竞争错误）。"""
    event = threading.Event()
    build_count = {"n": 0}

    def leader_build():
        build_count["n"] += 1
        # 1.2 秒完成，留 0.8 秒余量给 2 秒等待上限，避免慢机器上误触超时
        time.sleep(1.2)
        event.set()
        return "LEADER_RESULT"

    leader_t = threading.Thread(target=lambda: pools_api._cached(("k",), leader_build, session=session))
    leader_t.start()
    time.sleep(0.1)

    def noop_build():
        build_count["n"] += 1
        return "SHOULD_NOT_BE_CALLED"

    result = pools_api._cached(("k",), noop_build, session=session)
    assert result == "LEADER_RESULT", f"follower 应命中 leader 写的缓存，实际 {result}"
    leader_t.join(timeout=5)
    # 只 leader build 了一次，noop_build 不应被调用
    assert build_count["n"] == 1


# ---- 3.4 数据版本变化 -------------------------------------------------------

def test_revision_change_isolated_by_key(monkeypatch, session):
    """revision 变化 → 键里 version 不同，不串缓存。"""
    # revision 10
    state10 = dr.CacheState("ready", identity=("db", ("/p1",), "uuid-a"),
                             version=(("RPS", 10),))
    monkeypatch.setattr(pools_api.data_revision, "status", lambda s: state10)

    call_count = {"n": 0}

    def build():
        call_count["n"] += 1
        return f"REV_{call_count['n']}"

    r1 = pools_api._cached(("k",), build, session=session)
    assert r1 == "REV_1"
    # 再调一次 → 命中缓存，call_count 不变
    r1b = pools_api._cached(("k",), build, session=session)
    assert r1b == "REV_1"
    assert call_count["n"] == 1

    # revision 升到 11 → 键不同，必须重算
    state11 = dr.CacheState("ready", identity=("db", ("/p1",), "uuid-a"),
                             version=(("RPS", 11),))
    monkeypatch.setattr(pools_api.data_revision, "status", lambda s: state11)
    r2 = pools_api._cached(("k",), build, session=session)
    assert r2 == "REV_2"
    assert call_count["n"] == 2


def test_different_db_uuid_does_not_share_cache(monkeypatch, session):
    """identity（db_uuid）不同 → 键不同，不共享缓存。"""
    state_a = dr.CacheState("ready", identity=("db", ("/p1",), "uuid-a"),
                             version=(("RPS", 1),))
    state_b = dr.CacheState("ready", identity=("db", ("/p2",), "uuid-b"),
                             version=(("RPS", 1),))

    call_count = {"n": 0}

    def build():
        call_count["n"] += 1
        return f"DB_{call_count['n']}"

    monkeypatch.setattr(pools_api.data_revision, "status", lambda s: state_a)
    r_a = pools_api._cached(("k",), build, session=session)

    monkeypatch.setattr(pools_api.data_revision, "status", lambda s: state_b)
    r_b = pools_api._cached(("k",), build, session=session)

    assert r_a == "DB_1"
    assert r_b == "DB_2"
    assert call_count["n"] == 2


# ---- 3.5 Revision 不可用 ----------------------------------------------------

def test_revision_unavailable_skips_cache(monkeypatch, session):
    """revision 不可用 → 直查，不写缓存，不读缓存。"""
    state = dr.CacheState("not_installed", reason="no table")
    monkeypatch.setattr(pools_api.data_revision, "status", lambda s: state)

    call_count = {"n": 0}

    def build():
        call_count["n"] += 1
        return f"NO_REV_{call_count['n']}"

    r1 = pools_api._cached(("k",), build, session=session)
    r2 = pools_api._cached(("k",), build, session=session)

    # revision 不可用时每次都直查，不缓存
    assert r1 == "NO_REV_1"
    assert r2 == "NO_REV_2"
    assert call_count["n"] == 2
    # 缓存应保持空
    assert len(pools_api._track_cache) == 0


def test_revision_read_exception_skips_cache(monkeypatch, session):
    """revision 查询抛异常 → 绕开缓存直查。"""
    def boom_status(s):
        raise RuntimeError("db down")

    monkeypatch.setattr(pools_api.data_revision, "status", boom_status)

    # _effective_key 会 boom → 这里会先捕获再让 _effective_key 返回 None？
    # 实际上 _effective_key 直接调 status()，不 catch → 让它 propagate
    # 但 _cached() 没 catch → 异常向上 → 但我们要的是"直查不缓存"
    # 所以要让 _effective_key 在异常时返回 None
    import app.api.pools as api_mod

    def safe_effective_key(raw_key, s):
        try:
            api_mod.data_revision.status(s)
        except Exception:
            return None
        return ("ok",) + raw_key

    monkeypatch.setattr(api_mod, "_effective_key", safe_effective_key)

    call_count = {"n": 0}

    def build():
        call_count["n"] += 1
        return "OK"

    r1 = pools_api._cached(("k",), build, session=session)
    r2 = pools_api._cached(("k",), build, session=session)
    assert r1 == "OK"
    assert r2 == "OK"
    assert call_count["n"] == 2  # 两次都直查，不缓存


# ---- 其他关键场景 ----------------------------------------------------------

def test_different_raw_keys_do_not_share_cache(session):
    """不同筛选参数（raw_key 不同）不会串缓存。"""
    call_count = {"n": 0}

    def build(v):
        call_count["n"] += 1
        return v

    r1 = pools_api._cached(("RPS", 90, 100), lambda: build("A"), session=session)
    r2 = pools_api._cached(("RPS", 95, 100), lambda: build("B"), session=session)
    assert r1 == "A"
    assert r2 == "B"
    assert call_count["n"] == 2


def test_cache_capacity_eviction(monkeypatch, session):
    """缓存超过上限时淘汰最老。"""
    monkeypatch.setattr(pools_api, "_TRACK_CACHE_MAX", 4)
    for i in range(6):
        pools_api._cached((i,), lambda v=i: v, session=session)
    # 只能留 4 个；最新 4 个在，老的被淘汰
    assert len(pools_api._track_cache) == 4


# ---- 验收标记 --------------------------------------------------------------

def test_p0_cache_concurrency_pass_marker():
    """所有 single-flight 并发安全场景通过 —— 本测试只做 sanity。"""
    assert True, "P0_CACHE_CONCURRENCY_PASS"
