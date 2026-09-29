"""东财直连 HTTP：日线（`push2his`）与快照（`push2`）。

本站有两个地方走它，都是**零 iFinD 配额**：

1. **日线** `/api/qt/stock/kline/get`：`jobs/scan_patterns._sync_stock_eastmoney` 给池外
   候选补近端日线，`scripts/backfill_history.py` 往回补多年历史写进 `history.db`。
2. **快照** `/api/qt/clist/get`：`jobs/scan_patterns._wudao_candidate_codes` 的「涨幅榜」
   那一路（`fetch_board_spot`）。

## 这两条路的可用性**不一样**，别混为一谈（2026-09-29 实测）

| 路 | 状态 | 关键约束 |
| --- | --- | --- |
| 快照 clist | 可用 | 但**每次请求都吃这个 IP 的配额**；连发就进惩罚期（实测 **> 10 分钟**），期间连同集群别的路径一起被拒 → 所以只发一次，**绝不翻页** |
| 日线 kline | **被路径级拒绝** | 同一台主机、同一秒：`stock/get`、`trends2/get`、`fflow/daykline/get` 全是 200，唯独 `stock/kline/get` 秒断（`RemoteDisconnected`）。换 4 台主机（`push2his` / `push2` / `82.push2` / `push2delay`）× 7 种参数与请求头变体全一样，akshare 自己的 `stock_zh_a_hist` 也失败。**本机与云端一致，跟网络、代理、IP 都无关** |

所以日线那一路**现在是死的**，靠腾讯接住（`_sync_stock_tencent`；`backfill_history.py`
默认源也是腾讯）。留着它是因为它一旦恢复就自动可用，而且失败很快（约 50ms，不拖时间）。

⚠️ 这里原先写的是「云端连不上东财直连」—— **那个判断是错的**：本机一样连不上，
而且原因不是网络层，是**这条路径本身**。照那个说法去查网络/代理，方向全错。

### 快照那一页的上限

`clist` 服务端**每页最多给约 100 行**，`pz` 传多大都差不多（实测 `pz=1000` 与 `pz=50000`
的响应体都是约 4.9 KB）。akshare 的 `stock_zh_a_spot_em` 正是 `pz=100` + 翻页凑全市场，
它那句 `per_page_num = len(diff)`（拿**实际**行数而不是请求的 `pz` 去算页数）就是这个
上限的旁证。
"""

import logging
import os
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# ---- 日线（push2his）----
_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
# 东财网页端自己用的固定 token，不是密钥
_UT = "fa5fd1943c7b386f172d6893dbfba10b"

# ---- 快照（push2 的 clist）----
_CLIST_URL = "https://82.push2.eastmoney.com/api/qt/clist/get"
# 与日线那个不是同一个 token（网页端各自带各自的）
_CLIST_UT = "bd1d9ddb04089700cf9c27f6f7426281"
# 创业板 + 科创板。**东财的 `fs` 用空格分隔，不是 `+`** —— 照 akshare 的写法抄，
# 别按 URL 习惯改成 `+`（虽然实测两者都能通，但没必要多一个变量）。
_BOARD_ONLY_FS = "m:0 t:80,m:1 t:23"

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
    """东财的证券 ID：沪市（5 / 6 / 900 开头的 B 股）加 `1.`，其余加 `0.`。

    ⚠️ 北交所（920xxx）虽然以 `9` 开头，**却不是沪市** —— 原来是「9 开头 → `1.`」，
    实测 `1.920427` 取不到任何数据、`0.920427` 正常返回华维设计的 K 线
    （2026-09-27 修）。判据用 `"90"` 而不是 `"9"`：900xxx 才是沪 B。
    """
    c = str(code).zfill(6)
    return f"1.{c}" if c.startswith(("5", "6", "90")) else f"0.{c}"


