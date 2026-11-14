"""S5-A：PIT v3 精确截止时刻（CLOSE = 15:00 Asia/Shanghai）的边界测试。

纯函数 `evaluate_known_time` 覆盖 14:59 / 15:00 / 15:01 / 15:05 / 22:00 / D+1 09:00、
UTC 与 +08:00、naive 时区约定、缺时间、来源未就绪、上游发布时间晚于 cutoff 等；
集成部分验证服务层不再把 15:00 之后的采集日志算作「当时已知」，
日终事实不冒充 15:00 已知，D+1 后验不反写。
"""

from datetime import date, datetime, timedelta, timezone

import pytest
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
from app.services import stock_research_context as ctx

D1 = date(2026, 11, 10)
D2 = date(2026, 11, 11)
D3 = date(2026, 11, 12)
CUTOFF = datetime(2026, 11, 11, 15, 0, tzinfo=timezone(timedelta(hours=8)))
UTC = timezone.utc
CST = timezone(timedelta(hours=8))


def evaluate(moment, **kwargs):
    # 纯函数层：显式声明信任本项目的 CST naive 约定；服务层另有用例覆盖不信任路径
    kwargs.setdefault("naive_timezone_trusted", True)
    return ctx.evaluate_known_time(observed_at=moment, cutoff_at=CUTOFF, **kwargs)


# ---------------------------------------------------------------- 纯函数时间边界


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 11, 11, 14, 59), ctx.KNOWN_CLOSE),
        (datetime(2026, 11, 11, 9, 25), ctx.KNOWN_CLOSE),
        (datetime(2026, 11, 11, 0, 0), ctx.KNOWN_CLOSE),
    ],
)
def test_before_cutoff_is_known(moment, expected):
    result = evaluate(moment)
    assert result["known_by"] == expected
    assert result["time_bucket"] == ctx.BUCKET_KNOWN
    assert result["reason_code"] is None


def test_exactly_cutoff_is_known_inclusive():
    result = evaluate(datetime(2026, 11, 11, 15, 0))
    assert result["known_by"] == ctx.KNOWN_CLOSE
    assert result["time_bucket"] == ctx.BUCKET_KNOWN


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 11, 11, 15, 1),
        datetime(2026, 11, 11, 15, 5),
        datetime(2026, 11, 11, 22, 0),
        datetime(2026, 11, 11, 23, 59, 59),
    ],
)
def test_after_cutoff_same_day_is_post_close_unknown(moment):
    result = evaluate(moment)
    assert result["known_by"] == ctx.KNOWN_POST_CLOSE
    assert result["time_bucket"] == ctx.BUCKET_UNKNOWN
    assert result["reason_code"] == "AFTER_CUTOFF_SAME_DAY"


def test_next_day_0900_is_later():
    result = evaluate(datetime(2026, 11, 12, 9, 0))
    assert result["known_by"] == ctx.KNOWN_LATER
    assert result["time_bucket"] == ctx.BUCKET_UNKNOWN
    assert result["reason_code"] == "AFTER_CUTOFF_LATER_DAY"


def test_utc_0659_is_1459_cst_and_known():
    result = evaluate(datetime(2026, 11, 11, 6, 59, tzinfo=UTC))
    assert result["known_by"] == ctx.KNOWN_CLOSE
    assert result["known_at"].utcoffset() == timedelta(hours=8)


def test_utc_0701_is_1501_cst_and_post_close():
    result = evaluate(datetime(2026, 11, 11, 7, 1, tzinfo=UTC))
    assert result["known_by"] == ctx.KNOWN_POST_CLOSE
    assert result["reason_code"] == "AFTER_CUTOFF_SAME_DAY"


def test_aware_cst_input_is_equivalent():
    result = evaluate(datetime(2026, 11, 11, 14, 30, tzinfo=CST))
    assert result["known_by"] == ctx.KNOWN_CLOSE


