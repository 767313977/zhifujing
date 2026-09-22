import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { IndexHistory, IndexSeries, Sentiment } from '../api/types'
import Alert from '../components/Alert'
import EChart from '../components/EChart'
import type { ChartOption } from '../components/EChart'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import {
  AXIS_LABEL,
  AXIS_LINE,
  CHART,
  GRID,
  LEGEND,
  SERIES_PALETTE,
  SPLIT_LINE,
  TOOLTIP,
} from '../lib/chart'
import { fmtAmount, fmtShortDate } from '../lib/format'

/** 首日归一为 100，让点位数量级不同的指数能在同一轴上比较。 */
function normalize(values: (number | null)[]): (number | null)[] {
  const base = values.find((value) => value != null && value !== 0)
  if (base == null) return values.map(() => null)
  return values.map((value) =>
    value == null ? null : Number(((value / base) * 100).toFixed(2)),
  )
}

function rangeLabel(dates: string[]): string {
  if (dates.length === 0) return '暂无数据'
  return `${fmtShortDate(dates[0])} ~ ${fmtShortDate(dates[dates.length - 1])} · ${dates.length} 个交易日`
}

interface TooltipItem {
  seriesName: string
  value: number | null
  axisValue: string
}

/** 成交额是 12 位数字，必须换算成万亿才可读；其余序列按原值显示。 */
function axisTooltip(params: unknown): string {
  const items = params as TooltipItem[]
  if (!items?.length) return ''
  const rows = items.map((item) => {
    const text =
      item.seriesName === '两市成交额'
        ? // ECharts 对空值点传给 formatter 的是 '-' 而不是 null，必须按类型判断，
          // 否则 fmtAmount('-') 里 Math.abs 得 NaN、最后 .toFixed 抛异常
          typeof item.value === 'number'
          ? fmtAmount(item.value)
          : '—'
        : item.value == null
          ? '—'
          : String(item.value)
    return `${item.seriesName}：${text}`
  })
  return [items[0].axisValue, ...rows].join('<br/>')
}

