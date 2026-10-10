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
from datetime import date, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.models import LimitPool, LimitReason, StockBasic, StockDaily, StockUniverse
from app.schemas import AnomalyMapOut
from app.services.patterns import is_st

router = APIRouter(prefix="/api", tags=["复盘"])

logger = logging.getLogger(__name__)

# 主板前缀**白名单**（含 002 中小板 / 001 / 003 深市主板）。
# 白名单本身就是排除：300/301（创业板）、688/689（科创板）、8/4（北交所）、
# 9（B 股）都不在名单里 —— 不必再单独判一遍，单独判反而容易漏一个前缀。
_MAINBOARD_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")

# 总市值门槛（元）：40 亿。⚠️ 是**总市值**，原型用的流通市值（见模块说明 1）。
MIN_TOTAL_MV = 40e8

# 中大阳线阈值：当日涨幅 ≥ 5%（`stock_daily.pct_chg` 是百分数口径，5.0 = +5%）
BIG_YANG_PCT = 5.0

# 类型标签。用常量而不是散落的字面量：前端/统计都按这几个值对齐
TYPE_LIMIT_UP = "涨停"
TYPE_BROKEN = "涨停炸板"
TYPE_BIG_YANG = "中大阳线异动"

FILTER_TEXT = "主板(含002) / 非ST / 总市值≥40亿（本站口径；原型用流通市值）"
SOURCE_TEXT = "东财涨停/炸板池(limit_pool) + 本地日线 · 板块=同花顺行业末段"

# 同花顺行业末段结尾的**级别记号**（它自己用来标 Ⅰ/Ⅱ/Ⅲ 级）。展示与分组都要去掉，
# 见 `_strip_level` —— 罗马数字在这张表上没有信息量，还容易让「板块」看起来像脏数据。
_LEVEL_MARKS = "ⅠⅡⅢⅣⅤ"


def _is_mainboard(code: str) -> bool:
    """是不是主板（含 002 中小板）。判据见 `_MAINBOARD_PREFIXES` 的说明。"""
    return code[:3] in _MAINBOARD_PREFIXES


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
        # `in_` 一次查回来即可 —— 逐票查市值是这份接口最容易写慢的地方。
        codes = {row.code for row in pool_rows} | {row.code for row in daily_rows}
        basics = {
            row.code: (row.name, row.industry)
            for row in db.execute(
                select(StockBasic.code, StockBasic.name, StockBasic.industry).where(
                    StockBasic.code.in_(codes)
                )
            ).all()
        }
        mv_map = dict(
            db.execute(
                select(StockUniverse.code, StockUniverse.total_mv).where(
                    StockUniverse.code.in_(codes)
                )
            ).all()
        )

        rows: list[dict] = []

        def name_of(code: str, name: str | None) -> str:
            """名称：池子 / 日线自带的优先，取不到退回建池表，再取不到退回代码。"""
            return name or basics.get(code, (None, None))[0] or code

        # 1) 涨停：过滤 主板(含002) / 非ST / 总市值≥40亿
        #    reason 取同花顺涨停原因（可能没有 → None）
        for day in window:
            for row in up_by_day.get(day, []):
                if not _is_mainboard(row.code) or is_st(row.name):
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
                if not _is_mainboard(row.code) or is_st(row.name):
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

        # 3) 中大阳线异动：当日**未在涨停/炸板池**、日线涨幅 ≥5% 的票。
        #    市值取 `stock_universe`（日线表不存市值）。
        for row in daily_rows:
            day = row.trade_date
            if row.code in pool_codes.get(day, set()):
                continue
            if not _is_mainboard(row.code) or is_st(row.name):
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

        # ---- 题材启动日：窗口内该板块**最早**满足条件的那一天 ----
        # 判据二选一：当日该板块涨停 ≥2 只；或 涨停 ≥1 只且该板块当日异动 ≥3 条。
        # 按 (交易日, 板块) 升序遍历，每个板块只在**第一次**满足时落标记。
        stats: dict[tuple[str, str], list[int]] = {}
        for row in rows:
            cell = stats.setdefault((row["date"], row["sector"]), [0, 0])
            cell[1] += 1
            if row["type"] == TYPE_LIMIT_UP:
                cell[0] += 1
        first_key: dict[str, tuple[str, str]] = {}
        for key in sorted(stats):  # (日期, 板块) 元组升序 → 日期早的在前
            limit_count, total = stats[key]
            if limit_count >= 2 or (limit_count >= 1 and total >= 3):
                first_key.setdefault(key[1], key)
        for row in rows:
            row["theme_start"] = (
                first_key.get(row["sector"]) == (row["date"], row["sector"])
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
            "sectors": sectors,
            # 「题材启动板块日」的个数 = 被标记的 (交易日, 板块) 对数
            "theme_start_days": len(first_key),
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
    """组一行。`cohort` / `theme_start` 先占位，分组之后再回填。"""
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
