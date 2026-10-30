"""可转债当日快照采集（T+0 观察池用）。零 iFinD 配额。

三个来源各补一段（详见 `models.CbDaily`）：

1. 新浪 `bond_zh_hs_cov_spot` —— 现价 / 涨跌幅 / 最高最低 / 成交额；
2. 东财 `bond_zh_cov` —— 正股 / 转股价 / 转股价值 / 转股溢价率 / 发行规模 / 评级 / 上市日；
3. 集思录 `bond_cb_redeem_jsl` —— 强赎状态 / 剩余规模 / 到期日 / 最后交易日。

**只能当天采**：三个来源都只有实时快照、没有按日历史。这里显式拒绝非今天的日期 ——
拿今天的行情写进昨天的日期会让百分位与打分全错，比缺一天严重得多。

失败策略：**行情是硬依赖**（拿不到就抛错，不写半张表）；条款与强赎是增强数据，
某个源挂了就只缺那几列（页面上显示「—」），照常落库。
"""

import logging
from datetime import date

from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import session_scope, upsert_many
from app.models import CbDaily, StockDaily
from app.sources.akshare_source import AkshareSource
from app.timeutil import today_cn

logger = logging.getLogger(__name__)

#: 写进 `cb_daily.source`，排查口径时用
SOURCE = "sina+em+jsl"

#: 少于这么多只有行情的转债，就认为源挂了（正常 300 只上下）。宁可整天不写，
#: 也不要写一张残缺的快照 —— 观察池的百分位是按当天全样本算的。
MIN_SPOT_ROWS = 50

#: `StockDaily.code` 的 IN 查询分批（SQLite 参数上限 32766，900 只是留余量）
_IN_BATCH = 900


