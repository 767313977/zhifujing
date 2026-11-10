"""RPS 一线红排序 NULL 安全测试（P0-1 修复）。

覆盖任务书 §1.3 全部验收场景：三档均有值、一档 NULL、两档 NULL、三档全 NULL、
只有单档达标、边界值 90、稳定排序、显式日期一致。
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    Base,
    RpsDaily,
    StockBasic,
    StockDaily,
)
from app.services import pools

DAY = date(2026, 10, 23)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _seed(
    session: Session,
    code: str,
    *,
    rps50: float | None,
    rps120: float | None,
    rps250: float | None,
    close: float = 10.0,
    pct_chg: float = 0.0,
) -> None:
    session.add(
        StockBasic(code=code, name=f"票{code}"),
    )
    session.add(
        StockDaily(
            trade_date=DAY, code=code, close=close, pct_chg=pct_chg,
            open=close, high=close, low=close, amount=1_000_000, volume=100_000,
        )
    )
    session.add(
        RpsDaily(
            trade_date=DAY, code=code,
            rps50=rps50, rps120=rps120, rps250=rps250,
            ret50=None, ret120=None, ret250=None,
        )
    )


def _run_line_red(session: Session):
    """跑一遍 line_red 模式，返回 (有效RPS最大值, code) 列表（按排序顺序）。"""
    board = pools.rps_board(session, trade_date=DAY, mode="line_red", limit=1000)
    return [
        (max((v for v in (row.rps50, row.rps120, row.rps250) if v is not None), default=None), row.code)
        for row in board.rows
    ]


# ---- 场景 1-3：NULL 组合 -----------------------------------------------

def test_all_three_have_values(session):
    """三档均有值 → 取最大值排序。"""
    _seed(session, "000001", rps50=95, rps120=93, rps250=91)  # max=95
    _seed(session, "000002", rps50=90, rps120=89, rps250=88)  # max=90
    session.commit()
    result = _run_line_red(session)
    codes = [c for _, c in result]
    assert codes == ["000001", "000002"], f"期望 000001 排第一，实际 {codes}"


def test_one_null(session):
    """一档 NULL → 取非 NULL 中的最大值。GitHub 旧代码会把这票排到 NULL 位置。"""
    _seed(session, "000001", rps50=95, rps120=None, rps250=91)  # 有效 max=95
    _seed(session, "000002", rps50=90, rps120=89, rps250=88)     # max=90
    session.commit()
    result = _run_line_red(session)
    codes = [c for _, c in result]
    assert codes == ["000001", "000002"], f"NULL 一档的票应排第一，实际 {codes}"


def test_two_nulls(session):
    """两档 NULL → 取唯一非 NULL 值。"""
    _seed(session, "000001", rps50=None, rps120=97, rps250=None)  # 有效 max=97
    _seed(session, "000002", rps50=90, rps120=89, rps250=88)       # max=90
    session.commit()
    result = _run_line_red(session)
    codes = [c for _, c in result]
    assert codes == ["000001", "000002"], f"两档 NULL 的票应排第一，实际 {codes}"


def test_all_three_null(session):
    """三档全 NULL → 一线红 where 子句不会选中它（where 里用 >= 与 NULL 比较都是 NULL → false）。"""
    _seed(session, "000001", rps50=None, rps120=None, rps250=None)
    _seed(session, "000002", rps50=95, rps120=93, rps250=91)
    session.commit()
    result = _run_line_red(session)
    codes = [c for _, c in result]
    assert codes == ["000002"], f"三档全 NULL 不应入选，实际 {codes}"


# ---- 场景 4-6：只有单档达标 ---------------------------------------------

def test_only_rps50_passes_threshold(session):
    _seed(session, "000001", rps50=92, rps120=50, rps250=50)
    session.commit()
    result = _run_line_red(session)
    codes = [c for _, c in result]
    assert codes == ["000001"]


def test_only_rps120_passes_threshold(session):
    _seed(session, "000001", rps50=50, rps120=92, rps250=50)
    session.commit()
    result = _run_line_red(session)
    codes = [c for _, c in result]
    assert codes == ["000001"]


def test_only_rps250_passes_threshold(session):
    _seed(session, "000001", rps50=50, rps120=50, rps250=92)
    session.commit()
    result = _run_line_red(session)
    codes = [c for _, c in result]
    assert codes == ["000001"]


# ---- 场景 7-9：边界值与稳定排序 -----------------------------------------

def test_boundary_exactly_90(session):
    """边界值恰好 90 → 应入选且排在 91 后面。"""
    _seed(session, "000001", rps50=90, rps120=50, rps250=50)
    _seed(session, "000002", rps50=91, rps120=50, rps250=50)
    session.commit()
    result = _run_line_red(session)
    codes = [c for _, c in result]
    assert codes == ["000002", "000001"]


def test_stable_tie_breaker_by_code(session):
    """相同有效 RPS 最大值 → 按 code 升序。"""
    _seed(session, "000002", rps50=95, rps120=None, rps250=80)
    _seed(session, "000001", rps50=95, rps120=70, rps250=88)
    session.commit()
    for _ in range(3):
        result = _run_line_red(session)
        codes = [c for _, c in result]
        assert codes == ["000001", "000002"], f"稳定排序失败：{codes}"


def test_explicit_date_matches_default(session):
    """显式交易日期与默认最新 RPS 日结果一致。"""
    _seed(session, "000001", rps50=95, rps120=None, rps250=91)
    _seed(session, "000002", rps50=None, rps120=97, rps250=None)
    session.commit()
    board_explicit = pools.rps_board(session, trade_date=DAY, mode="line_red", limit=1000)
    board_default = pools.rps_board(session, mode="line_red", limit=1000)
    assert [r.code for r in board_explicit.rows] == [r.code for r in board_default.rows]


# ---- 验收：SQLite 结果 = Python 独立计算 --------------------------------

def test_matches_python_independent_calculation(session):
    """最终必须与 Python 独立计算得到的有效 RPS 最大值排序完全一致。"""
    _seed(session, "A00001", rps50=95, rps120=None, rps250=91)   # 有效 max=95
    _seed(session, "A00002", rps50=90, rps120=89, rps250=88)     # max=90
    _seed(session, "A00003", rps50=None, rps120=97, rps250=None)  # 有效 max=97
    _seed(session, "A00004", rps50=None, rps120=None, rps250=85) # 85 < 90，不入选
    _seed(session, "A00005", rps50=90, rps120=None, rps250=None) # 90，入选
    session.commit()

    board = pools.rps_board(session, trade_date=DAY, mode="line_red", limit=1000)

    # Python 独立计算：对每一行取三个 RPS 中有效（非 None）的最大值
    def py_valid_max(row):
        vals = [v for v in (row.rps50, row.rps120, row.rps250) if v is not None]
        return max(vals) if vals else -1

    py_order = sorted(board.rows, key=lambda r: (-py_valid_max(r), r.code))

    assert [r.code for r in board.rows] == [r.code for r in py_order], (
        f"SQLite 排序 {[r.code for r in board.rows]} ≠ Python 独立排序 {[r.code for r in py_order]}"
    )
