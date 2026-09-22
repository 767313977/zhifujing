import type {
  AdminStatus,
  CollectResult,
  DdeBoard,
  DdeOrder,
  EtfFlowBoard,
  FundFlowHistoryOut,
  FundFlowTaxonomy,
  EtfFlowOrder,
  EtfIndustryBoard,
  FundFlowOverview,
  FundsSeries,
  IndexHistory,
  InstitutionBoard,
  InstitutionOrder,
  KPeriod,
  LimitPool,
  LimitThemes,
  LhbItem,
  MarketOverview,
  PoolType,
  PatternMeta,
  PatternStock,
  PatternSummary,
  Preset,
  PromotionSeries,
  ReviewNote,
  RotationLeaderDay,
  RotationMetric,
  ScreenRun,
  SectorCompare,
  SectorFundFlowOut,
  SectorHeat,
  SectorMembers,
  SectorRanking,
  SectorRotation,
  SectorSeries,
  SectorTaxonomy,
  Sentiment,
  StockDailyRow,
  StockDde,
  StockProfile,
  StockThemes,
  WatchlistRow,
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
  if (!date) return path
  // 路径自带查询串时要用 & 接，否则第二个参数会被并进前一个参数值里
  return `${path}${path.includes('?') ? '&' : '?'}date=${date}`
}

