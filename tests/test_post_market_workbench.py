"""S5-C：盘后主线研究工作台（只读聚合）。

覆盖：交易日校验、AND/OR、市场过滤、跨 taxonomy、缺成员、分页守恒、
涨停/炸板冲突、D+1 未成熟不写 0、pct_chg 缺失掩码、池不可信、
按需加载个股依据、参数上限、只读无外呼。
"""

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import (
    Base,
    CollectLog,
    LimitPool,
    MarketSentiment,
    SectorBasic,
    SectorMember,
    StockBasic,
    StockDaily,
    TradeCalendar,
)
from app.services import post_market_workbench as wb

D1 = date(2026, 11, 3)
D2 = date(2026, 11, 4)
D3 = date(2026, 11, 5)


def _log(day, task, rows, status="ok"):
    return CollectLog(
        trade_date=day,
        task=task,
        status=status,
        rows=rows,
        created_at=datetime(day.year, day.month, day.day, 16, 0),
    )


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([TradeCalendar(trade_date=day) for day in (D1, D2, D3)])
        db.add_all(
            [
                SectorBasic(code="A01", name="题材一", taxonomy="tax_a"),
                SectorBasic(code="A02", name="题材二", taxonomy="tax_a"),
                SectorBasic(code="B01", name="题材B", taxonomy="tax_b"),
                SectorBasic(code="A03", name="无快照", taxonomy="tax_a"),
            ]
        )
        for code in ("600000", "600001", "600002", "600003", "600004", "000001"):
            db.add(StockBasic(code=code, name=code, industry="测试"))
            db.add(
                SectorMember(
                    trade_date=D2,
                    sector_code="A01",
                    code=code,
                    name=code,
                    fetched_at=datetime(2026, 11, 3, 22, 0),
                )
            )
        for code in ("600000", "600001"):
            db.add(
                SectorMember(
                    trade_date=D2,
                    sector_code="A02",
                    code=code,
                    name=code,
                    fetched_at=datetime(2026, 11, 3, 22, 0),
                )
            )
        db.add(
            SectorMember(
                trade_date=D2,
                sector_code="B01",
                code="600005",
                name="600005",
                fetched_at=datetime(2026, 11, 3, 22, 0),
            )
        )
        db.add_all(
            [
                LimitPool(trade_date=D1, code="600000", pool_type="up", name="600000", consecutive=1),
                LimitPool(trade_date=D1, code="600001", pool_type="up", name="600001", consecutive=1),
                _log(D1, "pool_up", 2),
                _log(D1, "pool_broken", 0),
                _log(D1, "pool_down", 0),
                LimitPool(trade_date=D2, code="600000", pool_type="up", name="600000", consecutive=1),
                LimitPool(trade_date=D2, code="600001", pool_type="up", name="600001", consecutive=2),
                LimitPool(trade_date=D2, code="600002", pool_type="up", name="600002", consecutive=3),
                LimitPool(trade_date=D2, code="600003", pool_type="broken", name="600003"),
                LimitPool(trade_date=D2, code="600004", pool_type="up", name="600004", consecutive=1),
                LimitPool(trade_date=D2, code="600004", pool_type="broken", name="600004"),
                LimitPool(trade_date=D2, code="000001", pool_type="up", name="000001", consecutive=1),
                _log(D2, "pool_up", 5),
                _log(D2, "pool_broken", 2),
                _log(D2, "pool_down", 0),
                _log(D3, "pool_up", 1),
                _log(D3, "pool_broken", 0),
                _log(D3, "pool_down", 0),
                LimitPool(trade_date=D3, code="600000", pool_type="up", name="600000", consecutive=2),
                MarketSentiment(
                    trade_date=D2,
                    limit_up_count=5,
                    broken_rate=33.3,
                    max_consecutive=3,
                    up_count=3000,
                    down_count=2000,
                ),
            ]
        )
        for code, pct in (
            ("600000", 5.0),
            ("600001", -2.0),
            ("600002", 0.0),
            ("600003", 1.0),
            ("600004", 3.0),
            ("000001", 4.0),
        ):
            db.add(StockDaily(trade_date=D3, code=code, open=10, close=10, pct_chg=pct))
        db.commit()
        yield db


def build(db, **kwargs):
    params = {"as_of": D2, "sector_codes": ["A01", "A02"], "mode": "AND", "limit": 50}
    params.update(kwargs)
    return wb.build(db, **params)


# ---------------------------------------------------------------- 基本聚合


