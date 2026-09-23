"""板块采集（开盘红口径）。

与旧版最大的区别：**只有一个来源、没有优先级之争**。

开盘红的板块排行接口一次给全某个口径当天的所有板块（精选 270 / 行业 104），
传哪天就是哪天 —— 所以「当日采集」与「历史回补」是同一条代码路径，不存在旧版
「当日用 iFinD 兜底、次日由板块指数订正」那套机制，`sector_daily.source` 因此
恒为 `kph`，当年那套 `_writable` 覆盖优先级也一并删掉了。

⚠️ 只有 5 个字段是真值（代码 / 名称 / **强度值** / 涨跌幅 / 成交额）。净流入、涨跌家数、
领涨股、成分股数一律为 None，页面显示 `—` —— 理由见 `sources/kaipanhong.py`
顶部与 `models.SectorDaily` 的说明，一句话是「反解不出来就不填」。
"""

import logging
from datetime import date, timedelta

from sqlalchemy import delete, func, select

from app.config import Settings, get_settings
from app.db import session_scope, upsert, upsert_many
from app.models import SOURCE_KPH, SectorBasic, SectorDaily, SectorMember, TradeCalendar
from app.sources.kaipanhong import TAXONOMIES, KaipanhongSource

logger = logging.getLogger(__name__)

# 回补时每采完多少个交易日打一行进度。一天两个口径要翻十几次页，
# 250 天就是 3500 多次请求，没有进度日志看着像卡死。
PROGRESS_EVERY = 20

# 一次取数拿到的板块数不得少于「已知板块数」的这个比例，否则**整天都不写**。
#
# 这是防「静默截断」的闸门。开盘红历史接口的 `Count` 只报当页条数（50/50/…/20），
# 不能当总数用；万一哪天翻页逻辑出问题、或者接口改了分页语义，拿到的可能正好
# 是 50 个板块 —— 行数看着正常、日期也对、不报错，但另外 220 个板块就这么没了。
# 而本模块又是「先删后插」（见 collect_day），一次残缺的取数会把好数据删掉，
# 所以这道闸门是必需的：宁可整天不写并告警。
MIN_BOARD_RATIO = 0.8

# 回补默认的回看自然日数（约 252 个交易日），与现有板块历史的深度对齐
DEFAULT_BACKFILL_DAYS = 380


