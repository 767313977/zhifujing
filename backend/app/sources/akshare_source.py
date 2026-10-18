"""akshare 数据源：补齐 iFinD 的能力缺口。

负责：涨停池 / 跌停池 / 炸板池 / 龙虎榜 / 交易日历 / 全市场活跃度。

关于集群选择：
- 只使用 push2ex.eastmoney.com 集群（涨停池、跌停池、炸板池、龙虎榜）。
  push2*.eastmoney.com 集群会因请求频率触发本机 IP 临时频控
  （实测连接级失败、等待 75s 未恢复），本站不复用。
- 市场活跃度走乐咕乐股，与东财无关。
"""

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, TimeoutError as FuturesTimeoutError
from datetime import date

import akshare as ak
import numpy as np
import pandas as pd

from app.config import Settings, get_settings
from app.sources.base import TokenBucket, retry_call

logger = logging.getLogger(__name__)


def _ymd(value: date) -> str:
    return value.strftime("%Y%m%d")


def _clean(value: object) -> object:
    """归一单元格：NaN/NaT → None，numpy 标量 → Python 原生类型。

    numpy 标量（np.int64 等）不是 Python 类型的子类，直接交给 SQLAlchemy
    可能报错；NaN 直接入库会变成字符串 'nan'。
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.str_):
        return str(value)
    return value


def _records(df: pd.DataFrame) -> list[dict]:
    """DataFrame → list[dict]。"""
    if df is None or df.empty:
        return []
    return [
        {key: _clean(value) for key, value in row.items()}
        for row in df.to_dict(orient="records")
    ]


def _except_lhb_not_published(exc: Exception) -> bool:
    """龙虎榜调用的重试判定：东财「还没发布」的那一种 TypeError 不重试。

    没发布时 akshare 内部取 `data_json["result"]["pages"]`，而东财给的 `result`
    是 null → `None[...]` 抛 `TypeError: 'NoneType' object is not subscriptable`。
    这是**确定性**的：同一时刻重试 3 次（白等 2+4 秒）结果不会变，发布时间没到
    就是没到。其余异常（断连 / 超时等）照旧重试，宁可慢也别漏采。
    """
    return not (isinstance(exc, TypeError) and "subscriptable" in str(exc))


def _call_bounded(fn: Callable[[], pd.DataFrame], description: str, timeout: float) -> pd.DataFrame:
    """在**守护线程**里执行 `fn`，最多等 `timeout` 秒。

    akshare 内部大量直接 `requests.get/post` 且**不带 timeout**，而它又跑在全局采集锁
    里 —— 半开连接会让它一直等，把锁占死（之后每一趟采集都 `CollectionBusy`，只能
    重启）。这里给它加一层限时：超时抛 `TimeoutError`，让上层照常记 failed、把锁让出来。

    用普通守护线程（不是线程池）：超时后那个线程可能还挂在 socket 上，守护线程不会
    拖住进程退出；用线程池的话，挂住的 worker 还会占着池子、让后续调用排队。
    """
    future: Future = Future()

    def _run() -> None:
        try:
            future.set_result(fn())
        except BaseException as exc:  # noqa: BLE001 - 原样交给调用方
            future.set_exception(exc)

    threading.Thread(target=_run, name="akshare-call", daemon=True).start()
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError as exc:
        raise TimeoutError(
            f"{description} 超时（{timeout}s，akshare 内部无超时）"
        ) from exc


class AkshareSource:
    """iFinD 缺口的数据源。线程安全（限速器有锁保护）。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._bucket = TokenBucket(self.settings.akshare_rate_limit)

    def _call(
        self,
        fn: Callable[[], pd.DataFrame],
        description: str,
        *,
        retry_on: Callable[[Exception], bool] | None = None,
    ) -> pd.DataFrame:
        def _do() -> pd.DataFrame:
            self._bucket.acquire()
            return _call_bounded(fn, description, self.settings.http_timeout)

        return retry_call(
            _do,
            retries=self.settings.http_retries,
            backoff=self.settings.http_backoff,
            description=description,
            retry_on=retry_on,
        )

    # ------------------------------------------------------------------ 日历

    def trade_calendar(self) -> list[date]:
        """交易日历。

        注意：返回的日历包含**未来日期**（如 2026-12-31），
        取"最新交易日"时必须先过滤掉大于今天的部分。
        """
        df = self._call(ak.tool_trade_date_hist_sina, "akshare 交易日历")
        return [pd.Timestamp(value).date() for value in df["trade_date"]]

    # ------------------------------------------------------------ 涨停板三池

    def limit_up_pool(self, trade_date: date) -> list[dict]:
        """涨停池。含封板资金、首次/最后封板时间、炸板次数、连板数、所属行业。"""
        df = self._call(
            lambda: ak.stock_zt_pool_em(date=_ymd(trade_date)),
            f"akshare 涨停池 {trade_date}",
        )
        return _records(df)

    def limit_down_pool(self, trade_date: date) -> list[dict]:
        """跌停池。字段与涨停池不同：封单资金、连续跌停、开板次数，无首次封板时间。"""
        df = self._call(
            lambda: ak.stock_zt_pool_dtgc_em(date=_ymd(trade_date)),
            f"akshare 跌停池 {trade_date}",
        )
        return _records(df)

    def broken_pool(self, trade_date: date) -> list[dict]:
        """炸板池。含涨停价、涨速、振幅，无连板数与最后封板时间。"""
        df = self._call(
            lambda: ak.stock_zt_pool_zbgc_em(date=_ymd(trade_date)),
            f"akshare 炸板池 {trade_date}",
        )
        return _records(df)

    # ------------------------------------------------------------------ 龙虎榜

    def lhb(self, trade_date: date) -> list[dict]:
        """龙虎榜。同一股票可能因多条上榜原因重复出现。

        ⚠️ **当天还没发布时它返回空、而且是抛 TypeError 而不是空表**：
        akshare 的 `stock_lhb_detail_em` 内部直接取 `data_json["result"]["pages"]`，
        而东财在没有数据时返回的 `result` 是 **null** → `None[...]` 就是
        `TypeError: 'NoneType' object is not subscriptable`。
        （2026-09-28 实测：15:55 取当天就是这个错，16:35 再取就有数据了。）

        所以这里把这一种情况当成「来源暂无数据」而不是失败 ——
        采集时刻 2026-09-28 起改到 15:05，而龙虎榜要等收盘后一段时间才发布，
        **每天都会走到这个分支**；当成 failed 会让「数据管理」页每天多一条红字，
        真出故障时反而看不出来。

        只吞 TypeError，其它异常（网络、解析）照旧往上抛。
        这一种 TypeError 已通过 `retry_on` 排除在重试之外（2026-10-11 修）：
        以前要先白等 2+4 秒重试 3 次才落到这里，每天一次毫无意义。
        """
        ymd = _ymd(trade_date)
        try:
            df = self._call(
                lambda: ak.stock_lhb_detail_em(start_date=ymd, end_date=ymd),
                f"akshare 龙虎榜 {trade_date}",
                retry_on=_except_lhb_not_published,
            )
        except TypeError as exc:
            if "subscriptable" not in str(exc):
                raise  # 不是那个签名，就不当「暂无数据」处理
            logger.info("龙虎榜 %s 来源暂无数据（东财还没发布），跳过", trade_date)
            return []
        return _records(df)

    def lhb_range(self, start: date, end: date) -> list[dict]:
        """龙虎榜按日期区间批量取。

        该接口走 datacenter-web.eastmoney.com（与涨停池的 push2ex 不是同一集群），
        且原生支持区间查询，回补时一次取多天比逐日调用省大量请求。
        每行自带「上榜日」，调用方按它分组即可。
        """
        df = self._call(
            lambda: ak.stock_lhb_detail_em(start_date=_ymd(start), end_date=_ymd(end)),
            f"akshare 龙虎榜 {start}~{end}",
        )
        return _records(df)

    def lhb_institutions_range(self, start: date, end: date) -> list[dict]:
        """龙虎榜机构买卖每日统计。**只有区间版**：单日就是 `start == end`。

        与 `lhb()` 的区别：那个是**全部**席位的汇总（游资、散户都算在内），
        这个只算机构；数据源也不同（这个是 datacenter 的统计口径）。

        接口原生支持区间、由 akshare 自己翻页 —— 实测**整年一次调用**
        返回 12,481 行 / 252 个日期、约 22 秒，逐日调用要 250 次，
        所以补历史和不补的价格差不多。每行自带 `上榜日期`，写库按它分日期。

        **无数据返回空表而不是报错**：东财对空结果返回 `result: null`，
        akshare 却直接取 `data_json["result"]["pages"]`，于是抛 TypeError
        （`'NoneType' object is not subscriptable`）。非交易日、以及交易日但
        无机构上榜，都会走到这条路径 —— 那是「没有」，不是取数失败，
        不该让整段补采中断。
        这一种 TypeError 同样不重试（与 `lhb()` 共用 `_except_lhb_not_published`）。
        """
        try:
            df = self._call(
                lambda: ak.stock_lhb_jgmmtj_em(
                    start_date=_ymd(start), end_date=_ymd(end)
                ),
                f"akshare 龙虎榜机构统计 {start}~{end}",
                retry_on=_except_lhb_not_published,
            )
        except TypeError as exc:
            logger.info("龙虎榜机构统计 %s~%s 无数据: %s", start, end, exc)
            return []
        return _records(df)

    # -------------------------------------------------------------- 资金面

    def etf_list_ths(self) -> list[dict]:
        """同花顺 ETF 列表：代码 / 名称 / 最新交易日。**不占 iFinD 配额**。

        只当「代码清单 + 数据日期」用：实测 1725 行、一次请求就能列全市场，
        而 iFinD 的基金工具一次最多问几十只，列不全。但这份列表是**净值口径**
        （只有单位净值/累计净值/增长率，**没有份额、也没有成交额**），
        所以份额与行情要另找 iFinD —— 见 `collect_funds.collect_etf`。

        日期字段「最新-交易日」是该来源自己的写法（通常为上一交易日）。
        """
        df = self._call(ak.fund_etf_spot_ths, "同花顺 ETF 列表")
        return _records(df)

    # -------------------------------------------------------------- 财务摘要

    def financial_abstract_ths(self, code: str) -> list[dict]:
        """同花顺「财务摘要（按报告期）」—— 杀猪盘体检的信号 6 用。

        返回列：`报告期 / 净利润 / 净利润同比增长率 / 营业总收入 /
        营业总收入同比增长率 / 净资产收益率 / 销售毛利率 / 资产负债率`。
        金额带「823.20亿」「249.75万」这类后缀，统一交给 `markdown_table.to_float`
        换算成元；同比与比率是百分数（`-4.53%` → `-4.53`）。

        **单只一次请求、免费**，所以只在个股体检时按需调用（见 `services/financials`），
        不做全市场批量。
        """
        df = self._call(
            lambda: ak.stock_financial_abstract_ths(symbol=code, indicator="按报告期"),
            f"同花顺财务摘要 {code}",
        )
        return _records(df)

    # -------------------------------------------------------------- 人气 / 互动

    def hot_rank(self, code: str) -> dict | None:
        """东财个股人气榜（**仅热度参考**，不是「有人在引流」的证据）。

        除最新排名外，还补两项能解释「热度在往哪走」的读数：

        - `stock_hot_rank_detail_realtime_em`：当日 10 分钟级排名序列 →
          `today_change`（正 = 排名上升）、`today_best`（当日最好名次）
        - `stock_hot_keyword_em`：热门关键词与热度（如「ST股 719、算力概念 109」）

        后两项失败时只少几个字段，**不影响最新排名**。整体 10 分钟缓存由调用方控制。
        """
        if code.startswith("6"):
            prefix = "SH"
        elif code.startswith("92") or code.startswith(("4", "8")):
            prefix = "BJ"
        elif code.startswith("9"):
            prefix = "SH"  # 沪 B 股 900xxx（人气榜多半查不到，交给失败分支）
        else:
            prefix = "SZ"
        symbol = f"{prefix}{code}"

        df = self._call(
            lambda: ak.stock_hot_rank_latest_em(symbol=symbol),
            f"东财人气榜 {code}",
        )
        if df is None or df.empty:
            return None
        values = {str(row["item"]): row["value"] for _, row in df.iterrows()}
        try:
            result = {
                "rank": int(float(values["rank"])),
                "total": int(float(values["marketAllCount"])),
                "change": int(float(values["rankChange"])),
                "his_change": int(float(values["hisRankChange"])),
                "calc_time": str(values.get("calcTime") or ""),
                "today_change": None,
                "today_best": None,
                "today_points": 0,
                "keywords": [],
            }
        except (KeyError, ValueError):
            return None

        try:
            series = self._call(
                lambda: ak.stock_hot_rank_detail_realtime_em(symbol=symbol),
                f"人气榜分时 {code}",
            )
            if series is not None and not series.empty:
                ranks = [int(float(value)) for value in series["排名"].tolist()]
                if ranks:
                    # 名次数字越小越热：first - last 为正 = 今天排名在上升
                    result["today_change"] = ranks[0] - ranks[-1]
                    result["today_best"] = min(ranks)
                    result["today_points"] = len(ranks)
        except Exception:  # noqa: BLE001 - 辅助读数失败不影响主排名
            logger.info("%s 的人气榜分时取数失败", code, exc_info=True)

        try:
            frame = self._call(
                lambda: ak.stock_hot_keyword_em(symbol=symbol),
                f"人气榜关键词 {code}",
            )
            rows = _records(frame)
            rows.sort(key=lambda row: float(row.get("热度") or 0), reverse=True)
            result["keywords"] = [
                {"name": str(row.get("概念名称") or ""), "heat": float(row.get("热度") or 0)}
                for row in rows[:2]
                if row.get("概念名称")
            ]
        except Exception:  # noqa: BLE001 - 辅助读数失败不影响主排名
            logger.info("%s 的人气榜关键词取数失败", code, exc_info=True)
        return result

    def interactive_qa(self, code: str) -> list[dict]:
        """深市/北交所的**互动易问答**（问题 + 回答 + 时间）。

        ⚠️ 上证e互动（`stock_sns_sseinfo`）2026-10-18 实测对 600519 / 601398 / 603119
        **全部返回空表**（接口页面结构已变），所以沪市直接返回空列表，不做无谓请求。
        互动易只给深市/北交所 —— 沪市的「澄清」仍只能靠东财个股新闻。
        """
        if code.startswith("6"):
            return []
        df = self._call(
            lambda: ak.stock_irm_cninfo(symbol=code), f"互动易问答 {code}"
        )
        return _records(df)

    # -------------------------------------------------------------- 市场宽度

    def market_activity(self) -> dict:
        """全市场涨跌家数与涨跌停统计。

        iFinD 的涨跌家数只对上证指数有效（深证A指/国证A指/中证全指均返回 null），
        故用此接口补齐。返回形如：
            {"上涨": 2503.0, "下跌": 2558.0, "平盘": 148.0, "停牌": 12.0,
             "涨停": 50.0, "跌停": 2.0, "活跃度": "47.94%", "统计日期": datetime}
        """
        df = self._call(ak.stock_market_activity_legu, "akshare 市场活跃度")
        activity: dict = {}
        for row in _records(df):
            key = str(row.get("item") or "").strip()
            if key:
                activity[key] = row.get("value")
        return activity
