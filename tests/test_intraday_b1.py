"""知行B1 盘中预览：纯函数部分（不碰网络与数据库）。"""

from datetime import date, timedelta

from app.jobs.intraday_b1 import (
    B1_KEYS,
    MAX_PER_PATTERN,
    _as_record,
    _format,
    _scan,
    build_card,
)

DAY = date(2026, 10, 19)


def _snapshot_row(*, close: float = 10.0, pct: float = 1.0, name: str = "测试股") -> dict:
    return {
        "trade_date": DAY,
        "code": "600000",
        "name": name,
        "open": 9.9,
        "high": 10.1,
        "low": 9.8,
        "close": close,
        "volume": 1e6,
        "amount": 1e7,
        "pct_chg": pct,
    }


def _history(count: int = 30) -> list[dict]:
    start = date(2026, 9, 1)
    return [
        {
            "date": start + timedelta(days=index),
            "name": "测试股",
            "open": 10.0,
            "high": 10.0,
            "low": 10.0,
            "close": 10.0,
            "volume": 1e6,
            "amount": 1e7,
            "pct_chg": 0.0,
        }
        for index in range(count)
    ]


def test_as_record_uses_date_key():
    record = _as_record(_snapshot_row())
    assert record["date"] == DAY
    assert record["close"] == 10.0
    assert "trade_date" not in record


def test_format_without_hits():
    hits = {key: [] for key in B1_KEYS}
    text = _format(DAY, "14:51", hits, 14)
    assert "收盘复盘" in text  # 飞书群机器人的关键词
    assert "暂无命中" in text
    assert "14 次调用" in text


def test_format_uses_fixed_order_and_truncates():
    hits = {key: [] for key in B1_KEYS}
    hits["zgnb_turn"] = [
        {"code": "000001", "name": "平安银行", "score": 80, "close": 12.3, "pct_chg": 2.0}
    ]
    hits["zgnb_shrink"] = [
        {
            "code": f"60000{index}",
            "name": f"票{index}号",
            "score": 90 - index,
            "close": 10.0,
            "pct_chg": 1.5,
        }
        for index in range(MAX_PER_PATTERN + 3)
    ]
    text = _format(DAY, "14:51", hits, 14)
    assert text.index("缩量拐头B1") < text.index("缩量B1")
    assert "平安银行" in text
    assert f"票{MAX_PER_PATTERN - 1}号" in text
    assert f"票{MAX_PER_PATTERN}号" not in text
    assert "其余 3 只未列出" in text


def test_scan_skips_missing_history():
    hits = _scan({}, {"600000": _snapshot_row()})
    assert set(hits) == set(B1_KEYS)
    assert all(items == [] for items in hits.values())


def test_scan_skips_st():
    hits = _scan(
        {"600000": _history()},
        {"600000": _snapshot_row(name="ST测试")},
    )
    assert all(items == [] for items in hits.values())


def test_scan_skips_dropped_codes():
    hits = _scan(
        {"600000": _history()},
        {"600000": _snapshot_row()},
        dropped={"600000"},
    )
    assert all(items == [] for items in hits.values())


def test_build_card_makes_one_table_per_pattern():
    hits = {key: [] for key in B1_KEYS}
    hits["zgnb_turn"] = [
        {"code": "000001", "name": "平安银行", "score": 80, "close": 12.3, "pct_chg": 2.0}
    ]
    hits["zgnb_shrink"] = [
        {
            "code": f"60000{index}",
            "name": f"票{index}号",
            "score": 90 - index,
            "close": 10.0,
            "pct_chg": 1.5,
        }
        for index in range(MAX_PER_PATTERN + 3)
    ]
    elements = build_card(DAY, "14:51", hits, 14)
    tables = [element for element in elements if element["tag"] == "table"]
    assert len(tables) == 2
    assert [column["display_name"] for column in tables[0]["columns"]] == [
        "名称",
        "代码",
        "现价",
        "涨幅",
        "评分",
    ]
    assert len(tables[1]["rows"]) == MAX_PER_PATTERN
    assert tables[1]["rows"][0]["pct"].endswith("%")
    assert "收盘复盘" in elements[0]["text"]["content"]


def test_build_card_without_hits():
    elements = build_card(DAY, "14:51", {key: [] for key in B1_KEYS}, 14)
    assert len(elements) == 2
    assert all(element["tag"] == "div" for element in elements)
    assert "暂无命中" in elements[1]["text"]["content"]
