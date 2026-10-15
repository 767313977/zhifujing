"""形态选股的查询接口。

**一次查全、前端筛**：命中量每天几百条，`/hits` 的 SQL 一次把当天全部取出来，
前端自己按形态与分数切 —— 沿用板块页 `/ranking` 的做法（那里一次返回 465 个板块
给前端反复切排序）。拖滑块就发一次请求是没必要的，而且筛选逻辑放前端，
「标签上的家数」和「筛出来的行数」天然不会打架。

⚠️ 但**响应**会按 `limit` 截断（默认 50，2026-09-24 用户要求从 500 改小）。
所以「家数」要看 `/summary` 的全量统计，别拿返回条数当总数 ——
前端在截断时会给提示（`HIT_LIMIT`）。
"""

import threading
from collections import OrderedDict
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import (
    PatternHit,
    StockBasic,
    StockConcept,
    StockDaily,
    StockUniverse,
    TradeCalendar,
)
from app.schemas import (
    PatternCount,
    PatternHitItem,
    PatternMeta,
    PatternStockOut,
    PatternSummary,
    PatternTrackDetail,
    PatternTrackOut,
    PoolStandingOut,
)
from app.services import pattern_track
from app.services.patterns import PATTERNS
from app.services.stock_phase import load_phases
from app.sources.base import chunked

router = APIRouter(prefix="/api/patterns", tags=["patterns"])

# 单日命中去重后最多约等于全市场代码数（5300+）。老版本 SQLite 的单条 SQL 变量
# 上限是 999，一次性 IN 这么多 code 会 OperationalError；3.32+ 放宽到 32766 才不炸。
# 按 900 一批拆（给 999 留余量），结果在内存里合并，排序 / 去重语义不变。
_IN_BATCH = 900

_META = {pattern.key: pattern for pattern in PATTERNS}
# 注册表里**当前存在**的 key，所有查询都必须带上这道过滤。
#
# 为什么需要它：形态被删掉之后，**历史那几天的 `pattern_hit` 行还在库里**
# （扫描是「先删后插」，但它只重写自己扫的那一天，没人去清旧日期）。少这道过滤
# 会出现三件怪事：① 被删形态的命中混进默认列表，标签名显示成 key、分组显示「其他」；
# ② 「共 N 只命中」把它们的去重只数也算进去，与筛选条上各家数之和对不上；
# ③ 飞书简报的口径跟着一起错。
# 过滤放在查询侧而不是去删库：删库要在云端也执行一遍 SQL，而过滤跟着代码走，
# 一次部署就生效，历史行留着也无害。
_KEYS = tuple(_META)

# `/track` / `/standing` 的结果缓存。这是全站最重的一类读接口（30 个循环 × 30 天要从
# `stock_daily` 拉 40 多万行压成矩阵，实测 2.4 秒），而**它依赖的数据一天只变一次**
# （当天采集跑完）。所以缓存键里带上「数据版本」= (最新命中日, 最新日线日)，
# 采集一写库就自动失效，不需要靠 TTL 猜。
#
# ⚠️ 旧实现是个**无上限的普通 dict，且过期条目从不删除** —— 而键里带着 cohorts / top /
# track_days（用户可传），组合远不止注释里写的「4×2×2」，每个组合都是一份压出来的大对象，
# 反复换参数就能把内存堆满。这里换成**带上限的 LRU**（2026-10-11 修）：命中提到队首，
# 满了淘汰最久未用的那条。
_TRACK_TTL = timedelta(minutes=10)
_TRACK_CACHE_MAX = 32


class _LruCache:
    """够用就好的 LRU + TTL：`get` 命中即算「刚用过」，`set` 满了丢最久未用的那条。

    单独写一个而不是引依赖：全站就这处用得上，几十行足够。加锁是因为同步接口跑在线程池
    里，多个请求会同时读写同一个 OrderedDict。
    """

    def __init__(self, max_entries: int, ttl: timedelta) -> None:
        self.max_entries = max_entries
        self.ttl = ttl
        self._items: OrderedDict[tuple, tuple[datetime, object]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple):
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            stamp, value = item
            if datetime.now() - stamp >= self.ttl:
                # 过期即删：LRU 管「条数上限」，这里管「数据版本没变但结果已该重算」
                del self._items[key]
                return None
            self._items.move_to_end(key)
            return value

    def set(self, key: tuple, value: object) -> None:
        with self._lock:
            self._items[key] = (datetime.now(), value)
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)


