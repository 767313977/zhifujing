"""RPS 扫描的三条硬口径（2026-10-24「选股池-2」）：

1. 窗口按**交易日**截取，不是「最近 N 行」；
2. 当天日线覆盖率不到 90% 时**不写库**；
3. 当天没有日线的票不进当天的横截面排名。

`scan()` 用的是模块级 `session_scope`，测试照 `test_collect_activity` 的写法
换成内存库。
"""

from contextlib import contextmanager
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import Session

from app.db import upsert_many_fill
from app.jobs import scan_rps
from app.models import Base, RpsDaily, StockDaily, StockUniverse, TradeCalendar
from app.services import data_revision

# 从 2026-01-05（周一）起连续排 250 个「交易日」，实际跳过周末 —— 只用来看窗口，
# 不需要真的是交易所日历
START = date(2026, 1, 5)


def _days(count: int) -> list[date]:
    out: list[date] = []
    cursor = START
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


@pytest.fixture()
def engine():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return engine


def _patch_scope(monkeypatch, engine) -> None:
    @contextmanager
    def scope():
        with Session(engine) as session:
            yield session
            session.commit()

    monkeypatch.setattr(scan_rps, "session_scope", scope)


def _seed(session: Session, days: list[date], code: str, pct: float, skip: set[int]) -> None:
    """给一只票铺日线，`skip` 里的下标不铺（模拟停牌 / 漏采）。"""
    for index, day in enumerate(days):
        if index in skip:
            continue
        session.add(
            StockDaily(
                trade_date=day,
                code=code,
                name=code,
                close=10.0,
                pct_chg=pct,
            )
        )


def test_window_is_trading_days_not_rows(engine, monkeypatch):
    """中间缺 10 行时，`ret120` 只复利窗口内的行（不是「最近 120 行」）。"""
    days = _days(130)
    target = days[-1]
    # 缺 10 天（下标 60~69）→ 总共 120 行，但落在 120 交易日窗口内只有 110 行
    missing = set(range(60, 70))
    with Session(engine) as session:
        session.add_all(TradeCalendar(trade_date=day) for day in days)
        _seed(session, days, "000001", 1.0, missing)
        session.commit()

    _patch_scope(monkeypatch, engine)
    assert scan_rps.scan(target)["rows"] == 1

    with Session(engine) as session:
        row = session.scalar(select(RpsDaily).where(RpsDaily.code == "000001"))
    # 旧写法取「最近 120 行」会把窗口外的 10 行也算进来 → 1.01**120
    assert row.ret120 == pytest.approx(round((1.01**110 - 1) * 100, 2))
    # 只有 130 个交易日，250 日窗口覆盖不足 → 该档记 None
    assert row.ret250 is None
    assert row.rps250 is None


def test_skips_when_day_coverage_too_low(engine, monkeypatch):
    """当天只有一半的票有日线 → 不写库。"""
    days = _days(130)
    target = days[-1]
    with Session(engine) as session:
        session.add_all(TradeCalendar(trade_date=day) for day in days)
        # 池子 100 只，只有 50 只有当天日线（另 50 只连历史都没有）
        session.add_all(
            StockUniverse(code=f"000{index:03d}") for index in range(100)
        )
        for index in range(50):
            _seed(session, days, f"000{index:03d}", 1.0, set())
        session.commit()

    _patch_scope(monkeypatch, engine)
    result = scan_rps.scan(target)
    assert result == {
        "rows": 0,
        "trade_date": target,
        "skipped": "coverage",
        "cost_seconds": result["cost_seconds"],
    }
    with Session(engine) as session:
        assert session.scalar(select(RpsDaily).limit(1)) is None


def test_stock_without_target_day_is_excluded(engine, monkeypatch):
    """当天停牌的票不进当天排名 —— 收益截止在不同日子没法比。"""
    days = _days(130)
    target = days[-1]
    with Session(engine) as session:
        session.add_all(TradeCalendar(trade_date=day) for day in days)
        _seed(session, days, "000001", 1.0, set())
        # 000002 历史够，但最后 3 天没有行（停牌）
        _seed(session, days, "000002", 1.0, {127, 128, 129})
        session.commit()

    _patch_scope(monkeypatch, engine)
    assert scan_rps.scan(target)["rows"] == 1

    with Session(engine) as session:
        codes = list(session.scalars(select(RpsDaily.code)))
    assert codes == ["000001"]


