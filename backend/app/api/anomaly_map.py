"""个股异动图谱（`GET /api/anomaly-map`）。

复刻 yangban-desk 的「豆包异动图谱」：把最近 N 个交易日里「值得看一眼的异动」
摊成一张图 —— 涨停、涨停炸板、中大阳线各算一类，按**同花顺行业末段**（板块）
分组，同一板块同一天的行互为「同批异动」，并标出该板块在窗口内的**题材启动日**。

## 为什么完全用本站已有数据

原型的数据现取东财 / 前复权日线，本站**不允许页面直连外部源**（硬约束），
而且本站库里已经有等价物：

- 涨停 / 炸板 → `limit_pool`（东财 push2ex 口径，全站唯一的涨跌停池）
- 日线涨跌幅 → `stock_daily`（本地缓存的 iFinD 日线）
- 板块 → `stock_basic.industry`（同花顺三级行业路径）
- 涨停原因 → `limit_reason`（同花顺口径）

所以这个接口**只读库、不花任何配额、不联网**。

## 与原型的三处口径差异（页面上也写明白了，复核时以这里为准）

1. **市值门槛用总市值**：本站 `limit_pool` / `stock_universe` 都只有 `total_mv`，
   没有流通市值，所以门槛是「总市值 ≥ 40 亿」而不是原型的「流通市值 ≥ 40 亿」。
2. **板块用同花顺行业末段**：原型用它自己的东财行业，本站没有那一套，取
   `stock_basic.industry`（如 `电子-半导体-集成电路Ⅲ`）的**最后一段**（`集成电路`），
   并去掉结尾的 `Ⅰ/Ⅱ/Ⅲ` 级别记号（见 `_strip_level`）。
3. **炸板 reason 留空**：`limit_reason` 只覆盖涨停股（同花顺涨停池），炸板池本来
   就没有对应数据 —— 不拿别的东西顶（混口径比空着更糟）。

## 性能

窗口 22 天 × 全市场，**一次查全窗口**再在内存里分组：逐日逐票发查询在这个量级上
会变成几百次往返，沿用 `api/patterns.py` 里「一次查全、内存里挑」的做法。
"""

import logging
import threading
from datetime import date, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.models import LimitPool, LimitReason, StockBasic, StockDaily, StockUniverse
from app.schemas import AnomalyMapOut, IndustryStocksOut
from app.services.patterns import is_st
from app.sources.base import chunked

router = APIRouter(prefix="/api", tags=["复盘"])

logger = logging.getLogger(__name__)

# 窗口内「涨停池 ∪ 中大阳线候选」去重后最多约全市场代码数（5300+），老版本 SQLite
# 单条 SQL 变量上限 999，一次 IN 会 OperationalError（3.32+ 放宽到 32766 才不炸）。
# 按 900 一批拆（给 999 留余量），结果内存合并；窗口（约 22 天）那种小 IN 不用管。
_IN_BATCH = 900

# 展示范围**白名单**：主板（含 002 中小板 / 001 / 003）+ 创业板（300/301/302）
# + 科创板（688/689）。
#
# 2026-10-28 放开 20cm：原先只收主板，于是这张表里一只 300/688 都没有 —— 而题材
# 往往正是被 20cm 带起来的（用户连续两次指出）。白名单仍然是排除法：北交所
# （4/8/920）、B 股（9xx）不在名单里，不必再单独判一遍。
_MAINBOARD_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")
_WIDE_BOARD_PREFIXES = ("300", "301", "302", "688", "689")
_DISPLAY_BOARD_PREFIXES = _MAINBOARD_PREFIXES + _WIDE_BOARD_PREFIXES

# 总市值门槛（元）：40 亿。⚠️ 是**总市值**，原型用的流通市值（见模块说明 1）。
MIN_TOTAL_MV = 40e8

