import { useMemo } from 'react'
import type { FuturesBasis } from '../api/types'
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
 * 期指年化基差面板：IF / IH / IC / IM 主力连续相对现货指数的年化基差。
 *
 * - 图：四条年化折线（%）+ 0 轴虚线。年化 = 基差 × 365 / 距交割日天数，
 *   把不同到期日的基差拉到同一尺度上才可比；
 * - chips：四品种最新一天的**年化 / 原始基差**（原始基差不受换月近似影响）。
 *
 * **负值 = 贴水**（期货低于现货）：贴水越深，中性策略的空头对冲成本越高，
 * 是资金对后市谨慎的信号；年化在交割周换月有 2–3 天近似误差（后端口径），
 * 看长期形态没问题，别抠单日尖刺。
 */

/** 四条线的配色：直接用多序列调色板（避开红绿 —— 那两个颜色在本站有涨跌含义） */
const LINE_COLORS = [SERIES_PALETTE[0], SERIES_PALETTE[1], SERIES_PALETTE[2], SERIES_PALETTE[3]]

/** 带符号百分数：null 显示「—」。负 = 贴水用绿色（down），正 = 升水用红色（up） */
function signedPct(value: number | null): string {
  if (value == null) return '—'
  return `${value >= 0 ? '+' : ''}${fmtNum(value, 2)}%`
}

function valueColor(value: number | null): string {
  if (value == null) return 'var(--color-fg)'
  return value < 0 ? CHART.down : CHART.up
}

export default function FuturesBasisPanel({
  data,
  loading = false,
  delay = 0,
}: {
  data: FuturesBasis | null
  /** 取数中：显示「加载中…」而不是「暂无数据」 */
  loading?: boolean
  delay?: number
}) {
  const latest = data?.latest ?? []

  const option = useMemo<ChartOption>(() => {
    if (!data || data.dates.length === 0) return {}
    const shortDates = data.dates.map(fmtShortDate)
    const names = data.series.map((item) => `${item.code} ${item.name ?? ''}`.trim())
    return {
      grid: { ...GRID, top: 30, bottom: 34 },
      legend: { ...LEGEND, top: 0, right: 0, data: names },
      // 底部缩放条，与恐贪 / 宽度图同一套（250 天全铺开太挤，默认只给最近约 120 天）
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
          const rows = data.series.flatMap((item, s) => {
            const annual = item.annual_pct[i]
            const basis = item.basis_pct[i]
            if (annual == null && basis == null) return []
            return [
              `<span style="color:${LINE_COLORS[s % LINE_COLORS.length]}">●</span> ${item.code}：年化 ${signedPct(annual)} · 基差 ${signedPct(basis)}`,
            ]
          })
          return [items[0].axisValue, ...rows].join('<br/>')
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
      yAxis: {
        type: 'value' as const,
        // 年化基差有正有负，不能 0 起；scale 让轴自适应但给 0 轴虚线定位
        scale: true,
        axisLabel: { ...AXIS_LABEL, formatter: '{value}%' },
        splitLine: SPLIT_LINE,
        axisLine: { show: false },
      },
      series: data.series.map((item, s) => ({
        type: 'line' as const,
        name: names[s],
        data: item.annual_pct,
        smooth: false,
        symbol: 'none' as const,
        lineStyle: { width: 1.6, color: LINE_COLORS[s % LINE_COLORS.length] },
        itemStyle: { color: LINE_COLORS[s % LINE_COLORS.length] },
        // 只在第一条线上挂 0 轴虚线（四条都挂会画四遍）
        ...(s === 0
          ? {
              markLine: {
                silent: true,
                symbol: 'none' as const,
                data: [
                  {
                    yAxis: 0,
                    lineStyle: {
                      color: withAlpha(CHART.fgDim, 0.55),
                      type: 'dashed' as const,
                      width: 1,
                    },
                    label: { ...AXIS_LABEL, show: true, position: 'end' as const, formatter: '0', fontSize: 11 },
                  },
                ],
              },
            }
          : {}),
      })),
    }
  }, [data])

  if (!data || latest.length === 0) {
    return (
      <Panel title="期指年化基差" meta={<span>IF · IH · IC · IM 对冲成本</span>} delay={delay}>
        <div className="px-4 py-8 text-center text-[14px] text-fg-dim">
          {loading ? <span className="pulse-soft">加载中…</span> : '暂无数据，请先执行采集或回补'}
        </div>
      </Panel>
    )
  }

  return (
    <Panel
      title="期指年化基差"
      meta={
        <span className="num">
          {latest[0].trade_date} · 负值 = 贴水（对冲压力） · 库内 {data.coverage} 天
        </span>
      }
      delay={delay}
    >
      {/* 顶部读数：四品种最新年化 / 原始基差 */}
      <div className="flex flex-wrap gap-x-7 gap-y-3 px-4 py-4">
        {latest.map((item) => (
          <div
            key={item.code}
            className="min-w-[96px]"
            title={`${item.name}：年化基差 / 原始基差（距交割 ${item.days_to_expiry ?? '—'} 天）`}
          >
            <div className="text-[11px] tracking-[0.06em] text-fg-dim">
              {item.code} {item.name}
            </div>
            <div
              className="num mt-0.5 text-[16px] font-semibold"
              style={{ color: valueColor(item.annual_pct) }}
            >
              {signedPct(item.annual_pct)}
            </div>
            <div className="num mt-0.5 text-[11px] text-fg-dim">
              基差 {signedPct(item.basis_pct)}
            </div>
          </div>
        ))}
      </div>

      {data.dates.length > 0 && (
        <div className="border-t border-line-soft px-2 pt-2">
          <EChart option={option} height={300} />
        </div>
      )}
    </Panel>
  )
}