# ---------------------------------------------------------------------------
# B0.3：20 / 60 日扩展
# ---------------------------------------------------------------------------


def test_new_windows_do_not_change_old_semantics(engine, monkeypatch):
    """新增 20/60 日窗口不能改变 50/120/250 的数值与含义。"""
    days = _days(260)
    target = days[-1]
    with Session(engine) as session:
        session.add_all(TradeCalendar(trade_date=day) for day in days)
        _seed(session, days, "000001", 1.0, set())
        _seed(session, days, "000002", 0.5, set())
        _seed(session, days, "000003", -0.5, set())
        session.commit()

    _patch_scope(monkeypatch, engine)
    result = scan_rps.scan(target, with_meta=True)
    assert result["rows"] == 3
    # 默认返回值仍保持旧键（向后兼容）
    plain = scan_rps.scan(target)
    assert set(plain) == {"rows", "trade_date", "cost_seconds"}

    with Session(engine) as session:
        row = session.scalar(select(RpsDaily).where(RpsDaily.code == "000001"))
    for window in (20, 50, 60, 120, 250):
        expected = round((1.01**window - 1) * 100, 2)
        assert getattr(row, f"ret{window}") == pytest.approx(expected)
        assert getattr(row, f"rps{window}") is not None
    # 涨得最多的票在每个窗口都应是最高分位（3 只票时中位秩最高是 83.3，不是 100）
    assert row.rps20 > 80
    assert row.rps250 > 80


def test_pool_count_override_allows_sparse_history(engine, monkeypatch):
    """历史回补用「当时股票池」估计值，而不是当前 stock_universe。"""
    days = _days(130)
    target = days[-1]
    with Session(engine) as session:
        session.add_all(TradeCalendar(trade_date=day) for day in days)
        session.add_all(StockUniverse(code=f"000{index:03d}") for index in range(100))
        for index in range(50):
            _seed(session, days, f"000{index:03d}", 1.0, set())
        session.commit()

    _patch_scope(monkeypatch, engine)
    # 当前池 100、当天只有 50 行 → 旧口径直接停跑
    assert scan_rps.scan(target)["skipped"] == "coverage"
    # 当时池估计 50 → 50/50 通过
    result = scan_rps.scan(target, pool_count=50, with_meta=True)
    assert result["rows"] == 50
    assert result["pool_basis"] == "estimated_history"


def test_estimate_pool_count_uses_prior_days(engine):
    """估计口径 = 目标日之前 20 个交易日行数的中位数（不含目标日）。"""
    days = _days(30)
    target = days[-1]
    with Session(engine) as session:
        session.add_all(TradeCalendar(trade_date=day) for day in days)
        # 前 29 天各 10 只，目标日只有 2 只
        for day in days[:-1]:
            for index in range(10):
                session.add(StockDaily(trade_date=day, code=f"000{index:03d}", name="x", close=10, pct_chg=1.0))
        for index in range(2):
            session.add(StockDaily(trade_date=target, code=f"000{index:03d}", name="x", close=10, pct_chg=1.0))
        session.commit()
        assert scan_rps.estimate_pool_count(session, target) == 10


def test_scan_is_idempotent(engine, monkeypatch):
    """同一输入重复扫描：行数不增、数值不变。"""
    days = _days(260)
    target = days[-1]
    with Session(engine) as session:
        session.add_all(TradeCalendar(trade_date=day) for day in days)
        _seed(session, days, "000001", 1.0, set())
        _seed(session, days, "000002", 0.5, set())
        session.commit()

    _patch_scope(monkeypatch, engine)
    first = scan_rps.scan(target)
    with Session(engine) as session:
        before = {
            row.code: (row.ret20, row.ret60, row.rps20, row.rps60)
            for row in session.scalars(select(RpsDaily))
        }
    second = scan_rps.scan(target)
    with Session(engine) as session:
        after = {
            row.code: (row.ret20, row.ret60, row.rps20, row.rps60)
            for row in session.scalars(select(RpsDaily))
        }
    assert first["rows"] == second["rows"] == 2
    assert before == after


