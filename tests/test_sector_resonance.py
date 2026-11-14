"""S4-C：板块主线共振（只读研究）。

覆盖：AND/OR 去重、连板分桶、冲突不双计、D+1 pooled 统计、PENDING 不写 0%、
快照只向过去找、池不可信时不下结论、分页稳定、只读无外呼。
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
    StockDaily,
    TradeCalendar,
)
from app.services import sector_resonance as sr

D1 = date(2026, 11, 3)
D2 = date(2026, 11, 4)
D3 = date(2026, 11, 5)


def _log(day: date, task: str, rows: int) -> CollectLog:
    return CollectLog(
        trade_date=day, task=task, status="ok", rows=rows,
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
            ]
        )
        for code in ("600000", "600001", "600002", "600003", "600004"):
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
        db.add_all(
            [
                LimitPool(trade_date=D2, code="600000", pool_type="up", name="600000", consecutive=1, first_seal_time="09:35"),
                LimitPool(trade_date=D2, code="600001", pool_type="up", name="600001", consecutive=2),
                LimitPool(trade_date=D2, code="600002", pool_type="up", name="600002", consecutive=3),
                LimitPool(trade_date=D2, code="600003", pool_type="broken", name="600003"),
                LimitPool(trade_date=D2, code="600004", pool_type="up", name="600004", consecutive=1),
                LimitPool(trade_date=D2, code="600004", pool_type="broken", name="600004"),
                _log(D2, "pool_up", 4),
                _log(D2, "pool_broken", 2),
                _log(D2, "pool_down", 0),
                _log(D3, "pool_up", 1),
                _log(D3, "pool_broken", 0),
                _log(D3, "pool_down", 0),
                LimitPool(trade_date=D3, code="600000", pool_type="up", name="600000", consecutive=2),
                MarketSentiment(
                    trade_date=D2,
                    limit_up_count=4,
                    broken_rate=33.3,
                    max_consecutive=3,
                ),
            ]
        )
        for code, pct in (
            ("600000", 5.0),
            ("600001", -2.0),
            ("600002", 0.0),
            ("600003", 1.0),
            ("600004", 3.0),
        ):
            db.add(
                StockDaily(trade_date=D3, code=code, open=10, close=10, pct_chg=pct)
            )
        db.commit()
        yield db


def build(db, **kwargs):
    params = {"as_of": D2, "sector_codes": ["A01", "A02"], "mode": "AND"}
    params.update(kwargs)
    return sr.build(db, **params)


def test_and_or_dedupe_and_per_sector_contribution(session):
    and_data = build(session)
    assert and_data["total"] == 2
    assert {row["code"] for row in and_data["rows"]} == {"600000", "600001"}
    assert and_data["member_snapshot_date"] == D2
    assert and_data["coverage"]["member_unique_count"] == 5
    assert [item["member_count"] for item in and_data["selected_sectors"]] == [5, 2]

    or_data = build(session, mode="OR")
    assert or_data["total"] == 5  # 5 只去重，不是 5+3=8
    assert or_data["coverage"]["observed_sectors"] == 2
    assert or_data["coverage"]["expected_sectors"] == 2
    assert or_data["coverage"]["member_completion_proof"] is False


def test_state_buckets_and_conflict_not_double_counted(session):
    data = build(session, mode="OR")
    counts = data["state_counts"]
    assert counts[sr.STATE_FIRST] == 1  # 600000
    assert counts[sr.STATE_SECOND] == 1  # 600001
    assert counts[sr.STATE_THIRD_PLUS] == 1  # 600002
    assert counts[sr.STATE_BROKEN] == 1  # 600003
    assert counts[sr.STATE_CONFLICT] == 1  # 600004
    # 冲突股不双计：涨停档只数 3 只
    assert data["coverage"]["observed_limit_up_count"] == 3
    assert "POOL_CONFLICT" in data["warnings"]
    conflict_row = next(row for row in data["rows"] if row["code"] == "600004")
    assert conflict_row["state"] == sr.STATE_CONFLICT


def test_d1_pooled_stats_and_again_ratio(session):
    data = build(session)
    outcome = data["outcome"]
    assert data["outcome_date"] == D3
    assert outcome["sample_count"] == 2
    assert outcome["valid_count"] == 2
    assert outcome["missing_count"] == 0
    assert outcome["avg_pct"] == 1.5
    assert outcome["median_pct"] == 1.5
    assert outcome["outcome_up_count"] == 1
    assert outcome["outcome_down_count"] == 1
    assert outcome["up_ratio"] == 50.0
    assert outcome["limit_up_again_count"] == 1
    assert outcome["limit_up_again_ratio"] == 50.0
    assert data["quality"]["status"] == sr.STATUS_PARTIAL
    assert data["aggregation_version"] == "next-day-aggregation-v2"


def test_pending_outcome_does_not_write_zero(session):
    data = build(session, as_of=D3, sector_codes=["A01"], mode="AND")
    assert data["outcome_date"] is None
    assert data["outcome_status"] == "PENDING"
    assert data["outcome"]["avg_pct"] is None
    assert data["outcome"]["up_ratio"] is None


def test_invalid_pct_chg_is_not_a_valid_outcome_sample(session):
    # P1-04：行情行存在 ≠ 有效样本；列表/汇总/分桶必须用同一掩码
    session.query(StockDaily).filter(
        StockDaily.trade_date == D3, StockDaily.code == "600000"
    ).update({"pct_chg": None})
    session.commit()
    data = build(session, sector_codes=["A01"], mode="AND")
    assert data["outcome_status"] == sr.STATUS_PARTIAL
    assert data["outcome"]["valid_count"] == 4
    assert data["outcome"]["missing_count"] == 1
    row = next(item for item in data["rows"] if item["code"] == "600000")
    assert row["outcome_pct_chg"] is None
    # 该票在 D+1 涨停池里，但涨跌幅不可用 → 不给「是否再涨停」布尔值
    assert row["limit_up_again"] is None


def test_missing_sector_makes_and_unverifiable(session):
    session.add(SectorBasic(code="A03", name="题材三", taxonomy="tax_a"))
    session.commit()
    and_data = build(session, sector_codes=["A01", "A03"], mode="AND")
    assert and_data["quality"]["status"] == sr.STATUS_UNVERIFIABLE
    assert and_data["total"] == 0
    assert and_data["rows"] == []
    assert "AND_UNVERIFIABLE" in and_data["warnings"]

    or_data = build(session, sector_codes=["A01", "A03"], mode="OR")
    assert or_data["total"] == 5
    assert or_data["quality"]["status"] == sr.STATUS_PARTIAL


def test_untrusted_pool_does_not_claim_states(session):
    session.query(CollectLog).filter(
        CollectLog.trade_date == D2, CollectLog.task == "pool_up"
    ).delete()
    session.commit()
    data = build(session)
    assert data["coverage"]["observed_limit_up_count"] is None
    assert data["state_counts"][sr.STATE_FIRST] == 0
    assert data["state_counts"][sr.STATE_UNKNOWN] >= 1
    assert data["quality"]["status"] == sr.STATUS_UNVERIFIABLE
    assert "POOL_UP_UNVERIFIED" in data["warnings"]


def test_conflict_requires_both_sources_trusted(session):
    """P2-04：up 来源最新失败、旧行仍在 → 不得判 conflict。"""
    session.add(
        CollectLog(
            trade_date=D2,
            task="pool_up",
            status="failed",
            rows=0,
            created_at=datetime(2026, 11, 4, 20, 0),
        )
    )
    session.commit()
    data = build(session, mode="OR")
    row = next(item for item in data["rows"] if item["code"] == "600004")
    assert row["state"] == sr.STATE_UNKNOWN
    assert data["state_counts"][sr.STATE_CONFLICT] == 0
    assert "POOL_CONFLICT" not in data["warnings"]
    assert "POOL_UP_UNVERIFIED" in data["warnings"]
    # 分页不改变总统计
    paged = build(session, mode="OR", limit=2, offset=0)
    assert paged["state_counts"] == data["state_counts"]


def test_single_up_member_with_untrusted_broken_is_unknown(session):
    """R2-P2-01：broken 不可信时，不得把单边 up 成员标成 FIRST/SECOND。"""
    session.query(LimitPool).filter(
        LimitPool.trade_date == D2, LimitPool.pool_type == "broken"
    ).delete()
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
    data = build(session, mode="OR")
    row = next(item for item in data["rows"] if item["code"] == "600000")
    assert row["state"] == sr.STATE_UNKNOWN
    assert data["state_counts"][sr.STATE_FIRST] == 0
    assert data["state_counts"][sr.STATE_SECOND] == 0
    assert "POOL_BROKEN_UNVERIFIED" in data["warnings"]
    # 分页不改变桶计数
    paged = build(session, mode="OR", limit=2, offset=0)
    assert paged["state_counts"] == data["state_counts"]


def test_single_broken_member_with_untrusted_up_is_unknown(session):
    """up 不可信时，不得把单边 broken 成员标成 BROKEN。"""
    session.query(LimitPool).filter(
        LimitPool.trade_date == D2, LimitPool.pool_type == "up"
    ).delete()
    session.add(
        CollectLog(
            trade_date=D2,
            task="pool_up",
            status="failed",
            rows=0,
            created_at=datetime(2026, 11, 4, 20, 0),
        )
    )
    session.commit()
    data = build(session, mode="OR")
    row = next(item for item in data["rows"] if item["code"] == "600003")
    assert row["state"] == sr.STATE_UNKNOWN
    assert data["state_counts"][sr.STATE_BROKEN] == 0
    assert "POOL_UP_UNVERIFIED" in data["warnings"]


def test_both_sources_trusted_classifies_single_side_members(session):
    data = build(session, mode="OR")
    by_code = {row["code"]: row["state"] for row in data["rows"]}
    assert by_code["600000"] == sr.STATE_FIRST
    assert by_code["600001"] == sr.STATE_SECOND
    assert by_code["600002"] == sr.STATE_THIRD_PLUS
    assert by_code["600003"] == sr.STATE_BROKEN
    assert by_code["600004"] == sr.STATE_CONFLICT


def test_snapshot_never_rolls_forward(session):
    # 给 D3 加一份完全不同的成员快照；as_of=D2 时必须用 D2 的快照
    session.add_all(
        [
            SectorMember(
                trade_date=D3,
                sector_code="A01",
                code="600099",
                name="新成员",
                fetched_at=datetime(2026, 11, 5, 22, 0),
            )
        ]
    )
    session.commit()
    data = build(session, sector_codes=["A01"], mode="OR")
    assert data["member_snapshot_date"] == D2
    assert "600099" not in {row["code"] for row in data["rows"]}


def test_pagination_is_stable(session):
    first = build(session, mode="OR", limit=2, offset=0)
    second = build(session, mode="OR", limit=2, offset=2)
    assert first["total"] == second["total"] == 5
    assert first["shown"] == 2 and second["shown"] == 2
    assert first["has_more"] is True and second["has_more"] is True
    codes = {row["code"] for row in first["rows"]} | {row["code"] for row in second["rows"]}
    assert len(codes) == 4
    # 桶计数不随分页变化
    assert first["state_counts"] == second["state_counts"]


def test_pit_flags_prior_same_day_and_later(session):
    prior = build(session)
    assert prior["pit"]["member_snapshot_known_by"] == "PRIOR_DAY"
    assert prior["pit"]["known_by_observation_time"] is True
    assert prior["pit"]["historical_signal_eligible"] is False

    session.query(SectorMember).filter(SectorMember.trade_date == D2).update(
        {"fetched_at": datetime(2026, 11, 4, 9, 0)}
    )
    session.commit()
    same_day = build(session)
    assert same_day["pit"]["member_snapshot_known_by"] == "SAME_DAY_UNKNOWN"
    assert same_day["pit"]["known_by_observation_time"] is None
    assert same_day["pit"]["later_snapshot_for_review"] is False

    session.query(SectorMember).filter(SectorMember.trade_date == D2).update(
        {"fetched_at": datetime(2026, 11, 5, 9, 0)}
    )
    session.commit()
    later = build(session)
    assert later["pit"]["member_snapshot_known_by"] == "LATER"
    assert later["pit"]["known_by_observation_time"] is False
    assert later["pit"]["later_snapshot_for_review"] is True


def test_no_snapshot_is_not_collected(session):
    data = build(session, as_of=D1, sector_codes=["A01"], mode="AND")
    assert data["member_snapshot_date"] is None
    assert data["quality"]["status"] == sr.STATUS_NOT_COLLECTED
    assert "NO_MEMBERS_SNAPSHOT" in data["quality"]["reasons"]


def test_d1_missing_quotes_is_partial_not_zero(session):
    session.query(StockDaily).filter(StockDaily.code == "600001").delete()
    session.commit()
    data = build(session)
    assert data["outcome_status"] == sr.STATUS_PARTIAL
    assert data["outcome"]["valid_count"] == 1
    assert data["outcome"]["missing_count"] == 1
    assert "OUTCOME_PARTIAL" in data["outcome_reason_codes"]


def test_d1_untrusted_pool_makes_again_unverifiable(session):
    session.query(CollectLog).filter(
        CollectLog.trade_date == D3, CollectLog.task == "pool_up"
    ).delete()
    session.commit()
    data = build(session)
    assert data["outcome"]["limit_up_again_count"] is None
    assert data["outcome"]["limit_up_again_ratio"] is None
    assert "OUTCOME_LIMIT_POOL_UNVERIFIED" in data["outcome_reason_codes"]


def test_invalid_arguments(session):
    with pytest.raises(ValueError):
        build(session, as_of=date(2026, 11, 6))  # 非交易日
    with pytest.raises(ValueError):
        build(session, sector_codes=["A01", "NOPE"], mode="AND")
    with pytest.raises(ValueError):
        build(session, mode="XOR")
    with pytest.raises(ValueError):
        build(session, limit=0)


def test_service_is_read_only(session):
    before = (
        session.scalar(select(func.count()).select_from(LimitPool)),
        session.scalar(select(func.count()).select_from(SectorMember)),
        session.scalar(select(func.count()).select_from(StockDaily)),
    )
    build(session, mode="OR")
    after = (
        session.scalar(select(func.count()).select_from(LimitPool)),
        session.scalar(select(func.count()).select_from(SectorMember)),
        session.scalar(select(func.count()).select_from(StockDaily)),
    )
    assert before == after


def test_no_external_source_is_touched(session, monkeypatch):
    import app.sources.kaipanhong as kph
    import app.sources.tencent as tencent

    def boom(*_args, **_kwargs):
        raise AssertionError("sector resonance must not call external sources")

    monkeypatch.setattr(tencent, "fetch_spot", boom)
    monkeypatch.setattr(kph.KaipanhongSource, "_post", boom)
    data = build(session)
    assert data["rows"]
