import { useMemo } from 'react'
import type { Erp } from '../api/types'
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
  SPLIT_LINE,
  TOOLTIP,
  withAlpha,
} from '../lib/chart'
import { fmtNum, fmtShortDate } from '../lib/format'

/**
 * ERP 股债性价比面板：沪深300 盈利收益率（100/PE-TTM）− 10Y 国债收益率。
 *
 * **ERP 越高 = 股票相对债券越便宜**。单看 ERP 绝对值跨年代不可比（无风险利率的
 * 中枢在变），所以图上叠一条它的**滚动 250 交易日分位**（右轴 0–100，虚线）：
 * 分位 >80 说明股票处在过去一年里相对债券最便宜的一档，<20 则最贵。
 *
 * chips：最新一天的 ERP / 分位 / PE-TTM / 10Y 国债。
 */

const ERP_NAME = 'ERP（百分点）'
const PCT_NAME = '历史分位'

/** 百分数：null 显示「—」 */
function pctText(value: number | null, digits = 2): string {
  return value == null ? '—' : `${fmtNum(value, digits)}%`
}

/** 顶部一枚小读数（label + value），与宽度面板的 Chip 同款 */
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

export default function ErpPanel({
  data,
  loading = false,
  error = null,
  delay = 0,
}: {
  data: Erp | null
  /** 取数中：显示「加载中…」而不是「暂无数据」 */
  loading?: boolean
  /** 请求失败原因（404 由调用方过滤，算「暂无数据」）；有值时不显示「暂无数据」 */
  error?: string | null
  delay?: number
}) {
  const latest = data?.latest ?? null

  const option = useMemo<ChartOption>(() => {
    if (!data || data.dates.length === 0) return {}
    const shortDates = data.dates.map(fmtShortDate)
    return {
      grid: { ...GRID, top: 30, bottom: 34 },
      legend: { ...LEGEND, top: 0, right: 0, data: [ERP_NAME, PCT_NAME] },
      // 底部缩放条，与恐贪 / 宽度 / 基差图同一套
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
          return [
            items[0].axisValue,
            `● ${ERP_NAME}: ${data.erp[i] == null ? '—' : `${fmtNum(data.erp[i]!, 2)}`} 百分点`,
            `● ${PCT_NAME}: ${pctText(data.percentile[i], 1)}`,
            `PE-TTM: ${data.pe_ttm[i] == null ? '—' : fmtNum(data.pe_ttm[i]!, 2)} 倍`,
            `10Y 国债: ${pctText(data.bond_yield[i])}`,
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
          scale: true,
          axisLabel: { ...AXIS_LABEL, formatter: '{value}' },
          splitLine: SPLIT_LINE,
          axisLine: { show: false },
        },
        {
          type: 'value' as const,
          // 分位固定 0–100：分位是绝对刻度，纵轴自适应会让「80」看起来忽高忽低
          min: 0,
          max: 100,
          interval: 25,
          position: 'right' as const,
          axisLabel: { ...AXIS_LABEL, formatter: '{value}' },
          splitLine: { show: false },
          axisLine: { show: false },
        },
      ],
      series: [
        {
          type: 'line' as const,
          name: ERP_NAME,
          data: data.erp,
          smooth: false,
          symbol: 'none' as const,
          lineStyle: { width: 1.8, color: CHART.accent },
          itemStyle: { color: CHART.accent },
        },
        {
          type: 'line' as const,
          name: PCT_NAME,
          yAxisIndex: 1,
          data: data.percentile,
          smooth: false,
          symbol: 'none' as const,
          lineStyle: { width: 1.4, color: CHART.fgDim, type: 'dashed' as const },
          itemStyle: { color: CHART.fgDim },
          // 20 / 80 两档虚线：分位 >80 股票最便宜、<20 最贵（挂在分位线上，右轴刻度）
          markLine: {
            silent: true,
            symbol: 'none' as const,
            data: [20, 80].map((value) => ({
              yAxis: value,
              lineStyle: {
                color: withAlpha(CHART.fgDim, 0.4),
                type: 'dashed' as const,
                width: 1,
              },
              label: { ...AXIS_LABEL, show: true, position: 'end' as const, formatter: `${value}`, fontSize: 11 },
            })),
          },
        },
      ],
    }
  }, [data])

  if (!data || !latest) {
    return (
      <Panel title="ERP 股债性价比" meta={<span>沪深300 盈利收益率 − 10Y 国债</span>} delay={delay}>
        <LoadState loading={loading} error={error} emptyText="暂无数据，请先执行采集或回补" />
      </Panel>
    )
  }

  // 分位配色：>80 股票最便宜（红 = 值得关注）、<20 最贵（绿）；中间用默认色
  const pctColor =
    latest.percentile == null
      ? undefined
      : latest.percentile >= 80
        ? CHART.up
        : latest.percentile <= 20
          ? CHART.down
          : undefined

  return (
    <Panel
      title="ERP 股债性价比"
      meta={
        <span className="num">
          {latest.trade_date} · 越高 = 股票越便宜 · 分位窗口 {data.window} 天 · 库内 {data.coverage} 天
        </span>
      }
      delay={delay}
    >
      {/* 顶部读数：ERP / 分位 / PE-TTM / 10Y */}
      <div className="flex flex-wrap gap-x-7 gap-y-3 px-4 py-4">
        <Chip
          label="ERP"
          value={latest.erp == null ? '—' : `${fmtNum(latest.erp, 2)} 百分点`}
          hint="沪深300 盈利收益率（100/PE-TTM）− 10Y 国债收益率"
        />
        <Chip
          label={`历史分位（${data.window} 天）`}
          value={pctText(latest.percentile, 1)}
          color={pctColor}
          hint="ERP 在滚动 250 个交易日里的分位；>80% 是过去一年最便宜的一档，<20% 最贵"
        />
        <Chip
          label="沪深300 PE-TTM"
          value={latest.pe_ttm == null ? '—' : `${fmtNum(latest.pe_ttm, 2)} 倍`}
          hint="沪深300 滚动市盈率（iFinD EDB）"
        />
        <Chip
          label="10Y 国债"
          value={pctText(latest.bond_yield)}
          hint="中国 10 年期国债收益率（%）"
        />
      </div>

      {data.dates.length > 0 && (
        <div className="border-t border-line-soft px-2 pt-2">
          <EChart option={option} height={300} />
        </div>
      )}
    </Panel>
  )
}
