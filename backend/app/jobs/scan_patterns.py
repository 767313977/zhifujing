"""全市场形态扫描：读日线 → 复权 → 逐形态判定 → 落 `pattern_hit`。

**这一步对「通用形态」不花 iFinD 配额** —— 形态全在本地算，实测 3032 只 0.7 秒。
辉宾对齐悟道时，会对**池外候选**按需补近端日线（见 `_ensure_wudao_kline`），
那一小段才吃配额；候选**已有 ≥22 根连续 K 线、且最新一根就是当天**则 0 次调用
（两个条件缺一不可 —— 只有根数够但不含当天，就是拿旧 K 线出今天的信号，见 8.64）。

## 为什么整段重算而不是增量

形态看的是「最近 N 根 K 线的形状」，而 N 最长 249。今天补上一根新 K 线，
昨天那些票的形态可能全都变了（也可能没变）。增量更新得知道「哪些票的形状
被新数据影响了」，而这个问题没有便宜的解。整段重算是 0.7 秒，不值得为它做增量。

## 为什么先删后插

同一天重复扫描必须幂等 —— 手工补扫、定时任务撞车都会发生。所以按
`trade_date` 整段替换，而不是 upsert：**形态是会消失的**。昨天命中「平台突破」
的票今天可能已经跌回平台里，upsert 会把旧命中永久留在表里，榜单越看越假。

## 致富（原辉宾）与悟道对齐

**2026-10-08 起三个池子全部按原型口径收窄**（用户看到「和他选出来的不一样」后定的，
口径与对照见设计文档 §8.83）：

| 池子 | 候选来源与截断 |
| --- | --- |
| 致富三兄弟（明天盯 / 今天可买 / 洗完可盯） | 创业板候选，按来源优先级排序后**前 50** |
| 华宝早期 | **创业板候选 50 + 主板候选 35**，且该票不是样板/启动 |
| 强达型（洗后可盯 / 缩量洗盘中） | **创业板+主板候选，按插入顺序前 60** |

- 候选五路来源（与原型 `pattern._candidate_rows` 逐条一致）：东财强势 / 涨停 / 昨涨停 +
  涨幅榜前 100 + 悟道本地近端缓存；加池顺序也是原型的
  **强势 → 涨停 → 昨涨停 → 涨幅 → 本地**。
- ⚠️ **一只票归到「最先加进来的那个来源」**（原型 `add()` 只在首次见到时写 `source`，
  之后只往后拼字符串、排序时取 `source.split(",")[0]`）。所以我们这里用 `min(优先级)`
  是**错的** —— 会把「既在强势池、又在涨幅榜」的票从「强势档」提到「涨幅档」，
  池子内容跟着变（2026-10-08 修）。
- 板块 = **创业板（300/301/302）**，主板另算（`is_mainboard`）。2026-09-29 一度把创业板
  扩到「创业板 + 科创板」，10-08 按用户要求**退回原型口径**：科创板（688/689）在任何
  一个池子里都不出现 —— 原型那边 `only_chinext=True` 只认 `is_chinext`（300/301），
  而主板/强达那两路又都把 688/689 排掉了。
  ⚠️ 我们比原型多留了 **302**（创业板新号段，原型 `is_chinext` 里没有）—— 只差这一只。
- 涨幅榜窗口：创业板 `4.5~20.5%`、全市场版 `4.5~16%`（原型的 `hi` 就是这两个值）。
  板块要**分开请求**（每页只给约 100 行，混在一起会互相挤掉），见 `_spot_rows`。
- 池外缺 K 线时从悟道库 / 东财补近端（见 `_ensure_wudao_kline`，覆盖三个池的并集）。
"""

import logging
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select

from app.config import Settings, get_settings
from app.db import session_scope, upsert_many
from app.jobs.collect_universe import load_codes
from app.models import (
    CollectLog,
    PatternHit,
    StockBasic,
    StockDaily,
    StockUniverse,
    TradeCalendar,
)
# 东财/腾讯两条兜底线的抓取逻辑都抽到了 `sources/` —— 本地回测往回补多年历史
# （scripts/backfill_history.py）也要用它们。口径（不复权价 + 真实涨跌幅）只能有一份。
from app.sources.eastmoney import clear_proxies as _clear_proxies
from app.sources.eastmoney import fetch_board_spot as _fetch_board_spot_em
from app.sources.eastmoney import fetch_daily as _fetch_daily_eastmoney
from app.sources.tencent import fetch_daily as _fetch_tencent_daily
from app.sources.ths_rank import fetch_board_spot as _fetch_board_spot_ths
from app.services.patterns import (
    MIN_SCORE,
    WUDAO_MIN_BARS,
    Bars,
    build_bars,
    evaluate,
    is_st,
)
from app.sources.ifind import IfindError

logger = logging.getLogger(__name__)

# 扫描时每只票取多少根 K 线。要够 249 日新高用（最长窗口 + 1），再留一点余量
SCAN_BARS = 260

# 采集日志里的一类任务名
PATTERN_TASK = "patterns"

# 扫某一天之前，要求那一天在库里至少覆盖池子的这个比例（见 `_require_bars`）。
# 90% 是「明显没采全」的底线：正常日子缺失约 0.3%（停牌/次新），
# 而 2026-09-24 那种抽风是 9% —— 卡在 90% 能拦住整片缺，又不至于因为几只停牌票就不让扫。
_MIN_DAY_COVERAGE = 0.9

# ---- 悟道（原型）各池的候选口径。2026-10-08 按原型逐条对齐，改动前先读模块顶部的表 ----

# 致富三兄弟（明天盯 / 今天可买 / 洗完可盯）：只在**创业板候选前 50** 里出信号
WUDAO_KEYS = frozenset({"wudao_sample", "wudao_start", "wudao_wash2"})

# 强达型（洗后可盯 / 缩量洗盘中）：只在**创业板+主板候选前 60** 里出信号
PILE_KEYS = frozenset({"pile_wash_ready", "pile_wash_wash"})

