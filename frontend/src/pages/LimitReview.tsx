import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { LimitPool, PromotionSeries } from '../api/types'
import EChart from '../components/EChart'
import type { ChartOption } from '../components/EChart'
import LadderBoard from '../components/LadderBoard'
import Layout from '../components/Layout'
import LimitTable from '../components/LimitTable'
import Panel from '../components/Panel'
import { AXIS_LABEL, AXIS_LINE, CHART, GRID, LEGEND, SPLIT_LINE, TOOLTIP } from '../lib/chart'
import { fmtInt, fmtPct, fmtShortDate, toneOf } from '../lib/format'

/** 首封时间分桶。首封越早说明资金越坚决，所以时间分布是强度的直接体现。 */
const TIME_BUCKETS: { label: string; from: number; to: number }[] = [
  { label: '竞价', from: 0, to: 93000 },
  { label: '09:30-10:00', from: 93000, to: 100000 },
  { label: '10:00-10:30', from: 100000, to: 103000 },
  { label: '10:30-11:30', from: 103000, to: 113000 },
  { label: '13:00-13:30', from: 130000, to: 133000 },
  { label: '13:30-14:30', from: 133000, to: 143000 },
  { label: '14:30-15:00', from: 143000, to: 240000 },
]

/** 晋级率只展示样本量足够的档位；4进5 及以上每日基数常只有 1-3 只，会 0/100 反复跳。 */
const CHART_LEVELS = [1, 2, 3]

