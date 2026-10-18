"""ETF 份额的**交易所官方**来源：上交所按日归档 + 深交所当前快照。

两所都是官方数据、零鉴权、单位**万份**（本模块统一换算成「份」返回）。

- **上交所**：`query.sse.com.cn/commonQuery.do`，可按任意历史日期查（实测
  2026-10-16 返回 924 只、自报 total 一致）。
- **深交所**：`fund.szse.cn/api/report/ShowReport/data`，**只有当前快照**，
  数据日期在 `metadata.subname` 里（实测 T 日晚是预估值、T+1 早是确认值）。
  分页很碎（758 只 / 38 页），逐页取完并核对 `recordcount`。

## 为什么必须**按同一个日期**取两所

两所的数据日期经常差一天：深交所 T 日的数据要 T+1 早才确认，而上交所 T 日盘后就有。
如果「上交所拿 T、深交所拿 T-1」直接合并，会写出一条**只有一半代码**的日期 ——
而 `services.etf_flow` 会把它当成正常日（`MIN_ROWS = 300`），净申赎于是只配对了
半个市场，算出一个偏小但看起来正常的数。所以本模块以**深交所的快照日期**为准：
先取深交所，拿到 `sz_day`，再用 `sz_day` 去取上交所的历史。两边都齐了才返回。

⚠️ 上交所单页被服务端压到 2000 条（请求 10000 也回 2000），所以按 `pageHelp`
翻页；返回条数必须等于它自报的 `total`，否则按「结果不完整」报错。
"""

import logging
import re
import time
from datetime import date

import requests

logger = logging.getLogger(__name__)

SSE_URL = "https://query.sse.com.cn/commonQuery.do"
SZSE_URL = "https://fund.szse.cn/api/report/ShowReport/data"

# 深交所分页之间的礼貌间隔（38 页，实测每页 ~0.6 秒）
SZSE_PAGE_SLEEP = 0.3

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class EtfSourceError(RuntimeError):
    """交易所 ETF 份额取数失败（网络 / 结构变化）。"""


class EtfNotReady(EtfSourceError):
    """两所里至少有一所当天还没有数据（还没发布，不是坏掉）。"""


def _get(url: str, *, params: dict, referer: str) -> requests.Response:
    try:
        response = requests.get(
            url,
            params=params,
            headers={"User-Agent": _UA, "Referer": referer},
            timeout=(10, 40),
        )
    except requests.RequestException as exc:
        raise EtfSourceError(f"请求 {url} 失败: {type(exc).__name__}: {exc}") from exc
    if response.status_code != 200:
        raise EtfSourceError(f"{url} 返回 HTTP {response.status_code}")
    return response


def _json(response: requests.Response) -> object:
    try:
        return response.json()
    except ValueError as exc:
        raise EtfSourceError(f"{response.url} 返回的不是 JSON，可能是错误页") from exc


def _number(value: object) -> float:
    if isinstance(value, bool):
        raise EtfSourceError(f"来源在数值字段给了布尔值 {value!r}")
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError) as exc:
        raise EtfSourceError(f"来源返回了无法识别的数值 {value!r}") from exc


def sse_shares(day: date) -> list[dict]:
    """上交所某日的 ETF 份额（返回「份」）。当天没有数据时返回空列表。"""
    rows: list[dict] = []
    total = None
    page = 1
    while total is None or len(rows) < total:
        payload = _json(
            _get(
                SSE_URL,
                referer="https://www.sse.com.cn/",
                params={
                    "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
                    "STAT_DATE": day.isoformat(),
                    "isPagination": "true",
                    "pageHelp.pageSize": 10000,
                    "pageHelp.pageNo": page,
                    "pageHelp.beginPage": page,
                    "pageHelp.cacheSize": 1,
                    "pageHelp.endPage": page,
                },
            )
        )
        if not isinstance(payload, dict):
            raise EtfSourceError("上交所 ETF 规模接口返回结构改变")
        result = payload.get("result")
        page_help = payload.get("pageHelp")
        if not isinstance(result, list) or not isinstance(page_help, dict):
            raise EtfSourceError("上交所 ETF 规模接口返回结构改变")
        if total is None:
            total = int(page_help.get("total") or 0)
        for rec in result:
            if not isinstance(rec, dict):
                raise EtfSourceError("上交所 ETF 规模接口返回了非对象行")
            if rec.get("STAT_DATE") != day.isoformat():
                raise EtfSourceError("上交所返回了其他日期的数据")
            try:
                rows.append(
                    {
                        "trade_date": day,
                        "code": str(rec["SEC_CODE"]).strip(),
                        "name": str(rec["SEC_NAME"]).strip(),
                        "shares": _number(rec["TOT_VOL"]) * 1e4,
                    }
                )
            except KeyError as exc:
                raise EtfSourceError(f"上交所 ETF 规模字段缺失: {exc!r}") from exc
        if not result:
            break
        page += 1
        if page > 50:
            raise EtfSourceError("上交所 ETF 规模分页超过 50 页，分页信息异常")
    if total is None:
        raise EtfSourceError("上交所 ETF 规模接口没有返回 pageHelp")
    if len(rows) != total:
        raise EtfSourceError(
            f"上交所 ETF 规模取到 {len(rows)} 条，与自报总数 {total} 不符"
        )
    return rows


