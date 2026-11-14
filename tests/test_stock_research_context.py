"""S3-C → S4-A → S5-A：个股观察日研究依据（三桶互斥的时点证据 v3）。

覆盖硬要求：
- `AT_CUTOFF_KNOWN / UNKNOWN_OR_LATER / FORWARD_OUTCOME` 三桶互斥且并集守恒；
- `cutoff=CLOSE` 明确对应观察日 15:00 Asia/Shanghai；15:00 之后的日志进「后得」；
- `LATER / SAME_DAY_UNKNOWN / POST_CLOSE_UNKNOWN / UNKNOWN / END_OF_DAY_FACT`
  绝不进入已知桶；
- 池冲突标 `POOL_CONFLICT`，不按 if/elif 优先级择一；
- `NOT_IN_POOL` 必须三个池都可信且日志时点可证明；
- `PREMARKET_0925` 明确拒绝，不假装同口径。
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
    StockConcept,
    StockDaily,
    TradeCalendar,
)
from app.services import stock_research_context as ctx

D1 = date(2026, 11, 10)
D2 = date(2026, 11, 11)
D3 = date(2026, 11, 12)


def _log(day: date, task: str, rows: int, *, created: datetime | None = None) -> CollectLog:
    # 默认 14:50：早于 CLOSE cutoff（15:00），用于「当时已知」场景
    return CollectLog(
        trade_date=day,
        task=task,
        status="ok",
        rows=rows,
        created_at=created or datetime(day.year, day.month, day.day, 14, 50),
    )


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([TradeCalendar(trade_date=day) for day in (D1, D2, D3)])
        db.add(StockBasic(code="600000", name="合成银行", industry="银行"))
        db.add_all(
            [
                LimitPool(trade_date=D2, code="600000", pool_type="up", name="合成银行", consecutive=2),
                LimitPool(trade_date=D3, code="600000", pool_type="broken", name="合成银行"),
                # D3 涨停池另有一只票，保证池本身可信（不是 ok+0 异常）
                LimitPool(trade_date=D3, code="600001", pool_type="up", name="合成科技", consecutive=1),
                _log(D2, "pool_up", 1),
                _log(D2, "pool_broken", 0),
                _log(D2, "pool_down", 0),
                _log(D3, "pool_up", 1),
                _log(D3, "pool_broken", 1),
                _log(D3, "pool_down", 0),
                StockDaily(trade_date=D2, code="600000", open=9.5, close=10.0, pct_chg=10.0),
                StockDaily(trade_date=D3, code="600000", open=10.0, close=9.5, pct_chg=-5.0),
                MarketSentiment(
                    trade_date=D2,
                    limit_up_count=50,
                    broken_rate=20.0,
                    max_consecutive=2,
                    up_count=3000,
                    down_count=2000,
                ),
                SectorBasic(code="801001", name="芯片", taxonomy="kph_selected"),
                SectorMember(
                    trade_date=D2,
                    sector_code="801001",
                    code="600000",
                    name="合成银行",
                    fetched_at=datetime(2026, 11, 11, 22, 0),
                ),
                StockConcept(trade_date=D2, code="600000", concept="芯片"),
            ]
        )
        db.commit()
        yield db


def build(db, **kwargs):
    params = {"code": "600000", "as_of": D2}
    params.update(kwargs)
    return ctx.build(db, **params)


def _bucket(data, name):
    return [item for item in data["evidence"] if item["time_bucket"] == name]


def _set_theme_fetched_at(db, moment: datetime | None):
    db.query(SectorMember).filter(SectorMember.code == "600000").update(
        {"fetched_at": moment}
    )
    db.commit()


# ---------------------------------------------------------------- 三桶结构


def test_three_buckets_are_mutually_exclusive_and_exhaustive(session):
    data = build(session)
    assert data["evidence_version"] == "research-evidence-v3"
    assert data["pit_version"] == "research-evidence-v3"
    assert data["cutoff"] == "CLOSE"
    assert data["cutoff_at"] == "2026-11-11T15:00:00+08:00"
    assert data["timezone"] == "Asia/Shanghai"
    assert data["observation_date"] == D2

    counts = data["evidence_counts"]
    assert counts[ctx.BUCKET_KNOWN] == len(_bucket(data, ctx.BUCKET_KNOWN))
    assert counts[ctx.BUCKET_UNKNOWN] == len(_bucket(data, ctx.BUCKET_UNKNOWN))
    assert counts[ctx.BUCKET_FORWARD] == len(_bucket(data, ctx.BUCKET_FORWARD))
    assert sum(counts.values()) == len(data["evidence"])

    for item in data["evidence"]:
        assert item["time_bucket"] in ctx.TIME_BUCKETS
        assert item["is_forward_known"] is (item["time_bucket"] == ctx.BUCKET_FORWARD)
        assert item["known_by"] in ctx.BUCKET_ALLOWED_KNOWN_BY[item["time_bucket"]]
        # 上游发布时间拿不到时必须为 null，不能虚构
        assert item["source_published_at"] is None


def test_same_day_theme_is_not_known_at_cutoff(session):
    data = build(session)  # fetched_at 是 D2 22:00（同日收盘后）
    assert data["at_cutoff_known"]["themes"] == []
    unknown = data["unknown_or_later"]["themes"]
    assert len(unknown) == 1
    assert unknown[0]["known_by"] == ctx.KNOWN_SAME_DAY_UNKNOWN
    assert "THEME_PIT_UNKNOWN" in data["unknown_or_later"]["reasons"]
    # 兼容字段也不得再承载同日未知题材
    assert data["as_known_on_day"]["themes"] == []


def test_later_theme_never_appears_in_known(session):
    _set_theme_fetched_at(session, datetime(2026, 11, 12, 9, 0))
    data = build(session)
    assert data["at_cutoff_known"]["themes"] == []
    assert data["unknown_or_later"]["themes"][0]["known_by"] == ctx.KNOWN_LATER
    known_known_by = {
        item["known_by"] for item in _bucket(data, ctx.BUCKET_KNOWN)
    }
    assert ctx.KNOWN_LATER not in known_known_by
    assert "THEME_PIT_UNKNOWN" in data["quality"]["reasons"]


def test_prior_day_theme_is_known(session):
    _set_theme_fetched_at(session, datetime(2026, 11, 10, 22, 0))
    data = build(session)
    assert len(data["at_cutoff_known"]["themes"]) == 1
    assert data["at_cutoff_known"]["themes"][0]["known_by"] == ctx.KNOWN_PRIOR_DAY
    assert data["unknown_or_later"]["themes"] == []
    assert data["as_known_on_day"]["themes"][0]["known_by"] == ctx.KNOWN_PRIOR_DAY


def test_null_theme_time_is_unknown_not_known():
    # `sector_member.fetched_at` 是非空列，库里不会出现 null；
    # 这里直接验证归类函数：拿不到时间就必须是 UNKNOWN，不能猜成已知。
    assert ctx._theme_known_by(None, D2) == ctx.KNOWN_UNKNOWN
    assert ctx.classify_evidence_time(ctx._theme_known_by(None, D2)) == ctx.BUCKET_UNKNOWN


def test_concept_is_never_known_at_cutoff(session):
    data = build(session)
    assert data["at_cutoff_known"]["concepts"] == []
    assert data["as_known_on_day"]["concepts"] == []
    assert data["unknown_or_later"]["concepts"][0]["known_by"] == ctx.KNOWN_POST_CLOSE
    assert "CONCEPT_POST_CLOSE_UNKNOWN" in data["quality"]["reasons"]


def test_forward_evidence_is_only_in_forward_bucket(session):
    data = build(session)
    forward = _bucket(data, ctx.BUCKET_FORWARD)
    assert len(forward) == 1
    assert forward[0]["source_trade_date"] == D3
    assert forward[0]["known_by"] == ctx.KNOWN_FORWARD
    known_tables = {(item["source_table"], item["source_trade_date"]) for item in _bucket(
        data, ctx.BUCKET_KNOWN
    )}
    assert ("stock_daily", D3) not in known_tables
    # D+1 结果不会流回已知区域
    assert data["later_outcome"]["pct_chg"] == -5.0
    # stock_daily 没有首次入库时间：只进「日终事实」，不再冒充 15:00 已知
    assert data["at_cutoff_known"]["daily"] is None
    assert data["end_of_day_fact"]["daily"]["pct_chg"] == 10.0
    assert data["end_of_day_fact"]["known_by"] == ctx.KNOWN_END_OF_DAY_FACT


# ---------------------------------------------------------------- 池状态与冲突


def test_pool_conflict_is_explicit_not_priority_picked(session):
    session.add(
        LimitPool(trade_date=D2, code="600000", pool_type="broken", name="合成银行")
    )
    session.commit()
    data = build(session)
    assert data["at_cutoff_known"]["pool_status"] == ctx.POOL_STATUS_CONFLICT
    assert "POOL_CONFLICT" in data["quality"]["reasons"]
    pool_evidence = [
        item for item in data["evidence"] if item["claim"] == "观察日池记录"
    ]
    assert pool_evidence[0]["reason_code"] == "POOL_CONFLICT"
    assert "不能按优先级择一" in pool_evidence[0]["summary"]


def test_not_in_pool_requires_all_three_trusted_pools(session):
    session.add(StockBasic(code="600009", name="合成空仓", industry="银行"))
    session.add(StockDaily(trade_date=D2, code="600009", open=5, close=5, pct_chg=0))
    session.commit()
    data = build(session, code="600009")
    assert data["at_cutoff_known"]["pool_status"] == ctx.POOL_STATUS_NOT_IN_POOL

    # 缺跌停池日志 → 不能确认「不在任何池」
    session.query(CollectLog).filter(
        CollectLog.trade_date == D2, CollectLog.task == "pool_down"
    ).delete()
    session.commit()
    data = build(session, code="600009")
    assert data["at_cutoff_known"]["pool_status"] == ctx.POOL_STATUS_UNKNOWN


def test_pool_log_written_next_day_is_not_known_at_cutoff(session):
    session.query(CollectLog).filter(
        CollectLog.trade_date == D2, CollectLog.task == "pool_up"
    ).update({"created_at": datetime(2026, 11, 12, 8, 0)})
    session.commit()
    data = build(session)
    assert data["at_cutoff_known"]["pool_status"] == ctx.POOL_STATUS_UNKNOWN
    pool_evidence = [
        item for item in data["evidence"] if item["claim"] == "观察日池记录"
    ][0]
    assert pool_evidence["known_by"] == ctx.KNOWN_LATER
    assert pool_evidence["time_bucket"] == ctx.BUCKET_UNKNOWN
    assert pool_evidence["observed_at"] == datetime(2026, 11, 12, 8, 0)


def test_missing_pool_is_not_backfilled(session):
    session.query(CollectLog).filter(
        CollectLog.trade_date == D2, CollectLog.task == "pool_up"
    ).delete()
    session.commit()
    data = build(session)
    assert data["at_cutoff_known"]["pool_status"] == ctx.POOL_STATUS_UNKNOWN
    assert data["quality"]["status"] == ctx.STATUS_UNVERIFIABLE
    assert "POOL_LOG_MISSING_WITH_ROWS" in data["quality"]["reasons"]
    assert data["resolved_trade_date"] == D2


# ---------------------------------------------------------------- 时点协议边界


def test_premarket_cutoff_is_rejected(session):
    with pytest.raises(ValueError):
        build(session, cutoff="PREMARKET_0925")


def test_non_trade_day_is_rejected(session):
    with pytest.raises(ValueError):
        build(session, as_of=date(2026, 11, 13))


def test_invalid_code_is_rejected(session):
    with pytest.raises(ValueError):
        build(session, code="abc")
    with pytest.raises(ValueError):
        build(session, code="999999")


def test_pending_when_no_next_trade_day(session):
    data = build(session, as_of=D3)
    assert data["later_outcome"]["outcome_date"] is None
    assert data["later_outcome"]["status"] == ctx.STATUS_PENDING
    assert data["later_outcome"]["pct_chg"] is None
    assert len(_bucket(data, ctx.BUCKET_FORWARD)) == 0


def test_d1_quote_with_untrusted_pool_is_partial(session):
    session.query(CollectLog).filter(
        CollectLog.trade_date == D3, CollectLog.task == "pool_up"
    ).delete()
    session.commit()
    data = build(session)
    outcome = data["later_outcome"]
    assert outcome["pct_chg"] == -5.0
    assert outcome["status"] == ctx.STATUS_PARTIAL
    assert outcome["limit_up_again"] is None
    assert outcome["reason_code"] == "OUTCOME_LIMIT_POOL_UNVERIFIED"


def test_default_as_of_uses_latest_trade_day(session, monkeypatch):
    monkeypatch.setattr(ctx, "today_cn", lambda: D3)
    data = build(session, as_of=None)
    assert data["requested_as_of"] is None
    assert data["resolved_trade_date"] == D3


def test_theme_evidence_keeps_observed_at_and_no_publish_time(session):
    _set_theme_fetched_at(session, datetime(2026, 11, 12, 9, 0))
    data = build(session)
    theme_evidence = [
        item for item in data["evidence"] if item["source_table"] == "sector_member"
    ][0]
    assert theme_evidence["fetched_at"] == datetime(2026, 11, 12, 9, 0)
    assert theme_evidence["observed_at"] == datetime(2026, 11, 12, 9, 0)
    assert theme_evidence["source_published_at"] is None


def test_invariants_reject_illegal_bucket_mix():
    bad = [
        {
            "known_by": ctx.KNOWN_LATER,
            "is_forward_known": False,
            "time_bucket": ctx.BUCKET_KNOWN,
            "source_trade_date": D2,
        }
    ]
    with pytest.raises(RuntimeError):
        ctx.validate_time_bucket_invariants(bad, observation_date=D2)

    forward = [
        {
            "known_by": ctx.KNOWN_FORWARD,
            "is_forward_known": True,
            "time_bucket": ctx.BUCKET_FORWARD,
            "source_trade_date": D2,
        }
    ]
    with pytest.raises(RuntimeError):
        ctx.validate_time_bucket_invariants(forward, observation_date=D2)


def test_classify_evidence_time_is_total_and_strict():
    assert ctx.classify_evidence_time(ctx.KNOWN_CLOSE) == ctx.BUCKET_KNOWN
    assert ctx.classify_evidence_time(ctx.KNOWN_PRIOR_DAY) == ctx.BUCKET_KNOWN
    for value in (
        ctx.KNOWN_SAME_DAY_UNKNOWN,
        ctx.KNOWN_LATER,
        ctx.KNOWN_POST_CLOSE,
        ctx.KNOWN_UNKNOWN,
        ctx.KNOWN_END_OF_DAY_FACT,
    ):
        assert ctx.classify_evidence_time(value) == ctx.BUCKET_UNKNOWN
    assert ctx.classify_evidence_time(ctx.KNOWN_FORWARD) == ctx.BUCKET_FORWARD
    with pytest.raises(ValueError):
        ctx.classify_evidence_time("SOMETHING_ELSE")


# ---------------------------------------------------------------- 只读 / 无外呼


def test_service_is_read_only(session):
    before = (
        session.scalar(select(func.count()).select_from(LimitPool)),
        session.scalar(select(func.count()).select_from(StockDaily)),
        session.scalar(select(func.count()).select_from(CollectLog)),
        session.scalar(select(func.count()).select_from(SectorMember)),
    )
    build(session)
    after = (
        session.scalar(select(func.count()).select_from(LimitPool)),
        session.scalar(select(func.count()).select_from(StockDaily)),
        session.scalar(select(func.count()).select_from(CollectLog)),
        session.scalar(select(func.count()).select_from(SectorMember)),
    )
    assert before == after


def test_no_external_sync_is_called(session, monkeypatch):
    import app.api.stock as stock_api

    def boom(*_args, **_kwargs):
        raise AssertionError("research-context must not sync external data")

    monkeypatch.setattr(stock_api, "DailyCollector", boom)
    data = build(session)
    assert data["code"] == "600000"
