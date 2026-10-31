"""ETF 日线采集（通达信盘后包，2026-10-31 加）。零 iFinD 配额。

和可转债最大的不同：**ETF 的历史能回补**。盘后包按日期下载（官网保留 ≥ 3 年），
所以动量 / RPS / 趋势从第一天就能算，不用像转债那样等数据慢慢积累。

名单口径（2026-10-31 ETF-0 实测结论）：

1. **以「当天包内名称含 ETF」为准**，不要拿今天的 `etf_share` 名单去过滤历史日 ——
   2026 年新上的约 390 只（如 `158000 港股通金融ETF鹏华`）在 2025 年的包里本来就没有；
2. 名称截断导致不含「ETF」的漏网之鱼，用「代码在 `etf_share` 历史里出现过」补一次并集；
3. 深市 `1` 开头的还有债券 / 资产支持证券（15411 行里绝大多数不是 ETF），必须过滤。

`pct_chg` 直接用 `close / prev_close − 1`：盘后包的 `prev_close` 是**交易所口径的前收**，
除息与份额折算日都已经调整过（实测 510100 XD 除息、159583 1 拆 2 都算出正常涨跌幅），
所以按它复利不会在分红日出现假跌。

写库策略：

- `etf_bar`：只写有收盘价的行；覆盖率闸（低于上一份快照的 80% 就不写）挡住残缺包；
- `etf_basic`：每天 upsert，**不删行**；连续 `DELIST_DAYS` 个交易日不在包里才写
  `delist_date`（回测要保留已退市的样本）。
"""

import logging
import re
from datetime import date

from sqlalchemy import func, select

from app.db import session_scope, upsert_many, upsert_many_fill
from app.models import EtfBar, EtfBasic, EtfShare, TradeCalendar
from app.services.etf_category import norm_name
from app.sources.tdx import TdxError, TdxUnavailable, daily_package

logger = logging.getLogger(__name__)

SOURCE = "tdx"

#: 覆盖率闸：当天行数少于上一份快照的这个比例就抛错、整天不写
MIN_RATIO = 0.8

#: 连续多少个交易日不在盘后包里，就认为已退市（写日期、不删行）
DELIST_DAYS = 10

#: 盘后包名称里的除权除息前缀（存 `name` 时剥掉，便于展示与去重）
_EX_PREFIX = re.compile(r"^(?:XD|UC|XR|DR)+")

#: 「指数型-其他」里能判成商品的名称关键词（黄金 / 白银 / 豆粕 / 有色 / 原油…）
COMMODITY_WORDS = ("黄金", "白银", "豆粕", "有色", "商品", "期货", "原油")


def is_etf_row(row: dict, share_codes: set[str]) -> bool:
    """这条盘后包记录是不是 ETF。

    代码段先粗筛（沪 `5`、深 `1`），再要求「名称含 ETF」或「代码在 etf_share 里出现过」。
    只看代码段会把深市的债券、资产支持证券全放进来；只看今天的名册又会漏掉历史新券。
    """
    code = str(row.get("code") or "")
    market = row.get("market")
    if market == "sh":
        if not code.startswith("5"):
            return False
    elif market == "sz":
        if not code.startswith("1"):
            return False
    else:
        return False
    name = str(row.get("name") or "")
    return "ETF" in name or code in share_codes


#: `etf_share` 代码集合的进程内缓存：`(引擎, 代码集合)`。
#: 这张表生产约 40 万行，`select distinct code` 实测要 8~9 秒 —— 回补 240 天就等于
#: 白扫 240 次。它一天最多变几只（新上市），进程内复用一次足够（2026-10-31 加）。
_SHARE_CODES_CACHE: tuple[object, set[str]] | None = None


def _share_codes() -> set[str]:
    """`etf_share` 历史里出现过的全部代码（用来补「名称被截断」的漏网之鱼）。"""
    global _SHARE_CODES_CACHE
    with session_scope() as session:
        bind = session.get_bind()
        cached = _SHARE_CODES_CACHE
        if cached is not None and cached[0] is bind:
            return cached[1]
        codes = set(session.scalars(select(EtfShare.code).distinct()))
    _SHARE_CODES_CACHE = (bind, codes)
    return codes