_TRACK_CACHE = _LruCache(_TRACK_CACHE_MAX, _TRACK_TTL)


def _track_version(db: Session) -> tuple[date | None, date | None]:
    """`/track` 依赖的两张表的最新日期 —— 缓存键的一部分。"""
    return (
        db.scalar(select(func.max(PatternHit.trade_date))),
        db.scalar(select(func.max(StockDaily.trade_date))),
    )


def _latest_date(db: Session) -> date | None:
    """最近一个**有命中记录**的交易日。

    不能用「最近交易日」：当天的扫描要等日线采完才跑，用日历日期当默认值会
    让页面在收盘后到扫描完成之间显示空白 —— 看起来像坏了，其实只是还没到点。
    """
    return db.scalar(select(func.max(PatternHit.trade_date)))


@router.get("/catalog", response_model=list[PatternMeta])
def catalog() -> list[PatternMeta]:
    """形态清单，前端据此生成分组筛选条。"""
    return [
        PatternMeta(key=p.key, name=p.name, group=p.group) for p in PATTERNS
    ]


def _window_dates(db: Session, target: date, days: int) -> list[date]:
    """`target` 及往前共 `days` 个交易日，**升序**。`days <= 1` 就是 `[target]`。

    窗口一律按**交易日历**往前数（不是自然日），与 `_hits_window` 同源 ——
    两处要是各数各的，「近 10 日」的名单和徽标就会差一两天。
    """
    if days <= 1:
        return [target]
    window = db.scalars(
        select(TradeCalendar.trade_date)
        .where(TradeCalendar.trade_date <= target)
        .order_by(TradeCalendar.trade_date.desc())
        .limit(days)
    ).all()
    return list(window)[::-1]


def _intersection_codes(
    db: Session, dates: list[date], keys: list[str], min_score: float = 0.0
) -> list[str]:
    """在 `dates` 这几个交易日里命中**全部** `keys` 的 code 列表（多选=**交集**）。

    `having count(distinct pattern) = len(keys)` 一步求交 —— 不用把每个形态的 code
    集合都取回内存再求交。`/hits` 与 `/preview` 共用这一条定义，免得两处口径漂移。

    - 单日模式传 `[target]`：交集 = **同一天**命中全部所选形态（2026-09-27 定的口径）。
    - 窗口模式传窗口那几天：交集 = **窗口内先后命中过每一个**，不要求同一天
      （2026-10-14 补 —— 原先窗口模式取的是「命中任意一个」，前端再拿首次那天的标签
      去要求全部命中，一只票第 1 天中 A、第 3 天中 B 会被筛掉）。

    ⚠️ 先**去重**再比个数：判据是「命中的不同形态个数 = 传入个数」，重复的 key 会让
    右边多算一个、把本该非空的结果打成空。**去重必须在这一层**（两个调用方都靠它），
    调用方各自去重早晚会漏一处 —— `?pattern=a&pattern=a` 这种手工拼的 URL 不该悄悄
    变成「0 只」。
    """
    uniq = list(dict.fromkeys(keys))
    if not uniq or not dates:
        return []
    return list(
        db.scalars(
            select(PatternHit.code)
            .where(
                PatternHit.trade_date.in_(dates),
                PatternHit.pattern.in_(uniq),
                PatternHit.score >= min_score,
            )
            .group_by(PatternHit.code)
            .having(func.count(func.distinct(PatternHit.pattern)) == len(uniq))
        )
    )