export default function SentimentPage() {
  const [history, setHistory] = useState<IndexHistory | null>(null)
  const [sentiment, setSentiment] = useState<Sentiment[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    Promise.all([api.indexHistory(120), api.sentimentSeries(60)])
      .then(([historyData, sentimentData]) => {
        setHistory(historyData)
        // 情绪指标只有数据源保留窗口内的日期才有值，其余为 null
        setSentiment(sentimentData.filter((item) => item.limit_up_count != null))
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false))
  }, [])

  /** 两市成交额 = 上证指数 + 深证成指，与今日复盘口径一致。 */
  const indexOption = useMemo<ChartOption>(() => {
    if (!history) return {}
    const shortDates = history.dates.map(fmtShortDate)
    const amountSeries = ['000001.SH', '399001.SZ']
      .map((code) => history.series.find((item) => item.code === code))
      .filter((item): item is IndexSeries => item != null)

    const amount = history.dates.map((_, i) => {
      const values = amountSeries
        .map((item) => item.amount[i])
        .filter((value): value is number => value != null)
      return values.length ? values.reduce((a, b) => a + b, 0) : null
    })

    return {
      // top 留 46px 给图例：否则图例画在绘图区内、会被高柱压住
      grid: { ...GRID, top: 46 },
      legend: {
        ...LEGEND,
        top: 2,
        // 成交额是柱序列，必须显式写进 legend.data，否则不显示图例
        data: [...history.series.map((item) => item.name ?? item.code), '两市成交额'],
      },
      tooltip: { ...TOOLTIP, trigger: 'axis' as const, formatter: axisTooltip },
      axisPointer: {
        type: 'line' as const,
        lineStyle: { color: CHART.fgDim, type: 'dashed' as const },
      },
      xAxis: {
        type: 'category' as const,
        data: shortDates,
        axisLabel: { ...AXIS_LABEL, interval: Math.max(0, Math.floor(shortDates.length / 10)) },
        axisLine: AXIS_LINE,
        axisTick: { show: false },
        boundaryGap: true,
      },
      yAxis: [
        {
          type: 'value' as const,
          name: '首日=100',
          nameTextStyle: { color: CHART.fgDim, fontSize: 10, fontFamily: AXIS_LABEL.fontFamily },
          scale: true,
          axisLabel: { ...AXIS_LABEL, formatter: '{value}' },
          splitLine: SPLIT_LINE,
          axisLine: { show: false },
        },
        {
          type: 'value' as const,
          name: '成交额',
          nameTextStyle: { color: CHART.fgDim, fontSize: 10, fontFamily: AXIS_LABEL.fontFamily },
          axisLabel: {
            ...AXIS_LABEL,
            formatter: (value: number) => `${(value / 1e12).toFixed(1)}万亿`,
          },
          splitLine: { show: false },
          axisLine: { show: false },
        },
      ],
      series: [
        {
          type: 'bar' as const,
          name: '两市成交额',
          yAxisIndex: 1,
          data: amount,
          itemStyle: { color: 'rgba(138,147,160,0.16)' },
          barMaxWidth: 10,
          z: 0,
        },
        ...history.series.map((item, i) => ({
          type: 'line' as const,
          name: item.name ?? item.code,
          data: normalize(item.close),
          smooth: false,
          symbol: 'none' as const,
          lineStyle: { width: 1.6, color: SERIES_PALETTE[i % SERIES_PALETTE.length] },
          itemStyle: { color: SERIES_PALETTE[i % SERIES_PALETTE.length] },
          z: 2,
        })),
      ],
    }
  }, [history])

  const emotionOption = useMemo<ChartOption>(() => {
    const dates = sentiment.map((item) => fmtShortDate(item.trade_date))
    return {
      grid: { ...GRID, top: 46 },
      legend: { ...LEGEND, top: 2, data: ['涨停', '跌停', '炸板', '封板率'] },
      tooltip: { ...TOOLTIP, trigger: 'axis' as const },
      axisPointer: { type: 'shadow' as const },
      xAxis: {
        type: 'category' as const,
        data: dates,
        axisLabel: AXIS_LABEL,
        axisLine: AXIS_LINE,
        axisTick: { show: false },
      },
      yAxis: [
        {
          type: 'value' as const,
          name: '家数',
          nameTextStyle: { color: CHART.fgDim, fontSize: 10, fontFamily: AXIS_LABEL.fontFamily },
          axisLabel: AXIS_LABEL,
          splitLine: SPLIT_LINE,
          axisLine: { show: false },
        },
        {
          type: 'value' as const,
          name: '封板率 %',
          nameTextStyle: { color: CHART.fgDim, fontSize: 10, fontFamily: AXIS_LABEL.fontFamily },
          min: 0,
          max: 100,
          axisLabel: { ...AXIS_LABEL, formatter: '{value}%' },
          splitLine: { show: false },
          axisLine: { show: false },
        },
      ],
      series: [
        {
          type: 'bar' as const,
          name: '涨停',
          data: sentiment.map((item) => item.limit_up_count),
          itemStyle: { color: CHART.up },
          barMaxWidth: 14,
        },
        {
          type: 'bar' as const,
          name: '跌停',
          data: sentiment.map((item) => item.limit_down_count),
          itemStyle: { color: CHART.down },
          barMaxWidth: 14,
        },
        {
          type: 'bar' as const,
          name: '炸板',
          data: sentiment.map((item) => item.broken_count),
          itemStyle: { color: 'rgba(138,147,160,0.45)' },
          barMaxWidth: 14,
        },
        {
          type: 'line' as const,
          name: '封板率',
          yAxisIndex: 1,
          data: sentiment.map((item) => item.seal_rate),
          symbol: 'circle',
          symbolSize: 5,
          lineStyle: { width: 1.8, color: CHART.accent },
          itemStyle: { color: CHART.accent },
        },
      ],
    }
  }, [sentiment])

  const ladderOption = useMemo<ChartOption>(() => {
    const dates = sentiment.map((item) => fmtShortDate(item.trade_date))
    return {
      grid: { ...GRID, top: 24 },
      tooltip: { ...TOOLTIP, trigger: 'axis' as const },
      xAxis: {
        type: 'category' as const,
        data: dates,
        axisLabel: AXIS_LABEL,
        axisLine: AXIS_LINE,
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value' as const,
        name: '连板',
        nameTextStyle: { color: CHART.fgDim, fontSize: 10, fontFamily: AXIS_LABEL.fontFamily },
        minInterval: 1,
        axisLabel: AXIS_LABEL,
        splitLine: SPLIT_LINE,
        axisLine: { show: false },
      },
      series: [
        {
          type: 'bar' as const,
          name: '最高连板',
          data: sentiment.map((item) => item.max_consecutive),
          itemStyle: { color: CHART.accent },
          barMaxWidth: 14,
        },
      ],
    }
  }, [sentiment])

  const indexDates = history?.dates ?? []
  const emotionDates = sentiment.map((item) => item.trade_date)

  const toolbar = (
    <span className="num hidden text-[12px] text-fg-dim lg:inline">
      {loading ? '加载中…' : `${indexDates.length} / ${emotionDates.length} 天`}
    </span>
  )

  return (
    <Layout toolbar={toolbar}>
      {error && (
        <Alert onClose={() => setError(null)}>{error}</Alert>
      )}

      <div className="space-y-4">
        <Panel
          title="指数走势"
          meta={
            <span className="num">
              {rangeLabel(indexDates)}
              <span className="ml-3 text-fg-dim">首日归一为 100 · 灰色柱为两市成交额</span>
            </span>
          }
          delay={40}
        >
          {loading ? (
            <div className="flex h-[320px] items-center justify-center text-[13px] text-fg-dim">
              <span className="pulse-soft">加载中…</span>
            </div>
          ) : (
            <div className="px-2 pt-2">
              <EChart option={indexOption} height={320} />
            </div>
          )}
        </Panel>

        {/* 区间不同必须显式说明，否则会被当成与上图同一段时间读 */}
        <div className="rise flex items-start gap-2.5 border border-accent/25 bg-accent/[0.04] px-3.5 py-2.5 text-[12px] leading-relaxed text-fg-muted">
          <span className="mt-[3px] h-[6px] w-[6px] shrink-0 bg-accent" />
          <span>
            下方情绪指标的区间比指数短得多：涨停 / 跌停 / 炸板明细的数据源
            <span className="text-fg">只保留最近 15 个交易日</span>
            ，而指数有完整历史。两者是
            <span className="text-fg">并列关系，不是同一时间轴</span>
            ——上图提供长周期背景，下图看当下的情绪强弱。
          </span>
        </div>

        <Panel title="情绪指标" meta={<span className="num">{rangeLabel(emotionDates)}</span>} delay={120}>
          {loading ? (
            <div className="px-4 py-10 text-center text-[13px] text-fg-dim">加载中…</div>
          ) : sentiment.length === 0 ? (
            <div className="px-4 py-10 text-center text-[13px] text-fg-dim">
              暂无情绪数据，请先在「今日复盘」执行采集
            </div>
          ) : (
            <div className="px-2 pt-2">
              <EChart option={emotionOption} height={300} />
            </div>
          )}
        </Panel>

        <Panel title="连板高度" meta={<span className="num">市场高度 · 最高连板数</span>} delay={180}>
          {loading ? (
            <div className="px-4 py-10 text-center text-[13px] text-fg-dim">加载中…</div>
          ) : sentiment.length === 0 ? (
            <div className="px-4 py-10 text-center text-[13px] text-fg-dim">暂无数据</div>
          ) : (
            <div className="px-2 pt-2">
              <EChart option={ladderOption} height={200} />
            </div>
          )}
        </Panel>

        {/* 补一段文字口径说明，避免只看图产生误读 */}
        <div className="panel rise px-4 py-3 text-[12px] leading-relaxed text-fg-dim" style={{ animationDelay: '220ms' }}>
          <div className="mb-1.5 text-fg-muted">口径说明</div>
          <ul className="space-y-1">
            <li>
              · 涨停 / 跌停 / 炸板家数取自涨停板三池，与「今日复盘」同源；
              封板率 = 涨停 ÷（涨停 + 炸板）
            </li>
            <li>
              · 成交额 = 上证指数 + 深证成指，单位见轴标签
            </li>
            <li>
              · 指数涨跌家数与全市场涨跌家数<span className="text-fg-muted">无法回补</span>：
              前者的两个接口对同一指数同一天给出不同数值，后者数据源只提供当日值，
              因此本页不展示历史涨跌家数，避免混入口径不一致的序列
            </li>
          </ul>
        </div>
      </div>
    </Layout>
  )
}