# 中大阳线阈值：当日涨幅 ≥ 5%（`stock_daily.pct_chg` 是百分数口径，5.0 = +5%）。
#
# ⚠️ **20cm 板块（创业板 / 科创板）要 10%**：对它们 5% 只是半根阳线，实测 10-28
# 一天就有 431 只（≥40 亿的 346 只），按 5% 收会把这张表彻底淹掉；10% 相当于
# 「自己涨跌停幅度的一半」，与主板的 5% 是同一强度。
BIG_YANG_PCT = 5.0
BIG_YANG_PCT_WIDE = 10.0

# 类型标签。用常量而不是散落的字面量：前端/统计都按这几个值对齐
TYPE_LIMIT_UP = "涨停"
TYPE_BROKEN = "涨停炸板"
TYPE_BIG_YANG = "中大阳线异动"

# ---- 题材启动日 ----
#
# 判定：同板块当日**涨停 ≥2**，或**涨停 ≥1 且异动 ≥3**（异动＝涨停+炸板+中大阳线）。
#
# 两处口径要注意：
#
# 1. **纳入范围与表格的展示过滤不同**。表格只收主板(含002) + 非ST + 总市值≥40亿，
#    但 20cm 的创业板 / 科创板往往才是题材的发起者 —— 按展示口径统计会把它们整段漏掉
#    （2026-10-28 用户指出）。所以判定时只剔 **ST 与北交所**，不限主板、不限市值。
# 2. **往回看 120 个交易日、并按 10 个交易日断代**。原来只在页面窗口（22 天）里找
#    「最早达标的那天」，于是窗口首日就活跃的板块统统被标成「当天启动」—— 那是窗口的
#    边界，不是题材的起点。往前回看之后还要断代：常年活跃的板块（半导体之类）若只认
#    「历史第一次」，启动日会停在半年前，标签就失去意义了；间隔超过 10 个交易日算换了一轮。
THEME_HISTORY_DAYS = 120
THEME_RUN_GAP = 10

# 北交所代码段：4 / 8 开头，另有 920 新段。判定口径里剔掉，与全站其它口径一致
_BSE_PREFIXES = ("4", "8", "92")

THEME_RULE = (
    "题材启动＝本轮首次集群涨停：同板块当日涨停≥2，或涨停≥1且异动≥3"
    "（含创业板/科创板，剔 ST/北交所；判定不看市值；回看 120 个交易日，间隔 10 个交易日以上算新一轮）"
)

# ⚠️ 同一个 40 亿门槛用的是**两套市值**：涨停 / 炸板来自 `limit_pool`（东财、当日值），
# 中大阳来自 `stock_universe`（iFinD 建池快照、最多 7 天前）—— `stock_daily` 不存市值
# （模型与实际库都确认过），没有「当日值」可换。边界票（40 亿上下波动）可能一类进了
# 另一类没进，如实写进文案、不改查询。
FILTER_TEXT = (
    "主板+创业板+科创板（剔北交所/B股）/ 非ST / 总市值≥40亿（本站口径；原型用流通市值）。"
    "中大阳线：主板涨幅≥5%、20cm(300/688)≥10%。"
    "市值两套来源：涨停/炸板按当日东财值，中大阳按建池日快照（最多7天前），"
    "边界票归类可能略有出入"
)
SOURCE_TEXT = "东财涨停/炸板池(limit_pool) + 本地日线 · 板块=同花顺行业末段"

# 同花顺行业末段结尾的**级别记号**（它自己用来标 Ⅰ/Ⅱ/Ⅲ 级）。展示与分组都要去掉，
# 见 `_strip_level` —— 罗马数字在这张表上没有信息量，还容易让「板块」看起来像脏数据。
_LEVEL_MARKS = "ⅠⅡⅢⅣⅤ"


def _is_display_board(code: str) -> bool:
    """在不在展示范围里：主板 + 创业板 + 科创板（判据见 `_DISPLAY_BOARD_PREFIXES`）。"""
    return code[:3] in _DISPLAY_BOARD_PREFIXES