def test_workbench_aggregates_all_sections(session):
    data = build(session)
    assert data["as_of"] == D2
    assert data["quality_version"] == "workbench-quality-v1"
    assert data["scope"] == {
        "sector_codes": ["A01", "A02"],
        "mode": "AND",
        "market": "all",
        "view": "overview",
    }
    assert data["sentiment"]["as_of"] == D2
    assert data["sentiment"]["summary"]["limit_up_count"] == 5
    assert data["promotion"]["prev_date"] == D1
    assert data["promotion"]["levels"]
    assert data["resonance"]["status"] in ("VERIFIED", "PARTIAL", "UNVERIFIABLE")
    assert data["outcome"]["status"] in ("PENDING", "PARTIAL", "VERIFIED", "UNVERIFIABLE")
    assert data["pagination"]["total"] == 2
    assert {row["code"] for row in data["rows"]} == {"600000", "600001"}
    assert data["stock_research"] is None


def test_default_as_of_uses_latest_trade_day(session, monkeypatch):
    monkeypatch.setattr(wb, "today_cn", lambda: D3)
    data = build(session, as_of=None)
    assert data["as_of"] == D3


def test_non_trade_day_is_rejected(session):
    with pytest.raises(ValueError):
        build(session, as_of=date(2026, 11, 6))


def test_invalid_parameters_are_rejected(session):
    with pytest.raises(ValueError):
        build(session, mode="XOR")
    with pytest.raises(ValueError):
        build(session, market="hk")
    with pytest.raises(ValueError):
        build(session, view="unknown")
    with pytest.raises(ValueError):
        build(session, limit=0)
    with pytest.raises(ValueError):
        build(session, limit=101)
    with pytest.raises(ValueError):
        build(session, offset=-1)
    with pytest.raises(ValueError):
        build(session, sector_codes=[f"S{i}" for i in range(11)])


def test_empty_sectors_keeps_other_sections(session):
    data = build(session, sector_codes=[])
    assert data["resonance"]["status"] == "NOT_SELECTED"
    assert data["outcome"]["status"] == "NOT_SELECTED"
    assert data["pagination"] == {"total": 0, "shown": 0, "has_more": False}
    assert data["rows"] == []
    assert data["sentiment"]["summary"]["limit_up_count"] == 5
    assert "NO_SECTORS_SELECTED" in data["warnings"]


# ---------------------------------------------------------------- 交集与覆盖


def test_and_or_dedupe(session):
    assert build(session, mode="AND")["pagination"]["total"] == 2
    or_data = build(session, mode="OR")
    assert or_data["pagination"]["total"] == 6  # A01 6 只，A02 为子集
    assert or_data["resonance"]["coverage"]["member_unique_count"] == 6


def test_missing_sector_makes_and_unverifiable(session):
    data = build(session, sector_codes=["A01", "A03"], mode="AND")
    assert data["resonance"]["status"] == "UNVERIFIABLE"
    assert data["pagination"]["total"] == 0
    assert "AND_UNVERIFIABLE" in data["resonance"]["reasons"]


def test_cross_taxonomy_records_warning(session):
    data = build(session, sector_codes=["A01", "B01"], mode="OR")
    assert "TAXONOMY_MIXED" in data["resonance"]["warnings"]
    assert data["pagination"]["total"] == 7


def test_observed_sample_warning_without_completion_proof(session):
    data = build(session)
    assert data["resonance"]["coverage"]["member_completion_proof"] is False
    assert "OBSERVED_SAMPLE_ONLY" in data["warnings"]


def test_market_filter_reduces_rows(session):
    data = build(session, mode="OR", market="sh")
    assert data["pagination"]["total"] == 5
    assert all(row["code"].startswith("60") for row in data["rows"])
    sz = build(session, mode="OR", market="sz")
    assert {row["code"] for row in sz["rows"]} == {"000001"}


# ---------------------------------------------------------------- 分页与守恒


def test_pagination_keeps_total_and_sample_count(session):
    first = build(session, limit=1, offset=0)
    second = build(session, limit=1, offset=1)
    assert first["pagination"]["total"] == second["pagination"]["total"] == 2
    assert first["pagination"]["shown"] == second["pagination"]["shown"] == 1
    assert first["pagination"]["has_more"] is True
    assert second["pagination"]["has_more"] is False
    assert first["outcome"]["stats"]["sample_count"] == 2
    assert second["outcome"]["stats"]["sample_count"] == 2
    assert {row["code"] for row in first["rows"]}.isdisjoint(
        {row["code"] for row in second["rows"]}
    )


def test_pool_conflict_is_not_double_counted(session):
    data = build(session, mode="OR")
    counts = data["resonance"]["state_counts"]
    assert counts["conflict"] == 1
    assert "POOL_CONFLICT" in data["resonance"]["warnings"]
    conflict = next(row for row in data["rows"] if row["code"] == "600004")
    assert conflict["state"] == "conflict"


# ---------------------------------------------------------------- D+1 结果


def test_pending_outcome_never_writes_zero(session):
    data = build(session, as_of=D3, sector_codes=["A01"], mode="AND")
    assert data["outcome"]["status"] == "PENDING"
    stats = data["outcome"]["stats"]
    assert stats["avg_pct"] is None
    assert stats["median_pct"] is None
    assert stats["up_ratio"] is None
    assert stats["limit_up_again_ratio"] is None
    assert "OUTCOME_PENDING" in data["warnings"]


