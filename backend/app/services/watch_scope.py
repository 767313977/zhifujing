"""自选里「只算股票」的那部分代码（2026-10-30 复查 P1-1 / P2-2）。

自选从 2026-10-30 起允许放**可转债代码**（可转债-5）。凡是按「股票口径」处理自选的
地方，都必须先过这一层，否则会出现这些事：

- 每日采集拿转债代码去问 iFinD 股票日线（`11x` 还会被换算成 `.SZ`，问的是不存在的
  代码）—— 白花配额，连续失败还会把 `watchlist` 这一步记成 failed，连带当天被判成
  「没采完」、简报被推迟；
- 明日计划给转债代码生成一行空计划；
- 简报的「自选股异动」拿转债代码去查公告；
- 配额日耗把转债也算进「每只 1 次/天」。

判断依据是 `stock_basic` 里有没有这个代码 —— 转债只在 `cb_basic` 里，两表代码不重叠。
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import StockBasic, Watchlist

#: 可转债代码前缀：沪 110/111/113/118/132、深 123/127/128。
#: 与前端 `lib/bondCode.ts` 同一套；A 股没有以这些开头的代码。
BOND_PREFIXES = ("110", "111", "113", "118", "132", "123", "127", "128")


def is_bond_code(code: str) -> bool:
    """按代码前缀判断是不是转债（不查库，给「任何入口都兜住」用）。"""
    return bool(code) and code.startswith(BOND_PREFIXES)


def stock_watch_codes(session: Session, user_id: int | None = None) -> list[str]:
    """自选里的**股票**代码（去重）。`user_id` 为空时取全站并集。"""
    statement = (
        select(Watchlist.code)
        .distinct()
        .where(Watchlist.code.in_(select(StockBasic.code)))
    )
    if user_id is not None:
        statement = statement.where(Watchlist.user_id == user_id)
    return list(session.scalars(statement))


def filter_stock_codes(session: Session, codes: list[str]) -> list[str]:
    """把一批代码里的转债剔掉，保持传入顺序。"""
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
