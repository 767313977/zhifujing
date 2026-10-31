"""结构化笔记（FEAT-JOURNAL-STRUCT）：标签保存 + 月汇总 + 用户隔离。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import note as note_api
from app.models import AppUser, Base
from app.schemas import NoteIn
from app.timeutil import today_cn


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'notes.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        mine = AppUser(username="u1", password_hash="x")
        other = AppUser(username="u2", password_hash="x")
        session.add_all([mine, other])
        session.commit()
        yield session, mine, other


def test_structured_note_roundtrip_and_summary(db):
    session, user, other = db
    day = today_cn()
    payload = NoteIn(
        market_view="缩量退潮",
        next_plan="看高度能否延续",
        mainlines=["机器人", "算力", "机器人"],
        mistake_tags=["追高", "追高", "情绪误判"],
        good_tags=["按计划"],
        watch_points="5 板以上能否连板",
        plan_ids=[1, 2],
    )
    saved = note_api.save_note(day, payload, user, session)
    assert saved.mainlines == ["机器人", "算力"]
    assert saved.mistake_tags == ["追高", "情绪误判"]
    assert saved.plan_ids == [1, 2]

    again = note_api.get_note(day, user, session)
    assert again.mainlines == ["机器人", "算力"]
    assert again.watch_points == "5 板以上能否连板"

    summary = note_api.note_summary(month=day.strftime("%Y-%m"), user=user, session=session)
    assert summary.notes == 1
    assert {row.tag: row.count for row in summary.mainlines} == {"机器人": 1, "算力": 1}
    assert {row.tag: row.count for row in summary.mistakes} == {"追高": 1, "情绪误判": 1}
    assert summary.plan_links == 1
    assert summary.watch_points == ["5 板以上能否连板"]

    # 另一个用户看不到我的笔记统计
    other_summary = note_api.note_summary(
        month=day.strftime("%Y-%m"), user=other, session=session
    )
    assert other_summary.notes == 0
