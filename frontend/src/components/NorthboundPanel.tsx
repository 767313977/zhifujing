import { useMemo } from 'react'
import type { Northbound } from '../api/types'
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
 * 北向活跃度面板：外资参与 A 股的程度。
 *
 * ⚠️ **只有成交额，没有净流入**：2024-08-19 起交易所不再按日披露买入 / 卖出金额，
 * 「北向净流入」在数据源层面已经不存在（见 `backend/app/models.py` 的 `HsgtDaily`）。
 * 所以这里量的是**活跃度 / 参与度，不含方向** —— 图上那条线只说「外资今天参与得多
 * 不多」，不说「在买还是在卖」。
 *
 * 口径：`沪深股通成交额 ÷ 两市成交额 × 100（%）`，读 `hsgt_daily` 与
 * `market_sentiment.total_amount` **读取时现算**（见 `services/northbound.py`），不建新表。
 * 用占比而不是成交额绝对值：绝对值随全市场量能潮起潮落，占比才是归一化后的参与度。
 *
 * 日期骨架取两市成交额的交易日；**通道关闭日没有北向数据，那个点留 null、图上断开**
 * —— 断口是「没数据」，不是「北向没交易」。虚线是上证指数收盘点位，右轴。
 */

/** 占比线用金（调色板首位）：与恐贪 / NH-NL 的基准虚线（accent 蓝）分得开 */
const RATIO_COLOR = SERIES_PALETTE[0]

const RATIO_NAME = '北向成交占比'

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

/** 占比与均值都是百分数 */
function pctText(value: number | null | undefined): string {
  return value == null ? '—' : `${value.toFixed(2)}%`
}

/** 北向成交额（亿元） */
function yiText(value: number | null | undefined): string {
  return value == null ? '—' : `${value.toLocaleString('en-US', { maximumFractionDigits: 0 })}亿`
}

/** 指数点位是四位数，轴标签与 tooltip 带千分位才读得快（与 NH-NL 面板同一处理） */
function withComma(value: number): string {
  return Math.round(value).toLocaleString('en-US')
}

export default function NorthboundPanel({
  data,
  loading = false,
  error = null,
  delay = 0,
}: {
  data: Northbound | null
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
    const legendData = benchmark ? [RATIO_NAME, benchmark.name] : [RATIO_NAME]
    return {
      grid: { ...GRID, top: 30, bottom: 34 },
      legend: { ...LEGEND, top: 0, right: 0, data: legendData },
      // 底部缩放条，与恐贪 / NH-NL 同一套（250 天全铺开太挤，默认只给最近约 120 天）
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
            `● ${RATIO_NAME}: ${pctText(data.ratio[i])}`,
            `北向成交额: ${yiText(data.turnover[i])}`,
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
          name: '占比 %',
          nameTextStyle: { color: CHART.fgDim, fontSize: 11, fontFamily: AXIS_LABEL.fontFamily },
          // 不从 0 起：占比在 10~20% 这个窄区间里，0 轴强拉会把形态压平
          scale: true,
          axisLabel: { ...AXIS_LABEL, formatter: '{value}%' },
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
          name: RATIO_NAME,
          // 通道关闭日原样传 null（图上断开）：补 0 会把「没数据」画成「外资没交易」
          data: data.ratio,
          smooth: false,
          symbol: 'none' as const,
          z: 3,
          lineStyle: { width: 1.8, color: RATIO_COLOR },
          itemStyle: { color: RATIO_COLOR },
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
      <Panel title="北向活跃度" meta={<span>沪深股通成交额 · 外资参与度</span>} delay={delay}>
        <LoadState loading={loading} error={error} emptyText="暂无数据，请先执行采集或回补" />
      </Panel>
    )
  }

  return (
    <Panel
      title="北向活跃度"
      meta={
        <span className="num">
          {latest.trade_date} · 占两市成交额 · 库内可算 {data.coverage} 天
        </span>
      }
      delay={delay}
    >
      {/* 顶部读数：当日占比 + 近 5/20 日均值 + 当日成交额 */}
      <div className="flex flex-wrap gap-x-7 gap-y-3 px-4 py-4">
        <Chip
          label="当日占比"
          value={pctText(latest.ratio)}
          hint="沪深股通成交额 ÷ 两市成交额；越高说明外资参与越活跃"
        />
        <Chip label="近 5 日均值" value={pctText(latest.ratio_ma5)} hint="最近 5 个交易日占比的均值" />
        <Chip
          label="近 20 日均值"
          value={pctText(latest.ratio_ma20)}
          hint="最近 20 个交易日占比的均值"
        />
        <Chip
          label="北向成交额"
          value={yiText(latest.turnover)}
          hint="沪 + 深股通当日成交金额（买卖合计）。⚠️ 只有成交额、没有净流入"
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