# 华宝早期：在**创业板候选 50 + 主板候选 35** 里出，且排掉已经是样板/启动的票。
# 它的 key 单独拎出来：上面那两类池子都用不上它，而它自己要多一道过滤。
HUABAO_KEY = "huabao_early"

# 候选池的截断条数 —— 三个数都是原型里的实参，别顺手改：
#   致富 = `sorted(cands)[:max(limit,20)]`，原型 `limit=50` → 50
#   主板 = `sorted(main)[:max(35,limit//2)]`，原型 `limit=50` → 35
#   强达 = `cands[:max(limit,40)]`，原型 `limit=60` → 60（**按插入顺序**，不排序）
WUDAO_SCAN_LIMIT = 50
WUDAO_MAIN_LIMIT = 35
PILE_SCAN_LIMIT = 60

# 涨幅榜这一路的窗口与取数上限。
#
# 下界挡掉「不算强势」的，上界挡掉「首日无涨跌幅限制的新股」（实测榜上能到 +653%）。
# **上界随口径变**（原型 `hi = 20.5 if only_chinext else 16.0`）：创业板版放 20.5
# （按 20cm 板校准），全市场版只放到 16。
# 上限 100 是因为服务端**每页最多约 100 行**，要更多只能翻页 —— 而目标池最多 60 个位
# 置、涨幅那一路优先级最高会先占满，所以 100 够。
WUDAO_GAIN_MIN = 4.5
WUDAO_GAIN_CYB_MAX = 20.5
WUDAO_GAIN_ALL_MAX = 16.0
WUDAO_GAIN_TOP = 100

# 池外致富候选补多少交易日日线（日历日粗算 ×1.5 在 sync_stock 内）
_WUDAO_SYNC_DAYS = 60

# 悟道之路本地日线库
# __file__ = .../zhifujing/backend/app/jobs/scan_patterns.py → parents[4]=cursorzhb
_WUDAO_MARKET_DB = Path(__file__).resolve().parents[4] / "data" / "market.db"

# 与原型 `pattern._candidate_rows` 排序一致：涨幅 > 强势 > 昨涨停 > 本地 > 涨停。
#
# ⚠️ 它只用来**排序**（`sorted(key=...)`）；一只票归到哪一档看的是「**最先加进来的那个
# 来源**」，不是「优先级最高的来源」—— 见 `_candidate_rows` 里 `add()` 的注释。
_WUDAO_SRC_PRIORITY = {"涨幅": 0, "强势": 1, "昨涨停": 2, "本地": 3, "涨停": 4}


class _NoBars(Exception):
    """这一级**没有这只票的数据** —— 与「这条源挂了」是两回事。

    退市股、还没上市的代码、以及腾讯那边根本没有的代码都属于这一类。实测（2026-09-24）
    300038 数知退、300060 都是这种：东财/腾讯都没有它们的近端日线，iFinD 却会给一行
    只有收盘价、其余全空的行。**「没有数据」不该记进熔断计数** —— 几个退市股就能凑够
    3 次、把一整条源整轮关掉，那才是真的亏。
    """


# 创业板前缀。**只有这三段**（300/301/302）—— 科创板 2026-10-08 起不再进任何池子。
# ⚠️ 原型 `is_chinext` 只认 300/301，我们多留了 302（创业板新号段，如 302132 中航成飞）。
_WUDAO_BOARD_PREFIXES = ("300", "301", "302")

# 沪深主板前缀（原型 `is_mainboard`）：600/601/603/605/000/001/002/003。
# 688/689（科创）、8/4（北交所）、9（B 股）都不算。
_MAINBOARD_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")


def is_wudao_board(code: str) -> bool:
    """创业板（300/301/302）。致富候选与「明天盯 / 今天可买」只收这一块。

    ⚠️ 名字故意不叫 `is_chinext` —— 2026-09-29 ~ 10-08 之间它一度还认科创板，
    叫 `is_chinext` 会骗人。调用点在候选池、涨幅榜过滤、以及扫描末段的落库闸门。
    """
    c = str(code or "").strip().zfill(6)
    return c.startswith(_WUDAO_BOARD_PREFIXES)


def is_mainboard(code: str) -> bool:
    """沪深主板（含中小板 002/003）。华宝早期与强达型会额外收这一块。"""
    c = str(code or "").strip().zfill(6)
    if is_wudao_board(c):
        return False
    if c.startswith(("688", "689", "8", "4", "9")):
        return False
    return c.startswith(_MAINBOARD_PREFIXES)


def already_scanned(trade_date: date) -> bool:
    """该交易日是否已经扫过。

    只用来在日志上留个记号并避免同一轮里重复打印，**不承担正确性职责** ——
    重扫是幂等的、只要 0.7 秒、还零配额，所以「多扫一次」没有代价。
    """
    return _scanned_marker(trade_date)


def _scanned_marker(trade_date: date) -> bool:
    with session_scope() as session:
        return bool(
            session.scalar(
                select(CollectLog.trade_date)
                .where(
                    CollectLog.trade_date == trade_date,
                    CollectLog.task == PATTERN_TASK,
                    CollectLog.status == "ok",
                )
                .limit(1)
            )
        )


def _record(
    trade_date: date, status: str, rows: int, message: str | None, cost: float | None = None
) -> None:
    with session_scope() as session:
        session.add(
            CollectLog(
                trade_date=trade_date,
                task=PATTERN_TASK,
                status=status,
                rows=rows,
                message=message,
                cost_seconds=cost,
            )
        )


