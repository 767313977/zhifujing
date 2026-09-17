"""iFinD 自然语言工具返回 Markdown 表格，这里负责解析与数值归一化。

实测坑位：
- search_stocks / get_stock_performance / sector_data 都在 `data.answer` 里塞 Markdown 表
- 一次回答可能包含**多张**表（如 sector_data）
- 表头常写"单位：元"但值是"22.1734亿"，必须按值上的单位后缀换算
- 非交易日行的涨跌幅/成交额是空白或字面量 "\\t"
"""

import re

_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_SEPARATOR = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
# 兼容 1.58E12 这类科学计数法，以及 22.1734亿 / 52.7511万 / 3.1275% 这类带单位的值
_NUM_RE = re.compile(r"^\s*(-?[\d,]+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(万亿|亿|万|千|%)?\s*$")

_UNIT_FACTOR = {"万亿": 1e12, "亿": 1e8, "万": 1e4, "千": 1e3, "%": 1.0}
# 空值有多种写法，包括字面量反斜杠 t（iFinD 用 \t 表示空白单元格）
_EMPTY = {"", "-", "--", "null", "nan", "none", "n/a", "\\t", "\t"}


def parse_tables(text: str) -> list[list[dict[str, str]]]:
    """从一段文本中提取所有 Markdown 表格。

    返回 [[{列名: 单元格原文}, ...], ...]，外层每个元素是一张表。
    """
    tables: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    header: list[str] | None = None

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if _TABLE_ROW.match(line):
            if _SEPARATOR.match(line):
                continue
            cells = [
                c.strip() for c in _TABLE_ROW.match(line).group(1).split("|")  # type: ignore[union-attr]
            ]
            if header is None:
                header = cells
                continue
            if len(cells) < len(header):
                cells += [""] * (len(header) - len(cells))
            current.append(dict(zip(header, cells)))
        else:
            if header is not None:
                if current:
                    tables.append(current)
                current, header = [], None

    if header is not None and current:
        tables.append(current)
    return tables


def to_float(value: object) -> float | None:
    """把 iFinD 返回值转成 float，空值返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().replace(",", "")
    if text.lower() in _EMPTY:
        return None

    match = _NUM_RE.match(text)
    if not match:
        return None
    return float(match.group(1)) * _UNIT_FACTOR.get(match.group(2) or "", 1.0)


def to_int(value: object) -> int | None:
    number = to_float(value)
    if number is None:
        return None
    return int(round(number))


def to_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _EMPTY:
        return None
    return text


def pick(row: dict[str, str], *keywords: str) -> str | None:
    """按关键词模糊匹配列名取值。

    iFinD 的列名不稳定（如"成交额"会写成"成交额（单位：元）"、
    "涨跌幅（单位：%）"），用关键词包含匹配比精确匹配可靠。
    """
    for keyword in keywords:
        for column, value in row.items():
            if keyword in column:
                return value
    return None


def pick_float(row: dict[str, str], *keywords: str) -> float | None:
    return to_float(pick(row, *keywords))


def pick_int(row: dict[str, str], *keywords: str) -> int | None:
    return to_int(pick(row, *keywords))


def pick_text(row: dict[str, str], *keywords: str) -> str | None:
    return to_text(pick(row, *keywords))