def fetch_bars(day: date) -> tuple[list[dict], dict[str, str]]:
    """下载并解析某一天的盘后包，返回（可直接写 `etf_bar` 的行, 代码 → 原始名称）。

    `TdxUnavailable`（非交易日 / 还没发布 / 超出保留期）原样抛给调用方；
    `TdxError`（包损坏）也原样抛，绝不能当成「这天没有 ETF」。
    """
    rows = daily_package(day)
    share_codes = _share_codes()
    result: list[dict] = []
    names: dict[str, str] = {}
    for row in rows:
        if not is_etf_row(row, share_codes):
            continue
        close = row.get("close")
        prev_close = row.get("prev_close")
        pct_chg = None
        if close and prev_close and prev_close > 0:
            pct_chg = round((close / prev_close - 1) * 100, 4)
        names[row["code"]] = str(row.get("name") or "")
        result.append(
            {
                "trade_date": day,
                "code": row["code"],
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": close,
                "prev_close": prev_close,
                "pct_chg": pct_chg,
                "volume": row.get("volume"),
                "amount": row.get("amount"),
                "source": SOURCE,
            }
        )
    return result, names


def _last_count(day: date) -> int:
    """`day` 之前最近一份快照有多少行（覆盖率闸的基准）。"""
    with session_scope() as session:
        previous = session.scalar(
            select(func.max(EtfBar.trade_date)).where(EtfBar.trade_date < day)
        )
        if previous is None:
            return 0
        return int(
            session.scalar(
                select(func.count()).select_from(EtfBar).where(EtfBar.trade_date == previous)
            )
            or 0
        )


#: 基金类型表的进程内缓存：`(缓存日期, 代码 → 类型)`。
#: akshare 这个接口要拉 2.8 万行、约 4 秒，而 `run_late` 一天会走到三次 ——
#: 同一天成功拉过一次就不再拉（2026-10-31 复查 P2-1）。
_FUND_TYPES_CACHE: tuple[date, dict[str, str]] | None = None

#: 拉基金类型表的超时（秒）。akshare 内部不带 timeout，半开连接会把采集锁占死
#: （与 10-18 复查 em_events 同一个坑），所以必须套 `_call_bounded`。
FUND_TYPES_TIMEOUT = 30.0


def _fund_types(day: date) -> dict[str, str]:
    """代码 → 基金类型（`fund_name_em`，同一天只拉一次，30 秒超时）。

    拿不到就返回空表：调用方据此**不写** `fund_type`（留空），下次再补 ——
    宁可缺，不要拿默认值把已有类型覆盖掉。
    """
    global _FUND_TYPES_CACHE
    cached = _FUND_TYPES_CACHE
    if cached is not None and cached[0] == day:
        return cached[1]
    try:
        import akshare as ak

        from app.sources.akshare_source import _call_bounded

        frame = _call_bounded(
            lambda: ak.fund_name_em(), "ETF 基金类型表", FUND_TYPES_TIMEOUT
        )
    except Exception:  # noqa: BLE001 - 类型是增强信息，失败不影响日线
        logger.warning("取基金类型失败，本次跳过 fund_type 更新", exc_info=True)
        return {}
    if frame is None or frame.empty:
        return {}
    code_col, type_col = frame.columns[0], "基金类型"
    if type_col not in frame.columns:
        logger.warning("基金类型表没有「基金类型」列，跳过 fund_type 更新")
        return {}
    result = {
        str(code): str(kind or "").strip()
        for code, kind in zip(frame[code_col], frame[type_col])
        if str(code or "").strip()
    }
    _FUND_TYPES_CACHE = (day, result)
    return result


def _kind_of(raw_type: str, name: str) -> str | None:
    """`基金类型` + 名称 → 股票 / QDII / 债券 / 货币 / 商品。

    「指数型-其他」里混着黄金 / 商品 ETF，只能按名称关键词判断；判不出来返回 None
    （`t0` 保持 False —— 宁可漏标，也不要标错）。
    """
    text = raw_type or ""
    if "海外" in text or "QDII" in text.upper():
        return "QDII"
    if "固收" in text or "债券" in text:
        return "债券"
    if "货币" in text:
        return "货币"
    if "其他" in text:
        return "商品" if any(word in name for word in COMMODITY_WORDS) else None
    if "股票" in text:
        return "股票"
    return None


