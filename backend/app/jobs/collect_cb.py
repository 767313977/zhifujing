"""可转债当日快照采集（2026-10-30 加）。零 iFinD 配额。

来源与实测（线上服务器跑过，见 `models.CbDaily` 的说明）：

1. 新浪 `bond_zh_hs_cov_spot` —— 现价 / 涨跌幅 / 最高最低 / 成交额；
2. 东财 `bond_zh_cov` —— 正股 / 转股价 / 转股价值 / 转股溢价率 / 发行规模 / 评级 / 上市日；
3. 集思录 `bond_cb_redeem_jsl` —— 强赎状态 / 剩余规模 / 到期日 / 最后交易日。

**东财比价表 `bond_cov_comparison` 不能用**：本机与云端都是 `RemoteDisconnected`
（2026-10-30 实测，同一个上午连跑两次都断），所以行情改走新浪。

**只能当天采**：三个来源都只有实时快照，没有按日历史接口
（`bond_zh_cov_value_analysis` 在 akshare 1.18.96 上直接 `Length mismatch` 报错）。
这里显式拒绝非今天 —— 把今天的行情写进旧日期，比缺一天严重得多。

写入策略：

- `cb_basic`：**不删行**。某只券若从快照里消失且已到期，写 `delist_date`；
  否则原样留着（删了回测就有幸存者偏差）。
- `cb_daily`：只写**今天有行情**的券（价格 0 / 停牌的不写，避免被双低排到第一）。
- 覆盖率闸：今天的行数少于上一采集日的 80% 就抛错、整天不写 —— 上游偶发只给
  半张表时可以靠它挡住（照 10-16 复查「派生指标无覆盖率闸」的教训）。
"""

import logging
from datetime import date

from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.db import session_scope, upsert_many, upsert_many_fill
from app.models import CbBasic, CbDaily, StockDaily
from app.services.convertible import parse_coupons, ytm
from app.sources.akshare_source import AkshareSource
from app.timeutil import today_cn

logger = logging.getLogger(__name__)

#: 写进两张表的 `source`
SOURCE = "sina+em+jsl"

#: 少于这么多只有行情的转债，就认为源挂了（正常 300 只上下）
MIN_SPOT_ROWS = 50

#: 覆盖率闸：低于上一采集日的这个比例就整天不写
COVERAGE_RATIO = 0.8

#: 每天最多补多少只券的条款（票息 / 到期赎回价）。全市场 400 多只，一券一次请求，
#: 一天补完要两分钟、还会和别的采集挤在同一个锁里，所以摊到多天；
#: 想立刻补全就跑 `scripts/fill_cb_terms.py`。
TERMS_PER_RUN = 30

#: `StockDaily.code` 的 IN 查询分批
_IN_BATCH = 900


def _num(value: object) -> float | None:
    """单元格转 float；NaN / NaT / 空串 / `-` 一律 None（**不能写成 0**）。"""
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


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _stock_pct_map(trade_date: date, codes: set[str]) -> dict[str, float]:
    """当日正股涨跌幅（%）。取自本地 `stock_daily` —— 采集在 17:30，那时当天日线已入库。

    东财那张表只给正股价、不给涨跌幅。取不到就是空 map，页面显示「—」。
    """
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


def _basic_rows(cov: list[dict], redeem: dict[str, dict]) -> dict[str, dict]:
    """从东财一览里取**已上市**的券，拼出 `cb_basic` 的静态列。

    未上市的（`上市时间` 为空）跳过：它们还没有行情，进了每日采集只会让覆盖率闸误判。

    `maturity_date` 优先用集思录强赎表里的「到期日」—— 它每天都有，**不必等条款补齐**；
    剩余年限这个筛选条件依赖它，没有它整个筛选池会是空的（2026-10-30 实测踩过）。
    """
    result: dict[str, dict] = {}
    for row in cov:
        code = str(row.get("债券代码") or "").strip()
        list_date = _date(row.get("上市时间"))
        if not code or list_date is None:
            continue
        extra = redeem.get(code, {})
        result[code] = {
            "bond_code": code,
            "bond_name": _text(row.get("债券简称")),
            "stock_code": _text(row.get("正股代码")),
            "stock_name": _text(row.get("正股简称")),
            "list_date": list_date,
            "maturity_date": _date(extra.get("到期日")),
            "issue_size": _num(row.get("发行规模")),
            "rating": _text(row.get("信用评级")),
        }
    return result


