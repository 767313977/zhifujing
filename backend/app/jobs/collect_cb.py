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
import re
from datetime import date, timedelta

from sqlalchemy import func, or_, select

from app.config import Settings, get_settings
from app.db import session_scope, upsert_many_fill
from app.models import CbBasic, CbDaily, StockDaily
from app.services.convertible import parse_coupons, ytm
from app.services.usage import QuotaLevel, quota_level
from app.sources.akshare_source import AkshareSource
from app.sources.ifind import IfindClient, to_bond_symbol
from app.timeutil import today_cn

logger = logging.getLogger(__name__)

#: 写进两张表的 `source`
SOURCE = "sina+em+jsl"
#: iFinD 兜底写入时的 source 标记：`em` = 价格来自东财 `债现价`，
#: `+ifind` = 成交额来自 iFinD。页面 / 排查时能一眼看出这天的行情不是新浪来的。
SOURCE_EM = "em"
SOURCE_EM_IFIND = "em+ifind"

#: 少于这么多只有行情的转债，就认为源挂了（正常 300 只上下）
MIN_SPOT_ROWS = 50

#: iFinD 兜底时取的列。**只取成交额与成交量**：它的「最新价 / 最高价 / 最低价」是
#: **净价**（不含应计利息，实测上银转债 112.27 vs 全价 115.33，差的就是当年 4% 票息的
#: 应计部分），拿它当全价去算溢价率 / 双低 / YTM 会直接算错。价格改由东财 `债现价` 兜底。
IFIND_INDICATORS = ("成交额", "成交量")

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


#: 集思录「强赎天计数」只认**开头**的 `已满足/需要` 这种写法（如 `12/15 | 30`）。
#: ⚠️ 不能用「文字里出现 x/y 就算」的宽松匹配：这一栏偶尔是日期（`2026/11/05`）或
#: 一段 HTML，宽松匹配会把日期读成「已满足 2026 天」（2026-10-30 复查）。
#: 另外分子上限 30 —— 条款窗口就是 30 个交易日，超过的一定不是计数；
#: 下限是 0（实测 318 只里 299 只是 `0/15 | 30`，那是真的「一天都没满足」，
#: 不能当解析失败丢掉，否则整条计数会退回本地窗口、白白丢掉集思录的完整口径）。
_REDEEM_DAYS_RE = re.compile(r"^\s*(\d{1,2})\s*/\s*(\d{1,2})\b")


def _parse_redeem_days(value: object) -> tuple[int, int] | None:
    """返回 (已满足天数, 触发所需天数)；认不出来就 None。"""
    match = _REDEEM_DAYS_RE.match(str(value or ""))
    if not match:
        return None
    done, need = int(match.group(1)), int(match.group(2))
    if not (0 <= done <= 30) or not (0 < need <= 30):
        return None
    return done, need


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


#: 到期赎回价在条款原文里的写法：「票面面值的112%（含最后一期计息年度利息）」
_CALL_PRICE_RE = re.compile(r"面值(?:的)?\s*(\d+(?:\.\d+)?)\s*%")


def call_price_from_terms(info: dict) -> float | None:
    """到期赎回价（含最后一期利息）。

    `EXECUTE_PRICE_SH` 只有**已经发生强赎 / 摘牌**的券才有值（实测 371 只里只有 66 只
    有），光靠它 YTM 基本算不出来。所以缺的时候从 `REDEEM_CLAUSE` 原文里抠
    「面值的 112%」这种写法 —— 条款原文每只券都有。

    抠出来的值只接受 100~130（复查 P2-6）：条款里还会出现「转股价的 130%」这类比例，
    或者别的百分数，超出范围一律当作没解析出来，宁可不给 YTM 也不给错值。
    """
    price = _num(info.get("EXECUTE_PRICE_SH"))
    if price is None:
        match = _CALL_PRICE_RE.search(str(info.get("REDEEM_CLAUSE") or ""))
        if match:
            price = float(match.group(1))
    if price is None or not 100 <= price <= 130:
        return None
    return price


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
        trigger = _num(extra.get("强赎触发比"))
        result[code] = {
            "bond_code": code,
            "bond_name": _text(row.get("债券简称")),
            "stock_code": _text(row.get("正股代码")),
            "stock_name": _text(row.get("正股简称")),
            "list_date": list_date,
            "maturity_date": _date(extra.get("到期日")),
            "issue_size": _num(row.get("发行规模")),
            "rating": _text(row.get("信用评级")),
            # 强赎触发比例直接用集思录的「强赎触发比」（130 / 150 这种），
            # 比自己用「触发价 ÷ 转股价」算可靠 —— 下修后东财的触发价未必同步更新
            "redeem_trigger_pct": trigger if trigger and 100 < trigger < 200 else None,
        }
    return result


