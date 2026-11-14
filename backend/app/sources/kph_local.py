"""开盘红**本地数据服务**（`StockDataServer.exe`）客户端。

⚠️ 与 `sources/kaipanhong.py` 不是一回事（两个都叫「开盘红」）：

- `kaipanhong.py`：**直连开盘红的 App 接口**（`apphwshhq` / `apphis`），游客态即可，
  目前用于板块排行 / 成分股 / 涨停天梯；
- 本文件：**直连内网里的包装程序**（说明书 v2.7.2）。它把开盘红的 mTLS 长连接、
  腾讯/东财代理收成 HTTP JSON，默认 `http://127.0.0.1:5000`，**没有任何鉴权**，
  只能本机 / 内网用；云端 VPS 一般访问不到，所以整条链路默认关闭
  （`settings.kph_local_enabled`）。

为什么还要多这一层：打板池、竞价、龙虎榜、市场情绪这几类只有包装器里有，
直连 App 接口要么没有、要么要付费账号。代价是它必须在那台机器上跑着。

响应外层统一是：

    {"success": true, "response": {"cmd_id": "08 3F", "data": {...}}}

业务数据在 `response.data`；`success=false` 或 `data` 缺失一律抛 `KphLocalError`，
**不返回半个空字典** —— 「取不到」和「今天没有数据」必须能区分，否则上层会把
一次故障当成「今天情绪为空」记成成功。

`/api/status` 是**例外**：它没有外层包装（直接 `{"connected": ..., ...}`），
所以单独一个 `status()`。
"""

import logging

import requests

from app.config import Settings, get_settings
from app.external_guard import ensure_external_fetch
from app.sources.base import TokenBucket, is_deterministic_http_error, retry_call

logger = logging.getLogger(__name__)


class KphLocalError(RuntimeError):
    """本地服务不可用 / 返回失败。调用方按「这次没取到」处理，不要写库。"""


class KphLocalSource:
    """开盘红本地服务的只读客户端（线程安全：限速器自带锁）。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._bucket = TokenBucket(self.settings.kph_rate_limit)

    @property
    def base(self) -> str:
        return self.settings.kph_local_base.rstrip("/")

    def _request_json(
        self, path: str, params: dict | None = None, *, description: str, timeout: float | None = None
    ) -> dict:
        """发一次 GET 并返回原始 JSON（不做外层解包）。"""
        ensure_external_fetch(f"开盘红本地服务 {description}")

        def _do() -> dict:
            self._bucket.acquire()
            response = requests.get(
                f"{self.base}{path}",
                params=params or {},
                timeout=timeout or self.settings.kph_local_timeout,
            )
            response.raise_for_status()
            return response.json()

        return retry_call(
            _do,
            retries=self.settings.http_retries,
            backoff=self.settings.http_backoff,
            description=description,
            # 4xx（408/429 除外）是确定性错误：路径写错 / 服务版本不对，重发也是同一个结果
            retry_on=lambda exc: not is_deterministic_http_error(exc),
        )

    def _data(self, path: str, params: dict | None, *, description: str) -> dict:
        """mTLS 类接口：解包到 `response.data`。"""
        payload = self._request_json(path, params, description=description)
        if payload.get("success") is False:
            raise KphLocalError(f"{description} 返回失败：{payload.get('message')}")
        response = payload.get("response")
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, dict):
            raise KphLocalError(f"{description} 的 response.data 不是对象")
        return data

    # ------------------------------------------------------------ 接口

    def status(self) -> dict:
        """健康检查（**没有**外层包装，5 秒足够）。

        业务接口之前先看 `connected`：false 说明 mTLS 断了（说明书 §7.2：某个接口
        超时后整条链路会失效，要重启 exe），这时所有 mTLS 类接口都会失败。
        """
        return self._request_json(
            "/api/status", description="开盘红本地服务状态", timeout=min(self.settings.kph_local_timeout, 5)
        )

    def market_sentiment(self) -> dict:
        """市场情绪（**一次性版**，订阅 → 取一帧 → 自动退订）。

        不要换成 `/api/get_market_sentiment`：那是订阅型，调用后服务端会周期性推送，
        没人管就会一直占着订阅名额。
        """
        return self._data(
            "/api/get_market_sentiment_once",
            {"type": 0},
            description="开盘红市场情绪",
        )

    def zd_stat(self) -> dict:
        """涨跌统计：涨/跌家数、涨停/跌停家数、以及 -10…10 的档位分布。"""
        return self._data("/api/get_zd_stat", None, description="开盘红涨跌统计")