def test_missing_pct_is_not_a_valid_sample(session):
    session.query(StockDaily).filter(
        StockDaily.trade_date == D3, StockDaily.code == "600000"
    ).update({"pct_chg": None})
    session.commit()
    data = build(session)
    assert data["outcome"]["status"] == "PARTIAL"
    assert data["outcome"]["stats"]["valid_count"] == 1
    assert data["outcome"]["stats"]["missing_count"] == 1
    row = next(item for item in data["rows"] if item["code"] == "600000")
    assert row["outcome_pct_chg"] is None
    # 即使该票在 D+1 涨停池里，也不能给布尔值
    assert row["limit_up_again"] is None


def test_untrusted_next_pool_nulls_again_ratio(session):
    session.query(CollectLog).filter(
        CollectLog.trade_date == D3, CollectLog.task == "pool_up"
    ).delete()
    session.commit()
    data = build(session)
    assert data["outcome"]["status"] == "PARTIAL"
    assert data["outcome"]["stats"]["limit_up_again_ratio"] is None
    assert all(row["limit_up_again"] is None for row in data["rows"])


# ---------------------------------------------------------------- 个股按需


def test_stock_research_is_loaded_only_on_demand(session):
    data = build(session)
    assert data["stock_research"] is None
    with_stock = build(session, stock="600000")
    assert with_stock["stock_research"]["code"] == "600000"
    assert with_stock["stock_research"]["pit_version"] == "research-evidence-v3"


def test_invalid_stock_is_rejected(session):
    with pytest.raises(ValueError):
        build(session, stock="abc")
    with pytest.raises(ValueError):
        build(session, stock="999999")


# ---------------------------------------------------------------- 质量与只读


def test_quality_block_lists_all_sources(session):
    data = build(session)
    sources = {item["source"]: item for item in data["quality"]["sources"]}
    assert set(sources) == {
        "stock_daily",
        "limit_pool",
        "sector_member",
        "market_sentiment",
        "auction_snapshot",
        "trade_calendar",
    }
    assert sources["auction_snapshot"]["status"] == "NOT_COLLECTED"
    assert data["quality"]["version"] == "data-quality-v2"
    assert data["quality"]["warnings"]
    # 每个来源同时给单来源状态与组合视图状态
    for item in data["quality"]["sources"]:
        assert item["source_status"] == item["status"]
        assert "combined_view_status" in item
    assert "data_as_of" in data["quality"]["data_as_of_note"]


def test_combined_view_warning_reaches_top_level(session):
    """C3：单来源 VERIFIED 但组合视图 PARTIAL 时，顶层 warnings 必须带原因。"""
    session.add(
        CollectLog(
            trade_date=D2,
            task="pool_broken",
            status="failed",
            rows=0,
            created_at=datetime(2026, 11, 4, 20, 0),
        )
    )
    session.commit()
    data = build(session)
    limit = next(
        item for item in data["quality"]["sources"] if item["source"] == "limit_pool"
    )
    assert limit["source_status"] == "VERIFIED_SOURCE"
    assert limit["combined_view_status"] == "PARTIAL"
    assert "limit_pool:COMBINED_VIEW_PARTIAL" in data["quality"]["warnings"]
    assert "limit_pool:COMBINED_VIEW_PARTIAL" in data["warnings"]


def test_promotion_levels_cover_requested_bands(session):
    data = build(session)
    levels = {row["level"] for row in data["promotion"]["levels"]}
    assert levels == {1, 2, 3, 4}
    assert all("verified_rate" in row and "legacy_rate" in row for row in data["promotion"]["levels"])


def test_workbench_is_read_only(session):
    before = (
        session.scalar(select(func.count()).select_from(LimitPool)),
        session.scalar(select(func.count()).select_from(StockDaily)),
        session.scalar(select(func.count()).select_from(CollectLog)),
        session.scalar(select(func.count()).select_from(SectorMember)),
    )
    build(session, stock="600000")
    after = (
        session.scalar(select(func.count()).select_from(LimitPool)),
        session.scalar(select(func.count()).select_from(StockDaily)),
        session.scalar(select(func.count()).select_from(CollectLog)),
        session.scalar(select(func.count()).select_from(SectorMember)),
    )
    assert before == after


def test_workbench_never_calls_external_sources(session, monkeypatch):
    import app.api.stock as stock_api
    import app.sources.kaipanhong as kph
    import app.sources.tencent as tencent

    def boom(*_args, **_kwargs):
        raise AssertionError("workbench must be read-only")

    monkeypatch.setattr(tencent, "fetch_spot", boom)
    monkeypatch.setattr(kph.KaipanhongSource, "_post", boom)
    monkeypatch.setattr(stock_api, "DailyCollector", boom)
    data = build(session)
    assert data["rows"]
