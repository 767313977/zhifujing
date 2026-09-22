"""同花顺 iFinD MCP 客户端。

协议：JSON-RPC over HTTP（MCP Streamable HTTP）
    1. POST initialize  → 响应头返回 Mcp-Session-Id
    2. POST notifications/initialized
    3. POST tools/call

实测坑位（务必保持对应处理）：
- `symbols` 单次上限 10，**超出会被服务端静默丢弃且不报错**，必须自行分片
- 响应三层嵌套：MCP result → content[0].text(JSON 字符串) → data(又一个 JSON 字符串)
- 高频行情工具返回结构化 `tables`；自然语言工具返回 `answer`（Markdown 表格）
"""

import csv
import io
import json
import logging
import re
import threading
from datetime import date, timedelta
from typing import Any

import requests
import urllib3

from app.config import Settings, get_settings
from app.services.usage import record_call
from app.sources.base import TokenBucket, chunked, retry_call
from app.sources.markdown_table import parse_tables, to_int, to_text

logger = logging.getLogger(__name__)

# iFinD 使用自签或非标准证书链，官方参考实现同样关闭校验
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SERVERS = {
    "stock": "hexin-ifind-ds-stock-mcp",
    "fund": "hexin-ifind-ds-fund-mcp",
    "edb": "hexin-ifind-ds-edb-mcp",
    "news": "hexin-ifind-ds-news-mcp",
    "bond": "hexin-ifind-ds-bond-mcp",
    "global_stock": "hexin-ifind-ds-global-stock-mcp",
    "index": "hexin-ifind-ds-index-mcp",
    "future": "hexin-ifind-ds-futures-mcp",
}

# 单次请求的指标上限，与 symbols 同为 10
MAX_INDICATORS = 10

# 6 位代码 → 交易所后缀。akshare 返回裸代码，iFinD 需要带后缀的写法。
_SH_PREFIXES = ("60", "68", "90")
_BJ_PREFIXES = ("43", "83", "87", "88", "92")


def to_ths_symbol(code: str) -> str:
    """6 位股票代码转同花顺代码，如 600519 → 600519.SH。"""
    code = str(code).strip().zfill(6)
    if code.startswith(_SH_PREFIXES):
        return f"{code}.SH"
    if code.startswith(_BJ_PREFIXES):
        return f"{code}.BJ"
    return f"{code}.SZ"


def from_ths_symbol(symbol: str) -> str:
    """同花顺代码转 6 位裸代码，如 600519.SH → 600519。"""
    return str(symbol).strip().split(".")[0].zfill(6)


def normalize_code(raw: str) -> str:
    """把 600519 / 600519.SH / sh600519 等写法统一成 6 位数字。

    用户在选股结果、K 线页、手工输入等场景给出的写法不一，统一抽数字即可。
    不在这里做长度校验 —— 那是调用方的业务判断。
    """
    return "".join(ch for ch in (raw or "") if ch.isdigit())


class IfindError(RuntimeError):
    """iFinD 调用失败。"""


class IfindRateLimitError(IfindError):
    """iFinD 限流。属于**可重试**错误，交由 retry_call 退避后重试。"""


# iFinD 限流时 HTTP 状态码仍是 200，错误藏在工具结果文本里，只能按文本识别
_RATE_LIMIT_MARKERS = ("请求过于频繁", "status: 429")

# 结果超出表格上限（100 行）时，iFinD 会在回答里附一个**全量** CSV 下载链接。
# 走 o.thsi.cn，不受 5 req/s 限制，也不计 tools/call 配额。
#
# ⚠️ **但 CSV 不是每个工具都给**。`search_stocks`（选股）一直给；
# `get_stock_performance`（批量行情）在 2026-09-21 起不给了 ——
# 那天起它任何超过 100 行的请求都只回一张抽样表，回答末尾写
# 「数据被截断，目前无生成csv权限」。**抽样表看起来和完整数据一模一样**
# （同样的列名、同样的数值格式、日期也对），只有这句话能区分 ——
# 所以批量行情只能按「结果 ≤100 行」的形状去问，见 `jobs/collect_kline.py`。
_CSV_URL = re.compile(r"https?://\S+?\.csv")