# ---------------------------------------------------------------------------
# 2026-11-10：写入改 `upsert_many_fill`（None 不覆盖），过期单元格显式清空
#
# 分两层验证，边界必须分清：
# - 写入层（`upsert_many_fill`）：新值 None **不覆盖**库里的非空值；
# - 业务层（`scan()`）：本次重算**判定为不可算**的单元格，先显式清成 NULL 再写 ——
#   否则 COALESCE 会把「底层行情修订后已失效」的旧分位一直留在库里冒充当前值。
# ---------------------------------------------------------------------------

DAY = date(2026, 1, 5)


def _put(session: Session, **values) -> None:
    """按主键写入/覆盖一条 RPS 行（直接 ORM，不走被测的批量写入）。"""
    row = session.get(RpsDaily, (DAY, "000001"))
    if row is None:
        row = RpsDaily(trade_date=DAY, code="000001")
        session.add(row)
    for name, value in values.items():
        setattr(row, name, value)
    session.commit()


def _get(session: Session, code: str = "000001", day: date = DAY) -> RpsDaily:
    session.expire_all()  # 批量写入走 Core，ORM 身份图里的旧对象需要刷新
    return session.get(RpsDaily, (day, code))


def test_fill_keeps_existing_when_new_is_none(engine):
    """场景 1：原值非 NULL、新值 None → 原值保留。"""
    with Session(engine) as session:
        _put(session, ret20=5.0, rps20=90.0)
        upsert_many_fill(
            session,
            RpsDaily,
            [{"trade_date": DAY, "code": "000001", "ret20": None, "rps20": None}],
        )
        session.commit()
        row = _get(session)
        assert (row.ret20, row.rps20) == (5.0, 90.0)


def test_fill_overwrites_with_valid_value(engine):
    """场景 2：原值非 NULL、新值为有效数值 → 使用新值。"""
    with Session(engine) as session:
        _put(session, ret20=5.0, rps20=90.0)
        upsert_many_fill(
            session,
            RpsDaily,
            [{"trade_date": DAY, "code": "000001", "ret20": 7.5, "rps20": 42.0}],
        )
        session.commit()
        row = _get(session)
        assert (row.ret20, row.rps20) == (7.5, 42.0)


def test_fill_writes_zero(engine):
    """场景 3：新值为 0 → 正确更新为 0（0 不是 NULL，不能被当成「没算」）。"""
    with Session(engine) as session:
        _put(session, ret20=5.0, rps20=90.0)
        upsert_many_fill(
            session,
            RpsDaily,
            [{"trade_date": DAY, "code": "000001", "ret20": 0.0, "rps20": 0.0}],
        )
        session.commit()
        row = _get(session)
        assert (row.ret20, row.rps20) == (0.0, 0.0)


def test_fill_fills_null(engine):
    """场景 4：原值 NULL、新值非 NULL → 正确填充。"""
    with Session(engine) as session:
        _put(session, ret20=None, rps20=None)
        upsert_many_fill(
            session,
            RpsDaily,
            [{"trade_date": DAY, "code": "000001", "ret20": 3.25, "rps20": 61.0}],
        )
        session.commit()
        row = _get(session)
        assert (row.ret20, row.rps20) == (3.25, 61.0)


def test_fill_both_null_stays_null(engine):
    """场景 5：新旧值均 NULL → 保持 NULL。"""
    with Session(engine) as session:
        _put(session, ret20=None, rps20=None)
        upsert_many_fill(
            session,
            RpsDaily,
            [{"trade_date": DAY, "code": "000001", "ret20": None, "rps20": None}],
        )
        session.commit()
        row = _get(session)
        assert row.ret20 is None and row.rps20 is None


def test_fill_handles_columns_independently(engine):
    """场景 6：一行里部分指标 None、部分有新值 → 逐列处理，互不影响。"""
    with Session(engine) as session:
        _put(session, ret20=5.0, rps20=80.0, ret50=6.0, rps50=70.0)
        upsert_many_fill(
            session,
            RpsDaily,
            [
                {
                    "trade_date": DAY,
                    "code": "000001",
                    "ret20": None,  # 保留 5.0
                    "rps20": 95.0,  # 覆盖 80.0
                    "ret50": 0.0,  # 覆盖成 0
                    "rps50": None,  # 保留 70.0
                }
            ],
        )
        session.commit()
        row = _get(session)
        assert row.ret20 == 5.0
        assert row.rps20 == 95.0
        assert row.ret50 == 0.0
        assert row.rps50 == 70.0


