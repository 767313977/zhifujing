/** 与后端 app/schemas.py 对应的类型。 */

export interface IndexQuote {
  code: string
  name: string | null
  close: number | null
  pct_chg: number | null
  amount: number | null
  up_count: number | null
  down_count: number | null
  limit_up_count: number | null
  limit_down_count: number | null
  /** 5 日 / 20 日均线与「站上还是跌破」，后端按本地日线现算 */
  ma5: number | null
  ma20: number | null
  above_ma5: boolean | null
  above_ma20: boolean | null
  /** 今日成交额 / 前 5 日均额。>1.1 放量、<0.9 缩量 */
  vol_ratio: number | null
  /** 量价配合标签：放量上涨 / 缩量下跌 / 平量横盘 … */
  vol_price: string | null
}

export interface Sentiment {
  trade_date: string
  limit_up_count: number | null
  limit_down_count: number | null
  broken_count: number | null
  seal_rate: number | null
  broken_rate: number | null
  max_consecutive: number | null
  up_count: number | null
  down_count: number | null
  /** 全市场口径的涨跌超 5% 家数（只有当日值） */
  up5_count: number | null
  down5_count: number | null
  total_amount: number | null
  yesterday_limit_today_avg: number | null
}

/** 指数与个股广度是否背离。level 决定配色，detail 是判据里的原始数字 */
export interface Divergence {
  level: 'weight_pull' | 'theme_active' | 'aligned'
  title: string
  detail: string
}

/** 两市成交额的近期序列，给情绪面板的柱状图。三个数组一一对应。 */
export interface TurnoverSeries {
  dates: string[]
  /** 上证指数 + 深证成指 成交额（元）。缺数的日子为 null，图上留空隙而非画成 0 */
  amounts: (number | null)[]
  /** 当日上证指数涨跌幅，只决定柱子的红绿（红=涨、绿=跌），与成交额自身涨跌无关 */
  pct_chg: (number | null)[]
}

export interface MarketOverview {
  trade_date: string
  indexes: IndexQuote[]
  sentiment: Sentiment | null
  /** 历史日期没有涨跌家数（数据源只给当日），这里是 null */
  divergence: Divergence | null
  /** 截至 trade_date 的近 30 个交易日成交额 */
  turnover: TurnoverSeries | null
}

/** 指数历史序列。各数组与 IndexHistory.dates 一一对应，缺失为 null。 */
export interface IndexSeries {
  code: string
  name: string | null
  close: (number | null)[]
  pct_chg: (number | null)[]
  amount: (number | null)[]
}

export interface IndexHistory {
  dates: string[]
  series: IndexSeries[]
}

export type PoolType = 'up' | 'down' | 'broken'

export interface LimitStock {
  code: string
  name: string | null
  pct_chg: number | null
  price: number | null
  amount: number | null
  float_mv: number | null
  total_mv: number | null
  turnover: number | null
  seal_amount: number | null
  first_seal_time: string | null
  last_seal_time: string | null
  open_times: number | null
  consecutive: number | null
  /** iFinD 的**同花顺行业**（半导体）。梯队 chip 上不再显示它，只在 tooltip 里留着。 */
  industry: string | null
  /** **开盘啦精选板块**（芯片），涨停天梯口径 —— 梯队 chip 上显示的是这个。 */
  board: string | null
  /** **涨停原因**（同花顺）：`房地产+城市更新+北京国资`。原样展示，不拆分。 */
  reason: string | null
}

export interface LadderLevel {
  consecutive: number
  count: number
  stocks: LimitStock[]
}

export interface LimitPool {
  trade_date: string
  pool_type: PoolType
  total: number
  /** 梯队分层只对涨停存在 */
  ladder: LadderLevel[] | null
  stocks: LimitStock[]
}

/** 某一档连板的晋级情况。各数组与 PromotionSeries.dates 一一对应。 */
export interface PromotionLevel {
  level: number
  label: string
  /** 昨日该档的股票数 */
  counts: number[]
  /** 其中今日晋级到下一档的数量 */
  promoted: number[]
  /** 晋级率（百分比），昨日该档无票时为 null */
  rates: (number | null)[]
}

export interface PromotionSeries {
  dates: string[]
  levels: PromotionLevel[]
  overall_counts: number[]
  overall_promoted: number[]
  overall_rates: (number | null)[]
}

