"""开盘红本地服务（StockDataServer.exe）客户端与补采（2026-11-08）。

钉四件事：
1. 客户端只认 `response.data`：`success=false` / 缺 `data` 一律抛错，不返回空字典；
2. `market_sentiment` 用 `upsert_fill`：**只补空值**，不覆盖 iFinD 已采到的数；
3. `payload.data.date` 与目标日对不上就跳过该来源（上游「问今天给昨天」）；
4. 一行可用字段都没有时不建行，`--dry-run` 不写库。
"""

from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import pytest

from app.db import upsert
from app.jobs import collect_kph
from app.models import Base, MarketSentiment
from app.sources.kph_local import KphLocalError, KphLocalSource
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

DAY = date(2026, 11, 6)
PREV = date(2026, 11, 5)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        kph_local_base="http://127.0.0.1:5000",
        kph_local_timeout=5.0,
        kph_rate_limit=1000.0,
        http_retries=1,
        http_backoff=0.0,
    )


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


@pytest.fixture()
def fake_get(monkeypatch):
    """替换 `requests.get`，把返回的 payload 队列化。"""

    calls: list[dict] = []
    payloads: list[dict] = []

    def _get(url, params=None, timeout=None, **kwargs):
        calls.append({"url": url, "params": params, "timeout": timeout})
        return _FakeResponse(payloads.pop(0))

    monkeypatch.setattr("app.sources.kph_local.requests.get", _get)
    return SimpleNamespace(calls=calls, payloads=payloads)


# ---------------------------------------------------------------- 客户端


def test_market_sentiment_unwraps_response_data(fake_get):
    fake_get.payloads.append(
        {
            "success": True,
            "response": {"cmd_id": "08 3F", "data": {"date": "2026-11-06", "zt_num": 52}},
        }
    )
    data = KphLocalSource(_settings()).market_sentiment()
    assert data["zt_num"] == 52
    # 走的是一次性版，且带 type=0（订阅版会一直占着服务端订阅名额）
    assert fake_get.calls[0]["url"].endswith("/api/get_market_sentiment_once")
    assert fake_get.calls[0]["params"] == {"type": 0}


def test_failure_flag_raises(fake_get):
    fake_get.payloads.append({"success": False, "message": "缺少参数 stock_id"})
    with pytest.raises(KphLocalError, match="缺少参数"):
        KphLocalSource(_settings()).zd_stat()


def test_missing_data_raises(fake_get):
    fake_get.payloads.append({"success": True, "response": {"cmd_id": "08 40"}})
    with pytest.raises(KphLocalError, match="response.data"):
        KphLocalSource(_settings()).zd_stat()


def test_status_has_no_envelope(fake_get):
    fake_get.payloads.append({"connected": True, "initialized": True, "timestamp": "x"})
    assert KphLocalSource(_settings()).status()["connected"] is True


# ---------------------------------------------------------------- 补采


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'kph.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        @contextmanager
        def _scope():
            yield session
            session.commit()

        monkeypatch.setattr(collect_kph, "session_scope", _scope)
        yield session


def _collector(monkeypatch, *, sentiment: dict, zd: dict):
    collector = collect_kph.KphCollector(_settings())
    monkeypatch.setattr(collector.source, "market_sentiment", lambda: sentiment)
    monkeypatch.setattr(collector.source, "zd_stat", lambda: zd)
    return collector


def test_collect_fills_missing_values_only(db, monkeypatch):
    # iFinD 已经写过 limit_up_count=99（模拟），其余为空
    upsert(db, MarketSentiment, [{"trade_date": DAY, "limit_up_count": 99}])
    db.commit()

    collector = _collector(
        monkeypatch,
        sentiment={"date": DAY.isoformat(), "zt_num": 52, "continue_high": 7},
        zd={"date": DAY.isoformat(), "zt": 56, "dt": 12, "rise": 2567, "down": 2823},
    )
    summary = collector.collect(DAY)
    assert summary["written"] == 1

    row = db.scalar(select(MarketSentiment).where(MarketSentiment.trade_date == DAY))
    assert row is not None
    assert row.limit_up_count == 99  # 已有值不被覆盖
    assert row.max_consecutive == 7
    assert row.limit_down_count == 12
    assert row.up_count == 2567
    assert row.down_count == 2823


def test_collect_skips_source_with_other_date(db, monkeypatch):
    collector = _collector(
        monkeypatch,
        # 情绪接口给的是**昨天**的 → 整段跳过
        sentiment={"date": PREV.isoformat(), "zt_num": 52, "continue_high": 7},
        zd={"date": DAY.isoformat(), "zt": 56, "dt": 12, "rise": 2567, "down": 2823},
    )
    summary = collector.collect(DAY)
    assert "日期对不上" in summary["sentiment"]

    row = db.scalar(select(MarketSentiment).where(MarketSentiment.trade_date == DAY))
    assert row is not None
    assert row.limit_up_count == 56  # 情绪被跳过 → 用 zd_stat.zt 兜底
    assert row.max_consecutive is None  # 只在情绪里有，跳过就不写


def test_both_sources_unusable_raises(db, monkeypatch):
    collector = _collector(
        monkeypatch,
        sentiment={"date": PREV.isoformat(), "zt_num": 52},
        zd={"date": PREV.isoformat(), "rise": 1},
    )
    with pytest.raises(KphLocalError):
        collector.collect(DAY)


def test_no_fields_does_not_create_row(db, monkeypatch):
    collector = _collector(
        monkeypatch,
        sentiment={"date": DAY.isoformat()},
        zd={"date": DAY.isoformat()},
    )
    summary = collector.collect(DAY)
    assert summary["written"] == 0
    assert "没有任何可用字段" in summary["reason"]
    assert db.scalar(select(MarketSentiment)) is None


def test_dry_run_does_not_write(db, monkeypatch):
    collector = _collector(
        monkeypatch,
        sentiment={"date": DAY.isoformat(), "zt_num": 52},
        zd={"date": DAY.isoformat(), "rise": 2567},
    )
    summary = collector.collect(DAY, dry_run=True)
    assert summary["written"] == 0
    assert "dry-run" in summary["reason"]
    assert db.scalar(select(MarketSentiment)) is None


def test_cli_prints_summary_containing_a_date(monkeypatch, capsys):
    """摘要里带 `date` 对象，命令行必须在落库之后也能正常打印（json 需要 default=str）。"""

    class _FakeCollector:
        def __init__(self, settings=None) -> None:
            pass

        def collect(self, trade_date, *, dry_run=False):
            return {
                "task": "kph_sentiment",
                "trade_date": trade_date.isoformat(),
                "written": 1,
                "row": {"trade_date": trade_date, "up_count": 2567},
            }

    monkeypatch.setattr(collect_kph, "KphCollector", _FakeCollector)
    assert collect_kph.main(["--date", DAY.isoformat()]) == 0
    out = capsys.readouterr().out
    assert DAY.isoformat() in out
    assert '"written": 1' in out
