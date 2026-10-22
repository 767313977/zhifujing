import { useMemo } from 'react'
import type { NhNl } from '../api/types'
import EChart from './EChart'
import type { ChartOption } from './EChart'
import Panel from './Panel'
import LoadState from './LoadState'
import {
  AXIS_LABEL,
  AXIS_LINE,
  CHART,
  GRID,
  LEGEND,
  SERIES_PALETTE,
  SPLIT_LINE,
  TOOLTIP,
  withAlpha,
} from '../lib/chart'
import { fmtShortDate } from '../lib/format'

/**
 * NH-NL 新高新低扩散面板：指数新高有没有「广度」支撑。
 *
 * 原始值（创 60/250 日收盘新高新低家数）与宽度温度计同一份（`breadth_daily`），
 * 这里画的是宽度面板没有的两个视角：
 * - `diff`：当日「新高 − 新低」家数（顶部 chips）；
 * - `cum`：diff 的**逐日累计净值**（类 AD 线）—— 这才是看顶底背离的那条：
 *   指数收盘创新高而净值线不创新高 = 顶背离，指数新低而净值线不再下探 = 底背离。
 *
 * 图上叠加**上证指数收盘点位**（右轴，与恐贪同一基准），背离肉眼可见。
 * 250 日口径在站内 K 线历史不足一年时**整列为 null**（与宽度温度计同口径），
 * 折线自然断开 —— 不补 0：0 是「新高新低刚好打平」，那是个真实结论。
 */

/** 两条净值线的配色：刻意避开红绿（那两个颜色在本站有涨跌含义），用多序列调色板 */
const CUM60_COLOR = SERIES_PALETTE[0] // 金
const CUM250_COLOR = SERIES_PALETTE[2] // 紫

const CUM60_NAME = '60日净新高累计'
const CUM250_NAME = '250日净新高累计'

/** 顶部一枚小读数（label + value）。与宽度温度计面板同一版式。 */
function Chip({
  label,
  value,
  color,
  hint,
}: {
  label: string
  value: string
  color?: string
  hint?: string
}) {
  return (
    <div className="min-w-[88px]" title={hint}>
      <div className="text-[11px] tracking-[0.06em] text-fg-dim">{label}</div>
      <div className="num mt-0.5 text-[16px] font-semibold" style={{ color: color ?? 'var(--color-fg)' }}>
        {value}
      </div>
    </div>
  )
}

/** 差值：null 显示「—」，否则带符号 + 家；正红负绿（扩张/收缩） */
function diffText(value: number | null): string {
  return value == null ? '—' : `${value >= 0 ? '+' : ''}${value}家`
}

function diffColor(value: number | null): string | undefined {
  if (value == null) return undefined
  return value >= 0 ? CHART.up : CHART.down
}

/** 家数对：「12 / 3 家」（新高 / 新低） */
function pairText(high: number | null, low: number | null): string {
  return high == null && low == null ? '—' : `${high ?? '—'} / ${low ?? '—'} 家`
}

/** 指数点位是四位数，轴标签与 tooltip 带千分位才读得快（与恐贪面板同一处理） */
function withComma(value: number): string {
  return Math.round(value).toLocaleString('en-US')
}

