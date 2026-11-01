"""形态命中列表的「按量价因子分排序」开关（2026-11-01）。

钉三件事：
1. `order=factor` **只改顺序**：入选集合、`score` 字段都不变，另带回 `factor_score`；
2. 算不出因子分的票（日线不够 / 没入库）排最后，不给假分数；
3. 候选超过 999 只时走 IN 分批，不因 SQL 变量上限报错（与 `/hits` 其它查询同一约定）。
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import patterns as patterns_api
from app.models import Base, PatternHit, StockDaily, TradeCalendar
from app.services import factor_score

DAYS = [date(2026, 9, 1) + timedelta(days=index) for index in range(20)]
DAY = DAYS[-1]
#: 真实存在的形态 key，且不在 `SCORE_EXCLUDED` 里（评分会原样保留）
PATTERN = "volume_surge"


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'hits-order.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([TradeCalendar(trade_date=day) for day in DAYS])
        yield session


def _seed(session, code: str, *, pct: float, amplitude: float, amount_yi: float, score: float) -> None:
    """铺 20 根日线 + 一条当日命中（涨得越多 / 振幅越大 / 越不流动的票因子分越低）。"""
    close = 10.0
    for day in DAYS:
        session.add(
            StockDaily(
                trade_date=day,
                code=code,
                name=code,
                high=close * (1 + amplitude / 2),
                low=close * (1 - amplitude / 2),
                close=close,
                amount=amount_yi * 1e8,
                pct_chg=pct,
            )
        )
        close *= 1 + pct / 100
    session.add(
        PatternHit(
            trade_date=DAY,
            code=code,
            pattern=PATTERN,
            name=code,
            score=score,
            close=close,
            key_levels={},
            detail={},
        )
    )


def _hit(session, code: str, *, score: float) -> None:
    """只有命中、没有日线（因子分算不出来的那种票）。"""
    session.add(
        PatternHit(
            trade_date=DAY,
            code=code,
            pattern=PATTERN,
            name=code,
            score=score,
            close=10.0,
            key_levels={},
            detail={},
        )
    )


def _hits(session, **overrides):
    params = {
        "trade_date": DAY,
        "min_score": 0.0,
        "pattern": None,
        "days": 1,
        "limit": 50,
        "order": "score",
        "phase": None,
    }
    params.update(overrides)
    return patterns_api.hits(db=session, **params)


def test_factor_order_changes_only_the_order(db):
    session = db
    # 000001：涨 + 振幅大 + 成交额小 → 评分最高、因子分最低
    _seed(session, "000001", pct=1.0, amplitude=0.03, amount_yi=1, score=95.0)
    _seed(session, "000002", pct=0.0, amplitude=0.02, amount_yi=10, score=85.0)
    # 000003：跌 + 振幅小 + 成交额大 → 评分最低、因子分最高
    _seed(session, "000003", pct=-1.0, amplitude=0.01, amount_yi=100, score=70.0)
    session.commit()

    by_score = _hits(session)
    assert [row.code for row in by_score.items] == ["000001", "000002", "000003"]
    # 默认不算因子分（字段留在模型上，值为 None）
    assert all(row.factor_score is None for row in by_score.items)

    by_factor = _hits(session, order="factor")
    assert [row.code for row in by_factor.items] == ["000003", "000002", "000001"]
    # 评分字段原样带出（不是把因子分顶到 score 上）
    assert [row.score for row in by_factor.items] == [70.0, 85.0, 95.0]
    assert by_factor.items[0].factor_score > by_factor.items[-1].factor_score
    # 入选集合没变：因子排序只是开关，不是新的筛选
    assert {row.code for row in by_factor.items} == {"000001", "000002", "000003"}
    # 与 `factor_score.scores()` 逐值一致 —— `/hits` 走的就是这一份实现，不许另算一套
    computed = factor_score.scores(session, ["000001", "000002", "000003"], DAY)
    for row in by_factor.items:
        assert row.factor_score == computed[row.code].score


def test_factor_rows_without_history_sort_last(db):
    session = db
    _seed(session, "000001", pct=-1.0, amplitude=0.01, amount_yi=100, score=60.0)
    _seed(session, "000002", pct=1.0, amplitude=0.03, amount_yi=1, score=61.0)
    # 没有日线：评分最高，但因子分算不出来 → 必须排最后、显示 None
    _hit(session, "000009", score=99.0)
    session.commit()

    out = _hits(session, order="factor", min_score=0.0)
    assert [row.code for row in out.items][-1] == "000009"
    assert out.items[-1].factor_score is None
    assert out.items[0].code == "000001"


def test_factor_order_handles_more_than_999_hits(db):
    """候选 1000+ 只时 `scores()` 必须分批（老 SQLite 单条 SQL 变量上限 999）。"""
    session = db
    _seed(session, "000001", pct=-1.0, amplitude=0.01, amount_yi=100, score=60.0)
    for index in range(1000):
        _hit(session, f"{1000 + index:06d}", score=50.0)
    session.commit()

    out = _hits(session, order="factor", limit=1000)
    assert out.total == 1001
    # 池子里只有 000001 算得出因子分 → 池内唯一有效值的分位是 100，排第一
    assert out.items[0].code == "000001"
    assert out.items[0].factor_score == 100.0
    assert out.truncated is True

    # 形态模式（传 pattern）走的是另一条取数分支，同样要能过 999 只
    picked = _hits(session, order="factor", limit=1000, pattern=[PATTERN])
    assert picked.total == 1001
    assert picked.items[0].code == "000001"


def test_raw_factors_do_not_depend_on_pool():
    """原始因子只由这只票自己的日线决定 —— 分位才分池，所以 `/hits` 与首板池同源。

    `/hits` 与 `pools.first_board(order="factor")` 都调 `factor_score.scores`，
    这里锁的是「换一批候选也不会改原始值」这条性质（分数本身不跨池比较）。
    """
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([TradeCalendar(trade_date=day) for day in DAYS])
        _seed(session, "000001", pct=1.0, amplitude=0.03, amount_yi=1, score=95.0)
        _seed(session, "000002", pct=-1.0, amplitude=0.01, amount_yi=100, score=70.0)
        session.commit()

        with_peer = factor_score.scores(session, ["000001", "000002"], DAY)
        alone = factor_score.scores(session, ["000001"], DAY)
        for key in ("rev20", "illiq20", "amp20"):
            assert getattr(with_peer["000001"], key) == getattr(alone["000001"], key)
        # 加权分是池内分位：单票池恒为 100，与两只池里的值不必相等
        assert alone["000001"].score == 100.0
