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
  total_amount: number | null
  yesterday_limit_today_avg: number | null
}

export interface MarketOverview {
  trade_date: string
  indexes: IndexQuote[]
  sentiment: Sentiment | null
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
  industry: string | null
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

export interface AdminStatus {
  latest_sentiment_date: string | null
  latest_limit_pool_date: string | null
  latest_lhb_date: string | null
  latest_index_date: string | null
  data_days: number
  coverage: TableCoverage[]
  scheduler: SchedulerStatus
  recent_logs: CollectLog[]
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
