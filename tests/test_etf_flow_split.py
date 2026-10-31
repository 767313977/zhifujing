"""ETF 净申赎：份额折算日不能算成假流入（2026-10-31 加）。

折算日份额翻倍、价格减半，`(今日份额 − 基准份额) × 今日收盘` 会凭空多出一笔
与原规模等额的「净申购」。判据用 `etf_bar` 的交易所前收（`prev_close` 已调整）：
`|前一日 close ÷ 当日 prev_close − 1| > 20%` 即为折算日，那天这只整只跳过。
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, EtfBar, EtfShare
from app.services import etf_flow

DAY = date(2026, 10, 30)
PREV = date(2026, 10, 29)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'flow.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _share(session, day, code, shares, close):
    session.add(EtfShare(trade_date=day, code=code, shares=shares, close=close))


def _bar(session, day, code, close, prev_close):
    session.add(
        EtfBar(
            trade_date=day,
            code=code,
            close=close,
            prev_close=prev_close,
            pct_chg=round((close / prev_close - 1) * 100, 4),
        )
    )


def test_split_day_is_excluded_from_net(db):
    # 正常那只：份额 100 → 110，收盘 2.0 → 净申购 20
    _share(db, PREV, "510300", 100.0, 2.0)
    _share(db, DAY, "510300", 110.0, 2.0)
    _bar(db, PREV, "510300", 2.0, 2.0)
    _bar(db, DAY, "510300", 2.0, 2.0)
    # 折算那只：1 拆 2（份额 100 → 200、价格 2.0 → 1.0），不剔的话会算出 100 的假流入
    _share(db, PREV, "159583", 100.0, 2.0)
    _share(db, DAY, "159583", 200.0, 1.0)
    _bar(db, PREV, "159583", 2.0, 2.0)
    _bar(db, DAY, "159583", 1.0, 1.0)  # 前收已被交易所调整成 1.0
    db.commit()

    splits = etf_flow.split_codes(db, [DAY])
    nets = etf_flow.day_nets(db, [(DAY, PREV)], money=set())

    assert splits == {DAY: {"159583"}}
    assert nets[DAY] == pytest.approx(20.0)


def test_no_bar_data_keeps_old_behaviour(db):
    """`etf_bar` 还没回补时（空表）不能改变原有口径。"""
    _share(db, PREV, "159583", 100.0, 2.0)
    _share(db, DAY, "159583", 200.0, 1.0)
    db.commit()

    assert etf_flow.split_codes(db, [DAY]) == {}
    assert etf_flow.day_nets(db, [(DAY, PREV)], money=set())[DAY] == pytest.approx(100.0)


def test_normal_dividend_is_not_treated_as_split(db):
    """除息（3% 左右）不能误判成折算。"""
    _share(db, PREV, "510880", 100.0, 2.0)
    _share(db, DAY, "510880", 100.0, 1.94)
    _bar(db, PREV, "510880", 2.0, 2.0)
    _bar(db, DAY, "510880", 1.94, 1.94)  # 前收按除息调整，只差 3%
    db.commit()

    assert etf_flow.split_codes(db, [DAY]) == {}
