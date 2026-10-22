import { useMemo } from 'react'
import type { EtfNetFlow } from '../api/types'
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
 * ETF 净申赎面板：场内基金的份额是「钱的进出」最直接的证据。
 *
 * 口径：`Σ(份额变化 × 当日收盘价)`，从 `etf_share` 的逐日份额快照**读取时现算**
 * （见 `backend/app/services/etf_flow.py`），不建新表。
 * - **货币现金类整类剔除**：场内货基的份额单位与覆盖都不可信，申赎本身也是
 *   现金管理行为，不代表方向性资金；
 * - **只在两个「覆盖达标」的交易日之间算**：快照日期来自行情源的「最新-交易日」，
 *   而它不是全市场统一的一天（非交易日货基仍按自然日给净值快照，跨境/QDII 则
 *   常年比境内 ETF 晚一个交易日），这类碎片日不能当基准日。
 *
 * 用法：累计净额与指数点位的背离 —— 指数创新高而累计净额不创新高 = 增量资金
 * 跟不上了（存量博弈）；指数横盘而累计净额持续爬升 = 有资金在借道 ETF 进场。
 *
 * 覆盖不足的日子 `net` 为 null：图上**断线断开**，不补 0 —— 0 是「一分钱没进」，
 * 是个结论；null 只是缺数据。
 */

/** 累计线用金（调色板首位）：避开红绿 —— 那两个颜色在本站有涨跌含义 */
const CUM_COLOR = SERIES_PALETTE[0]

const NET_NAME = '单日净申赎'
const CUM_NAME = '累计净申赎'

/** 顶部一枚小读数（label + value）。与宽度温度计 / NH-NL / OBV 面板同一版式。 */
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

/** 带符号金额（元）：净申购红、净赎回绿，与全站涨跌色一致 */
function signedAmount(value: number | null | undefined): string {
  if (value == null) return '—'
  const text = fmtAmount(Math.abs(value))
  return value >= 0 ? `+${text}` : `-${text}`
}

function signedColor(value: number | null | undefined): string | undefined {
  if (value == null) return undefined
  return value >= 0 ? CHART.up : CHART.down
}

/** 指数点位是四位数，轴标签与 tooltip 带千分位才读得快（与 NH-NL 面板同一处理） */
function withComma(value: number): string {
  return Math.round(value).toLocaleString('en-US')
}

export default function EtfFlowPanel({
  data,
  loading = false,
  error = null,
  delay = 0,
}: {
  data: EtfNetFlow | null
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
    const legendData = benchmark ? [NET_NAME, CUM_NAME, benchmark.name] : [NET_NAME, CUM_NAME]
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
          const span = data.span[i]
          const net = data.net[i]
          // 跨度 > 1 时这笔净额不是「一天的」——必须写在 tooltip 里，
          // 否则那个偏大的数字会被当成单日流量读（跨的是覆盖不足的日子）
          const note =
            net == null
              ? '（当日快照覆盖不足，不算）'
              : span != null && span > 1
                ? `（跨 ${span} 个交易日）`
                : ''
          const rows = [
            items[0].axisValue,
            `● ${NET_NAME}: ${signedAmount(net)}${note}`,
            `● ${CUM_NAME}: ${signedAmount(data.cumulative[i])}`,
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
          name: '净申赎',
          nameTextStyle: { color: CHART.fgDim, fontSize: 11, fontFamily: AXIS_LABEL.fontFamily },
          // 不从 0 起：累计净额有正有负（净赎回期是负数），0 轴强拉会把形态压扁
          scale: true,
          axisLabel: {
            ...AXIS_LABEL,
            formatter: (value: number) => `${(value / 1e8).toFixed(0)}亿`,
          },
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
          type: 'bar' as const,
          name: NET_NAME,
          // 缺数的日子原样传 null（图上留空）：补 0 会把「不知道」画成「没进出」
          data: data.net,
          barMaxWidth: 8,
          // 净申购红、净赎回绿：与全站涨跌色同一套语义
          itemStyle: {
            color: (params: { value?: unknown }) =>
              typeof params.value === 'number' && params.value < 0 ? CHART.down : CHART.up,
          },
        },
        {
          type: 'line' as const,
          name: CUM_NAME,
          // 累计从 0 起加，缺数的日子累加器不动、该点记 null，图上断开
          data: data.cumulative,
          smooth: false,
          symbol: 'none' as const,
          z: 3,
          lineStyle: { width: 1.8, color: CUM_COLOR },
          itemStyle: { color: CUM_COLOR },
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
      <Panel title="ETF 净申赎" meta={<span>份额增减 × 收盘价 · 增量资金</span>} delay={delay}>
        <LoadState loading={loading} error={error} emptyText="暂无数据，请先执行采集或回补" />
      </Panel>
    )
  }

  return (
    <Panel
      title="ETF 净申赎"
      meta={
        <span className="num">
          {latest.trade_date} · 剔除货币现金类 · 库内可算 {data.coverage} 天
        </span>
      }
      delay={delay}
    >
      {/* 顶部读数：单日 + 近 5/20 日 + 区间累计 */}
      <div className="flex flex-wrap gap-x-7 gap-y-3 px-4 py-4">
        <Chip
          label={
            latest.span != null && latest.span > 1 ? `当日净额（跨 ${latest.span} 日）` : '当日净额'
          }
          value={signedAmount(latest.net)}
          color={signedColor(latest.net)}
          hint="Σ(份额变化 × 当日收盘价)，货币现金类 ETF 整类剔除；净申购为流入"
        />
        <Chip
          label="近 5 日"
          value={signedAmount(latest.sum5)}
          color={signedColor(latest.sum5)}
          hint="最近 5 个有数的交易日累计净申赎之和"
        />
        <Chip
          label="近 20 日"
          value={signedAmount(latest.sum20)}
          color={signedColor(latest.sum20)}
          hint="最近 20 个有数的交易日累计净申赎之和"
        />
        <Chip
          label="区间累计"
          value={signedAmount(latest.cumulative)}
          hint="窗口内逐日净申赎的累计值；看它与指数点位是否背离"
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