def test_scan_clears_cell_when_window_becomes_uncomputable(engine, monkeypatch):
    """底层行情删行让 250 日窗口覆盖不足 → 该档必须清空，不能被 COALESCE 留成当前值。"""
    days = _days(260)
    target = days[-1]
    with Session(engine) as session:
        session.add_all(TradeCalendar(trade_date=day) for day in days)
        _seed(session, days, "000001", 1.0, set())
        _seed(session, days, "000002", 0.5, set())
        session.commit()

    _patch_scope(monkeypatch, engine)
    scan_rps.scan(target)
    with Session(engine) as session:
        before = _get(session, "000001", target)
        assert before.ret250 is not None and before.rps250 is not None
        ret120_before = before.ret120

    # 删掉 250 日窗口最前面的 30 天（落在 120 日窗口之外）→ 250 档 220/250 = 88%
    with Session(engine) as session:
        session.execute(
            delete(StockDaily).where(
                StockDaily.code == "000001", StockDaily.trade_date.in_(days[10:40])
            )
        )
        session.commit()

    scan_rps.scan(target)
    with Session(engine) as session:
        after = _get(session, "000001", target)
        assert after.ret250 is None and after.rps250 is None
        assert after.ret120 == pytest.approx(ret120_before)
        assert after.rps120 is not None
        # 另一只票没被删行，250 档照常
        other = _get(session, "000002", target)
        assert other.ret250 is not None


def test_scan_clears_row_when_target_day_bar_removed(engine, monkeypatch):
    """当天日线被修订删掉 → 该行指标清空，但主键行保留（有行无值 = 缺失/待重算）。"""
    days = _days(130)
    target = days[-1]
    with Session(engine) as session:
        session.add_all(TradeCalendar(trade_date=day) for day in days)
        _seed(session, days, "000001", 1.0, set())
        _seed(session, days, "000002", 0.5, set())
        session.commit()

    _patch_scope(monkeypatch, engine)
    scan_rps.scan(target)
    with Session(engine) as session:
        assert _get(session, "000001", target).ret20 is not None
        session.execute(
            delete(StockDaily).where(
                StockDaily.code == "000001", StockDaily.trade_date == target
            )
        )
        session.commit()

    assert scan_rps.scan(target)["rows"] == 1
    with Session(engine) as session:
        row = _get(session, "000001", target)
        assert row is not None  # 主键行保留
        for window in (20, 50, 60, 120, 250):
            assert getattr(row, f"ret{window}") is None
            assert getattr(row, f"rps{window}") is None
        assert _get(session, "000002", target).ret20 is not None


def test_scan_backfills_multiple_days_without_touching_history(engine, monkeypatch):
    """多日回填 + 重复执行：逐日写入，历史日期与其他业务表不被改写。"""
    days = _days(270)
    targets = [days[-3], days[-2], days[-1]]
    sentinel_day = days[100]
    with Session(engine) as session:
        session.add_all(TradeCalendar(trade_date=day) for day in days)
        _seed(session, days, "000001", 1.0, set())
        _seed(session, days, "000002", 0.5, set())
        session.add(StockUniverse(code="000001"))
        session.add(StockUniverse(code="000002"))
        # 哨兵：一段早已存在的历史 RPS，任何一次扫描都不该碰它
        session.add(
            RpsDaily(trade_date=sentinel_day, code="000001", ret20=1.23, rps20=45.6)
        )
        session.commit()

    _patch_scope(monkeypatch, engine)
    for target in targets:
        assert scan_rps.scan(target)["rows"] == 2

    with Session(engine) as session:
        snapshot = {
            (row.trade_date, row.code): (row.ret20, row.ret250, row.rps250)
            for row in session.scalars(select(RpsDaily))
        }
    # 重复执行中间那天：不新增行、不改数值
    assert scan_rps.scan(targets[1])["rows"] == 2
    with Session(engine) as session:
        again = {
            (row.trade_date, row.code): (row.ret20, row.ret250, row.rps250)
            for row in session.scalars(select(RpsDaily))
        }
        sentinel = _get(session, "000001", sentinel_day)
    assert again == snapshot
    assert (sentinel.ret20, sentinel.rps20) == (1.23, 45.6)
    assert set(again) == {(day, code) for day in targets for code in ("000001", "000002")} | {
        (sentinel_day, "000001")
    }