@router.get("/preview", response_model=list[PatternCount])
def preview(
    db: Session = Depends(get_db),
    trade_date: date | None = Query(None, alias="date"),
    min_score: float = Query(0.0, ge=0.0, le=100.0),
    pattern: list[str] | None = Query(
        None, description="当前已选形态（可重复传多个）。不传 = 默认视图"
    ),
    days: int = Query(
        1,
        ge=1,
        le=60,
        description="窗口天数，与 `/hits` 同口径（>1 时按窗口内先后命中过算）",
    ),
) -> list[PatternCount]:
    """每个形态「**再加上它会剩多少只**」—— 筛选条 chip 徽标专用的交集预览。

    徽标要回答的不是「这个形态全市场有多少只」，而是「以当前选择为前提，再点它会
    剩多少只」：交集很容易筛到 0 只（选了「放量上涨」再点「揉搓线」可能一只都不剩），
    点之前就该看得出来。

    - **没选任何形态**：每个形态的预览值就是它自己的全市场命中家数（与 `/summary`
      的 `by_pattern` 同口径，这里照样算一遍，好让前端一条口径走到底）。
    - **选了 k 个**：先求出命中这 k 个形态的**全部** code（`_intersection_codes`），
      再数这些 code 里各命中了哪些形态 —— 对候选 X 这个数就是 `|已选 ∩ X|`。
      已选中的形态自己也在结果里（值等于当前交集数），徽标不会因为「已选中」而空掉。
    - **交集为空**：全部候选都返回 0（正是要显示的效果），不做特殊处理。

    ⚠️ **`days` 必须跟着 `/hits` 一起传**（2026-10-14 补）：徽标算的是「再点它会剩多少只」，
    而列表在窗口模式下是**窗口口径**。徽标按单日算、列表按窗口算，数字和行数就会对不上
    （用户 2026-10-14 报的那个不一致）。两个接口共用 `_window_dates` 与
    `_intersection_codes`，所以只要参数一致，口径就一致。

    只发两条查询（求交 + 分组计数），与候选形态个数无关；与 `/hits` 共用
    `_intersection_codes` 这一条定义，所以徽标数点进去的行数不会打架。
    """
    target = trade_date or _latest_date(db)
    if target is None:
        return []

    picked = [key for key in (pattern or []) if key]
    unknown = [key for key in picked if key not in _META]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"未知形态 {', '.join(unknown)}，可选：{', '.join(_META)}",
        )

    window = _window_dates(db, target, days)
    counts: dict[str, int] = {}
    if picked:
        codes = _intersection_codes(db, window, picked, min_score)
        # codes 为空 → counts 保持全 0（交集为空，任何候选都还是 0）
        for batch in chunked(sorted(codes), _IN_BATCH):
            rows = db.execute(
                select(
                    PatternHit.pattern,
                    func.count(func.distinct(PatternHit.code)),
                )
                .where(
                    PatternHit.trade_date.in_(window),
                    PatternHit.pattern.in_(_KEYS),
                    PatternHit.score >= min_score,
                    PatternHit.code.in_(batch),
                )
                .group_by(PatternHit.pattern)
            ).all()
            for key, n in rows:
                counts[key] = counts.get(key, 0) + n
    else:
        rows = db.execute(
            select(PatternHit.pattern, func.count(func.distinct(PatternHit.code)))
            .where(
                PatternHit.trade_date.in_(window),
                PatternHit.pattern.in_(_KEYS),
                PatternHit.score >= min_score,
            )
            .group_by(PatternHit.pattern)
        ).all()
        counts = {key: n for key, n in rows}

    return [
        PatternCount(
            pattern=p.key,
            pattern_name=p.name,
            group=p.group,
            stocks=counts.get(p.key, 0),
        )
        for p in PATTERNS
    ]