def _first_dates(codes: list[str]) -> dict[str, date]:
    """每只 ETF 在本地最早一根日线的日期（近似上市日）。"""
    if not codes:
        return {}
    with session_scope() as session:
        rows = session.execute(
            select(EtfBar.code, func.min(EtfBar.trade_date))
            .where(EtfBar.code.in_(codes))
            .group_by(EtfBar.code)
        ).all()
    return {code: day for code, day in rows}


def refresh_basic(day: date, names: dict[str, str] | None = None) -> int:
    """更新 `etf_basic`：名称 / 规范名 / 基金类型 / T+0 / 首次出现日。

    `names` 不给就按 `day` 当天库里的行 + 已存的名称算（回补完再单独跑一次即可）。
    用 `upsert_many_fill`（None 不覆盖）—— 某天基金类型接口挂了，不能把已经写好的
    类型清空。
    """
    if names is None:
        with session_scope() as session:
            codes = list(
                session.scalars(select(EtfBar.code).where(EtfBar.trade_date == day))
            )
            names = {
                basic.code: basic.name or ""
                for basic in session.scalars(
                    select(EtfBasic).where(EtfBasic.code.in_(codes))
                )
            }
        names = {code: names.get(code, "") for code in codes}

    types = _fund_types(day)
    firsts = _first_dates(list(names))

    typed: list[dict] = []
    untyped: list[dict] = []
    for code, raw_name in names.items():
        name = _EX_PREFIX.sub("", raw_name).strip() or raw_name or None
        base = {
            "code": code,
            "name": name,
            "norm_name": norm_name(raw_name) or None,
            "first_bar_date": firsts.get(code),
        }
        kind = _kind_of(types.get(code, ""), raw_name)
        if kind is None:
            untyped.append(base)
            continue
        typed.append(
            {**base, "fund_type": kind, "t0": kind in {"QDII", "债券", "货币", "商品"}}
        )

    with session_scope() as session:
        if typed:
            upsert_many_fill(session, EtfBasic, typed)
        if untyped:
            upsert_many_fill(session, EtfBasic, untyped)
    _mark_delisted(day)
    return len(typed) + len(untyped)


def _mark_delisted(day: date) -> int:
    """连续 `DELIST_DAYS` 个交易日没出现在盘后包里 → 写 `delist_date`（不删行）。"""
    with session_scope() as session:
        calendar = list(
            session.scalars(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date <= day)
                .order_by(TradeCalendar.trade_date.desc())
                .limit(DELIST_DAYS)
            )
        )
        if len(calendar) < DELIST_DAYS:
            return 0
        cutoff = calendar[-1]
        latest = dict(
            session.execute(
                select(EtfBar.code, func.max(EtfBar.trade_date)).group_by(EtfBar.code)
            ).all()
        )
        alive = set(
            session.scalars(
                select(EtfBar.code).where(EtfBar.trade_date == day)
            )
        )
        marked = 0
        for basic in session.scalars(
            select(EtfBasic).where(EtfBasic.delist_date.is_(None))
        ):
            if basic.code in alive:
                continue
            last = latest.get(basic.code)
            if last is not None and last < cutoff:
                basic.delist_date = last
                marked += 1
        return marked


def refresh(day: date, *, update_basic: bool = True) -> int:
    """采某一天的 ETF 日线并更新属性，返回写入 `etf_bar` 的行数。

    非交易日 / 包还没发布时抛 `TdxUnavailable`（调用方记 skipped），
    包残缺或覆盖率不足抛 `TdxError`（记 failed，等下一趟）。

    `update_basic=False` 给**回补脚本**用：`refresh_basic` 要拉一次
    `fund_name_em`（2.8 万行，约 4 秒）+ 算首次出现日，回补 240 天就等于白拉 240 次。
    回补结束时统一跑一次即可（首次出现日按库里的 min 日期算，与逐日跑结果一致）。
    """
    bars, names = fetch_bars(day)
    if not bars:
        raise TdxUnavailable(f"{day} 的盘后包里没有 ETF 行")
    previous = _last_count(day)
    if previous and len(bars) < previous * MIN_RATIO:
        raise TdxError(
            f"{day} 只解析到 {len(bars)} 只 ETF（上一份快照 {previous} 只，"
            f"低于 {MIN_RATIO:.0%}），盘后包可能不完整"
        )
    with session_scope() as session:
        written = upsert_many(session, EtfBar, bars)
    if update_basic:
        refresh_basic(day, names)
    logger.info("ETF 日线 %s：写入 %d 只（上一份 %d）", day, written, previous)
    return written
