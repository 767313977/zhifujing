"""个股分析：输入「代码 / 名称 / 拼音首字母」→ 直接给结论。

移植自原型 yangban-desk 的「查票分析」框（它那个框下面直接挂阶段标签 + 日期）。
站内落点是一个独立页面 `/stock-analysis`，导航里跟在「悟道之路」后面。

结论本身来自 `services/patterns.classify_phase` —— 与悟道那几张名单**共用同一批判据**
（`_wudao_is_sample` / `_wudao_is_diverge_or_dump` / `_wudao_day_geom`）。

⚠️ **但两处已经不完全等价了，别再按旧说法理解**（2026-10-09）：

- **板块**：名单只收**创业板 + 科创板**（`is_wudao_board`），这里是**全市场都判** ——
  所以查一只主板票也可能显示「明天盯 / 明天预案」，而名单里永远不会有它（这是**有意的**：
  个股页就该能查任何票）。
- **收盘过滤**：名单出口另有一道 `_wudao_leave_ok`（收盘离开最高 ≤ 2.9%，见 §8.88.3）。
  它**故意没有**并进 `_wudao_is_sample` —— 那个判据还被「洗完可盯」共用，并进去会一起砍掉。
  实测（2026-09-28）：这里判「明天盯」的 31 只里**只有 6 只在名单**；差的 25 只中 18 只是
  板块不符，剩下 7 只（板块合法的 13 只里的 **54%**）就是被这道过滤挡掉的。
- 反过来：**进名单的票必然通过了原型判据**，但它的阶段标签仍可能被更靠前的阶段抢走
  （`classify_phase` 的优先级：启动 > 吵/出货 > 明天盯 > 休息中 > 刚有人气 > 没动静）。

⚠️ 原型那个框写着「技术 / 新闻 / 基本面三面合议」，照实说我们这版是什么：

- **技术面** = 这里的阶段判定（`classify_phase`）；
- **新闻面** = **另一个接口**（`GET /api/stock/{code}/news`，东财口径，每次现取），
  页面自己再发一次请求，**不在**这条返回里 —— 它慢且可能失败，不该拖住结论（见 §8.86）；
- **基本面** = 只有**所属行业** + 涨停/龙虎榜计数，**没有**盈利 / 估值 / 现金流那一套
  （想看市值等去个股页）。所以「基本面」这个词是**借来的**，别当它真做了基本面分析。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Lhb, LimitPool, StockBasic, StockConcept, StockDaily
from app.schemas import StockAnalysis
from app.services.stock_lookup import LookupError, resolve_code
from app.services.stock_phase import load_phase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analysis", tags=["个股分析"])


@router.get("/lookup", response_model=StockAnalysis)
def lookup(
    q: str = Query(
        ...,
        min_length=1,
        max_length=32,
        description="6 位代码 / 股票名称 / 拼音首字母（如 300654 / 世纪天鸿 / sjth）",
    ),
    session: Session = Depends(get_db),
) -> StockAnalysis:
    """解析输入并给出当天结论。

    解析规则与自选股那个输入框**共用** `services/stock_lookup.resolve_code`
    （代码 → 名称全等 → 首字母全等 → 名称包含 / 首字母开头），解析不出或多解时
    原样把它的文案转成 400 —— 那套文案本来就是写给用户看的。
    """
    try:
        code = resolve_code(q)
    except LookupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    basic = session.execute(
        select(StockBasic.name, StockBasic.industry).where(StockBasic.code == code)
    ).first()
    name = (basic[0] if basic else None) or code
    industry = basic[1] if basic else None

    # 「最近一根」要滤掉停牌残行（iFinD 给的那种只有收盘价、`pct_chg` 为空的行）——
    # 同页的阶段判定 `load_phase` 也是这么滤的，不滤这边会取到残行、日期与阶段判定
    # 对不上（页面会出现「结论是今天、K 线日期却是昨天」）。2026-10-10 加。
    latest = session.scalar(
        select(StockDaily)
        .where(StockDaily.code == code, StockDaily.pct_chg.is_not(None))
        .order_by(StockDaily.trade_date.desc())
        .limit(1)
    )

    # 题材：与 `api/stock.themes` 同一条路 —— 取该股出现过的**最近一天**（涨停天梯快照），
    # 所以它回答的是「最近一次涨停是因为哪个板块」，不是「它属于哪些概念」。
    # 从没涨停过的票这里就是空的，如实返回空列表。
    sectors: list[str] = []
    concept_day = session.scalar(
        select(func.max(StockConcept.trade_date)).where(StockConcept.code == code)
    )
    if concept_day is not None:
        sectors = sorted(
            session.scalars(
                select(StockConcept.concept).where(
                    StockConcept.code == code, StockConcept.trade_date == concept_day
                )
            )
        )

    # 涨停：`limit_pool` 主键含 `pool_type`，同一票同一天最多一行 → 行数即「涨停天数」。
    limit_up_count = (
        session.scalar(
            select(func.count())
            .select_from(LimitPool)
            .where(LimitPool.code == code, LimitPool.pool_type == "up")
        )
        or 0
    )
    # 龙虎榜：**按「上榜天数」数，不按行数**。`lhb` 的主键是 (trade_date, code, reason)，
    # 同一天有几个上榜原因就是几行（实测库里 21236 行里有 2557 个「票-日」是多行、最多 6 行）
    # —— `count(*)` 会把它们当成「上榜 N 次」。去重后与 `limit_up_count`（因主键含
    # `pool_type`，本身即天数）口径一致。
    lhb_count = (
        session.scalar(
            select(func.count(func.distinct(Lhb.trade_date))).where(Lhb.code == code)
        )
        or 0
    )
    # 上面两个计数的**分母窗口**（两张表各自覆盖的交易日数）。必须一起返回：
    # 它们数的是「库里已有的那些天」，不是历史累计 —— 不写窗口就容易被读成
    # 「这只票一辈子涨停 12 次」。两张表分开给，因为窗口不一定一样。
    limit_up_days = (
        session.scalar(select(func.count(func.distinct(LimitPool.trade_date)))) or 0
    )
    lhb_days = session.scalar(select(func.count(func.distinct(Lhb.trade_date)))) or 0

    return StockAnalysis(
        code=code,
        name=name,
        trade_date=latest.trade_date if latest else None,
        close=latest.close if latest else None,
        pct_chg=latest.pct_chg if latest else None,
        phase=load_phase(session, code),
        industry=industry,
        sectors=sectors,
        limit_up_count=int(limit_up_count),
        lhb_count=int(lhb_count),
        limit_up_days=int(limit_up_days),
        lhb_days=int(lhb_days),
    )