export const api = {
  overview: (date?: string | null) =>
    request<MarketOverview>(withDate('/market/overview', date)),

  sentimentSeries: (days = 60) =>
    request<Sentiment[]>(`/market/sentiment?days=${days}`),

  // 上限 250（后端 le=250）。不传就只有 60 天，日期下拉能往回翻多远全看这个值
  dates: (days = 250) => request<string[]>(`/market/dates?days=${days}`),

  indexHistory: (days = 120) =>
    request<IndexHistory>(`/market/index-history?days=${days}`),

  limitPool: (type: PoolType, date?: string | null) =>
    request<LimitPool>(
      date ? `/limit/pool?type=${type}&date=${date}` : `/limit/pool?type=${type}`,
    ),

  lhb: (date?: string | null) => request<LhbItem[]>(withDate('/lhb', date)),

  sectorRanking: (taxonomy: SectorTaxonomy, date?: string | null) =>
    request<SectorRanking>(
      withDate(`/sectors/ranking?taxonomy=${taxonomy}`, date),
    ),

  sectorSeries: (code: string, days = 30, date?: string | null) =>
    request<SectorSeries>(
      withDate(`/sectors/series?code=${encodeURIComponent(code)}&days=${days}`, date),
    ),

  sectorCompare: (codes: string[], days = 30, date?: string | null) =>
    request<SectorCompare>(
      withDate(
        `/sectors/compare?codes=${encodeURIComponent(codes.join(','))}&days=${days}`,
        date,
      ),
    ),

  sectorMembers: (code: string, date?: string | null) =>
    request<SectorMembers>(
      withDate(`/sectors/members?code=${encodeURIComponent(code)}`, date),
    ),

  sectorHeat: (date?: string | null) => request<SectorHeat>(withDate('/sectors/heat', date)),

  sectorRotation: (
    taxonomy: SectorTaxonomy,
    options: { days: number; top: number; metric: RotationMetric },
    date?: string | null,
  ) =>
    request<SectorRotation>(
      withDate(
        `/sectors/rotation?taxonomy=${taxonomy}&days=${options.days}` +
          `&top=${options.top}&metric=${options.metric}`,
        date,
      ),
    ),

  /**
   * 矩阵「领涨」行的数据：**选中板块**在最近 N 天的涨停股。
   *
   * 与 `sectorRotation` 分开取：这一行点一次格子换一次，而矩阵本身不变 ——
   * 合在一个请求里的话，每点一下整个面板都会进 loading（那里面板整块变「加载中…」）。
   */
  sectorRotationLeaders: (
    taxonomy: SectorTaxonomy,
    options: { days: number; code: string },
    date?: string | null,
  ) =>
    request<RotationLeaderDay[]>(
      withDate(
        `/sectors/rotation/leaders?taxonomy=${taxonomy}&days=${options.days}` +
          `&code=${encodeURIComponent(options.code)}`,
        date,
      ),
    ),

  /**
   * 板块资金流向（**同花顺口径**：概念 / 行业）。
   *
   * 与 `sectorRanking` 是两套名字，别指望它的 name 能对上板块页的板块 ——
   * 后端 `SectorFundFlow` 的注释写明了原因。
   */
  sectorFundFlow: (taxonomy: FundFlowTaxonomy, date?: string | null) =>
    request<SectorFundFlowOut>(
      withDate(`/sectors/fund-flow?taxonomy=${taxonomy}`, date),
    ),

  /**
   * 各板块的**近 N 日累计净流入**曲线（同一张表，后端按日累加）。
   *
   * 与 `sectorFundFlow` 分开取：那个是「那一天的排行」，这个要跨多天，
   * 窗口档位也是独立的。
   */
  sectorFundFlowHistory: (
    taxonomy: FundFlowTaxonomy,
    options: { days: number },
    date?: string | null,
  ) =>
    request<FundFlowHistoryOut>(
      withDate(
        `/sectors/fund-flow/history?taxonomy=${taxonomy}&days=${options.days}`,
        date,
      ),
    ),

  limitThemes: (date?: string | null) =>
    request<LimitThemes>(withDate('/limit/themes', date)),

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

  watchlist: () => request<WatchlistRow[]>('/watchlist'),

  addWatchlist: (code: string, name?: string) =>
    // 后端 POST /watchlist 返回的是完整 WatchlistRow（含 latest_date/close/pct_chg），
    // 不是只有基础字段的 WatchlistItem —— 类型写错会在将来读返回值时给错结论
    request<WatchlistRow>('/watchlist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code, name }),
    }),

  removeWatchlist: (code: string) =>
    request<{ ok: boolean }>(`/watchlist/${code}`, { method: 'DELETE' }),

  updateWatchlistNote: (code: string, note: string) =>
    request<WatchlistRow>(`/watchlist/${code}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code, note }),
    }),

  syncWatchlist: (days = 250) =>
    request<{ rows: number }>(`/watchlist/sync?days=${days}`, { method: 'POST' }),

  stockProfile: (code: string) => request<StockProfile>(`/stock/${code}`),

  /**
   * 个股 K 线。
   *
   * `period` 只影响**后端怎么聚合**（周/月是本地日线重采样出来的，不额外取数）；
   * `days` 是「取多少根日线来聚合」—— 周/月要看长周期，所以要传得比日线大得多。
   */
  stockDaily: (
    code: string,
    options: { days?: number; adjust?: boolean; period?: KPeriod } = {},
  ) => {
    const { days = 120, adjust = false, period = 'day' } = options
    return request<StockDailyRow[]>(
      `/stock/${code}/daily?days=${days}&period=${period}${adjust ? '&adjust=1' : ''}`,
    )
  },

  stockThemes: (code: string) => request<StockThemes>(`/stock/${code}/themes`),

  /**
   * 个股的 **DDE 与主力净流入**（iFinD 口径，日频，单位元）。
   *
   * `days` 是「最近 N 个交易日」（后端 5~250，默认 60）。这两个指标只有 iFinD 有；
   * 库里没有最近交易日的数据时后端会**现取一次**（花 1 次配额），之后读库。
   */
  stockDde: (code: string, days = 60) =>
    request<StockDde>(`/stock/${code}/dde?days=${days}`),

  // --- 形态选股 ---
  patternCatalog: () => request<PatternMeta[]>('/patterns/catalog'),

  patternHits: (date?: string | null, minScore = 0, limit = 500) =>
    request<PatternStock[]>(
      withDate(`/patterns/hits?min_score=${minScore}&limit=${limit}`, date),
    ),

  patternSummary: (date?: string | null) => request<PatternSummary>(
    withDate('/patterns/summary', date),
  ),

  syncStock: (code: string, days = 250) =>
    request<{ rows: number }>(`/stock/${code}/sync?days=${days}`, { method: 'POST' }),

  note: (date: string) => request<ReviewNote>(`/note/${date}`),

  saveNote: (date: string, marketView: string, nextPlan: string) =>
    request<ReviewNote>(`/note/${date}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ market_view: marketView, next_plan: nextPlan }),
    }),

  adminStatus: () => request<AdminStatus>('/admin/status'),

  collect: (date?: string | null) =>
    request<CollectResult>(withDate('/admin/collect', date), { method: 'POST' }),

  backfill: (start: string, end?: string) =>
    request<{ days: number; failed_steps: number }>(
      `/admin/backfill?start=${start}${end ? `&end=${end}` : ''}`,
      { method: 'POST' },
    ),

  // --- 资金面 ---
  fundsOverview: (date?: string | null) =>
    request<FundFlowOverview>(withDate('/funds/overview', date)),

  fundsSeries: (days = 60, date?: string | null) =>
    request<FundsSeries>(withDate(`/funds/series?days=${days}`, date)),

  fundsEtf: (order: EtfFlowOrder = 'inflow', limit = 30, date?: string | null) =>
    request<EtfFlowBoard>(withDate(`/funds/etf?limit=${limit}&order=${order}`, date)),

  fundsEtfIndustry: (order: EtfFlowOrder = 'inflow', limit = 30, date?: string | null) =>
    request<EtfIndustryBoard>(withDate(`/funds/etf-industry?limit=${limit}&order=${order}`, date)),

  fundsInstitutions: (
    order: InstitutionOrder = 'net',
    limit = 30,
    date?: string | null,
  ) => request<InstitutionBoard>(withDate(`/funds/institutions?limit=${limit}&order=${order}`, date)),

  // DDE 榜。数据来自每天采集链末尾的全市场扫描，打开页面不花任何配额
  fundsDde: (order: DdeOrder = 'inflow', limit = 30, date?: string | null) =>
    request<DdeBoard>(withDate(`/funds/dde?limit=${limit}&order=${order}`, date)),
}
