"""个股 AI 研判：事实拼装、按天缓存、配置缺失 / 无日线三种失败。

不真的联网：`llm.chat` 一律被替换成假函数。要钉住的是三件事 ——
1. 喂给模型的**只有库里的数字**（名称、行情、均线、形态、资金、涨停痕迹都在）；
2. 同一只票同一天只调一次模型（第二次读缓存）；
3. `LLM_*` 没配齐时抛 `AiNotConfigured`（接口层翻成 503），而不是静默返回空话。
"""

from contextlib import contextmanager
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    Base,
    LimitPool,
    PatternHit,
    StockBasic,
    StockDaily,
    StockDde,
)
from app.services import llm, stock_ai

CODE = "688410"
DAY = date(2026, 10, 21)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(StockBasic(code=CODE, name="示例股份", industry="医疗器械-医疗设备"))
        # 30 根日线：够 `load_phase` 的 25 根门槛，也让 MA20 / 近 20 日涨幅都算得出来
        for offset in range(30):
            session.add(
                StockDaily(
                    trade_date=DAY - timedelta(days=offset),
                    code=CODE,
                    name="示例股份",
                    open=10.0 + (30 - offset) * 0.1,
                    high=10.5 + (30 - offset) * 0.1,
                    low=9.5 + (30 - offset) * 0.1,
                    close=10.2 + (30 - offset) * 0.1,
                    pct_chg=1.0,
                    volume=1e6,
                    amount=2e8,
                    turnover=3.0,
                )
            )
        session.add(
            PatternHit(
                trade_date=DAY,
                code=CODE,
                pattern="ma_bull",
                name="示例股份",
                score=80.0,
                close=13.0,
                pct_chg=1.0,
                amount=2e8,
            )
        )
        session.add_all(
            [
                StockDde(trade_date=DAY, code=CODE, net_inflow=1.2e8, dde=3.4e8),
                StockDde(trade_date=DAY - timedelta(days=1), code=CODE, net_inflow=-2e7),
                LimitPool(
                    trade_date=DAY - timedelta(days=3),
                    code=CODE,
                    pool_type="up",
                    name="示例股份",
                ),
            ]
        )
        session.commit()
        yield session


@contextmanager
def _scope(session: Session):
    yield session


def _settings() -> Settings:
    return Settings(
        llm_base_url="http://localhost:9/v1",
        llm_api_key="test-key",
        llm_model="test-model",
    )


def test_facts_only_use_stored_numbers(session):
    facts = stock_ai._facts(session, CODE, DAY)
    assert "标的：示例股份（688410）" in facts
    assert "医疗器械-医疗设备" in facts
    assert "均线：MA5" in facts
    assert "形态命中：均线多头排列" in facts
    assert "主力净流入" in facts
    assert "涨停 1 次" in facts


def test_review_caches_per_day(session, monkeypatch):
    monkeypatch.setattr(stock_ai, "_CACHE", {})
    monkeypatch.setattr(stock_ai, "_INFLIGHT", {})
    monkeypatch.setattr(stock_ai, "session_scope", lambda: _scope(session))
    calls: list[str] = []

    def fake_chat(settings, system, user, **kwargs):
        calls.append(user)
        return "第一段\n第二段"

    monkeypatch.setattr(llm, "chat", fake_chat)

    first = stock_ai.review(CODE, _settings())
    second = stock_ai.review(CODE, _settings())

    assert first.text == "第一段\n第二段"
    assert first.as_of == DAY
    assert second == first
    assert len(calls) == 1
    # 命中缓存时接口层不再占冷却（`cached_review` 只读缓存、不调模型）
    assert stock_ai.cached_review(CODE) == first


def test_review_requires_config(session, monkeypatch):
    monkeypatch.setattr(stock_ai, "session_scope", lambda: _scope(session))
    with pytest.raises(stock_ai.AiNotConfigured):
        stock_ai.review(CODE, Settings())


def test_review_without_bars_fails(session, monkeypatch):
    monkeypatch.setattr(stock_ai, "_CACHE", {})
    monkeypatch.setattr(stock_ai, "session_scope", lambda: _scope(session))
    with pytest.raises(stock_ai.AiFailed):
        stock_ai.review("600000", _settings())