def _load_bars(
    trade_date: date, codes: list[str] | set[str], *, label: str = "股票池"
) -> dict[str, list[dict]]:
    """取出最近 `SCAN_BARS` 个交易日内指定代码的日线，按代码分组。

    一定要显式给代码名单，不能把 `stock_daily` 里的票全拿来扫：库里还有一批
    **池外的涨停股**（见 8.22.4，每天只给它们补当天那一根）。它们的序列是断的 ——
    连着两天涨停才有两行相邻，其余日子是空的 —— 而形态引擎会把**相邻的行**
    当成**相邻的交易日**，等于喂进去一条带空洞的 K 线，正是这套引擎最怕的输入。

    辉宾池外候选必须先经 `_ensure_wudao_kline` 补成连续近端，再放进这份名单。
    """
    codes = list(codes)
    if not codes:
        raise IfindError(f"{label}为空，先建池或补候选")

    with session_scope() as session:
        dates = list(
            session.scalars(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date <= trade_date)
                .order_by(TradeCalendar.trade_date.desc())
                .limit(SCAN_BARS)
            )
        )
        if not dates:
            raise IfindError(f"{trade_date} 之前没有交易日历数据")
        start = min(dates)

        rows = session.execute(
            select(
                StockDaily.code,
                StockDaily.name,
                StockDaily.trade_date,
                StockDaily.open,
                StockDaily.high,
                StockDaily.low,
                StockDaily.close,
                StockDaily.volume,
                StockDaily.amount,
                StockDaily.pct_chg,
            )
            .where(
                StockDaily.trade_date >= start,
                StockDaily.trade_date <= trade_date,
                StockDaily.code.in_(codes),
            )
            .order_by(StockDaily.code, StockDaily.trade_date)
        ).all()

    grouped: dict[str, list[dict]] = defaultdict(list)
    for code, name, day, open_, high, low, close, volume, amount, pct in rows:
        # 涨跌幅为空的行在采集时就已经滤掉了，这里再挡一道：
        # 少了它 build_bars 会把停牌日当成 0% 涨跌，前复权序列直接失真
        if close is None or pct is None:
            continue
        grouped[code].append(
            {
                "date": day,
                "name": name,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
                "pct_chg": pct,
            }
        )
    return grouped


# 涨幅榜的两个口径 —— 原型对每个口径各取一页，**板块筛在前、取前 100 在后**
# （`spot = spot[is_chinext]` 在 `.head(100)` 之前），所以「创业板口径」要的是创业板
# 自己的榜，不是「全市场前 100 里恰好属于创业板的那几只」。
_SPOT_SCOPES = ("cyb", "all")

# 备源（同花顺）按前缀筛：创业板口径只认创业板，全市场口径收全部 A 股
# （含科创板 —— 全市场榜里本来就有它们，只是下游所有池子都限制在创业板/主板）
_SPOT_PREFIXES = {
    "cyb": _WUDAO_BOARD_PREFIXES,
    "all": _WUDAO_BOARD_PREFIXES + _MAINBOARD_PREFIXES + ("688", "689"),
}


def _spot_rows(scope: str) -> list[tuple[str, float]]:
    """涨幅榜那一路：`scope` 那个口径按涨幅降序的一页（**不截窗口**，由调用方过滤）。

    `scope` 见 `_SPOT_SCOPES`；两块**分开请求** —— 服务端每页只给约 100 行，混在一起
    会互相挤掉（原型是先按板筛、再取前 100）。

    旧实现是 akshare 的 `stock_zh_a_spot_em()`：`pz=100` 翻 56 页把全市场 5561 只拉回来、
    只为排序取前 100。2026-09-29 实测那 56 次连发会把这个 IP 打进东财 `push2*` 集群的
    惩罚期（> 10 分钟，期间连 `push2his` 的其它路径一起被拒）。换成「服务端按涨幅降序 +
    一次请求」就够（56 → 2，两个口径各一页）。

    **东财不可用就退到同花顺**（2026-09-29 用户要求）：走数据中心那张按涨跌幅降序的排行
    表，与东财完全独立，代价是翻几页、且解析的是 HTML 表格。两条都不通时这一路本轮为空
    —— 只影响池子内容，另外四路照常。
    """
    try:
        rows = _fetch_board_spot_em(limit=WUDAO_GAIN_TOP, board=scope)
        logger.info("涨幅榜(%s/东财)：拿到 %d 只", scope, len(rows))
        return rows
    except Exception as exc:  # noqa: BLE001
        logger.warning("拉东财 %s 涨幅榜失败，退到同花顺：%s", scope, exc)
    try:
        # 上界统一给最宽的那个（创业板 20.5），按口径收窄留给调用方 —— 备源多几行无妨
        rows = _fetch_board_spot_ths(
            boards=_SPOT_PREFIXES[scope],
            min_pct=WUDAO_GAIN_MIN,
            max_pct=WUDAO_GAIN_CYB_MAX,
            want=WUDAO_GAIN_TOP,
        )
        logger.info("涨幅榜(%s/同花顺)：拿到 %d 只", scope, len(rows))
        return rows
    except Exception as exc:  # noqa: BLE001
        logger.warning("拉同花顺 %s 涨幅榜也失败，这一路本轮为空：%s", scope, exc)
        return []


def _zt_rows(trade_date: date) -> list[tuple[str, list[tuple[str, str]]]]:
    """三张涨停池表，顺序就是原型的加池顺序：强势 → 涨停 → 昨涨停。

    ⚠️ 只拉**一次**、给两个口径复用 —— 这一路每张表一次请求，两个口径各拉一遍就是 6 次。
    某一张失败只跳过它（原型的 `try/except` 也是这样），不影响另外两张。
    """
    out: list[tuple[str, list[tuple[str, str]]]] = []
    try:
        import akshare as ak
    except Exception as exc:  # noqa: BLE001
        logger.warning("akshare 不可用，候选只剩涨幅榜与本地库：%s", exc)
        return out

    _clear_proxies()
    ymd = trade_date.strftime("%Y%m%d")
    loaders = (
        ("强势", lambda: ak.stock_zt_pool_strong_em(date=ymd)),
        ("涨停", lambda: ak.stock_zt_pool_em(date=ymd)),
        ("昨涨停", lambda: ak.stock_zt_pool_previous_em(date=ymd)),
    )
    for label, fn in loaders:
        try:
            df = fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("拉东财%s池失败：%s", label, exc)
            continue
        if df is None or getattr(df, "empty", True) or "代码" not in df.columns:
            continue
        name_col = "名称" if "名称" in df.columns else None
        out.append(
            (
                label,
                [
                    (rec.get("代码"), rec.get(name_col) or "" if name_col else "")
                    for rec in df.to_dict("records")
                ],
            )
        )
    return out