# 表格只给了抽样数据的两种情况，都在回答正文里留一句话。
# 真正的问题是**上面那句「无生成csv权限」的文案是新出现的** ——
# 旧代码只认「数据过大」，于是 2026-09-21 那天起批量行情一直静默退回抽样表，
# 连续几天把残缺数据写进库而没有任何告警。凡是要解析这类回答的地方，
# 都必须把这里当成白名单来用，宁可多认一种文案。
_OVERSIZED_MARKERS = ("数据过大", "数据被截断")

# 结果集 CSV 的下载超时。实测 50 只 × 48 天约 2400 行不到 1 秒，
# 但首次建库时单次可能上万行，给足余量
CSV_TIMEOUT = 180.0


def _content_text(response: dict) -> str:
    """取出 MCP 响应的内容文本。"""
    content = (response.get("result") or {}).get("content") or []
    texts = [
        item["text"] for item in content if isinstance(item, dict) and item.get("text")
    ]
    if not texts:
        raise IfindError(
            f"iFinD 返回内容为空: {json.dumps(response, ensure_ascii=False)[:200]}"
        )
    return "\n".join(texts)


def _is_rate_limited(text: str) -> bool:
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


# 2026-09-21：几个只服务板块链路的成员被删掉了 —— `sector_data` / `sector_quotes`
# （概念板块当日兜底）、`board_members`（成分股名单）、`stock_themes`（个股 →
# 同花顺概念）以及配套的 `THEME_COLUMN` / `_pick_stock_code` / `_split_concepts`。
# 板块分类整体换成开盘红之后它们没有任何调用方了（见设计文档 8.32）。
# 这里留一条记录而不是静默删除：那些注释里记着 iFinD 的问句坑（「同花顺」前缀是噪音、
# 返回行标签是反的、100 行上限），将来若还要用 iFinD 问板块，值得先回来翻一眼。


def _parse_body(text: str) -> Any:
    """响应可能是纯 JSON，也可能是 SSE 帧，两种都要能解。"""
    text = (text or "").strip()
    if not text:
        return None
    if text[0] in "{[":
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[len("data:") :].strip()
            if payload and payload != "[DONE]":
                return json.loads(payload)
    raise IfindError(f"无法解析 iFinD 响应: {text[:200]}")


def _extract(response: dict) -> tuple[dict, dict]:
    """剥开三层嵌套，返回 (外层状态, 内层数据)。"""
    text = _content_text(response)
    if _is_rate_limited(text):
        raise IfindRateLimitError(f"iFinD 限流: {text[:120]}")

    try:
        outer = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IfindError(f"iFinD 返回无法解析: {text[:200]}") from exc

    code = outer.get("code")
    if code not in (None, 0, 1):
        raise IfindError(f"iFinD 返回错误 code={code} msg={outer.get('msg')}")

    inner = outer.get("data")
    if isinstance(inner, str):
        text = inner.strip()
        if not text:
            inner = {}
        else:
            try:
                inner = json.loads(text)
            except json.JSONDecodeError:
                # 有些工具的 data 直接就是 Markdown 正文而不是嵌套 JSON
                # （实测 get_stock_summary），此时把它当成 answer
                inner = {"answer": text}
    return outer, inner or {}


def _rows_from_tables(tables: list) -> list[dict]:
    """高频行情工具的 tables 形如 [[表头...], [行...], ...]，转成 list[dict]。"""
    if not tables or len(tables) < 2:
        return []
    header = [str(col).strip() for col in tables[0]]
    rows: list[dict] = []
    for raw in tables[1:]:
        if not isinstance(raw, list):
            continue
        if len(raw) < len(header):
            raw = list(raw) + [None] * (len(header) - len(raw))
        rows.append(dict(zip(header, raw)))
    return rows