def _hits_window(
    db: Session, target: date, picked: list[str], min_score: float, days: int
) -> tuple[list[PatternHit], dict[str, tuple[int, date]]]:
    """窗口模式：把窗口内**同一只票的重复命中合并成一条**，日期取首次那天。

    为什么需要：形态里有**状态型**的 —— 条件一直成立就天天命中。「堆量上涨后缩量」就是：
    量能天天创新低就天天入选（实测中红医疗 8/14~8/25 连中 6 天），看十天名单时同一只票
    占五六行，榜单直接被撑爆（用户 2026-10-14 点名的「去噪」）。

    返回 `(代表行, 附加信息)`，附加信息给出
    `{code: (窗口内命中过的天数, 最后一次命中的日期)}` 供接口回填。代表行按模式分两种：

    - **传了 `picked`（形态视图）**：每只留下的票、每个所选形态各出一条「首次命中」的记录
      —— 这些记录的 `trade_date` 各不相同（各形态首次那天），每行的 `patterns` 才带得全
      所有所选形态（前端按「这一行是否包含全部所选形态」兜底过滤，缺一个会被筛掉）；
      接口取其中最早那天当这一行的 `trade_date`。
    - **没传 `picked`（默认视图）**：不做交集，只按票去重 —— 代表行就是它**首次命中那天**
      的全部命中（那天它中了哪几个形态，标签就显示哪几个）。这条路 2026-10-15 才放开
      （原先「没选形态」时前端直接把窗口按钮禁掉，理由是「合并得先确定是哪个形态在
      重复」）—— 但「同一只票只出一条」跟选不选形态无关，默认视图一样会被状态型撑爆。

    ## 多选时是「窗口内先后命中过每一个」（2026-10-14 补）

    与单日模式同一条交集定义（`_intersection_codes`），只是把「同一天」放宽成「窗口内」：
    **窗口内命中过的不同形态个数 = 所选形态数**才留下。原先这里取的是「命中任意一个」，
    而前端拿这一行（首次那天）的标签去要求全部命中 —— 一只票第 1 天中 A、第 3 天中 B
    就会被前端筛掉（用户 2026-10-14 报的口径不一致）。

    为什么不按「形态 × 票」各自串轮：十天窗口里同一只票走出两轮的情况很少，
    按票合并更简单、也正好对上用户要的「同一只票只出一条」。
    """
    window = _window_dates(db, target, days)
    if not window:
        return [], {}

    uniq = list(dict.fromkeys(picked))
    rows = db.scalars(
        select(PatternHit)
        .where(
            PatternHit.trade_date.in_(window),
            # 默认视图（`uniq` 空）就是注册表里那些 key —— 与单日模式的 `else` 分支同口径
            PatternHit.pattern.in_(uniq or _KEYS),
            PatternHit.score >= min_score,
        )
        .order_by(PatternHit.trade_date, PatternHit.score.desc())
    ).all()
    if not rows:
        return [], {}

    hit_keys: dict[str, set[str]] = {}
    first_row: dict[str, dict[str, PatternHit]] = {}
    first_rows: dict[str, list[PatternHit]] = {}
    first_date: dict[str, date] = {}
    seen: dict[str, set[date]] = {}
    last: dict[str, date] = {}
    for row in rows:
        # rows 按日期升序，所以同一形态的第一条就是它的首次、最后一条就是最近那次
        hit_keys.setdefault(row.code, set()).add(row.pattern)
        first_row.setdefault(row.code, {}).setdefault(row.pattern, row)
        seen.setdefault(row.code, set()).add(row.trade_date)
        last[row.code] = row.trade_date
        # 「首次那天」的全部命中：第一次见到这只票就记下那天，之后落在同一天的都收进来
        if first_date.setdefault(row.code, row.trade_date) == row.trade_date:
            first_rows.setdefault(row.code, []).append(row)

    keep: list[PatternHit] = []
    run_info: dict[str, tuple[int, date]] = {}
    for code in first_date:
        if uniq:
            # `hit_keys[code]` 一定 `⊆ uniq`，所以个数相等就是「一个不漏」
            if len(hit_keys[code]) < len(uniq):
                continue
            keep.extend(
                sorted((first_row[code][key] for key in uniq), key=lambda row: row.trade_date)
            )
        else:
            keep.extend(first_rows[code])
        run_info[code] = (len(seen[code]), last[code])
    return keep, run_info


