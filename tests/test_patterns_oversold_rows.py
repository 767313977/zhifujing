"""「超跌全市场」补入的行（api/patterns._oversold_only_rows）要带板块与行业。

背景（2026-10-28）：这些行原先把 `sectors` 写成 `[]`、`industry` 写成 `None`，
理由是「没涨停过就没有板块值」。线上实测 278 只超跌票里 **278 只都查得到行业**、
248 只查得到板块，于是页面上「板块 / 行业 / 命中形态」三列全空 —— 那看起来像数据
没加载出来，而不像「这些票没有形态」。

这里钉住三件事：
1. 行业按 `stock_basic.industry` 给；
2. 板块取该股**最近一天**的 `stock_concept`（不是当天 —— 涨停才写这张表）；
3. `patterns` 必须仍是空列表：这些票今天一个形态都没中，这是它们被补进来的原因。
"""

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.patterns import _oversold_only_rows
from app.models import StockBasic, StockConcept, StockDaily


def _session(tmp_path) -> Session:
    """只建用到的三张表 —— 不碰 settings 里那个真库。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'oversold_rows.db'}")
    StockDaily.__table__.create(engine)
    StockBasic.__table__.create(engine)
    StockConcept.__table__.create(engine)
    return Session(engine)


def _bar(db: Session, day: date, code: str, name: str, close: float) -> None:
    db.add(
        StockDaily(
            trade_date=day,
            code=code,
            name=name,
            close=close,
            pct_chg=-3.0,
            amount=1e8,
        )
    )


def test_oversold_rows_carry_sector_and_industry(tmp_path):
    target = date(2026, 10, 28)
    with _session(tmp_path) as db:
        _bar(db, target, "600000", "浦发银行", 9.5)
        _bar(db, target, "000001", "平安银行", 10.2)
        db.add_all(
            [
                StockBasic(code="600000", name="浦发银行", industry="银行-银行-股份制银行"),
                StockBasic(code="000001", name="平安银行", industry="银行-银行-股份制银行"),
                # 板块只有「涨停那天」才写，且同一天可能有多条
                StockConcept(trade_date=date(2026, 10, 20), code="600000", concept="银行"),
                StockConcept(trade_date=date(2026, 10, 24), code="600000", concept="高股息"),
                StockConcept(trade_date=date(2026, 10, 24), code="600000", concept="中特估"),
            ]
        )
        db.commit()

        rows = _oversold_only_rows(
            db, target, ["600000", "000001"], {"600000": -21.3, "000001": -16.8}
        )

    by_code = {row.code: row for row in rows}
    # 板块取**最近一天**那一组，不把更早的「银行」也带出来
    assert by_code["600000"].sectors == ["中特估", "高股息"]
    assert by_code["000001"].sectors == []
    # 行业两只都有
    assert by_code["600000"].industry == "银行-银行-股份制银行"
    assert by_code["000001"].industry == "银行-银行-股份制银行"
    # 没有形态这件事不能变：`patterns` 为空、评分为 0，排序按乖离从深到浅
    assert all(row.patterns == [] and row.score == 0.0 for row in rows)
    assert [row.code for row in rows] == ["600000", "000001"]
