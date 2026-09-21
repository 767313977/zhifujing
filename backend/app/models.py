"""ORM 模型。"""

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    """本地时间（Asia/Shanghai），替代 SQLAlchemy 的 `func.now()`。

    后者在 SQLite 里是 `CURRENT_TIMESTAMP`（UTC），而代码别处用 `datetime.now()`
    （本地 CST），两种口径混进同一批表会让时间戳差 8 小时 —— 采集日志、自选股
    加入时间等展示出来全偏 8 小时。统一成 Python 侧的本地时间，别再用 `func.now()`。
    """
    return datetime.now()


class TradeCalendar(Base):
    """交易日历。iFinD 的历史行情会返回周末行，必须以此表过滤。"""

    __tablename__ = "trade_calendar"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)


class IndexDaily(Base):
    """指数日线 + 市场宽度。"""

    __tablename__ = "index_daily"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(32))
    close: Mapped[float | None] = mapped_column(Float)
    pct_chg: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    up_count: Mapped[int | None] = mapped_column(Integer)
    down_count: Mapped[int | None] = mapped_column(Integer)
    limit_up_count: Mapped[int | None] = mapped_column(Integer)
    limit_down_count: Mapped[int | None] = mapped_column(Integer)


class StockBasic(Base):
    """个股基础信息。仅覆盖自选股与选股结果，不做全市场落库。"""

    __tablename__ = "stock_basic"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(32))
    industry: Mapped[str | None] = mapped_column(String(128))
    total_mv: Mapped[float | None] = mapped_column(Float)
    float_mv: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class StockDaily(Base):
    """个股日线。

    只存**不复权价 + 真实涨跌幅**：形态引擎在内存里按 `pct_chg` 复利出前复权序列，
    库里不放第二份前复权价（两种口径的价格混在一张表里迟早有人拿错）。

    **不存量比**。量比是「当日成交量 / 过去 N 日均量」的派生值，N 取 5 还是 10
    是两个不同的数；形态引擎自己按 5 日算（`patterns._volume_ratio`）。
    存一份进库只会造成两种口径并存。
    """

    __tablename__ = "stock_daily"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(32))
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    pct_chg: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    turnover: Mapped[float | None] = mapped_column(Float)


class LimitPool(Base):
    """涨停 / 跌停 / 炸板池。iFinD 无此数据，唯一来源是 akshare push2ex。"""

    __tablename__ = "limit_pool"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    # up=涨停 down=跌停 broken=炸板
    pool_type: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(32))
    pct_chg: Mapped[float | None] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    float_mv: Mapped[float | None] = mapped_column(Float)
    total_mv: Mapped[float | None] = mapped_column(Float)
    turnover: Mapped[float | None] = mapped_column(Float)
    seal_amount: Mapped[float | None] = mapped_column(Float)
    first_seal_time: Mapped[str | None] = mapped_column(String(16))
    last_seal_time: Mapped[str | None] = mapped_column(String(16))
    open_times: Mapped[int | None] = mapped_column(Integer)
    consecutive: Mapped[int | None] = mapped_column(Integer)
    industry: Mapped[str | None] = mapped_column(String(64))


# sector_daily.source 的取值。见 jobs/collect_sectors.py 的模块说明。
#
# 旧版有 ths_index / ths_summary / ifind 三个来源与一套「谁能覆盖谁」的优先级；
# 换开盘红之后**只剩一个来源**：它的板块排行接口一次给全某个口径当日的所有板块，
# 当日与历史是同一条代码路径，不存在兜底与次日订正，优先级机制也就没有意义了。
SOURCE_KPH = "kph"