export interface LhbItem {
  trade_date: string
  code: string
  name: string | null
  reason: string
  close: number | null
  pct_chg: number | null
  net_buy: number | null
  buy_amount: number | null
  sell_amount: number | null
  interpretation: string | null
}

/** 板块分类口径。开盘红的「精选板块」是它自家的分类，不是传统一级行业 */
export type SectorTaxonomy = 'kph_selected' | 'kph_industry'

export interface SectorQuote {
  code: string
  name: string
  taxonomy: string
  /** 开盘啦的强度值（板块榜的排序依据）。量纲不可跨口径比 */
  strength: number | null
  pct_chg: number | null
  pct_chg_5d: number | null
  amount: number | null
  net_inflow: number | null
  up_count: number | null
  down_count: number | null
  member_count: number | null
  leader_name: string | null
  leader_pct_chg: number | null
  /** 当日涨停家数。只有精选板块有（来自涨停天梯，行业口径没有归属，为 null） */
  limit_up_count: number | null
}

export interface SectorRanking {
  trade_date: string
  taxonomy: string
  total: number
  missing: number
  estimated: number
  boards: SectorQuote[]
}

export interface SectorSeries {
  code: string
  name: string
  taxonomy: string
  dates: string[]
  pct_chg: (number | null)[]
  amount: (number | null)[]
}

export interface SectorCompareSeries {
  code: string
  name: string
  taxonomy: string
  /** 以 base 为起点复利出的净值，不是真实指数点位，只能看相对强弱 */
  values: (number | null)[]
}

export interface SectorCompare {
  dates: string[]
  base: number
  series: SectorCompareSeries[]
}

export interface SectorMemberItem {
  code: string
  name: string | null
  close: number | null
  pct_chg: number | null
  amount: number | null
  turnover: number | null
  /** 当日连板数，没涨停为 null */
  consecutive: number | null
}

export interface SectorMembers {
  trade_date: string
  sector_code: string
  sector_name: string
  taxonomy: string
  member_count: number | null
  /** 旧字段：iFinD 的 100 行上限，换开盘红后恒为 false */
  truncated: boolean
  /** 取不到成分股的原因；能取到就是 null。当日必然取不到（数据源只给历史日期） */
  note: string | null
  members: SectorMemberItem[]
}

/** 板块资金流向的口径。**与 SectorTaxonomy 是两套名字**（同花顺概念 vs 开盘红精选） */
export type FundFlowTaxonomy = 'ths_concept' | 'ths_industry'

/** 资金流里的一行。金额单位是**亿元**（后端注释里也写死了这件事） */
export interface FundFlowItem {
  name: string
  pct_chg: number | null
  in_amount: number | null
  out_amount: number | null
  net_amount: number | null
  member_count: number | null
  leader_name: string | null
  leader_pct_chg: number | null
}

export interface SectorFundFlowOut {
  trade_date: string
  taxonomy: string
  taxonomy_label: string
  total: number
  /** 已按净额降序（净流入在前） */
  items: FundFlowItem[]
}

/**
 * 一条累计净流入曲线。`values` 与 `FundFlowHistoryOut.dates` 等长，单位亿元。
 *
 * 值由后端算好：**起点之前是 null**（该板块那时还没进过榜，画 0 是假的）、
 * **中间缺的那天顺延**（累计值不变，不归零也不断线）。前端不要再自己累加。
 */
export interface FundFlowSeries {
  name: string
  values: (number | null)[]
}

export interface FundFlowHistoryOut {
  taxonomy: string
  taxonomy_label: string
  /** 升序（左旧右新） */
  dates: string[]
  /** 已按「窗口内累计净额的绝对值」降序 */
  series: FundFlowSeries[]
  /** 库里实际有几个交易日。**少于 2 天时要提示**：一个点连不成线 */
  days: number
}

export interface SectorHeatItem {
  code: string
  name: string
  pct_chg: number | null
  amount: number | null
  limit_up_count: number | null
}

export interface SectorHeatGroup {
  taxonomy: string
  total: number
  rising: number
  falling: number
  average: number | null
  leaders: SectorHeatItem[]
  laggards: SectorHeatItem[]
}

export interface SectorHeat {
  trade_date: string
  selected: SectorHeatGroup
  industry: SectorHeatGroup
}