def test_naive_is_interpreted_as_cst_only_when_trusted():
    trusted = evaluate(datetime(2026, 11, 11, 14, 30), naive_timezone_trusted=True)
    assert trusted["known_by"] == ctx.KNOWN_CLOSE
    untrusted = evaluate(datetime(2026, 11, 11, 14, 30), naive_timezone_trusted=False)
    assert untrusted["known_by"] == ctx.KNOWN_UNKNOWN
    assert untrusted["reason_code"] == "NAIVE_TIMESTAMP_NO_TRUSTED_TZ"


def test_missing_observed_at_is_unknown():
    result = evaluate(None)
    assert result["known_by"] == ctx.KNOWN_UNKNOWN
    assert result["reason_code"] == "OBSERVED_AT_MISSING"
    assert result["time_bucket"] == ctx.BUCKET_UNKNOWN


def test_source_not_ready_is_unknown_even_if_early():
    result = evaluate(datetime(2026, 11, 11, 9, 0), data_ready=False)
    assert result["known_by"] == ctx.KNOWN_UNKNOWN
    assert result["reason_code"] == "SOURCE_NOT_READY"


def test_published_at_after_cutoff_demotes_early_observation():
    result = evaluate(
        datetime(2026, 11, 11, 9, 0),
        published_at=datetime(2026, 11, 11, 16, 0),
    )
    assert result["known_by"] == ctx.KNOWN_POST_CLOSE
    assert result["reason_code"] == "AFTER_CUTOFF_SAME_DAY"


def test_published_at_next_day_demotes_to_later():
    result = evaluate(
        datetime(2026, 11, 11, 9, 0),
        published_at=datetime(2026, 11, 12, 8, 0),
    )
    assert result["known_by"] == ctx.KNOWN_LATER


def test_published_at_before_cutoff_and_late_observation_uses_latest():
    result = evaluate(
        datetime(2026, 11, 11, 20, 0),
        published_at=datetime(2026, 11, 11, 10, 0),
    )
    assert result["known_by"] == ctx.KNOWN_POST_CLOSE


def test_naive_cutoff_is_rejected():
    with pytest.raises(ValueError):
        ctx.evaluate_known_time(
            observed_at=datetime(2026, 11, 11, 10, 0),
            cutoff_at=datetime(2026, 11, 11, 15, 0),
            naive_timezone_trusted=True,
        )


def test_resolve_cutoff_at_is_explicit_and_strict():
    moment = ctx.resolve_cutoff_at(D2, "CLOSE")
    assert moment == CUTOFF
    assert moment.isoformat() == "2026-11-11T15:00:00+08:00"
    for bad in ("PREMARKET_0925", "OPEN", "POST_CLOSE"):
        with pytest.raises(ValueError):
            ctx.resolve_cutoff_at(D2, bad)


# ---------------------------------------------------------------- 集成：池来源时点


def _session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add_all([TradeCalendar(trade_date=day) for day in (D1, D2, D3)])
    db.add(StockBasic(code="600000", name="合成银行", industry="银行"))
    db.add(StockBasic(code="600009", name="合成空仓", industry="银行"))
    db.add_all(
        [
            LimitPool(trade_date=D2, code="600000", pool_type="up", name="合成银行", consecutive=2),
            LimitPool(trade_date=D2, code="600001", pool_type="up", name="合成科技", consecutive=1),
            StockDaily(trade_date=D2, code="600000", open=9.5, close=10.0, pct_chg=10.0),
            StockDaily(trade_date=D2, code="600009", open=5, close=5, pct_chg=0),
            StockDaily(trade_date=D3, code="600000", open=10.0, close=9.5, pct_chg=-5.0),
            LimitPool(trade_date=D3, code="600000", pool_type="broken", name="合成银行"),
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
                fetched_at=datetime(2026, 11, 11, 9, 0),
            ),
            StockConcept(trade_date=D2, code="600000", concept="芯片"),
        ]
    )
    db.commit()
    return db


def _log(day, task, rows, moment, status="ok"):
    return CollectLog(trade_date=day, task=task, status=status, rows=rows, created_at=moment)


