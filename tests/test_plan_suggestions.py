"""次日计划候选表带出板块 / 行业（2026-10-29 加列）。

口径与形态页同源（`stock_meta.sectors_of` / `industries_of`），这里钉三件事：

1. 板块取该股**最近一天**的涨停题材，不是当天、也不是最早那天；
2. 行业原样带出完整路径（前端只显示一级、完整值放 title）；
3. 没涨停过的票 `sectors` 是空列表、查不到行业给 `None` —— **不要编「其他」**。
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, PatternHit, StockBasic, StockConcept
from app.services.trade_plan import suggestions

DAY = date(2026, 10, 28)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'plan-sug.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _seed(db: Session) -> None:
    db.add_all(
        [
            StockBasic(
                code="300461",
                name="田中精机",
                industry="机械设备-自动化设备-其他自动化设备",
            ),
            StockBasic(code="600351", name="亚宝药业", industry="医药生物-中药-中药Ⅲ"),
            PatternHit(
                trade_date=DAY,
                code="300461",
                pattern="v_bottom",
                name="田中精机",
                score=100.0,
                key_levels={"breakout": 40.78, "support": 27.33},
            ),
            PatternHit(
                trade_date=DAY,
                code="600351",
                pattern="new_high",
                name="亚宝药业",
                score=99.8,
                key_levels={"breakout": 7.07},
            ),
            # 同一只票两次涨停挂不同题材：板块必须取**最近那天**
            StockConcept(code="300461", trade_date=date(2026, 10, 20), concept="人形机器人"),
            StockConcept(code="300461", trade_date=date(2026, 10, 27), concept="工业母机"),
        ]
    )
    db.commit()


def test_suggestions_carry_sector_and_industry(db):
    _seed(db)

    rows = {row["code"]: row for row in suggestions(db, user_id=1)}

    assert rows["300461"]["sectors"] == ["工业母机"]
    assert rows["300461"]["industry"] == "机械设备-自动化设备-其他自动化设备"
    # 加列不影响原有字段
    assert rows["300461"]["buy_price"] == 40.78
    assert rows["300461"]["stop_price"] == 27.33


def test_missing_sector_is_empty_not_invented(db):
    """没涨停过的票板块给空列表；行业查不到给 None，都不编占位值。"""
    _seed(db)
    db.add(
        PatternHit(
            trade_date=DAY,
            code="000001",
            pattern="new_high",
            name="平安银行",
            score=90.0,
            key_levels={"breakout": 12.0},
        )
    )
    db.commit()

    rows = {row["code"]: row for row in suggestions(db, user_id=1)}

    assert rows["000001"]["sectors"] == []
    assert rows["000001"]["industry"] is None
