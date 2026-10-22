import { useMemo } from 'react'
import type { IndustryBreadth } from '../api/types'
import EChart from './EChart'
import type { ChartOption } from './EChart'
import Panel from './Panel'
import LoadState from './LoadState'
import { AXIS_LABEL, AXIS_LINE, CHART, GRID, SPLIT_LINE, TOOLTIP } from '../lib/chart'

/**
 * 行业宽度面板：把「站上 MA20 的个股占比」拆到**一级行业**看，一眼分辨谁在扛、谁在拖。
 *
 * 口径：读 `stock_daily` × `stock_basic.industry`，**读取时现算**
 * （见 `backend/app/services/industry_breadth.py`），不建新表。
 * - 行业取同花顺行业路径的第一段（31 个一级行业）；
 * - 「站上 MA20」与站内宽度温度计同一套约定（不满 20 根 K 线的票不进分母）；
 * - 这是**当日横截面**，不是时间序列 —— 逐日算要好几秒（见后端端点说明）。
 *
 * 用法：多数行业都在 50% 以上 = 普涨、行业共振；只有少数行业在上 = 结构性行情，
 * 指数可能靠这少数行业扛着。
 */

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

function pctText(value: number | null | undefined): string {
  return value == null ? '—' : `${value.toFixed(1)}%`
}

/** 占比 ≥ 50%（半数个股站上均线）用贪婪色、否则用恐慌色 */
function ratioColor(value: number | null | undefined): string | undefined {
  if (value == null) return undefined
  return value >= 50 ? CHART.up : CHART.down
}

export default function IndustryBreadthPanel({
  data,
  loading = false,
  error = null,
  delay = 0,
}: {
  data: IndustryBreadth | null
  /** 取数中：显示「加载中…」而不是「暂无数据」 */
  loading?: boolean
  /** 请求失败原因（404 由调用方过滤，算「暂无数据」）；有值时不显示「暂无数据」 */
  error?: string | null
  delay?: number
}) {
  const items = data?.items ?? []
  // 后端已按占比降序；占比为空的（分母 0）排在末尾，取最强/最弱时剔掉
  const ranked = items.filter((item) => item.ratio != null)
  const strongest = ranked[0]
  const weakest = ranked[ranked.length - 1]

  const option = useMemo<ChartOption>(() => {
    if (!data || data.items.length === 0) return {}
    const names = data.items.map((item) => item.industry)
    return {
      // containLabel 会自动给左侧行业名留位，右侧留 48px 放「xx%」标签
      grid: { ...GRID, top: 6, bottom: 6, left: 8, right: 48 },
      tooltip: {
        ...TOOLTIP,
        trigger: 'axis' as const,
        axisPointer: { type: 'shadow' as const },
        formatter: (params: unknown) => {
          const rows = params as { dataIndex: number }[]
          if (!rows?.length) return ''
          const item = data.items[rows[0].dataIndex]
          if (!item) return ''
          return [
            item.industry,
            `站上 MA${data.ma_window}：${item.above} / ${item.total} 家`,
            `占比：${pctText(item.ratio)}`,
          ].join('<br/>')
        },
      },
      xAxis: {
        type: 'value' as const,
        // 固定 0–100：跨日比较时轴不能自己伸缩，否则「今天更宽」可能是错觉
        min: 0,
        max: 100,
        axisLabel: { ...AXIS_LABEL, formatter: '{value}%' },
        splitLine: SPLIT_LINE,
        axisLine: { show: false },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'category' as const,
        data: names,
        // 第一项画在最上面（后端已按占比降序）
        inverse: true,
        axisLabel: AXIS_LABEL,
        axisLine: AXIS_LINE,
        axisTick: { show: false },
      },
      series: [
        {
          type: 'bar' as const,
          name: `站上MA${data.ma_window}占比`,
          // 占比为空的行业原样传 null（图上留空），不画成 0
          data: data.items.map((item) => item.ratio),
          barMaxWidth: 14,
          itemStyle: {
            color: (params: { value?: unknown }) =>
              typeof params.value === 'number' && params.value >= 50 ? CHART.up : CHART.down,
          },
          label: {
            show: true,
            position: 'right' as const,
            formatter: (params: { value?: unknown }) =>
              typeof params.value === 'number' ? `${params.value}%` : '—',
            color: CHART.fgMuted,
            fontSize: 11,
            fontFamily: AXIS_LABEL.fontFamily,
          },
          // 50% 是「半数个股在均线上」的分水岭，画一条虚线当参照
          markLine: {
            silent: true,
            symbol: 'none' as const,
            lineStyle: { color: CHART.fgDim, type: 'dashed' as const },
            label: { formatter: '50%', color: CHART.fgDim, fontSize: 11 },
            data: [{ xAxis: 50 }],
          },
        },
      ],
    }
  }, [data])

  if (!data || items.length === 0) {
    return (
      <Panel title="行业宽度" meta={<span>一级行业 · 站上 MA20 占比</span>} delay={delay}>
        <LoadState loading={loading} error={error} emptyText="暂无数据，请先执行采集或回补" />
      </Panel>
    )
  }

  return (
    <Panel
      title="行业宽度"
      meta={
        <span className="num">
          {data.trade_date} · 一级行业 {items.length} 个 · 纳入 {data.coverage} 只
        </span>
      }
      delay={delay}
    >
      {/* 顶部读数：全市场 / 行业中位数 / 最强 / 最弱 */}
      <div className="flex flex-wrap gap-x-7 gap-y-3 px-4 py-4">
        <Chip
          label="全市场"
          value={pctText(data.overall)}
          color={ratioColor(data.overall)}
          hint={`全部个股里站上 MA${data.ma_window} 的占比`}
        />
        <Chip
          label="行业中位数"
          value={pctText(data.median)}
          color={ratioColor(data.median)}
          hint="各一级行业占比的中位数（等权，不被大行业带偏）"
        />
        <Chip
          label="最强行业"
          value={strongest ? `${strongest.industry} ${pctText(strongest.ratio)}` : '—'}
          color={ratioColor(strongest?.ratio)}
          hint="站上 MA20 占比最高的一级行业"
        />
        <Chip
          label="最弱行业"
          value={weakest ? `${weakest.industry} ${pctText(weakest.ratio)}` : '—'}
          color={ratioColor(weakest?.ratio)}
          hint="站上 MA20 占比最低的一级行业"
        />
      </div>

      <div className="border-t border-line-soft px-2 pt-2">
        <EChart option={option} height={Math.max(340, items.length * 18 + 44)} />
      </div>
    </Panel>
  )
}
