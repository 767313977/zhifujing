"""API 响应模型。"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------- 指数


class IndexQuote(ApiModel):
    code: str
    name: str | None
    close: float | None
    pct_chg: float | None
    amount: float | None
    up_count: int | None
    down_count: int | None
    limit_up_count: int | None
    limit_down_count: int | None
    # --- 以下由本地算法补上（`services/index_tech.py`，不落库）---
    ma5: float | None = None
    ma20: float | None = None
    above_ma5: bool | None = None
    above_ma20: bool | None = None
    # 今日成交额 / 前 5 日均额。>1.1 放量、<0.9 缩量
    vol_ratio: float | None = None
    # 量价配合标签：放量上涨 / 缩量下跌 / 平量横盘 …
    vol_price: str | None = None


# --------------------------------------------------------------------- 情绪


class SentimentOut(ApiModel):
    trade_date: date
    limit_up_count: int | None
    limit_down_count: int | None
    broken_count: int | None
    seal_rate: float | None
    broken_rate: float | None
    max_consecutive: int | None
    up_count: int | None
    down_count: int | None
    # 全市场口径的涨跌超 5% 家数（只有当日值）
    up5_count: int | None
    down5_count: int | None
    total_amount: float | None
    yesterday_limit_today_avg: float | None


class DivergenceOut(BaseModel):
    """指数与个股广度是否背离（当日）。

    `level` 供前端上色：`weight_pull`（权重拉抬）/ `theme_active`（题材活跃）/
    `aligned`（同向）。判据与阈值写在 `services/index_tech.py` 里，
    `detail` 把依据数字（指数涨跌幅、涨跌家数）带出来，结论不自成黑盒。
    """

    level: str
    title: str
    detail: str


class TurnoverSeries(BaseModel):
    """两市成交额的近期序列，只给复盘页的柱状图用。

    不复用 `SentimentOut` 列表：这里要的是「**截至所选交易日**」的那一段
    （翻到历史日期时不能把之后的日子也画出来），且只要图表用得上的三个数组。
    """

    dates: list[date]
    # 上证指数 + 深证成指 成交额（元）。**缺数的日子必须是 null**，
    # 不能补 0 —— 图上留空隙与「当天真的没成交」是两件事
    amounts: list[float | None]
    # 当日**上证指数**涨跌幅，只用来给柱子定红绿（红=涨、绿=跌），与 amounts 等长。
    # 注意它跟成交额自己涨跌无关：这是 K 线副图的老惯例，让柱子同时说「量价配合」
    pct_chg: list[float | None]


class MarketOverview(BaseModel):
    trade_date: date
    indexes: list[IndexQuote]
    sentiment: SentimentOut | None
    # 背离判断。历史日期没有涨跌家数（数据源只给当日），这里是 null
    divergence: DivergenceOut | None = None
    # 成交额柱状图的尾巴（截至 trade_date），没有情绪数据时为 null
    turnover: TurnoverSeries | None = None


class IndexSeries(BaseModel):
    """一条指数序列。各数组与 IndexHistory.dates 一一对应，缺失为 null。"""

    code: str
    name: str | None
    close: list[float | None]
    pct_chg: list[float | None]
    amount: list[float | None]


class IndexHistory(BaseModel):
    """指数历史序列，按日期升序对齐，直接喂给图表。"""

    dates: list[date]
    series: list[IndexSeries]


# --------------------------------------------------------------- 涨停板三池


class LimitStock(ApiModel):
    code: str
    name: str | None
    pct_chg: float | None
    price: float | None
    amount: float | None
    float_mv: float | None
    total_mv: float | None
    turnover: float | None
    seal_amount: float | None
    first_seal_time: str | None
    last_seal_time: str | None
    open_times: int | None
    consecutive: int | None
    industry: str | None
    # 开盘啦**精选板块**名，来自涨停天梯的落库结果（`stock_concept`）。
    # 与 `industry` 是两个口径：industry 是 iFinD 的同花顺行业（「半导体」），
    # 这个是开盘啦 App 那张天梯的口径（「芯片」）。`stock_concept` 只覆盖涨停股，
    # 非涨停池的票恒为 None —— 不知道就留空，不拿 industry 顶上（混口径比空着更糟）。
    board: str | None = None
    # **涨停原因**，来自同花顺涨停池的 `reason_type`（`limit_reason` 表），
    # 形如「房地产+城市更新+北京国资」。原样展示，不拆分。
    reason: str | None = None


class LadderLevel(BaseModel):
    """涨停梯队的一层。"""

    consecutive: int
    count: int
    stocks: list[LimitStock]


class LimitPoolOut(BaseModel):
    trade_date: date
    pool_type: str
    total: int
    # 梯队分层只对涨停有意义（跌停是「连续跌停」、炸板没有连板数）
    ladder: list[LadderLevel] | None
    stocks: list[LimitStock]


class PromotionLevel(BaseModel):
    """某一档连板的晋级情况。各数组与 PromotionSeries.dates 一一对应。"""

    level: int
    label: str
    # 昨日该档的股票数
    counts: list[int]
    # 其中今日晋级到下一档的数量
    promoted: list[int]
    # 晋级率（百分比），昨日该档无票时为 null
    rates: list[float | None]


class PromotionSeries(BaseModel):
    """连板晋级率序列。打板复盘的核心指标。"""

    dates: list[date]
    levels: list[PromotionLevel]
    overall_counts: list[int]
    overall_promoted: list[int]
    overall_rates: list[float | None]


# ------------------------------------------------------------------ 龙虎榜


class LhbOut(ApiModel):
    trade_date: date
    code: str
    name: str | None
    reason: str
    close: float | None
    pct_chg: float | None
    net_buy: float | None
    buy_amount: float | None
    sell_amount: float | None
    interpretation: str | None


# -------------------------------------------------------------------- 板块


class SectorQuote(BaseModel):
    """板块排行里的一行。精选与行业一套结构，没有的字段留 null。

    ⚠️ 换开盘红之后**只有 4 个字段真的有值**：名称 / 涨跌幅 / 成交额 / 强度。
    净流入、涨跌家数、领涨股、成分股数在它的板块行里没有可反解的对应列，
    一律是 null（见 `sources/kaipanhong.py` 顶部）—— 本就没有的字段不能给 0。
    """

    code: str
    name: str
    taxonomy: str
    # 开盘啦的强度值，也是它 App 里板块榜的排序依据。**量纲不可跨口径比**
    strength: float | None
    pct_chg: float | None
    # 由已落库的历史复利算出，历史不够 N 个交易日时为 null
    pct_chg_5d: float | None
    amount: float | None
    net_inflow: float | None
    up_count: int | None
    down_count: int | None
    member_count: int | None
    leader_name: str | None
    leader_pct_chg: float | None
    # 当日涨停家数。走「个股 → 精选板块」归属聚合，只有精选口径有；
    # 行业为 null（涨停天梯只给精选板块的归属）
    limit_up_count: int | None


class SectorRanking(BaseModel):
    trade_date: date
    taxonomy: str
    total: int
    # 当日无涨跌幅（数据源还没更新）的板块数，用来提示数据不完整
    missing: int
    # 当日取自 iFinD 兜底口径（成分股加权平均）的板块数。同花顺概念指数
    # 当天拿不到，这些行的数字与同花顺官网可能差 0.5~2 个百分点，次日会订正。
    estimated: int
    boards: list[SectorQuote]


class SectorSeries(BaseModel):
    """单个板块的日线序列，用于板块详情走势图。"""

    code: str
    name: str
    taxonomy: str
    dates: list[date]
    pct_chg: list[float | None]
    amount: list[float | None]


class SectorCompareSeries(BaseModel):
    """对比图里的一条线。values 是以 base 为起点复利出来的净值，非真实指数点位。"""

    code: str
    name: str
    taxonomy: str
    values: list[float | None]


class SectorCompare(BaseModel):
    dates: list[date]
    # 归一化基准，各条线都从它起步，所以纵轴只能看相对强弱
    base: float
    series: list[SectorCompareSeries]


class SectorMemberItem(BaseModel):
    code: str
    name: str | None
    close: float | None
    pct_chg: float | None
    amount: float | None
    turnover: float | None
    # 当日连板数；没涨停时为 null（不是 0，0 会被读成「首板」）
    consecutive: int | None


class SectorMembers(BaseModel):
    trade_date: date
    sector_code: str
    sector_name: str
    taxonomy: str
    # 成分股数。开盘红一次给全量，所以就是取到几只
    member_count: int | None
    # 旧字段：iFinD 选股接口有 100 行上限，换开盘红之后不再有，恒为 false。
    # 保留是为了不动前端类型（前端仍拿它决定要不要显示「只显示前 100 只」的提示）
    truncated: bool
    # 取不到成分股时的原因，能取到就是 null。当日必然取不到 —— 开盘红的成分股
    # 接口只服务历史日期，页面要如实说明，不能显示成「该板块没有成分股」
    note: str | None = None
    members: list[SectorMemberItem]


class FundFlowItem(BaseModel):
    """板块资金流里的一行（**开盘啦板块口径**）。

    金额单位是**亿元**（库里 `sector_daily.net_inflow` 存的是元，接口层换算），
    字段名不带单位，所以口径写在这里 —— 免得有人按元去算，差 1e8 倍。

    `in_amount` / `out_amount` 恒为 None：净流入是「成分股主力净流入之和」，
    **没有「流入 / 流出」这个拆法**（见 `jobs/collect_board_flow.py`）。
    """

    name: str
    pct_chg: float | None
    in_amount: float | None
    out_amount: float | None
    net_amount: float | None
    member_count: int | None
    leader_name: str | None
    leader_pct_chg: float | None


class SectorFundFlowOut(BaseModel):
    """板块资金流向（页面上的排行榜 + 条形图）。

    `taxonomy` 与板块页**同一套**（`kph_selected` 精选 / `kph_industry` 行业）——
    2026-09-23 之前这里是同花顺口径，与站内板块不是一套名字，换掉之后
    `name` 可以直接和板块页的板块对上（但仍然只给名字、不给代码：曲线按名字画）。
    """

    trade_date: date
    taxonomy: str
    taxonomy_label: str
    total: int
    # 已按净额降序（净流入在前），前端自己切 top / bottom
    items: list[FundFlowItem]


class FundFlowSeries(BaseModel):
    """一条累计净流入曲线（一个板块）。

    `values` 与 `FundFlowHistoryOut.dates` **等长**，单位同样是亿元，存的是**累计值**
    （从窗口起点起加总），不是当日值 —— 曲线看的就是「这十几天资金净流进/流出多少」。

    三个取值规则（后端算好，前端不要再动）：

    - **起点之前是 `null`**：该板块在窗口前几天还没进过榜（来源每天返回的板块集合会
      有出入），画成 0 就成了「那几天不流入不流出」，是假的。
    - **中间缺的那天顺延**：板块存在但那天没出现在快照里，累计值保持不变（不是归零、
      也不是断线）。
    - 一旦有第一个数据点，后面就不会再出现 `null`。
    """

    name: str
    values: list[float | None]


class FundFlowHistoryOut(BaseModel):
    """板块资金流的**多日累计**曲线（页面下方的折线图）。"""

    taxonomy: str
    taxonomy_label: str
    # 升序（左旧右新），与 series 里每条曲线的 values 一一对应
    dates: list[date]
    # 已按「窗口内累计净额的绝对值」降序 —— 动得最狠的排前面，颜色也就固定了
    series: list[FundFlowSeries]
    # 库里实际有几个交易日。**少于 2 天时前端要提示**：一个点连不成线
    days: int


class FundFlowMatrixCell(BaseModel):
    """资金流矩阵里的一格：某天净流入的第 N 名。

    `net_amount` 单位是**亿元**（与 `FundFlowItem` 同一套），正值是净流入。
    带 `code` 是为了让格子能点进板块详情 —— 与轮动矩阵的 `RotationCell` 一样。
    """

    code: str
    name: str
    net_amount: float
    pct_chg: float | None


class FundFlowMatrixColumn(BaseModel):
    """矩阵的一列 = 一个交易日的净流入前 N 名。"""

    trade_date: date
    cells: list[FundFlowMatrixCell]


class FundFlowMatrixOut(BaseModel):
    """板块资金流的**多日矩阵**：列是交易日（从新到旧）、行是当日的第 N 名。

    与 `/rotation` 那张矩阵是同一套版式（前端两块表上下对齐、同一天落在同一列），
    区别只在排序指标 —— 这里固定按 `net_inflow` **降序**（净流入榜的多日版；
    净流出就是同一张榜的另一头，前端用红绿区分方向即可）。
    """

    # **实际数据日**（可能早于请求日，同 `/fund-flow`）；库里有几天由 columns 长度体现
    trade_date: date
    taxonomy: str
    taxonomy_label: str
    top: int
    columns: list[FundFlowMatrixColumn]


class SectorHeatItem(BaseModel):
    """板块热力里的一格。只带一眼要看的信息，不带全套字段。"""

    code: str
    name: str
    pct_chg: float | None
    amount: float | None
    limit_up_count: int | None


class SectorHeatGroup(BaseModel):
    taxonomy: str
    total: int
    rising: int
    falling: int
    average: float | None
    leaders: list[SectorHeatItem]
    laggards: list[SectorHeatItem]


class SectorHeat(BaseModel):
    """首页「板块热力」：精选与行业各一组强弱概览。"""

    trade_date: date
    # 字段名从 concept 改成 selected：口径换成了开盘红的「精选板块」，
    # 继续叫 concept 会让「概念」这个已经不存在的东西留在接口里
    selected: SectorHeatGroup
    industry: SectorHeatGroup


class RotationCell(BaseModel):
    """轮动矩阵里的一格：某天排名第 N 的板块。

    `value` 是**排序所依据的那个指标**（量能=成交额、强度=涨跌幅），
    `pct_chg` 另外带上，因为按成交额排序时格子里的数字也该按涨跌上色。
    """

    code: str
    name: str
    value: float | None
    pct_chg: float | None


class RotationLeader(BaseModel):
    """某个板块当天的领涨个股（矩阵里「领涨」那一行的一只）。

    ⚠️ 口径是「该板块**当日的涨停股**」，来自开盘红的涨停天梯（`stock_concept`），
    **不是**开盘啦那种「当日涨幅前 5 名」。后者的精确做法只有逐个板块调成分股接口
    （`ZhiShuStockList_W8`），20 列就是 20 次按需请求、首次要约 7 秒 ——
    为一行次级信息不值当。涨停股对短线也更直接，而且是零额外请求（数据已在库里）。
    """

    code: str
    name: str | None
    # 当日连板数。1 = 首板；拿不到（不在涨停池里）时是 null
    consecutive: int | None


class RotationLeaderDay(BaseModel):
    """「领涨」行的一天：某板块在该交易日的涨停股（按连板数 → 封板时间排序）。

    单独一个接口（不在 `/rotation` 里）是因为这一行**跟着选中的板块走**：
    点一次格子就换一次，而矩阵本身不变。塞进 `/rotation` 的话，前端每次点格子都得
    重取整个矩阵，而那个面板一进 loading 就整块变成「加载中…」—— 点一下闪一下。
    """

    trade_date: date
    # 该板块当天没有涨停股就是空列表（页面显示「—」）
    leaders: list[RotationLeader] = []


class RotationColumn(BaseModel):
    """一列 = 一个交易日，格子按指标从高到低排。"""

    trade_date: date
    cells: list[RotationCell]


class SectorRotation(BaseModel):
    """板块轮动矩阵：每列一天、每行当天的第 N 名。

    列是**从新到旧**排的（最新在最左）—— 盯盘时先看当天，再往左回溯。
    """

    taxonomy: str
    metric: str
    metric_label: str
    top: int
    columns: list[RotationColumn]


# ------------------------------------------------------------------ 涨停题材


class LimitThemeItem(BaseModel):
    """一个题材（概念板块）当日聚集了多少只涨停股。"""

    concept: str
    count: int
    pct_chg: float | None


class LimitStockTheme(BaseModel):
    """单只涨停股挂的题材。按板块当日涨跌幅降序，最热的排前面。

    返回**全部**题材，展示时再截断 —— 前端要按这个列表做题材筛选，
    截断了就会出现「标签写着 10 只涨停、点进去只剩 1 只」。
    """

    code: str
    themes: list[str]


class LimitThemes(BaseModel):
    trade_date: date
    # 至少 MIN_CLUSTER 只涨停股共同挂着的题材，按涨停家数降序
    clusters: list[LimitThemeItem]
    stocks: list[LimitStockTheme]


# --------------------------------------------------------------- 数据管理


class CollectStepResult(BaseModel):
    status: str
    rows: int
    cost: float
    message: str | None


class CollectResult(BaseModel):
    trade_date: str
    steps: dict[str, CollectStepResult]


class CollectLogOut(ApiModel):
    trade_date: date | None
    task: str
    status: str
    rows: int | None
    message: str | None
    cost_seconds: float | None
    created_at: datetime


class SchedulerStatus(BaseModel):
    """定时采集任务状态。"""

    enabled: bool
    running: bool
    collect_time: str
    catchup_on_start: bool
    next_run_time: str | None
    last_run: str | None
    last_result: dict | None


class TableCoverage(BaseModel):
    """单张表的覆盖情况。各表能回补的范围不同，必须分别展示。"""

    label: str
    days: int
    latest: date | None


class IfindToolUsage(BaseModel):
    server: str
    tool: str
    calls: int


class IfindQuota(BaseModel):
    """iFinD 调用配额视图。

    额度是**账号级**的 —— 定时采集、形态选股、手工补数共用一个池子，
    所以这个视图不属于任何单一链路，单独给一块。

    计量窗口是**订阅周期**不是自然月：iFinD 后台「计量区间」是
    2026-09-17 ~ 2026-10-17 这种滚动窗口，起止都落在订阅日（见
    IFIND_CYCLE_START_DAY），所以字段一律用 cycle 而不是 month。
    """

    # 当前计量周期的起止日（含）。跨月，所以两个都要给前端
    cycle_start: date
    cycle_end: date
    monthly_quota: int
    cycle_calls: int
    cycle_remaining: int
    today_calls: int
    cycle_trade_days_passed: int
    cycle_trade_days_total: int
    # 计量从哪天开始。晚于周期起点时，cycle_calls 只是本周期**有记录以来**的消耗
    counting_since: date | None
    # 外推实际用到的样本交易日数
    sampled_trade_days: int
    # 按样本期的日均用量外推到整周期。样本不足（<3 个交易日）时为 None
    projected_cycle_calls: int | None
    # 已用比例，用于页面变色与配额守卫
    usage_ratio: float | None
    by_tool: list[IfindToolUsage]


class AdminStatus(BaseModel):
    latest_sentiment_date: date | None
    latest_limit_pool_date: date | None
    latest_lhb_date: date | None
    latest_index_date: date | None
    data_days: int
    coverage: list[TableCoverage]
    scheduler: SchedulerStatus
    recent_logs: list[CollectLogOut]
    ifind_quota: IfindQuota


# -------------------------------------------------------------------- 选股器


class ScreenRunOut(BaseModel):
    """自然语言选股结果。

    列是动态的 —— iFinD 按提问内容决定返回哪些指标，且列名自带日期
    （如 `总市值[20260917]`），所以不能写死表头。
    """

    query: str
    columns: list[str]
    rows: list[dict[str, str]]
    # 匹配总数
    matched: int | None
    # 表格实际给出的行数（上限 100）
    returned: int
    # matched > returned 即为被截断，必须让用户看到
    truncated: bool
    answer: str
    cost_seconds: float


class PresetIn(BaseModel):
    name: str
    query: str


class PresetOut(ApiModel):
    id: int
    name: str
    kind: str
    conditions: dict
    created_at: datetime


# -------------------------------------------------------------------- 自选股


class WatchlistIn(BaseModel):
    code: str
    name: str | None = None
    note: str | None = None


class WatchlistRow(BaseModel):
    """自选股一行。

    行情来自本地缓存的日线（stock_daily），不是实时行情 —— 页面因此
    不需要联网，打开即读。没有缓存的股票这几项为 null。
    """

    code: str
    name: str | None
    tags: list[str] | None
    note: str | None
    added_at: datetime
    latest_date: date | None
    close: float | None
    pct_chg: float | None


# ------------------------------------------------------------------ 个股详情


class StockDailyRow(ApiModel):
    trade_date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    pct_chg: float | None
    volume: float | None
    amount: float | None


class StockDdeRow(ApiModel):
    """个股某一天的 DDE 与主力净流入。

    单位一律**元**（iFinD 原始单位）—— 亿/万的换算放在前端展示层，别在这里先除一遍。
    """

    trade_date: date
    # 主力净流入额
    net_inflow: float | None
    # 「5日DDE」—— 名字是来源自己的窗口口径，不是「当日 DDE」
    dde: float | None


class StockDdeOut(BaseModel):
    code: str
    name: str | None
    # 实际给了多少个交易日：少于请求的 days，说明这只票的历史（或缓存）就这么长
    days: int
    rows: list[StockDdeRow]
    # 取不到数据时的原因；取到了就是 null
    note: str | None = None


class StockProfile(BaseModel):
    code: str
    name: str | None
    in_watchlist: bool
    note: str | None
    latest: StockDailyRow | None
    # 本地缓存的日线覆盖
    day_count: int
    first_date: date | None
    last_date: date | None
    # 该股历史上过涨停池的日期，便于和复盘关联
    limit_up_dates: list[date]
    lhb_count: int


class StockThemeItem(BaseModel):
    """个股挂的一个题材，附带该板块最近一个交易日的表现。

    板块表现取自 `sector_daily` 的最新交易日 —— 个股题材本身不是按日的快照，
    所以只能给「最近一个交易日」的板块涨跌幅，不能声称是某天的。
    """

    concept: str
    # 能对上板块表时给板块代码；对不上（如「沪深300样本股」）为 null
    board_code: str | None
    pct_chg: float | None


class StockThemes(BaseModel):
    code: str
    name: str | None
    # 上面的板块涨跌幅对应的交易日
    board_date: date | None
    themes: list[StockThemeItem]


# ------------------------------------------------------------------ 复盘笔记


class NoteIn(BaseModel):
    market_view: str | None = None
    next_plan: str | None = None


class NoteOut(ApiModel):
    trade_date: date
    market_view: str | None
    next_plan: str | None
    updated_at: datetime | None


# ------------------------------------------------------------------ 形态选股


class PatternMeta(BaseModel):
    """形态清单的一项，给前端做分组筛选。"""

    key: str
    name: str
    group: str


class PatternHitItem(BaseModel):
    """一只票命中的某一个形态。"""

    pattern: str
    pattern_name: str
    group: str
    score: float
    # 突破价 / 支撑价等关键位，用于在 K 线图上画线
    key_levels: dict
    # 平台振幅、放量倍数等明细，用于解释「凭什么说它命中了」
    detail: dict


class PatternStockOut(BaseModel):
    """一只票的形态命中汇总 —— 列表页一行。

    一只票可能同时命中多个形态（如既创 120 日新高又回踩不破），归并成一行、
    多个标签。`score` 取其中最高分，作为排序依据。

    `avg_amount` / `total_mv` 来自**股票池**（`stock_universe`），是这只票的
    **属性**而不是信号当天的快照，所以从池子实时取、不冗余存进 `pattern_hit` ——
    池子外的票（日均成交额跌到门槛以下）这两项为 null。
    `close` / `pct_chg` / `amount` 相反，是逐日快照，必须随命中记录一起存下来。
    """

    code: str
    name: str | None
    trade_date: date
    close: float | None
    pct_chg: float | None
    amount: float | None
    # 近 20 日日均成交额（元），判断「这个突破能不能承接我的仓位」
    avg_amount: float | None
    # 总市值（元），判断「是题材小票还是权重」
    total_mv: float | None
    score: float
    patterns: list[PatternHitItem]


class PatternCount(BaseModel):
    pattern: str
    pattern_name: str
    group: str
    # 命中该形态的股票数
    stocks: int


class PatternSummary(BaseModel):
    """某日的形态命中概况，给首页面板与飞书简报用。"""

    trade_date: date | None
    total_hits: int
    total_stocks: int
    by_pattern: list[PatternCount]


# ------------------------------------------------------------------ 资金面


class MarginSnapshot(BaseModel):
    """单个市场的两融快照，**单位元**。"""

    financing_balance: float | None  # 融资余额
    financing_buy: float | None  # 融资买入额
    securities_balance: float | None  # 融券余额


class FundFlowOverview(BaseModel):
    """资金面总览（当日）。所有金额单位：元。"""

    trade_date: date
    sh: MarginSnapshot | None
    sz: MarginSnapshot | None
    # 两市合计。**两市都有数时才给** —— 深市常比沪市晚一天（实测），
    # 只有一个市场时相加会把「待披露」误报成「归零」
    financing_total: float | None
    financing_buy_total: float | None
    # 与上一交易日相比的融资余额变化（同口径，两边齐全才算）
    financing_change: float | None
    # 沪深股通**成交总额**。⚠️ 官方 2024-08 起不再披露买卖方向，
    # 所以这里没有「净流入」—— 只有活跃度
    hsgt_turnover: float | None
    hsgt_turnover_prev: float | None
    # 龙虎榜机构席位净买额合计（按代码去重后相加）
    institution_net: float | None
    institution_count: int
    # ETF 净申赎估算（份额变化 × 收盘价）。缺上一日数据时为 None
    etf_net_inflow: float | None


class FundsSeries(BaseModel):
    """两融与北向成交额的走势。"""

    dates: list[date]
    financing_balance: list[float | None]
    financing_buy: list[float | None]
    hsgt_turnover: list[float | None]
    # 该日两市数据是否齐全。不齐的日子上面两个序列是 None，
    # 前端据此断开曲线，而不是画成 0
    financing_complete: list[bool]


class EtfFlowItem(BaseModel):
    code: str
    name: str | None
    close: float | None
    pct_chg: float | None
    amount: float | None  # 成交额（元）
    shares: float | None  # 当日份额（份）
    share_delta: float | None  # 份额变化（份），正数 = 净申购
    net_inflow: float | None  # 净申赎估算（元）


class EtfFlowBoard(BaseModel):
    trade_date: date
    prev_date: date | None = None
    has_prev: bool
    items: list[EtfFlowItem]


class EtfIndustryItem(BaseModel):
    """一个行业 / 主题分类下的 ETF 汇总。

    **没有合计份额变化**：份额单位是「份」，不同 ETF 每份净值差几个数量级，
    相加出来的数字没有任何含义。能加的是金额 —— 净申赎与成交额。
    """

    category: str
    fund_count: int
    net_inflow: float | None  # 该分类净申赎合计（元）
    amount: float | None  # 该分类成交额合计（元）
    pct_chg: float | None  # 成交额加权平均涨跌幅
    # 该分类下的 ETF 明细，只给前几只（按净申赎降序），全量交给「按单只」视图
    funds: list[EtfFlowItem]


class EtfIndustryBoard(BaseModel):
    trade_date: date
    prev_date: date | None = None
    has_prev: bool
    items: list[EtfIndustryItem]


class InstitutionItem(BaseModel):
    code: str
    name: str | None
    close: float | None
    pct_chg: float | None
    buy_count: int | None
    sell_count: int | None
    buy_amount: float | None
    sell_amount: float | None
    net_amount: float | None
    reason: str | None


class InstitutionBoard(BaseModel):
    trade_date: date
    items: list[InstitutionItem]
    # 去重后的股票数（原始行数会因「多条上榜原因」更多）
    total: int


class DdeItem(BaseModel):
    code: str
    name: str | None
    # 收盘价与涨跌幅来自日线（`stock_daily`）。它只覆盖「流动性池 + 池外涨停」，
    # 所以池外那部分票这两列是空 —— 空的照实显示「—」，不要用 0 顶替
    close: float | None
    pct_chg: float | None
    # 5日DDE（元）。与个股页那一栏是**同一个数**（同一张表、同一口径，实测一致）
    dde: float | None


class DdeBoard(BaseModel):
    trade_date: date
    items: list[DdeItem]
    # 当天**有 DDE 的票数**，不是全市场只数 —— 来源在收盘后逐步发布，
    # 当天 18 点前后大约只覆盖七成（见设计文档 8.44/8.46）
    total: int