def _row_value(row: dict, exact: tuple[str, ...], contains: tuple[str, ...] = (), exclude: tuple[str, ...] = ()) -> object:
    """从 iFinD 返回的一行里取某个列。

    列名不稳定（「收盘价」/「收盘价(元)」/「前收盘价」混在一起），所以先精确匹配，
    再按子串兜底，并且用 `exclude` 挡掉「前收盘价」被当成「收盘价」这类误命中。
    """
    for name in exact:
        if name in row:
            return row[name]
    for key, value in row.items():
        text = str(key)
        if not any(part in text for part in contains):
            continue
        if any(part in text for part in exclude):
            continue
        return value
    return None


def _spot_from_cov(cov: list[dict], day: date, universe: set[str]) -> list[dict]:
    """兜底行情：用东财一览里的 `债现价`（**全价**）拼出与新浪一致的形状。

    实测 `债现价` 与新浪逐位一致（广核 133.11、上银 115.328），所以溢价比、双低、YTM
    都不会被算歪；缺的只是涨跌幅 / 最高最低（页面显示「—」）。

    两道过滤是必须的（2026-10-30 实测踩过）：东财这张表里**已退市**的 700 多只券
    `债现价` 仍停在 100，未上市的也是 100 —— 不过滤会把它们当成「100 元的转债」
    全塞进池子，覆盖率闸反而会因「行数变多」放行。所以只收
    `上市日 ≤ 今天` **且** 在昨日名单里（`universe`）的券。
    """
    rows: list[dict] = []
    for row in cov:
        code = str(row.get("债券代码") or "").strip()
        price = _num(row.get("债现价"))
        list_date = _date(row.get("上市时间"))
        if not code or not price or price <= 0 or code not in universe:
            continue
        if list_date is None or list_date > day:
            continue
        rows.append(
            {
                "code": code,
                "name": _text(row.get("债券简称")),
                "trade": price,
                "settlement": None,
                "changepercent": None,
                "high": None,
                "low": None,
                "amount": None,
            }
        )
    return rows


def _ifind_amounts(settings: Settings | None, codes: list[str]) -> dict[str, float]:
    """iFinD 兜底：批量取一次成交额（元）。失败返回空 dict。

    只取成交额 / 成交量：它的价格是**净价**，不能当全价用（见 `IFIND_INDICATORS`）。
    一次批量请求换一份成交额，比「成交额筛选把整池剔空」划算。
    """
    if not codes:
        return {}
    try:
        client = IfindClient(settings or get_settings())
        rows = client.bond_quotes([to_bond_symbol(code) for code in codes], list(IFIND_INDICATORS))
    except Exception:  # noqa: BLE001 - 兜底失败只少一列，不该让采集换个错误崩
        logger.exception("iFinD 可转债成交额兜底失败")
        return {}

    result: dict[str, float] = {}
    for row in rows:
        raw = _row_value(row, ("证券代码", "代码", "thscode", "symbol"), ("代码",))
        digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
        if len(digits) < 6:
            continue
        amount = _num(_row_value(row, ("成交额",), ("成交额",)))
        if amount is not None:
            result[digits[:6]] = amount
    return result


def _merge_daily(
    spot: list[dict],
    cov: dict[str, dict],
    redeem: dict[str, dict],
    basics: dict[str, CbBasic],
    day: date,
    source: str = SOURCE,
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
        redeem_days = _parse_redeem_days(extra.get("强赎天计数"))

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
                "redeem_days": redeem_days[0] if redeem_days else None,
                "redeem_days_required": redeem_days[1] if redeem_days else None,
                "last_trade_date": _date(extra.get("最后交易日")),
                "source": source,
            }
        )
    return rows


#: 条款补采失败的券隔几天再试（复查 P1-2：不设这个间隔，前 N 只会被「永远补不上」的券占死）
TERMS_RETRY_DAYS = 7

#: 触发比例只认常见档位。自己用「触发价 ÷ 转股价」算出来的值如果不像话，
#: 说明东财的触发价没跟着下修更新，宁可留空由服务默认值兜底（复查 P2-1）
REDEEM_TRIGGER_PCTS = (120.0, 125.0, 130.0, 150.0)
RESALE_TRIGGER_PCTS = (70.0,)


