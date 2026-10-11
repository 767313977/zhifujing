import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import type {
  AdminStatus,
  LadderBoard,
  LhbItem,
  LimitPool,
  MarketOverview,
  PatternStock,
  PatternSummary,
  PoolType,
  SectorHeat,
  SectorHeatGroup,
  WatchlistRow,
} from '../api/types'
import Alert from '../components/Alert'
import DivergenceNote from '../components/DivergenceNote'
import IndexStrip from '../components/IndexStrip'
import Layout from '../components/Layout'
import LhbTable from '../components/LhbTable'
import LimitPoolBoard from '../components/LimitPoolBoard'
import LimitTable from '../components/LimitTable'
import NotePanel from '../components/NotePanel'
import PatternPanel from '../components/PatternPanel'
import SectorHeatPanel from '../components/SectorHeatPanel'
import SentimentPanel from '../components/SentimentPanel'
import WatchlistPanel from '../components/WatchlistPanel'
import { useAuth } from '../lib/auth'

/** 次级区块取数失败时的占位：空池子 / 空热力，保证主内容渲染时类型仍完整 */
function emptyPool(poolType: PoolType): LimitPool {
  return { trade_date: '', pool_type: poolType, total: 0, ladder: null, stocks: [] }
}

const EMPTY_HEAT_GROUP: SectorHeatGroup = {
  taxonomy: '',
  total: 0,
  rising: 0,
  falling: 0,
  average: null,
  leaders: [],
  laggards: [],
}

const EMPTY_HEAT: SectorHeat = {
  trade_date: '',
  selected: EMPTY_HEAT_GROUP,
  industry: EMPTY_HEAT_GROUP,
}

/** 各次级区块自己的错误信息；null = 该区块取数成功 */
interface SectionErrors {
  up: string | null
  /** 涨停股票池（按昨日档分组）单独一块，与涨停明细的 `up` 分开成败 */
  board: string | null
  down: string | null
  broken: string | null
  lhb: string | null
  heat: string | null
  watchlist: string | null
}

interface DashboardData {
  overview: MarketOverview
  up: LimitPool
  board: LadderBoard | null
  down: LimitPool
  broken: LimitPool
  lhb: LhbItem[]
  heat: SectorHeat
  watchlist: WatchlistRow[]
  sectionErrors: SectionErrors
  patterns: PatternStock[]
  patternSummary: PatternSummary | null
  patternError: string | null
}

/** 次级接口失败时在它原来的位置显示一块错误条（可关），不拖白整页 */
function SectionError({ title, message, onClose }: { title: string; message: string; onClose: () => void }) {
  return (
    <Alert onClose={onClose}>
      {title}加载失败：{message}（其余内容不受影响，可稍后刷新重试）
    </Alert>
  )
}

