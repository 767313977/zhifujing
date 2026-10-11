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

- 成交量：⚠️ 腾讯给的**单位不一致** —— 对一部分票是**手**（要 ×100），对另一部分是**股**。
  **不猜，用「成交额 ÷ 均价」自洽判定**（见 `_to_shares`）。来历：
  · 2026-09-25 实测 000006（2026-09-24）：腾讯 278,599、iFinD 27,859,946（差 100 倍），
    两边成交额相同（203,622,700 vs 203,622,719）→ **那只票**腾讯给的是手。
  · 当时据此写成「一律 ×100」，**这条是错的**：2026-10-09 发现 601599（2026-02-06）
    腾讯 raw = 321,005,400、成交额 16.4 亿 → **本来就是股**，×100 得到 321 亿股
    （比它总股本还大）。用「成交额 ÷ 成交量 = 均价」一算就现形（730.88 是收盘价的 100 倍）。
  · 这一路每天都在写库（池外候选兜底 + 命中前 N 只日线补齐），于是主库里积了一大批
    「量×价/额 ≈ 100」的行 —— 实测本机 **122.8 万行 / 48.6%**（两个簇干净地落在 ×1 与 ×100）。
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
from collections.abc import Sequence
from datetime import date

import requests

from app.sources.base import chunked

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0

# 北交所代码前缀。与 `sources/ifind._BJ_PREFIXES` 保持一致（43/83/87/88/920）——
# 北交所的腾讯前缀是 `bj`，不是 `sh`/`sz`；漏了它会把北交所票拼成 sz/sh 取不到数据。
_BJ_PREFIXES = ("43", "83", "87", "88", "920")


def tx_symbol(code: str) -> str:
    """腾讯的代码写法：沪市 `sh`、深市 `sz`、北交所 `bj`。"""
    c = str(code).zfill(6)
    if c.startswith(_BJ_PREFIXES):
        return "bj" + c
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
        high = _num(record.get("high"))
        low = _num(record.get("low"))
        rows.append(
            {
                "trade_date": date.fromisoformat(day),
                "code": str(code).zfill(6),
                "open": _num(record.get("open")),
                "high": high,
                "low": low,
                "close": close,
                # 单位不猜，用成交额自洽判定（见 `_to_shares`）
                "volume": _to_shares(
                    _num(record.get("volume")) or 0.0,
                    amount=_num(record.get("amount")) or 0.0,
                    high=high,
                    low=low,
                    close=close,
                ),
                "amount": _num(record.get("amount")) or 0.0,
                "turnover": turnover * 100 if turnover is not None else None,
                "pct_chg": pct,
            }
        )
    return rows


def _to_shares(
    raw: float,
    *,
    amount: float,
    high: float | None,
    low: float | None,
    close: float | None,
) -> float:
    """把腾讯给的成交量归一到**股**（库里这一列一律是股）。

    ⚠️ 腾讯的成交量单位**不一致**：对一部分票是**手**（要 ×100），对另一部分是**股**。
    模块旧说明写「一律是手、故 ×100」，那是**按 000006 一只票定的**；
    2026-10-09 发现 601599 这类票本来就是股 —— 一律 ×100 会让它大 100 倍，
    而这一路（池外候选兜底 / 命中前 N 只日线补齐）每天都在写库，
    于是主库里出现了一大批「同一列混了两种单位」的脏数据
    （实测全库 122.8 万行 / 48.6% 的 `volume×收盘/成交额 ≈ 100`）。

    **不猜单位，用成交额自洽**：真实股数 ≈ 成交额 ÷ 均价，均价用 `(高+低+收)/3` 近似，
    再取 `raw` 与 `raw×100` 里更接近它的那个。成交额缺失时退回旧假设（×100）。

    拿 601599 在 2026-02-06 实测：腾讯 raw = 321,005,400、成交额 16.4 亿、收盘 5.01 →
    估算股数 ≈ 3.28 亿 → 选 raw（股），×100 会得到 321 亿股（比它总股本还大，明显错）。
    """
    if raw <= 0:
        return 0.0
    if amount > 0 and close:
        vwap = ((high + low + close) / 3) if (high and low) else close
        if vwap > 0:
            estimate = amount / vwap
            return raw * 100.0 if abs(raw * 100.0 - estimate) < abs(raw - estimate) else raw
    return raw * 100.0