def _build(db, code="600000"):
    # 服务级显式策略：这些用例验证「信任 CST naive」时的边界；
    # 不信任路径与服务默认值见 test_service_default_untrusted_naive_demotes_pool
    return ctx.build(db, code=code, as_of=D2, naive_timezone_trusted=True)


def _pool_evidence(data):
    return [
        item for item in data["evidence"] if item["claim"].startswith("观察日池记录")
    ]


@pytest.mark.parametrize(
    ("moment", "known_by", "bucket"),
    [
        (datetime(2026, 11, 11, 14, 59), ctx.KNOWN_CLOSE, ctx.BUCKET_KNOWN),
        (datetime(2026, 11, 11, 15, 0), ctx.KNOWN_CLOSE, ctx.BUCKET_KNOWN),
        (datetime(2026, 11, 11, 15, 1), ctx.KNOWN_POST_CLOSE, ctx.BUCKET_UNKNOWN),
        (datetime(2026, 11, 11, 15, 5), ctx.KNOWN_POST_CLOSE, ctx.BUCKET_UNKNOWN),
        (datetime(2026, 11, 11, 22, 0), ctx.KNOWN_POST_CLOSE, ctx.BUCKET_UNKNOWN),
        (datetime(2026, 11, 12, 9, 0), ctx.KNOWN_LATER, ctx.BUCKET_UNKNOWN),
    ],
)
def test_pool_log_time_boundaries_drive_bucket(moment, known_by, bucket):
    db = _session()
    try:
        db.add_all(
            [
                _log(D2, "pool_up", 2, moment),
                _log(D2, "pool_broken", 0, moment),
                _log(D2, "pool_down", 0, moment),
                _log(D3, "pool_up", 1, datetime(2026, 11, 12, 14, 0)),
                _log(D3, "pool_broken", 1, datetime(2026, 11, 12, 14, 0)),
                _log(D3, "pool_down", 0, datetime(2026, 11, 12, 14, 0)),
            ]
        )
        db.commit()
        data = _build(db)
        evidence = _pool_evidence(data)
        assert evidence, "池证据不应缺失"
        expected_status = (
            ctx.POOL_STATUS_UP if bucket == ctx.BUCKET_KNOWN else ctx.POOL_STATUS_UNKNOWN
        )
        assert data["at_cutoff_known"]["pool_status"] == expected_status
        if bucket == ctx.BUCKET_KNOWN:
            assert len(evidence) == 1
            assert evidence[0]["known_by"] == known_by
            assert evidence[0]["time_bucket"] == bucket
            assert evidence[0]["cutoff_at"] == "2026-11-11T15:00:00+08:00"
            assert data["at_cutoff_known"]["observed_pool_membership"] == ["up"]
        else:
            assert all(item["time_bucket"] == ctx.BUCKET_UNKNOWN for item in evidence)
            assert evidence[-1]["known_by"] == known_by
            assert evidence[-1]["cutoff_at"] == "2026-11-11T15:00:00+08:00"
            # 已知区不得出现盘后成员关系
            assert data["at_cutoff_known"]["observed_pool_membership"] == []
            assert "POOL_NOT_KNOWN_AT_CUTOFF" in data["quality"]["reasons"]
    finally:
        db.close()


def test_broken_failure_does_not_borrow_up_log_time():
    db = _session()
    try:
        db.add_all(
            [
                _log(D2, "pool_up", 2, datetime(2026, 11, 11, 14, 0)),
                _log(D2, "pool_broken", 0, datetime(2026, 11, 11, 14, 0), status="failed"),
                _log(D2, "pool_down", 0, datetime(2026, 11, 11, 14, 0)),
                _log(D3, "pool_up", 1, datetime(2026, 11, 12, 14, 0)),
                _log(D3, "pool_broken", 1, datetime(2026, 11, 12, 14, 0)),
                _log(D3, "pool_down", 0, datetime(2026, 11, 12, 14, 0)),
            ]
        )
        db.commit()
        data = _build(db, code="600009")
        # 空仓票需要三池都可信才能说 NOT_IN_POOL；broken 失败 → UNKNOWN
        assert data["at_cutoff_known"]["pool_status"] == ctx.POOL_STATUS_UNKNOWN
    finally:
        db.close()