def _trigger_pct(
    price: float | None, conv_price: float | None, bands: tuple[float, ...]
) -> float | None:
    """触发价 ÷ 转股价 → 比例（%）。只认 `bands` 里的档位（差 2 个百分点以内）。"""
    if not price or not conv_price:
        return None
    value = round(price / conv_price * 100, 2)
    nearest = min(bands, key=lambda item: abs(item - value))
    return nearest if abs(nearest - value) <= 2 else None


def _fill_terms(source: AkshareSource, day: date) -> int:
    """给在交易的券补条款（每天最多 `TERMS_PER_RUN` 只），返回补了几只。

    队列口径（复查 P1-2）：
    - **只补在交易的券**（昨日名单），否则 30 次请求全花在已退市的 110/113 老债上；
    - 按 `list_date` 倒序，新券优先；
    - 试过一次就写 `terms_tried_at`（不管成败），失败的券隔 `TERMS_RETRY_DAYS` 天再试，
      不然「解析不出票息」的那几十只会永久霸占队头。

    条款只影响 YTM 与回售计数，缺了不影响页面主体，所以失败只记日志、不抛。
    """
    universe = _recent_universe()
    if not universe:
        return 0
    retry_before = day - timedelta(days=TERMS_RETRY_DAYS)
    with session_scope() as session:
        pending = list(
            session.scalars(
                select(CbBasic.bond_code)
                .where(
                    CbBasic.bond_code.in_(universe),
                    # 票息与到期赎回价缺任一个 YTM 都算不出来，两个都要补
                    or_(
                        CbBasic.coupon_text.is_(None),
                        CbBasic.maturity_call_price.is_(None),
                    ),
                    or_(
                        CbBasic.terms_tried_at.is_(None),
                        CbBasic.terms_tried_at < retry_before,
                    ),
                )
                .order_by(CbBasic.list_date.desc().nulls_last(), CbBasic.bond_code)
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
        # ⚠️ 所有键**每行都要有**（算不出来给 None）：`upsert_many_fill` 只按第一行取列，
        # 后面的行少键会抛 CompileError、多键会被静默丢掉（复查 P1-3）。
        # 银行转债没有回售条款（RESALE_TRIG_PRICE 是 "-"），混在一批里就会踩到。
        rows.append(
            {
                "bond_code": code,
                "coupon_text": _text(info.get("INTEREST_RATE_EXPLAIN")),
                "maturity_call_price": call_price_from_terms(info),
                "maturity_date": _date(info.get("EXPIRE_DATE")),
                "convert_start_date": _date(info.get("TRANSFER_START_DATE")),
                "redeem_trigger_pct": _trigger_pct(
                    _num(info.get("REDEEM_TRIG_PRICE")), conv_price, REDEEM_TRIGGER_PCTS
                ),
                "resale_trigger_pct": _trigger_pct(
                    _num(info.get("RESALE_TRIG_PRICE")), conv_price, RESALE_TRIGGER_PCTS
                ),
            }
        )

    if rows:
        with session_scope() as session:
            upsert_many_fill(session, CbBasic, rows)
    # 失败的也要打上「试过了」，否则明天还是这 30 只
    with session_scope() as session:
        upsert_many_fill(
            session,
            CbBasic,
            [{"bond_code": code, "terms_tried_at": day} for code in pending],
        )
    logger.info("可转债条款补采：%d/%d 只（失败 %d）", len(rows), len(pending), len(pending) - len(rows))
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
    """消失的券写 `delist_date`（不删行）。

    两种「消失」都要认（复查 P2-5）：
    - **到期**：`maturity_date ≤ 今天`；
    - **强赎完成**：上一次快照里记着 `last_trade_date ≤ 今天`。强赎摘牌时到期日往往
      还有好几年，只按到期日判断的话这类券永远不会被标退市。
    """
    marked = 0
    with session_scope() as session:
        previous = session.scalar(
            select(func.max(CbDaily.trade_date)).where(CbDaily.trade_date < day)
        )
        last_days: dict[str, date | None] = {}
        if previous is not None:
            for code, last_trade in session.execute(
                select(CbDaily.bond_code, CbDaily.last_trade_date).where(
                    CbDaily.trade_date == previous
                )
            ).all():
                last_days[str(code)] = last_trade

        for basic in session.scalars(
            select(CbBasic).where(CbBasic.delist_date.is_(None))
        ):
            if basic.bond_code in alive:
                continue
            if basic.maturity_date is not None and basic.maturity_date <= day:
                basic.delist_date = basic.maturity_date
                marked += 1
                continue
            last_trade = last_days.get(basic.bond_code)
            if last_trade is not None and last_trade <= day:
                basic.delist_date = last_trade
                marked += 1
    return marked


def _recent_universe() -> set[str]:
    """上一次快照里的转债代码集合 —— 兜底时用它划「今天该有行情的券」。

    为什么不用 `cb_basic`：那张表里有 1000 多只（含已退市、未上市的），
    东财对它们的 `债现价` 一律给 100，直接拿去兜底会造出一整屏假行情。
    """
    with session_scope() as session:
        latest = session.scalar(select(func.max(CbDaily.trade_date)))
        if latest is None:
            return set()
        return set(
            session.scalars(select(CbDaily.bond_code).where(CbDaily.trade_date == latest))
        )


def _load_spot(
    source: AkshareSource,
    settings: Settings | None,
    cov: list[dict],
    day: date,
) -> tuple[list[dict], str]:
    """取当日行情：新浪优先；挂了就用东财 `债现价` 兜底，成交额再找 iFinD 补一次。

    为什么值得花 iFinD 配额：可转债页面的**所有**派生指标都以当天快照为基准，
    新浪是单点，它一挂就是整天全空。代价是一次批量请求（按 10 只/批，约 31 批），
    只在这条少见的路径上烧。

    ⚠️ 价格**不能**直接拿 iFinD 的「最新价」：它是净价（不含应计利息，实测上银转债
    112.27 vs 全价 115.33），拿去算溢价率 / 双低 / YTM 会系统性算错。东财 `债现价`
    与新浪逐位一致，才是正确的价格兜底。

    返回 `(行, source 标记)`；标记会写进 `cb_daily.source`。
    """
    spot: list[dict] = []
    try:
        spot = source.cb_spot()
    except Exception:  # noqa: BLE001 - 失败就走兜底，不让采集整体崩
        logger.exception("新浪可转债行情取数失败，改用东财 + iFinD 兜底")
    if len(spot) >= MIN_SPOT_ROWS:
        return spot, SOURCE

    logger.warning(
        "可转债行情只取到 %d 只（阈值 %d），改用东财债现价兜底", len(spot), MIN_SPOT_ROWS
    )
    universe = _recent_universe()
    if not universe:
        raise RuntimeError(
            f"可转债行情只取到 {len(spot)} 只（阈值 {MIN_SPOT_ROWS}），"
            "而且库里还没有昨日名单，无法兜底"
        )
    fallback = _spot_from_cov(cov, day, universe)
    if len(fallback) < MIN_SPOT_ROWS:
        raise RuntimeError(
            f"可转债行情只取到 {len(spot)} 只（阈值 {MIN_SPOT_ROWS}），"
            f"东财兜底也只有 {len(fallback)} 只"
        )

    amounts: dict[str, float] = {}
    if _ifind_allowed(settings, day):
        amounts = _ifind_amounts(settings, [row["code"] for row in fallback])
    if amounts:
        for row in fallback:
            row["amount"] = amounts.get(row["code"])
        logger.warning("兜底完成：价格来自东财、成交额来自 iFinD（source=%s）", SOURCE_EM_IFIND)
        return fallback, SOURCE_EM_IFIND

    # 今天早先已经兜过一次：这一趟只拿到价格（`amount` 全空），但库里早先那批
    # `em+ifind` 的成交额还在 —— `refresh` 用 fill 语义落库不会把它抹掉，
    # 所以 source 仍标 em+ifind 才与数据一致（2026-10-31 复查 P1）。
    if _already_fetched_amounts(day):
        logger.info(
            "兜底完成：价格来自东财；成交额沿用今天早先 iFinD 兜到的值（source=%s）",
            SOURCE_EM_IFIND,
        )
        return fallback, SOURCE_EM_IFIND

    logger.warning("兜底完成：只有价格、没有成交额（source=%s）", SOURCE_EM)
    return fallback, SOURCE_EM


def _already_fetched_amounts(day: date) -> int:
    """今天是否已经有「价格来自东财、成交额来自 iFinD」的行；返回行数（0 = 还没有）。

    单独一个函数是为了让 `_load_spot` 能区分**为什么**不取 iFinD：配额让路 vs 今天已经取过。
    两者的后续处理完全不同 —— 前者这一趟确实没有成交额，后者库里的成交额还在。
    """
    with session_scope() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(CbDaily)
                .where(CbDaily.trade_date == day, CbDaily.source == SOURCE_EM_IFIND)
            )
            or 0
        )


