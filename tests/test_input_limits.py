"""新写接口的入参长度上限（2026-11-01 审计 P2-2）。

`payload` 的**体积**上限在 `api/analysis.create_snapshot` 里按序列化后校验，
这里只钉住 pydantic 层的字段长度（超限直接 422，不会落库）。
"""

from datetime import date

import pytest
from pydantic import ValidationError

from app.schemas import (
    AnalysisSnapshotIn,
    PositionIn,
    TradeJournalIn,
    TradePlanIn,
    TradeTIn,
)

ENTRY = date(2026, 11, 2)


def test_journal_note_and_reason_text_have_limits():
    with pytest.raises(ValidationError):
        TradeJournalIn(
            code="600000", entry_date=ENTRY, entry_price=10.0, note="x" * 20001
        )
    with pytest.raises(ValidationError):
        TradeJournalIn(
            code="600000", entry_date=ENTRY, entry_price=10.0, reason_text="x" * 20001
        )
    # 上限之内照常通过
    TradeJournalIn(
        code="600000", entry_date=ENTRY, entry_price=10.0, note="x" * 20000
    )


def test_journal_name_and_code_limits():
    with pytest.raises(ValidationError):
        TradeJournalIn(
            code="600000", entry_date=ENTRY, entry_price=10.0, name="名" * 65
        )
    with pytest.raises(ValidationError):
        TradeJournalIn(code="6" * 17, entry_date=ENTRY, entry_price=10.0)


def test_trade_t_note_limit():
    with pytest.raises(ValidationError):
        TradeTIn(trade_date=ENTRY, note="x" * 20001)


def test_plan_text_limits():
    with pytest.raises(ValidationError):
        TradePlanIn(
            code="600000",
            plan_date=ENTRY,
            buy_price=10.0,
            stop_price=9.0,
            note="x" * 20001,
        )
    with pytest.raises(ValidationError):
        TradePlanIn(
            code="600000",
            plan_date=ENTRY,
            buy_price=10.0,
            stop_price=9.0,
            position_note="x" * 20001,
        )
    with pytest.raises(ValidationError):
        TradePlanIn(
            code="600000",
            plan_date=ENTRY,
            buy_price=10.0,
            stop_price=9.0,
            deviation="x" * 65,
        )


def test_position_limits():
    with pytest.raises(ValidationError):
        PositionIn(code="600000", entry_date=ENTRY, entry_price=10.0, name="名" * 65)
    with pytest.raises(ValidationError):
        PositionIn(code="600000", entry_date=ENTRY, entry_price=10.0, note="x" * 20001)


def test_snapshot_summary_limit():
    with pytest.raises(ValidationError):
        AnalysisSnapshotIn(code="600000", summary="x" * 2001)
    with pytest.raises(ValidationError):
        AnalysisSnapshotIn(code="600000", summary="ok", name="名" * 65)