def _add_conflict_rows(db):
    """让 600000 同时出现在 up / broken 两个池（模拟数据库当前行）。"""
    db.add(LimitPool(trade_date=D2, code="600000", pool_type="broken", name="合成银行"))


def test_post_cutoff_conflict_never_enters_known_area():
    """P1-01 反向用例：两个池都在 22:00 采集，已知区不得出现冲突或成员。"""
    db = _session()
    try:
        _add_conflict_rows(db)
        for task in ("pool_up", "pool_broken", "pool_down"):
            db.add(_log(D2, task, 1, datetime(2026, 11, 11, 22, 0)))
        db.commit()
        data = _build(db)
        known = data["at_cutoff_known"]
        assert known["pool_status"] == ctx.POOL_STATUS_UNKNOWN
        assert known["observed_pool_membership"] == []
        assert known["pool_scope_complete"] is False
        assert "POOL_CONFLICT" not in data["quality"]["reasons"]
        unknown = data["unknown_or_later"]["pool"]
        assert unknown["status"] == ctx.POOL_STATUS_CONFLICT
        assert unknown["reason_code"] == "POOL_CONFLICT_POST_CUTOFF"
        assert unknown["observed_pool_membership"] == ["broken", "up"]
        assert unknown["known_by"] == ctx.KNOWN_POST_CLOSE
        # 已知桶里没有任何 conflict 断言
        known_rows = [
            item for item in data["evidence"] if item["time_bucket"] == ctx.BUCKET_KNOWN
        ]
        assert all(item["reason_code"] != "POOL_CONFLICT" for item in known_rows)
        assert all(item["source_table"] != "limit_pool" for item in known_rows)
    finally:
        db.close()


def test_partially_known_conflict_is_not_asserted():
    """up 14:00、broken 22:00：只能描述已证明的 UP，不得断言无冲突。"""
    db = _session()
    try:
        _add_conflict_rows(db)
        db.add(_log(D2, "pool_up", 2, datetime(2026, 11, 11, 14, 0)))
        db.add(_log(D2, "pool_broken", 1, datetime(2026, 11, 11, 22, 0)))
        db.add(_log(D2, "pool_down", 0, datetime(2026, 11, 11, 14, 0)))
        db.commit()
        data = _build(db)
        known = data["at_cutoff_known"]
        assert known["pool_status"] == ctx.POOL_STATUS_UP
        assert known["observed_pool_membership"] == ["up"]
        assert known["pool_scope_complete"] is False
        assert known["unconfirmed_pool_sources"] == ["broken"]
        assert "POOL_CONFLICT" not in data["quality"]["reasons"]
        assert "POOL_OTHER_SOURCES_UNKNOWN" in data["quality"]["reasons"]
        assert data["unknown_or_later"]["pool"]["status"] == ctx.POOL_STATUS_CONFLICT
        # 兼容字段同样只含已知子集
        assert data["as_known_on_day"]["observed_pool_membership"] == ["up"]
        assert "POOL_CONFLICT" not in str(data["as_known_on_day"])
    finally:
        db.close()


def test_missing_broken_log_keeps_uncertainty():
    """up 14:00、broken 缺日志：保留不确定性，不得说「不在 broken」。"""
    db = _session()
    try:
        _add_conflict_rows(db)
        db.add(_log(D2, "pool_up", 2, datetime(2026, 11, 11, 14, 0)))
        db.add(_log(D2, "pool_down", 0, datetime(2026, 11, 11, 14, 0)))
        db.commit()
        data = _build(db)
        known = data["at_cutoff_known"]
        assert known["pool_status"] == ctx.POOL_STATUS_UP
        assert known["unconfirmed_pool_sources"] == ["broken"]
        assert known["pool_scope_complete"] is False
        unknown = data["unknown_or_later"]["pool"]
        assert unknown["status"] == ctx.POOL_STATUS_CONFLICT
        assert unknown["reason_code"] == "POOL_CONFLICT_NOT_PROVABLE"
        assert unknown["unconfirmed_membership"] == ["broken"]
    finally:
        db.close()


