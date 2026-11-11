"""前瞻研究库（`forward_research.db`）的表结构与字段映射。

## 这个模块只做两件事

1. **建表**：`create_schema()` 在独立文件上建四张表，供独立采集程序写入。
2. **映射与校验**：把供应商原始行转成研究字段，并给出可解释的校验问题。

它**不采集、不联网、不打开生产库**。测试全部用临时文件。

## 与既有 ORM 的兼容

`stock_daily` 的列名与 `backend/app/models.py::StockDaily` 完全一致
（含 `turnover`），因此：

- `select(StockDaily)` 这类整表查询可以直接跑；
- `research_backtest.load_matrix` / `backtest_reversal` 只要把 `--db` 指过来即可复用；
- 额外列（`board` / `source` / `pct_source` / `raw_code` / `data_version` /
  `ingested_at`）**只增不改**，ORM 不声明它们也不会影响查询。

`turnover` 在历史库里存在但在 C0 因子中用不到；新库照建，是为了以后做换手类研究时
不用再改表，也为了让 ORM 的整表查询不因缺列而报错。

## 硬规则（与方案文档一致）

- 价格一律**不复权**，是当日可成交价；复权价只用于收益评价，不入库。
- `pct_chg` 为**百分数**（`2.35` 表示 +2.35%），来源记为 `official`。
- `amount` 单位**元**；缺失即判该行不合格，**不允许估算**。
- `volume` 单位**股**；若供应商给「手」，由调用方先换算，本模块只校验非负。
- 代码统一 6 位数字，原始代码（含交易所后缀）保留在 `raw_code`。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

#: 与 `app.models.StockDaily` 对齐的四张表 + 两张辅助表
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    trade_date   TEXT NOT NULL,
    code         TEXT NOT NULL,
    name         TEXT,
    open         REAL,
    high         REAL,
    low          REAL,
    close        REAL,
    pct_chg      REAL,
    volume       REAL,
    amount       REAL,
    turnover     REAL,
    board        TEXT NOT NULL,
    source       TEXT NOT NULL,
    pct_source   TEXT NOT NULL DEFAULT 'official',
    raw_code     TEXT,
    data_version TEXT,
    ingested_at  TEXT NOT NULL,
    PRIMARY KEY (trade_date, code)
);
CREATE INDEX IF NOT EXISTS idx_forward_stock_daily_code ON stock_daily (code);

CREATE TABLE IF NOT EXISTS trade_calendar (
    trade_date TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS data_revision (
    domain     TEXT PRIMARY KEY,
    revision   INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    status     TEXT NOT NULL,
    rows       INTEGER,
    message    TEXT,
    created_at TEXT NOT NULL
);
"""

CODE_RE = re.compile(r"^\d{6}$")
SUFFIXES = (".SH", ".SZ", ".BJ", ".SS")

#: 必填字段（缺失即该行不合格）
REQUIRED = ("trade_date", "code", "open", "high", "low", "close", "pct_chg", "amount")


def board_of(code: str) -> str:
    """与 `scripts/forward_track_c0._board_of` 同一套代码段规则。"""
    if code.startswith("60"):
        return "沪主板"
    if code.startswith("68"):
        return "科创板"
    if code.startswith("00"):
        return "深主板"
    if code.startswith("30"):
        return "创业板"
    if code[:2] in ("43", "83", "87", "88", "92"):
        return "北交所"
    return "其他"


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text_value = str(value).strip().replace(",", "")
    if text_value in ("", "-", "--", "None", "nan", "NaN"):
        return None
    try:
        return float(text_value)
    except ValueError:
        return None


def normalize_code(raw: Any) -> tuple[str, str]:
    """返回 `(6 位代码, 原始代码)`；无法识别时抛 ValueError。"""
    original = "" if raw is None else str(raw).strip()
    value = original.upper()
    for suffix in SUFFIXES:
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    if not CODE_RE.match(value):
        raise ValueError(f"无法识别的证券代码：{original!r}")
    return value, original


def normalize_date(raw: Any) -> date:
    if isinstance(raw, date):
        return raw
    value = "" if raw is None else str(raw).strip()
    for pattern in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            from datetime import datetime

            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"无法识别的交易日期：{raw!r}")


