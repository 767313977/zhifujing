"""「知行B1 · 汇总」chip（2026-11-07 用户要求）。

汇总不是真实形态（扫描不写 `zgnb_all` 的行），只在查询层把 7 个成员并起来。
这里钉四件事：

1. 只选「汇总」= 命中七个成员里**任意一个**的票，一只票只出一行；
2. 汇总与其它形态之间仍是**交集**；
3. 汇总行的评分由**组内成员**决定，不被同一天命中的别的高分形态顶上去；
4. `/preview` 的徽标数 = 点进去 `/hits` 的行数（含窗口模式）。
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import patterns as patterns_api
from app.models import Base, PatternHit, TradeCalendar
from app.services.patterns import PATTERNS, ZGNB_GROUP, ZGNB_KEYS

ALIAS = "zgnb_all"
DAYS = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)]
DAY = DAYS[-1]
PREV = DAYS[-2]


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'alias.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([TradeCalendar(trade_date=day) for day in DAYS])

        def hit(code: str, pattern: str, score: float, day: date = DAY) -> None:
            session.add(
                PatternHit(
                    trade_date=day,
                    code=code,
                    pattern=pattern,
                    name=code,
                    score=score,
                    close=10.0,
                    key_levels={},
                    detail={},
                )
            )

        # 300001 同时中了组内一个成员和一个**不是**组内的高分形态
        hit("300001", "zgnb_turn", 60.0)
        hit("300001", "volume_surge", 90.0)
        # 300002 只中组内另一个成员
        hit("300002", "zgnb_shrink", 55.0)
        # 300003 只在昨天中了组内成员（窗口模式用）
        hit("300003", "zgnb_pull_yellow", 70.0, PREV)
        # 300004 一个组内成员都不中
        hit("300004", "volume_surge", 80.0)
        session.commit()
        yield session


def _hits(session, pattern: list[str], *, days: int = 1):
    return patterns_api.hits(
        db=session,
        trade_date=DAY,
        min_score=0.0,
        pattern=pattern,
        days=days,
        limit=800,
        order="score",
        phase=None,
    )


def _badge(session, pattern: list[str], *, days: int = 1) -> dict[str, int]:
    return {
        row.pattern: row.stocks
        for row in patterns_api.preview(
            db=session,
            trade_date=DAY,
            min_score=0.0,
            pattern=pattern,
            days=days,
            phase=None,
        )
    }


def test_catalog_puts_summary_last_in_its_group():
    metas = patterns_api.catalog()
    keys = [meta.key for meta in metas if meta.group == ZGNB_GROUP]
    assert keys[:-1] == list(ZGNB_KEYS)
    assert keys[-1] == ALIAS
    assert metas[-1].key == ALIAS
    assert metas[-1].name == "汇总"


def test_summary_is_union_of_members(db):
    out = _hits(db, [ALIAS])
    codes = {item.code for item in out.items}
    assert codes == {"300001", "300002"}
    # 一只票只出一行（两个成员都中也不会拆成两条）
    assert len(out.items) == len(codes) or out.truncated
    # 评分取组内成员的最高分（60），不是同一天命中的 volume_surge（90）
    row = next(item for item in out.items if item.code == "300001")
    assert row.score == 60.0


def test_summary_intersects_with_other_chips(db):
    out = _hits(db, [ALIAS, "zgnb_shrink"])
    assert {item.code for item in out.items} == {"300002"}
    # 组内成员与汇总同时选 = 还是那个成员（并集 ∩ 成员）
    out = _hits(db, [ALIAS, "volume_surge"])
    assert {item.code for item in out.items} == {"300001"}


def test_preview_badge_matches_hits_rows(db):
    # 没选任何 chip：汇总是当天并集家数
    assert _badge(db, [])[ALIAS] == 2
    # 选了汇总：徽标 = 点进去的行数；同时别的候选按「再加它会剩多少只」算
    badge = _badge(db, [ALIAS])
    assert badge[ALIAS] == len(_hits(db, [ALIAS]).items)
    assert badge["zgnb_turn"] == 1
    assert badge["volume_surge"] == 1
    assert badge["zgnb_pull_yellow"] == 0  # 今天没有它


def test_summary_window_spans_days(db):
    out = _hits(db, [ALIAS], days=3)
    assert {item.code for item in out.items} == {"300001", "300002", "300003"}
    badge = _badge(db, [ALIAS], days=3)
    assert badge[ALIAS] == len(out.items)
    # 窗口模式下「汇总 + 组内成员」仍是交集（300001/300002 都算，300003 只中另一支）
    assert {item.code for item in _hits(db, [ALIAS, "zgnb_shrink"], days=3).items} == {
        "300002"
    }


def test_alias_members_are_the_whole_group():
    """成员从注册表按分组现算 —— 以后往知行B1 加形态，汇总自动包含它。"""
    assert ZGNB_KEYS == (
        "zgnb_turn",
        "zgnb_shrink",
        "zgnb_origin",
        "zgnb_super_shrink",
        "zgnb_pull_white",
        "zgnb_pull_super",
        "zgnb_pull_yellow",
    )
    assert all(p.group == ZGNB_GROUP for p in PATTERNS if p.key in ZGNB_KEYS)
