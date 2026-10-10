import type {
  AdminStatus,
  AnomalyMap,
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
  PoolStandingOut,
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
 * 默认请求超时（毫秒）。**必须要有超时**：`fetch` 本身不会超时，弱网下一条请求挂住
 * 就是**永久等待** —— 页面停在「请稍候…」或空白页，用户只能自己刷新（2026-10-10 修）。
 */
const DEFAULT_TIMEOUT_MS = 30_000

/**
 * 长任务的超时（10 分钟）。与线上 nginx 的 `proxy_read_timeout 600s` 对齐 —— 超过它
 * nginx 也会先掐，给到同一个上限就够。用在**真的会跑很久**的三个接口上：
 * 历史回补、自选同步、单只同步（后两个要按年的跨度去问 iFinD）。
 */
const LONG_TIMEOUT_MS = 600_000

/**
 * 「确认登录态」用的超时，单独给一个**更短**的值。
 *
 * 这一步挡在**整页渲染之前**（见 `lib/auth.tsx` 的 RequireAuth：没确认完就渲染一个
 * 空白 div），所以这里等 30 秒 = 白屏 30 秒。10 秒足够一个正常请求跑完，
 * 超了就当「没登录」去登录页，比干等强。
 */
const AUTH_CHECK_TIMEOUT_MS = 10_000

/**
 * 收到 401 时发的自定义事件。定义在这里（而不是 lib/auth.tsx）是为了**避免循环导入**：
 * auth.tsx 要用 api，而这个事件要用在 api 里 —— 只能有一个方向。
 */
export const UNAUTHORIZED_EVENT = 'fupan:unauthorized'

async function request<T>(
  path: string,
  init?: RequestInit,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  // 为什么手写 AbortController、不用更短的 `AbortSignal.timeout()`：后者要
  // Chrome 103+ / Safari 16.4+，而本站会在手机套壳浏览器（微信 / QQ / 夸克）里打开，
  // 手写这几行到处都能跑。
  const controller = new AbortController()
  // 区分「超时」与「调用方主动取消」：两者都会让 fetch 抛 AbortError，但只有前者
  // 该被翻成「超时」给用户看。
  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, timeoutMs)
  // 合并调用方的 signal（而不是让 `signal: controller.signal` 把它覆盖掉）：调用方
  // 传了 signal 时（如切票要取消旧请求），任一 abort 都要能取消这次请求 —— 直接覆盖
  // 会把调用方的 signal 丢掉，旧请求就永远取消不掉。`AbortSignal.any` 兼容性不够，手写。
  const callerSignal = init?.signal ?? null
  const abortFromCaller = () => controller.abort()
  if (callerSignal) {
    if (callerSignal.aborted) controller.abort()
    else callerSignal.addEventListener('abort', abortFromCaller)
  }
  try {
    // `credentials: 'same-origin'`：登录态在 HttpOnly cookie 里，不带它就等于没登录。
    // 同源请求浏览器默认也会带，但显式写出来 —— 将来若改成跨域部署，这里不会静默失效。
    const response = await fetch(`${BASE}${path}`, {
      credentials: 'same-origin',
      ...init,
      signal: controller.signal,
    })
    if (!response.ok) {
      // 后端把可读原因放在 detail 里（如「暂无数据，请先执行采集」）。
      // HTTP/2 下 statusText 恒为空，只拼状态码会显示成「504 」这种半截文案，所以空时
      // 兜底成「HTTP 504」（2026-10-10 用户看到的就是「504 」）。
      let detail = response.statusText
        ? `${response.status} ${response.statusText}`
        : `HTTP ${response.status}`
      try {
        const body = (await response.json()) as { detail?: unknown }
        const fromDetail = formatDetail(body.detail)
        if (fromDetail) detail = fromDetail
      } catch {
        // 响应不是 JSON，保留状态文本
      }
      if (response.status === 401) {
        // 会话**在使用中**失效（闲置过期 / 在别处改了密码 / 被停用）时，页面早就渲染出来了，
        // RequireAuth 不会再跑。靠这个事件让 AuthProvider 清掉用户，下一次渲染自动跳登录页。
        // （这里不再抛专门的错误类型：没有调用方按类型分支，跳转全靠上面这个事件。）
        window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
        throw new Error(detail)
      }
      throw new Error(detail)
    }
    // 读**响应体**也要在超时保护内：`fetch` resolve 只代表收到了响应头，弱网下 body
    // 传到一半卡住时 `response.json()` 会一直挂着，页面永远「加载中」。所以这里不提前
    // `clearTimeout`，等 body 读完由 finally 一起清（2026-10-10 修）。
    return (await response.json()) as T
  } catch (err) {
    // 这里接住的是**没拿到响应**的失败：超时 / 连接被重置 / 断网 / DNS 失败。
    // 不接的话浏览器把它抛成 `TypeError: Failed to fetch` 直接显示在页面上
    //（2026-10-10 用户看到的就是这句）—— 对着英文原文没法判断该做什么，而且看着像
    // 服务器在超时，实际是**请求根本没走完**。所以翻成人话，并带上出错的接口名。
    if (err instanceof DOMException && err.name === 'AbortError') {
      // 调用方主动取消（切票 / 卸载）时原样抛出，交给上层按「这次请求不再需要」处理，
      // 别谎报成超时。
      if (!timedOut) throw err
      throw new Error(`请求超时（${timeoutMs / 1000} 秒无响应）：${path}`)
    }
    if (err instanceof TypeError) {
      throw new Error(`网络中断或超时，请重试：${path}`)
    }
    throw err
  } finally {
    clearTimeout(timer)
    if (callerSignal) callerSignal.removeEventListener('abort', abortFromCaller)
  }
}