def _local_rows(trade_date: date) -> list[str]:
    """悟道本地库近端的代码（原型这一路读的是它自己的 `kline`，近 10 天）。"""
    db = _WUDAO_MARKET_DB
    if not db.is_file() or db.stat().st_size == 0:
        return []
    import sqlite3

    cutoff = (trade_date - timedelta(days=14)).isoformat()
    with sqlite3.connect(str(db)) as conn:
        return [row[0] for row in conn.execute(
            "SELECT DISTINCT code FROM kline WHERE trade_date >= ?", (cutoff,)
        )]


def _candidate_rows(
    trade_date: date,
    *,
    only_chinext: bool,
    spots: dict[str, list[tuple[str, float]]],
    zt_rows: list[tuple[str, list[tuple[str, str]]]],
) -> list[dict]:
    """一个口径的候选表，返回**插入顺序**的 `[{code, name, source}, …]`。

    与原型 `pattern._candidate_rows` 逐条对齐：加池顺序 = 强势 → 涨停 → 昨涨停 → 涨幅 →
    本地；`source` 记**最先加进来的那一个**（原型 `add()` 只在首次见到时写 `source`，
    之后只往后拼字符串、排序时取 `source.split(",")[0]`）。

    ⚠️ 所以这里**不能**写成「取优先级最高的来源」—— 那样「既在强势池、又在涨幅榜」的票
    会被从强势档提到涨幅档、排序位置前移，池子内容跟着变（2026-10-08 修，之前是错的）。

    涨幅榜窗口随口径变：创业板版 `4.5~20.5%`、全市场版 `4.5~16%`（原型 `hi` 的两个值）。
    """
    gain_max = WUDAO_GAIN_CYB_MAX if only_chinext else WUDAO_GAIN_ALL_MAX
    seen: dict[str, dict] = {}

    def add(code, name, source: str) -> None:
        c = str(code or "").strip().zfill(6)
        # 原型的过滤：北交所（4/8）与 B 股（9）不收
        if len(c) != 6 or c.startswith(("4", "8", "9")):
            return
        if only_chinext and not is_wudao_board(c):
            return
        if c not in seen:
            seen[c] = {"code": c, "name": str(name or c), "source": source}

    for label, rows in zt_rows:
        for code, name in rows:
            add(code, name, label)

    for code, pct in spots["cyb" if only_chinext else "all"]:
        if not (WUDAO_GAIN_MIN <= pct <= gain_max):
            continue
        # 两个口径的榜都可能是全市场的（备源那条就是），所以这里**必须**再按板块筛一道
        if only_chinext and not is_wudao_board(code):
            continue
        add(code, "", "涨幅")

    try:
        for code in _local_rows(trade_date):
            add(code, "", "本地")
    except Exception as exc:  # noqa: BLE001
        logger.warning("读悟道本地候选失败：%s", exc)

    return list(seen.values())


def _wudao_pools(trade_date: date) -> dict[str, set[str]]:
    """当天三个候选池的代码集合。键：`cyb` / `huabao` / `pile` / `all`（并集，补日线用）。

    三处截断都是原型的实参，条数与来源见 `WUDAO_SCAN_LIMIT` 那一段注释：
    · `cyb`   —— 创业板候选，按 (来源优先级, 代码) 排序后前 50（致富三兄弟）
    · `huabao`—— `cyb` ∪ 主板候选前 35（华宝早期）
    · `pile`  —— 创业板+主板候选，**按插入顺序**前 60（强达型）
    """
    spots = {scope: _spot_rows(scope) for scope in _SPOT_SCOPES}
    zt_rows = _zt_rows(trade_date)
    cyb_rows = _candidate_rows(trade_date, only_chinext=True, spots=spots, zt_rows=zt_rows)
    all_rows = _candidate_rows(trade_date, only_chinext=False, spots=spots, zt_rows=zt_rows)

    def rank(rows: list[dict]) -> list[dict]:
        return sorted(
            rows, key=lambda r: (_WUDAO_SRC_PRIORITY.get(r["source"], 9), r["code"])
        )

    cyb = rank(cyb_rows)[:WUDAO_SCAN_LIMIT]
    main = rank([r for r in all_rows if is_mainboard(r["code"])])[:WUDAO_MAIN_LIMIT]
    # 强达这一路原型**不排序**：`cands = [过滤后…]`，紧接着 `cands[:max(limit,40)]`
    pile = [
        r for r in all_rows if is_wudao_board(r["code"]) or is_mainboard(r["code"])
    ][:PILE_SCAN_LIMIT]
    logger.info(
        "悟道候选：创业板 %d / 主板 %d / 强达 %d（涨幅榜 创业板 %d 只、全市场 %d 只）",
        len(cyb),
        len(main),
        len(pile),
        len(spots.get("cyb") or []),
        len(spots.get("all") or []),
    )
    cyb_codes = {r["code"] for r in cyb}
    main_codes = {r["code"] for r in main}
    pile_codes = {r["code"] for r in pile}
    return {
        "cyb": cyb_codes,
        "huabao": cyb_codes | main_codes,
        "pile": pile_codes,
        "all": cyb_codes | main_codes | pile_codes,
    }


def _in_wudao_pool(
    pattern: str, code: str, pools: dict[str, set[str]], huabao_skip: set[str]
) -> bool:
    """这条悟道信号允不允许落库 —— **非悟道形态一律放行**（照旧扫全池）。"""
    if pattern in WUDAO_KEYS:
        return code in pools["cyb"]
    if pattern in PILE_KEYS:
        return code in pools["pile"]
    if pattern == HUABAO_KEY:
        return code in pools["huabao"] and code not in huabao_skip
    return True


