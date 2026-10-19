"""知行B1 / 放量阳包阴 盘中预览：纯函数部分（不碰网络与数据库）。"""

from datetime import date, timedelta

from app.jobs.intraday_b1 import (
    B1_KEYS,
    MAX_PER_PATTERN,
    TABLE_ROWS,
    _as_record,
    _format,
    _scan,
    build_card,
)
from app.services.patterns import PATTERNS, _volume_engulf, build_bars

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


def _engulf_history() -> list[dict]:
    """25 天阴跌 + 昨日大阴（-5%）+ 今日放量阳包阴（吞没昨日实体）。"""
    start = date(2026, 9, 1)
    records: list[dict] = []
    price = 12.0
    for index in range(25):
        close = price * 0.992
        records.append(
            {
                "date": start + timedelta(days=index),
                "name": "测试股",
                "open": price,
                "high": price,
                "low": close,
                "close": close,
                "volume": 1e6,
                "amount": 1e7,
                "pct_chg": -0.8,
            }
        )
        price = close

    y_close = price * 0.95
    records.append(
        {
            "date": start + timedelta(days=25),
            "name": "测试股",
            "open": price,
            "high": price,
            "low": y_close,
            "close": y_close,
            "volume": 1e6,
            "amount": 1e7,
            "pct_chg": -5.0,
        }
    )
    return records


def _engulf_snapshot(*, volume: float = 2.5e6) -> dict:
    """今日：开 0.99 × 昨收、收 1.0737 × 昨收（> 昨开），实体吞没昨日。"""
    prev = _engulf_history()[-1]
    y_close = prev["close"]
    open_ = y_close * 0.99
    close = y_close * 1.0737
    return {
        "trade_date": DAY,
        "code": "600000",
        "name": "测试股",
        "open": open_,
        "high": close * 1.005,
        "low": open_ * 0.99,
        "close": close,
        "volume": volume,
        "amount": 2.5e7,
        "pct_chg": 7.37,
    }


def test_as_record_uses_date_key():
    record = _as_record(_snapshot_row())
    assert record["date"] == DAY
    assert record["close"] == 10.0
    assert "trade_date" not in record


def test_format_without_hits():
    hits = {key: [] for key in B1_KEYS}
    text = _format(DAY, "14:51", hits, [], 14)
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
    text = _format(DAY, "14:51", hits, [], 14)
    assert text.index("缩量拐头B1") < text.index("缩量B1")
    assert "平安银行" in text
    assert f"票{MAX_PER_PATTERN - 1}号" in text
    assert f"票{MAX_PER_PATTERN}号" not in text
    assert "其余 3 只未列出" in text


def test_format_lists_engulf():
    text = _format(
        DAY,
        "14:51",
        {key: [] for key in B1_KEYS},
        [
            {
                "code": "600000",
                "name": "测试股",
                "close": 10.5,
                "pct_chg": 5.0,
                "volume_ratio": 2.31,
            }
        ],
        14,
    )
    assert "放量阳包阴" in text
    assert "倍量 2.31" in text


def test_scan_skips_missing_history():
    hits, engulfs = _scan({}, {"600000": _snapshot_row()})
    assert set(hits) == set(B1_KEYS)
    assert all(items == [] for items in hits.values())
    assert engulfs == []


def test_scan_skips_st():
    hits, engulfs = _scan(
        {"600000": _history()},
        {"600000": _snapshot_row(name="ST测试")},
    )
    assert all(items == [] for items in hits.values())
    assert engulfs == []


def test_scan_skips_dropped_codes():
    hits, engulfs = _scan(
        {"600000": _history()},
        {"600000": _snapshot_row()},
        dropped={"600000"},
    )
    assert all(items == [] for items in hits.values())
    assert engulfs == []


def test_scan_detects_volume_engulf():
    history = _engulf_history()
    _, engulfs = _scan({"600000": history}, {"600000": _engulf_snapshot()})
    assert len(engulfs) == 1
    assert engulfs[0]["code"] == "600000"
    # 今量 2.5e6 ÷ 昨量 1e6
    assert engulfs[0]["volume_ratio"] == 2.5


def test_scan_rejects_engulf_without_volume():
    history = _engulf_history()
    _, engulfs = _scan(
        {"600000": history}, {"600000": _engulf_snapshot(volume=1e6)}
    )
    assert engulfs == []


def test_volume_engulf_registered_in_volume_group():
    pattern = next(item for item in PATTERNS if item.key == "volume_engulf")
    assert pattern.name == "放量阳包阴"
    assert pattern.group == "量价"


def test_volume_engulf_signal_detail():
    bars = build_bars(
        [*_engulf_history(), _as_record(_engulf_snapshot())], "600000"
    )
    signal = _volume_engulf(bars)
    assert signal is not None
    assert signal.pattern == "volume_engulf"
    assert signal.detail["volume_ratio"] == 2.5
    assert signal.score >= 50


def test_volume_engulf_requires_more_volume_than_yesterday():
    bars = build_bars(
        [*_engulf_history(), _as_record(_engulf_snapshot(volume=1e6))], "600000"
    )
    assert _volume_engulf(bars) is None


def test_build_card_uses_single_b1_table_sorted_by_score():
    """飞书一张卡片最多挂 5 张表，7 个形态各自成表会报错 —— B1 只能是一张总表。"""
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
        for index in range(TABLE_ROWS + 3)
    ]
    elements = build_card(DAY, "14:51", hits, [], 14)
    tables = [element for element in elements if element["tag"] == "table"]
    assert len(tables) == 1
    assert [column["display_name"] for column in tables[0]["columns"]] == [
        "形态",
        "名称",
        "代码",
        "现价",
        "涨幅",
        "评分",
    ]
    assert len(tables[0]["rows"]) == TABLE_ROWS
    scores = [int(row["score"]) for row in tables[0]["rows"]]
    assert scores == sorted(scores, reverse=True)
    assert tables[0]["rows"][0]["pattern"] == "缩量B1"
    assert "收盘复盘" in elements[0]["text"]["content"]


def test_build_card_has_engulf_table_too():
    elements = build_card(
        DAY,
        "14:51",
        {key: [] for key in B1_KEYS},
        [
            {
                "code": "600000",
                "name": "测试股",
                "close": 10.5,
                "pct_chg": 5.0,
                "volume_ratio": 2.31,
            }
        ],
        14,
    )
    tables = [element for element in elements if element["tag"] == "table"]
    assert len(tables) == 1
    assert [column["display_name"] for column in tables[0]["columns"]] == [
        "名称",
        "代码",
        "现价",
        "涨幅",
        "倍量",
    ]
    assert tables[0]["rows"][0]["ratio"] == "2.31"


def test_build_card_without_hits():
    elements = build_card(DAY, "14:51", {key: [] for key in B1_KEYS}, [], 14)
    assert len(elements) == 2
    assert all(element["tag"] == "div" for element in elements)
    assert "暂无命中" in elements[1]["text"]["content"]