@router.get("/hits", response_model=list[PatternStockOut])
def hits(
    db: Session = Depends(get_db),
    trade_date: date | None = Query(None, alias="date"),
    min_score: float = Query(0.0, ge=0.0, le=100.0),
    pattern: list[str] | None = Query(
        None, description="只看命中这些形态的票（可重复传多个，取**交集**）"
    ),
    days: int = Query(
        1,
        ge=1,
        le=60,
        description="窗口天数：>1 时把窗口内同一只票的重复命中合并成一条（不选形态也生效）",
    ),
    limit: int = Query(50, ge=1, le=3000),
) -> list[PatternStockOut]:
    """某交易日的命中，**按股票归并**（一只票命中多个形态就是一行多标签）。

    ## 两种模式（2026-09-27 用户要求区分）

    - **不传 `pattern`**：全市场命中按评分降序取前 `limit` 只 —— 列表页的默认视图，
      「今天最值得看的几十只」。
    - **传 `pattern`**：只返回命中这些形态的票，且**不再按 50 截断**（调用方传大
      `limit`）—— 「点进一个形态就该看到它的全部命中」，这是用户点名要的。
      **可重复传多个**（2026-10-13 起），多个之间是**交集**：必须**同时满足**全部所选
      形态才留下（用户明确要的是「两个条件都满足」，不是并集）。

    ⚠️ 形态模式下**「评分」与排序都换成所选形态的分数**：一只票可能同时命中了别的
    更高分的形态，但那一屏讲的只是当前选中的这几个，拿别的高分来排序会让人对不上账。
    多选时取所选形态里的**最高分**（与单选口径一致：让这一屏里最强的那个形态代表它）。
    每行仍会带上它命中的**全部**形态（`patterns` 数组，各自带自己的分数），
    所以信息没有丢。

    ⚠️ 取「命中形态的票」之后要按 code 把当天的行**全部**查回来，不能直接按
    `pattern` 过滤行 —— 那样每只票的标签就只剩选中的那些了。

    ## 窗口模式（`days > 1`，2026-10-14 用户要求）

    形态里有**状态型**的：条件一直成立就天天命中，「堆量上涨后缩量」就是（量能天天
    创新低就天天入选，实测中红医疗 8/14~8/25 连中 6 天）—— 看十天名单时同一只票占
    五六行，榜单被撑爆。`days > 1` 时窗口内**同一只票只出一条**，`trade_date` 取
    **首次**命中那天（用户点名要的就是「首次那天的日期」），并带上 `run_days` /
    `last_date` 说明这一轮持续了多久。逻辑见 `_hits_window`。

    ⚠️ 窗口模式**对两种模式都生效**（2026-10-15 放开）：
    - **传了 `pattern`**：窗口内每行的 `patterns` 只带**所选形态**的标签（不再回查「当天
      命中的全部形态」）—— 各行的「当天」已经不是同一天了，回查要按日期分组，代价不值。
    - **没传 `pattern`（默认视图）**：一样按票去重、日期取首次那天，代表行带回**那天**
      命中的全部形态。原先这条路是**禁用**的（「合并得先确定是哪个形态在重复」），
      2026-10-15 用户要求放开 —— 默认视图同样会被状态型形态撑爆。

    ⚠️ **多选在窗口模式下的口径**（2026-10-14 补）：交集从「同一天命中全部」放宽成
    **「窗口内先后命中过每一个」** —— 一只票第 1 天中 A、第 3 天中 B 也算数（原先会被
    前端按「这一行的标签要含全部所选形态」筛掉）。所以窗口模式下每行的 `patterns`
    带的是所选形态**各自首次命中**那几次的记录。与 `/preview` 同口径（那里也要传 `days`）。
    """
    target = trade_date or _latest_date(db)
    if target is None:
        return []

    # 允许 `?pattern=a&pattern=b`。空串条目直接丢掉 —— 手工拼 URL 会出现 `pattern=`，
    # 那不该被当成一个「未知形态」而报 400
    picked = [key for key in (pattern or []) if key]
    if picked:
        unknown = [key for key in picked if key not in _META]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"未知形态 {', '.join(unknown)}，可选：{', '.join(_META)}",
            )

    # 窗口模式下要回填「这一轮持续了几天 / 最近哪天」；单日模式留空。
    # 2026-10-15 起**没选形态（默认视图）也走窗口**，所以这里只看 `days` 不看 `picked`
    run_info: dict[str, tuple[int, date]] = {}
    if days > 1:
        rows, run_info = _hits_window(db, target, picked, min_score, days)
        if not rows:
            return []
    elif picked:
        # **交集**：命中全部所选形态的票才留下（定义见 `_intersection_codes`）
        codes = _intersection_codes(db, [target], picked, min_score)
        if not codes:
            return []
        rows = db.scalars(
            select(PatternHit)
            .where(
                PatternHit.trade_date == target,
                # codes 是「命中该形态的票」，单形态 + min_score=0 时可到全市场量级
                # （5300+），老 SQLite 单条 SQL 变量上限 999、一次 IN 会 OperationalError
                # —— 与下方三处「按 code 集合 IN 查」一样按 `_IN_BATCH`（900，给 999
                # 留余量）分批再 OR 起来，各批结果在内存里合并，与一次 IN 全量等价。
                # sorted 让分批稳定可复现（codes 来自 distinct 查询、顺序未定义）。
                # 走到这里 codes 必非空 —— 空集在上面已经 `return []` 了。
                or_(
                    *[
                        PatternHit.code.in_(batch)
                        for batch in chunked(sorted(codes), _IN_BATCH)
                    ]
                ),
                # 被删掉的形态的标签也要滤掉，否则那一行会多出一个「其他」组
                PatternHit.pattern.in_(_KEYS),
                # `min_score` 两种模式下都过滤行，口径一致（默认 0 时是空操作）
                PatternHit.score >= min_score,
            )
            .order_by(PatternHit.score.desc())
        ).all()
    else:
        rows = db.scalars(
            select(PatternHit)
            .where(
                PatternHit.trade_date == target,
                PatternHit.pattern.in_(_KEYS),
                PatternHit.score >= min_score,
            )
            .order_by(PatternHit.score.desc())
        ).all()
    if not rows:
        return []

    # 下面三处都是「按当天命中的全部 code 集合 IN 查」，集合可能到 5300+ 个，
    # 必须分批（理由见 `_IN_BATCH`）；各批结果在内存里合并，与一次查全等价。
    hit_codes = list({row.code for row in rows})

    # 日均成交额与市值是**股票的属性**，从股票池取；命中记录里不存这两项。
    # 少数票可能已经跌出池子（流动性降到门槛以下），那时给 null 而不是 0 ——
    # 「没有数据」和「是 0」是两件事
    pool: dict[str, tuple] = {}
    for batch in chunked(hit_codes, _IN_BATCH):
        pool.update(
            {
                code: (avg_amount, total_mv)
                for code, avg_amount, total_mv in db.execute(
                    select(
                        StockUniverse.code,
                        StockUniverse.avg_amount,
                        StockUniverse.total_mv,
                    ).where(StockUniverse.code.in_(batch))
                ).all()
            }
        )

    # 板块（口径与个股页「所属题材」一致，见 `PatternStockOut.sectors` 的说明）。
    #
    # ⚠️ 取「该股出现过的**最近一天**」而不是命中当天：`stock_concept` 来自涨停天梯、
    # 只有涨停股有值，用当天口径的话这个列表里 97% 的行会是空的。
    # **一次查全、在内存里按 code 挑最新的那天**：写成「每只票一条相关子查询」会把这个
    # 接口从一次查询变成上千次（`code` 不是那张表主键的首列，每次都要扫）。
    concept_rows = []
    for batch in chunked(hit_codes, _IN_BATCH):
        concept_rows.extend(
            db.execute(
                select(
                    StockConcept.code, StockConcept.trade_date, StockConcept.concept
                ).where(StockConcept.code.in_(batch))
            ).all()
        )
    latest_seen: dict[str, tuple[date, list[str]]] = {}
    for code, seen_on, concept in concept_rows:
        current = latest_seen.get(code)
        if current is None or seen_on > current[0]:
            latest_seen[code] = (seen_on, [concept])
        elif seen_on == current[0]:
            current[1].append(concept)
    sectors = {code: sorted(names) for code, (_, names) in latest_seen.items()}

    # 同花顺行业（三级路径）。与上面的板块**不是一套分类**，见 `PatternStockOut.industry`。
    # 一次查全即可 —— 建池 7 天一次、覆盖全 A，所以基本没有查不到的行；查不到就是 None。
    industries: dict[str, str | None] = {}
    for batch in chunked(hit_codes, _IN_BATCH):
        industries.update(
            dict(
                db.execute(
                    select(StockBasic.code, StockBasic.industry).where(
                        StockBasic.code.in_(batch)
                    )
                ).all()
            )
        )

    grouped: dict[str, PatternStockOut] = {}
    for row in rows:
        meta = _META.get(row.pattern)
        item = PatternHitItem(
            pattern=row.pattern,
            # 形态清单里没有的 key 只可能是旧数据（改过 key），照原样显示不丢信息
            pattern_name=meta.name if meta else row.pattern,
            group=meta.group if meta else "其他",
            score=row.score,
            key_levels=row.key_levels or {},
            detail=row.detail or {},
        )
        existing = grouped.get(row.code)
        if existing is None:
            avg_amount, total_mv = pool.get(row.code, (None, None))
            grouped[row.code] = PatternStockOut(
                code=row.code,
                name=row.name,
                trade_date=row.trade_date,
                # 单日模式：这一轮就今天这一天（窗口模式下下面会覆盖掉）
                run_days=1,
                last_date=row.trade_date,
                close=row.close,
                pct_chg=row.pct_chg,
                amount=row.amount,
                avg_amount=avg_amount,
                total_mv=total_mv,
                sectors=sectors.get(row.code, []),
                industry=industries.get(row.code),
                score=row.score,
                patterns=[item],
            )
        else:
            existing.patterns.append(item)
            # 已经按分数降序取过，所以第一条就是最高分；这里只在必要时提升
            existing.score = max(existing.score, row.score)

    # 窗口模式回填「这一轮持续了几天 / 最近哪天」（见 `_hits_window`）
    for run_code, (run_days, run_last) in run_info.items():
        run_stock = grouped.get(run_code)
        if run_stock is not None:
            run_stock.run_days = run_days
            run_stock.last_date = run_last

    result = sorted(grouped.values(), key=lambda stock: stock.score, reverse=True)
    if picked:
        # 形态模式：把「评分」换成**所选形态里的最高分**再排一次。
        # 上面那句 max 取的是这只票的最高形态分；在当前这一屏里那不是它该显的分
        # （页面上每行的其它形态仍各自带自己的分数，见 `patterns` 数组）。
        # 多选（交集）时一只票**必然**命中其中每一个，取最高那个 —— 与单选口径一致
        wanted = set(picked)
        for stock in result:
            scores = [item.score for item in stock.patterns if item.pattern in wanted]
            if scores:
                stock.score = max(scores)
        result.sort(key=lambda stock: stock.score, reverse=True)
    page = result[:limit]
    # 阶段判定：与个股页／个股分析页**同一个取数壳**（`stock_phase.load_phase`），
    # 保证同一只票在两处给出同一个判定。**只对真正返回的行算**（默认 50 只；
    # 按形态过滤时最多 3000，见 `load_phases` 的说明），不白算被 limit 截掉的那些。
    phases = load_phases(db, [stock.code for stock in page])
    for stock in page:
        verdict = phases.get(stock.code)
        if verdict is not None:
            stock.phase = verdict.phase
            stock.phase_label = verdict.label
    return page


