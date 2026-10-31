"""风险角标（FEAT-RISK-BADGES）：只读缓存、不触发外部请求。"""

from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import stock as stock_api
from app.models import AppUser, Base, StockEventCache
from app.services import trap
from app.timeutil import today_cn

CODE = "300001"


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'risk-badges.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = AppUser(username="u1", password_hash="x")
        session.add(user)
        session.flush()
        today = today_cn()
        session.add_all(
            [
                StockEventCache(
                    code=CODE,
                    kind="unlock",
                    fetched_at=datetime.now(),
                    payload=[
                        {
                            "unlock_date": (today + timedelta(days=3)).isoformat(),
                            "ratio_total": 0.08,
                        }
                    ],
                ),
                StockEventCache(
                    code=CODE,
                    kind="notices",
                    fetched_at=datetime.now(),
                    payload=[
                        {
                            "published_at": today.isoformat(),
                            "title": "关于股东减持计划的公告",
                            "category": "其他",
                            "url": "",
                        }
                    ],
                ),
            ]
        )
        session.commit()
        # 体检缓存：模拟个股页刚看过
        stock_api._trap_cache[(CODE, "")] = (
            datetime.now(),
            trap.TrapResult(
                code=CODE,
                name="测试",
                trade_date=today,
                score=80,
                level="high",
                level_label="高风险",
                summary="多项信号命中",
                signals=[],
            ),
        )
        yield session, user
        stock_api._trap_cache.pop((CODE, ""), None)


def test_badges_from_cache_only(db):
    session, user = db
    data = stock_api.risk_badges(codes=f"{CODE},300002", user=user, session=session)
    rows = {row.code: row for row in data.rows}
    kinds = {badge.kind for badge in rows[CODE].badges}
    assert kinds == {"trap", "unlock", "notice"}
    assert rows["300002"].badges == []
    assert data.note


def test_rejects_too_many_codes(db):
    session, user = db
    codes = ",".join(f"{600000 + index:06d}" for index in range(51))
    with pytest.raises(HTTPException) as exc:
        stock_api.risk_badges(codes=codes, user=user, session=session)
    assert exc.value.status_code == 400
