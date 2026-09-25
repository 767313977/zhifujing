"""腾讯日线（经 akshare `stock_zh_a_hist_tx`）。

**零 iFinD 配额**，本站两条路都走它：

- `jobs/scan_patterns._sync_stock_tencent`：池外候选补近端日线的第二级兜底
  （云端连不上东财直连，那一级在云端必然失败，这条线两边都通）
- `scripts/backfill_history.py`：往回补多年历史供回测用

## 为什么要拉两份

价格存**不复权**（与 `stock_daily` 口径一致），但 `pct_chg` 必须由**前复权**序列的相邻
收盘算出来 —— 拿不复权收盘价比，除权日会跳空成一个假暴跌。所以这里拉 plain 与 qfq
两次，价格取 plain、涨跌幅取 qfq 之比。与 iFinD 官方涨跌幅的差异见下面那条（**不是
逐位相同**：7,930 行里中位差 0、最大 0.43pp）。

## 其它口径（2026-09-25 重新实测，**修正了原先写错的一条**）

- 成交量腾讯给的是**手**，本站库里是**股**，故 ×100。
  ⚠️ 原先这里写的是「成交量(股) 与 iFinD 逐位相同」—— **那条是错的**，实测：
  000006 在 2026-09-24 腾讯 278,599、iFinD 27,859,946（差 100 倍），
  而两边的**成交额完全相同**（203,622,700 vs 203,622,719）。
  用「成交额 ÷ 成交量 = 均价」自洽判定：腾讯算出来 730.88（= 收盘价的 100 倍），
  iFinD 算出来 7.3088（≈ 收盘价）→ 腾讯是手、iFinD 是股，没有别的解释。
  按 100 倍修正后两边只差几十股（腾讯按手取整）。
  这个错误原先还让**线上那条兜底路径**把「手」写进了「股」的列，
  于是同一只票的成交额序列里混了两种单位（主库里能直接看到这种行）。
- 成交额单位是**元**，与 iFinD 一致（上例两边只差 19 元）
- `turnover` 腾讯给的是**比例**（0.0015），本站库里是**百分数**（0.15），故 ×100
- 前复权序列的**第一行**没有前值、算不出涨跌幅，会被丢掉 —— 调用方取数时多留一段
- `pct_chg` 由腾讯的前复权序列推得，与 iFinD 的官方涨跌幅有**微小差异**：
  7,930 行重叠样本里中位数差 0.0000、p90 0.028、p99 0.098、最大 0.43（差 >0.1pp 的占 0.95%）。
  属复权口径与除权日的差异，做形状回测可以接受，但**不是逐位相同**。

单次区间实测可到 3 年 / 743 行、10 年 / 2444 行（2026-09-25：请求 2016-09 起返回 2444 行），
一次调用约 3 秒（akshare 内部会分 6 页翻），所以补历史要并行。
"""

import logging
from datetime import date

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0


def tx_symbol(code: str) -> str:
    """腾讯的代码写法：沪市 `sh`、深市 `sz`。"""
    c = str(code).zfill(6)
    return ("sh" if c.startswith(("5", "6", "9")) else "sz") + c


def fetch_daily(code: str, *, start: date, end: date) -> list[dict]:
    """取 `code` 在 `[start, end]` 区间的不复权日线，附由前复权推得的真实涨跌幅。

    返回的行**不含 `name`**（调用方自己补：批量补历史时一次性查名字，省掉每只票一次查询）。
    **没有数据时返回空列表**（退市、未上市），网络异常则抛出。
    """
    import akshare as ak
    import pandas as pd

    def _num(value: object) -> float | None:
        """pandas 的 NaN/NaT → None，其余转 float（腾讯的空字段给的是 NaN）。"""
        if value is None or pd.isna(value):
            return None
        return float(value)

    symbol = tx_symbol(code)

    def _fetch(adjust: str) -> pd.DataFrame | None:
        try:
            return ak.stock_zh_a_hist_tx(
                symbol=symbol,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust=adjust,
                timeout=_TIMEOUT,
            )
        except (IndexError, KeyError):
            # ⚠️ 对「没有这只票」（退市 / 未上市），akshare 不是给空表，而是在解析里炸掉 ——
            # 实测 300060 报 `list index out of range`。那是**没有数据**，不是源挂了，
            # 所以这里吞掉并交给下面按空表处理（上层不能把它记进熔断）
            return None

    plain = _fetch("")
    if plain is None or plain.empty:
        return []
    qfq = _fetch("qfq")
    if qfq is None or qfq.empty:
        return []

    qfq_days = [str(value)[:10] for value in qfq["date"].tolist()]
    qfq_close = {day: _num(value) for day, value in zip(qfq_days, qfq["close"].tolist())}
    # 逐日真实涨跌幅：**前复权比前复权**（拿不复权收盘当分子会在除权日算错）
    pct_by_day: dict[str, float] = {}
    for index, day in enumerate(qfq_days):
        if not index:
            continue
        current = qfq_close.get(day)
        previous = qfq_close.get(qfq_days[index - 1])
        if current is None or not previous:
            continue
        pct_by_day[day] = (current / previous - 1) * 100

    rows: list[dict] = []
    for record in plain.to_dict("records"):
        day = str(record.get("date"))[:10]
        close = _num(record.get("close"))
        pct = pct_by_day.get(day)
        if close is None or pct is None:
            continue  # 首行，或前复权缺这一行：算不出真实涨跌幅，丢掉
        turnover = _num(record.get("turnover"))
        rows.append(
            {
                "trade_date": date.fromisoformat(day),
                "code": str(code).zfill(6),
                "open": _num(record.get("open")),
                "high": _num(record.get("high")),
                "low": _num(record.get("low")),
                "close": close,
                # 腾讯给的是**手**，库里是**股**，×100（见模块说明里那段实测）
                "volume": (_num(record.get("volume")) or 0.0) * 100.0,
                "amount": _num(record.get("amount")) or 0.0,
                "turnover": turnover * 100 if turnover is not None else None,
                "pct_chg": pct,
            }
        )
    return rows
