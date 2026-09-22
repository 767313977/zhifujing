"""akshare 数据源：补齐 iFinD 的能力缺口。

负责：涨停池 / 跌停池 / 炸板池 / 龙虎榜 / 交易日历 / 全市场活跃度。

关于集群选择：
- 只使用 push2ex.eastmoney.com 集群（涨停池、跌停池、炸板池、龙虎榜）。
  push2*.eastmoney.com 集群会因请求频率触发本机 IP 临时频控
  （实测连接级失败、等待 75s 未恢复），本站不复用。
- 市场活跃度走乐咕乐股，与东财无关。
"""

import logging
from collections.abc import Callable
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


class AkshareSource:
    """iFinD 缺口的数据源。线程安全（限速器有锁保护）。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._bucket = TokenBucket(self.settings.akshare_rate_limit)

    def _call(self, fn: Callable[[], pd.DataFrame], description: str) -> pd.DataFrame:
        def _do() -> pd.DataFrame:
            self._bucket.acquire()
            return fn()

        return retry_call(
            _do,
            retries=self.settings.http_retries,
            backoff=self.settings.http_backoff,
            description=description,
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
        """龙虎榜。同一股票可能因多条上榜原因重复出现。"""
        ymd = _ymd(trade_date)
        df = self._call(
            lambda: ak.stock_lhb_detail_em(start_date=ymd, end_date=ymd),
            f"akshare 龙虎榜 {trade_date}",
        )
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
        """
        try:
            df = self._call(
                lambda: ak.stock_lhb_jgmmtj_em(
                    start_date=_ymd(start), end_date=_ymd(end)
                ),
                f"akshare 龙虎榜机构统计 {start}~{end}",
            )
        except TypeError as exc:
            logger.info("龙虎榜机构统计 %s~%s 无数据: %s", start, end, exc)
            return []
        return _records(df)

    # -------------------------------------------------------------- 资金面

    def etf_spot(self) -> list[dict]:
        """ETF 实时快照，含**最新份额**。

        一次返回全市场约 1600 只（请求次数与只数无关，恒定 1 次），
        所以调用方不必按代码分批。
        """
        df = self._call(ak.fund_etf_spot_em, "akshare ETF 快照")
        return _records(df)

    # -------------------------------------------------------------- 板块资金流

    def concept_fund_flow(self) -> list[dict]:
        """同花顺数据中心的**概念资金流**（即时快照）。

        返回字段：`序号 / 行业（就是概念名）/ 行业指数 / 行业-涨跌幅 / 流入资金 /
        流出资金 / 净额 / 公司家数 / 领涨股 / 领涨股-涨跌幅 / 当前价`，金额单位是**亿元**。

        ⚠️ 四个坑，都是实测出来的（2026-09-22）：

        1. **行数随盘中时段变**：08:50（盘前）387 行 / 359 个名字；09:05~09:25（集合
           竞价中）200~337 行；连续 4 次调用稳定在 337 行 / 330 个名字。
           原因看懂了：**盘前给的是上一交易日的收盘数据，最完整；一进竞价页面就开始刷，
           刷的过程里行数是残的**。所以正确的用法只有一个 —— **收盘后再采**
           （`jobs/collect_flows.py` 里有守卫）。
        2. **返回行数 > 不重复名字数**（337 行 / 330 个名字）：「专精特新」「存储芯片」
           这类会各出现两行、值还不一样。落库前必须按名字去重，否则主键冲突。
        3. **没有历史**：`symbol` 只能是 `即时 / 3日 / 5日 / 10日` —— 这是**窗口**不是
           日期，所以补不了历史，只能从当天开始攒。
        4. 口径是**同花顺概念**（约 359 个），与站内板块（开盘红精选 270 / 行业 104）
           不是一套名字，页面必须写清来源。

        另外 akshare 这个函数内部会先用 `ths.js` 生成 `hexin-v` 反爬头，再逐页抓
        （概念 4 页 / 行业 2 页），页面数是它自己从首页 HTML 里读的 —— 所以行数异常时
        先怀疑这个链路，而不是我们的代码。
        """
        df = self._call(
            lambda: ak.stock_fund_flow_concept(symbol="即时"), "akshare 概念资金流"
        )
        return _records(df)

    def industry_fund_flow(self) -> list[dict]:
        """同花顺数据中心的**行业资金流**（即时快照），字段同上。

        90 个行业、名字稳定（半导体 / 文化传媒 / 软件开发），**四次调用都是 90 行**，
        盘前盘中都没波动 —— 概念那边会变，行业这边不会。同样**没有历史**。
        """
        df = self._call(
            lambda: ak.stock_fund_flow_industry(symbol="即时"), "akshare 行业资金流"
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
