"""形态选股的股票池：拉全市场 A 股，按流动性过滤。

池子的唯一用途是「今天要采哪些票的日线」，所以它要同时满足两条：
**覆盖该覆盖的**，且**不为僵尸票白花配额**。

### 为什么按代码前缀分段

选股接口的结果集 CSV 上限 **1000 行**，而全 A 有 5569 只，一次拿不完。

前缀分段的好处是它**天然互斥且完备** —— 每个 6 位代码只属于一个前缀，
所以既不会重复也不会漏。换成「按市值区间」会在边界上重复或遗漏，
「按板块」则更糟：实测 iFinD 会把「沪市主板的A股」直接理解成「全部A股」
（matched=5569），限定词被整个忽略。

实测各段规模（2026-09-18）：600=747、300=936、002=919、688=617、000=412、
601=227、920=348，全部在 1000 以内。若将来某段涨破 1000，会自动往后多取一位
把它拆开（`_crawl_prefix` 的递归），而不是静默截断。

**已知缺口**：北交所旧代码段（43/83/87/88）用「以…开头」问不出来（matched=0），
所以池子只含沪深两市与北交所的 920 段。
"""

import logging
import re
from collections.abc import Callable, Iterable
from datetime import date, datetime

from sqlalchemy import delete, func, select

from app.config import Settings, get_settings
from app.db import session_scope, upsert_many
from app.models import StockBasic, StockUniverse
from app.sources.ifind import IfindClient, IfindError, from_ths_symbol
from app.sources.markdown_table import pick_float, pick_text

logger = logging.getLogger(__name__)

# 代码前缀分段。刻意不用「沪市主板 / 创业板」这类写法，实测不认（见模块说明）。
PREFIXES = (
    "600", "601", "603", "605",  # 沪市主板
    "688", "689",  # 科创板
    "000", "001", "002", "003",  # 深市主板（含原中小板）
    "300", "301", "302",  # 创业板
    "920",  # 北交所（只有这一段问得出来）
)

# 选股接口结果集 CSV 的行数上限。超过就必须细分。
SCREEN_ROW_LIMIT = 1000
# 前缀细分的最大深度。2 位不够就到 5 位（如 300 → 3001），实际不会走到
MAX_PREFIX_DEPTH = 2

# 池子多久重建一次。流动性变化很慢，而重建一次要吃 19 次调用 ——
# 每天重建的话一个月要多花 400 次配额，不值。
REBUILD_INTERVAL_DAYS = 7

# 建池时一并取回来的指标。**加列不增加调用次数**（同一个选股请求，多要几列而已），
# 所以顺手把概况格要用的市值 / 自由流通股 / 预测市盈率也带上，落进 `stock_basic`。
#
# ⚠️ 「自由流通股」不要写成「自由流通股本」或「自由流通市值」：`pick` 是**子串**匹配，
# 而返回的列名里同时有 自由流通股 / 自由流通市值 —— 写成后者会命中错的那一列。
# 「预测市盈率」同理：返回的列名里还有 市盈率(pe) / 市盈率(pe,ttm)，
# 只有「预测市盈率」是**动态市盈率**（同花顺口径，按分析师预测净利润算）。
_UNIVERSE_COLUMNS = (
    "证券代码、证券简称、近20日日均成交额、总市值、自由流通股、预测市盈率(pe,最新预测)"
)

# iFinD 的列名自带数据日，如 `总市值[20260924]`。注意**区间列**是
# `区间日均成交额[20260828-20260924]`（带横杠），所以这里要求中括号里正好 8 位数字。
_DATE_IN_COLUMN = re.compile(r"\[(\d{8})\]")


def _prefix_query(prefix: str) -> str:
    return f"证券代码以{prefix}开头的A股股票的{_UNIVERSE_COLUMNS}"


