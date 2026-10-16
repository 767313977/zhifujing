import { useMemo } from 'react'
import type { Breadth } from '../api/types'
import EChart from './EChart'
import type { ChartOption } from './EChart'
import Panel from './Panel'
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
import { fmtNum, fmtShortDate } from '../lib/format'

/**
 * 市场宽度温度计面板：指数上涨有没有「广度」支撑。
 *
 * 两组维度（都是后端 `breadth_daily` 的原始值，前端不再加工）：
 * - 站上 MA20 / MA60 / MA250（年线）的个股占比 —— 三条 0–100 折线，左轴固定刻度；
 * - 创 60 / 250 日新高新低家数 —— 图上只画 60 日的「新高 − 新低」柱（红正绿负），
 *   250 日那组放在顶部 chips 里（家数量级小，画柱会压成一条平线）。
 *
 * `above_ma250` 在站内 K 线历史还不够一年时**整列为 null**（不满 N 根 K 线的票不进
 * 分母，后端口径），折线自然断开 —— 不补 0：0% 是「全市场都在年线之下」，那是个
 * 截然相反的结论。
 */

/** 三条占比线的配色：刻意避开红绿（那两个颜色在本站有涨跌含义），用多序列调色板 */
const MA20_COLOR = SERIES_PALETTE[1] // 蓝
const MA60_COLOR = SERIES_PALETTE[0] // 金
const MA250_COLOR = SERIES_PALETTE[2] // 紫

const MA20_NAME = 'MA20上方'
const MA60_NAME = 'MA60上方'
const MA250_NAME = '年线上方'
const DIFF_NAME = '60日新高−新低'

/** 顶部一枚小读数（label + value）。占比类不带颜色，新高红、新低绿。 */
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

/** 占比值：null 显示「—」，否则一位小数加 % */
function pctText(value: number | null): string {
  return value == null ? '—' : `${fmtNum(value, 1)}%`
}

/** 家数值：null 显示「—」 */
function countText(value: number | null): string {
  return value == null ? '—' : `${value}家`
}