def _big_yang_pct(code: str) -> float:
    """该票「中大阳线」的涨幅门槛：20cm 板块 10%，其余 5%（理由见 `BIG_YANG_PCT` 上方）。"""
    return BIG_YANG_PCT_WIDE if code[:3] in _WIDE_BOARD_PREFIXES else BIG_YANG_PCT


def _seal_time(value: str | None) -> str:
    """封板时间 `093101` → `09:31`。空 / `--` 留 `--`。

    只取到分钟：原型也是分钟粒度，秒在这一屏里是噪声。
    上游偶尔给 `--`（东财对「竞价一字」等情形会给空），**照实留占位符**、
    不猜成 09:25 之类。
    """
    if not value:
        return "--"
    text = value.strip()
    if len(text) < 4 or not text[:4].isdigit():
        return "--"
    return f"{text[:2]}:{text[2:4]}"


def _board(consecutive: int | None) -> str:
    """连板标签：1 板写「首板」、N 板写「N连板」。空 / 0 留空（炸板池没有这个字段）。"""
    if not consecutive or consecutive <= 0:
        return ""
    return "首板" if consecutive == 1 else f"{consecutive}连板"


def _strip_level(text: str) -> str:
    """去掉同花顺行业名结尾的**级别后缀**（`IT服务Ⅲ` → `IT服务`，`证券Ⅱ` → `证券`）。

    同花顺三级路径的最后一段自带 `Ⅰ/Ⅱ/Ⅲ` 标级别（如 `计算机-计算机应用-IT服务Ⅲ`），
    那个后缀只是它内部的分级记号，摆在这张表上没有任何信息量（2026-10-09 用户要求去掉）。
    ⚠️ 前端**只显示后端给的 `sector`**，自己不做二次处理 —— 早期版本在页面上另有一份
    等价实现，配着并排的「行业」列（那一列就是本函数的输出，等于重复）一起删掉了
    （同一天用户指出「重复了」）。所以这个规则**只有这一处**，改这里就够了。
    """
    stripped = text.rstrip(_LEVEL_MARKS).strip()
    # 全是级别记号（不会真的发生）时退回原文，别给出空字符串
    return stripped or text


def _sector_of(industry: str | None) -> str:
    """同花顺三级行业路径取**最后一段**当板块分组键；拿不到落「其他」。

    最后一段要去掉级别后缀（见 `_strip_level`）。
    """
    if not industry:
        return "其他"
    return _strip_level(industry.split("-")[-1].strip()) or "其他"


def _cap(total_mv: float | None) -> float | None:
    """市值换算成**亿元**、保留 1 位。缺数据留 None（不给 0，0 会被读成「市值归零」）。"""
    if total_mv is None:
        return None
    return round(total_mv / 1e8, 1)


def _theme_scope(code: str, name: str | None) -> bool:
    """题材启动判定纳入哪些票：**只要不是 ST、不是北交所**。

    展示范围（`_is_display_board`）是主板 + 创业板 + 科创板且市值 ≥40 亿；判定比它宽，
    **不看市值** —— 题材往往是从小市值的 20cm 票先起来的，用 40 亿门槛统计会把
    发起者整段漏掉（2026-10-28 用户指出）。
    """
    return not is_st(name) and not str(code or "").startswith(_BSE_PREFIXES)


