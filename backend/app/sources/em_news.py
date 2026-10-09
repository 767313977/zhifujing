"""个股新闻：**东财个股新闻**（搜索接口的「最近 N 条」）。

传输直接借 akshare 的 `stock_news_em`（单次请求、不在 `push2` 集群、没有 IP 配额问题），
这里只管**口径**：把它的 6 列归一成 5 个字段、去掉高亮标签、按发布时间倒序截断。

## 为什么不像 `eastmoney.py` / `tencent.py` 那样自己发 HTTP

那条请求的 `param` 里要带浏览器 cookie（`qgqp_b_id` 等）。本机实测（2026-10-09）：
**不带 cookie 时服务端会返回另一批结果** —— `hitsTotal=1`、`result` 里是 `passportWeb`
（一个用户资料）而不是 `cmsArticleWebOld`，`n=0`；换成 jQuery 风格的回调名也一样。
akshare 那份请求头里的 cookie 是现成的，所以先用它。

真到了它失效的那天（它把回调名硬编码在两处，服务端一换格式就会解析失败），
再自己发一次、把 akshare 那段头照抄过来 —— 探针脚本见设计文档 §8.86。
"""

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 标题/正文里被高亮的命中词包在 `<em>` 里（接口的 preTag/postTag），展示时要去掉。
_TAG = re.compile(r"<[^>]+>")

#: 摘要截断长度。东财给的是正文前一两句，偶尔很长 —— 一行放不下就截断，
#: 不截的话一个小区块会被一条长新闻撑开。
SUMMARY_CHARS = 120


@dataclass(frozen=True)
class NewsItem:
    """一条新闻。字段名与前端一一对应，别在中途改叫法。"""

    published_at: str  # "2026-08-26 09:59:14"（东财原样给，不做时区换算）
    title: str
    summary: str
    source: str  # 文章来源，如「界面新闻」「证券时报网」
    url: str


def _clean(text: object) -> str:
    return _TAG.sub("", str(text or "")).replace("\u200b", "").strip()


def _link(url: object) -> str:
    """新闻链接。

    东财给的是 `http://finance.eastmoney.com/a/xxxx.html` —— 本站是 https，
    浏览器实测这条链接能用，但站内出现 http 链接没必要（新窗口打开会有不安全提示）。
    同一个地址换成 https 实测 200，所以统一升到 https；不是东财那个域名的原样返回，
    别把别家的链接也改了。
    """
    text = _clean(url)
    if text.startswith("http://finance.eastmoney.com/"):
        return "https://" + text[len("http://") :]
    return text


def fetch_stock_news(code: str, *, name: str | None = None, limit: int = 20) -> list[NewsItem]:
    """这只票最近的 `limit` 条新闻，按**相关性 + 时间**排序。

    取不到（接口变动 / 网络）就抛，由调用方决定怎么显示 —— 空列表与「源挂了」
    是两回事：前者是「真没搜到」（小票、次新常见），后者要能在日志里看见。

    ⚠️ **排序不是纯按时间**：东财的搜索是**全文匹配**，于是「创业板最新筹码集中股名单」
    「股东户数降幅榜」这类**只在正文表格里列到代码**的稿子也算命中，而且它们天天发、
    常常最新 —— 纯按时间排，一只票的新闻面会被这种稿子占满（实测 300654 前 5 条全是）。
    所以先按时间倒序，再把「**标题里出现名称或代码**」的稳定排到前面：
    没有一条标题命中时，顺序就等于纯按时间（不会更糟）。
    """
    import akshare as ak

    df = ak.stock_news_em(symbol=str(code).zfill(6))
    if df is None or getattr(df, "empty", True):
        return []

    rows: list[NewsItem] = []
    for rec in df.to_dict("records"):
        title = _clean(rec.get("新闻标题"))
        if not title:
            continue
        summary = _clean(rec.get("新闻内容"))
        if len(summary) > SUMMARY_CHARS:
            summary = summary[:SUMMARY_CHARS] + "…"
        rows.append(
            NewsItem(
                published_at=_clean(rec.get("发布时间")),
                title=title,
                summary=summary,
                source=_clean(rec.get("文章来源")),
                url=_link(rec.get("新闻链接")),
            )
        )

    # 两次稳定排序：先时间倒序，再把标题命中的那批提到前面（组内仍是时间倒序）
    rows.sort(key=lambda item: item.published_at, reverse=True)
    rows.sort(key=lambda item: 0 if _mentions(item.title, code, name) else 1)
    return rows[:limit]


def _mentions(title: str, code: str, name: str | None) -> bool:
    """标题里有没有这只票 —— 名称（去掉 ST 前缀）或 6 位代码任一出现即可。"""
    code = str(code).zfill(6)
    if code in title:
        return True
    if not name:
        return False
    bare = name.replace("*", "").strip()
    return bool(bare) and bare in title

