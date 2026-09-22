"""板块资金流采集（**同花顺**口径，日频）。

零 iFinD 配额（走同花顺数据中心，经 akshare），**每天只有 2 个请求**（概念 + 行业），
成本可以忽略，所以和涨停原因一样排在配额让路判断之外。

三条要记住的限制（详见 `models.SectorFundFlow`）：

1. **只能采当天**：来源给的是「即时」窗口，没有历史日期可指定 —— 所以没有回补脚本，
   库里有多少天，就是从哪天开始采的。补不了就是补不了，不假装有。
2. **口径与站内板块不是一套**：同花顺概念 359 / 同花顺行业 90，而 `sector_daily` 是
   开盘红的精选 / 行业。名字对不上，两张表不要 join。
3. **按名字去重**：来源同一名字会给两行、值还不一样（387 行 / 359 个名字），
   这里**留第一行**（与接口返回的排序一致，序号靠前的那行）。
"""

import logging
from datetime import date, datetime, time

from sqlalchemy import delete, func, select

from app.config import Settings, get_settings
from app.db import session_scope
from app.models import (
    FUND_FLOW_CONCEPT,
    FUND_FLOW_INDUSTRY,
    SectorFundFlow,
)
from app.sources.akshare_source import AkshareSource

logger = logging.getLogger(__name__)

# 流入 - 流出 与「净额」应当相等（实测 77.68 - 52.84 = 24.84，一位不差）。
# 留一点浮点余量，超过才告警 —— 这是**来源自身**的一致性检查，不依赖别的表
NET_TOLERANCE = 0.05
# 每个口径**去重后**行数的合理下限，低于它就告警。
#
# 卡在「残页」而不是「正常波动」之间：盘后完整时概念约 359 个名字、行业 90 个；
# 竞价时段实测只回 200~337 行（见 `AkshareSource.concept_fund_flow`），所以低于
# 320 / 85 基本就是采到了残缺快照 —— 这时页面上「净流出榜」会缺尾部（源按涨跌幅
# 排序，缺的正是跌得最狠那批），**但已经写进去的仍是真数据**，所以只告警不改数据，
# 由人去判断要不要重采。真正的保护是下面 `CLOSE_READY` 那个守卫。
MIN_ROWS = {FUND_FLOW_CONCEPT: 320, FUND_FLOW_INDUSTRY: 85}
# 尾盘集合竞价 15:00 结束，留 5 分钟给数据源刷终值。
# **早于这个时刻取「当天」的数据，拿到的是当时的快照，不是收盘终值** —— 标成同一天
# 就等于把昨天的收盘数写成今天的（实测盘前 09:00 取到的仍是上一交易日的收盘值）。
CLOSE_READY = time(15, 5)


class FlowCollector:
    """板块资金流采集器。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.source = AkshareSource(self.settings)

    def _rows(self, taxonomy: str) -> list[dict]:
        raw = (
            self.source.concept_fund_flow()
            if taxonomy == FUND_FLOW_CONCEPT
            else self.source.industry_fund_flow()
        )
        records: list[dict] = []
        seen: set[str] = set()
        net_bad = 0
        for item in raw:
            name = str(item.get("行业") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            record = {
                "taxonomy": taxonomy,
                "name": name,
                "pct_chg": item.get("行业-涨跌幅"),
                "in_amount": item.get("流入资金"),
                "out_amount": item.get("流出资金"),
                "net_amount": item.get("净额"),
                "member_count": item.get("公司家数"),
                "leader_name": (str(item.get("领涨股")).strip() or None)
                if item.get("领涨股") is not None
                else None,
                "leader_pct_chg": item.get("领涨股-涨跌幅"),
            }
            inflow, outflow, net = (
                record["in_amount"],
                record["out_amount"],
                record["net_amount"],
            )
            if None not in (inflow, outflow, net) and abs(
                inflow - outflow - net
            ) > NET_TOLERANCE:
                net_bad += 1
            records.append(record)

        if net_bad:
            logger.warning(
                "%s 有 %d 行的「流入-流出」对不上净额，先信净额（页面只用它）",
                taxonomy,
                net_bad,
            )
        low = MIN_ROWS.get(taxonomy)
        if low and len(records) < low:
            logger.warning(
                "%s 只取到 %d 行（往常 ≥ %d），可能接口改了字段或分页，别当正常",
                taxonomy,
                len(records),
                low,
            )
        return records

    def collect(self, trade_date: date) -> dict[str, int]:
        """采两个口径并落库（按日整段替换），返回 `{taxonomy: 行数}`。

        两个前置判断，任一不满足就不写库 —— **宁可这天空着，也不写一行错日期的数**：

        - `trade_date == 今天` 且**还没收盘**（见 `CLOSE_READY`）：这时来源给的是
          上一交易日的收盘值或盘中的瞬时值，标成今天就错了。
        - `trade_date != 今天`：来源只服务「现在」，采到的是**最近的**快照，
          与指定日期未必对得上，所以只告警不拦（调度迟跑一天是正常场景）。
        """
        today = date.today()
        if trade_date == today and datetime.now().time() < CLOSE_READY:
            logger.warning(
                "%s 还没收盘（现在 %s，收盘后 15:05 才算终值），本次跳过："
                "此刻来源给的是上一交易日的收盘值，写成今天就错了一天",
                trade_date,
                datetime.now().strftime("%H:%M"),
            )
            return {taxonomy: 0 for taxonomy in (FUND_FLOW_CONCEPT, FUND_FLOW_INDUSTRY)}
        if trade_date != today:
            logger.warning(
                "%s ≠ 今天（%s）：来源只服务「即时」，采到的其实是最近的快照，"
                "与这个日期未必对得上",
                trade_date,
                today,
            )

        result: dict[str, int] = {}
        for taxonomy in (FUND_FLOW_CONCEPT, FUND_FLOW_INDUSTRY):
            records = self._rows(taxonomy)
            if not records:
                logger.warning("%s %s 取到 0 行，跳过（不清空已有数据）", trade_date, taxonomy)
                result[taxonomy] = 0
                continue
            with session_scope() as session:
                session.execute(
                    delete(SectorFundFlow).where(
                        SectorFundFlow.trade_date == trade_date,
                        SectorFundFlow.taxonomy == taxonomy,
                    )
                )
                for record in records:
                    session.merge(SectorFundFlow(trade_date=trade_date, **record))
            result[taxonomy] = len(records)
            logger.info("%s %s 资金流落库 %d 行", trade_date, taxonomy, len(records))
        return result

    def coverage(self) -> dict:
        """库里的覆盖情况，回补/排查时打印用。"""
        with session_scope() as session:
            rows = session.execute(
                select(
                    SectorFundFlow.taxonomy,
                    func.count(func.distinct(SectorFundFlow.trade_date)),
                    func.min(SectorFundFlow.trade_date),
                    func.max(SectorFundFlow.trade_date),
                    func.count(),
                ).group_by(SectorFundFlow.taxonomy)
            ).all()
        return {
            taxonomy: {
                "days": days,
                "first": first,
                "last": last,
                "rows": count,
            }
            for taxonomy, days, first, last, count in rows
        }
