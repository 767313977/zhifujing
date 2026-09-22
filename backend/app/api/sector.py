"""板块题材接口（开盘红口径）。

数据以本地 `sector_daily` 为主，页面不直连数据源 —— 板块行情的采集要翻几十页，
不可能放在请求里做。**唯一的例外是成分股**：270 个板块全量抓没有必要，
而一天实际只会看几个板块，所以改成「首次请求抓取并落库、之后读库」。

⚠️ 与旧版（同花顺口径）的差别不止是换了数据源：

- 净流入 / 涨跌家数 / 领涨股 / 成分股数在切换后**一律是空值**，页面显示 `—`。
  开盘红的板块行里这些列要么换口径就不是同一个含义、要么没有验证手段，见
  `sources/kaipanhong.py` 顶部。
- 成分股**当日要等盘后更新**（实测同一天 21:20 取不到、21:55 有），早看就没有。
- 开盘红行业代码与同花顺行业同源（都是 881xxx），切换时旧行必须清掉，
  见 `models.SectorDaily` 的注释。
"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.api.deps import resolve_trade_date
from app.db import get_db, session_scope
from app.jobs.collect_sectors import SectorCollector
from app.models import (
    LimitPool,
    SectorBasic,
    SectorDaily,
    SectorMember,
    StockConcept,
)
from app.schemas import (
    RotationCell,
    RotationColumn,
    RotationLeader,
    RotationLeaderDay,
    SectorCompare,
    SectorCompareSeries,
    SectorHeat,
    SectorHeatGroup,
    SectorHeatItem,
    SectorMemberItem,
    SectorMembers,
    SectorQuote,
    SectorRanking,
    SectorRotation,
    SectorSeries,
)
from app.sources.kaipanhong import TAXONOMY_INDUSTRY, TAXONOMY_SELECTED

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sectors", tags=["板块"])

# 展示顺序：精选在前（短线复盘的主视角），行业在后
TAXONOMIES = (TAXONOMY_SELECTED, TAXONOMY_INDUSTRY)
# 近 N 日涨幅用的窗口长度（交易日）
MULTI_DAY_SPAN = 5
# 首页板块热力各展示几个最强 / 最弱板块
HEAT_LEADERS = 10
HEAT_LAGGARDS = 6
# 对比图最多同时叠加几条线，太多颜色分不开
COMPARE_LIMIT = 8
# 对比图的归一化基准
COMPARE_BASE = 100.0

# 按**成交额或涨幅**排时要先剔掉的「业绩 / 身份 / 地域类」板块。
#
# 它们不是产业题材，而是「按财务或身份筛出来的集合」：业绩增长 / 中报增长几乎等于
# 全市场，地域板块是「按注册地筛」，国有企业是「按股东性质筛」—— 成交额自然永远
# 最大。实测 2026-09-18 成交额前 10 名里有 3 个是这类（中报增长第 1、业绩增长第 2、
# 国有企业第 10），不剔的话成交额榜永远被它们占着，一点题材轮动都看不出来。
#
# 按关键词而不是列死名单：「中报增长」每季度换个叫法。
#
# **成交额榜与涨幅榜都剔，只有强度榜不剔** —— 强度榜要跟开盘啦 App 对齐，它给什么
# 就显示什么（它自己那张榜里 09-02 那列第 2 名就是「中报增长」）。
#
# ⚠️ 这段注释原先写的是「**只对成交额生效**：涨跌幅可以横向比，没有哪类板块会结构性
# 占优」。「涨跌幅可以横向比」这个判断是对的，但我拿它去解释另一个现象时用错了 ——
# 我曾据此断言「涨幅榜的榜首常是筛出来的集合」（依据是「领涨」行 20 列里只有 1 列有
# 内容）。**实测 2026-09-21 的 20 列，只有 1 列的榜首命中关键词**（09-02 的「北交所」）。
# 涨幅榜的榜首多是「霍乱概念 / 压缩机 / 转基因 / VPN / 银行」这类真题材，它们没有
# 领涨股是因为**当天没有涨停股**，与伪板块无关。
#
# 地域里只滤带「省 / 自治区 / 自贸区」的，直辖市与「深圳 / 武汉」这类没滤 ——
# 它们排不进当天前 10（实测最高的地域板块「广东省」排第 15），而矩阵每天只看前 10。
#
# 保留并购重组 / 股权转让 / 举牌：它们同样是「筛出来的集合」，但在 A 股是真会炒的
# 题材，与业绩、地域不是一回事。
ROTATION_EXCLUDE = (
    "增长",  # 中报增长 / 三季报增长 / 业绩增长
    "预增",  # 年报预增
    "预盈",
    "扭亏",
    "超跌",
    "破净",
    "高股息",
    "低价股",
    "送转",
    "专精特新",
    "国有企业",
    "中字头",
    "中特估",
    "科创板",
    "北交所",
    "次新",
    "ST",
    "省",
    "自治区",
    "自贸区",
)

# 轮动矩阵可选的排序指标。
#
# `strength` 是开盘啦的**强度值**，也是它 App 里那张板块榜的排序依据 —— 想跟开盘啦
# 对齐就得用它；按成交额排得到的是完全不同的一张榜（芯片永远第一，因为成交额最大）。
# `pct_chg` 叫「涨幅」而不是「强度」，就是为了不和 `strength` 混。
ROTATION_METRICS = {"strength": "强度", "pct_chg": "涨幅", "amount": "成交额"}
# 矩阵「领涨」行每列最多列几只
ROTATION_LEADERS = 5
# 本地板块日线能回溯多少天。轮动矩阵的列数上限就取它（后端也会按实际数据截短）
ROTATION_MAX_DAYS = 60
# 走势图 / 对比图能取多长。跟板块历史一样长（2026-09-21 深回补之后是 252 天）——
# 图上的长历史是有意义的，跟矩阵不一样（矩阵列数太多就看不动了，所以只到 60）
CURVE_MAX_DAYS = 250


def _compound_returns(
    session: Session, taxonomy: str, trade_date: date, span: int
) -> dict[str, float]:
    """近 N 个交易日的区间涨跌幅。

    库里只有逐日涨跌幅、没有板块收盘价，所以把逐日涨跌幅复利回区间涨幅。
    只在 N 天**都有**数据时才给结果：缺一天就复利不出真实区间涨幅，
    宁可不显示，也不能给个偏小的数被读成「涨得少」。
    """
    days = list(
        session.scalars(
            select(SectorDaily.trade_date)
            .distinct()
            .where(SectorDaily.taxonomy == taxonomy, SectorDaily.trade_date <= trade_date)
            .order_by(SectorDaily.trade_date.desc())
            .limit(span)
        )
    )
    if len(days) < span:
        return {}

    rows = session.execute(
        select(SectorDaily.sector_code, SectorDaily.pct_chg)
        .where(
            SectorDaily.taxonomy == taxonomy,
            SectorDaily.trade_date >= min(days),
            SectorDaily.trade_date <= trade_date,
        )
        .order_by(SectorDaily.trade_date)
    ).all()

    grouped: dict[str, list[float | None]] = {}
    for code, pct in rows:
        grouped.setdefault(code, []).append(pct)

    result: dict[str, float] = {}
    for code, values in grouped.items():
        window = values[-span:]
        if len(window) < span or any(value is None for value in window):
            continue
        factor = 1.0
        for value in window:
            factor *= 1 + value / 100
        result[code] = (factor - 1) * 100
    return result


def _limit_up_by_board(session: Session, trade_date: date) -> dict[str, int]:
    """各精选板块当日的涨停家数。

    走「个股 → 板块」的归属聚合（`stock_concept`，来自开盘红涨停天梯），
    而不是按名字去匹配涨停池的行业字段（那是申万口径、还截断成 4 字，
    实测只有一半能对上，会把「没对上」读成 0）。
    """
    codes = list(
        session.scalars(
            select(LimitPool.code).where(
                LimitPool.trade_date == trade_date, LimitPool.pool_type == "up"
            )
        )
    )
    if not codes:
        return {}
    rows = session.execute(
        select(StockConcept.concept, func.count())
        .where(StockConcept.trade_date == trade_date, StockConcept.code.in_(codes))
        .group_by(StockConcept.concept)
    ).all()
    return dict(rows)


def _limit_up_state(session: Session, trade_date: date) -> dict[str, int]:
    """当日涨停股 → 连板数。成分股列表里标注涨停用。"""
    return {
        code: (consecutive or 1)
        for code, consecutive in session.execute(
            select(LimitPool.code, LimitPool.consecutive).where(
                LimitPool.trade_date == trade_date, LimitPool.pool_type == "up"
            )
        ).all()
    }


@router.get("/ranking", response_model=SectorRanking)
def ranking(
    taxonomy: str = Query(
        TAXONOMY_SELECTED,
        description="kph_selected=精选板块 kph_industry=行业板块",
    ),
    trade_date: date = Depends(resolve_trade_date),
    session: Session = Depends(get_db),
) -> SectorRanking:
    """某交易日的板块排行。

    一次返回全部板块（精选 270 / 行业 104），不排序不分页 ——
    前端要按涨跌幅、成交额反复切换排序，交给前端更省事。
    """
    if taxonomy not in TAXONOMIES:
        raise HTTPException(
            status_code=400, detail=f"taxonomy 只能是 {list(TAXONOMIES)}，收到 {taxonomy}"
        )

    rows = list(
        session.scalars(
            select(SectorDaily)
            .where(SectorDaily.trade_date == trade_date, SectorDaily.taxonomy == taxonomy)
            .order_by(SectorDaily.pct_chg.desc().nullslast())
        )
    )

    multi_day = _compound_returns(session, taxonomy, trade_date, MULTI_DAY_SPAN)
    # 涨停家数只有精选口径有（个股 → 板块的归属来自涨停天梯，只覆盖精选板块），
    # 行业留空。注意精选这一侧即使一个涨停都没有也要给 0，不能给 null
    by_board = (
        _limit_up_by_board(session, trade_date)
        if taxonomy == TAXONOMY_SELECTED
        else None
    )

    boards = [
        SectorQuote(
            code=row.sector_code,
            name=row.name or row.sector_code,
            taxonomy=row.taxonomy or taxonomy,
            strength=row.strength,
            pct_chg=row.pct_chg,
            pct_chg_5d=multi_day.get(row.sector_code),
            amount=row.amount,
            net_inflow=row.net_inflow,
            up_count=row.up_count,
            down_count=row.down_count,
            member_count=row.member_count,
            leader_name=row.leader_name,
            leader_pct_chg=row.leader_pct_chg,
            limit_up_count=(
                by_board.get(row.name or "", 0) if by_board is not None else None
            ),
        )
        for row in rows
    ]
    return SectorRanking(
        trade_date=trade_date,
        taxonomy=taxonomy,
        total=len(boards),
        missing=sum(1 for board in boards if board.pct_chg is None),
        # 换开盘红之后没有「估算口径」了：板块行就是数据源的原值，
        # 不存在当日兜底、次日订正那套。字段保留是为了不动前端类型。
        estimated=0,
        boards=boards,
    )


@router.get("/series", response_model=SectorSeries)
def series(
    code: str = Query(..., description="板块代码"),
    days: int = Query(30, ge=5, le=CURVE_MAX_DAYS, description="返回最近 N 个交易日，按日期升序"),
    trade_date: date = Depends(resolve_trade_date),
    session: Session = Depends(get_db),
) -> SectorSeries:
    """单个板块的日线序列，供板块详情走势图使用。

    `trade_date` 是窗口**截止日**，且与排行走同一个 `resolve_trade_date`：
    两者口径必须一致，否则页面选了历史日期时会出现「左边的表是某天的排行、
    右边的走势图却一路画到今天」这种前后矛盾。
    """
    rows = list(
        session.scalars(
            select(SectorDaily)
            .where(
                SectorDaily.sector_code == code,
                SectorDaily.trade_date <= trade_date,
            )
            .order_by(SectorDaily.trade_date.desc())
            .limit(days)
        )
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"板块 {code} 暂无数据")
    rows.reverse()

    latest = rows[-1]
    return SectorSeries(
        code=code,
        name=latest.name or code,
        taxonomy=latest.taxonomy or "",
        dates=[row.trade_date for row in rows],
        pct_chg=[row.pct_chg for row in rows],
        amount=[row.amount for row in rows],
    )


@router.get("/heat", response_model=SectorHeat)
def heat(
    trade_date: date = Depends(resolve_trade_date),
    session: Session = Depends(get_db),
) -> SectorHeat:
    """首页「板块热力」：精选与行业各取最热/最冷几个板块。

    首页只需要一眼看强弱，所以不返回全部板块，也不带近 5 日那些字段。
    """
    return SectorHeat(
        trade_date=trade_date,
        selected=_heat_group(session, TAXONOMY_SELECTED, trade_date),
        industry=_heat_group(session, TAXONOMY_INDUSTRY, trade_date),
    )


def _heat_group(session: Session, taxonomy: str, trade_date: date) -> SectorHeatGroup:
    rows = list(
        session.scalars(
            select(SectorDaily)
            .where(SectorDaily.trade_date == trade_date, SectorDaily.taxonomy == taxonomy)
            .order_by(SectorDaily.pct_chg.desc().nullslast())
        )
    )
    by_board = (
        _limit_up_by_board(session, trade_date)
        if taxonomy == TAXONOMY_SELECTED
        else None
    )

    def _item(row: SectorDaily) -> SectorHeatItem:
        return SectorHeatItem(
            code=row.sector_code,
            name=row.name or row.sector_code,
            pct_chg=row.pct_chg,
            amount=row.amount,
            limit_up_count=(
                by_board.get(row.name or "", 0) if by_board is not None else None
            ),
        )

    # rows 已按涨跌幅降序且空值沉底，取头部当领涨、去掉空值后取尾部当领跌
    items = [_item(row) for row in rows]
    valid = [item for item in items if item.pct_chg is not None]
    values = [item.pct_chg for item in valid]
    return SectorHeatGroup(
        taxonomy=taxonomy,
        total=len(items),
        rising=sum(1 for value in values if value > 0),
        falling=sum(1 for value in values if value < 0),
        average=sum(values) / len(values) if values else None,
        leaders=valid[:HEAT_LEADERS],
        laggards=list(reversed(valid[-HEAT_LAGGARDS:])),
    )


def _rotation_leaders(
    session: Session, targets: dict[date, str]
) -> dict[date, list[RotationLeader]]:
    """`{交易日: 板块名}` → 该板块当日的涨停股，按「连板数 → 封板时间」取最强的前几只。

    数据全部来自本地库、**零额外请求**：`stock_concept` 给「个股 → 板块」的归属
    （来自开盘红涨停天梯），`limit_pool` 补名称、连板数与首封时间。

    两者用**外连接**：两边的涨停家数口径略有差异（天梯 77 家 vs 涨停池 78 家），
    内连接会悄悄丢掉只在一边出现的票，而这里丢一只就少一个名字、还看不出来。

    ⚠️ 板块**当天不在榜上也没关系** —— 传进来的是「名字」，不要求它在那天的前 10 名里。
    这正是「领涨行跟着选中板块走」需要的行为：选中「医药」之后，就算它 8 月某天没进榜，
    那天也照样给出医药的涨停股（确实没有才是空列表）。
    """
    if not targets:
        return {}
    wanted = {(day, board) for day, board in targets.items()}
    rows = session.execute(
        select(
            StockConcept.trade_date,
            StockConcept.concept,
            StockConcept.code,
            LimitPool.name,
            LimitPool.consecutive,
            LimitPool.first_seal_time,
        )
        .select_from(StockConcept)
        .outerjoin(
            LimitPool,
            and_(
                LimitPool.trade_date == StockConcept.trade_date,
                LimitPool.code == StockConcept.code,
                LimitPool.pool_type == "up",
            ),
        )
        .where(StockConcept.trade_date.in_(list(targets)))
    ).all()

    # 先按「连板数 → 封板时间 → 代码」排好序再取前几只。
    #
    # 排序键的语义：**连板多的在前；同为 N 板时封板早的在前**（10:02 封住比 11:09 强，
    # 这是短线的常识）；还剩并列才用代码兜底，只为让顺序稳定（否则每次请求名字会换位）。
    # ⚠️ 一开始只用了「连板数 → 代码」，那是**稳定但没意义**的顺序 —— 代码小的排龙二，
    # 与强弱无关。核对数据时才发现的。
    collected: dict[date, list[tuple[tuple, RotationLeader]]] = {}
    for day, concept, code, name, consecutive, first_seal in rows:
        if (day, concept) not in wanted:
            continue
        order = (-(consecutive or 1), first_seal or "999999", code)
        collected.setdefault(day, []).append(
            (order, RotationLeader(code=code, name=name, consecutive=consecutive))
        )

    result: dict[date, list[RotationLeader]] = {}
    for day, items in collected.items():
        items.sort(key=lambda pair: pair[0])
        result[day] = [leader for _, leader in items[:ROTATION_LEADERS]]
    return result


@router.get("/rotation", response_model=SectorRotation)
def rotation(
    taxonomy: str = Query(
        TAXONOMY_SELECTED,
        description="kph_selected=精选板块 kph_industry=行业板块",
    ),
    days: int = Query(20, ge=5, le=ROTATION_MAX_DAYS, description="列出最近 N 个交易日"),
    top: int = Query(10, ge=3, le=30, description="每一天取前 N 名"),
    metric: str = Query(
        "strength",
        description="strength=强度（开盘啦口径） pct_chg=涨幅 amount=成交额",
    ),
    trade_date: date = Depends(resolve_trade_date),
    session: Session = Depends(get_db),
) -> SectorRotation:
    """板块轮动矩阵：每列一个交易日、每行是当天的第 N 名。

    一眼看出「谁今天进榜、前几天在哪儿」。要点是**每一列独立按指标排序**，
    而不是拿某一天的名单去对齐别的天 —— 后者行与行之间有意义，但看不出轮动；
    前者同一行每天都是不同的板块，那正是「轮动」本身。

    列从新到旧（最新一列在最左），跟着盯盘的习惯：先看今天，再往左回溯。

    默认按**强度**排 —— 那才是开盘啦 App 里那张板块榜的口径（按成交额排会得到完全
    不同的一张榜：芯片永远第一）。注意强度的**量纲不可跨口径比**：精选极值上万、
    行业一千出头，所以切口径时榜会整体换一套（这是对的，两个口径本就不可比）。

    `ROTATION_EXCLUDE` 那张黑名单**对成交额与涨幅都生效，只有强度榜不剔** ——
    强度榜要与开盘啦 App 对齐，它给什么就显示什么（它自己的榜里 09-02 那列第 2 名
    就是「中报增长」）。实测黑名单对涨幅榜的影响很小（20 列只有 1 列的榜首命中），
    但剔掉的是「北交所」这种按交易所筛的集合，与已在名单里的「科创板」同类。
    """
    if taxonomy not in TAXONOMIES:
        raise HTTPException(
            status_code=400, detail=f"taxonomy 只能是 {list(TAXONOMIES)}，收到 {taxonomy}"
        )
    if metric not in ROTATION_METRICS:
        raise HTTPException(
            status_code=400, detail=f"metric 只能是 {list(ROTATION_METRICS)}，收到 {metric}"
        )

    order_column = {
        "strength": SectorDaily.strength,
        "pct_chg": SectorDaily.pct_chg,
        "amount": SectorDaily.amount,
    }[metric]
    dates = list(
        session.scalars(
            select(SectorDaily.trade_date)
            .where(SectorDaily.trade_date <= trade_date, SectorDaily.taxonomy == taxonomy)
            .distinct()
            .order_by(SectorDaily.trade_date.desc())
            .limit(days)
        )
    )
    if not dates:
        return SectorRotation(
            taxonomy=taxonomy,
            metric=metric,
            metric_label=ROTATION_METRICS[metric],
            top=top,
            columns=[],
        )

    rows = session.execute(
        select(
            SectorDaily.trade_date,
            SectorDaily.sector_code,
            SectorDaily.name,
            SectorDaily.pct_chg,
            order_column,
        )
        .where(SectorDaily.taxonomy == taxonomy, SectorDaily.trade_date.in_(dates))
        .order_by(SectorDaily.trade_date.desc(), order_column.desc().nullslast())
    ).all()

    # 每天只留前 top 个。截断放在 Python 里而不是写窗口函数：SQLite 虽然支持，
    # 但这里总量最多 60 × 270 ≈ 1.6 万行，够小，可读性比省那点内存重要
    # 只有强度榜不过滤（见 `ROTATION_EXCLUDE` 的说明）
    excluded = () if metric == "strength" else ROTATION_EXCLUDE
    grouped: dict[date, list[RotationCell]] = {day: [] for day in dates}
    for day, code, name, pct_chg, value in rows:
        bucket = grouped[day]
        if len(bucket) >= top:
            continue
        if any(word in (name or "") for word in excluded):
            continue
        bucket.append(
            RotationCell(code=code, name=name or code, value=value, pct_chg=pct_chg)
        )

    return SectorRotation(
        taxonomy=taxonomy,
        metric=metric,
        metric_label=ROTATION_METRICS[metric],
        top=top,
        columns=[
            RotationColumn(trade_date=day, cells=grouped[day]) for day in dates
        ],
    )


# 「领涨」行单独一个接口，见 schemas.RotationLeaderDay 的说明：
# 它跟着**选中的板块**走、点一次格子换一次，而矩阵本身不变。
@router.get("/rotation/leaders", response_model=list[RotationLeaderDay])
def rotation_leaders(
    taxonomy: str = Query(
        TAXONOMY_SELECTED,
        description="kph_selected=精选板块 kph_industry=行业板块",
    ),
    code: str = Query(..., description="板块代码（矩阵格子里那个）"),
    days: int = Query(20, ge=5, le=ROTATION_MAX_DAYS, description="跟着矩阵的窗口"),
    trade_date: date = Depends(resolve_trade_date),
    session: Session = Depends(get_db),
) -> list[RotationLeaderDay]:
    """某板块在最近 N 个交易日的**涨停股**（矩阵「领涨」行的数据）。

    与 `/rotation` 的列严格同序（都由同一张表、同一个日期降序取出来），前端按
    `trade_date` 对齐即可。
    """
    if taxonomy not in TAXONOMIES:
        raise HTTPException(
            status_code=400, detail=f"taxonomy 只能是 {list(TAXONOMIES)}，收到 {taxonomy}"
        )

    # 板块代码 → 名称。`stock_concept` 存的是名字、不是代码，所以这一步绕不开；
    # 取最新一天的名字（板块改名极罕见，真改了也只会影响历史列的叫法）
    name = session.scalar(
        select(SectorDaily.name)
        .where(
            SectorDaily.taxonomy == taxonomy,
            SectorDaily.sector_code == code,
            SectorDaily.name.is_not(None),
        )
        .order_by(SectorDaily.trade_date.desc())
        .limit(1)
    )
    dates = list(
        session.scalars(
            select(SectorDaily.trade_date)
            .where(SectorDaily.trade_date <= trade_date, SectorDaily.taxonomy == taxonomy)
            .distinct()
            .order_by(SectorDaily.trade_date.desc())
            .limit(days)
        )
    )
    if not name or not dates:
        return []

    leaders = _rotation_leaders(session, {day: name for day in dates})
    return [
        RotationLeaderDay(trade_date=day, leaders=leaders.get(day, [])) for day in dates
    ]


@router.get("/compare", response_model=SectorCompare)
def compare(
    codes: str = Query(..., description="板块代码，逗号分隔"),
    days: int = Query(30, ge=5, le=CURVE_MAX_DAYS, description="对比窗口（交易日）"),
    trade_date: date = Depends(resolve_trade_date),
    session: Session = Depends(get_db),
) -> SectorCompare:
    """多板块走势对比。

    库里没有板块收盘价，所以把逐日涨跌幅复利成一条「起点 = 100」的净值曲线 ——
    纵轴没有真实含义（起点是任选的），但**相对强弱**是准的，对比图要看的正是这个。
    某天缺值的板块从那天起断线，不插值。
    """
    wanted = [item.strip() for item in codes.split(",") if item.strip()]
    if not wanted:
        raise HTTPException(status_code=400, detail="codes 不能为空")
    if len(wanted) > COMPARE_LIMIT:
        raise HTTPException(
            status_code=400, detail=f"最多同时对比 {COMPARE_LIMIT} 个板块，收到 {len(wanted)}"
        )

    # 与排行、单个板块走势同口径：窗口截止到 resolve_trade_date 那天
    days_list = list(
        session.scalars(
            select(SectorDaily.trade_date)
            .distinct()
            .where(SectorDaily.trade_date <= trade_date)
            .order_by(SectorDaily.trade_date.desc())
            .limit(days)
        )
    )
    if not days_list:
        raise HTTPException(status_code=404, detail="暂无板块数据，请先在「数据管理」中执行采集")
    dates = sorted(days_list)

    rows = session.execute(
        select(
            SectorDaily.sector_code,
            SectorDaily.name,
            SectorDaily.taxonomy,
            SectorDaily.trade_date,
            SectorDaily.pct_chg,
        ).where(
            SectorDaily.sector_code.in_(wanted),
            SectorDaily.trade_date >= dates[0],
            SectorDaily.trade_date <= dates[-1],
        )
    ).all()

    # {代码: (名称, 分类, {日期: 涨跌幅})}
    grouped: dict[str, tuple[str, str, dict[date, float | None]]] = {}
    for code, name, taxonomy, day, pct in rows:
        entry = grouped.setdefault(code, (name or code, taxonomy or "", {}))
        entry[2][day] = pct

    series: list[SectorCompareSeries] = []
    for code in wanted:
        if code not in grouped:
            continue
        name, taxonomy, by_date = grouped[code]
        values: list[float | None] = []
        previous: float | None = None
        for index, day in enumerate(dates):
            pct = by_date.get(day)
            if index == 0:
                # 首日只作为基准；它的涨跌幅算的是窗口之前那天到首日，用不上
                previous = COMPARE_BASE if day in by_date else None
            elif previous is not None and pct is not None:
                previous = previous * (1 + pct / 100)
            else:
                previous = None
            values.append(previous)
        series.append(
            SectorCompareSeries(code=code, name=name, taxonomy=taxonomy, values=values)
        )

    return SectorCompare(dates=dates, base=COMPARE_BASE, series=series)


@router.get("/members", response_model=SectorMembers)
def members(
    code: str = Query(..., description="板块代码"),
    trade_date: date = Depends(resolve_trade_date),
    session: Session = Depends(get_db),
) -> SectorMembers:
    """板块成分股，按当日涨跌幅降序。

    首次访问该板块时向开盘红现取并落库，之后同一天再看直接读库。

    ⚠️ **当日的成分股不一定有**：开盘红是盘后某个时刻才把当天这一份更新出来的
    （实测同一天 21:20 还返回 `errcode=1020`，21:55 就有了）。取不到时返回 `note`
    说明原因，而不是让页面显示成「该板块没有成分股」。
    """
    board = session.scalars(select(SectorBasic).where(SectorBasic.code == code)).first()
    if board is None:
        raise HTTPException(status_code=404, detail=f"未知板块代码 {code}")

    rows, note = _board_members(code, trade_date)
    limit_up = _limit_up_state(session, trade_date)
    items = [
        SectorMemberItem(
            code=row.code,
            name=row.name,
            close=row.close,
            pct_chg=row.pct_chg,
            amount=row.amount,
            turnover=row.turnover,
            consecutive=limit_up.get(row.code),
        )
        for row in rows
    ]
    return SectorMembers(
        trade_date=trade_date,
        sector_code=code,
        sector_name=board.name,
        taxonomy=board.taxonomy,
        # 开盘红一次给全量、翻页到底，所以「取到几只」就是成分股数
        member_count=len(items) or None,
        # 换开盘红之后不再有 100 行上限，字段保留是为了不动前端类型
        truncated=False,
        note=note,
        members=items,
    )


def _read_members(session: Session, code: str, trade_date: date) -> list[SectorMember]:
    return list(
        session.scalars(
            select(SectorMember)
            .where(
                SectorMember.sector_code == code, SectorMember.trade_date == trade_date
            )
            # 必须显式排序：写库时是按涨跌幅降序取的，但读缓存时 SQLite 会按主键
            # （也就是代码）给，不写 ORDER BY 就会出现「标题说降序、实际按代码排」
            .order_by(SectorMember.pct_chg.desc().nullslast())
        )
    )


def _board_members(code: str, trade_date: date) -> tuple[list[SectorMember], str | None]:
    """读库；库里没有就现取一次再读。返回（成分股, 取不到的原因）。

    现取与读库各开一个新的 session：现取那次是**另一个事务**写的，
    用请求自带的 session 接着读看不到（SQLite 在 WAL 下的读事务是一个快照）。
    """
    with session_scope() as reader:
        cached = _read_members(reader, code, trade_date)
    if cached:
        return cached, None

    try:
        SectorCollector().collect_members(trade_date, code)
    except Exception as exc:  # noqa: BLE001 - 取不到不该让板块页整个 500
        logger.warning("板块 %s %s 成分股取数失败：%s", code, trade_date, exc)
        return [], "开盘红的成分股当日要等盘后更新，这次没取到（历史日期不受影响）"

    with session_scope() as reader:
        rows = _read_members(reader, code, trade_date)
    if rows:
        return rows, None
    return [], "开盘红的成分股当日要等盘后更新，这次没取到（历史日期不受影响）"