# --------------------------------------------------------------- 批量实时快照
#
# 走腾讯公开行情口 `qt.gtimg.cn`（**不是 akshare**）：一次可带多只、零配额、没有
# 频控惩罚。给 `jobs/collect_auction.py` 的集合竞价采样用。
#
# 返回的是 `v_sh600000="字段1~字段2~…";` 这种按 `~` 分隔的**位置数组**，没有字段名。
# 下面的下标是逐列与腾讯网页端对账出来的（2026-10-11 用 600000 / 300750 核对：
# 现价 / 昨收 / 今开 / 成交额 / 换手 / 量比 / 涨停价 全部对上）。
#
# ⚠️ 竞价时段（9:15–9:25）这批字段含义与盘中不同：`现价` 是**虚拟开盘参考价**、
# 成交量/额是**虚拟匹配量**（9:25 才出真实成交价与量），9:25 之后 `今开` 才被写入。
# 采样任务逐分钟全存正是为了留住这个过程（见 `models.AuctionSnapshot.samples`）。
_SPOT_URL = "http://qt.gtimg.cn/q="
# 单次请求的代码数。腾讯未公开上限；50 只一批，响应约 20KB，全市场约 110 次。
_SPOT_BATCH = 50
_SPOT_TIMEOUT = 15.0
# 用到的最靠后的下标是 51（均价），短于这个长度说明这行不是完整快照
_SPOT_MIN_FIELDS = 52


def _spot_num(parts: list[str], index: int) -> float | None:
    """位置数组取值转 float：越界 / 空串 / 非数 → None（竞价时段不少列是空串）。"""
    if index >= len(parts):
        return None
    text = parts[index].strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_spot(payload: str) -> dict | None:
    """解析一只票的快照行。字段不够（涨跌停 / 退市等异常行）返回 None。"""
    parts = payload.split("~")
    if len(parts) < _SPOT_MIN_FIELDS:
        return None
    code = parts[2].strip()
    if not code:
        return None
    volume_hand = _spot_num(parts, 6)  # 成交量（手）
    amount_wan = _spot_num(parts, 37)  # 成交额（万元）
    float_mv_yi = _spot_num(parts, 44)  # 流通市值（亿元）
    total_mv_yi = _spot_num(parts, 45)  # 总市值（亿元）
    return {
        "code": code,
        "name": parts[1].strip() or None,
        "price": _spot_num(parts, 3),
        "pre_close": _spot_num(parts, 4),
        "open": _spot_num(parts, 5),
        "high": _spot_num(parts, 33),
        "low": _spot_num(parts, 34),
        # 库里的成交量一律是**股**，腾讯给的是手 → ×100（与 `fetch_daily` 的口径分开：
        # 那条走 akshare、单位还不一致，这条直连固定是手）
        "volume": volume_hand * 100 if volume_hand is not None else None,
        "amount": amount_wan * 1e4 if amount_wan is not None else None,
        "turnover": _spot_num(parts, 38),  # 换手率（%）
        "volume_ratio": _spot_num(parts, 49),  # 量比
        "float_mv": float_mv_yi * 1e8 if float_mv_yi is not None else None,
        "total_mv": total_mv_yi * 1e8 if total_mv_yi is not None else None,
        "limit_up": _spot_num(parts, 47),
        "limit_down": _spot_num(parts, 48),
        "avg_price": _spot_num(parts, 51),
        "quote_time": parts[30].strip() or None,
    }


def fetch_spot(codes: Sequence[str]) -> list[dict]:
    """批量实时快照：`codes` 为 6 位代码（自动补 sh/sz/bj 前缀）。

    返回字段见 `_parse_spot`。取不到的票（停牌、退市、代码不存在）直接跳过；
    整批网络 / HTTP 失败则抛出（由调用方决定是否重试）。
    """
    wanted = [str(code).zfill(6) for code in codes]
    rows: list[dict] = []
    for batch in chunked(wanted, _SPOT_BATCH):
        symbols = ",".join(tx_symbol(code) for code in batch)
        response = requests.get(_SPOT_URL + symbols, timeout=_SPOT_TIMEOUT)
        response.raise_for_status()
        # 腾讯返回 GBK；用 replace 兜住个别名字里的坏字节，不让整批解析失败
        text = response.content.decode("gbk", errors="replace")
        for chunk in text.split(";"):
            chunk = chunk.strip()
            if "=" not in chunk:
                continue
            payload = chunk.partition("=")[2].strip().strip('"')
            parsed = _parse_spot(payload)
            if parsed is not None:
                rows.append(parsed)
    return rows
