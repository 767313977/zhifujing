"""前瞻研究库的表结构与字段映射测试（离线，全部用临时文件）。

覆盖：ORM 列兼容、字段转换、单位与缺失值、代码/市场识别、只读连接、
以及「历史查询与回测函数能直接跑在新库上」这条硬要求。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from app.models import StockDaily  # noqa: E402
from app.services.research_backtest import load_matrix  # noqa: E402
from scripts import forward_store as store  # noqa: E402

INGESTED = "2026-11-12T15:40:00+08:00"


def _raw(**overrides) -> dict:
    base = {
        "证券代码": "600000.SH",
        "交易日期": "2026-11-11",
        "证券简称": "浦发银行",
        "开盘价": "10.00",
        "最高价": "10.50",
        "最低价": "9.90",
        "收盘价": "10.20",
        "涨跌幅": "2.35",
        "成交量": "1234567",
        "成交额": "12345678.9",
        "换手率": "0.87",
    }
    base.update(overrides)
    return base


# ---- 表结构 -----------------------------------------------------------------


def test_schema_covers_every_orm_column(tmp_path):
    engine = store.connect(tmp_path / "f.db")
    store.create_schema(engine)
    with engine.begin() as conn:
        columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(stock_daily)")
        }
    engine.dispose()
    for column in StockDaily.__table__.columns.keys():
        assert column in columns, column
    # 前瞻额外列
    for extra in ("board", "source", "pct_source", "raw_code", "data_version", "ingested_at"):
        assert extra in columns, extra


def test_schema_is_idempotent(tmp_path):
    engine = store.connect(tmp_path / "f.db")
    store.create_schema(engine)
    store.create_schema(engine)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(StockDaily)) == 0
    engine.dispose()


# ---- 字段转换 ---------------------------------------------------------------


def test_normalize_row_converts_code_and_units():
    row = store.normalize_row(_raw(), source="ifind", ingested_at=INGESTED)
    assert row["code"] == "600000"
    assert row["raw_code"] == "600000.SH"
    assert row["trade_date"] == "2026-11-11"
    assert row["board"] == "沪主板"
    # 百分数保持百分数，不除 100
    assert row["pct_chg"] == pytest.approx(2.35)
    # 成交额单位元，不做任何换算
    assert row["amount"] == pytest.approx(12345678.9)
    assert row["pct_source"] == "official"
    assert store.validate_row(row) == []


@pytest.mark.parametrize(
    ("code", "board"),
    [
        ("600000.SH", "沪主板"),
        ("688001.SH", "科创板"),
        ("000001.SZ", "深主板"),
        ("300001.SZ", "创业板"),
        ("830001.BJ", "北交所"),
        ("920001.BJ", "北交所"),
    ],
)
def test_normalize_board_by_code(code, board):
    row = store.normalize_row(_raw(证券代码=code), source="ifind", ingested_at=INGESTED)
    assert row["board"] == board


def test_normalize_rejects_unrecognized_code():
    with pytest.raises(ValueError):
        store.normalize_row(_raw(证券代码="ABCDEF"), source="ifind", ingested_at=INGESTED)


def test_normalize_rejects_bad_date():
    with pytest.raises(ValueError):
        store.normalize_row(_raw(交易日期="2026/13/45"), source="ifind", ingested_at=INGESTED)


def test_missing_amount_is_an_error_not_an_estimate():
    row = store.normalize_row(_raw(成交额="-"), source="ifind", ingested_at=INGESTED)
    problems = store.validate_row(row)
    assert any("amount" in item for item in problems)
    assert row["amount"] is None  # 绝不拿 volume×close 兜底


def test_missing_pct_is_an_error():
    row = store.normalize_row(_raw(涨跌幅=""), source="ifind", ingested_at=INGESTED)
    assert any("pct_chg" in item for item in store.validate_row(row))


def test_negative_price_is_an_error():
    row = store.normalize_row(_raw(收盘价="-1"), source="ifind", ingested_at=INGESTED)
    assert any("close" in item for item in store.validate_row(row))


def test_price_order_is_only_a_warning():
    row = store.normalize_row(
        _raw(最高价="9.00", 最低价="10.00"), source="ifind", ingested_at=INGESTED
    )
    assert store.validate_row(row) == []
    assert store.price_order_warnings(row)


def test_unknown_board_is_rejected():
    row = store.normalize_row(_raw(证券代码="900001.SH"), source="ifind", ingested_at=INGESTED)
    assert any("沪深京" in item for item in store.validate_row(row))


# ---- 与既有 ORM / 回测函数的兼容 --------------------------------------------


def _insert(engine, rows: list[dict]) -> None:
    with engine.begin() as conn:
        for row in rows:
            conn.exec_driver_sql(
                """
                INSERT INTO stock_daily
                (trade_date, code, name, open, high, low, close, pct_chg,
                 volume, amount, turnover, board, source, pct_source,
                 raw_code, data_version, ingested_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["trade_date"], row["code"], row["name"], row["open"],
                    row["high"], row["low"], row["close"], row["pct_chg"],
                    row["volume"], row["amount"], row["turnover"], row["board"],
                    row["source"], row["pct_source"], row["raw_code"],
                    row["data_version"], row["ingested_at"],
                ),
            )


def test_orm_and_backtest_read_new_schema(tmp_path):
    path = tmp_path / "f.db"
    engine = store.connect(path)
    store.create_schema(engine)
    rows = []
    for index, code in enumerate(("600000.SH", "000001.SZ", "830001.BJ")):
        rows.append(
            store.normalize_row(
                _raw(证券代码=code, 收盘价=str(10.0 + index)),
                source="ifind",
                ingested_at=INGESTED,
            )
        )
    _insert(engine, rows)
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO trade_calendar VALUES ('2026-11-11')")

    with Session(engine) as session:
        # 整表 ORM 查询（含 turnover）不能因缺列报错
        assert session.scalar(select(func.count()).select_from(StockDaily)) == 3
        matrix = load_matrix(session, date(2026, 11, 11), date(2026, 11, 11))
    engine.dispose()

    assert matrix.codes == ["000001", "600000", "830001"]
    assert matrix.calendar == [date(2026, 11, 11)]
    pos = matrix.date_pos[date(2026, 11, 11)]
    column = matrix.code_pos["830001"]
    assert matrix.close[pos, column] == pytest.approx(12.0)
    assert matrix.pct[pos, column] == pytest.approx(2.35)
    assert matrix.amount[pos, column] == pytest.approx(12345678.9)


def test_readonly_connection_rejects_writes(tmp_path):
    path = tmp_path / "f.db"
    engine = store.connect(path)
    store.create_schema(engine)
    engine.dispose()

    readonly = store.connect_readonly(path)
    assert "mode=ro" in str(readonly.url)
    with Session(readonly) as session:
        session.execute(select(func.count()).select_from(StockDaily)).all()
        with pytest.raises(Exception):
            session.execute(
                StockDaily.__table__.insert().values(
                    trade_date="2026-11-11", code="600000"
                )
            )
            session.commit()
    readonly.dispose()