def _metric_asof(row: dict[str, str]) -> date | None:
    """这行的指标是哪一天的：从**总市值那一列的列名**里解析（`总市值[20260924]`）。

    只认总市值那一列、不扫全行 —— 行里还有 `预测市盈率(pe,最新预测)[20261231]`
    这种**未来日期**，随便扫第一个会拿到 2026-12-31，那就错了。
    """
    column = next((name for name in row if "总市值" in name), None)
    match = _DATE_IN_COLUMN.search(column or "")
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def _basics(raw: Iterable[dict]) -> list[dict]:
    """市值 / 自由流通股 / 预测市盈率 → `stock_basic` 的行。

    **用全量 `raw`，不是过滤后的池子。** 这份数据是「一次问全市场」就带回来的：池子只留
    流动性达标的 3000 只，而池外那两千多只同样值得有名有市值 —— 打开池外个股页面时
    好几个格子不用显示「—」。覆盖范围从 3000 扩到全 A 是**白拿的**（同一次调用）。

    ⚠️ **池跨了也没关系**：这些字段是慢变量，有效期比 7 天的建池周期长得多；
    真正会过期的是市值（随股价变），那个由读取侧按收盘价缩放（见 `api/stock.py`）。
    """
    rows: list[dict] = []
    for row in raw:
        symbol = pick_text(row, "证券代码", "股票代码")
        if not symbol:
            continue
        item = {
            "code": from_ths_symbol(symbol),
            "name": pick_text(row, "证券简称", "股票简称"),
            "total_mv": pick_float(row, "总市值"),
            "free_float_shares": pick_float(row, "自由流通股"),
            "pe_forecast": pick_float(row, "预测市盈率"),
            "asof": _metric_asof(row),
        }
        if (
            item["total_mv"] is None
            and item["free_float_shares"] is None
            and item["pe_forecast"] is None
        ):
            continue  # 三个都没值，别白写一行
        rows.append(item)

    # ⚠️ `upsert_many` 会把「这一批里带了的列」一律覆盖 —— 所以 name 为 None 的行会**抹掉**
    # 已经存好的名字。iFinD 这个查询每行都有简称、正常不会缺；真缺了就整批不带 name，
    # 交给另两个写入方（`sync_stock` / `scan_dde`）去补，不值得冒抹名字的风险。
    if not all(row["name"] for row in rows):
        for row in rows:
            row.pop("name")
    return rows