export default function BreadthThermometerPanel({
  data,
  loading = false,
  delay = 0,
}: {
  data: Breadth | null
  /** 取数中：显示「加载中…」而不是「暂无数据」 */
  loading?: boolean
  delay?: number
}) {
  const latest = data?.latest ?? null

  const option = useMemo<ChartOption>(() => {
    if (!data || data.dates.length === 0) return {}
    const shortDates = data.dates.map(fmtShortDate)
    // 60 日新高 − 新低差值柱：任一侧为 null（历史不足）时整根为 null，图上留空
    const diff = data.high60.map((high, i) => {
      const low = data.low60[i]
      return high == null || low == null ? null : high - low
    })
    return {
      grid: { ...GRID, top: 30, bottom: 34 },
      legend: {
        ...LEGEND,
        top: 0,
        right: 0,
        data: [MA20_NAME, MA60_NAME, MA250_NAME, DIFF_NAME],
      },
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
          const diffValue = diff[i]
          return [
            items[0].axisValue,
            `● ${MA20_NAME}: ${pctText(data.above_ma20[i])}`,
            `● ${MA60_NAME}: ${pctText(data.above_ma60[i])}`,
            `● ${MA250_NAME}: ${pctText(data.above_ma250[i])}`,
            `60日新高 / 新低: ${data.high60[i] ?? '—'} / ${data.low60[i] ?? '—'} 家`,
            `● ${DIFF_NAME}: ${diffValue == null ? '—' : `${diffValue >= 0 ? '+' : ''}${diffValue}`} 家`,
          ].join('<br/>')
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
          // 固定 0–100：占比是绝对刻度，纵轴自适应会让「50%」看起来忽高忽低
          min: 0,
          max: 100,
          interval: 25,
          axisLabel: { ...AXIS_LABEL, formatter: '{value}%' },
          splitLine: SPLIT_LINE,
          axisLine: { show: false },
        },
        {
          type: 'value' as const,
          // 差值柱走右轴：家数量级与占比完全不同，不同 0 起（差值有正有负）
          scale: true,
          position: 'right' as const,
          axisLabel: { ...AXIS_LABEL, formatter: '{value}' },
          splitLine: { show: false },
          axisLine: { show: false },
        },
      ],
      series: [
        {
          type: 'line' as const,
          name: MA20_NAME,
          data: data.above_ma20,
          smooth: false,
          symbol: 'none' as const,
          lineStyle: { width: 1.6, color: MA20_COLOR },
          itemStyle: { color: MA20_COLOR },
          // 半仓线：占比 50% 是多空分界，虚线标出来
          markLine: {
            silent: true,
            symbol: 'none' as const,
            data: [
              {
                yAxis: 50,
                lineStyle: { color: withAlpha(CHART.fgDim, 0.55), type: 'dashed' as const, width: 1 },
                label: { ...AXIS_LABEL, show: true, position: 'end' as const, formatter: '50%', fontSize: 11 },
              },
            ],
          },
        },
        {
          type: 'line' as const,
          name: MA60_NAME,
          data: data.above_ma60,
          smooth: false,
          symbol: 'none' as const,
          lineStyle: { width: 1.6, color: MA60_COLOR },
          itemStyle: { color: MA60_COLOR },
        },
        {
          type: 'line' as const,
          name: MA250_NAME,
          // 历史不足一年时整列为 null，图上自然断线（不补 0）
          data: data.above_ma250,
          smooth: false,
          symbol: 'none' as const,
          lineStyle: { width: 2, color: MA250_COLOR },
          itemStyle: { color: MA250_COLOR },
        },
        {
          type: 'bar' as const,
          name: DIFF_NAME,
          yAxisIndex: 1,
          data: diff,
          barMaxWidth: 8,
          // 红正绿负：新高多于新低是广度在扩张
          itemStyle: {
            color: (params: { value?: unknown }) =>
              typeof params.value === 'number' && params.value < 0 ? CHART.down : CHART.up,
          },
        },
      ],
    }
  }, [data])

  if (!data || !latest) {
    return (
      <Panel title="市场宽度温度计" meta={<span>站上均线占比 · 新高新低</span>} delay={delay}>
        <div className="px-4 py-8 text-center text-[14px] text-fg-dim">
          {loading ? <span className="pulse-soft">加载中…</span> : '暂无数据，请先执行采集或回补'}
        </div>
      </Panel>
    )
  }

  return (
    <Panel
      title="市场宽度温度计"
      meta={
        <span className="num">
          {latest.trade_date} · 有效样本 {latest.universe_count ?? '—'} 只 · 库内 {data.coverage} 天
        </span>
      }
      delay={delay}
    >
      {/* 顶部读数：占比三档 + 两组新高新低 */}
      <div className="flex flex-wrap gap-x-7 gap-y-3 px-4 py-4">
        <Chip label="站上 MA20" value={pctText(latest.above_ma20)} hint="收盘价站上 20 日均线的个股占比" />
        <Chip label="站上 MA60" value={pctText(latest.above_ma60)} hint="收盘价站上 60 日均线的个股占比" />
        <Chip
          label="站上年线"
          value={pctText(latest.above_ma250)}
          hint="收盘价站上 250 日均线（年线）的个股占比；站内历史不足一年时为空"
        />
        <Chip label="60日新高" value={countText(latest.high60)} color={CHART.up} hint="当日收盘创 60 日新高的家数" />
        <Chip label="60日新低" value={countText(latest.low60)} color={CHART.down} hint="当日收盘创 60 日新低的家数" />
        <Chip label="250日新高" value={countText(latest.high250)} color={CHART.up} hint="当日收盘创 250 日新高的家数" />
        <Chip label="250日新低" value={countText(latest.low250)} color={CHART.down} hint="当日收盘创 250 日新低的家数" />
      </div>

      {data.dates.length > 0 && (
        <div className="border-t border-line-soft px-2 pt-2">
          <EChart option={option} height={300} />
        </div>
      )}
    </Panel>
  )
}
