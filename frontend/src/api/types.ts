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

export interface AdminStatus {
  latest_sentiment_date: string | null
  latest_limit_pool_date: string | null
  latest_lhb_date: string | null
  data_days: number
  recent_logs: CollectLog[]
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