def test_conflict_with_all_sources_known_before_cutoff_is_visible():
    """两个来源都在 15:00 前且质量可信：允许显示冲突。"""
    db = _session()
    try:
        _add_conflict_rows(db)
        db.add(_log(D2, "pool_up", 2, datetime(2026, 11, 11, 14, 0)))
        db.add(_log(D2, "pool_broken", 1, datetime(2026, 11, 11, 14, 30)))
        db.add(_log(D2, "pool_down", 0, datetime(2026, 11, 11, 14, 0)))
        db.commit()
        data = _build(db)
        known = data["at_cutoff_known"]
        assert known["pool_status"] == ctx.POOL_STATUS_CONFLICT
        assert known["observed_pool_membership"] == ["broken", "up"]
        assert known["pool_scope_complete"] is True
        assert "POOL_CONFLICT" in data["quality"]["reasons"]
        assert data["unknown_or_later"]["pool"] is None
        row = [
            item for item in data["evidence"] if item["claim"] == "观察日池记录"
        ][0]
        assert row["time_bucket"] == ctx.BUCKET_KNOWN
        assert row["reason_code"] == "POOL_CONFLICT"
        # known_at 取所有相关来源的最晚时刻，且带 +08:00
        assert row["known_at"].isoformat() == "2026-11-11T14:30:00+08:00"
    finally:
        db.close()


def test_single_up_member_still_requires_peer_sources():
    """R2-P1-01：只出现在 up 时，broken/down 的未知也必须进入 pending。"""
    db = _session()
    try:
        db.add(_log(D2, "pool_up", 2, datetime(2026, 11, 11, 14, 0)))
        db.commit()
        data = _build(db)
        known = data["at_cutoff_known"]
        assert known["pool_status"] == ctx.POOL_STATUS_UP
        assert known["observed_pool_membership"] == ["up"]
        assert known["pool_scope_complete"] is False
        assert known["unconfirmed_pool_sources"] == ["broken", "down"]
        unknown = data["unknown_or_later"]["pool"]
        assert unknown["pending_sources"] == ["broken", "down"]
        # 已证明的 up 成员不能被说成「未证明成员」
        assert unknown["unconfirmed_membership"] == []
        assert unknown["status"] == ctx.POOL_STATUS_UP
        assert unknown["reason_code"] == "POOL_SCOPE_INCOMPLETE_NOT_PROVABLE"
        assert "POOL_OTHER_SOURCES_UNKNOWN" in data["quality"]["reasons"]
        # 兼容字段与已知区完整性语义一致
        assert data["as_known_on_day"]["pool_scope_complete"] is False
        assert data["as_known_on_day"]["unconfirmed_pool_sources"] == ["broken", "down"]
    finally:
        db.close()


def test_post_cutoff_peer_without_rows_is_not_proved_empty():
    """broken 22:00 才采集且没有成员行：不得认为 broken 已证明为空。"""
    db = _session()
    try:
        db.add(_log(D2, "pool_up", 2, datetime(2026, 11, 11, 14, 0)))
        db.add(_log(D2, "pool_broken", 0, datetime(2026, 11, 11, 22, 0)))
        db.commit()
        data = _build(db)
        known = data["at_cutoff_known"]
        assert known["pool_status"] == ctx.POOL_STATUS_UP
        assert known["pool_scope_complete"] is False
        assert known["unconfirmed_pool_sources"] == ["broken", "down"]
        unknown = data["unknown_or_later"]["pool"]
        assert unknown["reason_code"] == "POOL_SCOPE_INCOMPLETE_POST_CUTOFF"
        assert unknown["pending_sources"] == ["broken", "down"]
        # 已证明的 up 仍保持正向观察，不因 peer 盘后采集而降级
        assert unknown["status"] == ctx.POOL_STATUS_UP
        assert unknown["unconfirmed_membership"] == []
    finally:
        db.close()


