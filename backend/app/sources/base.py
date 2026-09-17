"""数据源通用基座：限速、重试、请求头。

外部接口的脆弱性集中在这一层，上层业务不感知限流与重试细节。
"""

import logging
import threading
import time
from collections.abc import Callable
from typing import TypeVar

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


def retry_call(
    fn: Callable[[], T],
    *,
    retries: int = 3,
    backoff: float = 2.0,
    description: str = "请求",
) -> T:
    """指数退避重试：2s → 4s → 8s。

    akshare 的东财接口存在偶发 RemoteDisconnected，iFinD 也偶有超时，
    都靠这层兜住。
    """
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 需要捕获任意数据源异常并重试
            last_exc = exc
            if attempt < retries - 1:
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