@router.get("/track", response_model=PatternTrackOut)
def track(
    db: Session = Depends(get_db),
    cohorts: int = Query(pattern_track.DEFAULT_COHORTS, ge=1, le=120),
    top: int = Query(pattern_track.DEFAULT_TOP, ge=1, le=200),
    track_days: int = Query(pattern_track.DEFAULT_TRACK_DAYS, ge=1, le=120),
) -> PatternTrackOut:
    """每个筛选日的「评分前 N 只」在其后 `track_days` 个交易日里的走势（循环跟踪）。

    ## 口径

    - **循环**：最近 `cohorts` 个**有命中记录**的交易日，每个日子就是一个循环
      （不是自然日，也不是日历上的最近 N 个交易日）—— 建站早期只有零星几天有命中，
      所以窗口可能跨得更长，页面会如实写出实际天数。
    - **入选**：每天按票归并取最高分、降序取前 `top` 只 —— 与 `/hits` 默认视图、
      以及每天补 DDE 的那批**同一口径**。
    - **收益**：筛选日收盘 → 之后第 n 个交易日收盘（n = 1..`track_days`），
      **按涨跌幅逐日复利**（即前复权口径，见 `services.pattern_track` 的模块说明）。
    - **胜率**：每个点都给两个 —— 上涨占比（`up_pct`）与跑赢当天全市场平均的比例
      （`beat_pct`），外加平均超额（`excess`）。`average` 是所有循环摊平后的同一条线。
    - **样本**：逐循环不去重（同一只票连上三个循环算三个样本），同时给出去重只数。

    ⚠️ 循环还没走到第 n 个交易日时**不返回那个点**（`progress` 只记到能算的那个 n），
    所以越新的循环 `points` 越短。
    """
    # 缓存键里连「统计起点」一起带上：改了配置就该立刻生效，不能吃旧结果
    start = get_settings().pattern_track_start
    key = (cohorts, top, track_days, start, *_track_version(db))
    cached = _TRACK_CACHE.get(key)
    if cached is not None:
        return cached

    result = PatternTrackOut.model_validate(
        pattern_track.track(
            db, start=start, cohorts=cohorts, top=top, track_days=track_days
        )
    )
    _TRACK_CACHE.set(key, result)
    return result


