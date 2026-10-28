"""异动页「点板块看个股」的下钻（api/anomaly_map.build_industry_stocks）。

要钉住的三件事：

1. **按末段名匹配**：`stock_basic.industry` 是三级路径且末段带级别记号
   （`电子-半导体-集成电路Ⅲ`），点「集成电路」必须能命中它 —— 与异动图谱分组
   走同一个 `_sector_of`，两处口径不能走岔。
2. **看的是全部个股**，不是只有异动的那几只：板块里今天没涨停、没大涨的票也要在。
3. **不拿旧价冒充当天**：`stock_daily` 那天没有行的票（停牌）不进列表。
"""

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.anomaly_map import build_industry_stocks
from app.models import StockBasic, StockDaily, StockUniverse


def _session(tmp_path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'industry.db'}")
    StockBasic.__table__.create(engine)
    StockDaily.__table__.create(engine)
    StockUniverse.__table__.create(engine)
    return Session(engine)


def _bar(db: Session, day: date, code: str, pct: float | None, close: float) -> None:
    db.add(
        StockDaily(
            trade_date=day,
            code=code,
            name=code,
            close=close,
            pct_chg=pct,
            amount=1e8,
        )
    )


def test_matches_industry_last_segment_with_level_mark(tmp_path):
    day = date(2026, 10, 28)
    with _session(tmp_path) as db:
        db.add_all(
            [
                # 末段带级别记号，点的名字里没有 —— 必须命中
                StockBasic(code="688001", name="华兴源创", industry="电子-半导体-集成电路Ⅲ"),
                StockBasic(code="688002", name="睿创微纳", industry="电子-半导体-集成电路Ⅲ"),
                # 别的行业不该混进来
                StockBasic(code="600000", name="浦发银行", industry="银行-银行-股份制银行"),
                StockUniverse(code="688001", name="华兴源创", total_mv=2.5e10),
            ]
        )
        _bar(db, day, "688001", 3.2, 40.0)
        _bar(db, day, "688002", -1.5, 50.0)
        _bar(db, day, "600000", 0.4, 9.5)
        db.commit()

        result = build_industry_stocks(db, "集成电路", day)

    assert result["sector"] == "集成电路"
    assert result["trade_date"] == day
    # 两只都在，且按涨跌幅降序
    assert [item["code"] for item in result["items"]] == ["688001", "688002"]
    assert result["count"] == 2
    # 市值换算成亿元（与异动表 cap 同口径）
    assert result["items"][0]["cap"] == 250.0
    assert result["items"][1]["cap"] is None


def test_skips_stock_without_bar_on_that_day(tmp_path):
    """停牌票当天没有日线，不能拿别的日期的收盘价顶上。"""
    day = date(2026, 10, 28)
    with _session(tmp_path) as db:
        db.add_all(
            [
                StockBasic(code="688001", name="华兴源创", industry="电子-半导体-集成电路"),
                StockBasic(code="688002", name="睿创微纳", industry="电子-半导体-集成电路"),
            ]
        )
        _bar(db, day, "688001", 3.2, 40.0)
        _bar(db, date(2026, 10, 27), "688002", 1.0, 50.0)
        db.commit()

        result = build_industry_stocks(db, "集成电路", day)

    assert [item["code"] for item in result["items"]] == ["688001"]


def test_unknown_sector_returns_empty(tmp_path):
    with _session(tmp_path) as db:
        db.add(StockBasic(code="688001", name="华兴源创", industry="电子-半导体-集成电路"))
        _bar(db, date(2026, 10, 28), "688001", 1.0, 40.0)
        db.commit()

        result = build_industry_stocks(db, "不存在的板块", date(2026, 10, 28))

    assert result["count"] == 0
    assert result["items"] == []