/**
 * 轮动矩阵的排序指标。
 *
 * `strength` 是开盘啦的**强度值** —— 它 App 里那张板块榜就是按这个排的，想对齐就得用它；
 * `pct_chg`（涨幅）与它不是一回事，`amount`（成交额）排出来又是另一张榜（芯片永远第一）。
 * 注意强度的量纲**不可跨口径比**：精选极值上万、行业一千出头。
 */
export type RotationMetric = 'strength' | 'pct_chg' | 'amount'

export interface RotationCell {
  code: string
  name: string
  /** 排序所依据的指标值：metric=strength 时是强度、amount 时是成交额（元）、pct_chg 时是涨跌幅（%） */
  value: number | null
  pct_chg: number | null
}

/**
 * 「领涨」行的一只：某个板块当天的涨停股。
 *
 * ⚠️ 口径是**该板块当日的涨停股**（来自开盘红涨停天梯），**不是**「当日涨幅前 5 名」
 * —— 后者要逐个板块调成分股接口，20 列就是 20 次按需请求，为一行次级信息不值当。
 */
export interface RotationLeader {
  code: string
  name: string | null
  /** 当日连板数，1 = 首板；不在涨停池里时为 null */
  consecutive: number | null
}

/** 「领涨」行的一天。这一行跟着**选中的板块**走，所以单独一个接口取，见 `sectorRotationLeaders` */
export interface RotationLeaderDay {
  trade_date: string
  /** 该板块当天没有涨停股就是空数组（页面显示「—」） */
  leaders: RotationLeader[]
}

export interface RotationColumn {
  trade_date: string
  /** 当天的前 N 名，已按指标降序 */
  cells: RotationCell[]
}

export interface SectorRotation {
  taxonomy: string
  metric: RotationMetric
  metric_label: string
  top: number
  /** 一列一个交易日，**从新到旧** */
  columns: RotationColumn[]
}

export interface LimitThemeItem {
  concept: string
  count: number
  pct_chg: number | null
}

export interface LimitStockTheme {
  code: string
  /** 该股所属的开盘红精选板块（当日涨停天梯口径），按板块当日涨跌幅降序。展示时再截断 */
  themes: string[]
}

export interface LimitThemes {
  trade_date: string
  clusters: LimitThemeItem[]
  stocks: LimitStockTheme[]
}

export interface CollectLog {
  trade_date: string | null
  task: string
  status: string
  rows: number | null
  message: string | null
  cost_seconds: number | null
  created_at: string
}

export interface SchedulerStatus {
  enabled: boolean
  running: boolean
  collect_time: string
  catchup_on_start: boolean
  next_run_time: string | null
  last_run: string | null
  last_result: Record<string, unknown> | null
}

/** 单张表的覆盖情况。各表能回补的范围不同，必须分别看。 */
export interface TableCoverage {
  label: string
  days: number
  latest: string | null
}

export interface IfindToolUsage {
  server: string
  tool: string
  calls: number
}

/**
 * iFinD 调用配额。额度是**账号级**的，采集、形态选股、手工补数共用，
 * 所以它不属于任何单一链路，单独一块。
 */
export interface IfindQuota {
  /** 计量周期的起止日（含）。iFinD 按订阅周期滚动计量，不是自然月 */
  cycle_start: string
  cycle_end: string
  monthly_quota: number
  cycle_calls: number
  cycle_remaining: number
  today_calls: number
  cycle_trade_days_passed: number
  cycle_trade_days_total: number
  /** 计量从哪天开始。晚于周期起点时，cycle_calls 只是本周期有记录以来的消耗 */
  counting_since: string | null
  /** 外推实际用到的样本交易日数 */
  sampled_trade_days: number
  /** 按样本期日均外推到整周期，样本不足（<3 个交易日）时为 null */
  projected_cycle_calls: number | null
  usage_ratio: number | null
  by_tool: IfindToolUsage[]
}

export interface PatternMeta {
  key: string
  name: string
  /** 趋势 / 突破 / 量价 / 几何 */
  group: string
}

export interface PatternHitItem {
  pattern: string
  pattern_name: string
  group: string
  score: number
  /** 突破价 / 支撑价等关键位，用于在 K 线图上画线 */
  key_levels: Record<string, number>
  /** 平台振幅、放量倍数等明细，用于解释「凭什么说它命中了」 */
  detail: Record<string, number | string>
}

