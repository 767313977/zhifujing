import { useMemo } from 'react'
import type { Obv } from '../api/types'
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
import { fmtAmount, fmtShortDate } from '../lib/format'

/**
 * 量能潮 OBV 面板：量能趋势与指数点位是否背离。
 *
 * 口径（经典 OBV 把「成交量」换成「成交额」——本站盯的是钱的进出）：
 * 上涨日累加上证指数成交额、下跌日扣减，从 0 起累计；基准指数与恐贪同源
 * （上证指数）。另叠一条**成交额的滚动 250 日分位**（右轴 0–100）：OBV 说
 * 「钱在往哪个方向走」，分位说「今天的量在历史上算多大」。
 *
 * 用法：指数创新高而 OBV 不创新高 = 量能跟不上（存量博弈警示）；指数横盘而
 * OBV 持续爬升 = 有资金在悄悄进场。
 */

/** OBV 线用金（调色板首位）：避开红绿 —— 那两个颜色在本站有涨跌含义 */
const OBV_COLOR = SERIES_PALETTE[0]

const OBV_NAME = 'OBV 累计净额'
const PCT_NAME = '成交额分位'

/** 顶部一枚小读数（label + value）。与宽度温度计 / NH-NL 面板同一版式。 */
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

/** 带符号的成交额（当日净额用）：流入红、流出绿，与全站涨跌色一致 */
function signedAmount(value: number | null): string {
  if (value == null) return '—'
  const text = fmtAmount(Math.abs(value))
  return value >= 0 ? `+${text}` : `-${text}`
}

export default function ObvPanel({
  data,
  loading = false,
  error = null,
  delay = 0,
}: {
  data: Obv | null
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
      legend: { ...LEGEND, top: 0, right: 0, data: [OBV_NAME, PCT_NAME] },
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
          return [
            items[0].axisValue,
            `● ${OBV_NAME}: ${data.obv[i] == null ? '—' : signedAmount(data.obv[i])}`,
            `● ${PCT_NAME}: ${data.percentile[i] == null ? '—' : `${data.percentile[i]!.toFixed(1)}%`}`,
            `当日成交额: ${fmtAmount(data.amount[i])}`,
            `当日净额: ${signedAmount(data.obv_change[i])}`,
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
          name: '累计净额',
          nameTextStyle: { color: CHART.fgDim, fontSize: 11, fontFamily: AXIS_LABEL.fontFamily },
          // 不从 0 起：累计净额有正有负，0 轴强拉会把形态压扁
          scale: true,
          axisLabel: {
            ...AXIS_LABEL,
            formatter: (value: number) => `${(value / 1e12).toFixed(1)}万亿`,
          },
          splitLine: SPLIT_LINE,
          axisLine: { show: false },
        },
        {
          type: 'value' as const,
          name: '分位',
          nameTextStyle: { color: CHART.fgDim, fontSize: 11, fontFamily: AXIS_LABEL.fontFamily },
          // 固定 0–100：分位是绝对刻度，纵轴自适应会让「50%」看起来忽高忽低
          min: 0,
          max: 100,
          interval: 25,
          position: 'right' as const,
          axisLabel: { ...AXIS_LABEL, formatter: '{value}%' },
          splitLine: { show: false },
          axisLine: { show: false },
        },
      ],
      series: [
        {
          type: 'line' as const,
          name: OBV_NAME,
          // 成交额缺失的日子原样传 null（图上断开）：补 0 会把「不知道」画成「归零」
          data: data.obv,
          smooth: false,
          symbol: 'none' as const,
          lineStyle: { width: 1.8, color: OBV_COLOR },
          itemStyle: { color: OBV_COLOR },
        },
        {
          type: 'line' as const,
          name: PCT_NAME,
          yAxisIndex: 1,
          // 分位历史不足（窗口内有效值 < 20）的日子为 null，图上断开
          data: data.percentile,
          smooth: false,
          symbol: 'none' as const,
          lineStyle: { width: 1.4, color: CHART.accent },
          itemStyle: { color: CHART.accent },
          // 20 / 80 极端区参考线：>80% 是一年量能最大的一档，<20% 最冷清
          markLine: {
            silent: true,
            symbol: 'none' as const,
            data: [
              {
                yAxis: 80,
                lineStyle: { color: withAlpha(CHART.up, 0.55), type: 'dashed' as const, width: 1 },
                label: { ...AXIS_LABEL, show: true, position: 'end' as const, formatter: '80%', fontSize: 11 },
              },
              {
                yAxis: 20,
                lineStyle: { color: withAlpha(CHART.down, 0.55), type: 'dashed' as const, width: 1 },
                label: { ...AXIS_LABEL, show: true, position: 'end' as const, formatter: '20%', fontSize: 11 },
              },
            ],
          },
        },
      ],
    }
  }, [data])

  if (!data || !latest) {
    return (
      <Panel title="量能潮 OBV" meta={<span>成交额累计净额 · 量价背离</span>} delay={delay}>
        <LoadState loading={loading} error={error} emptyText="暂无数据，请先执行采集或回补" />
      </Panel>
    )
  }

  return (
    <Panel
      title="量能潮 OBV"
      meta={
        <span className="num">
          {latest.trade_date} · 上证指数口径 · 分位窗口 {data.window} 日 · 库内 {data.coverage} 天
        </span>
      }
      delay={delay}
    >
      {/* 顶部读数：成交额 + 分位 + 当日净额 + 累计净额 */}
      <div className="flex flex-wrap gap-x-7 gap-y-3 px-4 py-4">
        <Chip
          label="成交额"
          value={fmtAmount(latest.amount)}
          hint="当日上证指数成交额（与恐贪同一基准指数）"
        />
        <Chip
          label="成交额分位"
          value={latest.percentile == null ? '—' : `${latest.percentile.toFixed(1)}%`}
          hint={`当日成交额在滚动 ${data.window} 个交易日里的位置；>80% 是一年最大的一档，<20% 最冷清`}
        />
        <Chip
          label="当日净额"
          value={signedAmount(latest.obv_change)}
          color={latest.obv_change == null ? undefined : latest.obv_change >= 0 ? CHART.up : CHART.down}
          hint="上涨日 +成交额、下跌日 −成交额、平盘 0"
        />
        <Chip
          label="累计净额"
          value={signedAmount(latest.obv)}
          hint="当日净额从 0 起的累计值；看它与指数点位是否背离"
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