def _szse_page(page: int) -> tuple[date, int, int, list[dict]]:
    """取深交所基金列表的一页，返回 (快照日期, 总页数, 总条数, 行)。"""
    payload = _json(
        _get(
            SZSE_URL,
            referer="https://fund.szse.cn/",
            params={
                "SHOWTYPE": "JSON",
                "CATALOGID": "1000_lf",
                "TABKEY": "tab1",
                "selectJjlb": "ETF",
                "PAGENO": page,
            },
        )
    )
    table = payload[0] if isinstance(payload, list) and payload else None
    meta = table.get("metadata") if isinstance(table, dict) else None
    data = table.get("data") if isinstance(table, dict) else None
    if not isinstance(meta, dict) or not isinstance(data, list):
        raise EtfSourceError(f"深交所基金列表第 {page} 页结构改变（缺 metadata / data）")

    subname = str(meta.get("subname") or "").strip()
    try:
        snap_day = date.fromisoformat(subname[:10])
    except ValueError as exc:
        raise EtfSourceError(f"深交所快照日期无法识别: {subname!r}") from exc
    pages = int(meta.get("pagecount") or 0)
    total = int(meta.get("recordcount") or 0)
    if pages < 1 or total < 1:
        raise EtfSourceError(f"深交所基金列表分页信息异常: {snap_day}, {pages}, {total}")
    if not data:
        raise EtfSourceError(f"深交所 ETF 列表第 {page}/{pages} 页是空的，结果不完整")

    rows: list[dict] = []
    for rec in data:
        if not isinstance(rec, dict):
            raise EtfSourceError("深交所基金列表返回了非对象行")
        try:
            code = re.search(r"<u>(\d{6})</u>", str(rec["sys_key"]))
            name = re.search(r"<u>(.*?)</u>", str(rec["jjjcurl"]))
            shares = re.search(r">([\d,\.]+)</a>", str(rec["dqgm"]))
        except KeyError as exc:
            raise EtfSourceError(f"深交所基金列表字段缺失: {exc!r}") from exc
        if not (code and name and shares):
            raise EtfSourceError("深交所基金列表字段格式改变")
        rows.append(
            {
                "trade_date": snap_day,
                "code": code.group(1),
                "name": name.group(1),
                "shares": _number(shares.group(1)) * 1e4,
            }
        )
    return snap_day, pages, total, rows


def szse_snapshot() -> tuple[date, int, int]:
    """只看第一页，返回 (快照日期, 总页数, 总条数)。

    给「数据库里这一天已经完整」的早退用：不看日期就把 38 页翻完，第二趟纯属浪费。
    """
    day, pages, total, _ = _szse_page(1)
    return day, pages, total


def szse_shares(pages: int | None = None) -> tuple[date, list[dict]]:
    """深交所**当前**的 ETF 份额快照，返回 (数据日期, 行)。

    深交所只有当前快照，数据日期在 `metadata.subname`；翻页途中快照日期或总数
    变化说明拼出来的是两份快照的混合，直接报错重试。
    """
    snap_day, first_pages, total, rows = _szse_page(1)
    pages = pages or first_pages
    for page in range(2, pages + 1):
        time.sleep(SZSE_PAGE_SLEEP)
        day, page_count, record_count, data = _szse_page(page)
        if (day, page_count, record_count) != (snap_day, first_pages, total):
            raise EtfSourceError(
                f"深交所 ETF 列表翻页时快照从 {(snap_day, first_pages, total)} "
                f"变成 {(day, page_count, record_count)}，请重试"
            )
        rows.extend(data)
    if len(rows) != total:
        raise EtfSourceError(f"深交所 ETF 列表取到 {len(rows)} 条，与自报总数 {total} 不符")
    return snap_day, rows


def fetch_shares(day: date) -> list[dict]:
    """取一份**完整**的 ETF 份额快照（两所同一天）。

    以深交所的快照日期为准去取上交所的历史 —— 理由见模块说明。
    两所任一没有该日数据时抛 `EtfNotReady`（下一趟再试，不当成错误）。
    """
    sz_day, sz_rows = szse_shares()
    if sz_day > day:
        raise EtfSourceError(f"深交所快照日期 {sz_day} 晚于目标日 {day}，口径异常")
    sh_rows = sse_shares(sz_day)
    if not sh_rows:
        raise EtfNotReady(f"上交所 {sz_day} 的 ETF 份额还没发布（深交所已经是该日）")
    if not sz_rows:
        raise EtfNotReady(f"深交所 {sz_day} 的 ETF 份额为空")
    logger.info("ETF 交易所快照：%s（沪 %d 只 / 深 %d 只）", sz_day, len(sh_rows), len(sz_rows))
    return sh_rows + sz_rows
