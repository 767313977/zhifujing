"""风控提醒（FEAT-RISK-GUARD）：偏好落库、过热触发、集中度。"""

from datetime import date

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from app.models import (
    AppUser,
    Base,
    MarketSentiment,
    SectorBasic,
    SectorMember,
    StockConcept,
    TradeJournal,
)
from app.services import risk

DAY = date(2026, 10, 30)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'risk.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = AppUser(username="u1", password_hash="x")
        session.add(user)
        session.flush()
        session.add(MarketSentiment(trade_date=DAY, max_consecutive=7))
        session.add(StockConcept(trade_date=DAY, code="300001", concept="机器人"))
        session.add_all(
            [
                TradeJournal(
                    user_id=user.id,
                    code="300001",
                    name="测试A",
                    entry_date=DAY,
                    entry_price=10.0,
                    position_pct=20.0,
                ),
                TradeJournal(
                    user_id=user.id,
                    code="300001",
                    name="测试A",
                    entry_date=DAY,
                    entry_price=11.0,
                    position_pct=20.0,
                ),
                TradeJournal(
                    user_id=user.id,
                    code="300001",
                    name="测试A",
                    entry_date=DAY,
                    entry_price=12.0,
                    position_pct=20.0,
                ),
            ]
        )
        session.commit()
        yield session, user


def test_defaults_and_overheat(db):
    session, user = db
    pref = risk.get_pref(session, user.id)
    assert pref["heat_consecutive"] == 6

    data = risk.status(session, user.id)
    assert data["overheat"] is True
    assert any("连板高度" in item for item in data["triggers"])

    kinds = {(item["kind"], item["name"]) for item in data["concentration"]}
    assert ("position", "测试A") in kinds
    assert ("theme", "机器人") in kinds


def test_theme_concentration_reads_sector_member(db):
    """没涨停过的持仓也要能归到题材：`stock_concept` 只记当日涨停股，查不到就静默跳过。"""
    session, user = db
    session.add_all(
        [
            SectorBasic(code="801100", name="白酒", taxonomy="kph_selected"),
            SectorMember(
                trade_date=DAY, sector_code="801100", code="600519", name="测试C"
            ),
            TradeJournal(
                user_id=user.id,
                code="600519",
                name="测试C",
                entry_date=DAY,
                entry_price=100.0,
                position_pct=60.0,
            ),
        ]
    )
    session.commit()

    data = risk.status(session, user.id)
    kinds = {(item["kind"], item["name"]) for item in data["concentration"]}
    assert ("theme", "白酒") in kinds


def test_missing_theme_is_disclosed(db):
    """整只票查不到题材时，必须写清「这几笔没进题材集中度」，不能静默跳过。"""
    session, user = db
    session.execute(delete(StockConcept))
    session.commit()
    risk.save_pref(
        session,
        user.id,
        {
            "max_position_pct": 80.0,
            "max_theme_pct": 90.0,
            "heat_consecutive": 10,
            "heat_temperature": 100,
            "strict_mode": False,
        },
    )

    data = risk.status(session, user.id)
    assert data["concentration"] == []
    assert "3 笔持仓查不到题材归属" in data["message"]


def test_missing_position_pct_is_disclosed(db):
    """未填仓位比例的持仓不参与集中度 —— 必须在 message 里说出来，而不是静默跳过。"""
    session, user = db
    session.add(
        TradeJournal(
            user_id=user.id,
            code="600000",
            name="测试B",
            entry_date=DAY,
            entry_price=10.0,
        )
    )
    session.commit()

    data = risk.status(session, user.id)
    assert "1 笔持仓未填仓位比例" in data["message"]
    assert "不参与集中度计算" in data["message"]
    # 只提示、不阻断：过热仍然只看连板 / 恐贪
    assert data["overheat"] is True


def test_missing_position_pct_note_when_no_trigger(db):
    """没有任何触发时，message 也要带上这句说明。"""
    session, user = db
    risk.save_pref(
        session,
        user.id,
        {
            "max_position_pct": 80.0,
            "max_theme_pct": 90.0,
            "heat_consecutive": 10,
            "heat_temperature": 100,
            "strict_mode": False,
        },
    )
    session.add(
        TradeJournal(
            user_id=user.id,
            code="600000",
            name="测试B",
            entry_date=DAY,
            entry_price=10.0,
        )
    )
    session.commit()

    data = risk.status(session, user.id)
    assert data["concentration"] == []
    assert data["message"].startswith("当前没有触发风控提醒")
    assert "1 笔持仓未填仓位比例" in data["message"]


def test_pref_change_reflects_status(db):
    session, user = db
    risk.save_pref(
        session,
        user.id,
        {
            "max_position_pct": 80.0,
            "max_theme_pct": 90.0,
            "heat_consecutive": 10,
            "heat_temperature": 100,
            "strict_mode": True,
        },
    )
    data = risk.status(session, user.id)
    assert data["overheat"] is False
    assert data["concentration"] == []
    assert data["strict_mode"] is True
    assert data["message"] == "当前没有触发风控提醒"