class IfindClient:
    """iFinD 数据源。线程安全（限速器与会话均有锁保护）。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.ifind_auth_token:
            raise IfindError("缺少 IFIND_AUTH_TOKEN，请配置项目根目录 .env")
        self._bucket = TokenBucket(self.settings.ifind_rate_limit)
        self._sessions: dict[str, str] = {}
        self._ids: dict[str, int] = {}
        self._lock = threading.Lock()
        self._http = requests.Session()

    # ------------------------------------------------------------------ 底层

    def _next_id(self, server: str) -> int:
        with self._lock:
            self._ids[server] = self._ids.get(server, 0) + 1
            return self._ids[server]

    def _headers(self, server: str) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": self.settings.ifind_auth_token,
        }
        session_id = self._sessions.get(server)
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        return headers

    def _post(self, server: str, payload: dict) -> tuple[requests.Response, Any]:
        self._bucket.acquire()
        url = f"{self.settings.ifind_base_url}/{SERVERS[server]}"
        response = self._http.post(
            url,
            json=payload,
            headers=self._headers(server),
            verify=False,
            timeout=self.settings.http_timeout,
        )
        return response, _parse_body(response.text)

    def _ensure_session(self, server: str) -> None:
        with self._lock:
            if server in self._sessions:
                return

        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(server),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "fupan", "version": "0.1.0"},
            },
        }
        response, _ = self._post(server, payload)
        response.raise_for_status()

        session_id = response.headers.get("Mcp-Session-Id")
        if not session_id:
            raise IfindError(f"{server} initialize 未返回 Mcp-Session-Id")

        with self._lock:
            self._sessions[server] = session_id

        self._post(server, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        logger.debug("iFinD %s 会话已建立", server)

    def _drop_session(self, server: str) -> None:
        with self._lock:
            self._sessions.pop(server, None)

    def call(self, server: str, tool: str, params: dict) -> dict:
        """调用一个 MCP 工具，返回原始 MCP 响应。"""
        if server not in SERVERS:
            raise IfindError(f"未知 server_type: {server}")

        def _do() -> dict:
            self._ensure_session(server)
            payload = {
                "jsonrpc": "2.0",
                "id": self._next_id(server),
                "method": "tools/call",
                "params": {"name": tool, "arguments": params},
            }
            try:
                response, body = self._post(server, payload)
                # 一次 _post 就是一次真实发出、可能被计费的 tools/call。
                # 计数点必须在这里（_do 之内、retry_call 之内）而不是 call() 外层：
                # 外层只数到「一次调用」，把重试全漏掉，而重试同样在花配额。
                # 漏记会让配额守卫低估用量，低估是危险的方向。
                # 计量是数据源层的唯一一处例外：它必须与真实请求同生共死。
                record_call(server, tool)
                if response.status_code == 429:
                    raise IfindRateLimitError(f"iFinD HTTP 429: {response.text[:120]}")
                if isinstance(body, dict) and "error" in body:
                    raise IfindError(
                        f"{tool} 返回错误: {json.dumps(body['error'], ensure_ascii=False)}"
                    )
                response.raise_for_status()

                # 限流时 HTTP 仍是 200，错误在工具结果文本里。必须在此处（重试范围内）
                # 判定，否则 _extract 在重试之外，429 会被当成解析失败而不重试。
                text = _content_text(body)
                if _is_rate_limited(text):
                    raise IfindRateLimitError(f"iFinD 限流: {text[:120]}")
                return body or {}
            except IfindRateLimitError:
                # 限流只退避，不丢弃会话
                raise
            except Exception:
                # 会话可能已过期，丢弃以便下次重试时重新握手
                self._drop_session(server)
                raise

        return retry_call(
            _do,
            retries=self.settings.http_retries,
            backoff=self.settings.http_backoff,
            description=f"iFinD {server}.{tool}",
        )

    # ------------------------------------------------- 结构化行情（实时/高频）

    def _highfreq(
        self,
        server: str,
        tool: str,
        symbols: list[str],
        indicators: list[str],
        data_mode: str,
        interval: int | None,
    ) -> list[dict]:
        symbol_list = [s.strip() for s in symbols if s and s.strip()]
        if not symbol_list:
            return []
        if data_mode not in {"real_time", "highfreq"}:
            raise IfindError(f"data_mode 只能是 real_time 或 highfreq，收到 {data_mode}")
        if len(indicators) > MAX_INDICATORS:
            raise IfindError(f"indicators 上限 {MAX_INDICATORS}，当前 {len(indicators)}")
        if data_mode == "highfreq" and interval is None:
            raise IfindError("data_mode=highfreq 时必须指定 interval")

        rows: list[dict] = []
        batches = chunked(symbol_list, self.settings.ifind_max_symbols)
        if len(batches) > 1:
            logger.info("%s 分 %d 片请求（单次上限 %d 只）", tool, len(batches), self.settings.ifind_max_symbols)

        for batch in batches:
            params: dict[str, Any] = {
                "symbols": ",".join(batch),
                "indicators": ",".join(indicators),
                "data_mode": data_mode,
            }
            if interval is not None:
                params["interval"] = interval
            _, inner = _extract(self.call(server, tool, params))
            rows.extend(_rows_from_tables(inner.get("tables") or []))
        return rows

    def index_quotes(
        self,
        symbols: list[str],
        indicators: list[str],
        data_mode: str = "real_time",
        interval: int | None = None,
    ) -> list[dict]:
        """指数实时快照 / 高频序列。含上涨家数、涨停家数、跌停家数。"""
        return self._highfreq("index", "index_highfreq_quotes", symbols, indicators, data_mode, interval)

    def stock_quotes(
        self,
        symbols: list[str],
        indicators: list[str],
        data_mode: str = "real_time",
        interval: int | None = None,
    ) -> list[dict]:
        """个股实时快照 / 高频序列。自动按 10 只分片。"""
        return self._highfreq("stock", "stock_highfreq_quotes", symbols, indicators, data_mode, interval)

    # ------------------------------------------------------- 自然语言取数

    def _nl(self, server: str, tool: str, params: dict) -> tuple[str, list[dict]]:
        """执行自然语言工具，返回 (原始回答, 合并后的结果行)。

        注意：一次回答可能包含多张 Markdown 表（如 sector_data），
        这里全部展平；调用方若需区分表格请直接用 parse_tables。
        """
        _, inner = _extract(self.call(server, tool, params))
        answer = inner.get("answer") or ""
        rows = [row for table in parse_tables(answer) for row in table]
        return answer, rows

    def search_stocks(self, query: str) -> dict:
        """智能选股。这是找票的主入口。

        返回结构里的元信息文字极易误读，两处都已实测确认：

        - `selectedSecuritiesCount` 是**匹配总数**，不是返回条数。
          实测「市值大于100亿的股票」该值为 1801，而 markdown 表格只给出 100 行。
        - `dataTotalVolume` 是**数据格子数**（行数 × 列数），根本不是匹配数。
          实测 9 行 5 列 = 45、4 行 5 列 = 20，完全吻合。

        表格上限 100 行，超出会静默截断，且回答里的提示文案是固定模板
        （写着「只返回前1000行结果」但实际是 100 行），**不能拿文案判断**。
        判断是否被截断只能用 `matched > returned`。
        """
        _, inner = _extract(self.call("stock", "search_stocks", {"query": query}))
        answer = inner.get("answer") or ""
        tables = parse_tables(answer)
        rows = [row for table in tables for row in table]
        matched = to_int(inner.get("selectedSecuritiesCount"))
        return {
            "answer": answer,
            "rows": rows,
            "columns": list(rows[0].keys()) if rows else [],
            "matched": matched,
            "returned": len(rows),
            "truncated": matched is not None and matched > len(rows),
        }

    def stock_performance(self, query: str) -> tuple[str, list[dict]]:
        """个股日频历史行情与技术指标。返回结果含非交易日，需按交易日历过滤。"""
        return self._nl("stock", "get_stock_performance", {"query": query})

    def fund_profile(self, query: str) -> tuple[str, list[dict]]:
        """基金资料：份额 / 规模 / 净值 / 行情价（收盘价、成交额、涨跌幅）。

        2026-09-22 实测两个坑，调用方都要防：

        1. **一次只回一张表**。问四五个指标没问题（实测「基金份额、收盘价、
           成交额、涨跌幅」一起问能全部拿到），但一次问八个指标就只剩「净值日期」
           那张表、份额整列消失 —— 所以指标别贪多。
        2. **批量过大会整批返回空，而且不带任何提示**。实测一批 80 只正常返回
           80 行，一批 100 只返回 **0 行**，回答里也没有「以下为部分数据」这类
           提示 —— 所以调用方必须自己按批校验行数（见 `collect_funds.ETF_BATCH`）。
        """
        return self._nl("fund", "get_fund_profile", {"query": query})

    def stock_history(self, symbol: str, start: date, end: date) -> list[dict]:
        """个股日频 OHLCV。这是唯一能取历史行情的方式（高频接口只给当日）。

        实测三个坑，调用方都要处理：

        1. **返回的是日历日**，含周末。实测「近10个交易日」返回 15 行，
           其中夹着周六周日，且非交易日的值为空。必须按交易日历过滤。
        2. **区间过大时不是「砍掉末尾」而是抽样丢弃**。实测请求 200 个日历日
           被压成 100 行，且 139 个交易日里缺了 69 个、散布在整个区间上 ——
           画出来是带静默空洞的 K 线，肉眼根本看不出来。
           实测 90 个日历日以内完整，故调用方必须分块并逐块校验完整性。
        3. **列顺序不稳定**：同一个问法两次调用返回的列序都不同
           （收盘价一次在中间、一次在开头），只能按列名取值，不能按位置。
        """
        query = (
            f"{symbol} 从{start.isoformat()}到{end.isoformat()}"
            f"每个交易日的开盘价、最高价、最低价、收盘价、成交量、成交额、涨跌幅"
        )
        _, rows = self._nl("stock", "get_stock_performance", {"query": query})
        return rows

    def search_stocks_full(self, query: str) -> tuple[int | None, list[dict]]:
        """选股结果的**全量**行，配合 `matched` 一起返回。

        表格只给 100 行，CSV 上限 1000 行 —— 全 A 有 5569 只，所以调用方
        必须按代码前缀分段，保证每段都落在 1000 行以内，并用 `matched`
        校验该段确实拿全了（拿到 1000 行而 matched 更大，说明这段没取完）。
        """
        result = self.search_stocks(query)
        return result["matched"], self._prefer_csv(result["answer"], result["rows"])

    def _prefer_csv(self, answer: str, fallback: list[dict]) -> list[dict]:
        """结果集优先取 CSV 全量，CSV 不可用时退回表格。

        退回的那张表**可能是抽样过的 100 行**，所以调用方必须拿 `matched` 与
        `len(rows)` 对账。这里先把「为什么没有 CSV」打进日志 ——
        否则对账报错时，还要再猜一遍原因。
        """
        match = _CSV_URL.search(answer or "")
        if not match:
            if any(mark in (answer or "") for mark in _OVERSIZED_MARKERS):
                logger.warning("iFinD 未提供 CSV（结果被截断），退回表格：可能只有 100 行")
            return fallback
        rows = self._download_csv(match.group(0))
        if rows:
            return rows
        logger.warning("下载 iFinD 结果 CSV 失败，退回表格（可能被 100 行截断）")
        return fallback

    def _download_csv(self, url: str) -> list[dict]:
        """下载结果集 CSV。

        不占用 tools/call 配额（走 o.thsi.cn 这个静态资源域名），
        所以它既不受 5 req/s 限速，也**不计入调用次数**。
        """
        try:
            response = requests.get(url, verify=False, timeout=CSV_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("下载 iFinD 结果 CSV 失败：%s", exc)
            return []
        # 用 utf-8-sig：iFinD 的 CSV 带 BOM，第一列名会是 "\ufeff股票代码"，
        # 不解掉的话按列名取值全部落空
        text = response.content.decode("utf-8-sig", errors="replace")
        return [dict(row) for row in csv.DictReader(io.StringIO(text))]

    def index_data(self, query: str) -> tuple[str, list[dict]]:
        """指数行情、技术指标与估值指标。"""
        return self._nl("index", "index_data", {"query": query})

    def edb_data(self, query: str) -> tuple[str, list[dict]]:
        """宏观 / 行业经济指标（EDB）。两融、北向成交额等市场级数据从这里取。

        问句里要写**同花顺的原始指标名**（如「上交所:融资买入额」），
        否则模糊匹配会给出别的指标 —— 实测问「融资余额」会返回工商银行的
        个股融资余额，而不是市场总额。

        返回的 Markdown 表格里列名带单位后缀（形如
        `上交所:融资买入额（单位：亿元）`），取值时按列名前缀匹配更稳。
        """
        return self._nl("edb", "get_edb_data", {"query": query})

    def stock_summary(self, query: str) -> tuple[str, list[dict]]:
        return self._nl("stock", "get_stock_summary", {"query": query})

    def stock_financials(self, query: str) -> tuple[str, list[dict]]:
        return self._nl("stock", "get_stock_financials", {"query": query})

    def search_news(
        self,
        query: str,
        time_start: str | None = None,
        time_end: str | None = None,
        size: int = 10,
    ) -> str:
        params: dict[str, Any] = {"query": query, "size": size}
        if time_start:
            params["time_start"] = time_start
        if time_end:
            params["time_end"] = time_end
        answer, _ = self._nl("news", "search_news", params)
        return answer

    def search_notice(
        self,
        query: str,
        time_start: str | None = None,
        time_end: str | None = None,
        size: int = 10,
    ) -> str:
        params: dict[str, Any] = {"query": query, "size": size}
        if time_start:
            params["time_start"] = time_start
        if time_end:
            params["time_end"] = time_end
        answer, _ = self._nl("news", "search_notice", params)
        return answer
