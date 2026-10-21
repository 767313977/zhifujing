/** 与后端 app/schemas.py 对应的类型。 */

export interface IndexQuote {
  code: string
  name: string | null
  close: number | null
  pct_chg: number | null
  amount: number | null
  up_count: number | null
  down_count: number | null
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

/**
 * 恐惧贪婪指数（0–100，越高越贪婪）。
 *
 * 分数是后端**读取时现算**的：六项原始指标各自按滚动 `window` 个交易日分位折算，
 * 再等权平均。所以 `scores` 里为 null 的日子是「那天算不出分」（有效子指标不足 3 项），
 * 图上留空隙 —— 别把它当 0 画，0 是「极度恐慌」，结论完全相反。
 */
export interface FearGreedBand {
  /** 配色的稳定标识：extreme_fear / fear / neutral / greed / extreme_greed */
  key: string
  label: string
  lower: number
  upper: number
}

/** 六项子指标之一：原始值 + 它在窗口里的分位分。`score` 为 null 时该项当天无数据。 */
export interface FearGreedSub {
  key: string
  label: string
  value: number | null
  score: number | null
  /** 数值单位（`%` / `倍` / 空串），跟在数字后面 */
  unit: string
  /** 一句话口径说明，放 tooltip */
  hint: string
}

export interface FearGreedLatest {
  trade_date: string
  score: number | null
  level: string | null
  /** 较上一个有效分数的变化量；没有上一个时为 null */
  change: number | null
  subs: FearGreedSub[]
}

/** 首页用的一小块读数。与 `FearGreedLatest` 同源，但不带六项明细。 */
export interface FearGreedBrief {
  trade_date: string
  score: number | null
  level: string | null
  change: number | null
  /** 五档阈值（后端给），首页据此画与情绪周期页完全一致的色带 */
  bands: FearGreedBand[]
}

/**
 * 叠在恐贪曲线上做对照的基准指数收盘点位。
 *
 * `close` 与 `FearGreed.dates` **严格等长**（后端已按序号对齐，缺日留 null），
 * 前端直接按下标取用即可 —— 不要按日期 join，两边窗口差一天就会整体错位。
 */
export interface FearGreedBenchmark {
  code: string
  name: string
  close: (number | null)[]
}

export interface FearGreed {
  dates: string[]
  scores: (number | null)[]
  /** 分档阈值由后端给（唯一出处），前端不自己写 25/45/55/75 */
  bands: FearGreedBand[]
  /** 分位窗口（交易日） */
  window: number
  /** 表内总天数：说明读数背后有多少历史 */
  coverage: number
  latest: FearGreedLatest | null
  /** 叠加的基准指数（上证指数）收盘点位；库里该区间没有指数数据时为 null */
  benchmark: FearGreedBenchmark | null
}

/**
 * 市场宽度温度计（`/market/breadth`）。
 *
 * 各数组与 `dates` **严格等长**，缺数的日子是 null：
 * - `above_ma250` 在站内 K 线历史还不够 250 个交易日时**整列为 null**
 *   （不满 N 根 K 线的票不进分母，后端口径如此），图上断线展示、别补 0；
 * - 新高新低是**家数**（整数），不是占比。
 */
export interface BreadthLatest {
  trade_date: string
  above_ma20: number | null
  above_ma60: number | null
  above_ma250: number | null
  high60: number | null
  low60: number | null
  high250: number | null
  low250: number | null
  /** 当日参与统计的有效票数（占比的分母量级） */
  universe_count: number | null
}

export interface Breadth {
  dates: string[]
  /** 站上 MA20 / MA60 / MA250（年线）的个股占比，百分数 */
  above_ma20: (number | null)[]
  above_ma60: (number | null)[]
  above_ma250: (number | null)[]
  /** 创 60 / 250 日收盘新高、新低的家数 */
  high60: (number | null)[]
  low60: (number | null)[]
  high250: (number | null)[]
  low250: (number | null)[]
  /** 表内总天数：说明这条曲线背后有多少历史 */
  coverage: number
  latest: BreadthLatest | null
}

/**
 * 短线接力温度（`/market/relay-temp`，0-100，越高越热）。
 *
 * 与恐贪同构但**没有 `window`**：六项子指标按固定阈值折算（封板率直接用、
 * 连板高度 8 板封顶、昨涨停溢价 [-5%,+5%] 映射等），不看历史分位。
 * `scores` 里为 null 的日子是「有效子指标不足 3 项」，图上留空隙。
 */
export interface RelayTempBand {
  /** 配色的稳定标识：frozen / cold / mild / hot / boiling */
  key: string
  label: string
  lower: number
  upper: number
}

export interface RelayTempSub {
  key: string
  label: string
  value: number | null
  score: number | null
  unit: string
  hint: string
}

export interface RelayTempLatest {
  trade_date: string
  score: number | null
  level: string | null
  change: number | null
  subs: RelayTempSub[]
}

export interface RelayTemp {
  dates: string[]
  scores: (number | null)[]
  bands: RelayTempBand[]
  /** 情绪表总天数（曲线背后有多少历史） */
  coverage: number
  latest: RelayTempLatest | null
}

/**
 * 期指年化基差（`/market/futures-basis`）：IF / IH / IC / IM 四品种各一条年化线。
 *
 * 各序列数组与 `dates` **严格等长**，缺数的日子是 null（期现有一侧缺价的交易日
 * 整天没采到），图上断线展示、别补 0 —— 0% 是「期现完全平水」，是个真实结论。
 */
export interface FuturesBasisLatest {
  trade_date: string
  code: string
  name: string | null
  /** (期货 − 现货) / 现货 × 100（%）。负值 = 贴水（对冲压力） */
  basis_pct: number | null
  /** 年化基差（%）：basis × 365 / 距交割日天数；距交割不足 8 天按下月合约算 */
  annual_pct: number | null
  days_to_expiry: number | null
}

export interface FuturesBasisSeries {
  code: string
  name: string | null
  annual_pct: (number | null)[]
  basis_pct: (number | null)[]
}

export interface FuturesBasis {
  dates: string[]
  series: FuturesBasisSeries[]
  /** 表内总天数（按交易日去重）：曲线背后有多少历史 */
  coverage: number
  latest: FuturesBasisLatest[]
}

/**
 * ERP 股债性价比（`/market/erp`）：沪深300 盈利收益率 − 10Y 国债收益率（百分点）。
 *
 * **ERP 越高 = 股票相对债券越便宜**。分位是后端读取时现算的滚动 250 交易日
 * 中位秩分位（与恐贪同一套），0–100。各数组与 `dates` 严格等长。
 */
export interface ErpLatest {
  trade_date: string
  erp: number | null
  percentile: number | null
  pe_ttm: number | null
  bond_yield: number | null
}

export interface Erp {
  dates: string[]
  erp: (number | null)[]
  percentile: (number | null)[]
  pe_ttm: (number | null)[]
  bond_yield: (number | null)[]
  /** 分位窗口（交易日） */
  window: number
  /** 表内总天数：曲线背后有多少历史 */
  coverage: number
  latest: ErpLatest | null
}

/**
 * NH-NL 新高新低扩散（`/market/nh-nl`）：创 60/250 日收盘新高家数 − 新低家数。
 *
 * 原始值与宽度温度计同一份（`breadth_daily`），diff 与累计净值由后端读取时现算。
 * `cum*` 是 diff 的逐日累计（类 AD 线）—— 看顶底背离用：指数新高而净值线不新高
 * = 顶背离。250 日口径在站内 K 线历史不足时**整列为 null**（图上断线，不是 0）。
 */
export interface NhNlLatest {
  trade_date: string
  high60: number | null
  low60: number | null
  /** 新高 − 新低。任一侧缺数为 null（0 是「打平」，不拿来填空） */
  diff60: number | null
  high250: number | null
  low250: number | null
  diff250: number | null
}

export interface NhNl {
  dates: string[]
  diff60: (number | null)[]
  diff250: (number | null)[]
  /** diff 的逐日累计（从 0 起）；diff 缺数的日子为 null，累加器保持不动 */
  cum60: (number | null)[]
  cum250: (number | null)[]
  /** 表内总天数：曲线背后有多少历史 */
  coverage: number
  latest: NhNlLatest | null
  /** 叠加的基准指数（上证指数）收盘点位，看背离用 */
  benchmark: FearGreedBenchmark | null
}

/**
 * 量能潮 OBV（`/market/obv`）：上涨日累加成交额、下跌日扣减的累计净额（元）。
 *
 * 基准指数是上证指数（与恐贪基准同源），OBV 与成交额分位由后端读取时现算。
 * 用法：OBV 与指数点位的背离 —— 指数新高而 OBV 不新高 = 量能跟不上。
 */
export interface ObvLatest {
  trade_date: string
  /** 当日成交额（元） */
  amount: number | null
  /** 当日 OBV 变动额（元，带正负；平盘为 0） */
  obv_change: number | null
  /** OBV 累计净额（元，从 0 起累加） */
  obv: number | null
  /** 成交额在滚动 250 个交易日里的分位（0-100） */
  percentile: number | null
}

export interface Obv {
  dates: string[]
  obv: (number | null)[]
  /** 当日变动额（元，带正负；平盘为 0，成交额缺失为 null） */
  obv_change: (number | null)[]
  amount: (number | null)[]
  percentile: (number | null)[]
  /** 分位窗口（交易日） */
  window: number
  /** 表内总天数：曲线背后有多少历史 */
  coverage: number
  latest: ObvLatest | null
}

/** ETF 净申赎：最近一个可算日的读数（页面顶部 chips）。金额单位一律是元 */
export interface EtfNetFlowLatest {
  trade_date: string
  /** 当日净申赎（元，带正负；净赎回为负） */
  net: number | null
  /**
   * 这笔净额跨了几个交易日：正常相邻两天为 1；> 1 说明中间有覆盖不足的日子
   * （跨境/QDII 的滞后快照日），净额是「自上一个覆盖达标日以来」的累计
   */
  span: number | null
  /** 窗口内逐日净申赎的累计（元，从窗口首日起算） */
  cumulative: number | null
  /** 近 5 个有数的交易日累计净申赎（元） */
  sum5: number | null
  /** 近 20 个有数的交易日累计净申赎（元） */
  sum20: number | null
}

/**
 * ETF 净申赎序列。`Σ(份额变化 × 当日收盘价)`，货币现金类整类剔除。
 *
 * **净额只在两个「覆盖达标」的交易日之间算**：快照日期来自行情源的
 * 「最新-交易日」，而它不是全市场统一的一天（非交易日货基仍给自然日快照、
 * 跨境/QDII 常年滞后一天）。覆盖不足的日子为 null，图上断开而不是画成 0。
 */
export interface EtfNetFlow {
  dates: string[]
  /** 单日净申赎（元，带正负） */
  net: (number | null)[]
  /** 每一天的净额所跨越的交易日数（1 = 相邻两天） */
  span: (number | null)[]
  /** 窗口内逐日累计（元，从 0 起；缺数的日子为 null，累加器保持不动） */
  cumulative: (number | null)[]
  /** 「覆盖达标」的行数下限，口径说明里要写出来 */
  min_rows: number
  /** 表内覆盖达标的交易日数（不是窗口长度） */
  coverage: number
  latest: EtfNetFlowLatest | null
  /** 上证指数收盘点位（与 dates 等长），叠图看资金与点位的背离 */
  benchmark: FearGreedBenchmark | null
}

/** 两融杠杆温度的最新读数。金额单位为亿元 */
export interface MarginTempLatest {
  trade_date: string
  /** 沪 + 深合计两融余额（亿元） */
  balance: number | null
  /** 较前一交易日的增减（亿元，带正负） */
  balance_chg: number | null
  /** 20 日变化率（%） */
  chg20: number | null
  /** 杠杆温度：20 日变化率在滚动窗口里的中位秩（0-100） */
  temperature: number | null
}

/** 两融杠杆温度：余额走势 + 20 日变化率 + 分位温度。各数组与 dates 等长 */
export interface MarginTemp {
  dates: string[]
  /** 沪深合计两融余额（亿元） */
  balance: (number | null)[]
  /** 20 日变化率（%） */
  chg20: (number | null)[]
  /** 温度：chg20 在滚动 window 个观测窗口里的中位秩（0-100） */
  temperature: (number | null)[]
  /** 温度的分位窗口（交易日） */
  window: number
  /** 变化率回溯的观测数 */
  chg_days: number
  /** 表内可用交易日数（不是窗口长度） */
  coverage: number
  latest: MarginTempLatest | null
}

/** 一个一级行业的宽度（当日横截面） */
export interface IndustryBreadthItem {
  industry: string
  /** 该行业当日有效家数（有收盘价且 K 线数 ≥ ma_window） */
  total: number
  /** 站上均线的家数 */
  above: number
  /** 站上占比（%） */
  ratio: number | null
}

/** 行业宽度：各一级行业「站上 MA20」的个股占比。items 按占比降序 */
export interface IndustryBreadth {
  trade_date: string
  ma_window: number
  items: IndustryBreadthItem[]
  /** 全市场站上占比（各行业合计口径） */
  overall: number | null
  /** 各行业占比的中位数 */
  median: number | null
  /** 纳入统计的个股数 */
  coverage: number
}

/** 北向活跃度的最新读数。⚠️ 只有成交额、没有净流入（交易所 2024-08 起停披露） */
export interface NorthboundLatest {
  trade_date: string
  /** 沪深股通成交额（亿元） */
  turnover: number | null
  /** 占两市成交额（%） */
  ratio: number | null
  /** 近 5 个交易日占比均值（%） */
  ratio_ma5: number | null
  /** 近 20 个交易日占比均值（%） */
  ratio_ma20: number | null
}

/** 北向活跃度：沪深股通成交额占两市成交额的比例（%）。各数组与 dates 等长 */
export interface Northbound {
  dates: string[]
  /** 北向成交额 ÷ 两市成交额（%） */
  ratio: (number | null)[]
  /** 沪深股通成交额（亿元） */
  turnover: (number | null)[]
  /** 有北向数据的交易日数（不是窗口长度） */
  coverage: number
  latest: NorthboundLatest | null
  /** 上证指数收盘点位（与 dates 等长），叠图看参与度与点位的背离 */
  benchmark: FearGreedBenchmark | null
}

/** 最新一天一个品种的波动率读数 */
export interface VolatilityLatest {
  trade_date: string
  code: string
  name: string | null
  /** QVIX 收盘 = 隐含波动率（%） */
  close: number | null
  /** 较前一交易日的涨跌幅（%） */
  chg: number | null
  /** 在滚动 window 个交易日里的分位（0-100） */
  percentile: number | null
}

/** 一个品种的 QVIX 序列。数组与 Volatility.dates 等长 */
export interface VolatilitySeries {
  code: string
  name: string | null
  close: (number | null)[]
}

/**
 * 中国波指（QVIX）：50ETF / 300ETF 期权隐含波动率（%）。
 * ⚠️ 不是交易所官方 iVX（2018 年停更），由期权论坛合成。**与指数反向**
 */
export interface Volatility {
  dates: string[]
  series: VolatilitySeries[]
  /** 分位窗口（交易日） */
  window: number
  coverage: number
  latest: VolatilityLatest[]
  /** 上证指数收盘点位（与 dates 等长），叠图看「指数跌 → 波动率升」 */
  benchmark: FearGreedBenchmark | null
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
  /**
   * 恐惧贪婪指数的当日简报：后端**截到 trade_date** 算（翻历史日期不会泄露未来）。
   * 库里还没这天的数据、或那天算不出分时是 null
   */
  fear_greed: FearGreedBrief | null
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
  /**
   * 「几天几板」：最近一段的**交易日跨度**与**涨停次数**（`5天3板` → 5 / 3）。
   * 只在涨停次数 ≥ 2 时给（首板由 `consecutive` 表达）；只有涨停池会填。
   */
  board_days: number | null
  board_count: number | null
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

/**
 * 涨停股票池里的一只票 —— 在 `LimitStock` 上多一个 `status`。
 *
 * 行内会**同时**出现今日涨停（晋级）与今日炸板（失败）的票，这是这个池子的价值所在；
 * `status` 就是用来一眼区分两者的。炸板 / 未晋级的票往往没有题材数据
 * （`reason` / `board` / `seal_amount` 等多为 null），展示时留空即可，别拿别的口径顶上。
 */
export interface LadderBoardStock extends LimitStock {
  /** up=今日涨停（晋级）｜broken=今日炸板（失败）｜other=今日两者都不是 */
  status: 'up' | 'broken' | 'other'
}

/** 按**昨日**连板档分组的一行：这一档昨天有几家、今天晋级几家。 */
export interface LadderBoardRow {
  /** 昨日连板档；0 = 昨日非涨停（首板行） */
  level: number
  /** 展示用标签（「8进9」「首板」） */
  label: string
  /** 候选数（昨日该档全部家数） */
  base: number
  /** 其中今日晋级数 */
  promoted: number
  /** 晋级率（百分数）；base 为 0 时是 null */
  rate: number | null
  stocks: LadderBoardStock[]
}

/**
 * 涨停股票池（`/api/limit/ladder-board`）：按昨日连板档分组，看每一档今天的成败。
 *
 * `prev_date` 是对比基准日（上一交易日），为 null 说明**没有昨日涨停池**，晋级率无从算起。
 */
export interface LadderBoard {
  trade_date: string
  prev_date: string | null
  rows: LadderBoardRow[]
}

// ---------------------------------------------------------------- 竞价强度（集合竞价）

/**
 * 一只票的集合竞价快照（`/api/auction`）。**金额单位一律「元」**。
 *
 * 数据来自腾讯批量行情、交易日 9:15–9:26 逐分钟采样（后端 `jobs/collect_auction.py`）。
 * 取不到的字段是 null，显示「—」，不补 0。参考图里还有 净额 / 承接强度 / 北向 /
 * 人气排名 几列，那些**数据源层面拿不到**（北向净流入已停披露、承接强度是外部工具专有），
 * 本站在界面与接口上都不提供，别拿别的口径顶上。
 */
export interface AuctionRow {
  code: string
  name: string | null
  /** 竞价成交价（= 当日开盘价） */
  price: number | null
  pre_close: number | null
  /** 竞价涨幅（%） */
  pct_chg: number | null
  /**
   * 今开。⚠️ 与 `price` **恒等**（A 股开盘价 = 集合竞价成交价），所以接口**不给**
   * 「开盘涨幅」——那一项与 `pct_chg` 恒等，是重复列（2026-10-12 撤）。
   */
  open: number | null
  /** 竞价成交额（元） */
  amount: number | null
  /** 竞价成交量（股） */
  volume: number | null
  /** 竞价换手率（%） */
  turnover: number | null
  /** 竞价量比 */
  volume_ratio: number | null
  float_mv: number | null
  total_mv: number | null
  limit_up: number | null
  limit_down: number | null
  /** 昨日连板数（当日 9:25 时今日的池还没有）。非涨停股为 null */
  consecutive: number | null
  /**
   * **竞价量比（自算）**：当日竞价成交量 ÷ 前 5 个交易日的竞价成交量均值
   * （口径同同花顺）。采样历史不足 3 天时为 null —— 我们的采样从 2026-10-12 才开始。
   */
  auction_ratio: number | null
  /** **竞价价格波动率（%）** = 9:15–9:26 竞价最高价与最低价之差 ÷ 昨收 */
  amplitude: number | null
  /** **自由流通市值（元）** = 自由流通股 × 竞价价 */
  free_float_mv: number | null
  /** **真实竞价换手（%）** = 竞价量 ÷ 自由流通股（分母剔除不动筹码，比普通换手率更真） */
  free_turnover: number | null
  /** **竞价成交额占比（%）** = 竞价额 ÷ 昨日全天成交额 */
  amount_ratio: number | null
  /** **竞价涨幅排名**：全市场位次（1 起、并列同名次） */
  rank: number | null
  /** **板块内（同花顺行业）竞价涨幅排名** */
  industry_rank: number | null
  /** **昨日是否炸板**（与 `consecutive` 互斥） */
  prev_broken: boolean
  /** **昨日全天换手率（%）** */
  prev_turnover: number | null
  /**
   * **未匹配量差（委差，单位「股」）** = 委买总量 − 委卖总量（竞价终值）。正 = 未匹配的买盘更多。
   * ⚠️ 腾讯竞价时段是否填这个字段尚未核实，可能恒为空。
   */
  committee_diff: number | null
  /** **9:20 撤单率（%）**：(9:20 挂单量 − 9:21 的) ÷ 9:20 的。负值 = 挂单还在增加 */
  cancel_rate: number | null
  /** **封成比（%）** = 涨停价上的封单额 ÷ 自由流通市值。只在竞价就封在涨停价上时有值 */
  seal_ratio: number | null
  /**
   * **开盘后涨幅（%）** = 9:31 现价 ÷ 昨收 − 1 —— 看高开之后是站住了还是被砸。
   * ⚠️ 由 9:31 的补采帧提供，该帧上线之后的日子才有值（之前为空）。
   */
  post_open_pct: number | null
  /** **自由流通盘占比（%）** = 自由流通市值 ÷ 总市值。越低越容易被资金控盘 */
  free_float_ratio: number | null
}

/**
 * 竞价强度的**市场级**概览（页面顶部那条情绪带）。
 * 与主表不同：口径**不受筛选 / 截断影响**，一律按全市场算。
 */
export interface AuctionSummary {
  trade_date: string
  /** 上一交易日（按交易日历） */
  prev_trade_date: string | null
  /** 昨日涨停家数 */
  prev_limit_count: number
  /**
   * **短线情绪温度计（%）**：昨日所有涨停股今日的平均竞价涨幅。
   * >2% 情绪亢奋（高开胜率与溢价都高）；<0 退潮期（个股竞价再好看也易炸板）。
   */
  prev_limit_avg_pct: number | null
  /** 昨日连板梯队：1 板家数 */
  prev_board_1: number
  /** 昨日 2 板家数 */
  prev_board_2: number
  /** 昨日 3 板及以上家数 */
  prev_board_3plus: number
  /** 昨日最高连板数 */
  prev_max_board: number
  /** 全市场竞价上涨家数 */
  up_count: number
  /** 全市场竞价下跌家数 */
  down_count: number
  /** 全市场竞价平盘家数 */
  flat_count: number
  /** **竞价即涨停**家数（竞价价 ≥ 涨停价） */
  limit_count: number
}

export interface AuctionList {
  trade_date: string
  /** 当日有竞价数据的全市场只数（不受筛选 / 截断影响） */
  total: number
  /** 本次实际返回的行数 */
  shown: number
  rows: AuctionRow[]
  /** 市场级概览（情绪带）。当日没有竞价数据时为 null */
  summary: AuctionSummary | null
}

/**
 * 一个**同花顺行业（最后一级）**的竞价强度汇总（`/api/auction/boards`）。
 *
 * 用于判断「是个股独立行情还是板块共振」—— 个股高开 + 板块整体高开才有持续性。
 * ⚠️ 与页面上的「开盘红板块」不是一套分类：那个只覆盖涨停股，没法给全市场分组。
 */
export interface AuctionBoardItem {
  industry: string
  /** 该行业只数 */
  count: number
  /** 平均竞价涨幅（%） */
  avg_pct: number | null
  /** 竞价成交额合计（元） */
  amount: number | null
  /** **竞价即涨停**的家数（竞价价 ≥ 涨停价，≠ 收盘涨停） */
  limit_count: number
}

export interface AuctionBoards {
  trade_date: string
  items: AuctionBoardItem[]
}

export type AuctionSort =
  | 'amount'
  | 'pct'
  | 'ratio'
  | 'turnover'
  | 'float_mv'
  | 'price'
  // 派生列：库里没有、要现算。按它们排序时服务端会把**当日全部**票算出来再排
  | 'rank'
  | 'industry_rank'
  | 'amplitude'
  | 'free_turnover'
  | 'amount_ratio'
  | 'seal_ratio'
  | 'cancel_rate'
  | 'committee_diff'
  | 'auction_ratio'
  | 'prev_boards'
  | 'post_open_pct'
  | 'free_float_ratio'

/** 竞价强度主表的查询参数（对应页面顶部那排筛选）。区间都是**含端点**的。 */
export interface AuctionQuery {
  date?: string | null
  sort?: AuctionSort
  order?: 'asc' | 'desc'
  minPct?: number
  maxPct?: number
  /** 竞价金额（元） */
  minAmount?: number
  maxAmount?: number
  minRatio?: number
  maxRatio?: number
  minTurnover?: number
  maxTurnover?: number
  /** 昨日连板数下限（几板） */
  minBoards?: number
  /** 只看该同花顺行业（**末级名**，如「出版」）—— 板块面板点「只数」筛进来时用 */
  industry?: string
  limit?: number
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

/**
 * 个股异动图谱里的一行（对应后端 `AnomalyRowOut`）。
 *
 * `sector` 是**板块分组键**（同花顺行业末段，如「集成电路」），`industry` 是完整
 * 三级路径（`电子-半导体-集成电路`），页面按前者分组、后者放 tooltip。
 * `cap` 单位是**亿元**（后端已换算），不是元。
 */
export interface AnomalyRow {
  sector: string
  /** 交易日 YYYY-MM-DD */
  date: string
  /** 封板时间 HH:MM，或「收盘」（中大阳线） */
  time: string
  code: string
  name: string
  /** 涨停 / 涨停炸板 / 中大阳线异动 */
  type: string
  /** 首板 / N连板；炸板与中大阳线为空串 */
  board: string
  pct: number | null
  /** 总市值（亿元，1 位小数） */
  cap: number | null
  industry: string | null
  reason: string | null
  /** 同批异动标签（同一天同一板块的全部行，含自己） */
  cohort: string[]
  /** 所在 (交易日, 板块) 是不是窗口内该板块的题材启动日 */
  theme_start: boolean
}

export interface AnomalyMap {
  start: string
  end: string
  days: number
  generated_at: string
  /** 过滤口径文案，原样展示 */
  filter: string
  source: string
  /** 板块下拉选项（已按命中条数降序） */
  sectors: string[]
  theme_start_days: number
  rows: AnomalyRow[]
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

/**
 * 资金流里的一行。金额单位是**亿元**（后端注释里也写死了这件事）。
 *
 * `in_amount` / `out_amount` 恒为 null：开盘啦口径下净流入是「成分股主力净流入之和」，
 * 没有「流入 / 流出」这个拆法，所以面板上那两行不显示（不是「数据缺失」）。
 */
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
  /**
   * 成分股多于这个数的板块已被后端剔掉（业绩 / 地域 / 事件类另有一份关键词名单）。
   * 阈值是后端配置项，所以随响应回传 —— 前端硬写一个数字会在改配置后撒谎。
   */
  excluded_members_over: number
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

/**
 * 资金流矩阵里的一格：某天净流入的第 N 名。`net_amount` 单位亿元（正数是净流入）。
 *
 * 带上 `code` 是为了能点进板块详情 —— 与轮动矩阵的格子同一个行为。
 */
export interface FundFlowMatrixCell {
  code: string
  name: string
  net_amount: number
  pct_chg: number | null
}

/** 矩阵的一列 = 一个交易日的净流入前 N 名 */
export interface FundFlowMatrixColumn {
  trade_date: string
  cells: FundFlowMatrixCell[]
}

/**
 * 板块资金流的**多日矩阵**：列是交易日（从新到旧）、行是当日的第 N 名。
 *
 * 与 `/sectors/rotation` 那张矩阵同一套版式（两块表上下对齐、同一天同一列），
 * 排序指标固定为净额降序。`columns` 只有「真算过净流入的日子」——
 * 自算口径从 2026-09-23 才开始、补不了历史，所以它是一天一天长出来的。
 */
export interface FundFlowMatrixOut {
  /** 实际数据日（= 最新那一列），用来标「数据日期」 */
  trade_date: string
  taxonomy: string
  taxonomy_label: string
  top: number
  columns: FundFlowMatrixColumn[]
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
  /** 收盘后那一趟（龙虎榜 / 机构席位 / 涨停题材 / 两融 / 北向成交）—— 那几类 15:05 还没发布 */
  late_collect_time: string
  /** 同一趟的兜底时刻：来源当天什么时候更新不确定，只赌一个时刻会赌空 */
  late_retry_time: string
  /** 当天板块成分股的预取时刻：开盘红要到晚上才发布当天名单 */
  members_collect_time: string
  /** 它的兜底时刻：22:00 撞上「还没发布」时再试一次（正常日子几乎不花请求） */
  members_retry_time: string
  catchup_on_start: boolean
  next_run_time: string | null
  late_next_run_time: string | null
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
  /** 已用比例。**只用于展示** —— 让路判据已经改成 `need_ratio`（见下） */
  usage_ratio: number | null
  /** 本周期余下的交易日数（含今天） */
  days_left: number
  /** 余下交易日按满负荷估的调用数（常量 × days_left） */
  need_calls: number
  /** 让路判据：`need_calls ÷ cycle_remaining`。仅展示用；None = 剩余额度已是 0 */
  need_ratio: number | null
  /** 当前档位（0 = 正常，越大越紧张）。让路顺序见后端的 `QuotaLevel` */
  level: number
  /** 档位的说明文字（后端给，页面别再拼一遍） */
  level_label: string
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
  /**
   * 窗口模式（`?days=N`）下**这一轮首次**命中的日期；单日模式下就等于信号当天。
   * `run_days` = 这一轮持续了几个交易日、`last_date` = 最后一次命中是哪天。
   *
   * 为什么要这个：状态型形态（如「堆量上涨后缩量」）只要条件一直成立就天天命中，
   * 看十天名单时同一只票会占五六行。窗口模式把它合并成一条、日期显示首次那天。
   * 单日模式 `run_days` 恒为 1、`last_date` 等于 `trade_date`。
   */
  run_days: number
  last_date: string | null
  close: number | null
  pct_chg: number | null
  /** 信号当天的成交额 */
  amount: number | null
  /** 近 20 日日均成交额 */
  avg_amount: number | null
  /** 总市值 */
  total_mv: number | null
  /**
   * 所属的**开盘红精选板块**（口径 = 个股页「所属题材」：这只票**最近一次涨停**
   * 是因为哪个板块，不是它属于哪些概念）。开盘红没有非涨停个股的板块归属，
   * 所以从未涨停过的票这里是空数组 —— 实测覆盖约 75%。
   *
   * ⚠️ 接口这一层**不拿别的口径来顶**，页面上也是**两列分开、谁都不回落**：
   * 悟道页与形态页是「板块」「行业」各一列，个股页是「精选板块」「所属行业」两行。
   * （2026-10-09 曾短暂试过回落，看过效果后改成拆两列 —— 别再加回来。）
   */
  sectors: string[]
  /**
   * 所属**同花顺行业**，**三级路径**（`房地产-房地产-住宅开发`）。来自建池时顺手带回来的
   * `stock_basic.industry`（7 天一次、覆盖全 A），所以基本不会空。
   *
   * ⚠️ 与 `sectors` **不是一个口径**：这个答「公司做什么生意」，
   * `sectors` 答「最近一次涨停是因为哪个题材」。两处**各占一列**、只显示第一级、
   * 完整路径放 tooltip。
   */
  industry: string | null
  /**
   * 阶段判定（`classify_phase`）的 key 与中文标签 —— 与个股页／个股分析页同一判据。
   * 列表里只显示 `phase_label` 一个词（如「刚有人气」）。**日线不足 25 根时为 null**。
   */
  phase: string | null
  phase_label: string | null
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

/**
 * 每日「评分前 N 只」的 30 个交易日跟踪（`/patterns/track`）。
 *
 * ⚠️ **所有收益与比例都是百分数**（`mean: 1.23` 是 +1.23%、`up_pct: 54.3` 是 54.3%），
 * 不是 0~1 的比率 —— 与后端的 `PatternTrackPoint` 一一对应。
 */
export interface PatternTrackPoint {
  /** 筛选后第几个交易日（1 起） */
  day: number
  /** 有完整数据的样本数（该股这天没有行、或循环还没走到这天时不计入） */
  samples: number
  mean: number | null
  median: number | null
  /** 上涨占比 */
  up_pct: number | null
  /** 平均超额 = 各样本收益 − 它自己循环那天的全市场平均 */
  excess: number | null
  /** 跑赢当天全市场平均的比例 */
  beat_pct: number | null
}

/** 一个循环：某个筛选日选出的前 N 只，以及它们之后每一天的表现。 */
export interface PatternTrackCohort {
  trade_date: string
  /** 当天选出的只数（不足 top 就是那天命中总数不够） */
  stocks: number
  /** 已经能算到第几个交易日（= 已走完多少） */
  progress: number
  /** 长度 = progress：**没走到的日子不返回**（峰 / 谷由前端从这里取） */
  points: PatternTrackPoint[]
}

export interface PatternTrackSummary {
  cohorts_used: number
  /** 逐循环样本合计（同一只票重复上榜会重复计） */
  stocks: number
  /** 去重后的只数 */
  unique: number
  first_date: string | null
  last_date: string | null
}

export interface PatternTrack {
  cohorts: number
  track_days: number
  top: number
  summary: PatternTrackSummary
  /** 所有循环摊平后的同一条线（表格的「合计」行用） */
  average: PatternTrackPoint[]
  days: PatternTrackCohort[]
}

/** 明细表里的一只票。 */
export interface PatternTrackStock {
  code: string
  name: string | null
  score: number
  /**
   * 之后第 1..track_days 个交易日的**当日涨跌幅**（百分数，1.23 = +1.23%）。
   * ⚠️ 是每天的涨跌幅、不是累计；该股那天没有行情行时为 null
   */
  pct: (number | null)[]
}

/** 某个循环选中的票的逐日明细。 */
export interface PatternTrackDetail {
  trade_date: string
  top: number
  track_days: number
  /** 已经走到第几个交易日（0 = 还没走到任何一天） */
  progress: number
  /** 每一列对应的实际交易日，与每行 `pct` 一一对应 */
  days: string[]
  rows: PatternTrackStock[]
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
  /** 换手率（百分数，如 2.18 表示 2.18%）。**只有日 K 有值** ——
   *  周/月是几天合并，比率不能相加、均值也不成立，那边是 null */
  turnover: number | null
  /** 这一天是不是**收盘涨停**（按板块限幅判定，后端 `services/limit_rules.py`）。
   *  **只有日 K 有值**；周 / 月 K 是 null —— 一根柱子是几天的合并，
   *  「这天涨停」在那个粒度上不成立 */
  is_limit_up: boolean | null
  /** 这一天是不是**收盘跌停**（判据同 `is_limit_up`，方向相反：跌幅达限制且
   *  收盘价 = 当日最低价）。同样只有日 K 有值 */
  is_limit_down: boolean | null
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

/** 筹码分布直方图的一格：`weight` 是占比（0~1，全部格子求和为 1）。 */
export interface ChipPoint {
  price: number
  weight: number
}

/**
 * 个股筹码分布（CYQ）：本地按换手率衰减计算，零外部请求。
 *
 * 比例字段都是 0~1（`profit_ratio = 0.09` 表示 9% 的筹码处于盈利），价格单位元。
 * 日线不足 20 根时 `days = 0`、`histogram` 为空，`note` 说明原因。
 */
export interface ChipDistribution {
  code: string
  name: string | null
  /** 最后一根日线的日期；算不出时为 null */
  trade_date: string | null
  days: number
  price: number | null
  /** 获利比例：现价及以下的筹码占比 */
  profit_ratio: number | null
  avg_cost: number | null
  cost_90_low: number | null
  cost_90_high: number | null
  cost_70_low: number | null
  cost_70_high: number | null
  /** (90% 区间上沿 − 下沿) / (上沿 + 下沿)，越小越集中 */
  concentration_90: number | null
  concentration_70: number | null
  peak_price: number | null
  histogram: ChipPoint[]
  note: string | null
}

/** 杀猪盘体检的一个信号：hit / miss / unknown（数据不足，不投票）/ info（仅展示）。 */
export interface TrapSignal {
  key: string
  name: string
  status: 'hit' | 'miss' | 'unknown' | 'info'
  severity: 'high' | 'medium' | 'low'
  evidence: string
}

/** 杀猪盘体检结果：评分只加命中信号的权重，unknown 不当作 0 分。 */
export interface TrapCheck {
  code: string
  name: string | null
  trade_date: string | null
  score: number
  level: 'high' | 'orange' | 'medium' | 'low'
  level_label: string
  summary: string
  signals: TrapSignal[]
  /** 用户补充背景带来的等级上调档数（0 = 没提供或不含触发词） */
  context_boost: number
}

/**
 * 后端能聚合的 K 线周期。
 *
 * 周/月是**本地日线重采样**出来的（同一张 `stock_daily`，不额外取数），
 * 所以它们与日线是同一口径（不复权 / 前复权由调用方选）。
 */
export type KPeriod = 'day' | 'week' | 'month'

/**
 * K 线复权方式。三档与同花顺的复权菜单对齐（见 `AdjustMenu`）。
 *
 * `none` = 除权(不复权)：交易所真实价，**个股概况、涨跌停标记都是这个口径**；
 * `qfq` = 向前复权：锚在最新价（形态引擎判定用的就是这条）；
 * `hfq` = 向后复权：锚在窗口首日，历史段会大于真实价。
 */
export type FqMode = 'none' | 'qfq' | 'hfq'

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
  /** 被「身份 / 地域 / 业绩」黑名单剔掉的标签；`themes` 为空时用于悬浮说明 */
  suppressed: string[]
}

/**
 * 悟道「阶段判定」——一只票当天处于哪一档（移植自 yangban-desk 的 8 个标签）。
 *
 * 与形态选股的「明天盯 / 明天预案」**共用同一批判据**（`_wudao_is_sample` 等）。
 *
 * ⚠️ 但**不等价**（2026-10-09 起）：名单只收创业板 + 科创板，且出口另有一道
 * `_wudao_leave_ok`（收盘离开最高 ≤ 2.9%）—— 两处都故意没进这里。所以这里显示
 * 「明天盯 / 明天预案」时，名单里**可能没有它**（实测 09-28：31 只里只有 6 只在名单）。
 * 反过来进名单的票必然过了原型判据，但标签仍可能被更靠前的阶段抢走（如 `start`）。
 */
export interface WudaoPhase {
  /** silent / wake / sample / start / digest / diverge / dump / weak / unknown */
  phase: string
  label: string
  /** 现在该怎么做 */
  action: string
  /** 一句话结论（带量比等数字） */
  text: string
  fit_label: string
  fit_text: string
  prefer: number
  shadow_label: string
  shadow_text: string
  pressure_label: string
  pressure_text: string
  plan_text: string
  risk_text: string
  /** 关键价位：样板=今高、启动=昨高，其余为 null */
  watch_price: number | null
  metrics: Record<string, number | null>
}

/**
 * 一个收益档位的统计量（`/api/patterns/standing` 用）。
 *
 * ⚠️ **当前前端无界面调用（保留后端契约）**：下面这三个类型（`PoolHorizon` /
 * `PoolStanding` / `PoolStandingOut`）对应的成绩单页已从站内撤掉，但后端接口仍在，
 * 保留类型是为了让这份前端契约与后端 `schemas.py` 保持一致，别当成漏删的僵尸代码。
 *
 * 收益都是**百分数**（1.23 = +1.23%）。`excess` 是相对**同一天**全市场等权平均的超额 ——
 * 没有它，「涨了 5%」可能只是那几天大盘在涨。
 */
export interface PoolHorizon {
  days: number
  samples: number
  mean: number | null
  median: number | null
  up_pct: number | null
  excess: number | null
  beat_pct: number | null
}

/** 一个悟道池子的「成绩单」：每天照这个池子全买，之后会怎样。 */
export interface PoolStanding {
  pattern: string
  name: string
  /** 窗口内全部命中条数 / 去重只数 / 有命中的扫描日数 */
  hits: number
  stocks: number
  scan_days: number
  returns: PoolHorizon[]
  /** 破位率的样本数（到线率那一列用 `touch_samples`） */
  samples: number
  /** 到线率：命中后 N 日内**收盘**站上「要过的那条线」的比例；池子没有这条线时是 null */
  touch: number | null
  touch_samples: number
  /** 破位率：同期**收盘**跌破「作废位」的比例 */
  stop: number | null
  /** 破位用的是哪个位（「作废位」/「命中日最低价（代理）」）—— 两种口径不能看起来一样 */
  stop_source: string
}

export interface PoolStandingOut {
  start: string
  /** 窗口里**实际有命中**的扫描日数（样本还少时会等于 1） */
  cohorts: number
  scan_days: string[]
  track_days: number
  line_days: number
  pools: PoolStanding[]
}

/** 一个形态的成绩单（`/api/patterns/scorecard`）：与池子成绩单同口径，多分组与结论。 */
export interface PatternScorecardRow extends PoolStanding {
  /** 形态分组（趋势 / 突破 / 量价 / 几何 …） */
  group: string
  /** 结论：有效 / 观察 / 噪声 / 样本不足 */
  verdict: string
}

export interface PatternScorecardOut {
  start: string
  /** 窗口里实际有命中的扫描日数 */
  cohorts: number
  scan_days: string[]
  track_days: number
  line_days: number
  /** 同一 (形态, 代码) 的去重窗口（交易日） */
  dedup_days: number
  patterns: PatternScorecardRow[]
}

/** 一条个股新闻（东财口径）。 */
export interface StockNewsRow {
  /** 发布时间，东财原样给（"2026-08-26 09:59:14"） */
  published_at: string
  title: string
  summary: string
  /** 文章来源，如「界面新闻」 */
  source: string
  url: string
}

/**
 * 个股新闻列表（`/api/stock/{code}/news`）。
 *
 * `rows` 为空时看 `note`：**「真没搜到」与「源没返回」是两回事** ——
 * 页面要把这句话显示出来，别让用户以为这只票没有新闻。
 */
export interface StockNews {
  code: string
  name: string | null
  rows: StockNewsRow[]
  /**
   * 被筛掉的「只在正文表格里提到代码」的名单类稿件条数。
   *
   * 有标题命中的新闻时，名单类稿件**不进 `rows`**，但条数会带出来 —— 界面上要标一句
   * 「另隐去 N 条」，否则看起来像源少给了数据。
   */
  hidden: number
  note: string | null
}

/** 公告 / 研报 / 解禁三张表的公共字段。 */
export interface StockEventMeta {
  code: string
  name: string | null
  /** 取不到时的原因；`stale=true` 表示给的是上次缓存 */
  note: string | null
  stale: boolean
  /** 这份数据是什么时候写进缓存的 */
  fetched_at: string | null
}

/** 一条公告。 */
export interface StockNoticeRow {
  published_at: string
  title: string
  /** 东财公告类型（其他 / 半年度报告摘要 / 风险提示 …） */
  category: string
  url: string
}

export interface StockNotices extends StockEventMeta {
  rows: StockNoticeRow[]
}

/** 一条机构研报。 */
export interface StockReportRow {
  published_at: string
  title: string
  institution: string
  /** 东财评级（买入 / 增持 / 中性 …） */
  rating: string
  industry: string
  /** 盈利预测一行文本；上游没有预测时为空串（不是目标价） */
  forecast: string
  url: string
}

export interface StockReports extends StockEventMeta {
  rows: StockReportRow[]
}

/** 一笔未来限售解禁。两个比例都是**小数**（0.05 = 5%）。 */
export interface StockUnlockRow {
  unlock_date: string
  shares: number | null
  market_value: number | null
  ratio_total: number | null
  ratio_float: number | null
  share_type: string
}

export interface StockUnlocks extends StockEventMeta {
  rows: StockUnlockRow[]
}

/** 新增 / 修改一笔交易（对应后端 `TradeJournalIn`）。 */
export interface TradeJournalIn {
  code: string
  name?: string | null
  /** YYYY-MM-DD */
  entry_date: string
  entry_price: number
  /** 买入股数（可选）；买入金额由后端按 买入价 × 股数 现算 */
  shares?: number | null
  exit_date?: string | null
  exit_price?: number | null
  /** 仓位（占自己总资金的比例，0-100） */
  position_pct?: number | null
  /** pattern / wudao / theme / other */
  reason_type: string
  reason_key?: string | null
  reason_text?: string | null
  plan_stop?: number | null
  note?: string | null
}

/** 「按代码 + 买入日回溯理由」的结果（对应后端 `JournalReasonSuggestion`）。
 *
 * 只来自库里的形态扫描与涨停天梯快照：那天没跑扫描 / 该票当天没命中时两个列表都为空，
 * 页面上如实提示「当天没有可比对的记录」。 */
export interface JournalReasonSuggestion {
  code: string
  trade_date: string
  /** 当天命中的形态，按分数降序 */
  patterns: { key: string; name: string; score: number }[]
  /** 当天所属题材（没涨停过就是空） */
  themes: string[]
  /** 这只票今天的 20 日线乖离（%，负数）；没到超跌阈值时为 null */
  oversold_dev: number | null
  /** 上面的乖离是按哪天的日线算的（做 T 表单据此决定是否默认「超跌」） */
  oversold_date: string | null
}

/** 新增 / 修改一次做 T 的入参（对应后端 `TradeTIn`）。 */
export interface TradeTIn {
  /** 第一腿日期 */
  trade_date: string
  /** 第二腿日期；没做第二腿时 null */
  leg2_date: string | null
  /** long = 正T（先买后卖）/ short = 倒T（先卖后买） */
  direction: string
  buy_price: number | null
  sell_price: number | null
  /** 这次 T 占底仓的比例（0-100） */
  ratio_pct: number
  /** oversold / intraday_high / theme_fade / other */
  trigger: string
  note?: string | null
}

/** 一次做 T + 现算指标（对应后端 `TradeTRow`）。 */
export interface TradeTRow extends TradeTIn {
  id: number
  trade_id: number
  /** 两腿都有成交价 = 已完成；未完成时缺的那腿按期末价计 */
  completed: boolean
  /** 价差 = 卖出腿 / 买入腿 − 1（%） */
  spread_pct: number | null
  /** 扣费后价差（只对已完成的有意义） */
  net_pct: number | null
  /** 对底仓收益的贡献（降本），按底仓买入价计（%） */
  contrib_pct: number | null
  /** 卖飞幅度（只对「倒T 没接回」） */
  fly_pct: number | null
  success: boolean
  /** 期末价（未平仓 = 最新收盘，已平仓 = 卖出价） */
  end_price: number | null
  /** 盘中记的未完成 T：价差 / 降本等收盘后再算（页面显示「等收盘」） */
  pending_close: boolean
  /** 价格不在当天日线 [low, high] 区间内的软提示 */
  warnings: string[]
}

/** 一笔交易 + 用日线现算的指标（对应后端 `TradeJournalRow`）。 */
export interface TradeJournalRow extends TradeJournalIn {
  id: number
  /** 买入金额 = 买入价 × 股数（没填股数时为 null） */
  entry_amount: number | null
  reason_label: string
  /** 买入当天的接力温度档位 */
  sentiment_level: string | null
  sector: string | null
  /** 买入日到卖出日的交易日数（含两端） */
  holding_days: number | null
  return_pct: number | null
  max_drawdown_pct: number | null
  /** 持仓期间收盘价是否跌破计划止损位；没设止损为 null */
  stop_hit: boolean | null
  /** 触发止损后是否真的在止损位或更低卖出 */
  stop_executed: boolean | null
  /** 未平仓 = true（return_pct 是浮动盈亏） */
  open: boolean
  latest_date: string | null
  /** 最新一根日线的不复权收盘价（持仓行显示「最新价」） */
  latest_close: number | null
  metrics_note: string | null
  /** 这只票的做 T 明细，按第一腿日期升序 */
  t_ops: TradeTRow[]
  t_count: number
  /** 各条做 T 贡献的简单相加（每条相对各自底仓的百分比） */
  t_contrib_pct: number | null
  /** 未完成的做 T 条数 */
  t_open_count: number
  /** 含 T 收益 = return_pct + t_contrib_pct（return_pct 原口径不动） */
  return_with_t_pct: number | null
  created_at: string
  updated_at: string
}

export interface TradeJournalOut {
  rows: TradeJournalRow[]
}

export interface TradeStatsRow {
  label: string
  trades: number
  closed: number
  open: number
  win_rate: number | null
  avg_return: number | null
  avg_drawdown: number | null
  avg_holding_days: number | null
}

/** 做 T 的分组成绩（成功率只看已完成的）。 */
export interface TradeTStatsRow {
  label: string
  count: number
  completed: number
  open: number
  success_rate: number | null
  avg_net_pct: number | null
  total_contrib_pct: number | null
  fly_count: number
  avg_fly_pct: number | null
}

export interface TradeTStats {
  summary: TradeTStatsRow
  by_direction: TradeTStatsRow[]
  by_trigger: TradeTStatsRow[]
}

export interface TradeStatsOut {
  summary: TradeStatsRow
  by_reason: TradeStatsRow[]
  by_sentiment: TradeStatsRow[]
  by_sector: TradeStatsRow[]
  /** 做 T 成绩（2026-10-21）；没有记录时 count 全 0 */
  t_stats: TradeTStats
}

/** 次日计划（对应后端 `TradePlanIn`）。 */
export interface TradePlanIn {
  code: string
  name?: string | null
  /** YYYY-MM-DD，计划针对的交易日 */
  plan_date: string
  /** 过这个价买 */
  buy_price: number
  /** 破这个价放弃 */
  stop_price: number
  note?: string | null
}

export interface TradePlanRow extends TradePlanIn {
  id: number
  /** 触发 / 未触发 / 放弃（破位）/ 同日触发并跌破 / 待验证 / 无日线… */
  status: string
  high: number | null
  low: number | null
  close: number | null
  /** 实际成交价：跳空高开时是开盘价；一字板买不进时为 null */
  fill_price: number | null
  day_close_pct: number | null
  max_favorable_pct: number | null
  max_adverse_pct: number | null
  next_1d_pct: number | null
  next_3d_pct: number | null
  next_5d_pct: number | null
  created_at: string
  updated_at: string
}

export interface TradePlanOut {
  rows: TradePlanRow[]
}

/** 次日计划候选：自选股 + 当日形态命中（带 key_levels）。 */
export interface TradePlanSuggestion {
  code: string
  name: string | null
  source: string
  in_watchlist: boolean
  pattern: string | null
  pattern_name: string | null
  buy_price: number | null
  stop_price: number | null
}

export interface TradePlanSuggestionsOut {
  rows: TradePlanSuggestion[]
}

/** 相似日向量的一维：今天的原始值 + 历史分位（0~1）。 */
export interface SimilarDayDim {
  key: string
  label: string
  value: number | null
  rank: number | null
}

export interface SimilarDayRow {
  trade_date: string
  distance: number
  /** 0~100，越大越像 */
  similarity: number
  limit_up: number | null
  broken_rate: number | null
  max_consecutive: number | null
  above_ma20: number | null
  fear_greed: number | null
  relay_score: number | null
  index_1d: number | null
  index_3d: number | null
  index_5d: number | null
  sentiment_1d: number | null
  sentiment_3d: number | null
  sentiment_5d: number | null
}

export interface SimilarDays {
  trade_date: string | null
  vector: SimilarDayDim[]
  rows: SimilarDayRow[]
  lookback_days: number
}

/**
 * 个股分析的返回（`/api/analysis/lookup`）：把「代码 / 名称 / 拼音首字母」解析成结论。
 *
 * 移植自原型 yangban-desk 的「查票分析」框。`phase` 与悟道之路那几张名单
 * **共用同一批判据**，但**不等价**：名单只收创业板 + 科创板、且出口多一道收盘过滤
 * （`_wudao_leave_ok`），所以这一页说「明天盯 / 明天预案」时名单里可能没有它。
 * 详见 `api/analysis.py` 的模块说明。
 */
export interface StockAnalysis {
  code: string
  name: string | null
  /** 该股**最新一行**日线是哪天。通常也就是阶段判定用的那根 —— 只有那一行是
   *  停牌残行（被 `load_phase` 剔掉）时，判定会退到再往前一根，而这里仍显示最新那天 */
  trade_date: string | null
  close: number | null
  pct_chg: number | null
  /** 阶段判定；日线不足 25 根时是 null */
  phase: WudaoPhase | null
  /** 所属同花顺三级行业（做什么生意） */
  industry: string | null
  /** 最近一次涨停时挂的精选板块（做过什么题材），只覆盖涨停过的票 */
  sectors: string[]
  /** 上过涨停池 / 上过龙虎榜的**天数**（去重交易日，不是行数、更不是历史累计）。
   *  ⚠️ 数的是下面两个窗口内的那些天，页面必须把窗口一起显示 */
  limit_up_count: number
  lhb_count: number
  /** 上面两个计数的**分母窗口**：`limit_pool` / `lhb` 各自覆盖的交易日数。
   *  页面必须显示出来 —— 不写窗口会被读成「这只票一辈子涨停 12 次」 */
  limit_up_days: number
  lhb_days: number
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
  /** 近 5 个交易日涨跌幅（百分数）。日线不足 6 根时是 null */
  pct_chg_5d: number | null
  // 下面四个来自后端 `stock_basic`（建池时随选股接口取回，覆盖全 A）。
  // 市值与市盈率已按最新收盘价缩放，不是快照那天的旧值。
  /** 总市值（元） */
  total_mv: number | null
  /** 自由流通市值（元） */
  free_float_mv: number | null
  /** 实际换手率（百分数）= 成交量 ÷ 自由流通股 */
  actual_turnover: number | null
  /** 动态市盈率（预测市盈率，同花顺口径） */
  pe_forecast: number | null
  /** 上面那几个指标的数据日（缩放基准日） */
  total_mv_asof: string | null
  /** 悟道「阶段判定」。日线不足 25 根时是 null */
  phase: WudaoPhase | null
}

export interface ReviewNote {
  trade_date: string
  market_view: string | null
  next_plan: string | null
  updated_at: string | null
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
  /** ETF 净申赎估算（不含货币现金类）；没有上一交易日数据时为 null */
  etf_net_inflow: number | null
  /** 期指净空数据的日期。中金所约 16:00 才发布，可能早于 trade_date */
  futures_date: string | null
  /** 四个股指期货品种的前 20 席位净空（手） */
  futures_net_short: FuturesNetShort[]
  /** 大盘主力净流入（全市场 DDE 汇总，iFinD 口径，元）。负数 = 净流出 */
  main_net_inflow: number | null
  /** 上一交易日的主力净流入，用于算「较上日」 */
  main_net_inflow_prev: number | null
}

export interface FuturesNetShort {
  variety: string
  /** 沪深300 / 中证500 / 上证50 / 中证1000 */
  name: string
  /** 主要玩家（前 20 席位）净空（手） */
  net_short: number
  /** 主要玩家当日变化：正 = 加空，负 = 减空 */
  change: number
  /** 中信期货单席位净空（手） */
  citic_net_short: number
  /** 中信期货当日变化 */
  citic_change: number
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

/* ------------------------------- 会员与登录（设计见文档 §8.69） ------------------- */

/** 「我是谁」。前端启动时问一次，用来决定要不要跳登录页、能不能进数据管理页。 */
export interface Me {
  id: number
  username: string
  is_admin: boolean
}

export interface Invite {
  code: string
  /** 这个码打算给谁（纯备注） */
  note: string | null
  created_at: string
  used_by: number | null
  /** 用掉它的那个人的用户名 —— 列表里直接看得懂 */
  used_by_name: string | null
  used_at: string | null
  disabled_at: string | null
}

export interface Member {
  id: number
  username: string
  is_admin: boolean
  created_at: string
  /** 最近访问（任何带会话的请求都会按 1 小时节流刷新）—— 不是「最近登录」 */
  last_seen_at: string | null
  disabled_at: string | null
  /** 他是拿哪个邀请码进来的（可追来源） */
  invite_code: string | null
}
