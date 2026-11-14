"""S6-C2 回归：空库 / 空交易日历时，质量时间线必须返回空结构而不是 500/400。

背景：S6 浏览器验收在空 Staging 库上发现
`GET /api/quality/timeline?source=limit_pool&days=30` 返回 400
（`min() iterable argument is empty`），页面显示「加载失败」——
把「未采集」误报成接口错误。本测试锁定修复：返回 200 + `points=[]`。
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base
from app.services import data_quality


def _empty_session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_empty_calendar_timeline_returns_empty_points():
    with _empty_session() as session:
        payload = data_quality.timeline(session, source="limit_pool", days=30)
    assert payload["points"] == []
    assert payload["window_start"] is None
    assert payload["window_end"] is None
    assert payload["source"] == "limit_pool"


def test_empty_calendar_overview_still_works():
    with _empty_session() as session:
        payload = data_quality.overview(session, target_date=None, days=30)
    assert "sources" in payload
    assert payload["sources"]