def _bar_counts(codes: set[str], trade_date: date, *, lookback: int = 40) -> dict[str, int]:
    """近端 lookback 个交易日内，各代码已有多少根日线。"""
    if not codes:
        return {}
    with session_scope() as session:
        dates = list(
            session.scalars(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date <= trade_date)
                .order_by(TradeCalendar.trade_date.desc())
                .limit(lookback)
            )
        )
        if not dates:
            return {}
        start = min(dates)
        rows = session.execute(
            select(StockDaily.code, func.count())
            .where(
                StockDaily.code.in_(codes),
                StockDaily.trade_date >= start,
                StockDaily.trade_date <= trade_date,
            )
            .group_by(StockDaily.code)
        ).all()
    return {str(code).zfill(6): int(n) for code, n in rows}


def _last_bars(codes: set[str]) -> dict[str, date]:
    """各代码在库里的**最后一根 K 线是哪天**（不限窗口）。

    用来判断这份日线是不是**停在过去** —— 池外候选、以及「当天那根没采到的池内票」，
    都会表现为「有几十根 K 线、但最新的那根不是今天」，见 8.64。

    ⚠️ 只数 `pct_chg` 非空的行，与 `_load_bars` 的口径一致：iFinD 对停牌日会给一行
    「只有收盘价、其余全空」的残行（`sync_stock` 那条路照写不误），它算不了一根 K 线，
    却会把「最新一根」顶到当天、让这类票逃过补采。
    """
    if not codes:
        return {}
    with session_scope() as session:
        rows = session.execute(
            select(StockDaily.code, func.max(StockDaily.trade_date))
            .where(StockDaily.code.in_(codes), StockDaily.pct_chg.is_not(None))
            .group_by(StockDaily.code)
        ).all()
    return {str(code).zfill(6): day for code, day in rows}


def _sync_stock_eastmoney(code: str, *, days: int = _WUDAO_SYNC_DAYS) -> int:
    """池外致富候选的日线兜底：东财不复权 K 线 → `stock_daily`。

    iFinD 401 / 配额紧张时走这条。只写近端 `days` 个日历日，够量比窗口即可。

    抓取与口径都在 `app.sources.eastmoney`（本地回测往回补历史也用它，见
    `scripts/backfill_history.py`）—— 口径只有一份，别在这里再写一遍。
    """
    rows = _fetch_daily_eastmoney(code, days=days)
    if not rows:
        # 「没有数据」（退市股、未上市代码）与「源挂了」是两回事，见 `_NoBars`
        raise _NoBars("东财无数据")
    with session_scope() as session:
        return upsert_many(session, StockDaily, rows)


def _sync_stock_tencent(code: str, *, days: int = _WUDAO_SYNC_DAYS) -> int:
    """池外候选补日线（第二级兜底）：腾讯 K 线 → `stock_daily`。**不占 iFinD 配额**。

    为什么加这一级：云端连不上东财直连（8.51.3），那一级必然失败，于是每天几十只候选
    全落到 iFinD 兜底（实测 2026-09-24：48 只 = 48 次调用）。腾讯这条线**云端可达**
    （2026-09-24 在服务器上实测过），把它插进链条就把那几十次配额全省下来。

    **口径对过账**（本机 + 云端各跑一遍 `scripts/probe_akshare_tx.py`）：腾讯「不复权」的
    开高低收 / 成交量(股) / 成交额(元) 与库里 iFinD 写的值**逐位相同**（成交量有 14 股的
    舍入差，腾讯是整百股）；唯一的单位差是 `turnover` —— 腾讯给**比例** 0.0015、
    本站库里是**百分数** 0.15，故 ×100。

    **涨跌幅必须由前复权序列算，不能用不复权收盘价比**：除权日不复权价会跳空，
    比出来是一个假的暴跌。腾讯的 `qfq` 相邻收盘之比才是真实收益率（与 iFinD 的
    `涨跌幅` 对账：48 行、差异 >0.02 的 0 行）。

    抓取与口径（不复权价 + 由前复权推涨跌幅 + `turnover` ×100）都在 `app.sources.tencent`
    —— 本地补多年历史（`scripts/backfill_history.py`）用的是同一个函数。
    这一段只负责查名字与写库，口径只能有一份。
    """
    end = date.today()
    # 多留两周：前复权序列的**第一行**没有前值、算不出涨跌幅，那一行会被丢掉，
    # 多取的这段保证丢完之后仍在 `days` 个交易日以上
    start = end - timedelta(days=int(days * 1.5) + 14)
    rows = _fetch_tencent_daily(code, start=start, end=end)
    if not rows:
        raise _NoBars(f"腾讯无 {code} 日线")

    with session_scope() as session:
        name = session.scalar(
            select(StockBasic.name).where(StockBasic.code == str(code).zfill(6))
        )
    for row in rows:
        row["name"] = name or str(code).zfill(6)
    with session_scope() as session:
        return upsert_many(session, StockDaily, rows)


def _sync_stock_from_wudao(code: str, *, days: int = _WUDAO_SYNC_DAYS) -> int:
    """从本机悟道之路 `data/market.db` 抄近端日线进致富经 `stock_daily`。"""
    import sqlite3

    db = _WUDAO_MARKET_DB
    if not db.is_file() or db.stat().st_size == 0:
        raise RuntimeError(f"悟道日线库不可用：{db}")
    cutoff = (date.today() - timedelta(days=int(days * 1.5) + 5)).isoformat()
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        name_row = conn.execute(
            "SELECT name FROM stocks WHERE code = ? LIMIT 1", (code,)
        ).fetchone()
        name = (name_row["name"] if name_row else None) or code
        src = conn.execute(
            """
            SELECT trade_date, open, high, low, close, volume, amount, pct_chg
            FROM kline
            WHERE code = ? AND trade_date >= ?
            ORDER BY trade_date
            """,
            (code, cutoff),
        ).fetchall()
    if not src:
        raise RuntimeError(f"悟道库无 {code} 近端日线")
    rows = []
    for r in src:
        if r["close"] is None or r["pct_chg"] is None:
            continue
        rows.append(
            {
                "trade_date": date.fromisoformat(str(r["trade_date"])[:10]),
                "code": str(code).zfill(6),
                "name": name,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"] or 0),
                "amount": float(r["amount"] or 0) if r["amount"] is not None else None,
                "pct_chg": float(r["pct_chg"]),
            }
        )
    if not rows:
        raise RuntimeError(f"悟道库 {code} 近端无效")
    with session_scope() as session:
        return upsert_many(session, StockDaily, rows)