def test_all_three_sources_known_with_only_up_member_is_scope_complete():
    db = _session()
    try:
        db.add(_log(D2, "pool_up", 2, datetime(2026, 11, 11, 14, 0)))
        db.add(_log(D2, "pool_broken", 0, datetime(2026, 11, 11, 14, 0)))
        db.add(_log(D2, "pool_down", 0, datetime(2026, 11, 11, 14, 0)))
        db.commit()
        data = _build(db)
        known = data["at_cutoff_known"]
        assert known["pool_status"] == ctx.POOL_STATUS_UP
        assert known["observed_pool_membership"] == ["up"]
        assert known["pool_scope_complete"] is True
        assert known["unconfirmed_pool_sources"] == []
        assert data["unknown_or_later"]["pool"] is None
        # 不因为 broken/down 可信且无行而误报冲突
        assert ctx.POOL_STATUS_CONFLICT != known["pool_status"]
    finally:
        db.close()


def test_all_three_sources_known_without_members_is_not_in_pool():
    db = _session()
    try:
        for task in ("pool_up", "pool_broken", "pool_down"):
            db.add(_log(D2, task, 2 if task == "pool_up" else 0, datetime(2026, 11, 11, 14, 0)))
        db.commit()
        data = _build(db, code="600009")
        known = data["at_cutoff_known"]
        assert known["pool_status"] == ctx.POOL_STATUS_NOT_IN_POOL
        assert known["observed_pool_membership"] == []
        assert known["pool_scope_complete"] is True
        assert data["unknown_or_later"]["pool"] is None
    finally:
        db.close()


def test_proven_conflict_with_pending_source_is_scope_incomplete_only():
    """up+broken 已证明冲突、down 缺日志：unknown 只能表达 scope 未闭环。"""
    db = _session()
    try:
        _add_conflict_rows(db)
        db.add(_log(D2, "pool_up", 2, datetime(2026, 11, 11, 14, 0)))
        db.add(_log(D2, "pool_broken", 1, datetime(2026, 11, 11, 14, 0)))
        db.commit()
        data = _build(db)
        known = data["at_cutoff_known"]
        assert known["pool_status"] == ctx.POOL_STATUS_CONFLICT
        assert known["observed_pool_membership"] == ["broken", "up"]
        assert known["pool_scope_complete"] is False
        assert known["unconfirmed_pool_sources"] == ["down"]
        unknown = data["unknown_or_later"]["pool"]
        assert unknown["unconfirmed_membership"] == []
        assert unknown["pending_sources"] == ["down"]
        assert unknown["reason_code"] == "POOL_SCOPE_INCOMPLETE_NOT_PROVABLE"
        # 已证明的冲突不得在未知区被重新描述成「冲突不可证明」
        assert "POOL_CONFLICT_NOT_PROVABLE" not in str(unknown)
        unknown_row = [
            item
            for item in data["evidence"]
            if item["claim"] == "观察日池记录（截止前未证明）"
        ][0]
        assert "已知冲突" in unknown_row["summary"]
        assert "未能在截止前全部证明" not in unknown_row["summary"]
        # 兼容字段只承载已知子集，且完整性语义与 at_cutoff_known 一致
        legacy = data["as_known_on_day"]
        assert legacy["observed_pool_membership"] == ["broken", "up"]
        assert legacy["pool_scope_complete"] is False
        assert legacy["unconfirmed_pool_sources"] == ["down"]
    finally:
        db.close()


def test_known_positive_with_no_unconfirmed_member_keeps_status():
    """只有已知 up、没有未证明成员：unknown 不得把 UP 说成未知成员关系。"""
    db = _session()
    try:
        db.add(_log(D2, "pool_up", 2, datetime(2026, 11, 11, 14, 0)))
        db.commit()
        data = _build(db)
        known = data["at_cutoff_known"]
        assert known["pool_status"] == ctx.POOL_STATUS_UP
        unknown = data["unknown_or_later"]["pool"]
        assert unknown["status"] == ctx.POOL_STATUS_UP
        assert unknown["unconfirmed_membership"] == []
        assert unknown["pending_sources"] == ["broken", "down"]
        row = [
            item
            for item in data["evidence"]
            if item["claim"] == "观察日池记录（截止前未证明）"
        ][0]
        assert "已知 up 可证明" in row["summary"]
        assert "观察到 up" not in row["summary"]
    finally:
        db.close()


