"""把「代码 / 名称 / 拼音首字母」解析成 6 位代码 —— 加自选那个输入框用的。

原来那个框只认真代码（`normalize_code` 抽数字）。2026-09-26 用户要求名称与拼音
首字母也能直接加：手边想不到代码时，「贵州茅台」和「gzmt」比 600519 好记。

## 为什么不把首字母落库

首字母只是名字的**派生物**。落一列 `initials` 意味着三个写名字的地方
（`collect_daily.sync_stock`、`scan_dde.collect_market`、`collect_universe`）
都要跟着维护它，还得为老库写一次回填；而这里把 `stock_basic` 全表（约 5570 只）
建成内存索引只要 **0.1 秒**（实测），名字改了下次重建自然就对上了。

索引进程内建一次、`INDEX_TTL` 过期重建（进程可能连跑几周，新上市得跟得上）。
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from pypinyin import Style, lazy_pinyin
from sqlalchemy import select

from app.db import session_scope
from app.models import StockBasic
from app.sources.ifind import normalize_code

logger = logging.getLogger(__name__)

# 索引多久重建一次
INDEX_TTL = timedelta(hours=12)

# 报「匹配到多只」时最多列几个，再多就只报个数
MAX_CANDIDATES = 8

# 模糊那档（名称包含 / 首字母开头）至少要有这么长：单字母会把半个市场捞进来
LOOSE_MIN_LEN = 2


class LookupError(ValueError):
    """输入解析不出唯一一只票。文案直接给用户看，由接口层转成 400。"""


def _initials(name: str) -> str:
    """中文名的拼音首字母（小写、只留字母数字）。

    ⚠️ 过滤要**按字符**做，不能按 `lazy_pinyin` 返回的块过滤：`*ST美丽` 那一块是
    整段 `*ST`，`isalnum()` 为假 —— 按块过滤会把 `ST` 一起丢掉，得到 `ml` 而不是
    `stml`（实测踩过）。用户敲的是 `stml`。
    """
    joined = "".join(lazy_pinyin(name, style=Style.FIRST_LETTER))
    return "".join(ch for ch in joined if ch.isalnum()).lower()


@dataclass(frozen=True)
class _Index:
    """三张表：名称 → 代码、首字母 → 代码、代码 → 名称（报错文案里显示用）。"""

    names: dict[str, list[str]]
    initials: dict[str, list[str]]
    by_code: dict[str, str]
    built_at: datetime


def _build() -> _Index:
    names: dict[str, list[str]] = {}
    initials: dict[str, list[str]] = {}
    by_code: dict[str, str] = {}
    with session_scope() as session:
        rows = session.execute(select(StockBasic.code, StockBasic.name)).all()
    for code, name in rows:
        if not code or not name:
            continue
        key = name.strip()
        by_code[code] = key
        # 同名是可能的（不同板块/不同交易所），所以桶里放列表、不覆盖
        names.setdefault(key.lower(), []).append(code)
        initials.setdefault(_initials(key), []).append(code)
    for bucket in (*names.values(), *initials.values()):
        bucket.sort()
    logger.info("股票查找索引已建：%d 个名称 / %d 个首字母", len(names), len(initials))
    return _Index(names=names, initials=initials, by_code=by_code, built_at=datetime.now())


_index: _Index | None = None


def _current() -> _Index:
    global _index
    now = datetime.now()
    if _index is None or now - _index.built_at > INDEX_TTL:
        _index = _build()
    return _index


def _loose(index: _Index, lowered: str) -> list[str]:
    """放宽一档：名称**包含**输入，或首字母**以输入开头**（「茅台」/「gzm」）。"""
    found: set[str] = set()
    for name, codes in index.names.items():
        if lowered in name:
            found.update(codes)
    if len(lowered) >= LOOSE_MIN_LEN:
        for initials, codes in index.initials.items():
            if initials.startswith(lowered):
                found.update(codes)
    return sorted(found)


def _ambiguous(index: _Index, key: str, codes: list[str]) -> str:
    shown = "、".join(
        f"{code} {index.by_code.get(code, '')}".strip() for code in codes[:MAX_CANDIDATES]
    )
    more = "" if len(codes) <= MAX_CANDIDATES else f" 等共 {len(codes)} 只"
    return f"「{key}」匹配到多只：{shown}{more}，请输入完整代码"


def resolve_code(raw: str) -> str:
    """把用户输入解析成 6 位代码。解析不出或多解时抛 `LookupError`。

    顺序：**真代码 → 名称全等 → 首字母全等 → 名称包含 / 首字母开头**。
    前两档只要有匹配就出结果（全等还多解的话就是真有重名，报错让人补代码）；
    模糊那档**只有唯一一只才算数**，否则宁可报错也不要猜。
    """
    key = (raw or "").strip()
    if not key:
        raise LookupError("请输入股票代码、名称或拼音首字母")

    # 真代码：与改动前的行为**完全一致**（不查库）。库里没有的写法原来是放行的，
    # 这里若收紧就成了一次没人要求的行为变更。
    digits = normalize_code(key)
    if len(digits) == 6:
        return digits

    index = _current()
    lowered = key.lower()
    for bucket in (index.names, index.initials):
        codes = bucket.get(lowered)
        if codes:
            if len(codes) == 1:
                return codes[0]
            raise LookupError(_ambiguous(index, key, codes))

    codes = _loose(index, lowered)
    if len(codes) == 1:
        return codes[0]
    if codes:
        raise LookupError(_ambiguous(index, key, codes))
    raise LookupError(f"没有找到「{key}」——可以输入 6 位代码、股票名称或拼音首字母")