class SectorDaily(Base):
    """板块日度行情。

    采用**开盘红**分类口径（`kph_selected` 精选 270 个 / `kph_industry` 行业 104 个）。
    开盘红是开盘啦团队的新版 App，它的「精选板块」是自有分类，命名贴近短线
    （芯片 / 算力 / AI应用 / 机器人概念 / 次新股），见设计文档 8.32。

    ⚠️ 开盘红的板块行只反解出**代码 / 名称 / 涨跌幅 / 成交额**四列，其余列要么
    换个口径就不是同一个含义、要么没有验证手段（详见 `sources/kaipanhong.py`
    顶部说明）。所以 `net_inflow` / `up_count` / `down_count` / `member_count` /
    `leader_name` / `leader_pct_chg` 在切换后是**大片 None**，页面显示 `—` ——
    宁可空着，也不填一个猜的数。
    """

    __tablename__ = "sector_daily"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    sector_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(64))
    # kph_selected / kph_industry
    #
    # ⚠️ 注意开盘红的**行业**代码（881121 半导体）与同花顺行业代码是同一套，而本表
    # 主键是 (trade_date, sector_code)、**不含 taxonomy**。当年并存会互相覆盖，
    # 所以切换时旧口径的行必须整体清掉（见设计文档 8.32.3）。
    taxonomy: Mapped[str | None] = mapped_column(String(16))
    # 目前恒为 kph
    source: Mapped[str | None] = mapped_column(String(16))
    # 开盘啦的**强度值**（合成指标，上万即强势）：它才是开盘啦 App 板块榜的排序依据。
    # 两个口径都有，但**量纲不可跨口径比**（精选极值上万、行业一千出头），
    # 所以板块轮动页按强度排时要跟口径一起看。见 sources/kaipanhong.py 的列说明。
    strength: Mapped[float | None] = mapped_column(Float)
    pct_chg: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    net_inflow: Mapped[float | None] = mapped_column(Float)
    up_count: Mapped[int | None] = mapped_column(Integer)
    down_count: Mapped[int | None] = mapped_column(Integer)
    member_count: Mapped[int | None] = mapped_column(Integer)
    leader_name: Mapped[str | None] = mapped_column(String(32))
    leader_pct_chg: Mapped[float | None] = mapped_column(Float)


class SectorBasic(Base):
    """板块基础信息（名称与代码）。

    开盘红的板块排行榜本身就把代码与名称一起给了，所以本表只是把「某个交易日采集到
    的板块清单」固化下来 —— 板块增删很慢，不必每天刷。
    """

    __tablename__ = "sector_basic"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    # kph_selected / kph_industry
    taxonomy: Mapped[str] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class SectorMember(Base):
    """板块成分股快照。**按需抓取后缓存**，不是每日采集。

    开盘红有专门的成分股接口（`ZhiShuStockList_W8`），**当日要等盘后更新**（实测同
    一天 21:20 取不到、21:55 就有了，不是固定时刻），换来了两个好处：一次 1000 行、
    翻页到底没有行数上限，且带连板与主力净额等标签。原先用 iFinD 选股接口时每次
    只给 100 行，上千只成分股的大板块会被静默截断。

    仍然是「打开哪个板块抓哪个」：270 个板块一次全抓没有必要。
    """

    __tablename__ = "sector_member"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    sector_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(32))
    close: Mapped[float | None] = mapped_column(Float)
    pct_chg: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    turnover: Mapped[float | None] = mapped_column(Float)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class StockConcept(Base):
    """个股所属的开盘红**精选板块**（只针对当日涨停股）。

    这是「题材 × 涨停」联动的桥：涨停池的行业字段是申万口径且截断 4 字，与本站
    板块对不上，所以改成直接按「个股 → 板块」落库。

    来源是开盘红的**涨停天梯**（`GetZhangTingTianTi`），它逐只给出涨停股所属的
    精选板块代码与名称，实时与历史都通。这也是本表只能覆盖涨停股的原因 ——
    非涨停个股没有可用的归属接口（见设计文档 8.32.4）。
    """

    __tablename__ = "stock_concept"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    concept: Mapped[str] = mapped_column(String(64), primary_key=True)


class Lhb(Base):
    """龙虎榜。iFinD 无此数据，唯一来源是 akshare push2ex。"""

    __tablename__ = "lhb"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    reason: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(32))
    close: Mapped[float | None] = mapped_column(Float)
    pct_chg: Mapped[float | None] = mapped_column(Float)
    net_buy: Mapped[float | None] = mapped_column(Float)
    buy_amount: Mapped[float | None] = mapped_column(Float)
    sell_amount: Mapped[float | None] = mapped_column(Float)
    interpretation: Mapped[str | None] = mapped_column(Text)


class MarketSentiment(Base):
    """市场情绪物化快照。情绪曲线要快速读 60 天，实时计算太慢。"""

    __tablename__ = "market_sentiment"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    limit_up_count: Mapped[int | None] = mapped_column(Integer)
    limit_down_count: Mapped[int | None] = mapped_column(Integer)
    broken_count: Mapped[int | None] = mapped_column(Integer)
    seal_rate: Mapped[float | None] = mapped_column(Float)
    broken_rate: Mapped[float | None] = mapped_column(Float)
    max_consecutive: Mapped[int | None] = mapped_column(Integer)
    up_count: Mapped[int | None] = mapped_column(Integer)
    down_count: Mapped[int | None] = mapped_column(Integer)
    # 涨跌超 5% 的家数（全市场口径，走 iFinD 选股的 matched）。
    # 与涨跌家数一样**只有当日值** —— 历史回补时留空，不是 0
    up5_count: Mapped[int | None] = mapped_column(Integer)
    down5_count: Mapped[int | None] = mapped_column(Integer)
    total_amount: Mapped[float | None] = mapped_column(Float)
    # 昨日涨停股今日均涨幅：打板赚钱效应
    yesterday_limit_today_avg: Mapped[float | None] = mapped_column(Float)