def _sync_note(sync_info: dict) -> str:
    """采集日志里「补日线用了哪几级」那截说明；都没干活就不写。

    分来源列出来是为了**事后能看出哪一级在扛** —— 腾讯这一级的价值正是让 iFinD 归零，
    没有这个数就只能去翻 logger 才知道它到底生效没有。
    """
    parts = []
    if sync_info.get("via_wudao"):
        parts.append(f"悟道库 {sync_info['via_wudao']} 只")
    if sync_info.get("via_eastmoney"):
        parts.append(f"东财 {sync_info['via_eastmoney']} 只")
    if sync_info.get("via_tencent"):
        parts.append(f"腾讯 {sync_info['via_tencent']} 只")
    if sync_info.get("via_ifind"):
        parts.append(f"iFinD {sync_info['via_ifind']} 只")
    if sync_info.get("skipped_quota"):
        parts.append(f"因 iFinD 上限跳过 {sync_info['skipped_quota']} 只")
    return (" / 补日线 " + "、".join(parts)) if parts else ""


def _ensure_wudao_kline(
    codes: set[str], trade_date: date, settings: Settings | None = None
) -> dict:
    """池外辉宾候选补日线：悟道本地库 → 东财 → 腾讯 → iFinD。

    **什么时候要补**（两个判据，缺一不可）：近端根数不足 `WUDAO_MIN_BARS`，
    **或者最后一根 K 线不是 `trade_date` 那天**。后一条是 8.64 补上的：
    池外候选第一次补完就再也不刷新了（根数早就够），于是它们会一直拿几天前的
    K 线出「今天的信号」。

    **为什么四级**：前两级是「免费又快」（本地库一份不多花；东财一次请求），第三级
    腾讯也是免费的，但没有历史库、要两次请求并自己推涨跌幅；iFinD 是唯一**吃配额**的
    一级，放在最后、且有上限。

    **三道闸门**（后两道 2026-09-24 加，为同时省时间与配额；阈值见 `Settings` 的注释）：

    - **东财熔断**：连续失败 `wudao_em_breaker_failures` 次就本轮不再试它。
      云端连不上东财直连（1.7 / 8.51.3 记过），而这里是**逐只**补 —— 不熔断的话每只都要
      等一次约 10 秒的超时。实测 09-24：48 只候选白等约 8 分钟，且最终全部落到 iFinD。
    - **腾讯熔断**：同一个道理 —— 这条线也有可能整轮不通（接口变动 / 该机器不可达），
      不熔断就是 48 只 × 两次请求 × 超时。
    - **iFinD 上限**：每轮最多补 `wudao_ifind_fallback_max` 只，超出的**本轮不补**
      （当天就没有这些候选的形态信号）。这是刻意的「配额 ↔ 覆盖」取舍，不静默：
      跳过的只数进 `skipped_quota`，日志与采集日志里都会写出来。
      **有了腾讯这一级，它其实很少再触发** —— 只有在腾讯也整轮不通时才轮得到它顶上来。
    """
    settings = settings or get_settings()
    counts = _bar_counts(codes, trade_date)
    last = _last_bars(codes)
    # 两个判据：**根数不够**（池外新股，从没补过）与**停在过去**（补过、之后没再刷）。
    # 少了后一条就是 8.64 那个 bug：池外候选第一次补完就再也不刷新了，
    # 于是 09-24 那天用 09-22 的 K 线出信号，命中列表上的「最新价」是两天前的。
    need = {
        c
        for c in codes
        if counts.get(c, 0) < WUDAO_MIN_BARS or last.get(c) != trade_date
    }
    if not need:
        return {
            "synced": 0,
            "failed": 0,
            "needed": 0,
            "via_wudao": 0,
            "via_eastmoney": 0,
            "via_tencent": 0,
            "via_ifind": 0,
            "skipped_quota": 0,
        }

    from app.jobs.collect_daily import DailyCollector
    from app.sources.ifind import IfindError

    collector = DailyCollector()
    synced = 0
    failed = 0
    via_wudao = 0
    via_em = 0
    via_tencent = 0
    via_ifind = 0
    skipped_quota = 0
    skip_ifind = False
    em_failures = 0
    em_dead = False
    tx_failures = 0
    tx_dead = False
    for code in sorted(need):
        ok = False
        try:
            wrote = _sync_stock_from_wudao(code, days=_WUDAO_SYNC_DAYS)
            synced += 1
            via_wudao += 1
            ok = True
            logger.info("辉宾补日线(悟道库) %s → %d 行", code, wrote)
        except Exception as exc:  # noqa: BLE001
            logger.debug("辉宾补日线(悟道库) %s：%s", code, exc)
        if not ok and not em_dead:
            try:
                wrote = _sync_stock_eastmoney(code, days=_WUDAO_SYNC_DAYS)
                synced += 1
                via_em += 1
                ok = True
                logger.info("辉宾补日线(东财) %s → %d 行", code, wrote)
            except _NoBars as exc:
                # 这只票这一级没有 —— 换下一级，不算这条源挂了
                logger.info("辉宾补日线(东财) %s 无数据，换下一级：%s", code, exc)
            except Exception as exc:  # noqa: BLE001
                em_failures += 1
                if em_failures >= settings.wudao_em_breaker_failures:
                    # 熔断：后面几十只不再一只只等超时。只报一次，别刷屏
                    em_dead = True
                    logger.warning(
                        "辉宾补日线(东财) 连续失败 %d 次（最近一只 %s：%s），"
                        "本轮剩余候选不再试东财，改由腾讯 / iFinD 兜底",
                        em_failures,
                        code,
                        exc,
                    )
                else:
                    logger.warning("辉宾补日线(东财) %s 失败：%s", code, exc)
        if not ok and not tx_dead:
            try:
                wrote = _sync_stock_tencent(code, days=_WUDAO_SYNC_DAYS)
                synced += 1
                via_tencent += 1
                ok = True
                logger.info("辉宾补日线(腾讯) %s → %d 行", code, wrote)
            except _NoBars as exc:
                logger.info("辉宾补日线(腾讯) %s 无数据，换下一级：%s", code, exc)
            except Exception as exc:  # noqa: BLE001
                tx_failures += 1
                if tx_failures >= settings.wudao_tx_breaker_failures:
                    tx_dead = True
                    logger.warning(
                        "辉宾补日线(腾讯) 连续失败 %d 次（最近一只 %s：%s），"
                        "本轮剩余候选不再试腾讯，改由 iFinD 兜底（受上限约束）",
                        tx_failures,
                        code,
                        exc,
                    )
                else:
                    logger.warning("辉宾补日线(腾讯) %s 失败：%s", code, exc)
        if not ok and not skip_ifind:
            if via_ifind >= settings.wudao_ifind_fallback_max:
                # 到上限了：本轮不补它。不记 failed —— 这不是失败，是刻意的取舍
                skipped_quota += 1
                continue
            try:
                wrote = collector.sync_stock(code, days=_WUDAO_SYNC_DAYS)
                synced += 1
                via_ifind += 1
                ok = True
                logger.info("辉宾补日线(iFinD) %s → %d 行", code, wrote)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "401" in msg or isinstance(exc, IfindError):
                    skip_ifind = True
                    logger.warning("iFinD 不可用，本轮不再重试：%s", exc)
                else:
                    logger.warning("辉宾补日线(iFinD) %s 失败：%s", code, exc)
        if not ok:
            failed += 1
    if skipped_quota:
        logger.warning(
            "辉宾补日线：%d 只候选因 iFinD 兜底到上限（%d 只）本轮未补 —— "
            "它们当天没有形态信号；想让覆盖更全就调大 `WUDAO_IFIND_FALLBACK_MAX`",
            skipped_quota,
            settings.wudao_ifind_fallback_max,
        )
    return {
        "synced": synced,
        "failed": failed,
        "needed": len(need),
        "via_wudao": via_wudao,
        "via_eastmoney": via_em,
        "via_tencent": via_tencent,
        "via_ifind": via_ifind,
        "skipped_quota": skipped_quota,
    }