/**
 * 一只票的形态命中汇总 —— 列表一行。
 * 一只票可能同时命中多个形态，归并成一行多标签，`score` 取其中最高分。
 *
 * `avg_amount` / `total_mv` 来自股票池，是**股票属性**、不是信号当天的快照；
 * 跌出池子的票这两项为 null。
 */
export interface PatternStock {
  code: string
  name: string | null
  trade_date: string
  close: number | null
  pct_chg: number | null
  /** 信号当天的成交额 */
  amount: number | null
  /** 近 20 日日均成交额 */
  avg_amount: number | null
  /** 总市值 */
  total_mv: number | null
  score: number
  patterns: PatternHitItem[]
}

export interface PatternCount {
  pattern: string
  pattern_name: string
  group: string
  stocks: number
}

export interface PatternSummary {
  trade_date: string | null
  total_hits: number
  total_stocks: number
  by_pattern: PatternCount[]
}

export interface AdminStatus {
  latest_sentiment_date: string | null
  latest_limit_pool_date: string | null
  latest_lhb_date: string | null
  latest_index_date: string | null
  data_days: number
  coverage: TableCoverage[]
  scheduler: SchedulerStatus
  recent_logs: CollectLog[]
  ifind_quota: IfindQuota
}

/** 自然语言选股结果。列由 iFinD 按提问内容动态决定，不能写死表头。 */
export interface ScreenRun {
  query: string
  columns: string[]
  rows: Record<string, string>[]
  /** 匹配总数 */
  matched: number | null
  /** 表格实际给出的行数（上限 100） */
  returned: number
  /** matched > returned 即为被截断，必须显式提示 */
  truncated: boolean
  answer: string
  cost_seconds: number
}

export interface Preset {
  id: number
  name: string
  kind: string
  conditions: Record<string, unknown>
  created_at: string
}

export interface WatchlistItem {
  code: string
  name: string | null
  tags: string[] | null
  note: string | null
  added_at: string
}

/** 自选股一行。行情来自本地缓存的日线，没有缓存时为 null。 */
export interface WatchlistRow extends WatchlistItem {
  latest_date: string | null
  close: number | null
  pct_chg: number | null
}

export interface StockDailyRow {
  trade_date: string
  open: number | null
  high: number | null
  low: number | null
  close: number | null
  pct_chg: number | null
  volume: number | null
  amount: number | null
}

/**
 * 个股某一天的 DDE 与主力净流入。
 *
 * 单位一律**元**（iFinD 原始单位），亿/万的换算在展示层做，别在这里先除一遍。
 */
export interface StockDdeRow {
  trade_date: string
  /** 主力净流入额。来源没给那一天时为 null —— 不是 0 */
  net_inflow: number | null
  /** 「5日DDE」。名字是来源自己的窗口口径（5 日），不是「当日 DDE」 */
  dde: number | null
}

/** 个股的 DDE 序列，**升序（左旧右新）**。 */
export interface StockDde {
  code: string
  name: string | null
  /** 实际给了多少个交易日：少于请求的天数，说明这只票的历史（或缓存）就这么长 */
  days: number
  rows: StockDdeRow[]
  /** 取不到数据的原因；取到了就是 null */
  note: string | null
}

/**
 * 后端能聚合的 K 线周期。
 *
 * 周/月是**本地日线重采样**出来的（同一张 `stock_daily`，不额外取数），
 * 所以它们与日线是同一口径（不复权 / 前复权由调用方选）。
 */
export type KPeriod = 'day' | 'week' | 'month'

export interface StockThemeItem {
  concept: string
  /** 能对上板块表时有板块代码；「沪深300样本股」这类为 null */
  board_code: string | null
  /** 板块最近一个交易日的涨跌幅 */
  pct_chg: number | null
}

export interface StockThemes {
  code: string
  name: string | null
  /** 上面的板块涨跌幅对应的交易日 */
  board_date: string | null
  themes: StockThemeItem[]
}

export interface StockProfile {
  code: string
  name: string | null
  in_watchlist: boolean
  note: string | null
  latest: StockDailyRow | null
  /** 本地缓存的日线覆盖 */
  day_count: number
  first_date: string | null
  last_date: string | null
  /** 该股历史上过涨停池的日期 */
  limit_up_dates: string[]
  lhb_count: number
}

export interface ReviewNote {
  trade_date: string
  market_view: string | null
  next_plan: string | null
  updated_at: string | null
}

export interface CollectStepResult {
  status: string
  rows: number
  cost: number
  message: string | null
}