class Watchlist(Base):
    """自选股。"""

    __tablename__ = "watchlist"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(32))
    tags: Mapped[list | None] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ReviewNote(Base):
    """每日复盘笔记。"""

    __tablename__ = "review_note"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    market_view: Mapped[str | None] = mapped_column(Text)
    next_plan: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now
    )


class ScreenPreset(Base):
    """保存的选股条件。"""

    __tablename__ = "screen_preset"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    # natural=自然语言（iFinD search_stocks） structured=本地结构化条件
    kind: Mapped[str] = mapped_column(String(16))
    conditions: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ScreenResult(Base):
    """选股结果快照。"""

    __tablename__ = "screen_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    preset_id: Mapped[int | None] = mapped_column(Integer)
    query: Mapped[str | None] = mapped_column(Text)
    code: Mapped[str] = mapped_column(String(16))
    name: Mapped[str | None] = mapped_column(String(32))
    industry: Mapped[str | None] = mapped_column(String(128))
    extra: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class CollectLog(Base):
    """采集日志。"""

    __tablename__ = "collect_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date | None] = mapped_column(Date)
    task: Mapped[str] = mapped_column(String(32))
    # ok / partial / failed
    status: Mapped[str] = mapped_column(String(16))
    rows: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)
    cost_seconds: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class IfindUsage(Base):
    """iFinD 调用次数按天累计。

    iFinD 的权益**按 `tools/call` 次数计费**，账号级每月 5000 次 ——
    定时采集、形态选股、手工补数全都在花同一个额度。没有这张表，
    「这个月还剩多少」根本无从回答，配额守卫也无从谈起。

    按 `(日期, server, tool)` 累加而不是记流水：要知道的是「谁在花」，
    不是「每一笔分别是什么时候花的」。
    """

    __tablename__ = "ifind_usage"

    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    # stock / index / edb ...（对应 ifind.SERVERS 的键）
    server: Mapped[str] = mapped_column(String(32), primary_key=True)
    tool: Mapped[str] = mapped_column(String(64), primary_key=True)
    calls: Mapped[int] = mapped_column(Integer, default=0)


class StockUniverse(Base):
    """形态选股的股票池（当日快照）。

    不是「全部 A 股」，而是**近 20 日日均成交额达到门槛**的那一批 ——
    日成交额几百万的僵尸票出了形态也没法交易，采它们的日线纯属白花 iFinD 配额。

    每天**整表替换**而不是增量维护：池子的语义就是「今天要采哪些票」，
    留一批已经掉队的行只会让采集去拉无意义的股票。建池失败时保留旧池，
    否则当天会因为池子空掉而完全不采。
    """

    __tablename__ = "stock_universe"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(32))
    # 建池时选股接口给的区间日均成交额（元），门槛就是按它过滤的
    avg_amount: Mapped[float | None] = mapped_column(Float)
    total_mv: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class PatternHit(Base):
    """形态命中的个股（一天一个形态一行）。

    只存**命中**的行：全池 3032 只 × 10 个形态每天落 3 万行，其中绝大多数是
    「没命中」，没有任何查询价值。

    `close / pct_chg / amount` 是命中当日的行情快照，冗余存一份 —— 列表页每行都要
    展示这三项，存下来让接口变成单表读取；而且它们记的是**引擎当时看到的那个值**，
    事后补数覆盖了日线也不会让历史命中记录的展示跟着变。

    `key_levels`（突破价/支撑价）与 `detail`（平台振幅、放量倍数…）用 JSON 存：
    不同形态的字段完全不同，拆成列会得到一张几乎全空的大宽表。它们是给人看的
    解释材料（「凭什么说这只突破了」），不需要被查询。
    """

    __tablename__ = "pattern_hit"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    # 形态 key，对应 services.patterns.PATTERNS 里的标识
    pattern: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(32))
    score: Mapped[float] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    pct_chg: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    key_levels: Mapped[dict | None] = mapped_column(JSON)
    detail: Mapped[dict | None] = mapped_column(JSON)


