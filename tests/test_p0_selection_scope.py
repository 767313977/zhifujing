"""P0-2：selection_scope / selection_cap / evaluated_count 语义边界验证。

区分「输出截断（limit=50，full_market）」和「昂贵过滤前的预截断（cap=300，top_n）」。
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, RpsDaily, StockBasic, StockDaily
from app.services import pools

DAY = date(2026, 10, 23)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed_many(session, n, *, rps50=95, rps120=95, rps250=95):
    """注入 n 只股票，全部通过 RPS 基础阈值。"""
    for i in range(n):
        code = f"{i:06d}"
        session.add(StockBasic(code=code, name=f"票{code}", industry="T-Main"))
        session.add(StockDaily(trade_date=DAY, code=code, close=10.0, pct_chg=1.0,
                               open=10, high=10.5, low=9.5, amount=1e8, volume=1e7))
        session.add(RpsDaily(trade_date=DAY, code=code,
                             rps50=rps50, rps120=rps120, rps250=rps250,
                             ret50=10, ret120=20, ret250=30))
    session.commit()


# ---- 纯强度排序：输出截断 ≠ 预截断 ----------------------------------------

def test_pure_strength_order_is_full_market(session):
    """纯强度排序 + limit=50 → 只输出截断，不是预截断 → full_market。"""
    _seed_many(session, 100)  # 100 只都达阈值
    board = pools.rps_board(session, trade_date=DAY, mode="all", limit=50)
    assert board.selection_scope == "full_market"
    assert board.selection_cap is None
    assert board.evaluated_count is None
    assert len(board.rows) == 50  # 输出截断


def test_high_limit_also_full_market(session):
    """纯强度排序 + limit=1000 → 也不是预截断。"""
    _seed_many(session, 100)
    board = pools.rps_board(session, trade_date=DAY, mode="all", limit=1000)
    assert board.selection_scope == "full_market"
    assert len(board.rows) == 100  # 库里只有 100 只


# ---- breakout/trend + 非 factor → top_n ----------------------------------

def test_breakout_only_becomes_top_n(session):
    """breakout_only=True + 非 factor → top_n。"""
    _seed_many(session, 100)
    board = pools.rps_board(session, trade_date=DAY, mode="all", limit=50,
                            breakout_only=True)
    assert board.selection_scope == "top_n"
    assert board.selection_cap is not None  # max(50*3, 300)=300
    assert board.evaluated_count == 100  # 库里只有 100 只都被扫描
    # rows 可能很少（因为 breakout 过滤把命中的留下；测试库里 PatternHit 没数据 → 全被过滤）


def test_breakout_top_n_cap_calculation(session):
    """验证 selection_cap 计算：max(limit*3, 300)。"""
    _seed_many(session, 500)  # 远大于 cap
    board = pools.rps_board(session, trade_date=DAY, mode="all", limit=50,
                            breakout_only=True)
    assert board.selection_scope == "top_n"
    assert board.selection_cap == max(50 * 3, 300) == 300
    assert board.evaluated_count == 300  # SQL limit 300 只


def test_small_limit_does_not_collapse_cap(session):
    """limit=10 → cap=max(30, 300)=300（300 是昂贵过滤的最低扫描量）。"""
    _seed_many(session, 500)
    board = pools.rps_board(session, trade_date=DAY, mode="all", limit=10,
                            breakout_only=True)
    assert board.selection_scope == "top_n"
    assert board.selection_cap == 300  # 不是 30
    assert board.evaluated_count == 300


# ---- factor 排序：始终 full_market ----------------------------------------

def test_factor_sort_is_full_market_even_with_breakout(session):
    """factor 排序即便是 breakout + limit 也不做预截断（需要全市场因子分）。"""
    _seed_many(session, 500)
    board = pools.rps_board(session, trade_date=DAY, mode="all", limit=50,
                            breakout_only=True, order="factor")
    assert board.selection_scope == "full_market"
    assert board.selection_cap is None
    assert board.evaluated_count is None


# ---- evaluated_count ≠ returned_count -------------------------------------

def test_evaluated_count_vs_returned_count_distinction(session):
    """evaluated_count = 进入昂贵过滤的候选数；returned_count = 最终输出数。"""
    _seed_many(session, 500, rps50=95, rps120=95, rps250=95)
    # 加几只 RPS 达阈值但没 breakout 的 → 会被过滤
    board = pools.rps_board(session, trade_date=DAY, mode="all", limit=50,
                            breakout_only=True)
    assert board.selection_scope == "top_n"
    # 进入昂贵过滤的是 cap=300 只
    assert board.evaluated_count == 300
    # 最终输出数应 ≤ evaluated_count（因为 breakout 过滤）
    assert len(board.rows) <= board.evaluated_count  # noqa: SLF001
    assert len(board.rows) <= 50  # limit


# ---- 空结果场景的 selection_scope ------------------------------------------

def test_no_data_is_full_market(session):
    """完全没数据 → full_market（不是 top_n 空结果）。"""
    board = pools.rps_board(session, trade_date=DAY, mode="all", limit=50)
    assert board.selection_scope == "full_market"
    assert board.rows == []


def test_dual_mode_full_market(session):
    """dual 模式（RPS50+120 阈值）→ 不昂贵过滤 → full_market。"""
    _seed_many(session, 100)
    board = pools.rps_board(session, trade_date=DAY, mode="dual", limit=50)
    assert board.selection_scope == "full_market"
