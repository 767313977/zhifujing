import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import type { AdminStatus, LhbItem, LimitPool, MarketOverview } from '../api/types'
import Alert from '../components/Alert'
import IndexStrip from '../components/IndexStrip'
import LadderBoard from '../components/LadderBoard'
import Layout from '../components/Layout'
import LhbTable from '../components/LhbTable'
import LimitTable from '../components/LimitTable'
import NotePanel from '../components/NotePanel'
import SentimentPanel from '../components/SentimentPanel'

interface DashboardData {
  overview: MarketOverview
  up: LimitPool
  down: LimitPool
  broken: LimitPool
  lhb: LhbItem[]
}

export default function Dashboard() {
  const [date, setDate] = useState<string | null>(null)
  const [dates, setDates] = useState<string[]>([])
  const [status, setStatus] = useState<AdminStatus | null>(null)
  const [data, setData] = useState<DashboardData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [collecting, setCollecting] = useState(false)

  const load = useCallback(async (target: string | null) => {
    setLoading(true)
    setError(null)
    try {
      const [overview, up, down, broken, lhb] = await Promise.all([
        api.overview(target),
        api.limitPool('up', target),
        api.limitPool('down', target),
        api.limitPool('broken', target),
        api.lhb(target),
      ])
      setData({ overview, up, down, broken, lhb })
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
    try {
      const result = await api.collect(date)
      const failed = Object.entries(result.steps).filter(([, s]) => s.status !== 'ok')
      if (failed.length > 0) {
        setError(
          `采集部分失败：${failed.map(([name, s]) => `${name}(${s.message ?? '未知'})`).join('；')}`,
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
        {dates.map((item) => (
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

          {data.overview.sentiment && (
            <SentimentPanel sentiment={data.overview.sentiment} />
          )}

          <LadderBoard
            ladder={data.up.ladder ?? []}
            total={data.up.total}
            delay={200}
          />

          <LimitTable type="up" stocks={data.up.stocks} delay={260} />

          {/* 跌停/炸板不并排：这两张表都有 12 列，并排后单列容器装不下，
              会在 1280~1500px 这个常用宽度上溢出。整宽堆叠反而更稳。 */}
          <LimitTable type="down" stocks={data.down.stocks} delay={300} />
          <LimitTable type="broken" stocks={data.broken.stocks} delay={320} />

          <LhbTable items={data.lhb} delay={360} />

          <NotePanel tradeDate={data.overview.trade_date} delay={420} />
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