class SectorCollector:
    """板块采集器。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.kph = KaipanhongSource(self.settings)

    # ------------------------------------------------------------ 板块清单

    def refresh_sector_list(self, trade_date: date) -> dict[str, int]:
        """把某天的板块清单固化到 `sector_basic`。

        板块增删很慢，所以不进每日采集，只在首次装库、或怀疑板块清单变了时手动跑。
        返回每个口径的板块数。
        """
        counts: dict[str, int] = {}
        for taxonomy in TAXONOMIES:
            boards = self.kph.board_ranking(trade_date, taxonomy)
            rows = [
                {
                    "code": board["sector_code"],
                    "name": board["name"],
                    "taxonomy": taxonomy,
                }
                for board in boards
                if board["sector_code"] and board["name"]
            ]
            with session_scope() as session:
                upsert(session, SectorBasic, rows)
            counts[taxonomy] = len(rows)
            logger.info("板块清单 %s：%d 个", taxonomy, len(rows))
        return counts

    def known_counts(self) -> dict[str, int]:
        """`sector_basic` 里各口径已知的板块数，用作完整性闸门的基准。"""
        with session_scope() as session:
            return {
                taxonomy: count
                for taxonomy, count in session.execute(
                    select(SectorBasic.taxonomy, func.count()).group_by(SectorBasic.taxonomy)
                ).all()
            }

    # ------------------------------------------------------------ 每日行情

    def _kept_net_inflow(self, trade_date: date, taxonomy: str) -> dict[str, float]:
        """即将被整天替换删掉的那些行里，**别人写的** `net_inflow`。

        `net_inflow` 是 `jobs/collect_board_flow.py` 算的（板块成分股 × 逐股主力净流入），
        与本采集器落在**同一张表、同一个主键**上，而本采集器的写入策略是整天替换
        （先 DELETE 再 INSERT）—— 不捞出来就会被删掉，且**当天不会有谁再算一遍**
        （那个 job 排在采集链末尾、一天只跑一次）。
        实测 2026-09-23：手动重采一次板块行情，227 个板块的净流入当场全没了，
        资金流面板变成空白。
        """
        with session_scope() as session:
            rows = session.execute(
                select(SectorDaily.sector_code, SectorDaily.net_inflow).where(
                    SectorDaily.trade_date == trade_date,
                    SectorDaily.taxonomy == taxonomy,
                    SectorDaily.net_inflow.is_not(None),
                )
            ).all()
        return {str(code): float(value) for code, value in rows}

    def collect_day(self, trade_date: date, known: dict[str, int] | None = None) -> int:
        """采集某个交易日的两个口径，返回写入行数。

        写入策略是**整天替换**而不是逐行 upsert：一个交易日的板块清单是一次完整
        快照，先删能保证「上次采到、这次没采到」的板块不会留下幽灵行（比如某个
        概念被开盘红下线了）。代价是必须通过下面的完整性闸门。
        """
        baseline = known if known is not None else self.known_counts()
        written = 0
        for taxonomy in TAXONOMIES:
            boards = self.kph.board_ranking(trade_date, taxonomy)
            expected = baseline.get(taxonomy, 0)
            if expected and len(boards) < expected * MIN_BOARD_RATIO:
                logger.error(
                    "开盘红 %s %s 只取到 %d 个板块（已知 %d 个），疑似翻页被截断，"
                    "整天跳过不写 —— 免得把完整快照换成残缺的",
                    taxonomy,
                    trade_date,
                    len(boards),
                    expected,
                )
                continue
            if not boards:
                logger.warning("开盘红 %s %s 取到 0 个板块，跳过", taxonomy, trade_date)
                continue

            # 别人写的列要先捞出来（整天替换会连它一起删掉，见 `_kept_net_inflow`）。
            # **每一行都要带上这个键**，哪怕值是 None：`upsert_many` 要求一批里各行的列
            # 完全一致（多行 VALUES 不支持各行列不同）
            kept = self._kept_net_inflow(trade_date, taxonomy)
            rows = [
                {
                    "trade_date": trade_date,
                    "sector_code": board["sector_code"],
                    "name": board["name"],
                    "taxonomy": taxonomy,
                    "source": SOURCE_KPH,
                    "strength": board["strength"],
                    "pct_chg": board["pct_chg"],
                    "amount": board["amount"],
                    # 下面这几列开盘红没有可反解的对应列，如实留空；
                    # `net_inflow` 是唯一的例外 —— 那一列不是这里产的
                    "volume": None,
                    "net_inflow": kept.get(str(board["sector_code"])),
                    "up_count": None,
                    "down_count": None,
                    "member_count": None,
                    "leader_name": None,
                    "leader_pct_chg": None,
                }
                for board in boards
            ]
            with session_scope() as session:
                session.execute(
                    delete(SectorDaily).where(
                        SectorDaily.trade_date == trade_date,
                        SectorDaily.taxonomy == taxonomy,
                    )
                )
                written += upsert_many(session, SectorDaily, rows)
        return written

    # ------------------------------------------------------------ 历史回补

    def collect_range(
        self,
        end: date,
        *,
        start: date | None = None,
        days: int = DEFAULT_BACKFILL_DAYS,
    ) -> dict:
        """回补一段区间的每个交易日。

        成本与区间**成正比**（每天两个口径各翻几页），与旧版同花顺「每个板块一次
        调用、与区间无关」完全不同 —— 所以这里默认只回看 380 个自然日（约 252 个
        交易日），不会一口气啃两年。
        """
        since = start or (end - timedelta(days=days))
        with session_scope() as session:
            day_list = list(
                session.scalars(
                    select(TradeCalendar.trade_date)
                    .where(
                        TradeCalendar.trade_date >= since,
                        TradeCalendar.trade_date <= end,
                    )
                    .order_by(TradeCalendar.trade_date)
                )
            )
        if not day_list:
            logger.warning(
                "交易日历里 %s ~ %s 没有任何交易日，回补无从下手（先采一次日历）",
                since,
                end,
            )
            return {"days": 0, "written": 0}

        known = self.known_counts()
        if not any(known.values()):
            logger.info("板块清单为空，先按 %s 刷新一次", end)
            known = self.refresh_sector_list(end)

        written = 0
        for index, day in enumerate(day_list, 1):
            written += self.collect_day(day, known)
            if index % PROGRESS_EVERY == 0:
                logger.info("板块回补进度 %d/%d（最新 %s）", index, len(day_list), day)
        logger.info("板块回补完成：%d 个交易日，%d 行", len(day_list), written)
        return {"days": len(day_list), "written": written}

    # ------------------------------------------------------------ 成分股

    def collect_members(self, trade_date: date, sector_code: str) -> int:
        """抓某板块在某日的成分股，落 `sector_member`。

        ⚠️ 当日的成分股要等盘后更新（实测同一天 21:20 取不到、21:55 才有），
        所以「今天」打开板块时可能拿到 0 行 —— 这不是故障，调用方要如实告诉用户，
        不能显示成「该板块没有成分股」。
        """
        members = self.kph.board_members(sector_code, trade_date)
        if not members:
            return 0
        rows = [
            {
                "trade_date": trade_date,
                "sector_code": sector_code,
                "code": member["code"],
                "name": member["name"],
                "close": member["price"],
                "pct_chg": member["pct_chg"],
                "amount": member["amount"],
                "turnover": member["turnover"],
            }
            for member in members
            if member["code"]
        ]
        with session_scope() as session:
            return upsert_many(session, SectorMember, rows)
