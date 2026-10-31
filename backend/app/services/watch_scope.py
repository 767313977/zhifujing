"""自选里「只算股票」的那部分代码（2026-10-30 复查 P1-1 / P2-2）。

自选从 2026-10-30 起允许放**可转债代码**（可转债-5），2026-10-31 起允许放 **ETF**
（ETF-5）。凡是按「股票口径」处理自选的地方，都必须先过这一层，否则会出现这些事：

- 每日采集拿转债 / ETF 代码去问 iFinD 股票日线（`11x` 还会被换算成 `.SZ`，问的是
  不存在的代码）—— 白花配额，连续失败还会把 `watchlist` 这一步记成 failed，
  连带当天被判成「没采完」、简报被推迟；
- 明日计划给转债代码生成一行空计划；
- 简报的「自选股异动」拿转债代码去查公告；
- 配额日耗把转债 / ETF 也算进「每只 1 次/天」。

判断依据是 `stock_basic` 里有没有这个代码 —— 转债只在 `cb_basic`、ETF 只在
`etf_basic`，三张表代码不重叠。
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CbDaily, EtfBar, StockBasic, StockDaily, Watchlist

#: 可转债代码前缀：沪 110/111/113/118/132、深 123/127/128。
#: 与前端 `lib/bondCode.ts` 同一套；A 股没有以这些开头的代码。
BOND_PREFIXES = ("110", "111", "113", "118", "132", "123", "127", "128")

#: ETF / LOF 代码前缀：沪 50/51/52/56/58（588 是科创 ETF）、深 15/16/18。
#: 与 A 股（60/68/00/30/8/4）不重叠；分级基金与 LOF 也落在这些前缀里，
#: 它们同样没有股票日线。ETF 的日线由 `etf_bar` 提供（通达信盘后包，零配额）。
ETF_PREFIXES = ("15", "16", "18", "50", "51", "52", "56", "58")


def is_bond_code(code: str) -> bool:
    """按代码前缀判断是不是转债（不查库，给「任何入口都兜住」用）。"""
    return bool(code) and code.startswith(BOND_PREFIXES)


def is_etf_code(code: str) -> bool:
    """按代码前缀判断是不是 ETF / LOF（不查库，给「任何入口都兜住」用）。"""
    return bool(code) and code.startswith(ETF_PREFIXES)


def stock_watch_codes(session: Session, user_id: int | None = None) -> list[str]:
    """自选里的**股票**代码（去重）。`user_id` 为空时取全站并集。

    转债 / ETF 不在 `stock_basic` 里，天然被这一层挡掉（2026-10-30 / 10-31）。
    """
    statement = (
        select(Watchlist.code)
        .distinct()
        .where(Watchlist.code.in_(select(StockBasic.code)))
    )
    if user_id is not None:
        statement = statement.where(Watchlist.user_id == user_id)
    return list(session.scalars(statement))


def filter_stock_codes(session: Session, codes: list[str]) -> list[str]:
    """把一批代码里的转债 / ETF 剔掉，保持传入顺序。"""
    if not codes:
        return []
    known = set(session.scalars(select(StockBasic.code).where(StockBasic.code.in_(codes))))
    return [code for code in codes if code in known]


def stock_watch_count(session: Session) -> int:
    """自选里的股票只数（去重，与 `sync_watchlist` 的并集口径一致）。"""
    return int(
        session.scalar(
            select(func.count(func.distinct(Watchlist.code))).where(
                Watchlist.code.in_(select(StockBasic.code))
            )
        )
        or 0
    )


def latest_quotes(
    session: Session, codes: list[str]
) -> dict[str, StockDaily | CbDaily | EtfBar]:
    """每个代码取最新一条本地缓存的日线（自选 / 盘前页共用，2026-10-31 从 api 层搬来）。

    取的是缓存而不是实时行情：页面因此不必联网、打开即读。

    ⚠️ 「最新」要滤掉停牌残行（iFinD 给停牌日写的那种只有收盘价、`pct_chg` 为空的行）；
    转债 / ETF 在 `stock_daily` 里永远查不到，回落 `cb_daily` / `etf_bar`，那两张表
    只要求 `close` 非空（兜底源那天没有涨跌幅）。
    """
    if not codes:
        return {}
    newest = (
        select(StockDaily.code, func.max(StockDaily.trade_date).label("latest"))
        .where(StockDaily.code.in_(codes), StockDaily.pct_chg.is_not(None))
        .group_by(StockDaily.code)
        .subquery()
    )
    rows = session.scalars(
        select(StockDaily).join(
            newest,
            (StockDaily.code == newest.c.code)
            & (StockDaily.trade_date == newest.c.latest),
        )
    )
    quotes: dict[str, StockDaily | CbDaily | EtfBar] = {row.code: row for row in rows}

    missing = [code for code in codes if code not in quotes]
    if not missing:
        return quotes
    newest_cb = (
        select(CbDaily.bond_code, func.max(CbDaily.trade_date).label("latest"))
        .where(CbDaily.bond_code.in_(missing), CbDaily.close.is_not(None))
        .group_by(CbDaily.bond_code)
        .subquery()
    )
    for row in session.scalars(
        select(CbDaily).join(
            newest_cb,
            (CbDaily.bond_code == newest_cb.c.bond_code)
            & (CbDaily.trade_date == newest_cb.c.latest),
        )
    ):
        quotes[row.bond_code] = row

    missing = [code for code in codes if code not in quotes]
    if not missing:
        return quotes
    newest_etf = (
        select(EtfBar.code, func.max(EtfBar.trade_date).label("latest"))
        .where(EtfBar.code.in_(missing), EtfBar.close.is_not(None))
        .group_by(EtfBar.code)
        .subquery()
    )
    for row in session.scalars(
        select(EtfBar).join(
            newest_etf,
            (EtfBar.code == newest_etf.c.code) & (EtfBar.trade_date == newest_etf.c.latest),
        )
    ):
        quotes[row.code] = row
    return quotes