def test_end_of_day_facts_never_enter_known_bucket():
    db = _session()
    try:
        for task in ("pool_up", "pool_broken", "pool_down"):
            db.add(_log(D2, task, 2 if task == "pool_up" else 0, datetime(2026, 11, 11, 14, 0)))
        for task, rows in (("pool_up", 1), ("pool_broken", 1), ("pool_down", 0)):
            db.add(_log(D3, task, rows, datetime(2026, 11, 12, 14, 0)))
        db.commit()
        data = _build(db)
        assert data["at_cutoff_known"]["daily"] is None
        assert data["at_cutoff_known"]["sentiment"] is None
        assert data["end_of_day_fact"]["daily"]["pct_chg"] == 10.0
        known_known_by = {item["known_by"] for item in data["evidence"] if item["time_bucket"] == ctx.BUCKET_KNOWN}
        assert ctx.KNOWN_END_OF_DAY_FACT not in known_known_by
        assert "END_OF_DAY_FACT_NOT_AT_CUTOFF" in data["unknown_or_later"]["reasons"]
    finally:
        db.close()


def test_forward_outcome_stays_out_of_known_and_as_known():
    db = _session()
    try:
        for task in ("pool_up", "pool_broken", "pool_down"):
            db.add(_log(D2, task, 2 if task == "pool_up" else 0, datetime(2026, 11, 11, 14, 0)))
        for task, rows in (("pool_up", 1), ("pool_broken", 1), ("pool_down", 0)):
            db.add(_log(D3, task, rows, datetime(2026, 11, 12, 14, 0)))
        db.commit()
        data = _build(db)
        forward = [item for item in data["evidence"] if item["time_bucket"] == ctx.BUCKET_FORWARD]
        assert len(forward) == 1
        assert forward[0]["known_by"] == ctx.KNOWN_FORWARD
        assert forward[0]["source_trade_date"] == D3
        assert data["later_outcome"]["pct_chg"] == -5.0
        assert data["as_known_on_day"]["daily"] is None
        assert data["as_known_on_day"]["sentiment"] is None
        assert data["as_known_on_day"]["schema_version"] == "research-evidence-v3"
        # 兼容字段里不允许出现后验或日终事实标记
        flat = str(data["as_known_on_day"])
        assert ctx.KNOWN_FORWARD not in flat
        assert ctx.KNOWN_END_OF_DAY_FACT not in flat
    finally:
        db.close()


def test_naive_timezone_untrusted_demotes_pool_evidence():
    db = _session()
    try:
        for task in ("pool_up", "pool_broken", "pool_down"):
            db.add(_log(D2, task, 2 if task == "pool_up" else 0, datetime(2026, 11, 11, 14, 0)))
        db.commit()
        result = ctx.evaluate_known_time(
            observed_at=datetime(2026, 11, 11, 14, 0),
            cutoff_at=CUTOFF,
            data_ready=True,
            naive_timezone_trusted=False,
        )
        assert result["known_by"] == ctx.KNOWN_UNKNOWN
        assert result["reason_code"] == "NAIVE_TIMESTAMP_NO_TRUSTED_TZ"
    finally:
        db.close()


# ---------------------------------------------------------------- 服务级 naive 策略


def _seed_simple_pools(db, moment):
    for task, rows in (("pool_up", 2), ("pool_broken", 0), ("pool_down", 0)):
        db.add(_log(D2, task, rows, moment))
    db.commit()


