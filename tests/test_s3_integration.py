"""S3-D：四套固定合成数据集的跨模块集成检查。

FULL / MISSING_MEMBERS / PARTIAL_OUTCOME / PIT_LEAKAGE，各自独立临时 SQLite；
不读取任何真实库、不发外部请求。
"""

from datetime import date, datetime

from sqlalchemy import create_engine
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
from app.services import sentiment_next_day as snd
from app.services import stock_research_context as ctx
from app.services import theme_intersection as ti

D1 = date(2026, 9, 23)
D2 = date(2026, 9, 24)
D3 = date(2026, 9, 25)


def _db():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _base(db):
    db.add_all([TradeCalendar(trade_date=day) for day in (D1, D2, D3)])
    for code, name in (("600000", "合成银行"), ("600001", "合成科技"), ("600002", "合成制造")):
        db.add(StockBasic(code=code, name=name, industry="测试行业"))


def _day(db, day, up, broken, daily=True):
    for code in up:
        db.add(LimitPool(trade_date=day, code=code, pool_type="up", name=code, consecutive=2))
    for code in broken:
        db.add(LimitPool(trade_date=day, code=code, pool_type="broken", name=code))
    # S5-A：`cutoff=CLOSE` 明确对应 15:00；日志必须早于该时刻才能算「当时已知」
    stamp = datetime(day.year, day.month, day.day, 14, 50)
    db.add(CollectLog(trade_date=day, task="pool_up", status="ok", rows=len(up), created_at=stamp))
    db.add(
        CollectLog(
            trade_date=day, task="pool_broken", status="ok", rows=len(broken), created_at=stamp
        )
    )
    db.add(CollectLog(trade_date=day, task="pool_down", status="ok", rows=0, created_at=stamp))
    if daily:
        for code in set(up) | set(broken):
            db.add(StockDaily(trade_date=day, code=code, open=10, close=10, pct_chg=1.0))


def test_full_dataset_works_across_modules():
    db = _db()
    try:
        _base(db)
        for day in (D1, D2, D3):
            _day(db, day, {"600000", "600001"}, {"600002"})
            db.add(
                MarketSentiment(
                    trade_date=day,
                    limit_up_count=50,
                    broken_rate=20.0,
                    max_consecutive=2,
                    up_count=3000,
                    down_count=2000,
                )
            )
        db.add_all(
            [
                SectorBasic(code="A01", name="题材一", taxonomy="tax_a"),
                SectorBasic(code="A02", name="题材二", taxonomy="tax_a"),
                SectorMember(trade_date=D2, sector_code="A01", code="600000", name="合成银行"),
                SectorMember(trade_date=D2, sector_code="A02", code="600000", name="合成银行"),
                StockConcept(trade_date=D2, code="600000", concept="题材一"),
            ]
        )
        db.commit()

        sentiment = snd.build_next_day(
            db, days=3, group=snd.GROUP_ALL_LIMIT, metric="limit_up_count", end_date=D3
        )
        assert sentiment["summary"]["valid_count"] > 0

        themes = ti.search(
            db, sector_codes=["A01", "A02"], mode="AND", market="all", as_of=D2, limit=10, offset=0
        )
        assert themes["total"] == 1
        assert themes["quality"]["status"] == ti.STATUS_PARTIAL
        assert themes["coverage"]["has_completion_proof"] is False

        context = ctx.build(db, code="600000", as_of=D2)
        assert context["as_known_on_day"]["limit_status"] == "UP"
        assert context["later_outcome"]["outcome_date"] == D3
    finally:
        db.close()