class UniverseCollector:
    """维护 `stock_universe`（顺带把市值 / 股本 / 市盈率落进 `stock_basic`）。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.ifind = IfindClient(self.settings)

    # ------------------------------------------------------------------ 建池

    def is_stale(self, today: date | None = None) -> bool:
        """池子是否需要重建：没建过，或上次重建超过 `REBUILD_INTERVAL_DAYS` 天。"""
        today = today or date.today()
        with session_scope() as session:
            latest = session.scalar(select(func.max(StockUniverse.updated_at)))
            size = session.scalar(select(func.count()).select_from(StockUniverse)) or 0
        if not size or latest is None:
            return True
        return (today - latest.date()).days >= REBUILD_INTERVAL_DAYS

    def collect(self, today: date | None = None, *, force: bool = False) -> dict:
        """拉全市场 → 过门槛 → 整表替换。池子没过期就什么都不做。"""
        today = today or date.today()
        if not force and not self.is_stale(today):
            with session_scope() as session:
                size = session.scalar(select(func.count()).select_from(StockUniverse)) or 0
            return {"status": "skipped", "reason": "池子未过期", "codes": size}

        raw = self._crawl()
        if not raw:
            raise IfindError("全市场分段查询一条都没拿到，池子保持原样")

        picked = self._filter(raw)
        if not picked:
            # 门槛配错（比如写得过高）会把池子清空，那样当天就完全不采日线了。
            # 宁可报错保留旧池，也不要静默变成空池。
            raise IfindError(
                f"门槛 {self.settings.universe_min_amount:,.0f} 元过滤后一只不剩，"
                f"原始匹配 {len(raw)} 只，请检查 universe_min_amount"
            )

        with session_scope() as session:
            session.execute(delete(StockUniverse))
            session.add_all([StockUniverse(**row) for row in picked])
            # 顺手把市值 / 自由流通股 / 预测市盈率落进 `stock_basic`（见 `_basics`）
            saved = upsert_many(session, StockBasic, _basics(raw))

        logger.info(
            "股票池已重建：全市场 %d 只 → 池子 %d 只（门槛 %.2f 亿）；"
            "顺带更新 %d 只的市值 / 股本 / 市盈率",
            len(raw),
            len(picked),
            self.settings.universe_min_amount / 1e8,
            saved,
        )
        return {
            "status": "ok",
            "matched": len(raw),
            "codes": len(picked),
            "min_amount": self.settings.universe_min_amount,
        }

    def _crawl(self) -> list[dict]:
        """按前缀（必要时逐位细分）拉全市场，返回去重后的原始行。"""
        found, _ = crawl_prefixes(self.ifind, _prefix_query, "股票池")
        return list(found.values())

    def _filter(self, raw: Iterable[dict]) -> list[dict]:
        """按日均成交额门槛过滤，顺便把名称、成交额、市值落下来。"""
        threshold = self.settings.universe_min_amount
        picked: list[dict] = []
        for row in raw:
            amount = pick_float(row, "日均成交额")
            if amount is None or amount < threshold:
                continue
            symbol = pick_text(row, "证券代码", "股票代码")
            if not symbol:
                continue
            picked.append(
                {
                    "code": from_ths_symbol(symbol),
                    "name": pick_text(row, "证券简称", "股票简称"),
                    "avg_amount": amount,
                    "total_mv": pick_float(row, "总市值"),
                }
            )
        return picked


def load_codes() -> list[str]:
    """当前池子里的 6 位代码。"""
    with session_scope() as session:
        return list(session.scalars(select(StockUniverse.code)))


def crawl_prefixes(
    client: IfindClient, query_of: Callable[[str], str], what: str
) -> tuple[dict[str, dict], int]:
    """按代码前缀（必要时逐位细分）拉一份全市场结果。

    返回 `({6 位代码: 原始行}, 实际调用次数)`。

    选股接口的结果集 CSV 上限 1000 行、全 A 有 5569 只，一次拿不完；前缀分段
    天然互斥且完备（见模块说明），所以两个调用方共用这一份爬取逻辑 ——
    建池时问流动性指标，采日线时问某一天的行情。

    **共用而不是各写一份的理由是完备性校验在这里**：`matched` 与 `len(rows)`
    一比就知道这段取全了没有，少这一道就是又一个静默缺票。`what` 只进日志，
    免得打出来不知道在拉什么。
    """
    found: dict[str, dict] = {}
    calls = 0
    for prefix in PREFIXES:
        calls = _crawl_prefix(client, query_of, prefix, 0, found, what, calls)
    return found, calls


def _crawl_prefix(
    client: IfindClient,
    query_of: Callable[[str], str],
    prefix: str,
    depth: int,
    found: dict[str, dict],
    what: str,
    calls: int,
) -> int:
    matched, rows = client.search_stocks_full(query_of(prefix))
    calls += 1

    if matched is None:
        # matched 是判断「这段取全了没有」的唯一依据，缺了就没法保证完备性
        raise IfindError(f"选股接口未返回 matched，无法确认前缀 {prefix} 段是否取全")

    if matched > SCREEN_ROW_LIMIT and depth < MAX_PREFIX_DEPTH:
        logger.info(
            "%s：前缀 %s 匹配 %d 只超过结果集上限，往后细分成 10 段",
            what,
            prefix,
            matched,
        )
        for digit in "0123456789":
            calls = _crawl_prefix(
                client, query_of, prefix + digit, depth + 1, found, what, calls
            )
        return calls

    if matched > len(rows):
        # 拆到底还是拿不全：**不能静默接受**，否则缺票而没人知道
        raise IfindError(
            f"前缀 {prefix} 匹配 {matched} 只，只取回 {len(rows)} 只，"
            f"已细分到 {depth} 位仍超限"
        )

    for row in rows:
        symbol = pick_text(row, "证券代码", "股票代码")
        if not symbol:
            continue
        # 库里存 6 位裸代码，与 stock_daily 保持一致；上层要用时再补后缀
        found[from_ths_symbol(symbol)] = row
    return calls
