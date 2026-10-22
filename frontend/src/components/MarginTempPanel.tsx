import { useMemo } from 'react'
import type { MarginTemp } from '../api/types'
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
 * 两融杠杆温度面板：两融余额是「散户与游资加了多少杠杆」最直接的水位。
 *
 * 口径：余额 = `margin_daily` 的沪 + 深「融资余额 + 融券余额」合计（亿元），
 * 从逐日快照**读取时现算**（见 `backend/app/services/margin_temp.py`），不建新表。
 * - 只取**两所都披露**的日子：深市披露比沪市晚一天，只有一所时相加会把
 *   「深市待披露」误报成「深市归零」；
 * - 变化率 = 余额较 20 个观测前的涨跌幅（%）；
 * - **温度 = 变化率的滚动分位（0–100）**，不是余额水平的分位 —— 余额是长期上行的
 *   慢变量，绝对水平的分位几乎恒在 100 附近，没有信息量。
 *
 * 用法：余额持续抬升 = 资金在加杠杆、风险偏好在升；余额掉头 = 去杠杆、
 * 与指数一起看是否背离。
 *
 * 缺失的日子 `balance` 为 null：图上**断线断开**，不补 0 —— 0 是「余额归零」，
 * 那是错的结论；null 只是缺数据。
 */

/** 余额线用金（调色板首位）：避开红绿 —— 那两个颜色在本站有涨跌含义 */
const BALANCE_COLOR = SERIES_PALETTE[0]

const BALANCE_NAME = '两融余额'
const CHG_NAME = '20日变化率'

/** 顶部一枚小读数（label + value）。与其它情绪面板同一版式。 */
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

/** 余额（亿元）→ 万亿，两位小数（两融余额惯用万亿读） */
function wanYi(value: number | null | undefined): string {
  if (value == null) return '—'
  return `${(value / 10000).toFixed(2)}万亿`
}

/** 日增减（亿元）：带正负 + 千分位，加杠杆红、去杠杆绿（与全站涨跌色一致） */
function signedYi(value: number | null | undefined): string {
  if (value == null) return '—'
  const text = Math.abs(value).toLocaleString('en-US', { maximumFractionDigits: 1 })
  return value >= 0 ? `+${text}亿` : `-${text}亿`
}

function signedPct(value: number | null | undefined): string {
  if (value == null) return '—'
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`
}

/** 带符号量的红绿：正红负绿 */
function signedColor(value: number | null | undefined): string | undefined {
  if (value == null) return undefined
  return value >= 0 ? CHART.up : CHART.down
}

/** 温度（0–100）越高越热：偏高用贪婪色、偏低用恐慌色，中段保持中性 */
function tempColor(value: number | null | undefined): string | undefined {
  if (value == null) return undefined
  if (value >= 60) return CHART.up
  if (value <= 40) return CHART.down
  return undefined
}

export default function MarginTempPanel({
  data,
  loading = false,
  error = null,
  delay = 0,
}: {
  data: MarginTemp | null
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
      legend: { ...LEGEND, top: 0, right: 0, data: [BALANCE_NAME, CHG_NAME] },
      // 底部缩放条，与恐贪 / NH-NL 同一套（250 天全铺开太挤，默认给最近约 120 天）
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
            `● ${BALANCE_NAME}: ${data.balance[i] == null ? '—' : wanYi(data.balance[i])}`,
            `● ${CHG_NAME}: ${signedPct(data.chg20[i])}`,
            `● 杠杆温度（${data.window}日分位）: ${
              data.temperature[i] == null ? '—' : data.temperature[i]!.toFixed(1)
            }`,
          ]
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
          name: '两融余额',
          nameTextStyle: { color: CHART.fgDim, fontSize: 11, fontFamily: AXIS_LABEL.fontFamily },
          // 不从 0 起：余额是 2 万亿级的慢变量，0 轴强拉会把加/去杠杆的形态压平
          scale: true,
          axisLabel: { ...AXIS_LABEL, formatter: (value: number) => (value / 10000).toFixed(1) },
          splitLine: SPLIT_LINE,
          axisLine: { show: false },
        },
        {
          type: 'value' as const,
          name: '变化率 %',
          position: 'right' as const,
          nameTextStyle: { color: CHART.fgDim, fontSize: 11, fontFamily: AXIS_LABEL.fontFamily },
          scale: true,
          axisLabel: { ...AXIS_LABEL, formatter: '{value}%' },
          splitLine: { show: false },
          axisLine: { show: false },
        },
      ],
      series: [
        {
          type: 'line' as const,
          name: BALANCE_NAME,
          // 缺数的日子原样传 null（图上断开）：补 0 会把「不知道」画成「归零」
          data: data.balance,
          smooth: false,
          symbol: 'none' as const,
          z: 3,
          lineStyle: { width: 1.8, color: BALANCE_COLOR },
          itemStyle: { color: BALANCE_COLOR },
        },
        {
          type: 'bar' as const,
          name: CHG_NAME,
          yAxisIndex: 1,
          data: data.chg20,
          barMaxWidth: 8,
          // 加杠杆红、去杠杆绿：与全站涨跌色同一套语义
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
      <Panel title="两融杠杆温度" meta={<span>沪深合计余额 · 变化率分位</span>} delay={delay}>
        <LoadState loading={loading} error={error} emptyText="暂无数据，请先执行采集或回补" />
      </Panel>
    )
  }

  return (
    <Panel
      title="两融杠杆温度"
      meta={
        <span className="num">
          {latest.trade_date} · 沪深合计 · 库内可算 {data.coverage} 天
        </span>
      }
      delay={delay}
    >
      {/* 顶部读数：余额 + 日增减 + 20 日变化率 + 温度 */}
      <div className="flex flex-wrap gap-x-7 gap-y-3 px-4 py-4">
        <Chip
          label="两融余额"
          value={wanYi(latest.balance)}
          hint="沪 + 深「融资余额 + 融券余额」合计；只取两所都披露的日子"
        />
        <Chip
          label="较上一日"
          value={signedYi(latest.balance_chg)}
          color={signedColor(latest.balance_chg)}
          hint="余额较前一交易日的增减；正 = 加杠杆"
        />
        <Chip
          label={`${data.chg_days}日变化率`}
          value={signedPct(latest.chg20)}
          color={signedColor(latest.chg20)}
          hint={`余额较 ${data.chg_days} 个观测前的涨跌幅`}
        />
        <Chip
          label="杠杆温度"
          value={latest.temperature == null ? '—' : latest.temperature.toFixed(1)}
          color={tempColor(latest.temperature)}
          hint={`${data.chg_days}日变化率在滚动 ${data.window} 个交易日的分位（0–100），越高越热`}
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