export interface CollectResult {
  trade_date: string
  steps: Record<string, CollectStepResult>
}

/** 单个市场的两融快照。金额单位与下面所有资金面字段一样，统一是「元」。 */
export interface MarginSnapshot {
  /** 融资余额 */
  financing_balance: number | null
  /** 融资买入额 */
  financing_buy: number | null
  /** 融券余额 */
  securities_balance: number | null
}

export interface FundFlowOverview {
  trade_date: string
  sh: MarginSnapshot | null
  sz: MarginSnapshot | null
  /** 两市融资余额合计。深市当日未披露时为 null —— 不是 0 */
  financing_total: number | null
  financing_buy_total: number | null
  /** 与上一交易日的融资余额变化（同口径，两市都齐才算得出来） */
  financing_change: number | null
  /** 沪深股通**成交总额**（不是净流入：官方 2024-08 起已停披露买卖方向） */
  hsgt_turnover: number | null
  hsgt_turnover_prev: number | null
  /** 龙虎榜机构席位净买额合计（按代码去重后相加） */
  institution_net: number | null
  /** 上机构榜的股票数 */
  institution_count: number
  /** ETF 净申赎估算；没有上一交易日数据时为 null */
  etf_net_inflow: number | null
}

/** 资金面走势。数组与 dates 一一对应，缺失为 null（图上必须断开，不能画成 0）。 */
export interface FundsSeries {
  dates: string[]
  financing_balance: (number | null)[]
  financing_buy: (number | null)[]
  hsgt_turnover: (number | null)[]
  /** 该日两市两融数据是否齐全 */
  financing_complete: boolean[]
}

export type EtfFlowOrder = 'inflow' | 'outflow' | 'amount'

export interface EtfFlowItem {
  code: string
  name: string | null
  close: number | null
  pct_chg: number | null
  /** 成交额（元） */
  amount: number | null
  /** 当日份额（份） */
  shares: number | null
  /** 份额变化（份），正数 = 净申购 */
  share_delta: number | null
  /** 净申赎估算（元），正数 = 净申购 */
  net_inflow: number | null
}

export interface EtfFlowBoard {
  trade_date: string
  prev_date: string | null
  /** false = 还没有上一交易日数据（首次采集），此时 items 必然为空 */
  has_prev: boolean
  items: EtfFlowItem[]
}

/** ETF 榜的两个视角：按行业汇总 / 按单只明细 */
export type EtfGroup = 'industry' | 'fund'

export interface EtfIndustryItem {
  /** 分类名。来自后端词典（ETF 名称关键词），没有数据源直接给 ETF 的行业归属 */
  category: string
  fund_count: number
  /** 该分类净申赎合计（元）。份额不可加（不同 ETF 每份净值差几个数量级），所以只加金额 */
  net_inflow: number | null
  /** 该分类成交额合计（元） */
  amount: number | null
  /** 成交额加权平均涨跌幅 */
  pct_chg: number | null
  /** 该分类下的 ETF 明细，后端只给前几只（看全量用「按单只」） */
  funds: EtfFlowItem[]
}

export interface EtfIndustryBoard {
  trade_date: string
  prev_date: string | null
  has_prev: boolean
  items: EtfIndustryItem[]
}

export type InstitutionOrder = 'net' | 'buy' | 'sell'

export interface InstitutionItem {
  code: string
  name: string | null
  close: number | null
  pct_chg: number | null
  /** 买方 / 卖方机构家数 */
  buy_count: number | null
  sell_count: number | null
  buy_amount: number | null
  sell_amount: number | null
  net_amount: number | null
  reason: string | null
}

export interface InstitutionBoard {
  trade_date: string
  items: InstitutionItem[]
  /** 去重后的股票数（原始行数会因「多条上榜原因」更多） */
  total: number
}

export type DdeOrder = 'inflow' | 'outflow'

export interface DdeItem {
  code: string
  name: string | null
  /** 收盘价与涨跌幅来自日线；池外的票没有日线，这两列为 null */
  close: number | null
  pct_chg: number | null
  /** 5日DDE（元）。与个股页那一栏是同一个数（同一张表、同一口径） */
  dde: number | null
}

export interface DdeBoard {
  trade_date: string
  items: DdeItem[]
  /** 当天**有 DDE 的票数**（覆盖度），不是全市场只数 */
  total: number
}
