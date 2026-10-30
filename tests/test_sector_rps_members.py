"""板块 RPS 榜「点开看成分股」（2026-10-30 加）。

钉三件事：

1. 默认取该板块**最近一次**成分快照，按涨幅降序；
2. 指定一个没有快照的旧日期时**回落到最近一次**并把实际日期返回 —— `sector_member`
   只有快照、没有历史回补，给空表会让人以为「这个板块是空的」；
3. 不存在的板块给空列表，不报错。
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.pools import sector_rps_members
from app.models import Base, SectorMember

D1 = date(2026, 10, 27)
D2 = date(2026, 10, 28)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'rps-member.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                SectorMember(
                    trade_date=D1,
                    sector_code="801027",
                    code="600036",
                    name="招商银行",
                    close=40.0,
                    pct_chg=1.0,
                    amount=1e9,
                    turnover=0.5,
                ),
                SectorMember(
                    trade_date=D2,
                    sector_code="801027",
                    code="600036",
                    name="招商银行",
                    close=41.0,
                    pct_chg=2.5,
                    amount=2e9,
                    turnover=0.8,
                ),
                SectorMember(
                    trade_date=D2,
                    sector_code="801027",
                    code="601398",
                    name="工商银行",
                    close=6.0,
                    pct_chg=1.2,
                    amount=3e9,
                    turnover=0.3,
                ),
            ]
        )
        session.commit()
        yield session


def test_members_default_latest_sorted_by_pct_desc(db):
    out = sector_rps_members(code="801027", trade_date=None, session=db)

    assert out.trade_date == D2
    assert [row.code for row in out.rows] == ["600036", "601398"]
    assert out.rows[0].pct_chg == 2.5


def test_old_date_falls_back_to_latest_snapshot(db):
    """旧日期没有快照 → 回落到最近一次，并把实际日期带回去。"""
    out = sector_rps_members(code="801027", trade_date=date(2026, 9, 1), session=db)

    assert out.trade_date == D2
    assert len(out.rows) == 2


def test_unknown_sector_returns_empty(db):
    out = sector_rps_members(code="999999", trade_date=None, session=db)

    assert out.trade_date is None
    assert out.rows == []
