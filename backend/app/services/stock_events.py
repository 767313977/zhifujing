"""个股事件（公告 / 研报 / 解禁）的**带 TTL 的落库缓存**。

分工：`sources/em_events.py` 只管取数与归一，这里负责「什么时候该重新请求」。
TTL：公告 6 小时，研报 / 解禁 1 天 —— 它们都不是盘中会变的数据，没必要每次打开
个股页都打一次上游；而杀猪盘体检与事件面板读的是同一份缓存，一次请求两处用。

**不抛异常**：返回值里 `note` 说明失败原因，`stale=True` 表示给的是旧缓存。
页面把这一块当增强信息 —— 上游挂了不该让个股页报错，也不该把已缓存的公告清空。
"""

import logging
from dataclasses import asdict
from datetime import datetime, timedelta

from app.db import session_scope
from app.models import StockEventCache
from app.sources import em_events

logger = logging.getLogger(__name__)

#: kind → (取数函数, 缓存有效期)
KINDS: dict[str, tuple[object, timedelta]] = {
    "notices": (em_events.fetch_notices, timedelta(hours=6)),
    "reports": (em_events.fetch_reports, timedelta(hours=24)),
    "unlock": (em_events.fetch_unlocks, timedelta(hours=24)),
}


def _read(code: str, kind: str) -> tuple[list[dict] | None, datetime | None]:
    with session_scope() as session:
        row = session.get(StockEventCache, (code, kind))
        if row is None:
            return None, None
        return list(row.payload or []), row.fetched_at


def _write(code: str, kind: str, rows: list[dict]) -> None:
    with session_scope() as session:
        row = session.get(StockEventCache, (code, kind))
        if row is None:
            session.add(
                StockEventCache(
                    code=code, kind=kind, fetched_at=datetime.now(), payload=rows
                )
            )
        else:
            row.fetched_at = datetime.now()
            row.payload = rows


def peek(code: str, kind: str) -> list[dict]:
    """只读缓存、**不请求上游**；没有缓存时返回空列表。

    给「不想为了看一眼而打外部接口」的场景用（如收盘简报里的自选公告）：
    用户打开过个股页或跑过体检后自然会有缓存。
    """
    rows, _ = _read(code, kind)
    return rows or []


def load(code: str, kind: str) -> dict:
    """读一件事的数据：`{rows, fetched_at, stale, note}`。

    - 缓存新鲜 → 直接用，不发请求；
    - 过期或没有 → 请求上游，成功后写回缓存；
    - 上游失败 → 有旧缓存就给旧缓存（`stale=True`），没有就返回空行 + `note`。

    `kind` 只接受 `KINDS` 里的三个值；非法值直接抛 `ValueError`（这是编程错误，
    不是数据源问题，不该静默吞掉）。
    """
    if kind not in KINDS:
        raise ValueError(f"未知的事件类型：{kind}")
    fetcher, ttl = KINDS[kind]
    rows, fetched_at = _read(code, kind)
    fresh = fetched_at is not None and datetime.now() - fetched_at < ttl
    if rows is not None and fresh:
        return {
            "rows": rows,
            "fetched_at": fetched_at.isoformat(timespec="seconds"),
            "stale": False,
            "note": None,
        }

    try:
        items = fetcher(code)  # type: ignore[operator]
    except Exception as exc:  # noqa: BLE001 - 上游失败只降级，不让页面 500
        logger.warning("取 %s 的 %s 失败：%s", code, kind, exc)
        if rows is not None:
            return {
                "rows": rows,
                "fetched_at": (
                    fetched_at.isoformat(timespec="seconds") if fetched_at else None
                ),
                "stale": True,
                "note": "数据源暂时不可用，显示的是上次缓存",
            }
        return {
            "rows": [],
            "fetched_at": None,
            "stale": False,
            "note": "数据源暂时没返回，请稍后再试",
        }

    payload = [asdict(item) for item in items]
    try:
        _write(code, kind, payload)
    except Exception as exc:  # noqa: BLE001 - 写缓存失败不影响这次返回
        logger.warning("写 %s 的 %s 缓存失败：%s", code, kind, exc)
    return {
        "rows": payload,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "stale": False,
        "note": None,
    }
