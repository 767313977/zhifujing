"""2026-10-28 复查（第六轮之后）修掉的三处，逐条钉住。

这三条都是「看起来修了、其实没修到」的那类：iFinD 的重试包错层、前端迟到 401
绕过了守卫、筹码缺涨跌幅时把真实跌幅当成除权。每条都用**会失败的反例**锁住。
"""

from datetime import date

import pytest

from app.config import Settings
from app.services import chip
from app.sources import ifind
from app.sources.kaipanhong import (
    MEMBER_AMOUNT,
    MEMBER_CODE,
    MEMBER_FLOAT_MV,
    MEMBER_NAME,
    MEMBER_PCT_CHG,
    MEMBER_PRICE,
    MEMBER_TAGS,
    MEMBER_TURNOVER,
    KaipanhongSource,
)


# ---------------------------------------------------------------- iFinD：解析错必须在重试范围内


class _FakeIfind(ifind.IfindClient):
    """只替换 `call`：返回一个假响应，计调用次数。"""

    def __init__(self) -> None:
        super().__init__(Settings(ifind_auth_token="test", http_backoff=0))
        self.calls = 0

    def call(self, server: str, tool: str, params: dict) -> dict:  # type: ignore[override]
        self.calls += 1
        return {"fake": self.calls}


def test_call_extract_retries_when_inner_parse_fails(monkeypatch):
    """第一次 `_extract` 抛解析错、第二次正常 → 必须重试并成功。

    这条正是原实现的漏洞：`_extract` 在 `retry_call` **外面**，
    `retry_on=IfindParseError` 永远命中不到，一次都不会重试。
    """
    client = _FakeIfind()
    seen: list[int] = []

    def fake_extract(response: dict) -> tuple[dict, dict]:
        seen.append(response["fake"])
        if len(seen) == 1:
            raise ifind.IfindParseError("content 被截断")
        return {}, {"ok": True}

    monkeypatch.setattr(ifind, "_extract", fake_extract)

    assert client._call_extract("stock", "get_stock_performance", {}) == {"ok": True}
    assert len(seen) == 2, "解析失败必须触发第二次请求"
    assert client.calls == 2


def test_call_extract_does_not_retry_other_errors(monkeypatch):
    """普通 `IfindError`（参数错那类）不重试 —— 只兜解析错。"""
    client = _FakeIfind()

    def fake_extract(response: dict) -> tuple[dict, dict]:
        raise ifind.IfindError("参数错")

    monkeypatch.setattr(ifind, "_extract", fake_extract)

    with pytest.raises(ifind.IfindError):
        client._call_extract("stock", "x", {})
    assert client.calls == 1


# ---------------------------------------------------------------- 筹码：缺涨跌幅时不能把跌幅当除权


def _decline_rows(with_pct: bool) -> list[dict]:
    """真实下跌 20 → 10（30 个交易日，每天约 -2.5%，**没有除权**）。"""
    rows: list[dict] = []
    price = 20.0
    start = date(2026, 3, 2)
    for index in range(30):
        rows.append(
            {
                "trade_date": date.fromordinal(start.toordinal() + index),
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "turnover": 1.0,
                "pct_chg": ((price * 0.975) / price - 1) * 100 if with_pct else None,
            }
        )
        price *= 0.975
    return rows


def test_chip_missing_pct_does_not_fake_a_split():
    """缺 `pct_chg` 时不做复权换算：分布必须仍横跨 10~20，而不是被压成一根针。"""
    result = chip.distribute(_decline_rows(with_pct=False))
    assert result.avg_cost > 15, f"整段被归一到最新价（avg={result.avg_cost:.2f}）"
    assert result.cost_90[1] - result.cost_90[0] > 5, "分布宽度被压扁了"
    assert result.profit_ratio < 0.2, "绝大多数筹码在亏损位，收益率不该是 0.5"


def test_chip_with_pct_keeps_distribution_shape():
    """有 `pct_chg` 时按前复权递推：真跌就是真跌，同样不该被压扁。"""
    result = chip.distribute(_decline_rows(with_pct=True))
    assert result.cost_90[1] - result.cost_90[0] > 5


def test_chip_adjusts_split_when_pct_present():
    """10 转 10：除权前 20 元的筹码要被换算到 10 元附近。"""
    rows: list[dict] = []
    start = date(2026, 1, 5)
    for index in range(10):
        rows.append(
            {
                "trade_date": date.fromordinal(start.toordinal() + index),
                "high": 20.2,
                "low": 19.8,
                "close": 20.0,
                "turnover": 1.0,
                "pct_chg": 0.0,
            }
        )
    for index in range(10, 30):
        rows.append(
            {
                "trade_date": date.fromordinal(start.toordinal() + index),
                "high": 10.1,
                "low": 9.9,
                "close": 10.0,
                "turnover": 1.0,
                "pct_chg": 0.0,
            }
        )
    result = chip.distribute(rows)
    assert result.avg_cost == pytest.approx(10.0, abs=0.05)
    assert result.cost_90[1] == pytest.approx(10.0, abs=0.2)


# ---------------------------------------------------------------- 开盘红：名单残缺要能被调用方看见


def _member_row(code: str) -> list:
    row = [None] * 11
    row[MEMBER_CODE] = code
    row[MEMBER_NAME] = f"票{code}"
    row[MEMBER_TAGS] = ""
    row[MEMBER_PRICE] = 10.0
    row[MEMBER_PCT_CHG] = 1.0
    row[MEMBER_AMOUNT] = 1e8
    row[MEMBER_TURNOVER] = 3.0
    row[MEMBER_FLOAT_MV] = 1e9
    return row


def _source_with_pages(pages: dict[int, dict]) -> KaipanhongSource:
    source = KaipanhongSource(Settings(http_backoff=0))
    page_size = 1000

    def fake_post(trade_date: date, params: dict, description: str) -> dict:
        return pages[int(params["Index"]) // page_size]

    source._post = fake_post  # type: ignore[method-assign]
    return source


def test_board_members_reports_complete_list():
    source = _source_with_pages({0: {"Count": "2", "list": [_member_row("600000"), _member_row("600001")]}})
    stats: dict = {}
    rows = source.board_members("801001", date(2026, 10, 28), stats=stats)
    assert len(rows) == 2
    assert stats == {"reported": 2, "unique": 2, "short": False}


def test_board_members_flags_short_list():
    """接口说 3 只、短页只给 2 只、再翻也是同一批 → `short=True`（不再静默）。"""
    page = {"Count": "3", "list": [_member_row("600000"), _member_row("600001")]}
    source = _source_with_pages({0: page, 1: page})
    stats: dict = {}
    rows = source.board_members("801001", date(2026, 10, 28), stats=stats)
    assert len(rows) == 2
    assert stats["reported"] == 3
    assert stats["unique"] == 2
    assert stats["short"] is True