export default function LimitReview() {
  const [date, setDate] = useState<string | null>(null)
  const [dates, setDates] = useState<string[]>([])
  const [pool, setPool] = useState<LimitPool | null>(null)
  const [broken, setBroken] = useState<LimitPool | null>(null)
  const [promotion, setPromotion] = useState<PromotionSeries | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.dates().then(setDates).catch(() => setDates([]))
    api
      .promotion(15)
      .then(setPromotion)
      .catch(() => setPromotion(null))
  }, [])

  const load = useCallback(async (target: string | null) => {
    setLoading(true)
    setError(null)
    try {
      const [up, brokenPool] = await Promise.all([
        api.limitPool('up', target),
        api.limitPool('broken', target),
      ])
      setPool(up)
      setBroken(brokenPool)
    } catch (err) {
      setPool(null)
      setBroken(null)
      setError((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(date)
  }, [date, load])

  const stocks = useMemo(() => pool?.stocks ?? [], [pool])

  /** 首封时间分桶计数。 */
  const timeDistribution = useMemo(() => {
    const counts = TIME_BUCKETS.map(() => 0)
    for (const stock of stocks) {
      if (!stock.first_seal_time) continue
      const value = Number(stock.first_seal_time)
      const index = TIME_BUCKETS.findIndex(
        (bucket) => value >= bucket.from && value < bucket.to,
      )
      if (index >= 0) counts[index] += 1
    }
    return counts
  }, [stocks])

  /** 按所属行业聚合涨停家数。注意行业名被上游截断为 4 个字。 */
  const industryRanking = useMemo(() => {
    const map = new Map<string, number>()
    for (const stock of stocks) {
      if (!stock.industry) continue
      map.set(stock.industry, (map.get(stock.industry) ?? 0) + 1)
    }
    return [...map.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12)
  }, [stocks])

  const promotionOption = useMemo<ChartOption>(() => {
    if (!promotion || promotion.dates.length === 0) return {}
    const labels = promotion.dates.map(fmtShortDate)
    const seriesFor = (label: string, data: (number | null)[], color: string) => ({
      type: 'line' as const,
      name: label,
      data,
      symbol: 'circle',
      symbolSize: 5,
      connectNulls: false,
      lineStyle: { width: 1.6, color },
      itemStyle: { color },
    })

    const palette = [CHART.up, CHART.accent, '#5b9dff', CHART.fgMuted]
    const series = promotion.levels
      .filter((item) => CHART_LEVELS.includes(item.level))
      .map((item, i) => seriesFor(item.label, item.rates, palette[i]))
    series.push(seriesFor('整体', promotion.overall_rates, palette[3]))

    return {
      grid: { ...GRID, top: 46 },
      legend: {
        ...LEGEND,
        top: 2,
        data: [...series.map((item) => item.name)],
      },
      tooltip: {
        ...TOOLTIP,
        trigger: 'axis' as const,
        // ECharts 对空值点传给 formatter 的是 '-'，不是 null，都要归成占位符
        valueFormatter: (value: unknown) =>
          value == null || value === '-' ? '—' : `${value}%`,
      },
      axisPointer: {
        type: 'line' as const,
        lineStyle: { color: CHART.fgDim, type: 'dashed' as const },
      },
      xAxis: {
        type: 'category' as const,
        data: labels,
        axisLabel: AXIS_LABEL,
        axisLine: AXIS_LINE,
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value' as const,
        name: '晋级率 %',
        nameTextStyle: { color: CHART.fgDim, fontSize: 10, fontFamily: AXIS_LABEL.fontFamily },
        min: 0,
        max: 100,
        axisLabel: { ...AXIS_LABEL, formatter: '{value}%' },
        splitLine: SPLIT_LINE,
        axisLine: { show: false },
      },
      series,
    }
  }, [promotion])

  const timeOption = useMemo<ChartOption>(() => {
    return {
      grid: { ...GRID, top: 18, bottom: 8 },
      tooltip: { ...TOOLTIP, trigger: 'axis' as const, axisPointer: { type: 'shadow' as const } },
      xAxis: {
        type: 'category' as const,
        data: TIME_BUCKETS.map((bucket) => bucket.label),
        axisLabel: { ...AXIS_LABEL, interval: 0, rotate: 30, fontSize: 10 },
        axisLine: AXIS_LINE,
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value' as const,
        minInterval: 1,
        axisLabel: AXIS_LABEL,
        splitLine: SPLIT_LINE,
        axisLine: { show: false },
      },
      series: [
        {
          type: 'bar' as const,
          name: '首封家数',
          data: timeDistribution,
          itemStyle: { color: CHART.up },
          barMaxWidth: 30,
        },
      ],
    }
  }, [timeDistribution])

  const latestOverall = promotion?.overall_rates.at(-1) ?? null
  const latestPromoted = promotion?.overall_promoted.at(-1) ?? null
  const latestBase = promotion?.overall_counts.at(-1) ?? null

  const toolbar = (
    <>
      <span className="num hidden text-[11px] text-fg-dim lg:inline">
        {loading ? '加载中…' : `${stocks.length} 只涨停`}
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
    </>
  )

  return (
    <Layout toolbar={toolbar}>
      {error && (
        <div className="rise mb-4 border border-down/40 bg-down/5 px-4 py-3 text-[13px] text-down">
          {error}
        </div>
      )}

      <div className="space-y-4">
        <Panel
          title="连板晋级率"
          meta={
            <span className="num">
              {promotion && promotion.dates.length > 0
                ? `${fmtShortDate(promotion.dates[0])} ~ ${fmtShortDate(promotion.dates.at(-1)!)} · ${promotion.dates.length} 个交易日`
                : '暂无数据'}
              <span className="ml-3 text-fg-dim">
                昨日 N 板股今日晋级 N+1 板的比例 · 样本不足的高档位已略去
              </span>
            </span>
          }
          delay={40}
        >
          {promotion && promotion.dates.length > 0 ? (
            <>
              <div className="flex flex-wrap items-stretch gap-px border-b border-line-soft bg-line-soft">
                <Stat label="最新一日整体晋级率" value={fmtPct(latestOverall, 1)} tone={toneOf(latestOverall)} />
                <Stat label="昨日涨停基数" value={fmtInt(latestBase)} unit="只" />
                <Stat label="今日晋级" value={fmtInt(latestPromoted)} unit="只" />
                <Stat
                  label="口径"
                  value="整体 = 昨日涨停股今日仍涨停"
                  muted
                />
              </div>
              <div className="px-2 pt-2">
                <EChart option={promotionOption} height={260} />
              </div>
            </>
          ) : (
            <div className="px-4 py-10 text-center text-[13px] text-fg-dim">
              暂无晋级率数据，需要至少 2 个交易日的涨停池
            </div>
          )}
        </Panel>

        <LadderBoard
          ladder={pool?.ladder ?? []}
          total={pool?.total ?? 0}
          delay={100}
        />

        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <Panel title="首封时间分布" meta={<span className="num">首封越早，资金越坚决</span>} delay={160}>
            {stocks.length === 0 ? (
              <div className="px-4 py-10 text-center text-[13px] text-fg-dim">暂无数据</div>
            ) : (
              <div className="px-2 pt-2">
                <EChart option={timeOption} height={240} />
              </div>
            )}
          </Panel>

          <Panel
            title="涨停行业分布"
            meta={<span className="num">口径为涨停板池的所属行业，上游截断为 4 字</span>}
            delay={200}
          >
            {industryRanking.length === 0 ? (
              <div className="px-4 py-10 text-center text-[13px] text-fg-dim">暂无数据</div>
            ) : (
              <IndustryBars items={industryRanking} total={stocks.length} />
            )}
          </Panel>
        </div>

        <LimitTable type="up" stocks={stocks} delay={240} />
        <LimitTable type="broken" stocks={broken?.stocks ?? []} delay={280} />
      </div>
    </Layout>
  )
}

function Stat({
  label,
  value,
  unit,
  tone = 'text-fg',
  muted = false,
}: {
  label: string
  value: string
  unit?: string
  tone?: string
  muted?: boolean
}) {
  return (
    <div className="flex-1 bg-ink-900 px-4 py-2.5">
      <div className="text-[11px] tracking-[0.1em] text-fg-dim">{label}</div>
      <div className={`num mt-1 leading-tight ${muted ? 'text-[12px] text-fg-muted' : `text-[20px] font-medium ${tone}`}`}>
        {value}
        {unit && <span className="ml-1 text-[11px] text-fg-dim">{unit}</span>}
      </div>
    </div>
  )
}

/** 行业分布用横向条形列表而非图表：名称较长，图表轴上放不下。 */
function IndustryBars({ items, total }: { items: [string, number][]; total: number }) {
  const max = Math.max(...items.map(([, count]) => count), 1)
  return (
    // 高度要容得下全部 12 行，否则最后一行会被裁掉半截
    <div className="max-h-[320px] space-y-1 overflow-auto px-4 py-3">
      {items.map(([name, count]) => (
        <div key={name} className="flex items-center gap-2.5">
          <span className="w-16 shrink-0 truncate text-[12px] text-fg-muted" title={name}>
            {name}
          </span>
          <div className="h-[10px] flex-1 bg-ink-800">
            <div
              className="h-full"
              style={{ width: `${(count / max) * 100}%`, backgroundColor: CHART.up }}
            />
          </div>
          <span className="num w-9 shrink-0 text-right text-[12px] text-fg">{count}</span>
          <span className="num w-10 shrink-0 text-right text-[11px] text-fg-dim">
            {total > 0 ? `${Math.round((count / total) * 100)}%` : '—'}
          </span>
        </div>
      ))}
    </div>
  )
}
