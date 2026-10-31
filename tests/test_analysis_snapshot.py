"""查票结论快照（FEAT-ANALYSIS-SNAPSHOT）：保留上限与用户隔离。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import AppUser, Base
from app.services import analysis_snapshot


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'snapshot.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        mine = AppUser(username="u1", password_hash="x")
        other = AppUser(username="u2", password_hash="x")
        session.add_all([mine, other])
        session.commit()
        yield session, mine.id, other.id


def test_save_and_list_newest_first(db):
    session, user_id, _ = db
    first = analysis_snapshot.save(
        session, user_id, "300001", "甲", "明天盯 · 2026-10-30", {"code": "300001"}
    )
    second = analysis_snapshot.save(
        session, user_id, "300001", "甲", "启动 · 2026-10-31", {"code": "300001"}
    )
    session.commit()

    rows = analysis_snapshot.rows(session, user_id)
    assert [row.id for row in rows] == [second.id, first.id]
    assert rows[0].summary == "启动 · 2026-10-31"
    assert rows[0].payload == {"code": "300001"}


def test_keeps_only_latest_twenty_per_code(db):
    session, user_id, _ = db
    ids = []
    for index in range(analysis_snapshot.MAX_PER_CODE + 5):
        row = analysis_snapshot.save(
            session, user_id, "300001", "甲", f"第 {index} 条", None
        )
        ids.append(row.id)
    session.commit()

    rows = analysis_snapshot.rows(session, user_id)
    assert len(rows) == analysis_snapshot.MAX_PER_CODE
    # 留下的必须是最新的那 20 条
    assert [row.id for row in rows] == list(reversed(ids[-analysis_snapshot.MAX_PER_CODE :]))


def test_user_isolation(db):
    session, user_id, other_id = db
    row = analysis_snapshot.save(session, user_id, "300001", "甲", "我的结论", None)
    session.commit()

    assert analysis_snapshot.rows(session, other_id) == []
    assert analysis_snapshot.get(session, other_id, row.id) is None
    assert analysis_snapshot.remove(session, other_id, row.id) is False
    # 别人的操作没有把记录删掉
    assert analysis_snapshot.get(session, user_id, row.id) is not None