def _ifind_allowed(settings: Settings | None, day: date) -> bool:
    """兜底要不要花 iFinD 配额（复查 P2-2）。

    两道闸：
    - **配额档位**：与 DDE 补齐用同一个门槛（`PAUSE_HIT_DDE`）。连「命中前 50 只的
      DDE」都让路时，可转债成交额这种纯增强数据更没有理由花配额；
    - **当天已经兜过一次**：17:30 / 19:30 / 22:35 三趟是全量重跑，新浪挂一整天就是
      31 批 × 3 次。同一天已经有 `em+ifind` 的行就不再取。
    """
    level = quota_level(settings=settings)
    if level >= QuotaLevel.PAUSE_HIT_DDE:
        logger.warning("配额档位已到 %s（DDE 都已让路），跳过 iFinD 成交额兜底", level.name)
        return False
    done = _already_fetched_amounts(day)
    if done:
        logger.info("今天的成交额已经从 iFinD 兜过（%d 行），不再重复取", done)
        return False
    return True


def refresh(settings: Settings | None = None, trade_date: date | None = None) -> int:
    """采一次当日快照，返回写入 `cb_daily` 的行数。**只允许今天**（见模块说明）。"""
    day = trade_date or today_cn()
    if day != today_cn():
        raise RuntimeError(
            f"可转债快照只能当天采（请求 {day}、今天是 {today_cn()}）：三个来源都只有实时行情，"
            "历史无法回补，写进旧日期会让所有派生指标算错"
        )

    source = AkshareSource(settings or get_settings())
    cov = source.cb_basic()
    redeem = source.cb_redeem()
    if not cov:
        raise RuntimeError("可转债一览（东财）返回 0 行，无法归类正股 / 溢价率")
    # 行情放在 cov 之后：新浪挂了要拿东财的 `债现价` 兜底
    spot, spot_source = _load_spot(source, settings, cov, day)

    # 1) 静态条款：**fill 语义**（新的 None 不覆盖库里已有的非空值）
    redeem_by_code = {str(row.get("代码")): row for row in redeem if row.get("代码")}
    basic_by_code = _basic_rows(cov, redeem_by_code)
    with session_scope() as session:
        upsert_many_fill(session, CbBasic, list(basic_by_code.values()))

    # 2) 票息 / 到期赎回价：每天补一批，补齐之后 YTM 才有值。
    # 条款是增强数据，**绝不能让异常冒到 refresh** —— 那会把当天整步记成 failed，
    # 页面就停在前一天（复查 P1-3）。
    try:
        _fill_terms(source, day)
    except Exception:  # noqa: BLE001
        logger.exception("可转债条款补采失败（不影响行情落库）")
    with session_scope() as session:
        basics = {row.bond_code: row for row in session.scalars(select(CbBasic))}

    # 3) 当日行情
    cov_by_code = {str(row.get("债券代码")): row for row in cov if row.get("债券代码")}
    rows = _merge_daily(spot, cov_by_code, redeem_by_code, basics, day, spot_source)
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

    # 用 **fill** 语义写（2026-10-31 复查 P1）：17:30 / 19:30 是全量重跑，而新浪是单点 ——
    # 「17:30 新浪正常（有成交额/最高最低）、19:30 新浪挂了（这些列全空）」这种组合下，
    # `upsert_many` 会把已经采到的成交额 / 换手 / 涨跌幅**整列刷成 NULL**。
    # 后果不是「少一列」：`t0` 预设要求成交额 ≥5000 万，19:30 之后整池会变空，页面显示
    # 「当前条件下没有可转债」。fill 语义下新的非空值照常覆盖（新浪恢复后仍是新价），
    # 只有「这次没取到」的列保留旧值。
    with session_scope() as session:
        written = upsert_many_fill(session, CbDaily, rows)

    # 6) 消失的券写退市日期（不删行）
    marked = _mark_delisted(day, {row["bond_code"] for row in rows})

    logger.info(
        "可转债快照 %s：写入 %d 只（行情源 %s、退市标记 %d、缺溢价 %d、缺剩余规模 %d、有 YTM %d）",
        day,
        written,
        spot_source,
        marked,
        sum(1 for row in rows if row["premium_pct"] is None),
        sum(1 for row in rows if row["remain_size"] is None),
        sum(1 for row in rows if row["ytm_pct"] is not None),
    )
    return written