@router.get("/standing", response_model=PoolStandingOut)
def standing(
    db: Session = Depends(get_db),
    cohorts: int = Query(pattern_track.DEFAULT_COHORTS, ge=1, le=120),
    track_days: int = Query(max(pattern_track.HORIZONS), ge=1, le=60),
    line_days: int = Query(5, ge=1, le=30),
) -> PoolStandingOut:
    """悟道六池的「成绩单」：收益（1/3/5/10 日）+ 到线率 + 破位率。

    与 `/track` 的分工：那边问的是「**全形态混合的评分前 50 只**后来怎么样」，
    这边问的是「**这个池子**后来怎么样」—— 悟道那些池子（尤其几百只的安静型池子）
    本来也进不了前 50，只能按池子单独算。这也是「这个池子值不值得留」的唯一依据。

    - **收益**：命中日收盘 → 之后第 n 个交易日（n ∈ 1/3/5/10），按涨跌幅逐日复利；
      超额减掉**它自己那天**的全市场等权平均（与回测脚本、`/track` 同一口径）。
    - **到线率**：命中后 `line_days` 个交易日内**收盘**站上「要过的那条线」
      （样板池的今高、洗盘池的洗盘高）的比例 —— 回答「这一步到底有没有发生」。
    - **破位率**：同期**收盘**跌破「作废位」的比例；作废位优先取 `key_levels` 里那个
      （`wash_low` / `start_low`），没有的池子退回用**命中日最低价**当代理，
      `stop_source` 会写明用的是哪一种（页面上照原样显示）。

    ⚠️ 统计起点由 `Settings.pool_track_start` 定（＝2026-10-08，候选池定稿那天）：
    更早的命中是**另一套候选口径**下的名单，混进来等于把两个分布平均。
    样本会随每天的扫描自然攒起来（`cohorts` 就是实际有命中的扫描日数，示例：刚上线时是 1）；
    想要更早的日子，正确做法是用现在的判定把那些天**重扫一遍**（幂等、零配额）。
    """
    start = get_settings().pool_track_start
    key = ("standing", cohorts, track_days, line_days, start, *_track_version(db))
    cached = _TRACK_CACHE.get(key)
    if cached is not None:
        return cached

    result = PoolStandingOut.model_validate(
        pattern_track.pool_standing(
            db, start=start, cohorts=cohorts, hold_days=track_days, line_days=line_days
        )
    )
    _TRACK_CACHE.set(key, result)
    return result