@pytest.mark.parametrize(
    ("hour", "minute", "expected_status", "expected_bucket"),
    [
        (14, 59, ctx.POOL_STATUS_UP, ctx.BUCKET_KNOWN),
        (15, 0, ctx.POOL_STATUS_UP, ctx.BUCKET_KNOWN),
        (15, 1, ctx.POOL_STATUS_UNKNOWN, ctx.BUCKET_UNKNOWN),
    ],
)
def test_service_trusted_naive_boundaries(hour, minute, expected_status, expected_bucket):
    db = _session()
    try:
        _seed_simple_pools(db, datetime(2026, 11, 11, hour, minute))
        data = ctx.build(db, code="600000", as_of=D2, naive_timezone_trusted=True)
        assert data["time_policy"]["naive_timestamp_policy"] == ctx.NAIVE_POLICY_CST_ASSUMED
        assert data["at_cutoff_known"]["pool_status"] == expected_status
        evidence = _pool_evidence(data)
        assert evidence[-1]["time_bucket"] == expected_bucket
    finally:
        db.close()


def test_service_default_untrusted_naive_demotes_pool():
    """服务默认（配置 false）不信任历史 naive 时间 → 池证据降级 UNKNOWN。"""
    db = _session()
    try:
        _seed_simple_pools(db, datetime(2026, 11, 11, 14, 0))
        data = ctx.build(db, code="600000", as_of=D2)  # 不传显式策略
        assert data["time_policy"]["naive_timestamp_policy"] == ctx.NAIVE_POLICY_UNTRUSTED
        assert data["time_policy"]["naive_timezone_trusted"] is False
        assert data["at_cutoff_known"]["pool_status"] == ctx.POOL_STATUS_UNKNOWN
        assert data["at_cutoff_known"]["observed_pool_membership"] == []
        unknown = data["unknown_or_later"]["pool"]
        assert unknown["known_by"] == ctx.KNOWN_UNKNOWN
        assert unknown["reason_code"] == "POOL_MEMBERSHIP_NOT_PROVABLE"
        # 来源质量与 PIT 分离：质量仍然 VERIFIED，但不等于「15:00 已知」
        assert data["source_quality"]["pools"]["up"] == ctx.STATUS_VERIFIED
        assert data["source_quality"]["pool_trusted"]["up"] is True
        assert data["source_quality"]["pool_known_at"]["up"] is None
        assert "POOL_NOT_KNOWN_AT_CUTOFF" in data["quality"]["reasons"]
    finally:
        db.close()


def test_service_known_at_is_aware_cst():
    db = _session()
    try:
        _seed_simple_pools(db, datetime(2026, 11, 11, 14, 30))
        data = ctx.build(db, code="600000", as_of=D2, naive_timezone_trusted=True)
        row = _pool_evidence(data)[0]
        assert row["known_at"].utcoffset() == timedelta(hours=8)
        assert row["known_at"].isoformat() == "2026-11-11T14:30:00+08:00"
        assert row["observed_at"] == row["known_at"]
        assert data["source_quality"]["pool_known_at"]["up"] == "2026-11-11T14:30:00+08:00"
        assert data["as_known_on_day"]["observed_pool_membership"] == ["up"]
    finally:
        db.close()


def test_service_theme_time_also_requires_naive_policy():
    """题材时点同样受 naive 策略约束：不信任时不能靠 naive fetched_at 判 PRIOR_DAY。"""
    db = _session()
    try:
        _seed_simple_pools(db, datetime(2026, 11, 11, 14, 30))
        db.query(SectorMember).filter(SectorMember.code == "600000").update(
            {"fetched_at": datetime(2026, 11, 10, 22, 0)}
        )
        db.commit()
        trusted = ctx.build(db, code="600000", as_of=D2, naive_timezone_trusted=True)
        assert trusted["at_cutoff_known"]["themes"][0]["known_by"] == ctx.KNOWN_PRIOR_DAY
        untrusted = ctx.build(db, code="600000", as_of=D2, naive_timezone_trusted=False)
        assert untrusted["at_cutoff_known"]["themes"] == []
        assert untrusted["unknown_or_later"]["themes"][0]["known_by"] == ctx.KNOWN_UNKNOWN
    finally:
        db.close()

