"""ETF 日线采集（ETF-1）的四条口径。

1. 深市 `1` 开头的债券 / 资产支持证券不能当 ETF；
2. 覆盖率不足（低于上一份快照 80%）报错、当天**一行都不写**；
3. 除息 / 折算日 `pct_chg` 用交易所前收算，不出现假跌；
4. `norm_name` 能把不同管理人 / 带 XD 前缀的同指数 ETF 归成一个名字。
"""

from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.jobs import collect_etf_bar as job
from app.models import Base, EtfBar
from app.services.etf_category import norm_name
from app.sources.tdx import TdxError

DAY = date(2026, 10, 30)
PREV = date(2026, 10, 29)


@pytest.fixture()
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'etf-bar.db'}")
    Base.metadata.create_all(engine)
    return engine


@contextmanager
def _scope(engine):
    with Session(engine) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def _patch_scope(monkeypatch, engine):
    monkeypatch.setattr(job, "session_scope", lambda: _scope(engine))


def _row(code, name, market="sh", close=1.0, prev_close=1.0):
    return {
        "date": DAY.isoformat(),
        "market": market,
        "code": code,
        "name": name,
        "prev_close": prev_close,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1000,
        "amount": 1e7,
    }


def test_deep_bonds_are_not_etf():
    assert job.is_etf_row(_row("123456", "某某转债", market="sz"), set()) is False
    assert job.is_etf_row(_row("143435", "安康5B", market="sz"), set()) is False
    assert job.is_etf_row(_row("159915", "创业板ETF", market="sz"), set()) is True
    # 名称被截断、不含 ETF，但在 etf_share 历史里出现过 → 补进来
    assert job.is_etf_row(_row("510300", "沪深300", market="sh"), {"510300"}) is True
    # 沪市非 5 开头、深市非 1 开头一律不收
    assert job.is_etf_row(_row("600519", "贵州茅台ETF", market="sh"), set()) is False


def test_coverage_gate_refuses_to_write(engine, monkeypatch):
    _patch_scope(monkeypatch, engine)
    with Session(engine) as setup:
        setup.add_all(
            [
                EtfBar(trade_date=PREV, code=f"5100{index:02d}", close=1.0)
                for index in range(100)
            ]
        )
        setup.commit()
    monkeypatch.setattr(
        job,
        "daily_package",
        lambda day: [_row(f"5100{index:02d}", "某某ETF") for index in range(50)],
    )
    monkeypatch.setattr(job, "_share_codes", lambda: set())

    with pytest.raises(TdxError):
        job.refresh(DAY)

    with Session(engine) as check:
        assert check.scalar(select(func.count()).select_from(EtfBar).where(EtfBar.trade_date == DAY)) == 0


def test_dividend_day_uses_adjusted_prev_close(engine, monkeypatch):
    _patch_scope(monkeypatch, engine)
    monkeypatch.setattr(job, "_share_codes", lambda: set())
    # 510100 在 2026-10-30 那天除息：前一日收 3.117，但包内 prev_close 是 3.020
    monkeypatch.setattr(
        job,
        "daily_package",
        lambda day: [_row("510100", "XD上证50ETF易方达", close=3.033, prev_close=3.020)],
    )

    bars, names = job.fetch_bars(DAY)

    assert bars[0]["pct_chg"] == pytest.approx(0.43, abs=0.01)
    assert names["510100"] == "XD上证50ETF易方达"


def test_norm_name_merges_same_index():
    assert norm_name("华夏沪深300ETF") == norm_name("沪深300ETF易方达") == "沪深300"
    assert norm_name("XD红利ETF汇添富") == norm_name("红利ETF华泰柏瑞") == "红利"
    assert norm_name("XD红利国企ETF") == "红利国企"
    assert norm_name(None) == ""
