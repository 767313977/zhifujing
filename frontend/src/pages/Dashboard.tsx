import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import type {
  AdminStatus,
  LhbItem,
  LimitPool,
  MarketOverview,
  PatternStock,
  PatternSummary,
  SectorHeat,
  WatchlistRow,
} from '../api/types'
import Alert from '../components/Alert'
import DivergenceNote from '../components/DivergenceNote'
import IndexStrip from '../components/IndexStrip'
import LadderBoard from '../components/LadderBoard'
import Layout from '../components/Layout'
import LhbTable from '../components/LhbTable'
import LimitTable from '../components/LimitTable'
import NotePanel from '../components/NotePanel'
import PatternPanel from '../components/PatternPanel'
import SectorHeatPanel from '../components/SectorHeatPanel'
import SentimentPanel from '../components/SentimentPanel'
import WatchlistPanel from '../components/WatchlistPanel'

interface DashboardData {
  overview: MarketOverview
  up: LimitPool
  down: LimitPool
  broken: LimitPool
  lhb: LhbItem[]
  heat: SectorHeat
  watchlist: WatchlistRow[]
  patterns: PatternStock[]
  patternSummary: PatternSummary | null
  patternError: string | null
}

export default function Dashboard() {
  const [date, setDate] = useState<string | null>(null)
  const [dates, setDates] = useState<string[]>([])
  const [status, setStatus] = useState<AdminStatus | null>(null)
  const [data, setData] = useState<DashboardData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [collecting, setCollecting] = useState(false)

  const load = useCallback(async (target: string | null) => {
    setLoading(true)
    setError(null)
    try {
      const [overview, up, down, broken, lhb, heat, watchlist] = await Promise.all([
        api.overview(target),
        api.limitPool('up', target),
        api.limitPool('down', target),
        api.limitPool('broken', target),
        api.lhb(target),
        api.sectorHeat(target),
        // 自选股是最新行情，不随所选日期变，所以单独取一次即可
        api.watchlist(),
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
      setData({
        overview,
        up,
        down,
        broken,
        lhb,
        heat,
        watchlist,
        patterns,
        patternSummary,
        patternError,
      })
    } catch (err) {
      setData(null)
      setError((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  // 首次进入先取可用日期与采集状态，再取页面数据
  useEffect(() => {
    api.dates().then(setDates).catch(() => setDates([]))
    api.adminStatus().then(setStatus).catch(() => setStatus(null))
  }, [])

  useEffect(() => {
    void load(date)
  }, [date, load])

  const handleCollect = useCallback(async () => {
    setCollecting(true)
    setNotice(null)
    try {
      const result = await api.collect(date)
      const steps = Object.entries(result.steps)
      const failed = steps.filter(([, s]) => s.status !== 'ok')
      // 行数与耗时一起报：只说「成功」的话，某个步骤返回 0 行也会被读成
      // 一切正常 —— 而那正是「采了个寂寞」的样子
      const rows = steps.reduce((sum, [, s]) => sum + s.rows, 0)
      const cost = steps.reduce((sum, [, s]) => sum + s.cost, 0)
      if (failed.length > 0) {
        setError(
          `采集部分失败：${failed.map(([name, s]) => `${name}(${s.message ?? '未知'})`).join('；')}` +
            `｜成功 ${steps.length - failed.length} 个步骤、${rows} 行，用时 ${cost.toFixed(0)}s`,
        )
      } else {
        setNotice(
          `${result.trade_date} 采集完成：${steps.length} 个步骤 · ${rows.toLocaleString('zh-CN')} 行 · 用时 ${cost.toFixed(0)}s`,
        )
      }
      setDate(null)
      setDates(await api.dates())
      setStatus(await api.adminStatus())
      await load(null)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setCollecting(false)
    }
  }, [date, load])

  const toolbar = (
    <>
      <span className="num hidden text-[11px] text-fg-dim lg:inline">
        {status ? `${status.data_days} 个交易日` : '—'}
      </span>
      <select
        value={date ?? ''}
        onChange={(event) => setDate(event.target.value || null)}
        className="num border border-line bg-ink-900 px-2 py-[3px] text-[12px] text-fg outline-none focus:border-fg-dim"
      >
        <option value="">最新</option>
        {/* 倒序渲染：最近的排最上面。原先是升序，展开后要一路滚到底才够得着昨天 */}
        {[...dates].reverse().map((item) => (
          <option key={item} value={item}>
            {item}
          </option>
        ))}
      </select>
      <button
        type="button"
        onClick={handleCollect}
        disabled={collecting}
        className="num border border-line px-2.5 py-[3px] text-[12px] text-fg-muted transition-colors hover:border-fg-dim hover:text-fg disabled:cursor-not-allowed disabled:opacity-40"
      >
        {collecting ? '采集中…' : '采集'}
      </button>
    </>
  )

  return (
    <Layout toolbar={toolbar}>
      {error && (
        <Alert onClose={() => setError(null)}>{error}</Alert>
      )}
      {notice && (
        <Alert tone="accent" onClose={() => setNotice(null)}>
          {notice}
        </Alert>
      )}

      {loading && !data ? (
        <div className="panel flex h-64 items-center justify-center text-[13px] text-fg-dim">
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

          {/* 板块热力排在涨停梯队之前：先看「哪个方向在涨」，再看「涨停梯队里谁接力」 */}
          {(data.heat.selected.total > 0 || data.heat.industry.total > 0) && (
            <SectorHeatPanel heat={data.heat} delay={160} />
          )}

          <LadderBoard
            ladder={data.up.ladder ?? []}
            total={data.up.total}
            loading={loading}
            delay={200}
          />

          <LimitTable type="up" stocks={data.up.stocks} loading={loading} delay={260} />

          {/* 跌停/炸板不并排：这两张表都有 12 列，并排后单列容器装不下，
              会在 1280~1500px 这个常用宽度上溢出。整宽堆叠反而更稳。 */}
          <LimitTable type="down" stocks={data.down.stocks} loading={loading} delay={300} />
          <LimitTable type="broken" stocks={data.broken.stocks} loading={loading} delay={320} />

          <LhbTable items={data.lhb} loading={loading} delay={360} />

          {/* 形态排在这里：前面几块回答「今天发生了什么」，这一块回答「明天盯什么」，
              再往后才是自己的票 */}
          <PatternPanel
            summary={data.patternSummary}
            stocks={data.patterns}
            error={data.patternError}
            delay={380}
          />

          <WatchlistPanel rows={data.watchlist} delay={400} />

          <NotePanel tradeDate={data.overview.trade_date} delay={430} />
        </div>
      ) : (
        !error && (
          <div className="panel px-4 py-10 text-center text-[13px] text-fg-dim">
            暂无数据，点击右上角「采集」拉取当日行情
          </div>
        )
      )}
    </Layout>
  )
}
