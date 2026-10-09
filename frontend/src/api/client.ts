import type {
  AdminStatus,
  DdeBoard,
  DdeOrder,
  EtfFlowBoard,
  FundFlowHistoryOut,
  FundFlowMatrixOut,
  EtfFlowOrder,
  EtfIndustryBoard,
  FqMode,
  FundFlowOverview,
  FundsSeries,
  IndexHistory,
  InstitutionBoard,
  InstitutionOrder,
  Invite,
  KPeriod,
  LimitPool,
  LimitThemes,
  LhbItem,
  MarketOverview,
  Me,
  Member,
  PoolType,
  PatternMeta,
  PatternStock,
  PatternSummary,
  PatternTrack,
  PatternTrackDetail,
  PromotionSeries,
  ReviewNote,
  RotationLeaderDay,
  RotationMetric,
  SectorCompare,
  SectorFundFlowOut,
  SectorHeat,
  SectorMembers,
  SectorRanking,
  SectorRotation,
  SectorSeries,
  SectorTaxonomy,
  Sentiment,
  StockAnalysis,
  StockDailyRow,
  StockDde,
  StockNews,
  StockProfile,
  StockThemes,
  WatchlistRow,
} from './types'

const BASE = '/api'

/**
 * 收到 401 时发的自定义事件。定义在这里（而不是 lib/auth.tsx）是为了**避免循环导入**：
 * auth.tsx 要用 api，而这个事件要用在 api 里 —— 只能有一个方向。
 */
export const UNAUTHORIZED_EVENT = 'fupan:unauthorized'

/**
 * 未登录 / 会话过期。
 *
 * **单独一个错误类型**是必要的：调用方（`RequireAuth`）要靠它决定「跳登录页」，
 * 而不是把「请先登录」当成普通业务错误，在页面上显示成一行红字。
 */