def normalize_row(
    raw: dict,
    *,
    source: str,
    ingested_at: str,
    pct_source: str = "official",
    data_version: str | None = None,
) -> dict:
    """把供应商原始行转成研究行。

    字段名按供应商原始列名传入（如 `证券代码` / `成交额`），也接受已改名的英文键。
    转换失败**抛 ValueError**（调用方据此整日拒绝冻结），不做「猜一个值」。
    """
    code, raw_code = normalize_code(_pick(raw, "code", "证券代码", "code"))
    trade_date = normalize_date(_pick(raw, "trade_date", "交易日期", "date"))
    row = {
        "trade_date": trade_date.isoformat(),
        "code": code,
        "name": _pick(raw, "name", "证券简称", "证券名称", "name"),
        "open": _to_float(_pick(raw, "open", "开盘价", "开盘")),
        "high": _to_float(_pick(raw, "high", "最高价", "最高")),
        "low": _to_float(_pick(raw, "low", "最低价", "最低")),
        "close": _to_float(_pick(raw, "close", "收盘价", "收盘")),
        "pct_chg": _to_float(_pick(raw, "pct_chg", "涨跌幅")),
        "volume": _to_float(_pick(raw, "volume", "成交量")),
        "amount": _to_float(_pick(raw, "amount", "成交额")),
        "turnover": _to_float(_pick(raw, "turnover", "换手率")),
        "board": board_of(code),
        "source": source,
        "pct_source": pct_source,
        "raw_code": raw_code,
        "data_version": data_version,
        "ingested_at": ingested_at,
    }
    return row


def _pick(raw: dict, *keys: str) -> Any:
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    return None


def validate_row(row: dict) -> list[str]:
    """返回问题列表；空列表 = 可入库。"""
    problems: list[str] = []
    code = row.get("code") or ""
    if not CODE_RE.match(str(code)):
        problems.append(f"代码不合法：{code!r}")
    if board_of(str(code)) == "其他":
        problems.append(f"代码段无法归入沪深京市场：{code!r}")
    for field in REQUIRED:
        if row.get(field) is None:
            problems.append(f"缺必填字段 {field}")
    for field in ("open", "high", "low", "close"):
        value = row.get(field)
        if value is not None and value <= 0:
            problems.append(f"{field} 必须为正：{value}")
    pct = row.get("pct_chg")
    if pct is not None and not (-100 < pct < 1000):
        problems.append(f"pct_chg 超出合理范围：{pct}")
    amount = row.get("amount")
    if amount is not None and amount < 0:
        problems.append(f"成交额不能为负：{amount}")
    volume = row.get("volume")
    if volume is not None and volume < 0:
        problems.append(f"成交量不能为负：{volume}")
    if row.get("pct_source") not in ("official", "derived"):
        problems.append(f"pct_source 只能为 official/derived：{row.get('pct_source')!r}")
    if not row.get("source"):
        problems.append("缺 source（数据供应商）")
    if not row.get("ingested_at"):
        problems.append("缺 ingested_at（采集时间）")
    return problems


def price_order_warnings(row: dict, tolerance: float = 1e-6) -> list[str]:
    """价格关系异常只报警告，不阻断（真实行情有极小概率因四舍五入违反）。"""
    warnings: list[str] = []
    high, low = row.get("high"), row.get("low")
    open_, close = row.get("open"), row.get("close")
    if None not in (high, low) and high + tolerance < low:
        warnings.append(f"high < low：{high} < {low}")
    if None not in (high, open_) and high + tolerance < open_:
        warnings.append(f"high < open：{high} < {open_}")
    if None not in (high, close) and high + tolerance < close:
        warnings.append(f"high < close：{high} < {close}")
    if None not in (low, open_) and low - tolerance > open_:
        warnings.append(f"low > open：{low} > {open_}")
    if None not in (low, close) and low - tolerance > close:
        warnings.append(f"low > close：{low} > {close}")
    return warnings


def create_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        for statement in SCHEMA_SQL.split(";"):
            if statement.strip():
                conn.execute(text(statement))


def connect(path) -> Engine:
    """采集程序用：读写连接（只有独立采集脚本可以调它）。"""
    return create_engine(f"sqlite:///{path}")


def connect_readonly(path) -> Engine:
    """前瞻工具用：只读连接。"""
    return create_engine(f"sqlite:///file:{path.as_posix()}?mode=ro&uri=true")
