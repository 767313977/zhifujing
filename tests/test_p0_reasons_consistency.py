"""P0-3：_build_rps_reasons() 与真实筛选逻辑一致性验证。

覆盖用户列出的 12 个检查点的关键子集：
1. 指标值来自真实目标日期（不是 latest）
2. NULL 不转 0
3. 当前 RPS 模式正确匹配阈值
4. all 模式不套 dual 条件
5. line_red = 至少一档达标（不是三档全达标）
6. dual 分别检查 50 + 120
7. 突破形态名称与命中记录一致
8. passed=false 的可选指标不误导
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, RpsDaily, StockBasic, StockDaily
from app.services import pools
from app.services.pools import _build_rps_reasons

DAY = date(2026, 10, 23)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


# ---- 单元测试 _build_rps_reasons（纯函数，不需要 DB） ---------------------

def test_all_mode_lists_rps250_and_rps120_only():
    """all 模式：SQL 同时要求 RPS250 与 RPS120 非 NULL 且 ≥ min_rps，
    因此 reasons 必须同时列出这两档，且不得混入 dual 的 RPS50 条件。"""
    reasons = _build_rps_reasons(
        mode="all", breakout_only=False, trend_template=False,
        rps50=95, rps120=95, rps250=92, hits=[], min_rps=90,
    )
    keys = [r["key"] for r in reasons]
    assert keys == ["rps250", "rps120"]
    assert all(r["threshold"] == 90 for r in reasons)
    assert all(r["passed"] is True for r in reasons)
    # all 模式不使用 dual 的 RPS50 档
    assert "rps50" not in keys


def test_all_mode_rps120_below_threshold_is_not_passed():
    """all 模式 RPS250 达标但 RPS120 未达标 → 该项 passed=False（实际也不会入选）。"""
    reasons = _build_rps_reasons(
        mode="all", breakout_only=False, trend_template=False,
        rps50=99, rps120=70, rps250=95, hits=[], min_rps=90,
    )
    by_key = {r["key"]: r for r in reasons}
    assert by_key["rps250"]["passed"] is True
    assert by_key["rps120"]["passed"] is False
    assert by_key["rps120"]["value"] == 70


def test_all_mode_null_rps120_omitted_not_zero():
    """all 模式 RPS120 为 NULL → 不产生该项，绝不显示为 0。"""
    reasons = _build_rps_reasons(
        mode="all", breakout_only=False, trend_template=False,
        rps50=99, rps120=None, rps250=95, hits=[], min_rps=90,
    )
    keys = [r["key"] for r in reasons]
    assert keys == ["rps250"]


def test_dual_mode_lists_both_50_and_120():
    """dual 模式分别检查 RPS50(≥90) 和 RPS120(≥85)，分别列 passed 状态。"""
    from app.services.pools import RPS_DUAL_50, RPS_DUAL_120
    reasons = _build_rps_reasons(
        mode="dual", breakout_only=False, trend_template=False,
        rps50=92, rps120=83, rps250=None, hits=[], min_rps=80,
    )
    keys = {r["key"]: r for r in reasons}
    # RPS50 过阈值
    assert keys["rps50"]["passed"] is True
    assert keys["rps50"]["value"] == 92
    assert keys["rps50"]["threshold"] == RPS_DUAL_50
    # RPS120=83 < 85(RPS_DUAL_120) → 不过
    assert keys["rps120"]["passed"] is False
    assert keys["rps120"]["value"] == 83
    assert keys["rps120"]["threshold"] == RPS_DUAL_120


def test_line_red_lists_only_passing_windows():
    """line_red 只列 ≥90 的档位；不是三档全达标。"""
    reasons = _build_rps_reasons(
        mode="line_red", breakout_only=False, trend_template=False,
        rps50=95, rps120=50, rps250=92, hits=[], min_rps=80,
    )
    keys = [r["key"] for r in reasons]
    # 只应列 rps50 和 rps250 两档（≥90）
    assert keys == ["rps50", "rps250"]
    assert all(r["passed"] is True for r in reasons)
    # rps120=50 < 90 → 不应出现
    assert "rps120" not in keys


def test_line_red_all_three_pass():
    """line_red 三档全达标 → 全列出。"""
    reasons = _build_rps_reasons(
        mode="line_red", breakout_only=False, trend_template=False,
        rps50=91, rps120=92, rps250=93, hits=[], min_rps=80,
    )
    keys = [r["key"] for r in reasons]
    assert keys == ["rps50", "rps120", "rps250"]


def test_null_rps_not_converted_to_zero():
    """NULL 不转成有效的 0（不会出现在 reasons 里）。"""
    reasons = _build_rps_reasons(
        mode="dual", breakout_only=False, trend_template=False,
        rps50=None, rps120=92, rps250=None, hits=[], min_rps=80,
    )
    keys = [r["key"] for r in reasons]
    # rps50 和 rps250 是 None → 不出现
    assert keys == ["rps120"]


def test_rps250_null_in_all_mode_omits_only_that_item():
    """all 模式 rps250=None → 该档不产生 reason；RPS120 有值时仍正常列出。"""
    reasons = _build_rps_reasons(
        mode="all", breakout_only=False, trend_template=False,
        rps50=99, rps120=99, rps250=None, hits=[], min_rps=80,
    )
    keys = [r["key"] for r in reasons]
    assert keys == ["rps120"]


def test_breakout_only_empty_hits_not_false_reason():
    """breakout_only=True 但 hits=[] → 实际不会出现在最终 rows（被过滤掉），
    所以这个 reason 理论上不该到达前端。但纯函数层面要确保不出现误报。"""
    # 注意：breakout_only=True + empty hits 时，真实筛选流程里这票会被 continue 掉，
    # 不会进最终 rows。所以实际上前端不会看到 passed=False 的 breakout reason。
    # 但纯函数层面我们也要它语义正确。
    reasons = _build_rps_reasons(
        mode="all", breakout_only=True, trend_template=False,
        rps50=95, rps120=95, rps250=95, hits=[], min_rps=90,
    )
    breakout_reasons = [r for r in reasons if r["key"] == "breakout"]
    assert len(breakout_reasons) == 1
    assert breakout_reasons[0]["passed"] is False
    # 但真实筛选流程里这票会 breakout_only and not hits → continue → 不出现在 rows 里
    # 所以用户不会看到 passed=False 的 breakout reason。


def test_breakout_hit_names_match():
    """突破形态名称直接从 hits 列表取值 → 和真实命中记录一致。"""
    reasons = _build_rps_reasons(
        mode="all", breakout_only=True, trend_template=False,
        rps50=95, rps120=95, rps250=95,
        hits=["跳空缺口", "放量突破新高"],
        min_rps=90,
    )
    breakout_values = [r["value"] for r in reasons if r["key"] == "breakout"]
    assert breakout_values == ["跳空缺口", "放量突破新高"]


# ---- 端到端：确保 reasons 来自真实目标日期的 RPS --------------------------

def test_reasons_come_from_target_date(session):
    """同一天里故意让两档 RPS 值不同 → 确保 reasons 取的是当天值不是 latest。"""
    from datetime import timedelta

    old_day = DAY - timedelta(days=5)
    # 老日期的 RPS
    session.add(StockBasic(code="000001", name="票1"))
    session.add(StockDaily(trade_date=old_day, code="000001", close=10.0, pct_chg=1.0,
                           open=10, high=10.5, low=9.5, amount=1e8, volume=1e7))
    session.add(RpsDaily(trade_date=old_day, code="000001",
                         rps50=50, rps120=50, rps250=50,
                         ret50=5, ret120=5, ret250=5))
    # 今天的 RPS（高很多）
    session.add(StockDaily(trade_date=DAY, code="000001", close=11.0, pct_chg=10.0,
                           open=10, high=11.5, low=9.5, amount=2e8, volume=2e7))
    session.add(RpsDaily(trade_date=DAY, code="000001",
                         rps50=98, rps120=95, rps250=96,
                         ret50=20, ret120=30, ret250=40))
    session.commit()

    board = pools.rps_board(session, trade_date=DAY, mode="all", limit=50)
    assert len(board.rows) == 1
    row = board.rows[0]
    # reasons 里的值必须和 DAY 那行一致，不是 old_day 的 50
    r250_reason = [r for r in row.reasons if r["key"] == "rps250"]
    assert len(r250_reason) == 1
    assert r250_reason[0]["value"] == 96  # 不是 50


def test_all_mode_rps120_below_threshold_not_selected(session):
    """all 模式：RPS250 达标但 RPS120 未达 min_rps → 不得入选（SQL 与 reasons 口径一致）。"""
    session.add(StockBasic(code="000001", name="仅250达标"))
    session.add(StockDaily(trade_date=DAY, code="000001", close=10.0, pct_chg=1.0,
                           open=10, high=10.5, low=9.5, amount=1e8, volume=1e7))
    # rps250=95 达标，rps120=60 < 90 → SQL 应把它排除
    session.add(RpsDaily(trade_date=DAY, code="000001",
                         rps50=95, rps120=60, rps250=95,
                         ret50=10, ret120=10, ret250=10))
    session.commit()

    board = pools.rps_board(session, trade_date=DAY, mode="all", min_rps=90, limit=50)
    assert board.rows == []


def test_all_mode_both_passing_lists_both_reasons(session):
    """all 模式：两档都达标 → 入选，且 reasons 同时含 RPS250 与 RPS120（均为 passed）。"""
    session.add(StockBasic(code="000001", name="双档达标"))
    session.add(StockDaily(trade_date=DAY, code="000001", close=10.0, pct_chg=1.0,
                           open=10, high=10.5, low=9.5, amount=1e8, volume=1e7))
    session.add(RpsDaily(trade_date=DAY, code="000001",
                         rps50=70, rps120=93, rps250=96,
                         ret50=10, ret120=10, ret250=10))
    session.commit()

    board = pools.rps_board(session, trade_date=DAY, mode="all", min_rps=90, limit=50)
    assert len(board.rows) == 1
    by_key = {r["key"]: r for r in board.rows[0].reasons}
    assert by_key["rps250"]["value"] == 96
    assert by_key["rps120"]["value"] == 93
    assert by_key["rps250"]["passed"] is True
    assert by_key["rps120"]["passed"] is True
    # all 模式不使用 RPS50 档
    assert "rps50" not in by_key


def test_dual_mode_reasons_values_match_filters(session):
    """dual 模式：SQL where 要求 rps50≥90 AND rps120≥85 → 任何一档不过都不选中。"""
    session.add(StockBasic(code="000001", name="票1"))
    session.add(StockDaily(trade_date=DAY, code="000001", close=10.0, pct_chg=1.0,
                           open=10, high=10.5, low=9.5, amount=1e8, volume=1e7))
    # dual 阈值：rps50≥90 AND rps120≥85；这里 rps120=83 < 85
    session.add(RpsDaily(trade_date=DAY, code="000001",
                         rps50=92, rps120=83, rps250=99,
                         ret50=10, ret120=10, ret250=10))
    session.commit()

    board = pools.rps_board(session, trade_date=DAY, mode="dual", limit=50)
    # dual 模式 where 要求 rps50>=90 AND rps120>=85 → rps120=83 的票不该被选中
    assert len(board.rows) == 0  # 空
    # 这意味着 passed=False 的情况不会出现在最终 reasons 里（因为不会被 SQL 选中）


def test_line_red_only_one_window_passes(session):
    """line_red 模式：只有 RPS50 达标 → 入选；reasons 里只列 RPS50。"""
    session.add(StockBasic(code="000001", name="票1"))
    session.add(StockDaily(trade_date=DAY, code="000001", close=10.0, pct_chg=1.0,
                           open=10, high=10.5, low=9.5, amount=1e8, volume=1e7))
    session.add(RpsDaily(trade_date=DAY, code="000001",
                         rps50=95, rps120=80, rps250=75,
                         ret50=10, ret120=5, ret250=2))
    session.commit()

    board = pools.rps_board(session, trade_date=DAY, mode="line_red", limit=50)
    assert len(board.rows) == 1
    row = board.rows[0]
    keys = [r["key"] for r in row.reasons]
    # 只有 rps50 达标 → 只列 rps50
    assert keys == ["rps50"]
    assert row.reasons[0]["value"] == 95


def test_reasons_match_api_output_schema(session):
    """端到端：RpsRowOut schema 能正确接收 reasons，Selection 元数据贯通。"""
    session.add(StockBasic(code="000001", name="票1"))
    session.add(StockDaily(trade_date=DAY, code="000001", close=10.0, pct_chg=1.0,
                           open=10, high=10.5, low=9.5, amount=1e8, volume=1e7))
    session.add(RpsDaily(trade_date=DAY, code="000001",
                         rps50=95, rps120=95, rps250=95,
                         ret50=10, ret120=15, ret250=20))
    session.commit()

    from app.schemas import RpsRowOut, RpsOut
    from dataclasses import asdict

    board = pools.rps_board(session, trade_date=DAY, mode="all", limit=50)
    row_out = RpsRowOut(**asdict(board.rows[0]))
    # reasons 必须是 list[dict]
    assert isinstance(row_out.reasons, list)
    for r in row_out.reasons:
        assert "key" in r
        assert isinstance(r["key"], str)
    # RpsBoard 元数据也贯通
    out = RpsOut(
        trade_date=board.trade_date,
        rows=[row_out],
        industry_top=[],
        selection_scope=board.selection_scope,
        selection_cap=board.selection_cap,
        evaluated_count=board.evaluated_count,
    )
    assert out.selection_scope in ("full_market", "top_n")
    assert isinstance(out.selection_cap, int | type(None))
    assert isinstance(out.evaluated_count, int | type(None))
