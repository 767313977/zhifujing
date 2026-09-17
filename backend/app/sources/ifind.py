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

import json
import logging
import threading
from typing import Any

import requests
import urllib3

from app.config import Settings, get_settings
from app.sources.base import TokenBucket, chunked, retry_call
from app.sources.markdown_table import parse_tables

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


class IfindError(RuntimeError):
    """iFinD 调用失败。"""


class IfindRateLimitError(IfindError):
    """iFinD 限流。属于**可重试**错误，交由 retry_call 退避后重试。"""


# iFinD 限流时 HTTP 状态码仍是 200，错误藏在工具结果文本里，只能按文本识别
_RATE_LIMIT_MARKERS = ("请求过于频繁", "status: 429")


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
        inner = json.loads(inner) if inner.strip() else {}
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

    def search_stocks(self, query: str) -> tuple[str, list[dict]]:
        """智能选股。这是找票的主入口。"""
        return self._nl("stock", "search_stocks", {"query": query})

    def stock_performance(self, query: str) -> tuple[str, list[dict]]:
        """个股日频历史行情与技术指标。返回结果含非交易日，需按交易日历过滤。"""
        return self._nl("stock", "get_stock_performance", {"query": query})

    def index_data(self, query: str) -> tuple[str, list[dict]]:
        """指数行情、技术指标与估值指标。"""
        return self._nl("index", "index_data", {"query": query})

    def sector_data(self, query: str) -> tuple[str, list[dict]]:
        """板块行情、成分股指标。

        注意：板块代码体系混用（中信行业分类 / 同花顺行业类），
        query 里应显式指明分类，否则跨日不可比。
        """
        return self._nl("index", "sector_data", {"query": query})

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