def scan(
    trade_date: date | None = None,
    settings: Settings | None = None,
    *,
    min_score: float = MIN_SCORE,
) -> dict:
    """扫描一个交易日的全市场形态，整段替换该日的命中记录。"""
    settings = settings or get_settings()
    started = time.monotonic()

    target = trade_date or _latest_trade_date()
    _require_bars(target)

    universe = load_codes()
    if not universe:
        raise IfindError("股票池为空，先建池（UniverseCollector.collect）")

    # 悟道三个池子的候选（创业板 50 / 主板 35 / 强达 60）—— **只在这三份名单里**出
    # 悟道家族的信号，其余形态照旧扫全池。
    pools = _wudao_pools(target)
    wudao_cands = pools["all"]
    extras = {c for c in wudao_cands if c not in set(universe)}
    sync_info = (
        _ensure_wudao_kline(wudao_cands, target, settings)
        if wudao_cands
        else {
            "synced": 0,
            "failed": 0,
            "needed": 0,
            "via_wudao": 0,
            "via_eastmoney": 0,
            "via_tencent": 0,
            "via_ifind": 0,
            "skipped_quota": 0,
        }
    )

    scan_codes = set(universe) | extras
    grouped = _load_bars(target, scan_codes)
    if not grouped:
        raise IfindError(f"{target} 没有日线数据，先跑 collect_kline")

    # **日线停在过去的票一律不出信号**。它的「最新」K 线可能是几天前、甚至几周前的，
    # 而命中列表那一列写的是「最新价」—— 混进去就是给用户看旧价（8.64 那次：
    # 列表显示 33.86 / +3.71%，而当天真实收盘是 35.39 / +4.98%）。
    # 上面 `_ensure_wudao_kline` 已经把**候选**里这种票补过一轮，剩下的补不到
    # （当天没采到的池内票、以及真没成交的停牌票），只能不出。
    stale = {code for code, records in grouped.items() if records[-1]["date"] != target}
    for code in stale:
        grouped.pop(code, None)
    if stale:
        logger.warning(
            "%d 只票的日线停在 %s 之前，本轮不出它们的信号（先补日线："
            "`collect_kline` 或 `_ensure_wudao_kline`）",
            len(stale),
            target,
        )

    # **ST 票一律不参与**（2026-09-28 用户要求「形态选股剔除 st 票」）。
    # 放在「日线停在过去」之后、`build_bars` 之前：判据取的是**最新那根 K 线的名字**，
    # 也就是命中列表上要显示的那个名字 —— 用别处的名字（池子里的、`stock_basic` 的）
    # 会有对不上的可能。
    # 致富候选那一支也走这里，所以 ST 涨停股同样进不来（实测 09-24 那天有 111 条）。
    st_codes = {code for code, records in grouped.items() if is_st(records[-1].get("name"))}
    for code in st_codes:
        grouped.pop(code, None)
    if st_codes:
        logger.info("%s：剔除 %d 只 ST 票（形态选股不收 ST）", target, len(st_codes))

    rows: list[dict] = []
    skipped = 0
    dropped_wudao = 0
    by_pattern: dict[str, int] = defaultdict(int)
    bars_map: dict[str, Bars] = {}
    for code, records in grouped.items():
        # 致富只要约 22 根；其它形态内部仍按各自 MIN_BARS 自行跳过
        if len(records) < WUDAO_MIN_BARS:
            skipped += 1
            continue
        bars_map[code] = build_bars(records)

    # 先把每只票的形态全算出来，再决定哪条能落库 —— 华宝早期要排掉**同一只票**上已经
    # 命中的样板/启动（原型 `_early_card_from` 开头就把 start/sample/diverge/dump 退回），
    # 不先算完就没有这个信息。我们只搬了 sample/start 两个阶段，diverge/dump 没有。
    code_sigs: dict[str, list] = {}
    huabao_skip: set[str] = set()
    for code, bars in bars_map.items():
        sigs = list(evaluate(bars, min_score=min_score))
        code_sigs[code] = sigs
        if any(s.pattern in ("wudao_sample", "wudao_start") for s in sigs):
            huabao_skip.add(code)

    for code, sigs in code_sigs.items():
        last = grouped[code][-1]
        for signal in sigs:
            # 悟道各池只收自己候选名单里的票（非悟道形态一律放行）
            if not _in_wudao_pool(signal.pattern, code, pools, huabao_skip):
                dropped_wudao += 1
                continue
            by_pattern[signal.pattern] += 1
            rows.append(
                {
                    "trade_date": target,
                    "code": code,
                    "pattern": signal.pattern,
                    "name": last["name"],
                    "score": round(signal.score, 1),
                    "close": last["close"],
                    "pct_chg": last["pct_chg"],
                    "amount": last["amount"],
                    "key_levels": signal.key_levels,
                    "detail": signal.detail,
                }
            )

    with session_scope() as session:
        session.execute(delete(PatternHit).where(PatternHit.trade_date == target))
        written = upsert_many(session, PatternHit, rows)

    cost = round(time.monotonic() - started, 2)
    pool_brief = (
        f"候选 创{len(pools['cyb'])}/主{len(pools['huabao']) - len(pools['cyb'])}"
        f"/强{len(pools['pile'])}"
    )
    logger.info(
        "形态扫描完成：%s，%d 只票 → %d 条命中"
        "（跳过 %d / 悟道剔池外 %d / %s / 日线停在过去 %d / 剔除 ST %d / 补日线 %s），"
        "用时 %ss",
        target,
        len(grouped),
        written,
        skipped,
        dropped_wudao,
        pool_brief,
        len(stale),
        len(st_codes),
        sync_info,
        cost,
    )
    _record(
        target,
        "ok",
        written,
        f"{len(grouped)} 只 / {written} 条命中 / {pool_brief}"
        + (f" / 日线停在过去剔除 {len(stale)} 只" if stale else "")
        + (f" / 剔除 ST {len(st_codes)} 只" if st_codes else "")
        + _sync_note(sync_info),
        cost,
    )
    return {
        "status": "ok",
        "trade_date": target.isoformat(),
        "codes": len(grouped),
        "skipped": skipped,
        # 日线停在 target 之前、本轮没出信号的只数（>0 就说明当天的日线没采全）
        "stale": len(stale),
        # 因带 ST 被剔除的只数（2026-09-28 起的口径）
        "st_dropped": len(st_codes),
        "wudao_cands": len(wudao_cands),
        "wudao_pools": {key: len(value) for key, value in pools.items()},
        "wudao_extras": len(extras),
        "wudao_sync": sync_info,
        "wudao_dropped": dropped_wudao,
        "rows": written,
        "by_pattern": dict(by_pattern),
        "cost_seconds": cost,
    }