export default function NhNlPanel({
  data,
  loading = false,
  error = null,
  delay = 0,
}: {
  data: NhNl | null
  /** 取数中：显示「加载中…」而不是「暂无数据」 */
  loading?: boolean
  /** 请求失败原因（404 由调用方过滤，算「暂无数据」）；有值时不显示「暂无数据」 */
  error?: string | null
  delay?: number
}) {
  const latest = data?.latest ?? null

  const option = useMemo<ChartOption>(() => {
    if (!data || data.dates.length === 0) return {}
    const benchmark = data.benchmark
    const shortDates = data.dates.map(fmtShortDate)
    const legendData = benchmark
      ? [CUM60_NAME, CUM250_NAME, benchmark.name]
      : [CUM60_NAME, CUM250_NAME]
    return {
      grid: { ...GRID, top: 30, bottom: 34 },
      legend: { ...LEGEND, top: 0, right: 0, data: legendData },
      // 底部缩放条，与恐贪图同一套（250 天全铺开太挤，默认只给最近约 120 天）
      dataZoom: [
        {
          type: 'slider' as const,
          xAxisIndex: 0,
          bottom: 4,
          height: 16,
          start: 52,
          end: 100,
          brushSelect: false,
          borderColor: CHART.line,
          fillerColor: withAlpha(CHART.accent, 0.12),
          dataBackground: {
            lineStyle: { color: CHART.line },
            areaStyle: { color: CHART.soft },
          },
          handleStyle: { color: CHART.ink, borderColor: CHART.fgDim },
          textStyle: AXIS_LABEL,
        },
      ],
      tooltip: {
        ...TOOLTIP,
        trigger: 'axis' as const,
        formatter: (params: unknown) => {
          const items = params as { axisValue: string; dataIndex: number }[]
          if (!items?.length) return ''
          const i = items[0].dataIndex
          const rows = [
            items[0].axisValue,
            `● ${CUM60_NAME}: ${data.cum60[i] == null ? '—' : `${data.cum60[i]! >= 0 ? '+' : ''}${data.cum60[i]}`} 家`,
            `● ${CUM250_NAME}: ${data.cum250[i] == null ? '—' : `${data.cum250[i]! >= 0 ? '+' : ''}${data.cum250[i]}`} 家`,
            `当日60日新高 / 新低: ${data.diff60[i] == null ? '—' : diffText(data.diff60[i])}`,
            `当日250日新高 / 新低: ${data.diff250[i] == null ? '—' : diffText(data.diff250[i])}`,
          ]
          if (benchmark) {
            const close = benchmark.close[i]
            rows.push(`● ${benchmark.name}（收盘点位）: ${close == null ? '—' : withComma(close)}`)
          }
          return rows.join('<br/>')
        },
      },
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
      },
      yAxis: [
        {
          type: 'value' as const,
          name: '累计家数',
          nameTextStyle: { color: CHART.fgDim, fontSize: 11, fontFamily: AXIS_LABEL.fontFamily },
          // 不从 0 起：累计净值有正有负，0 轴强拉会把形态压扁
          scale: true,
          axisLabel: { ...AXIS_LABEL, formatter: '{value}' },
          splitLine: SPLIT_LINE,
          axisLine: { show: false },
        },
        // 叠加的指数线走右轴。**没有 benchmark 时整根轴不出现** —— 空轴白占一条刻度
        ...(benchmark
          ? [
              {
                type: 'value' as const,
                scale: true,
                position: 'right' as const,
                axisLabel: { ...AXIS_LABEL, formatter: (value: number) => withComma(value) },
                splitLine: { show: false },
                axisLine: { show: false },
              },
            ]
          : []),
      ],
      series: [
        {
          type: 'line' as const,
          name: CUM60_NAME,
          data: data.cum60,
          smooth: false,
          symbol: 'none' as const,
          lineStyle: { width: 1.6, color: CUM60_COLOR },
          itemStyle: { color: CUM60_COLOR },
        },
        {
          type: 'line' as const,
          name: CUM250_NAME,
          // 历史不足一年时整列为 null，图上自然断线（不补 0）
          data: data.cum250,
          smooth: false,
          symbol: 'none' as const,
          lineStyle: { width: 1.6, color: CUM250_COLOR },
          itemStyle: { color: CUM250_COLOR },
        },
        ...(benchmark
          ? [
              {
                type: 'line' as const,
                name: benchmark.name,
                yAxisIndex: 1,
                // 与 dates 严格等长（后端已对齐），缺日留 null 图上断开
                data: benchmark.close,
                smooth: false,
                symbol: 'none' as const,
                lineStyle: { width: 1.4, color: CHART.accent, type: 'dashed' as const },
                itemStyle: { color: CHART.accent },
              },
            ]
          : []),
      ],
    }
  }, [data])

  if (!data || !latest) {
    return (
      <Panel title="NH-NL 新高新低扩散" meta={<span>净新高 · 顶底背离</span>} delay={delay}>
        <LoadState loading={loading} error={error} emptyText="暂无数据，请先执行采集或回补" />
      </Panel>
    )
  }

  return (
    <Panel
      title="NH-NL 新高新低扩散"
      meta={
        <span className="num">
          {latest.trade_date} · 与宽度温度计同源 · 库内 {data.coverage} 天
        </span>
      }
      delay={delay}
    >
      {/* 顶部读数：两档差值 + 两档家数对 */}
      <div className="flex flex-wrap gap-x-7 gap-y-3 px-4 py-4">
        <Chip
          label="60日新高−新低"
          value={diffText(latest.diff60)}
          color={diffColor(latest.diff60)}
          hint="当日创 60 日收盘新高家数 − 新低家数；正 = 广度在扩张"
        />
        <Chip
          label="250日新高−新低"
          value={diffText(latest.diff250)}
          color={diffColor(latest.diff250)}
          hint="当日创 250 日收盘新高家数 − 新低家数；站内历史不足一年时为空"
        />
        <Chip
          label="60日新高 / 新低"
          value={pairText(latest.high60, latest.low60)}
          hint="当日收盘创 60 日新高 / 新低的家数"
        />
        <Chip
          label="250日新高 / 新低"
          value={pairText(latest.high250, latest.low250)}
          hint="当日收盘创 250 日新高 / 新低的家数"
        />
      </div>

      {data.dates.length > 0 && (
        <div className="border-t border-line-soft px-2 pt-2">
          <EChart option={option} height={280} />
        </div>
      )}
    </Panel>
  )
}
