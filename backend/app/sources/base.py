"""数据源通用基座：限速、重试、请求头。

外部接口的脆弱性集中在这一层，上层业务不感知限流与重试细节。
"""

import logging
import threading
import time
from collections.abc import Callable
from typing import TypeVar

import requests

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
REFERER = "https://www.10jqka.com.cn/"

T = TypeVar("T")


class TokenBucket:
    """线程安全令牌桶，用于遵守数据源的每秒请求上限。

    默认 capacity=1，即**不预留突发额度**，请求按 1/rate 秒均匀放出。
    iFinD 实测对突发敏感：一次性放行 4 个请求会触发 429「用户请求过于频繁」，
    均匀发送则稳定通过。
    """

    def __init__(self, rate: float, capacity: float = 1.0) -> None:
        if rate <= 0:
            raise ValueError("rate 必须为正数")
        if capacity <= 0:
            raise ValueError("capacity 必须为正数")
        self.rate = rate
        self.capacity = capacity
        self._tokens = capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait = (tokens - self._tokens) / self.rate
            time.sleep(wait)


def is_deterministic_http_error(exc: Exception) -> bool:
    """这个异常是不是「换下一秒重发也还是同一个结果」的确定性 HTTP 错误。

    目前只认 4xx（408 请求超时、429 限流除外 —— 那两个是明确的「等会儿再来」）。
    5xx 与网络层异常一律不算确定性错误，照旧重试。

    为什么要把这条抽出来（2026-10-28 体检）：开盘红与同花顺涨停池的 `retry_call`
    原来都是默认参数（**一律重试**），而这两个来源在参数写错 / 接口改版时会稳定
    返回 400 / 404 —— 每次都要白等 2+4 秒、并发时还白占令牌桶。iFinD 与 akshare
    的龙虎榜早就按错误类型判过了，这里把同一套判定给到这两条链路。
    """
    if not isinstance(exc, requests.HTTPError):
        return False
    response = exc.response
    if response is None:
        return False
    code = response.status_code
    return 400 <= code < 500 and code not in (408, 429)


def retry_call(
    fn: Callable[[], T],
    *,
    retries: int = 3,
    backoff: float = 2.0,
    description: str = "请求",
    # 异常类型 / 类型元组 / 回调 (exc) -> bool；None 表示保持旧行为「一律重试」
    retry_on: "type[Exception] | tuple[type[Exception], ...] | Callable[[Exception], bool] | None" = None,
) -> T:
    """指数退避重试：2s → 4s → 8s。

    akshare 的东财接口存在偶发 RemoteDisconnected，iFinD 也偶有超时，
    都靠这层兜住。

    `retry_on` 决定哪些异常才重试：
    - None（默认）：**一律重试**，保持旧行为 —— 没有区分错误类型的数据源照旧；
    - 异常类型 / 元组：只重试这些类型；
    - 回调：按 `retry_on(exc)` 的真假决定。
    参数错 / 认证错 / 「上游还没发布」这类**确定性错误**重试多少次都是同一个结果，
    只会白持锁、白等退避、白发一次计费请求，数据源应显式传入判定（见 ifind /
    akshare 的龙虎榜）。拿不准则留给默认（都重试），宁可慢也别漏采。
    """
    if retries < 1:
        # 配置成 0 的话循环一次都不进、`last_exc` 停在 None，下面那句 assert 会抛
        # **AssertionError** —— 那种报错完全看不出是配置问题（2026-09-27 修）
        raise ValueError(f"retries 必须 ≥ 1，收到 {retries}")

    def _retryable(exc: Exception) -> bool:
        if retry_on is None:
            return True
        if isinstance(retry_on, type) or isinstance(retry_on, tuple):
            return isinstance(exc, retry_on)
        return bool(retry_on(exc))

    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 需要捕获任意数据源异常并重试
            last_exc = exc
            # 确定性错误立刻上抛：不白等退避，也不再发第二次（会被计费的）请求。
            # 最后一次失败同样不必 sleep，直接抛
            if not _retryable(exc) or attempt == retries - 1:
                raise
            delay = backoff * (2**attempt)
            logger.warning(
                "%s 第 %d/%d 次失败: %s，%.1fs 后重试",
                description,
                attempt + 1,
                retries,
                exc,
                delay,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def chunked(items: list[T], size: int) -> list[list[T]]:
    """按固定大小分片。iFinD 单次 symbols 上限 10，超出会被静默丢弃。"""
    if size <= 0:
        raise ValueError("size 必须为正数")
    return [items[i : i + size] for i in range(0, len(items), size)]