def _theme_counts(
    db: Session, end: date
) -> tuple[dict[tuple[date, str], list[int]], list[date]]:
    """回看 `THEME_HISTORY_DAYS` 个交易日，按 (交易日, 板块) 数 `[涨停数, 异动数]`。

    异动数 = 涨停 + 炸板 + 中大阳线（当日不在涨停/炸板池里、涨幅 ≥5%），与表格那三类
    的定义一致，只是**不加主板 / 市值门槛**（见 `_theme_scope`）。一只票一天只算一行：
    涨停与炸板互斥、进了池子就不再算中大阳线。

    返回 `(counts, dates)`，`dates` 升序 —— 后面用它算「两次达标隔了几个交易日」。
    """
    dates = sorted(
        db.scalars(
            select(LimitPool.trade_date)
            .distinct()
            .where(LimitPool.pool_type == "up", LimitPool.trade_date <= end)
            .order_by(LimitPool.trade_date.desc())
            .limit(THEME_HISTORY_DAYS)
        )
    )
    if not dates:
        return {}, []

    names: dict[str, str | None] = {}
    sectors: dict[str, str] = {}
    for code, name, industry in db.execute(
        select(StockBasic.code, StockBasic.name, StockBasic.industry)
    ).all():
        names[code] = name
        sectors[code] = _sector_of(industry)

    counts: dict[tuple[date, str], list[int]] = {}
    pooled: dict[date, set[str]] = {}
    # ST 判定用**当日**的名称（池子里带的），不是 `stock_basic` 的当前名 ——
    # 往回看 120 天，一只票可能中途才戴帽/摘帽，拿今天的名字判历史会判错。
    for day, code, pool_type, name in db.execute(
        select(
            LimitPool.trade_date,
            LimitPool.code,
            LimitPool.pool_type,
            LimitPool.name,
        ).where(
            LimitPool.trade_date.in_(dates),
            LimitPool.pool_type.in_(("up", "broken")),
        )
    ).all():
        pooled.setdefault(day, set()).add(code)
        if not _theme_scope(code, name or names.get(code)):
            continue
        cell = counts.setdefault((day, sectors.get(code, "其他")), [0, 0])
        cell[1] += 1
        if pool_type == "up":
            cell[0] += 1

    for day, code, name, pct in db.execute(
        select(
            StockDaily.trade_date, StockDaily.code, StockDaily.name, StockDaily.pct_chg
        ).where(
            StockDaily.trade_date.in_(dates),
            StockDaily.pct_chg >= BIG_YANG_PCT,
        )
    ).all():
        if code in pooled.get(day, ()):
            continue
        if not _theme_scope(code, name or names.get(code)):
            continue
        # 20cm 的票要 10% 才算中大阳线，与表格的展示门槛一致（见 `_big_yang_pct`）
        if pct is None or pct < _big_yang_pct(code):
            continue
        counts.setdefault((day, sectors.get(code, "其他")), [0, 0])[1] += 1
    return counts, dates


def _start_days(
    counts: dict[tuple[date, str], list[int]], dates: list[date]
) -> dict[str, date]:
    """每个板块**本轮**的启动日（首次达标那天）。

    从该板块最近一次达标往回走，只要与上一次达标相隔 ≤ `THEME_RUN_GAP` 个交易日就
    继续往前，直到断开 —— 断开处之后的那一天就是本轮起点。不这么断代的话，常年活跃
    的板块会一直被标成「启动于半年前」。
    """
    if not dates:
        return {}
    pos = {day: index for index, day in enumerate(dates)}
    qualified: dict[str, list[date]] = {}
    for (day, sector), (limit_ups, total) in counts.items():
        if limit_ups >= 2 or (limit_ups >= 1 and total >= 3):
            qualified.setdefault(sector, []).append(day)

    starts: dict[str, date] = {}
    for sector, days in qualified.items():
        days.sort()
        start = days[-1]
        for day in reversed(days[:-1]):
            if pos[start] - pos[day] > THEME_RUN_GAP:
                break
            start = day
        starts[sector] = start
    return starts


# 回看 120 天的两次查询约 0.8 秒，而结果一天只变一次 —— 按「数据截止日」缓存。
# 多进程部署下每个进程各算一次，不影响正确性（纯读、无副作用）。
_START_CACHE_MAX = 4
_START_CACHE: dict[date, dict[str, date]] = {}
_START_LOCK = threading.Lock()