@router.get("/track/detail", response_model=PatternTrackDetail)
def track_detail(
    db: Session = Depends(get_db),
    trade_date: date = Query(..., alias="date", description="哪一天的循环"),
    top: int = Query(pattern_track.DEFAULT_TOP, ge=1, le=200),
    track_days: int = Query(pattern_track.DEFAULT_TRACK_DAYS, ge=1, le=120),
) -> PatternTrackDetail:
    """某个循环选中的票的**逐日明细**：是哪 50 只、之后每个交易日各涨跌多少。

    - 入选口径与 `/track` 完全一致（按票归并取最高分的前 `top` 只、只认注册表里的形态），
      所以两边的「只数」永远对得上。
    - `pct` 的每一列是**那一天的当日涨跌幅**（相对前一交易日），不是从筛选日起算的累计。
    - `days` 给出每列对应的实际交易日；`progress` = 已经走到第几个交易日（与 `/track` 同义）。
    - 只走到「最后一天真有行情」那天，没走到的日子**不返回**（不是返回 null 让你以为是
      当天没涨跌）。
    """
    return PatternTrackDetail.model_validate(
        pattern_track.detail(db, trade_date, top=top, track_days=track_days)
    )


@router.get("/summary", response_model=PatternSummary)
def summary(
    db: Session = Depends(get_db),
    trade_date: date | None = Query(None, alias="date"),
) -> PatternSummary:
    """各形态的命中家数，给筛选条的徽标、首页面板与飞书简报用。"""
    target = trade_date or _latest_date(db)
    if target is None:
        return PatternSummary(trade_date=None, total_hits=0, total_stocks=0, by_pattern=[])

    rows = db.execute(
        select(PatternHit.pattern, func.count(), func.count(func.distinct(PatternHit.code)))
        .where(PatternHit.trade_date == target, PatternHit.pattern.in_(_KEYS))
        .group_by(PatternHit.pattern)
    ).all()
    counts = {pattern: (hits, stocks) for pattern, hits, stocks in rows}

    # 按形态清单的顺序输出，命中为 0 的也留着 —— 筛选条上少一个按钮比显示 0 更让人困惑
    by_pattern = [
        PatternCount(
            pattern=p.key,
            pattern_name=p.name,
            group=p.group,
            stocks=counts.get(p.key, (0, 0))[1],
        )
        for p in PATTERNS
    ]
    return PatternSummary(
        trade_date=target,
        total_hits=sum(hits for hits, _ in counts.values()),
        total_stocks=db.scalar(
            select(func.count(func.distinct(PatternHit.code))).where(
                PatternHit.trade_date == target, PatternHit.pattern.in_(_KEYS)
            )
        )
        or 0,
        by_pattern=by_pattern,
    )