# ---------------------------------------------------------------------------
# 2026-11-10：清理前的三道安全前提（行情完整性 / 清理规模上限）
#
# 目标：宁可停跑并报「重新采集」，也不拿残缺行情当「历史数据被删除」去清空。
# ---------------------------------------------------------------------------


def test_scan_clears_stale_row_when_day_is_complete(engine, monkeypatch):
    """场景 1：当日 K 线完整、待清理行在正常范围内 → 正常清空过期指标。"""
    days = _days(130)
    target = days[-1]
    with Session(engine) as session:
        session.add_all(TradeCalendar(trade_date=day) for day in days)
        _seed(session, days, "000001", 1.0, set())
        _seed(session, days, "000002", 0.5, set())
        # 库内该日有一行已无对应 K 线（行情被修订删掉），规模远低于上限 → 应清空
        session.add(RpsDaily(trade_date=target, code="000099", ret20=9.9, rps20=99.0))
        session.commit()

    _patch_scope(monkeypatch, engine)
    result = scan_rps.scan(target)
    assert "skipped" not in result
    assert result["rows"] == 2
    with Session(engine) as session:
        stale = _get(session, "000099", target)
        assert stale is not None  # 保留主键行
        for window in (20, 50, 60, 120, 250):
            assert getattr(stale, f"ret{window}") is None
            assert getattr(stale, f"rps{window}") is None
        assert _get(session, "000001", target).ret20 is not None


def test_scan_refuses_clear_when_day_bars_incomplete(engine, monkeypatch):
    """场景 2：当日 K 线比前 5 个交易日中位数少 100 行且低于 90% → 拒绝清理。

    采集没跑完时「今天少了一批 K 线」不等于「这些股票的历史数据被删了」，
    必须停在原地并报重新采集，不能把旧 RPS 清空。
    """
    days = _days(6)
    target = days[-1]
    with Session(engine) as session:
        session.add_all(TradeCalendar(trade_date=day) for day in days)
        # 前 5 个交易日各 200 只，目标日只到 100 只（采集中途）
        for day in days[:-1]:
            for index in range(200):
                session.add(
                    StockDaily(
                        trade_date=day, code=f"B{index:03d}", name="x", close=10, pct_chg=1.0
                    )
                )
        for index in range(100):
            session.add(
                StockDaily(
                    trade_date=target, code=f"B{index:03d}", name="x", close=10, pct_chg=1.0
                )
            )
        session.add(RpsDaily(trade_date=target, code="B000", ret20=5.0, rps20=90.0))
        session.commit()

    _patch_scope(monkeypatch, engine)
    result = scan_rps.scan(target, with_meta=True)
    assert result["skipped"] == "incomplete_bars"
    assert result["rows"] == 0
    assert result["day_rows"] == 100
    with Session(engine) as session:
        row = _get(session, "B000", target)
        assert (row.ret20, row.rps20) == (5.0, 90.0)  # 旧值原样保留，未被破坏性清理


def test_clear_cells_skips_already_null_and_bumps_revision_once(engine):
    """场景 3：已全 NULL 的行不产生 UPDATE；真正清理只推一格 revision。"""
    with Session(engine) as session:
        data_revision.install(session)
        session.add(RpsDaily(trade_date=DAY, code="000001"))  # 全 NULL
        session.commit()

        base = _rps_revision(session)
        assert scan_rps._clear_cells(session, DAY, ["000001"], scan_rps._METRIC_COLUMNS) == 0
        session.commit()
        assert _rps_revision(session) == base  # 全 NULL 不触发 UPDATE

        _put(session, ret20=5.0, rps20=90.0)
        after_put = _rps_revision(session)
        # 一行里多列同时清空也只算 1 行、只推一格 revision
        assert scan_rps._clear_cells(session, DAY, ["000001"], scan_rps._METRIC_COLUMNS) == 1
        session.commit()
        assert _rps_revision(session) == after_put + 1

        # 再清一次：已全 NULL，不再产生无效写入
        assert scan_rps._clear_cells(session, DAY, ["000001"], scan_rps._METRIC_COLUMNS) == 0
        session.commit()
        assert _rps_revision(session) == after_put + 1