def _cached_start_days(db: Session, end: date) -> dict[str, date]:
    """`_start_days` 的缓存版，键是数据截止日。"""
    with _START_LOCK:
        cached = _START_CACHE.get(end)
    if cached is not None:
        return cached
    counts, dates = _theme_counts(db, end)
    starts = _start_days(counts, dates)
    with _START_LOCK:
        _START_CACHE[end] = starts
        while len(_START_CACHE) > _START_CACHE_MAX:
            _START_CACHE.pop(min(_START_CACHE))
    return starts


def build_anomaly_map(session: Session | None = None, days: int = 22) -> dict:
    """取数主逻辑（**不依赖 FastAPI**，便于单测 / 脚本直接调用）。

    传 `session` 就用它；不传就自己开一个（用完关掉）。返回可直接喂给
    `AnomalyMapOut.model_validate` 的 dict。
    """
    owns_session = session is None
    db = session or SessionLocal()
    try:
        # ---- 窗口：limit_pool 涨停池里最近 `days` 个交易日 ----
        # 用涨停池的交易日而不是 `trade_calendar`：没有异动的日子在这张图上就是空行，
        # 用日历会带出一堆「全空的日子」，图上白占位置。
        window = sorted(
            db.scalars(
                select(LimitPool.trade_date)
                .distinct()
                .where(LimitPool.pool_type == "up")
                .order_by(LimitPool.trade_date.desc())
                .limit(days)
            )
        )
        if not window:
            return _empty()

        # ---- 一次查全窗口：涨停 / 炸板池 ----
        pool_rows = db.execute(
            select(
                LimitPool.trade_date,
                LimitPool.code,
                LimitPool.pool_type,
                LimitPool.name,
                LimitPool.pct_chg,
                LimitPool.total_mv,
                LimitPool.first_seal_time,
                LimitPool.consecutive,
            ).where(
                LimitPool.trade_date.in_(window),
                LimitPool.pool_type.in_(("up", "broken")),
            )
        ).all()

        up_by_day: dict[date, list] = {}
        broken_by_day: dict[date, list] = {}
        # 当日「在涨停 / 炸板池里出现过」的全部代码 —— 中大阳线要按**池子成员**排除，
        # 而不是按「通过过滤的池子成员」：一只 ST 涨停股不该因为它被过滤掉就又能
        # 以「中大阳线」的身份再出现一次。
        pool_codes: dict[date, set[str]] = {}
        for row in pool_rows:
            pool_codes.setdefault(row.trade_date, set()).add(row.code)
            if row.pool_type == "up":
                up_by_day.setdefault(row.trade_date, []).append(row)
            else:
                broken_by_day.setdefault(row.trade_date, []).append(row)
        # 炸板要跳过「当天也在涨停池」的票（同一只票不会既封住又炸板）
        up_codes = {day: {row.code for row in items} for day, items in up_by_day.items()}

        # ---- 一次查全窗口：涨停原因（同花顺，只覆盖涨停股） ----
        reasons = {
            (row.trade_date, row.code): row.reason
            for row in db.execute(
                select(
                    LimitReason.trade_date, LimitReason.code, LimitReason.reason
                ).where(LimitReason.trade_date.in_(window))
            ).all()
        }

        # ---- 一次查全窗口：当日涨幅 ≥ 5% 的日线（中大阳线候选） ----
        daily_rows = db.execute(
            select(
                StockDaily.trade_date,
                StockDaily.code,
                StockDaily.name,
                StockDaily.pct_chg,
            ).where(
                StockDaily.trade_date.in_(window),
                StockDaily.pct_chg >= BIG_YANG_PCT,
            )
        ).all()

        # 板块（同花顺行业）与股票池市值各查一次全量。涉及到的代码就是池子 ∪ 日线候选，
        # `in_` 查回来即可 —— 逐票查市值是这份接口最容易写慢的地方。集合可能到 5300+，
        # 按 `_IN_BATCH` 分批（老 SQLite 单条变量上限 999），结果在内存里合并。
        codes = list({row.code for row in pool_rows} | {row.code for row in daily_rows})
        basics: dict[str, tuple[str | None, str | None]] = {}
        mv_map: dict[str, float | None] = {}
        for batch in chunked(codes, _IN_BATCH):
            basics.update(
                {
                    row.code: (row.name, row.industry)
                    for row in db.execute(
                        select(
                            StockBasic.code, StockBasic.name, StockBasic.industry
                        ).where(StockBasic.code.in_(batch))
                    ).all()
                }
            )
            mv_map.update(
                dict(
                    db.execute(
                        select(
                            StockUniverse.code, StockUniverse.total_mv
                        ).where(StockUniverse.code.in_(batch))
                    ).all()
                )
            )

        rows: list[dict] = []

        def name_of(code: str, name: str | None) -> str:
            """名称：池子 / 日线自带的优先，取不到退回建池表，再取不到退回代码。"""
            return name or basics.get(code, (None, None))[0] or code

        # 1) 涨停：过滤 主板+创业板+科创板 / 非ST / 总市值≥40亿
        #    reason 取同花顺涨停原因（可能没有 → None）
        for day in window:
            for row in up_by_day.get(day, []):
                if not _is_display_board(row.code) or is_st(row.name):
                    continue
                if row.total_mv is None or row.total_mv < MIN_TOTAL_MV:
                    continue
                rows.append(
                    _make_row(
                        day=day,
                        code=row.code,
                        name=name_of(row.code, row.name),
                        sector_industry=basics.get(row.code, (None, None))[1],
                        type=TYPE_LIMIT_UP,
                        time=_seal_time(row.first_seal_time),
                        board=_board(row.consecutive),
                        pct=row.pct_chg,
                        cap=_cap(row.total_mv),
                        reason=reasons.get((day, row.code)),
                    )
                )

        # 2) 涨停炸板：同过滤，跳过当天也在涨停池的票；board / reason 没有数据就留空
        for day in window:
            seen_up = up_codes.get(day, set())
            for row in broken_by_day.get(day, []):
                if row.code in seen_up:
                    continue
                if not _is_display_board(row.code) or is_st(row.name):
                    continue
                if row.total_mv is None or row.total_mv < MIN_TOTAL_MV:
                    continue
                rows.append(
                    _make_row(
                        day=day,
                        code=row.code,
                        name=name_of(row.code, row.name),
                        sector_industry=basics.get(row.code, (None, None))[1],
                        type=TYPE_BROKEN,
                        time=_seal_time(row.first_seal_time),
                        board="",
                        pct=row.pct_chg,
                        cap=_cap(row.total_mv),
                        reason=None,
                    )
                )

        # 3) 中大阳线异动：当日**未在涨停/炸板池**、涨幅过门槛的票。
        #    门槛按板块：主板 5%，20cm（300/688）10%（见 `BIG_YANG_PCT` 上方）。
        #    市值取 `stock_universe`（日线表不存市值）—— 与上面两类的 `limit_pool`
        #    当日值是两套口径，边界票归类可能略有出入（已在 FILTER_TEXT 注明）。
        for row in daily_rows:
            day = row.trade_date
            if row.code in pool_codes.get(day, set()):
                continue
            if not _is_display_board(row.code) or is_st(row.name):
                continue
            # SQL 那边按全市场 5% 兜底捞，20cm 的 10% 门槛在这里补一刀
            if row.pct_chg is None or row.pct_chg < _big_yang_pct(row.code):
                continue
            total_mv = mv_map.get(row.code)
            if total_mv is None or total_mv < MIN_TOTAL_MV:
                continue
            rows.append(
                _make_row(
                    day=day,
                    code=row.code,
                    name=name_of(row.code, row.name),
                    sector_industry=basics.get(row.code, (None, None))[1],
                    type=TYPE_BIG_YANG,
                    time="收盘",
                    board="",
                    pct=row.pct_chg,
                    cap=_cap(total_mv),
                    reason=None,
                )
            )

        # ---- 排序：日期降序（最新在最前），其次 板块 / 时间 / 代码（与原型一致） ----
        # 两趟排序：先按次要键升序排、再按主键降序稳定排 —— Python 的排序是稳定的，
        # 第一趟的顺序会被第二趟保留下来。
        rows.sort(key=lambda r: (r["sector"], r["time"], r["code"]))
        rows.sort(key=lambda r: r["date"], reverse=True)

        # ---- 同批异动：同一天、同一板块的所有行互为同批，标签按 (时间, 代码) 升序 ----
        groups: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            groups.setdefault((row["date"], row["sector"]), []).append(row)
        for items in groups.values():
            items.sort(key=lambda r: (r["time"], r["code"]))
            labels = [f"{r['name']}({r['type']}@{r['time']})" for r in items]
            # 自己也在列表里（与原型一致）—— 一屏里能看到「同批还有谁」比只给邻居更直接
            for row in items:
                row["cohort"] = labels

        # ---- 题材启动日：本轮首次集群涨停 ----
        # 判据二选一：当日该板块涨停 ≥2 只；或 涨停 ≥1 只且该板块当日异动 ≥3 条。
        # 统计口径与回看深度见 `THEME_RULE` 上方那段（含创业板/科创板、回看 120 天、按 10 天断代）。
        window_start = window[0]
        start_days = _cached_start_days(db, window[-1])

        # 每个板块在**窗口内**第一次出现的那天。启动日早于窗口时只在这一行提示，
        # 否则同一板块的几十行都挂一个「启动于 XX」，反而是噪声。
        first_seen: dict[str, str] = {}
        for row in rows:
            current = first_seen.get(row["sector"])
            if current is None or row["date"] < current:
                first_seen[row["sector"]] = row["date"]

        for row in rows:
            sector = row["sector"]
            start = start_days.get(sector)
            row["theme_start"] = start is not None and start.isoformat() == row["date"]
            row["early_start"] = (
                start.isoformat()
                if start is not None
                and start < window_start
                and first_seen.get(sector) == row["date"]
                else None
            )

        # 板块下拉的选项：按命中条数降序（最活跃的排前面），同数按名字稳定排
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["sector"]] = counts.get(row["sector"], 0) + 1
        sectors = sorted(counts, key=lambda name: (-counts[name], name))

        return {
            "start": str(window[0]),
            "end": str(window[-1]),
            "days": len(window),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "filter": FILTER_TEXT,
            "source": SOURCE_TEXT,
            "theme_rule": THEME_RULE,
            "sectors": sectors,
            # 「题材启动板块日」的个数 = 窗口内确实亮了绿标的板块数（与屏幕上的标签对得上）
            "theme_start_days": len({row["sector"] for row in rows if row["theme_start"]}),
            "rows": rows,
        }
    finally:
        if owns_session:
            db.close()