class MarginDaily(Base):
    """两融（融资融券）市场日度数据。来源：iFinD EDB（上交所 / 深交所）。

    按交易所分开存（`market` 区分），因为两所的披露时点不同 —— 实测深交所比
    上交所**晚一天**（09-18 那天上所有数、深市还是空），合并成一行会让
    「今天两融增加了多少」在深市数据到达前后给出两个矛盾的答案。

    单位统一成**亿元**（iFinD 原始单位）。存融券**余额**而不是卖出量，
    因为它与融资余额相加才是「两融余额」这个市场最常看的指标。
    """

    __tablename__ = "margin_daily"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    market: Mapped[str] = mapped_column(String(8), primary_key=True)  # sh / sz
    financing_balance: Mapped[float | None] = mapped_column(Float)  # 融资余额
    financing_buy: Mapped[float | None] = mapped_column(Float)  # 融资买入额
    securities_balance: Mapped[float | None] = mapped_column(Float)  # 融券余额


class HsgtDaily(Base):
    """沪深股通成交金额（「北向资金」现在唯一还公布的口径）。

    ⚠️ **只有成交总额，没有净流入。** 2024-08-19 起交易所不再按日披露买入 /
    卖出金额，改为按季公布持股数量，所以「北向净流入」这个指标**在数据源层面
    已经不存在** —— 实测三方印证：akshare 的净买额字段恒为 0、iFinD 的
    「沪股通:当日买入成交金额」截止在 20240816（停披露那天）、而
    「沪股通:当日成交金额」仍在更新。

    所以这里的数字是**买卖合计**，只反映外资活跃度，**不含方向**。

    单位：百万元（iFinD 原始单位）。
    """

    __tablename__ = "hsgt_daily"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    channel: Mapped[str] = mapped_column(String(8), primary_key=True)  # sh / sz
    turnover: Mapped[float | None] = mapped_column(Float)  # 当日成交金额


class EtfShare(Base):
    """ETF 每日份额与行情快照。来源：akshare（东方财富），**不占 iFinD 配额**。

    为什么要存**份额**而不是只看涨跌：份额变化就是真金白银的申赎 ——
    份额涨是净申购（钱进来）、跌是净赎回（钱出去），比「成交额」更能反映
    资金的真实方向（成交额只说明换手活跃）。

    全市场约 1600 只每天都存，接口一次返回全部，成本是恒定的 1 次请求，
    与只数无关。
    """

    __tablename__ = "etf_share"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(64))
    shares: Mapped[float | None] = mapped_column(Float)  # 最新份额（份）
    close: Mapped[float | None] = mapped_column(Float)
    pct_chg: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)  # 成交额（元）


class EtfCategory(Base):
    """ETF → 行业 / 主题的分类映射。

    分类**不是上游给的字段**，是本地词典从 ETF 名称推出来的
    （见 `services/etf_category.py` —— 没有任何数据源直接提供 ETF 的行业归属）。

    单独一张表而不是塞进 `etf_share`：分类是 ETF 的**属性**，不是某一天的属性。
    词典调整后重跑一次采集，全部历史立刻跟着变；塞进日表就得逐天回填。
    """

    __tablename__ = "etf_category"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32))


class LhbInstitution(Base):
    """龙虎榜**机构席位**统计。来源：akshare「机构买卖每日统计」。

    单独一张表而不是并进 `lhb`：那张表记的是个股**总**买卖额（含游资、散户
    席位），这里只算**机构专用席位**，口径与来源都不同（一个 push2ex、
    一个 datacenter），合并会互相污染。机构净买额是「机构在买」最直接的证据。

    ⚠️ **逐股营业部明细（识别具体游资营业部）做不了**：实测
    `stock_lhb_stock_detail_em` 返回 None（接口已下线），所以这里只有机构汇总，
    没有「哪家营业部买了哪只票」。
    """

    __tablename__ = "lhb_institution"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    # 同一只股票可能因多条上榜原因重复出现（与 `lhb` 表同理），
    # 所以 reason 必须进主键，否则 upsert 会撞 UNIQUE 约束
    reason: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(32))
    close: Mapped[float | None] = mapped_column(Float)
    pct_chg: Mapped[float | None] = mapped_column(Float)
    buy_count: Mapped[int | None] = mapped_column(Integer)  # 买方机构数
    sell_count: Mapped[int | None] = mapped_column(Integer)  # 卖方机构数
    buy_amount: Mapped[float | None] = mapped_column(Float)  # 机构买入总额
    sell_amount: Mapped[float | None] = mapped_column(Float)  # 机构卖出总额
    net_amount: Mapped[float | None] = mapped_column(Float)  # 机构买入净额


Index("ix_stock_daily_code", StockDaily.code)
Index("ix_limit_pool_type", LimitPool.trade_date, LimitPool.pool_type)
Index("ix_pattern_hit_date", PatternHit.trade_date, PatternHit.pattern, PatternHit.score)
Index("ix_lhb_code", Lhb.code)
