import type {
  AdminStatus,
  CollectResult,
  IndexHistory,
  LimitPool,
  LhbItem,
  MarketOverview,
  PoolType,
  Preset,
  PromotionSeries,
  ScreenRun,
  Sentiment,
  WatchlistItem,
} from './types'

const BASE = '/api'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, init)
  if (!response.ok) {
    // 后端把可读原因放在 detail 里（如「暂无数据，请先执行采集」）
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = (await response.json()) as { detail?: string }
      if (body.detail) detail = body.detail
    } catch {
      // 响应不是 JSON，保留状态文本
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

function withDate(path: string, date?: string | null): string {
  return date ? `${path}?date=${date}` : path
}

export const api = {
  overview: (date?: string | null) =>
    request<MarketOverview>(withDate('/market/overview', date)),

  sentimentSeries: (days = 60) =>
    request<Sentiment[]>(`/market/sentiment?days=${days}`),

  dates: () => request<string[]>('/market/dates'),

  indexHistory: (days = 120) =>
    request<IndexHistory>(`/market/index-history?days=${days}`),

  limitPool: (type: PoolType, date?: string | null) =>
    request<LimitPool>(
      date ? `/limit/pool?type=${type}&date=${date}` : `/limit/pool?type=${type}`,
    ),

  lhb: (date?: string | null) => request<LhbItem[]>(withDate('/lhb', date)),

  promotion: (days = 15) =>
    request<PromotionSeries>(`/limit/promotion?days=${days}`),

  runScreen: (query: string) =>
    request<ScreenRun>(`/screener/run?query=${encodeURIComponent(query)}`, {
      method: 'POST',
    }),

  presets: () => request<Preset[]>('/screener/presets'),

  savePreset: (name: string, query: string) =>
    request<Preset>('/screener/presets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, query }),
    }),

  deletePreset: (id: number) =>
    request<{ ok: boolean }>(`/screener/presets/${id}`, { method: 'DELETE' }),

  watchlist: () => request<WatchlistItem[]>('/watchlist'),

  addWatchlist: (code: string, name?: string) =>
    request<WatchlistItem>('/watchlist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code, name }),
    }),

  removeWatchlist: (code: string) =>
    request<{ ok: boolean }>(`/watchlist/${code}`, { method: 'DELETE' }),

  adminStatus: () => request<AdminStatus>('/admin/status'),

  collect: (date?: string | null) =>
    request<CollectResult>(withDate('/admin/collect', date), { method: 'POST' }),

  backfill: (start: string, end?: string) =>
    request<{ days: number; failed_steps: number }>(
      `/admin/backfill?start=${start}${end ? `&end=${end}` : ''}`,
      { method: 'POST' },
    ),
}