def _merge_daily(
    spot: list[dict],
    cov: dict[str, dict],
    redeem: dict[str, dict],
    basics: dict[str, CbBasic],
    day: date,
) -> list[dict]:
    """三个来源按转债代码并成 `cb_daily` 的行。纯函数，便于单测。"""
    rows: list[dict] = []
    for row in spot:
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        close = _num(row.get("trade"))
        # 停牌 / 未上市 / 北交所定向转债的价格是 0：没有行情就不写
        if not close or close <= 0:
            continue
        basic = basics.get(code)
        # 上市日晚于今天的（还没上市就先有报价了）不写，避免新债首日的极端值进池
        if basic is not None and basic.list_date and basic.list_date > day:
            continue

        prev_close = _num(row.get("settlement"))
        high = _num(row.get("high"))
        low = _num(row.get("low"))
        amplitude = None
        if high is not None and low is not None and prev_close:
            amplitude = round((high - low) / prev_close * 100, 2)

        info = cov.get(code, {})
        extra = redeem.get(code, {})
        remain_size = _num(extra.get("剩余规模"))
        amount = _num(row.get("amount"))
        turnover = None
        if amount is not None and remain_size:
            turnover = round(amount / (remain_size * 1e8) * 100, 2)

        conv_price = _num(info.get("转股价"))
        stock_close = _num(info.get("正股价"))
        conv_value = _num(info.get("转股价值"))
        if conv_value is None and conv_price and stock_close:
            # 东财没给转股价值时自己算：100 ÷ 转股价 × 正股价
            conv_value = round(100 / conv_price * stock_close, 2)

        rows.append(
            {
                "trade_date": day,
                "bond_code": code,
                "close": close,
                "prev_close": prev_close,
                "pct_chg": _num(row.get("changepercent")),
                "high": high,
                "low": low,
                "amount": amount,
                "amplitude": amplitude,
                "turnover": turnover,
                "stock_close": stock_close,
                "stock_pct_chg": None,  # 随后按本地 stock_daily 补
                "conv_price": conv_price,
                "conv_value": conv_value,
                "premium_pct": _num(info.get("转股溢价率")),
                "ytm_pct": None,  # 票息齐全之后再算（见 refresh）
                "remain_size": remain_size,
                "redeem_status": _text(extra.get("强赎状态")),
                "last_trade_date": _date(extra.get("最后交易日")),
                "source": SOURCE,
            }
        )
    return rows


def _fill_terms(source: AkshareSource) -> int:
    """给还没票息的券补条款（每天最多 `TERMS_PER_RUN` 只），返回补了几只。

    条款只影响 YTM 与回售计数，缺了不影响页面主体，所以失败只记日志、不抛。
    """
    with session_scope() as session:
        pending = list(
            session.scalars(
                select(CbBasic.bond_code)
                .where(CbBasic.coupon_text.is_(None))
                .order_by(CbBasic.bond_code)
                .limit(TERMS_PER_RUN)
            )
        )
    if not pending:
        return 0

    rows: list[dict] = []
    for code in pending:
        info = source.cb_terms(code)
        if not info:
            continue
        conv_price = _num(info.get("TRANSFER_PRICE"))
        row: dict = {
            "bond_code": code,
            "coupon_text": _text(info.get("INTEREST_RATE_EXPLAIN")),
            # `EXECUTE_PRICE_SH` 实测就是「到期赎回价 112」这类值（含最后一期利息）
            "maturity_call_price": _num(info.get("EXECUTE_PRICE_SH")),
            "maturity_date": _date(info.get("EXPIRE_DATE")),
            "convert_start_date": _date(info.get("TRANSFER_START_DATE")),
        }
        # 触发价 ÷ 转股价 = 触发比例（130 / 70），解析不出来就留空、由服务的默认值兜底
        redeem_price = _num(info.get("REDEEM_TRIG_PRICE"))
        if redeem_price and conv_price:
            row["redeem_trigger_pct"] = round(redeem_price / conv_price * 100, 2)
        resale_price = _num(info.get("RESALE_TRIG_PRICE"))
        if resale_price and conv_price:
            row["resale_trigger_pct"] = round(resale_price / conv_price * 100, 2)
        rows.append(row)

    if rows:
        with session_scope() as session:
            upsert_many_fill(session, CbBasic, rows)
    logger.info("可转债条款补采：%d/%d 只", len(rows), len(pending))
    return len(rows)