export default function Dashboard() {
  const { me } = useAuth()
  // 采集状态整个 `/admin/*` 都是管理员接口，普通会员调必 403 —— 只有管理员才问它，
  // 免得每个会员进来都白跑一次（2026-10-10 修）
  const isAdmin = me?.is_admin ?? false
  const [date, setDate] = useState<string | null>(null)
  const [dates, setDates] = useState<string[]>([])
  const [status, setStatus] = useState<AdminStatus | null>(null)
  const [data, setData] = useState<DashboardData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // 被手动关掉的次级区块错误条（切日期重新取数时清空）
  const [dismissed, setDismissed] = useState<Partial<Record<keyof SectionErrors, boolean>>>({})
  const dismiss = (key: keyof SectionErrors) =>
    setDismissed((prev) => ({ ...prev, [key]: true }))

  /**
   * @param isStale 判断这次请求还算不算数。切日期时两个请求的返回顺序不保证，
   *   旧日期的响应晚到会盖掉新日期的数据（各面板没有自己的守卫，见 2026-09-27 修）。
   */
  const load = useCallback(
    async (target: string | null, isStale: () => boolean = () => false) => {
      setLoading(true)
      setError(null)
      // 次级接口各自兜底：失败记错误、回落空值，不拖白整页。
      // overview 不走这里 —— 它是整页的主数据（指数条、情绪、涨跌家数，且 trade_date
      // 是笔记等区块的日期锚点），没它整页无意义，它的失败仍走外层 catch 的整页错误。
      const soft = async <T,>(promise: Promise<T>, fallback: T) => {
        try {
          return { value: await promise, error: null as string | null }
        } catch (err) {
          return { value: fallback, error: (err as Error).message }
        }
      }
      try {
        const [overview, upR, boardR, downR, brokenR, lhbR, heatR, watchR] = await Promise.all([
          api.overview(target),
          soft(api.limitPool('up', target), emptyPool('up')),
          // 涨停股票池按昨日档分组，与「涨停明细」是两份不同的数据，各自独立成败
          soft(api.ladderBoard(target ?? undefined), null as LadderBoard | null),
          soft(api.limitPool('down', target), emptyPool('down')),
          soft(api.limitPool('broken', target), emptyPool('broken')),
          soft(api.lhb(target), [] as LhbItem[]),
          soft(api.sectorHeat(target), EMPTY_HEAT),
          // 自选股是最新行情，不随所选日期变，所以单独取一次即可
          soft(api.watchlist(), [] as WatchlistRow[]),
        ])
        // 形态：榜单只取前 8 只（首页只列 8 行，没必要把 350 多只的明细都传过来），
        // 但家数用 summary 的全量结果。两者故意不同口径 —— 面板上有「全部 →」说明
        // 这是节选。单独 catch：形态是增强内容，拉失败不该把整个首页变成空白
        let patternError: string | null = null
        const [patterns, patternSummary] = await Promise.all([
          api.patternHits(target, 0, 8).catch((err: Error) => {
            // 形态是增强内容，拉失败不该把整个首页变成空白 —— 但也不能悄悄
            // 退回空列表，那样面板会显示成「当日没有命中的形态」
            patternError = err.message
            return []
          }),
          api.patternSummary(target).catch(() => null),
        ])
        if (isStale()) return
        setData({
          overview,
          up: upR.value,
          board: boardR.value,
          down: downR.value,
          broken: brokenR.value,
          lhb: lhbR.value,
          heat: heatR.value,
          watchlist: watchR.value,
          sectionErrors: {
            up: upR.error,
            board: boardR.error,
            down: downR.error,
            broken: brokenR.error,
            lhb: lhbR.error,
            heat: heatR.error,
            watchlist: watchR.error,
          },
          patterns,
          patternSummary,
          patternError,
        })
      } catch (err) {
        if (isStale()) return
        setData(null)
        setError((err as Error).message)
      } finally {
        if (!isStale()) setLoading(false)
      }
    },
    [],
  )

  // 首次进入先取可用日期，再取页面数据
  useEffect(() => {
    api.dates().then(setDates).catch(() => setDates([]))
  }, [])

  useEffect(() => {
    if (!isAdmin) {
      setStatus(null)
      return
    }
    api.adminStatus().then(setStatus).catch(() => setStatus(null))
  }, [isAdmin])

  useEffect(() => {
    let stale = false
    // 切日期先清空：旧日期的数字留在页面上会被当成新日期的数据看
    // （只有 IndexStrip / 情绪 / 板块热力 / 形态 / 自选 / 笔记这几块没接
    //  `loading`，不清的话它们会继续显旧日期的值，与下方「加载中」的表混在一起）
    setData(null)
    // 新一轮取数：上一轮被关掉的错误条标记也清掉
    setDismissed({})
    void load(date, () => stale)
    return () => {
      stale = true
    }
  }, [date, load])

  const toolbar = (
    <>
      {isAdmin && (
        <span className="num hidden text-[13px] text-fg-dim lg:inline">
          {status ? `${status.data_days} 个交易日` : '—'}
        </span>
      )}
      <select
        value={date ?? ''}
        onChange={(event) => setDate(event.target.value || null)}
        className="num border border-line bg-ink-900 px-2 py-[3px] text-[13px] text-fg outline-none focus:border-fg-dim"
      >
        <option value="">最新</option>
        {/* 倒序渲染：最近的排最上面。原先是升序，展开后要一路滚到底才够得着昨天 */}
        {[...dates].reverse().map((item) => (
          <option key={item} value={item}>
            {item}
          </option>
        ))}
      </select>
      {/* 这里原来有个「采集」按钮。**2026-09-28 删掉了**（用户要求「删掉手动采集按钮，
          禁止手动采集」）：它就在落地页工具栏上，点一下就白跑一轮采集（约 50 次
          iFinD 调用）—— 当天真发生过一次误点。采集现在只有自动那一条路：
          每天收盘后跑，错过时刻由启动补采兜住。 */}
    </>
  )

  return (
    <Layout toolbar={toolbar}>
      {error && (
        <Alert onClose={() => setError(null)}>{error}</Alert>
      )}

      {loading && !data ? (
        <div className="panel flex h-64 items-center justify-center text-[14px] text-fg-dim">
          <span className="pulse-soft">加载中…</span>
        </div>
      ) : data ? (
        <div className="space-y-4">
          <IndexStrip
            indexes={data.overview.indexes}
            tradeDate={data.overview.trade_date}
          />

          {/* 判读条紧跟指数条：它的依据就是上面那排指数与下面的涨跌家数 */}
          <DivergenceNote data={data.overview.divergence} />

          {data.overview.sentiment && (
            <SentimentPanel
              sentiment={data.overview.sentiment}
              turnover={data.overview.turnover}
            />
          )}

          {/* 板块热力排在涨停梯队之前：先看「哪个方向在涨」，再看「涨停梯队里谁接力」。
              次级接口失败时在原位置显示错误条（可关，关掉后该区块优雅隐藏） */}
          {data.sectionErrors.heat && !dismissed.heat ? (
            <SectionError
              title="板块热力"
              message={data.sectionErrors.heat}
              onClose={() => dismiss('heat')}
            />
          ) : (
            (data.heat.selected.total > 0 || data.heat.industry.total > 0) && (
              <SectorHeatPanel heat={data.heat} delay={160} />
            )
          )}

          {/* 涨停股票池（按昨日档分组）与涨停明细是两份数据，各自独立成败 */}
          {data.sectionErrors.board ? (
            // 失败未关：显示错误条；已关：整个区块不渲染。失败回落的是 null，
            // 照常渲染的话会被误读成「没有候选票」（2026-10-11 修）。
            // 下面涨停明细/跌停/炸板/龙虎榜/自选是同一套处理，不再重复注释
            !dismissed.board ? (
              <SectionError
                title="涨停股票池"
                message={data.sectionErrors.board}
                onClose={() => dismiss('board')}
              />
            ) : null
          ) : (
            <LimitPoolBoard data={data.board} loading={loading} delay={200} />
          )}

          {data.sectionErrors.up ? (
            !dismissed.up ? (
              <SectionError
                title="涨停明细"
                message={data.sectionErrors.up}
                onClose={() => dismiss('up')}
              />
            ) : null
          ) : (
            <LimitTable type="up" stocks={data.up.stocks} loading={loading} delay={260} />
          )}

          {/* 跌停/炸板不并排：这两张表都有 12 列，并排后单列容器装不下，
              会在 1280~1500px 这个常用宽度上溢出。整宽堆叠反而更稳。 */}
          {data.sectionErrors.down ? (
            !dismissed.down ? (
              <SectionError
                title="跌停明细"
                message={data.sectionErrors.down}
                onClose={() => dismiss('down')}
              />
            ) : null
          ) : (
            <LimitTable type="down" stocks={data.down.stocks} loading={loading} delay={300} />
          )}
          {data.sectionErrors.broken ? (
            !dismissed.broken ? (
              <SectionError
                title="炸板明细"
                message={data.sectionErrors.broken}
                onClose={() => dismiss('broken')}
              />
            ) : null
          ) : (
            <LimitTable type="broken" stocks={data.broken.stocks} loading={loading} delay={320} />
          )}

          {data.sectionErrors.lhb ? (
            !dismissed.lhb ? (
              <SectionError
                title="龙虎榜"
                message={data.sectionErrors.lhb}
                onClose={() => dismiss('lhb')}
              />
            ) : null
          ) : (
            <LhbTable items={data.lhb} loading={loading} delay={360} />
          )}

          {/* 形态排在这里：前面几块回答「今天发生了什么」，这一块回答「明天盯什么」，
              再往后才是自己的票（它本来就有独立的 error 态，维持原样） */}
          <PatternPanel
            summary={data.patternSummary}
            stocks={data.patterns}
            error={data.patternError}
            delay={380}
          />

          {data.sectionErrors.watchlist ? (
            !dismissed.watchlist ? (
              <SectionError
                title="自选股表现"
                message={data.sectionErrors.watchlist}
                onClose={() => dismiss('watchlist')}
              />
            ) : null
          ) : (
            <WatchlistPanel rows={data.watchlist} delay={400} />
          )}

          <NotePanel tradeDate={data.overview.trade_date} delay={430} />
        </div>
      ) : (
        !error && (
          <div className="panel px-4 py-10 text-center text-[14px] text-fg-dim">
            暂无数据 —— 采集是每天收盘后自动跑的（当前 {status?.scheduler.collect_time ?? '—'}），
            还没跑到、或者那一轮有步骤失败时就会是空的。
          </div>
        )
      )}
    </Layout>
  )
}