def test_missing_members_dataset_never_claims_complete():
    db = _db()
    try:
        _base(db)
        for index in range(1, 11):
            db.add(SectorBasic(code=f"A{index:02d}", name=f"A{index}", taxonomy="tax_a"))
        for index in range(1, 21):
            db.add(SectorBasic(code=f"B{index:02d}", name=f"B{index}", taxonomy="tax_b"))
        # tax_a 只有 1/10 有成员；tax_b 20/20
        db.add(SectorMember(trade_date=D2, sector_code="A01", code="600000", name="合成银行"))
        for index in range(1, 21):
            db.add(
                SectorMember(
                    trade_date=D2,
                    sector_code=f"B{index:02d}",
                    code=f"61{index:04d}",
                    name=f"合成{index}",
                )
            )
        db.commit()

        partial = ti.search(
            db, sector_codes=["A01"], mode="AND", market="all", as_of=D2, limit=5, offset=0
        )
        assert partial["coverage"]["observed_sector_count"] == 1
        assert partial["coverage"]["expected_sector_count"] == 10
        assert partial["coverage"]["has_completion_proof"] is False
        assert partial["quality"]["status"] == ti.STATUS_PARTIAL

        missing = ti.search(
            db, sector_codes=["A01", "A02"], mode="AND", market="all", as_of=D2, limit=5, offset=0
        )
        assert missing["quality"]["status"] == ti.STATUS_UNVERIFIABLE
        assert missing["items"] == []
    finally:
        db.close()


def test_partial_outcome_dataset_keeps_pending_and_partial():
    db = _db()
    try:
        _base(db)
        _day(db, D1, {"600000"}, set())
        _day(db, D2, {"600000"}, set(), daily=False)
        _day(db, D3, {"600001"}, set())
        db.commit()

        data = snd.build_next_day(
            db, days=2, group=snd.GROUP_ALL_LIMIT, metric="limit_up_count", end_date=D2
        )
        row = {item["sample_date"]: item for item in data["series"]}[D1]
        # D2 日线缺失但 D3 已有日线 → 样本缺失，不是 PENDING 0%
        assert row["valid_count"] == 0
        assert row["avg_pct"] is None
        assert row["data_quality_status"] == snd.STATUS_PARTIAL

        context = ctx.build(db, code="600000", as_of=D2)
        assert context["as_known_on_day"]["daily"] is None
        assert context["later_outcome"]["outcome_date"] == D3
    finally:
        db.close()


def test_pit_leakage_dataset_marks_theme_as_unknown_or_later():
    db = _db()
    try:
        _base(db)
        _day(db, D2, {"600000"}, set())
        _day(db, D3, {"600000"}, set())
        db.add_all(
            [
                SectorBasic(code="A01", name="题材一", taxonomy="tax_a"),
                SectorBasic(code="A02", name="题材二", taxonomy="tax_a"),
                SectorMember(
                    trade_date=D2,
                    sector_code="A01",
                    code="600000",
                    name="合成银行",
                    fetched_at=datetime(2026, 9, 25, 9, 0),
                ),
                SectorMember(
                    trade_date=D2,
                    sector_code="A02",
                    code="600000",
                    name="合成银行",
                    fetched_at=datetime(2026, 9, 22, 22, 0),
                ),
            ]
        )
        db.commit()

        themes = ti.search(
            db, sector_codes=["A01"], mode="AND", market="all", as_of=D2, limit=5, offset=0
        )
        assert themes["known_by_observation_time"] is False
        assert "THEME_PIT_UNKNOWN" in themes["warnings"]
        assert themes["historical_signal_eligible"] is False

        context = ctx.build(db, code="600000", as_of=D2)
        # S4-A：LATER 题材不得再出现在已知集合里
        known = {
            item["name"]: item["known_by"]
            for item in context["at_cutoff_known"]["themes"]
        }
        assert known == {"题材二": ctx.KNOWN_PRIOR_DAY}
        later = {
            item["name"]: item["known_by"]
            for item in context["unknown_or_later"]["themes"]
        }
        assert later == {"题材一": ctx.KNOWN_LATER}
        assert context["as_known_on_day"]["themes"] == context["at_cutoff_known"]["themes"]
    finally:
        db.close()