def _make_row(
    *,
    day: date,
    code: str,
    name: str,
    sector_industry: str | None,
    type: str,
    time: str,
    board: str,
    pct: float | None,
    cap: float | None,
    reason: str | None,
) -> dict:
    """组一行。`cohort` / `theme_start` / `early_start` 先占位，分组之后再回填。"""
    return {
        "sector": _sector_of(sector_industry),
        "date": str(day),
        "time": time,
        "code": code,
        "name": name,
        "type": type,
        "board": board,
        "pct": pct,
        "cap": cap,
        # 完整的三级路径原样返回（页面 tooltip 用），sector 只是它的末段
        "industry": sector_industry,
        "reason": reason,
        "cohort": [],
        "theme_start": False,
        "early_start": None,
    }


def _empty() -> dict:
    """窗口内一条涨停都没有时返回空壳（页面照常渲染、显示「暂无数据」）。"""
    return {
        "start": "",
        "end": "",
        "days": 0,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "filter": FILTER_TEXT,
        "source": SOURCE_TEXT,
        "theme_rule": THEME_RULE,
        "sectors": [],
        "theme_start_days": 0,
        "rows": [],
    }


@router.get("/anomaly-map", response_model=AnomalyMapOut)
def anomaly_map(
    days: int = Query(22, ge=5, le=60, description="回看多少个交易日"),
    session: Session = Depends(get_db),
) -> AnomalyMapOut:
    """个股异动图谱。口径与取数细节见模块说明与 `build_anomaly_map`。"""
    return AnomalyMapOut.model_validate(build_anomaly_map(session, days))