def fetch_daily(code: str, *, days: int) -> list[dict]:
    """取 `code` 最近 `days` 个**日历日**的不复权日线，返回可直接 upsert 的 dict 列表。

    **口径必须与 `stock_daily` 一致**，否则 `build_bars` 复权出来的序列是错的：

    - OHLC 用 `fqt=0`**不复权**，与全市场采集口径相同
    - `pct_chg` 是东财给的真实涨跌幅 —— 复权交给 `build_bars` 用涨跌幅复利，
      不落第二份前复权价
    - 成交量东财给**手**，这里 ×100 换成**股**；成交额单位是**元**
    - `turnover`（换手率）东财这条线不提供，**不写**：`upsert_many` 只更新传入的列，
      所以库里已有的换手率不会被刷成 NULL

    与库里 iFinD 写的值逐位对过账（见设计文档 8.51 与 `scripts/probe_akshare_tx.py`），
    唯一差异是成交量偶有十几股的舍入。

    `days` 是日历日，内部再 ×1.5 放宽（一年约 243 个交易日 ≈ 日历日的 0.66 倍），
    多出来的一段能覆盖停牌与长假。

    网络失败重试 `_RETRIES` 次后抛 `RuntimeError`；**没有数据时返回空列表**。
    这个区分很重要：退市股、还没上市的代码属于「没有数据」，不该被当成「数据源挂了」
    —— 后者会触发上层的熔断，把整条源关掉（见 `scan_patterns._NoBars`）。

    ⚠️ **这条路 2026-09-29 起被东财路径级拒绝**（见模块说明），调用方得自己有兜底。
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


def fetch_board_spot(*, limit: int = 100) -> list[tuple[str, float]]:
    """东财快照：创业板 + 科创板里按涨幅降序的前 `limit` 只，返回 `[(6 位代码, 涨幅%), …]`。

    **一次请求，绝不翻页。** 这是这个函数存在的全部理由 ——

    原来用的是 akshare 的 `stock_zh_a_spot_em()`：它是 `pz=100` **翻页**拉全市场
    5561 只（56 次请求）、再按涨幅排序取前 100。2026-09-29 实测：单页请求在干净的 IP
    上返回 200，但连发几十次后**第 1 页就开始断**，进入 `push2*` 集群的惩罚期
    （> 10 分钟，期间连 `push2his` 的其它路径一起被拒；`push2ex` 不受影响）。
    后果是「涨幅榜」这一路时好时坏，候选池从约 80 只掉到 39~54 只。

    而这一路实际需要的只是「这两块板里最热的几十只」——`fs` 收窄到两块板、
    `fid=f3` 让服务端按涨幅降序排，**一次请求**就能拿到，请求数 56 → 1。

    ⚠️ 服务端**每页上限约 100 行**（见模块说明），所以 `limit` 要大于 100 是没用的；
    目标池只有 80 个位置、而涨幅这一路优先级最高会先占满，所以一页足够。

    网络失败重试 `_RETRIES` 次后抛 `RuntimeError`（调用方按「这条源挂了」处理，
    见 `scan_patterns._wudao_candidate_codes` 的 `except`）。停牌股东财给的 `f3`
    是字符串 `"-"`，**直接跳过**，不当成 0。
    """
    import requests

    clear_proxies()
    last_err: Exception | None = None
    payload = None
    session = requests.Session()
    session.trust_env = False
    for _ in range(_RETRIES):
        try:
            resp = session.get(
                _CLIST_URL,
                params={
                    "pn": "1",
                    "pz": str(limit),
                    "po": "1",  # 降序
                    "np": "1",
                    "ut": _CLIST_UT,
                    "fltt": "2",
                    "invt": "2",
                    "fid": "f3",  # 按涨跌幅排
                    "fs": _BOARD_ONLY_FS,
                    "fields": "f12,f14,f3",
                },
                timeout=_TIMEOUT,
                proxies={"http": None, "https": None},
                headers={"Referer": "https://quote.eastmoney.com/", "User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            payload = resp.json()
            break
        except Exception as exc:  # noqa: BLE001 - 网络异常一律重试
            last_err = exc
    if payload is None:
        raise RuntimeError(f"东财快照失败：{last_err}")

    rows: list[tuple[str, float]] = []
    for item in (payload.get("data") or {}).get("diff") or []:
        code = str(item.get("f12") or "").strip().zfill(6)
        if not code or code == "000000":
            continue
        try:
            rows.append((code, float(item.get("f3"))))
        except (TypeError, ValueError):
            continue  # 停牌等：f3 是 "-"
    return rows