def _require_bars(trade_date: date) -> None:
    """确认库里真的有这一天的日线，而且**大部分票都采到了**。

    少了这道校验，扫描会拿**昨天**的 K 线当今天用 —— 结果不是空的，而是「用
    昨天的数据打上今天的日期」，看起来完全正常。这种错误没有任何外部症状，
    只会在事后复盘时发现「那天的信号怎么是用前一天的价算的」。

    ⚠️ 2026-09-24 补：原来只要求「那天有一行」。实测那天 274 只（9%）没采到，
    这个校验照样通过，扫描仍然出了 44 条拿旧 K 线的命中。现在要求覆盖率
    ≥ `_MIN_DAY_COVERAGE`：低于它说明当天没采全，**宁可报错**（当天不出信号），
    也不要出一份掺着旧价的榜单 —— 上面那道「日线停在过去就剔除」只挡得住单只，
    整片缺的时候该让人去看采集，而不是默默筛掉一成票。
    """
    with session_scope() as session:
        latest = session.scalar(select(func.max(StockDaily.trade_date)))
        pool = session.scalar(select(func.count()).select_from(StockUniverse)) or 0
        # 只数**能用**的行（`pct_chg` 非空），与 `_load_bars` 同口径：
        # iFinD 的停牌残行（只有收盘价）会在库里堆着，但它们算不了一根 K 线
        count = (
            session.scalar(
                select(func.count())
                .select_from(StockDaily)
                .where(
                    StockDaily.trade_date == trade_date,
                    StockDaily.pct_chg.is_not(None),
                )
            )
            or 0
        )
    if latest is None:
        raise IfindError("stock_daily 是空的，先跑 collect_kline")
    if latest < trade_date or count == 0:
        raise IfindError(f"{trade_date} 的日线还没采到（库里最新是 {latest}），先跑 collect_kline")
    if pool and count < pool * _MIN_DAY_COVERAGE:
        raise IfindError(
            f"{trade_date} 的日线只采到 {count}/{pool} 只（{count / pool:.0%}），"
            f"低于 {_MIN_DAY_COVERAGE:.0%} —— 当天没采全，先补 collect_kline 再扫"
        )


def _latest_trade_date() -> date:
    with session_scope() as session:
        found = session.scalar(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= date.today())
            .order_by(TradeCalendar.trade_date.desc())
            .limit(1)
        )
    if found is None:
        raise IfindError("交易日历为空，先采集交易日历")
    return found
