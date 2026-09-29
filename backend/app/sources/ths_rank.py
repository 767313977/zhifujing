"""同花顺-数据中心「个股资金流排行」：东财快照不可用时的**涨幅榜备源**。

## 为什么拿资金流页面当涨幅榜

东财那条（`eastmoney.fetch_board_spot`）一次请求就够，但**这个 IP 有一份会耗尽的配额**
（连发几十次就进惩罚期，实测 > 10 分钟），云端尤其容易撞上；而涨幅榜是候选池里
优先级最高的一路，它一空、池子就从约 80 只掉到 40~55 只。所以要有第二条路。

同花顺这边两条路实测（2026-09-29）：

| 路 | 结果 |
| --- | --- |
| `q.10jqka.com.cn/index/index/board/cyb/field/zdf/...`（行情排行，本来一次给 20 行创业板涨幅榜） | **nginx 层 403**（回 `Nginx forbidden` + 客户端 IP；换 UA、换 cookie 都没用）→ 不可用 |
| `data.10jqka.com.cn/funds/ggzjl/field/zdf/order/desc/...`（个股资金流排行） | **可用**：服务端就按 `zdf`（涨跌幅）降序，50 行/页，含代码与涨跌幅两列 |

所以走第二条。它实质是一张**全市场**涨幅榜（第 1 页确实是 +653% 的新股、+20% 的 20cm
涨停…），要按板块 + 涨幅区间筛出我们要的，就得翻几页（实测 5 页左右能凑够 100 只）。
代价是几页请求，换来一个**与东财完全独立**的源。

## 三个必须记住的点

1. **必须带 `hexin-v` 头**，否则 401（实测：不带 401、带上 200）。它是同花顺前端反爬
   token，用 akshare 自带的 `ths.js` + `py_mini_racer` 跑出来的。**不是新引一个库** ——
   `py_mini_racer` 本来就是 akshare 的依赖，本站每天调的 `ak.stock_fund_flow_concept` /
   `_industry` 也走这套（它们能跑通，就说明这一套在云端可用）。
2. **页面是全市场、不是两块板**，所以板块过滤和「涨幅掉到区间下界以下就停」这两个
   终止条件都在这里做 —— 不做的话会一直翻到几百页。
3. **列号从表头读，不写死**。眼下是 序号/代码/简称/最新价/涨跌幅/…（涨跌幅在 index 4），
   但同花顺改版会挪列，所以按 `<th>` 的文字定位，找不到就报错而不是拿错列当涨幅。

⚠️ 这条路的**数据是资金流页面的表格**（HTML），比东财的 JSON 脆。同花顺一改版就可能
解析失败 —— 解析失败的后果是「这一路本轮为空 + 一条 warning」，不会影响别的源。
"""

import functools
import logging
import re

import requests

from app.config import Settings, get_settings
from app.sources.base import UA, TokenBucket, retry_call

logger = logging.getLogger(__name__)

URL = "http://data.10jqka.com.cn/funds/ggzjl/field/zdf/order/desc/page/{page}/ajax/1/free/1/"
_REFERER = "http://data.10jqka.com.cn/funds/ggzjl/"

# 单页超时。**故意不用 `settings.http_timeout`（默认 60 秒）**：这条路是兜底，
# 一次要翻好几页，每页 60 秒 × 重试 3 次会把整轮扫描拖死。东财那条同理，也自带 20 秒。
_TIMEOUT = 15.0

# 每页行数（实测 50）。只用来判断「短页 = 到底了」
PAGE_ROWS = 50
# 翻页上限。全市场榜里约一半是主板，凑 100 只实测要 5 页左右；给到 8 页留余量，
# 同时也是防死循环的兜底。
MAX_PAGES = 8