/**
 * 把后端的 `detail` 拼成给人看的文案。
 *
 * - 字符串：原样返回（业务错误都是这种，如「暂无数据，请先执行采集」）。
 * - 数组：FastAPI 422 校验错误的形态，每项是 `{loc, msg, type}`。直接 `String()` 会
 *   显示成 `[object Object]`（2026-10-10 用户看到的就是这个），所以把 `loc` 与 `msg` 拼起来。
 * - 其它：返回 null，让调用方退回状态码文案。
 */
function formatDetail(detail: unknown): string | null {
  if (typeof detail === 'string' && detail) return detail
  if (Array.isArray(detail)) {
    const lines = detail
      .map((item) => {
        if (typeof item === 'string') return item
        if (item && typeof item === 'object') {
          const { loc, msg } = item as { loc?: unknown; msg?: unknown }
          // loc 形如 ['body', 'username']，去掉来源前缀（body/query/path）只留下字段名
          const where = Array.isArray(loc)
            ? loc
                .filter((part) => part !== 'body' && part !== 'query' && part !== 'path')
                .join('.')
            : ''
          return [where, typeof msg === 'string' ? msg : ''].filter(Boolean).join('：')
        }
        return ''
      })
      .filter(Boolean)
    if (lines.length > 0) return lines.join('；')
  }
  return null
}