def _previous_count(session, day: date) -> int:
    prev_day = session.scalar(
        select(func.max(CbDaily.trade_date)).where(CbDaily.trade_date < day)
    )
    if prev_day is None:
        return 0
    return int(
        session.scalar(
            select(func.count()).select_from(CbDaily).where(CbDaily.trade_date == prev_day)
        )
        or 0
    )


def _mark_delisted(day: date, alive: set[str]) -> int:
    """消失的券：到期日已过的写 `delist_date`，其余原样保留（不删行）。"""
    marked = 0
    with session_scope() as session:
        for basic in session.scalars(
            select(CbBasic).where(CbBasic.delist_date.is_(None))
        ):
            if basic.bond_code in alive:
                continue
            if basic.maturity_date is not None and basic.maturity_date <= day:
                basic.delist_date = basic.maturity_date
                marked += 1
    return marked


def refresh(settings: Settings | None = None, trade_date: date | None = None) -> int:
    """采一次当日快照，返回写入 `cb_daily` 的行数。**只允许今天**（见模块说明）。"""
    day = trade_date or today_cn()
    if day != today_cn():
        raise RuntimeError(
            f"可转债快照只能当天采（请求 {day}、今天是 {today_cn()}）：三个来源都只有实时行情，"
            "历史无法回补，写进旧日期会让所有派生指标算错"
        )

    source = AkshareSource(settings or get_settings())
    spot = source.cb_spot()
    if len(spot) < MIN_SPOT_ROWS:
        raise RuntimeError(
            f"可转债行情只取到 {len(spot)} 只（阈值 {MIN_SPOT_ROWS}），疑似源异常"
        )
    cov = source.cb_basic()
    redeem = source.cb_redeem()
    if not cov:
        raise RuntimeError("可转债一览（东财）返回 0 行，无法归类正股 / 溢价率")

    # 1) 静态条款：**fill 语义**（新的 None 不覆盖库里已有的非空值）
    redeem_by_code = {str(row.get("代码")): row for row in redeem if row.get("代码")}
    basic_by_code = _basic_rows(cov, redeem_by_code)
    with session_scope() as session:
        upsert_many_fill(session, CbBasic, list(basic_by_code.values()))

    # 2) 票息 / 到期赎回价：每天补一批，补齐之后 YTM 才有值
    _fill_terms(source)
    with session_scope() as session:
        basics = {row.bond_code: row for row in session.scalars(select(CbBasic))}

    # 3) 当日行情
    cov_by_code = {str(row.get("债券代码")): row for row in cov if row.get("债券代码")}
    rows = _merge_daily(spot, cov_by_code, redeem_by_code, basics, day)
    if not rows:
        raise RuntimeError("可转债快照合并后 0 行")

    # 4) 覆盖率闸：拿今天的行数与上一次采集日比
    with session_scope() as session:
        previous = _previous_count(session, day)
    if previous and len(rows) < previous * COVERAGE_RATIO:
        raise RuntimeError(
            f"可转债行情覆盖率不足：今天 {len(rows)} 只 / 上次 {previous} 只"
            f"（阈值 {COVERAGE_RATIO:.0%}），整天不写"
        )

    # 5) 正股涨跌幅 + YTM
    stock_codes = {row.stock_code for row in basics.values() if row.stock_code}
    pct_by_stock = _stock_pct_map(day, stock_codes)
    for row in rows:
        basic = basics.get(row["bond_code"])
        row["stock_pct_chg"] = pct_by_stock.get(basic.stock_code) if basic and basic.stock_code else None
        if basic is not None:
            row["ytm_pct"] = ytm(
                row["close"],
                day,
                basic.maturity_date,
                parse_coupons(basic.coupon_text),
                basic.maturity_call_price,
            )

    with session_scope() as session:
        written = upsert_many(session, CbDaily, rows)

    # 6) 消失的券写退市日期（不删行）
    marked = _mark_delisted(day, {row["bond_code"] for row in rows})

    logger.info(
        "可转债快照 %s：写入 %d 只（退市标记 %d、缺溢价 %d、缺剩余规模 %d、有 YTM %d）",
        day,
        written,
        marked,
        sum(1 for row in rows if row["premium_pct"] is None),
        sum(1 for row in rows if row["remain_size"] is None),
        sum(1 for row in rows if row["ytm_pct"] is not None),
    )
    return written
