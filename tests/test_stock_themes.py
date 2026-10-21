"""「所属题材」的取数口径：优先板块成分股快照，退回落涨停天梯。

2026-10-21 用户反馈「所属题材没有数据」：原来只读 `stock_concept`（只覆盖涨停股），
没涨停过的票永远是「—」。这里钉住新的两级取数顺序。
"""

from datetime import date

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.api.stock import _is_non_theme, themes
from app.models import (
    Base,
    SectorBasic,
    SectorDaily,
    SectorMember,
    StockBasic,
    StockConcept,
)

MEMBER_DAY = date(2026, 10, 19)
BOARD_DAY = date(2026, 10, 21)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", future=True)

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(StockBasic(code="688410", name="山外山"))
        session.add(StockBasic(code="600000", name="浦发银行"))
        # 只挂着「科创板」这类身份标签的票：黑名单剔完一个不剩时仍要显示
        session.add(StockBasic(code="600002", name="只有身份标签"))
        session.add_all(
            [
                SectorBasic(code="801046", name="医疗器械", taxonomy="kph_selected"),
                SectorBasic(code="881144", name="医疗器械", taxonomy="kph_industry"),
                SectorBasic(code="801351", name="科创板", taxonomy="kph_selected"),
                SectorBasic(code="801900", name="再融资", taxonomy="kph_selected"),
                SectorDaily(
                    trade_date=BOARD_DAY,
                    sector_code="801046",
                    name="医疗器械",
                    taxonomy="kph_selected",
                    pct_chg=1.2,
                ),
                SectorDaily(
                    trade_date=BOARD_DAY,
                    sector_code="801351",
                    name="科创板",
                    taxonomy="kph_selected",
                    pct_chg=-0.5,
                ),
                # 688410 在最新快照里挂了 2 个精选板块 + 1 个行业板块（行业不进题材）
                SectorMember(
                    trade_date=MEMBER_DAY,
                    sector_code="801046",
                    code="688410",
                    name="山外山",
                ),
                SectorMember(
                    trade_date=MEMBER_DAY,
                    sector_code="801351",
                    code="688410",
                    name="山外山",
                ),
                SectorMember(
                    trade_date=MEMBER_DAY,
                    sector_code="881144",
                    code="688410",
                    name="山外山",
                ),
                # 融资 / 事件型标签（2026-10-21 补进黑名单）
                SectorMember(
                    trade_date=MEMBER_DAY,
                    sector_code="801900",
                    code="688410",
                    name="山外山",
                ),
                # 600000 没有成分股快照，只有涨停天梯里的两条历史题材
                # （「国有企业」是身份标签，走天梯分支时也要被剔掉）
                StockConcept(
                    trade_date=date(2026, 9, 1),
                    code="600000",
                    concept="银行",
                ),
                StockConcept(
                    trade_date=date(2026, 9, 1),
                    code="600000",
                    concept="国有企业",
                ),
                # 600003 只有一条身份标签：剔空后按原样保留
                StockConcept(
                    trade_date=date(2026, 9, 1),
                    code="600003",
                    concept="低价股",
                ),
                # 600002 只有一个「身份」标签，黑名单会把它剔空
                SectorMember(
                    trade_date=MEMBER_DAY,
                    sector_code="801351",
                    code="600002",
                    name="只有身份标签",
                ),
            ]
        )
        session.commit()
        yield session


def test_membership_beats_limit_up_concept(db):
    out = themes("688410", session=db)
    assert out.name == "山外山"
    # 科创板（身份标签）、再融资（事件标签）都被黑名单剔掉，只剩医疗器械
    assert [item.concept for item in out.themes] == ["医疗器械"]
    assert out.board_date == BOARD_DAY
    # 行业板块（同一名字、kph_industry）不重复出现在题材里
    assert all(item.board_code != "881144" for item in out.themes)


def test_generic_label_kept_when_nothing_else(db):
    out = themes("600002", session=db)
    # 剔完一个不剩时保留原样，不能又变回「—」
    assert [item.concept for item in out.themes] == ["科创板"]


def test_falls_back_to_limit_up_concept(db):
    out = themes("600000", session=db)
    # 天梯分支同样过黑名单：国有企业被剔掉，银行留下
    assert [item.concept for item in out.themes] == ["银行"]
    # 板块表里没有「银行」，board_code 为 None，但题材名仍要显示
    assert out.themes[0].board_code is None


def test_fallback_branch_keeps_all_when_filtered_empty(db):
    out = themes("600003", session=db)
    assert [item.concept for item in out.themes] == ["低价股"]


def test_event_labels_are_filtered():
    """2026-10-21 补进共享黑名单的融资 / 事件型标签。"""
    for name in ("再融资", "可转债", "分拆上市预期", "实控人变更", "壳资源"):
        assert _is_non_theme(name)


def test_unknown_code_returns_empty(db):
    out = themes("600001", session=db)
    assert out.themes == []