/** POST / PUT / PATCH 带 JSON body。抽出来只为少写三行样板。 */
function send<T>(
  method: 'POST' | 'PUT' | 'PATCH',
  path: string,
  body?: unknown,
  timeoutMs?: number,
): Promise<T> {
  return request<T>(
    path,
    {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    },
    timeoutMs,
  )
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

  /**
   * 个股异动图谱：最近 `days` 个交易日里的涨停 / 涨停炸板 / 中大阳线异动，
   * 按同花顺行业末段分组。**整块一次取回，筛选/搜索都在前端内存里做**，不再发请求。
   *
   * 数据全部来自本站库（涨停池 + 本地日线），不联网、不花配额；口径见后端
   * `api/anomaly_map.py`。
   */
  anomalyMap: (days = 22) => request<AnomalyMap>(`/anomaly-map?days=${days}`),

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
    request<{ ok: boolean }>(`/watchlist/${encodeURIComponent(code)}`, { method: 'DELETE' }),

  updateWatchlistNote: (code: string, note: string) =>
    request<WatchlistRow>(`/watchlist/${encodeURIComponent(code)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code, note }),
    }),

  syncWatchlist: (days = 250) =>
    request<{ rows: number }>(`/watchlist/sync?days=${days}`, { method: 'POST' }, LONG_TIMEOUT_MS),

  stockProfile: (code: string) =>
    request<StockProfile>(`/stock/${encodeURIComponent(code)}`),

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
      `/stock/${encodeURIComponent(code)}/daily?days=${days}&period=${period}&fq=${fq}${vol}`,
    )
  },

  stockThemes: (code: string) =>
    request<StockThemes>(`/stock/${encodeURIComponent(code)}/themes`),

  /**
   * 个股的 **DDE 与主力净流入**（iFinD 口径，日频，单位元）。
   *
   * `days` 是「最近 N 个交易日」（后端 5~100，默认 60）。这两个指标只有 iFinD 有；
   * 库里没有最近交易日的数据时后端会**现取一次**（花 1 次配额），之后读库。
   */
  stockDde: (code: string, days = 60) =>
    request<StockDde>(`/stock/${encodeURIComponent(code)}/dde?days=${days}`),

  /**
   * 个股新闻（**东财口径**，按发布时间倒序）。
   *
   * 后端**每次打开都现取、不落库**（新闻的价值全在「新」，而这条源没有配额）；
   * 取不到时 `rows` 为空、`note` 说明原因 —— 别把两者混成「这只票没有新闻」。
   * `hidden` 是「只在正文里提到代码」的名单类稿件的条数（有标题命中时被筛掉），
   * 页面上要标出来，别静默丢。
   */
  stockNews: (code: string, limit = 20) =>
    request<StockNews>(`/stock/${encodeURIComponent(code)}/news?limit=${limit}`),

  /**
   * 个股分析：把「代码 / 名称 / 拼音首字母」解析成结论（阶段判定 + 旁证）。
   *
   * 解析不出或匹配到多只时后端返回 400，`detail` 就是给用户看的那句话
   * （如「「平安」匹配到多只：…，请输入完整代码」）—— 直接显示，不用自己拼文案。
   */
  analysisLookup: (q: string) =>
    request<StockAnalysis>(`/analysis/lookup?q=${encodeURIComponent(q)}`),

  /**
   * 悟道六池的「成绩单」：收益（1/3/5/10 日）+ 到线率 + 破位率。
   *
   * 与 `patternTrack` 的分工：那个问「全形态混合的评分前 50 只后来怎么样」，
   * 这个问「**这个池子**后来怎么样」—— 几百只的安静型池子进不了前 50，只能按池子算。
   * ⚠️ 统计起点是 2026-10-08（池子的候选范围定稿那天），样本会随每天扫描慢慢攒。
   *
   * **当前前端无界面调用（保留后端契约）**：`/api/patterns/standing` 后端仍在，
   * 但站内已没有页面展示这张成绩单，保留此方法与类型只为不漏掉接口。
   */
  poolStanding: (cohorts = 30, trackDays = 10, lineDays = 5) =>
    request<PoolStandingOut>(
      `/patterns/standing?cohorts=${cohorts}&track_days=${trackDays}&line_days=${lineDays}`,
    ),

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
    request<{ rows: number }>(
      `/stock/${encodeURIComponent(code)}/sync?days=${days}`,
      { method: 'POST' },
      LONG_TIMEOUT_MS,
    ),

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
      LONG_TIMEOUT_MS,
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

  /**
   * 「我是谁」。未登录时 `request` 会派发 `UNAUTHORIZED_EVENT`，由 `AuthProvider`
   * 清空用户 → 下一次渲染 `RequireAuth` 自动跳登录页；本调用自身也会失败（被
   * `AuthProvider` 的 catch 当作「没登录」处理）。
   *
   * ⚠️ 用**更短**的超时（`AUTH_CHECK_TIMEOUT_MS`）：它挡在整页渲染之前，
   * 干等多久就是白屏多久（2026-10-10 修）。
   */
  authMe: () => request<Me>('/auth/me', undefined, AUTH_CHECK_TIMEOUT_MS),

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