export class UnauthorizedError extends Error {
  constructor(message = '请先登录') {
    super(message)
    this.name = 'UnauthorizedError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // `credentials: 'same-origin'`：登录态在 HttpOnly cookie 里，不带它就等于没登录。
  // 同源请求浏览器默认也会带，但显式写出来 —— 将来若改成跨域部署，这里不会静默失效。
  const response = await fetch(`${BASE}${path}`, { credentials: 'same-origin', ...init })
  if (!response.ok) {
    // 后端把可读原因放在 detail 里（如「暂无数据，请先执行采集」）
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = (await response.json()) as { detail?: string }
      if (body.detail) detail = body.detail
    } catch {
      // 响应不是 JSON，保留状态文本
    }
    if (response.status === 401) {
      // 会话**在使用中**失效（闲置过期 / 在别处改了密码 / 被停用）时，页面早就渲染出来了，
      // RequireAuth 不会再跑。靠这个事件让 AuthProvider 清掉用户，下一次渲染自动跳登录页。
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
      throw new UnauthorizedError(detail)
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

/** POST / PUT / PATCH 带 JSON body。抽出来只为少写三行样板。 */
function send<T>(
  method: 'POST' | 'PUT' | 'PATCH',
  path: string,
  body?: unknown,
): Promise<T> {
  return request<T>(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
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
   * 板块资金流向（**开盘啦口径**：精选 / 行业，与板块页同一套名字）。
   *
   * 净流入 = 该板块**成分股的主力净流入之和**（本项目自己算的口径，见后端
   * `jobs/collect_board_flow.py`），单位亿元。与开盘啦 App 里它自己的板块资金流
   * 不保证相等；`in_amount` / `out_amount` 恒为空。
   */
  sectorFundFlow: (taxonomy: SectorTaxonomy, date?: string | null) =>
    request<SectorFundFlowOut>(
      withDate(`/sectors/fund-flow?taxonomy=${taxonomy}`, date),
    ),

  /**
   * 各板块的**近 N 日累计净流入**曲线（与 `sectorFundFlow` 同一份数据，后端按日累加）。
   *
   * 与 `sectorFundFlow` 分开取：那个是「那一天的排行」，这个要跨多天，
   * 窗口档位也是独立的。
   */
  sectorFundFlowHistory: (
    taxonomy: SectorTaxonomy,
    options: { days: number },
    date?: string | null,
  ) =>
    request<FundFlowHistoryOut>(
      withDate(
        `/sectors/fund-flow/history?taxonomy=${taxonomy}&days=${options.days}`,
        date,
      ),
    ),

  /**
   * 资金流多日矩阵（与轮动矩阵同一套版式）：列是交易日、行是当日净流入第 N 名。
   *
   * `days` 与累计曲线**共用同一个窗口档位**（都是「近 N 日」）—— 页面上只放一个
   * 选择器，两处一起变；数据量很小，分两个请求取是因为矩阵要按天排名、曲线要按
   * 板块累加，后端的取数方式不同。
   */
  sectorFundFlowMatrix: (
    taxonomy: SectorTaxonomy,
    options: { days: number; top: number },
    date?: string | null,
  ) =>
    request<FundFlowMatrixOut>(
      withDate(
        `/sectors/fund-flow/matrix?taxonomy=${taxonomy}` +
          `&days=${options.days}&top=${options.top}`,
        date,
      ),
    ),

  limitThemes: (date?: string | null) =>
    request<LimitThemes>(withDate('/limit/themes', date)),

  promotion: (days = 15) =>
    request<PromotionSeries>(`/limit/promotion?days=${days}`),

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
   * `fq` 是复权方式（三档），`volAdjust` 让成交量跟着同一比例缩放 —— 两者都只影响
   * 价格 / 量，**涨跌停标记始终按原始价判**（后端在复权之前算好、按交易日挂回来）。
   */
  stockDaily: (
    code: string,
    options: { days?: number; fq?: FqMode; volAdjust?: boolean; period?: KPeriod } = {},
  ) => {
    const { days = 120, fq = 'none', volAdjust = false, period = 'day' } = options
    const vol = volAdjust ? '&vol_adjust=1' : ''
    return request<StockDailyRow[]>(
      `/stock/${code}/daily?days=${days}&period=${period}&fq=${fq}${vol}`,
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

  /**
   * 个股新闻（**东财口径**，按发布时间倒序）。
   *
   * 后端**每次打开都现取、不落库**（新闻的价值全在「新」，而这条源没有配额）；
   * 取不到时 `rows` 为空、`note` 说明原因 —— 别把两者混成「这只票没有新闻」。
   */
  stockNews: (code: string, limit = 20) =>
    request<StockNews>(`/stock/${code}/news?limit=${limit}`),

  /**
   * 个股分析：把「代码 / 名称 / 拼音首字母」解析成结论（阶段判定 + 旁证）。
   *
   * 解析不出或匹配到多只时后端返回 400，`detail` 就是给用户看的那句话
   * （如「「平安」匹配到多只：…，请输入完整代码」）—— 直接显示，不用自己拼文案。
   */
  analysisLookup: (q: string) =>
    request<StockAnalysis>(`/analysis/lookup?q=${encodeURIComponent(q)}`),

  // --- 形态选股 ---
  patternCatalog: () => request<PatternMeta[]>('/patterns/catalog'),

  /**
   * 命中列表。`pattern` 传了就只看该形态、且**不再按 50 截断**（后端会把它全部
   * 返回，评分与排序也换成该形态自己的分数）；不传则是「全市场按评分取前 limit 只」。
   */
  patternHits: (
    date?: string | null,
    minScore = 0,
    limit = 50,
    pattern?: string | null,
  ) => {
    const pick = pattern ? `&pattern=${pattern}` : ''
    return request<PatternStock[]>(
      withDate(`/patterns/hits?min_score=${minScore}&limit=${limit}${pick}`, date),
    )
  },

  patternSummary: (date?: string | null) => request<PatternSummary>(
    withDate('/patterns/summary', date),
  ),

  /**
   * 每个筛选日的「评分前 `top` 只」在其后 `trackDays` 个交易日里的走势。
   *
   * `cohorts` 是**最近多少个有命中记录的交易日**（每个日子 = 一个循环，不是自然日）。
   */
  patternTrack: (cohorts = 30, top = 50, trackDays = 30) =>
    request<PatternTrack>(
      `/patterns/track?cohorts=${cohorts}&top=${top}&track_days=${trackDays}`,
    ),

  /**
   * 某个循环选中的票的**逐日明细**（哪 50 只、之后每个交易日各涨跌多少）。
   *
   * 每列是**当天的涨跌幅**，不是累计。入选口径与 `patternTrack` 完全一致。
   */
  patternTrackDetail: (date: string, top = 50, trackDays = 30) =>
    request<PatternTrackDetail>(
      `/patterns/track/detail?date=${date}&top=${top}&track_days=${trackDays}`,
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

  // 这里原来有个 `collect`（POST /admin/collect）。**2026-09-28 删掉**：采集是
  // 「点一下就白跑一轮（约 50 次 iFinD 调用）」的操作，用户要求禁止手动采集，
  // 后端那个接口也一并删了。要强制重采就重启服务（启动补采会补跑）。

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

  // --- 登录与会员（设计见文档 §8.69）---

  /** 「我是谁」。未登录会抛 UnauthorizedError，由 RequireAuth 兜住跳登录页。 */
  authMe: () => request<Me>('/auth/me'),

  login: (username: string, password: string) =>
    send<Me>('POST', '/auth/login', { username, password }),

  /** 注册成功即登录（后端直接种 cookie），所以不需要再调一次 login。 */
  register: (inviteCode: string, username: string, password: string) =>
    send<Me>('POST', '/auth/register', {
      invite_code: inviteCode,
      username,
      password,
    }),

  logout: () => send<{ ok: boolean }>('POST', '/auth/logout'),

  /** 改密码会把**其它**设备的会话全部踢掉，当前这个保留。 */
  changePassword: (oldPassword: string, newPassword: string) =>
    send<Me>('PUT', '/auth/password', {
      old_password: oldPassword,
      new_password: newPassword,
    }),

  // --- 管理员：邀请码与会员 ---

  invites: () => request<Invite[]>('/admin/invites'),

  createInvites: (note: string, count = 1) =>
    send<Invite[]>('POST', '/admin/invites', { note, count }),

  /** 只对**没用过**的邀请码生效；用过的后端会拒绝（会失去来源记录）。 */
  deleteInvite: (code: string) =>
    request<{ ok: boolean }>(`/admin/invites/${encodeURIComponent(code)}`, {
      method: 'DELETE',
    }),

  members: () => request<Member[]>('/admin/users'),

  resetMemberPassword: (id: number, newPassword: string) =>
    send<{ ok: boolean; kicked_sessions: number }>(
      'POST',
      `/admin/users/${id}/reset-password`,
      { new_password: newPassword },
    ),

  setMemberDisabled: (id: number, disabled: boolean) =>
    send<{ ok: boolean; kicked_sessions: number }>(
      'POST',
      `/admin/users/${id}/disabled`,
      { disabled },
    ),
}