_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TH_RE = re.compile(r"<th[^>]*>(.*?)</th>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _text(html: str) -> str:
    return _TAG_RE.sub("", html).replace("&nbsp;", " ").strip()


def _hexin_v() -> str:
    """同花顺前端反爬 token；没有它 `data.10jqka.com.cn` 的数据接口回 401。"""
    import py_mini_racer
    from akshare.datasets import get_ths_js

    with open(get_ths_js("ths.js"), encoding="utf-8") as f:
        js = py_mini_racer.MiniRacer()
        js.eval(f.read())
    return js.call("v")


def _parse(html: str) -> list[tuple[str, float]]:
    """把一页表格解析成 `[(6 位代码, 涨跌幅%), …]`。列号按表头定位。"""
    head = re.search(r"<thead>(.*?)</thead>", html, re.S)
    if not head:
        raise RuntimeError("同花顺涨幅榜：响应里没有表头，页面结构可能改了")
    names = [_text(th) for th in _TH_RE.findall(head.group(1))]
    try:
        code_col = names.index("股票代码")
        pct_col = names.index("涨跌幅")
    except ValueError as exc:
        raise RuntimeError(f"同花顺涨幅榜：表头里没有「股票代码 / 涨跌幅」（实际 {names}）") from exc

    body = re.search(r"<tbody[^>]*>(.*?)</tbody>", html, re.S)
    out: list[tuple[str, float]] = []
    for row in _ROW_RE.findall(body.group(1) if body else html):
        cells = [_text(c) for c in _CELL_RE.findall(row)]
        if len(cells) <= max(code_col, pct_col):
            continue  # 表头行或结构不对的行
        code = cells[code_col].zfill(6)
        if not re.fullmatch(r"\d{6}", code):
            continue
        try:
            out.append((code, float(cells[pct_col].rstrip("%").strip())))
        except ValueError:
            continue  # 停牌等给 "-"
    return out


def fetch_board_spot(
    *,
    boards: tuple[str, ...],
    min_pct: float,
    max_pct: float,
    want: int,
    settings: Settings | None = None,
) -> list[tuple[str, float]]:
    """`boards` 开头的票里，涨幅在 `[min_pct, max_pct]` 内的，按涨幅降序前 `want` 只。

    `boards` 传代码前缀元组（如 `("300", "301", "302", "688", "689")`）而不是一个
    判定函数：源这一层不该知道「致富候选收哪些板」这条业务口径，只按给的前缀筛。

    服务端已按涨跌幅降序，所以**页内最小涨幅掉到 `min_pct` 以下就可以停** —— 后面的页
    只会更低。这一条是这条路能只翻几页而不是几百页的关键。

    网络或解析出错会抛（`retry_call` 重试完仍失败就往上抛），调用方按「这条源本轮为空」
    处理；**通了但没有符合的票**才返回空表 —— 两者分得开。
    """
    settings = settings or get_settings()
    bucket = TokenBucket(settings.ths_rate_limit)

    def _fetch(page: int) -> list[tuple[str, float]]:
        bucket.acquire()
        resp = requests.get(
            URL.format(page=page),
            headers={
                "User-Agent": UA,
                "Referer": _REFERER,
                "hexin-v": _hexin_v(),
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "text/html, */*; q=0.01",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return _parse(resp.text)

    got: list[tuple[str, float]] = []
    seen: set[str] = set()
    pages = 0
    for page in range(1, MAX_PAGES + 1):
        # 用 partial 而不是闭包：`def`/`lambda` 直接写在循环里会晚绑定 `page`
        # （ruff B023），partial 一次绑死，不留这个坑
        rows = retry_call(
            functools.partial(_fetch, page),
            retries=settings.http_retries,
            backoff=settings.http_backoff,
            description=f"同花顺涨幅榜 第 {page} 页",
        )
        pages = page
        if not rows:
            break
        for code, pct in rows:
            if code in seen:
                continue
            seen.add(code)
            if code.startswith(boards) and min_pct <= pct <= max_pct:
                got.append((code, pct))
        if min(p for _, p in rows) < min_pct:
            break  # 已经翻过区间下界，后面只会更低
        if len(got) >= want:
            break
        if len(rows) < PAGE_ROWS:
            break  # 短页 = 最后一页

    got.sort(key=lambda item: item[1], reverse=True)
    logger.info(
        "同花顺涨幅榜：翻 %d 页 / 扫过 %d 只，命中 %d 只（%s 开头、%.1f~%.1f%%），取前 %d",
        pages,
        len(seen),
        len(got),
        "/".join(boards),
        min_pct,
        max_pct,
        min(len(got), want),
    )
    return got[:want]
