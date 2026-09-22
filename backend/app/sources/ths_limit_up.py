"""同花顺涨停池：**涨停原因**（`reason_type`）。

「涨停原因」在开盘红那边拿不到：它的免费接口（`GetZhangTingTianTi`）实测 11 个字段
全看过，没有原因文本 —— `ZhuShuList` 看着像「注释」，实际是按板块聚合的成员代码表；
App 里那个「涨停原因」是付费功能。所以换来源：同花顺**数据中心**的涨停池接口带
`reason_type`，正是它页面上显示的那串，例如：

    金融街   房地产+城市更新+北京国资
    永和智控 水暖阀门+医疗器械+资产出售+业绩扭亏
    鼎阳科技 测量仪器+自研芯片+高端新品

实测（2026-09-22 探）：

- 一次请求给全当日涨停股：09-21 共 101 只（站内涨停池 103 只，差 2 只，正常）
- **历史回溯到 2025-09-22 附近**：再往前 `status_code=-1`（超出范围）；
  周末与节假日是 `status_code=0` + `total=0`，**不是错误**
- 不需要登录或 cookie，但**必须带 UA 与 Referer**，否则会被挡
- `limit=200` 一页装得下当日全部（101 只），所以正常不会翻页；仍写了翻页兜底

⚠️ **口径提醒**：这是**同花顺**的涨停原因，与开盘红 / 开盘啦无关，也不是本站的
「板块」。`reason_type` 里的分隔符是 `+`，里面混着行业、事件、地域（如「北京国资」），
展示时照原样给，不要拆开当成三个字段去用。
"""

import logging
from datetime import date

import requests

from app.config import Settings, get_settings
from app.sources.base import UA, TokenBucket, retry_call

logger = logging.getLogger(__name__)

URL = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"

# 照抄浏览器请求的参数，**一个都别删**：`field` 的编号列表决定接口返回哪些列，
# 删掉对应编号，响应里就没有那个 key（`reason_type` 正是靠它带出来的）。
FIELD = (
    "199112,10,9001,330323,330324,330325,9002,330329,"
    "133971,133970,1968584,3475914,9003,9004"
)
FILTER = "HS,GEM2STAR"
ORDER_FIELD = "330324"
# 单页上限。当日涨停很少超过 200 只，所以正常情况下一次就取全。
PAGE_SIZE = 200
# 翻页上限，只用来防死循环
MAX_PAGES = 5
# 超出接口覆盖范围的日期返回这个值
STATUS_OUT_OF_RANGE = -1

_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://data.10jqka.com.cn/",
}


class ThsLimitUpSource:
    """同花顺涨停池（只取涨停原因）。线程安全（限速器有锁保护）。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        # 与 q.10jqka.com.cn 共用 ths_rate_limit：同一个站点族，实测 3/s 未被限
        self._bucket = TokenBucket(self.settings.ths_rate_limit)

    def _get(self, trade_date: date, page: int) -> dict:
        params = {
            "page": page,
            "limit": PAGE_SIZE,
            "field": FIELD,
            "filter": FILTER,
            "order_field": ORDER_FIELD,
            "order_type": "0",
            "date": trade_date.strftime("%Y%m%d"),
        }

        def _do() -> dict:
            self._bucket.acquire()
            response = requests.get(
                URL,
                params=params,
                headers=_HEADERS,
                timeout=self.settings.http_timeout,
            )
            response.raise_for_status()
            return response.json()

        return retry_call(
            _do,
            retries=self.settings.http_retries,
            backoff=self.settings.http_backoff,
            description=f"同花顺涨停池 {trade_date}",
        )

    def limit_reasons(self, trade_date: date) -> list[dict]:
        """当日涨停原因：`{code, name, reason}`，按接口给的原顺序。

        **超出覆盖范围**（早于 2025-09 附近）与**当天真没有涨停**（周末、节假日）
        都会返回空列表 —— 两者分不开，但调用方按日期就知道是哪一种，所以这里只把
        前者记一条 warning，不当异常抛：回补脚本跑到区间边缘时不该整个中断。

        空原因（`reason_type` 为空的个股）**不丢行**：仍返回该股、`reason` 为空串，
        让上层能区分「接口没给」与「这只票不在接口里」。
        """
        rows: list[dict] = []
        seen: set[str] = set()
        total: int | None = None

        for page in range(1, MAX_PAGES + 1):
            payload = self._get(trade_date, page)
            status = payload.get("status_code")
            if status == STATUS_OUT_OF_RANGE or str(status) == str(STATUS_OUT_OF_RANGE):
                logger.warning(
                    "%s 超出同花顺涨停池的覆盖范围（status_code=-1，只回溯到 2025-09 附近）",
                    trade_date,
                )
                return []

            data = payload.get("data") or {}
            info = data.get("info") or []
            total = (data.get("page") or {}).get("total")
            for item in info:
                code = str(item.get("code") or "").strip()
                # 越界页可能重复吐尾部（开盘红成分股踩过同样的坑），按代码去重
                if not code or code in seen:
                    continue
                seen.add(code)
                rows.append(
                    {
                        "code": code,
                        "name": str(item.get("name") or "").strip(),
                        "reason": str(item.get("reason_type") or "").strip(),
                    }
                )

            # 停法是**短页**：`total` 只用于日志对账，不用它截断 ——
            # 接口在越界时也可能给出一个看着合理的 total
            if len(info) < PAGE_SIZE:
                break

        if not rows:
            # 周末 / 节假日走到这里。留一行日志，否则「回补了 0 行」看不出是哪种情况
            logger.info(
                "%s 同花顺涨停池没有数据（total=%s），非交易日时段属正常", trade_date, total
            )
            return []

        logger.info(
            "%s 同花顺涨停原因 %d 行（接口 total=%s）", trade_date, len(rows), total
        )
        return rows
