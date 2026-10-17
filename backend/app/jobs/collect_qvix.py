"""中国波指（QVIX）采集（akshare，**零 iFinD 配额**）。

两个品种：
- `QVIX50`  ← `ak.index_option_50etf_qvix()`（回溯到 **2015-02-09**，最全）
- `QVIX300` ← `ak.index_option_300etf_qvix()`（回溯到 2019-12-23，300ETF 期权那年才上市）

## 口径与取舍（读之前先看这段）

QVIX 是**期权论坛**按方差互换思路合成的「中国波指」，**不是**交易所官方 iVX ——
官方那条 2018 年就停更了。在「口径可控」与「历史长度 / 成本」之间我们选了后者：
**直取现成指数**，历史 10 年+、零计算、零 iFinD 配额；代价是加权方案不在我们手里，
且依赖对方站点（改版即断，但 akshare 社区通常会跟进修）。自建那条路（拉期权链自定
权重合成）的原委见 B 档讨论，不是这里能顺手做的事。

## 两个实现要点

1. 接口**一次返回全历史**，所以 `refresh()` 就是**全量 upsert** —— 没有分块、没有
   补数逻辑，数据源修订历史时也会自动同步。这也是本模块**没有 `backfill()`** 的原因：
   它和 `refresh()` 是同一件事，留两个名字只会让人以为有区别。
2. ⚠️ 同类接口 `index_option_50index_qvix` / `300index_qvix` / `1000index_qvix`
   **不可用**（2026-10-16 实测最新 `close` 恒为 `0.0`）—— 名字看着对，别拿来用。

单位是**百分数**（17.95 表示隐含波动率 17.95%）。**与指数反向**：指数跌、波动率升。
"""

import logging
from datetime import date

from sqlalchemy import func, select

from app.db import session_scope, upsert_many
from app.models import TradeCalendar, VolatilityDaily
from app.sources.markdown_table import to_float

logger = logging.getLogger(__name__)

# 代码 → (akshare 接口名, 中文名)
CONTRACTS: dict[str, tuple[str, str]] = {
    "QVIX50": ("index_option_50etf_qvix", "50ETF波动率"),
    "QVIX300": ("index_option_300etf_qvix", "300ETF波动率"),
}

# akshare 的 date 列是 str 或 pandas Timestamp / date，统一归一成 date
_DATE_KEYS = ("date", "日期")


def _as_date(raw: object) -> date | None:
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except ValueError:
        return None


def _rows(api: str, code: str, name: str) -> list[dict]:
    """拉一个品种的全历史，返回可直接写库的行（close 为空的行丢掉）。"""
    import akshare as ak  # 延迟导入：scheduler 启动不背这个重依赖

    frame = getattr(ak, api)()
    out: list[dict] = []
    for record in frame.to_dict("records"):
        day = None
        for key in _DATE_KEYS:
            if key in record:
                day = _as_date(record[key])
                break
        close = to_float(record.get("close"))
        if day is None or close is None:
            # 300ETF 的 2015~2019 那段整行是 NaN（标的期权还没上市），丢掉而不是写空行
            continue
        out.append(
            {
                "trade_date": day,
                "code": code,
                "name": name,
                "open": to_float(record.get("open")),
                "high": to_float(record.get("high")),
                "low": to_float(record.get("low")),
                "close": close,
            }
        )
    return out


def _trading_days(start: date, end: date) -> set[date]:
    with session_scope() as session:
        return set(
            session.scalars(
                select(TradeCalendar.trade_date).where(
                    TradeCalendar.trade_date >= start,
                    TradeCalendar.trade_date <= end,
                )
            )
        )


def refresh() -> int:
    """全量拉取两个品种并 upsert，返回写入行数。

    接口一次给全历史，所以这就是「回补 + 增量」的全部 —— 本模块没有单独的 `backfill()`。
    单品种失败只记日志、不拖垮另一个（两个来源是独立页面）。
    """
    total = 0
    for code, (api, name) in CONTRACTS.items():
        try:
            rows = _rows(api, code, name)
        except Exception:  # noqa: BLE001 - 单品种失败不该拖垮另一个
            logger.exception("QVIX 拉取失败：%s", api)
            continue
        if not rows:
            logger.warning("QVIX %s 未返回任何数据，请检查接口", api)
            continue
        # 来源会混进非交易日行（2026-10-17 体检发现 6 行，日期落在周末 / 节假日）。
        # 交易日历覆盖 1990 至今，按它过滤即可；日历为空（新库还没采）时**不过滤**，
        # 免得把整段历史当垃圾丢掉。
        days = _trading_days(
            min(row["trade_date"] for row in rows), max(row["trade_date"] for row in rows)
        )
        if days:
            kept = [row for row in rows if row["trade_date"] in days]
            if len(kept) != len(rows):
                logger.warning(
                    "QVIX %s：丢弃 %d 行非交易日数据（来源口径问题）",
                    code,
                    len(rows) - len(kept),
                )
            rows = kept
        if not rows:
            continue
        with session_scope() as session:
            written = upsert_many(session, VolatilityDaily, rows)
        total += written
        logger.info(
            "QVIX %s：写入 %d 行（%s ~ %s）", code, written, rows[0]["trade_date"], rows[-1]["trade_date"]
        )
    return total


def latest_trade_date() -> date | None:
    with session_scope() as session:
        return session.scalar(select(func.max(VolatilityDaily.trade_date)))