def _num(value: object) -> float | None:
    """单元格转 float；NaN / NaT / 空串 / "-" 一律 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value == value else None
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text or text in {"-", "--", "nan", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _date(value: object) -> date | None:
    """单元格转 date。支持 `2025-07-25` / `2025/07/25` / datetime / NaT。"""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text or text.lower() in {"nat", "nan", "none"}:
        return None
    text = text.replace("/", "-")
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _stock_pct_map(trade_date: date, codes: set[str]) -> dict[str, float]:
    """当日正股涨跌幅（百分比）。取自本地 `stock_daily` —— 采集在 17:30，
    那时 15:05 那趟已经把当天日线写好了。取不到就是空 map，页面显示「—」。"""
    if not codes:
        return {}
    ordered = sorted(codes)
    result: dict[str, float] = {}
    with session_scope() as session:
        for start in range(0, len(ordered), _IN_BATCH):
            chunk = ordered[start : start + _IN_BATCH]
            rows = session.execute(
                select(StockDaily.code, StockDaily.pct_chg).where(
                    StockDaily.trade_date == trade_date, StockDaily.code.in_(chunk)
                )
            ).all()
            for code, pct in rows:
                if pct is not None:
                    result[str(code)] = float(pct)
    return result


def _merge(spot: list[dict], basic: list[dict], redeem: list[dict]) -> list[dict]:
    """把三个来源按转债代码并成 `cb_daily` 的行。纯函数，便于单测。"""
    basic_by_code = {
        str(row.get("债券代码")): row for row in basic if row.get("债券代码")
    }
    redeem_by_code = {str(row.get("代码")): row for row in redeem if row.get("代码")}

    merged: list[dict] = []
    for row in spot:
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        close = _num(row.get("trade"))
        # 停牌 / 未上市 / 北交所定向转债的价格是 0：没有行情就没有观察意义
        if not close or close <= 0:
            continue
        prev_close = _num(row.get("settlement"))
        high = _num(row.get("high"))
        low = _num(row.get("low"))
        amplitude = None
        if high is not None and low is not None and prev_close:
            amplitude = round((high - low) / prev_close * 100, 2)

        info = basic_by_code.get(code, {})
        extra = redeem_by_code.get(code, {})
        remain_size = _num(extra.get("剩余规模"))
        amount = _num(row.get("amount"))
        turnover_rate = None
        if amount is not None and remain_size:
            turnover_rate = round(amount / (remain_size * 1e8) * 100, 2)

        merged.append(
            {
                "code": code,
                "name": str(row.get("name") or "").strip() or info.get("债券简称"),
                "stock_code": str(info.get("正股代码") or "").strip() or None,
                "stock_name": str(info.get("正股简称") or "").strip() or None,
                "close": close,
                "prev_close": prev_close,
                "pct_chg": _num(row.get("changepercent")),
                "high": high,
                "low": low,
                "amount": amount,
                "amplitude": amplitude,
                "convert_price": _num(info.get("转股价")),
                "convert_value": _num(info.get("转股价值")),
                "premium_rt": _num(info.get("转股溢价率")),
                "issue_size": _num(info.get("发行规模")),
                "rating": (str(info.get("信用评级") or "").strip() or None),
                "list_date": _date(info.get("上市时间")),
                "remain_size": remain_size,
                "turnover_rate": turnover_rate,
                "maturity_date": _date(extra.get("到期日")),
                "last_trade_date": _date(extra.get("最后交易日")),
                "redeem_status": (str(extra.get("强赎状态") or "").strip() or None),
                "stock_close": _num(info.get("正股价")),
                # 正股涨跌幅要按本地 `stock_daily` 补（东财这张表只给正股价），
                # 先占位成 None，保证所有行的键完全一致（`upsert_many` 的要求）
                "stock_pct_chg": None,
                "source": SOURCE,
            }
        )
    return merged


def refresh(settings: Settings | None = None, trade_date: date | None = None) -> int:
    """采一次当日快照，返回写入行数。**只允许今天**（见模块说明）。"""
    day = trade_date or today_cn()
    if day != today_cn():
        raise RuntimeError(
            f"可转债快照只能当天采（请求 {day}、今天是 {today_cn()}）：三个来源都只有实时行情，"
            "历史无法回补，写进旧日期会让百分位与打分全错"
        )

    source = AkshareSource(settings or get_settings())
    spot = source.cb_spot()
    if len(spot) < MIN_SPOT_ROWS:
        # 源挂了 or 接口改版：整天空着比写一张残缺快照好（百分位是按当天全样本算的）
        raise RuntimeError(f"可转债行情只取到 {len(spot)} 只（阈值 {MIN_SPOT_ROWS}），疑似源异常")

    try:
        basic = source.cb_basic()
    except Exception:  # noqa: BLE001 - 条款是增强数据，挂了只缺列
        logger.exception("可转债条款取数失败，本次只写行情")
        basic = []
    try:
        redeem = source.cb_redeem()
    except Exception:  # noqa: BLE001 - 同上
        logger.exception("可转债强赎表取数失败，本次缺剩余规模 / 强赎状态")
        redeem = []

    rows = _merge(spot, basic, redeem)
    if not rows:
        raise RuntimeError("可转债快照合并后 0 行")

    stock_codes = {row["stock_code"] for row in rows if row.get("stock_code")}
    pct_by_stock = _stock_pct_map(day, stock_codes)
    for row in rows:
        row["trade_date"] = day
        row["stock_pct_chg"] = pct_by_stock.get(row.get("stock_code") or "")

    with session_scope() as session:
        written = upsert_many(session, CbDaily, rows)

    missing_premium = sum(1 for row in rows if row["premium_rt"] is None)
    missing_size = sum(1 for row in rows if row["remain_size"] is None)
    logger.info(
        "可转债快照 %s：写入 %d 只（缺溢价 %d、缺剩余规模 %d、有正股涨跌幅 %d）",
        day,
        written,
        missing_premium,
        missing_size,
        sum(1 for row in rows if row["stock_pct_chg"] is not None),
    )
    return written