def test_partial_clear_failure_rolls_back_whole_day(engine, monkeypatch):
    """场景 4：局部清理失败 → 整个目标日期事务回滚，已清掉的第一批也恢复原值。"""
    days = _days(260)
    target = days[-1]
    with Session(engine) as session:
        session.add_all(TradeCalendar(trade_date=day) for day in days)
        _seed(session, days, "000001", 1.0, set())
        _seed(session, days, "000002", 0.5, set())
        session.commit()

    _patch_scope(monkeypatch, engine)
    scan_rps.scan(target)
    with Session(engine) as session:
        before_1 = _get(session, "000001", target)
        before_values = (before_1.ret20, before_1.rps20, before_1.ret250, before_1.rps250)
        before_2_ret250 = _get(session, "000002", target).ret250
    assert all(value is not None for value in before_values)
    assert before_2_ret250 is not None

    with Session(engine) as session:
        # 000001 当天 K 线被删 → 第一批整行清理
        session.execute(
            delete(StockDaily).where(
                StockDaily.code == "000001", StockDaily.trade_date == target
            )
        )
        # 000002 的 250 日窗口缺 30 天 → 第二批按窗口清理
        session.execute(
            delete(StockDaily).where(
                StockDaily.code == "000002", StockDaily.trade_date.in_(days[10:40])
            )
        )
        session.commit()

    real_clear = scan_rps._clear_cells
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("模拟局部清理失败")
        return real_clear(*args, **kwargs)

    monkeypatch.setattr(scan_rps, "_clear_cells", flaky)
    with pytest.raises(RuntimeError):
        scan_rps.scan(target)

    with Session(engine) as session:
        after_1 = _get(session, "000001", target)
        assert (after_1.ret20, after_1.rps20, after_1.ret250, after_1.rps250) == before_values
        assert _get(session, "000002", target).ret250 == before_2_ret250


def test_scan_refuses_clear_when_stale_scope_too_large(engine, monkeypatch):
    """场景 5：待清理行超过 max(50, 5%) → 整轮拒绝（不写不清），旧数据不被覆盖。"""
    days = _days(130)
    target = days[-1]
    with Session(engine) as session:
        session.add_all(TradeCalendar(trade_date=day) for day in days)
        _seed(session, days, "000001", 1.0, set())
        _seed(session, days, "000002", 0.5, set())
        # 库内该日 60 行已无对应 K 线 → 待清理 60 行 > 上限 max(50, 3) = 50
        for index in range(60):
            session.add(
                RpsDaily(trade_date=target, code=f"S{index:03d}", ret20=1.23, rps20=45.6)
            )
        session.commit()

    _patch_scope(monkeypatch, engine)
    result = scan_rps.scan(target, with_meta=True)
    assert result["skipped"] == "stale_guard"
    assert result["rows"] == 0
    assert result["stale_rows"] >= 60
    with Session(engine) as session:
        assert _get(session, "S000", target).ret20 == 1.23
        # 当天有效的两只票也没有被写入（拒绝是整轮的）
        assert session.get(RpsDaily, (target, "000001")) is None


def _rps_revision(session: Session) -> int:
    return int(
        session.execute(
            text("SELECT revision FROM data_revision WHERE domain = 'rps'")
        ).scalar_one()
    )


def test_scan_failure_rolls_back_clears(engine, monkeypatch):
    """事务失败：清理动作与写入一起回滚，旧值原样保留。"""
    days = _days(130)
    target = days[-1]
    with Session(engine) as session:
        session.add_all(TradeCalendar(trade_date=day) for day in days)
        _seed(session, days, "000001", 1.0, set())
        _seed(session, days, "000002", 0.5, set())
        session.commit()

    _patch_scope(monkeypatch, engine)
    scan_rps.scan(target)
    with Session(engine) as session:
        before = (_get(session, "000001", target).ret20, _get(session, "000001", target).rps20)
        session.execute(
            delete(StockDaily).where(
                StockDaily.code == "000001", StockDaily.trade_date == target
            )
        )
        session.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("模拟写入失败")

    monkeypatch.setattr(scan_rps, "upsert_many_fill", boom)
    with pytest.raises(RuntimeError):
        scan_rps.scan(target)

    with Session(engine) as session:
        after = (_get(session, "000001", target).ret20, _get(session, "000001", target).rps20)
    assert after == before
