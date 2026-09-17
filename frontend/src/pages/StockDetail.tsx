import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api/client'
import type { StockDailyRow, StockProfile } from '../api/types'
import Alert from '../components/Alert'
import EChart from '../components/EChart'
import type { ChartOption } from '../components/EChart'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import { AXIS_LABEL, CHART, SPLIT_LINE, TOOLTIP } from '../lib/chart'
import { fmtAmount, fmtNum, fmtPct, fmtShortDate, toneOf } from '../lib/format'

const MA_WINDOWS = [5, 10, 20]
const MA_COLORS = ['#ffb020', '#5b9dff', '#c084fc']

/** 移动平均。窗口不足或含空值时给 null，ECharts 会自然断线。 */
function movingAverage(values: (number | null)[], window: number): (number | null)[] {
  return values.map((_, index) => {
    if (index + 1 < window) return null
    const slice = values.slice(index + 1 - window, index + 1)
    if (slice.some((value) => value == null)) return null
    const sum = slice.reduce<number>((acc, value) => acc + (value as number), 0)
    return Number((sum / window).toFixed(2))
  })
}

export default function StockDetail() {
  const { code = '' } = useParams<{ code: string }>()
  const [profile, setProfile] = useState<StockProfile | null>(null)
  const [daily, setDaily] = useState<StockDailyRow[]>([])
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(
    async (target: string) => {
      const [p, rows] = await Promise.all([
        api.stockProfile(target),
        api.stockDaily(target, 250),
      ])
      setProfile(p)
      setDaily(rows)
      return p
    },
    [],
  )

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    ;(async () => {
      try {
        const p = await load(code)
        // 本地没有缓存说明是第一次看这只票，自动拉一次，之后走缓存
        if (!cancelled && p.day_count === 0) {
          setSyncing(true)
          await api.syncStock(code, 250)
          if (!cancelled) await load(code)
        }
      } catch (err) {
        if (!cancelled) setError((err as Error).message)
      } finally {
        if (!cancelled) {
          setLoading(false)
          setSyncing(false)
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [code, load])

  const toggleWatch = useCallback(async () => {
    if (!profile) return
    try {
      if (profile.in_watchlist) {
        await api.removeWatchlist(profile.code)
        setNotice('已移出自选')
      } else {
        await api.addWatchlist(profile.code, profile.name ?? undefined)
        setNotice('已加入自选')
      }
      await load(profile.code)
    } catch (err) {
      setError((err as Error).message)
    }
  }, [profile, load])

  const klineOption = useMemo<ChartOption>(() => {
    if (daily.length === 0) return {}
    const dates = daily.map((row) => fmtShortDate(row.trade_date))
    const closes = daily.map((row) => row.close)
    // ECharts 蜡烛图的数据顺序是 [开, 收, 低, 高]
    const candles = daily.map((row) => [row.open, row.close, row.low, row.high])
    const volumes = daily.map((row) => ({
      value: row.volume,
      // 成交量柱跟随当日涨跌染色；涨跌幅缺失时用收盘价与开盘价比较
      itemStyle: {
        color:
          (row.pct_chg ?? (row.close ?? 0) - (row.open ?? 0)) >= 0
            ? 'rgba(255,77,79,0.55)'
            : 'rgba(0,185,107,0.55)',
      },
    }))

    return {
      grid: [
        { left: 8, right: 14, top: 34, height: '56%', containLabel: true },
        { left: 8, right: 14, top: '76%', height: '16%', containLabel: true },
      ],
      legend: {
        top: 2,
        left: 8,
        icon: 'rect',
        itemWidth: 10,
        itemHeight: 10,
        itemGap: 14,
        textStyle: { color: CHART.fgMuted, fontSize: 11 },
        data: [...MA_WINDOWS.map((w) => `MA${w}`), '成交量'],
      },
      tooltip: { ...TOOLTIP, trigger: 'axis', axisPointer: { type: 'cross' } },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      xAxis: [
        {
          type: 'category',
          data: dates,
          gridIndex: 0,
          axisLabel: { ...AXIS_LABEL, interval: Math.max(0, Math.floor(dates.length / 8)) },
          axisLine: { lineStyle: { color: CHART.line } },
          axisTick: { show: false },
        },
        {
          type: 'category',
          data: dates,
          gridIndex: 1,
          axisLabel: { show: false },
          axisLine: { lineStyle: { color: CHART.line } },
          axisTick: { show: false },
        },
      ],
      yAxis: [
        {
          scale: true,
          gridIndex: 0,
          axisLabel: AXIS_LABEL,
          splitLine: SPLIT_LINE,
          axisLine: { show: false },
        },
        {
          gridIndex: 1,
          axisLabel: { ...AXIS_LABEL, fontSize: 10 },
          splitLine: { show: false },
          axisLine: { show: false },
        },
      ],
      series: [
        {
          type: 'candlestick' as const,
          name: 'K线',
          data: candles,
          xAxisIndex: 0,
          yAxisIndex: 0,
          itemStyle: {
            // ECharts 默认是欧美惯例（绿涨红跌），必须覆盖成 A 股的红涨绿跌：
            // color = 阳线（收 ≥ 开），color0 = 阴线
            color: CHART.up,
            color0: CHART.down,
            borderColor: CHART.up,
            borderColor0: CHART.down,
          },
        },
        ...MA_WINDOWS.map((window, index) => ({
          type: 'line' as const,
          name: `MA${window}`,
          data: movingAverage(closes, window),
          xAxisIndex: 0,
          yAxisIndex: 0,
          smooth: true,
          symbol: 'none' as const,
          lineStyle: { width: 1.2, color: MA_COLORS[index] },
          itemStyle: { color: MA_COLORS[index] },
        })),
        {
          type: 'bar' as const,
          name: '成交量',
          data: volumes,
          xAxisIndex: 1,
          yAxisIndex: 1,
          barMaxWidth: 8,
        },
      ],
    }
  }, [daily])

  const latest = profile?.latest
  const toolbar = (
    <>
      {syncing && (
        <span className="num pulse-soft text-[11px] text-accent">同步中…</span>
      )}
      <button
        type="button"
        onClick={() => void toggleWatch()}
        disabled={!profile}
        className="num border border-line px-2.5 py-[3px] text-[12px] text-fg-muted transition-colors hover:border-accent/50 hover:text-accent disabled:cursor-not-allowed disabled:opacity-40"
      >
        {profile?.in_watchlist ? '移出自选' : '加入自选'}
      </button>
    </>
  )

  return (
    <Layout toolbar={toolbar}>
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}
      {notice && (
        <Alert tone="accent" onClose={() => setNotice(null)}>
          {notice}
        </Alert>
      )}

      <div className="space-y-4">
        <Panel
          title={`${profile?.name ?? code}  ${profile?.code ?? ''}`}
          meta={
            <span className="num">
              {profile
                ? `本地缓存 ${profile.day_count} 天 · ${profile.first_date ?? '—'} ~ ${profile.last_date ?? '—'}`
                : '加载中…'}
              <Link to="/watchlist" className="ml-3 text-fg-dim hover:text-fg">
                ← 回自选
              </Link>
            </span>
          }
          delay={40}
        >
          <div className="grid grid-cols-2 overflow-hidden md:grid-cols-5">
            <Cell label="收盘价" value={fmtNum(latest?.close, 2)} tone={toneOf(latest?.pct_chg)} />
            <Cell label="涨跌幅" value={fmtPct(latest?.pct_chg)} tone={toneOf(latest?.pct_chg)} />
            <Cell label="成交量" value={`${fmtAmount(latest?.volume)}股`} />
            <Cell label="成交额" value={fmtAmount(latest?.amount)} />
            <Cell
              label="复盘关联"
              value={`涨停 ${profile?.limit_up_dates.length ?? 0} 次`}
              sub={`龙虎榜 ${profile?.lhb_count ?? 0} 次`}
            />
          </div>

          {latest?.trade_date && (
            <div className="num border-t border-line-soft px-4 py-2 text-[11px] text-fg-dim">
              最新数据 {latest.trade_date}
              <span className="mx-2">·</span>
              开 {fmtNum(latest.open, 2)}
              <span className="mx-2">·</span>
              高 {fmtNum(latest.high, 2)}
              <span className="mx-2">·</span>
              低 {fmtNum(latest.low, 2)}
            </div>
          )}
        </Panel>

        <Panel
          title="日 K 线"
          meta={
            <span className="num">
              近 {daily.length} 个交易日 · 前复权未做，红涨绿跌
            </span>
          }
          delay={80}
        >
          {loading || syncing ? (
            <div className="flex h-[420px] items-center justify-center text-[13px] text-fg-dim">
              <span className="pulse-soft">
                {syncing ? '首次打开，正在同步日线…' : '加载中…'}
              </span>
            </div>
          ) : daily.length === 0 ? (
            <div className="px-4 py-10 text-center text-[13px] text-fg-dim">
              本地没有该股的日线，可点右上角「加入自选」后同步
            </div>
          ) : (
            <div className="px-2 pt-2">
              <EChart option={klineOption} height={420} />
            </div>
          )}
        </Panel>

        <Panel
          title="涨停记录"
          meta={<span className="num">该股上过涨停池的日期（数据源只保留最近 15 个交易日）</span>}
          delay={120}
        >
          {!profile || profile.limit_up_dates.length === 0 ? (
            <div className="px-4 py-6 text-center text-[12px] text-fg-dim">
              窗口内没有涨停记录
            </div>
          ) : (
            <div className="flex flex-wrap gap-1.5 px-4 py-3">
              {profile.limit_up_dates.map((day) => (
                <span
                  key={day}
                  className="num border border-line-soft bg-ink-850 px-2 py-1 text-[11px] text-up"
                >
                  {day}
                </span>
              ))}
            </div>
          )}
        </Panel>
      </div>
    </Layout>
  )
}

function Cell({
  label,
  value,
  tone = 'text-fg',
  sub,
}: {
  label: string
  value: string
  tone?: string
  sub?: string
}) {
  return (
    <div className="relative -mr-px -mb-px border-r border-b border-line-soft px-4 py-3">
      <div className="text-[11px] tracking-[0.1em] text-fg-dim">{label}</div>
      <div className={`num mt-1.5 text-[18px] leading-tight font-medium ${tone}`}>
        {value}
      </div>
      {sub && <div className="num mt-1 text-[11px] text-fg-dim">{sub}</div>}
    </div>
  )
}
