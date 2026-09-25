"""东财（push2his）不复权日线。

本站有两条路都走它，都是**零 iFinD 配额**：

- `jobs/scan_patterns._sync_stock_eastmoney`：池外候选的近端补日线。
  （云端连不上东财直连，那一级在云端必然失败、由腾讯接住，见 8.51.3）
- `scripts/backfill_history.py`：把历史往回补几年，写进独立的 `history.db` 供回测用

**口径必须与 `stock_daily` 一致**，否则 `build_bars` 复权出来的序列是错的：

- OHLC 用 `fqt=0`**不复权**，与全市场采集口径相同
- `pct_chg` 是东财给的真实涨跌幅 —— 复权交给 `build_bars` 用涨跌幅复利，不落第二份前复权价
- 成交量东财给**手**，这里 ×100 换成**股**；成交额单位是**元**
- `turnover`（换手率）东财这条线不提供，**不写**：`upsert_many` 只更新传入的列，
  所以库里已有的换手率不会被刷成 NULL

与库里 iFinD 写的值逐位对过账（见设计文档 8.51 与 `scripts/probe_akshare_tx.py`），
唯一差异是成交量偶有十几股的舍入。
"""

import logging
import os
from datetime import date, timedelta

logger = logging.getLogger(__name__)

_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
# 东财网页端自己用的固定 token，不是密钥
_UT = "fa5fd1943c7b386f172d6893dbfba10b"
_TIMEOUT = 20.0
_RETRIES = 3


def clear_proxies() -> None:
    """把进程里的代理环境变量清掉。

    本机环境变量里可能挂着代理（装过 VPN/抓包工具留下的），而东财是**直连可达**的 ——
    带上代理反而会被拒。放在源模块里而不是各调用点，是为了两条线都别忘了清。
    """
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        os.environ.pop(key, None)


def em_secid(code: str) -> str:
    """东财的证券 ID：沪市（5/6/9 开头）加 `1.`，其余加 `0.`。"""
    c = str(code).zfill(6)
    return f"1.{c}" if c.startswith(("5", "6", "9")) else f"0.{c}"


def fetch_daily(code: str, *, days: int) -> list[dict]:
    """取 `code` 最近 `days` 个**日历日**的不复权日线，返回可直接 upsert 的 dict 列表。

    `days` 是日历日，内部再 ×1.5 放宽（一年约 243 个交易日 ≈ 日历日的 0.66 倍），
    多出来的一段能覆盖停牌与长假。

    网络失败重试 `_RETRIES` 次后抛 `RuntimeError`；**没有数据时返回空列表**。
    这个区分很重要：退市股、还没上市的代码属于「没有数据」，不该被当成「数据源挂了」
    —— 后者会触发上层的熔断，把整条源关掉（见 `scan_patterns._NoBars`）。
    """
    import requests

    clear_proxies()
    end = date.today()
    beg = (end - timedelta(days=int(days * 1.5) + 5)).strftime("%Y%m%d")
    end_text = end.strftime("%Y%m%d")

    last_err: Exception | None = None
    payload = None
    session = requests.Session()
    session.trust_env = False
    for _ in range(_RETRIES):
        try:
            resp = session.get(
                _URL,
                params={
                    "fields1": "f1,f2,f3,f4,f5,f6",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                    "ut": _UT,
                    "klt": "101",
                    "fqt": "0",
                    "secid": em_secid(code),
                    "beg": beg,
                    "end": end_text,
                },
                timeout=_TIMEOUT,
                proxies={"http": None, "https": None},
                headers={"Referer": "https://finance.eastmoney.com/", "User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            payload = resp.json()
            break
        except Exception as exc:  # noqa: BLE001 - 网络异常一律重试
            last_err = exc
    if payload is None:
        raise RuntimeError(f"东财直连失败：{last_err}")

    data = payload.get("data") or {}
    name = (data.get("name") or code).strip()
    rows: list[dict] = []
    for line in data.get("klines") or []:
        parts = str(line).split(",")
        if len(parts) < 11:
            continue
        try:
            day = date.fromisoformat(parts[0][:10])
        except ValueError:
            continue
        rows.append(
            {
                "trade_date": day,
                "code": str(code).zfill(6),
                "name": name,
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5] or 0) * 100.0,  # 手 → 股
                "amount": float(parts[6] or 0),
                "pct_chg": float(parts[8] or 0),
            }
        )
    return rows