def build_industry_stocks(
    session: Session, sector: str, day: date | None = None
) -> dict:
    """某个板块（同花顺行业末段名）在某交易日的全部个股。

    取数逻辑（**不依赖 FastAPI**，便于单测直接调）：

    1. `stock_basic.industry` 是三级路径（`电子-半导体-集成电路Ⅲ`），先把每只票用
       `_sector_of` 压成末段名，再与 `sector` 比 —— 与异动图谱分组**同一个函数**，
       两处口径不会走岔（级别记号 Ⅰ/Ⅱ/Ⅲ 也是在这里一起剥掉的）。
    2. 一次性把 `stock_basic` 全表读进内存（约 5300 行），在 Python 里挑。写成
       SQL 的 `LIKE '%-名字'` 也可以，但要额外枚举 5 个级别记号的后缀，容易漏。
    3. 行情只查**一天**（`stock_daily`），市值取 `stock_universe` 的快照 —— 与图谱
       那张表同源。停牌票当天没有日线，就不出现在列表里（不拿旧价冒充当天）。

    `day` 不给就用库里最新的交易日。
    """
    if day is None:
        day = session.scalar(select(func.max(StockDaily.trade_date)))

    basics = [
        (code, name)
        for code, name, industry in session.execute(
            select(StockBasic.code, StockBasic.name, StockBasic.industry)
        ).all()
        if _sector_of(industry) == sector
    ]
    if not basics or day is None:
        return {"sector": sector, "trade_date": None, "count": 0, "items": []}

    names = dict(basics)
    quotes: dict[str, tuple] = {}
    caps: dict[str, float | None] = {}
    for batch in chunked(sorted(names), _IN_BATCH):
        for code, close, pct_chg, amount in session.execute(
            select(
                StockDaily.code,
                StockDaily.close,
                StockDaily.pct_chg,
                StockDaily.amount,
            ).where(StockDaily.trade_date == day, StockDaily.code.in_(batch))
        ).all():
            quotes[code] = (close, pct_chg, amount)
        caps.update(
            dict(
                session.execute(
                    select(StockUniverse.code, StockUniverse.total_mv).where(
                        StockUniverse.code.in_(batch)
                    )
                ).all()
            )
        )

    items = [
        {
            "code": code,
            "name": names[code],
            "close": close,
            "pct_chg": pct_chg,
            "amount": amount,
            "cap": _cap(caps.get(code)),
        }
        for code, (close, pct_chg, amount) in quotes.items()
    ]
    # 涨跌幅降序；没有涨跌幅的排最后（`-inf` 而不是 0，否则会被当成「平盘」混在中间）
    items.sort(
        key=lambda item: (
            item["pct_chg"] if item["pct_chg"] is not None else float("-inf")
        ),
        reverse=True,
    )
    return {
        "sector": sector,
        "trade_date": day,
        "count": len(items),
        "items": items,
    }


@router.get("/anomaly-map/industry", response_model=IndustryStocksOut)
def industry_stocks(
    sector: str = Query(
        ...,
        min_length=1,
        max_length=32,
        description="板块名＝同花顺行业路径末段（已去 Ⅰ/Ⅱ/Ⅲ 级别记号），如「其他通用设备」",
    ),
    day: date | None = Query(None, alias="date", description="交易日，默认库里最新一天"),
    session: Session = Depends(get_db),
) -> IndustryStocksOut:
    """点「板块」后看到的该板块全部个股（异动页的下钻）。"""
    return IndustryStocksOut.model_validate(
        build_industry_stocks(session, sector, day)
    )
