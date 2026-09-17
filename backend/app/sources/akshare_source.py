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
