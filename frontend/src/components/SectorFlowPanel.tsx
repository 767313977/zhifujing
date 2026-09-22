import type {
  FundFlowHistoryOut,
  FundFlowItem,
  FundFlowTaxonomy,
  SectorFundFlowOut,
} from '../api/types'
import EChart from './EChart'
import type { ChartOption } from './EChart'
import Segmented from './Segmented'
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
import { fmtNum, fmtPct, fmtShortDate } from '../lib/format'

/**
 * 两个榜单各显示多少条。
 *
 * 10 条是「一屏看得完」与「够看出主线」的折中：行业一共才 90 个，前 10 已经能覆盖
 * 当天的主要方向；概念 359 个，前 10 只够看最强的，但再多图表就变成密密麻麻的条，
 * 反而看不出对比。
 */
const TOP = 10

/** 口径切换项。**与上面板块的「精选/行业」不是一套**，标签也就不能复用 */
const TAXONOMIES: { key: FundFlowTaxonomy; label: string }[] = [
  { key: 'ths_concept', label: '概念' },
  { key: 'ths_industry', label: '行业' },
]

const BAR_HEIGHT = 30

/**
 * 横向条形图：一边净流入、一边净流出。
 *
 * 值**原样用正负数**（流出就是负），不取绝对值 —— 取绝对值后横轴会同时出现正向的
 * 红条和正向的绿条，看图的人得先读颜色才知道方向。负号虽然朴素，但零基线在哪边
 * 一眼就能认出来。
 */
function buildOption(items: FundFlowItem[], inflow: boolean): ChartOption {
  return {
    grid: { ...GRID, top: 6, bottom: 6, left: 8, right: 56 },
    tooltip: {
      ...TOOLTIP,
      trigger: 'item' as const,
      formatter: (params: unknown) => {
        const first = params as { dataIndex: number }[] | undefined
        const item = items[first?.[0]?.dataIndex ?? 0]
        if (!item) return ''
        return [
          `<b>${item.name}</b>`,
          `净额 ${fmtNum(item.net_amount, 2, ' 亿')}`,
          `涨跌幅 ${fmtPct(item.pct_chg)}`,
          `流入 ${fmtNum(item.in_amount, 2, ' 亿')} / 流出 ${fmtNum(item.out_amount, 2, ' 亿')}`,
          item.leader_name
            ? `领涨 ${item.leader_name} ${fmtPct(item.leader_pct_chg)}`
            : '领涨 —',
          item.member_count == null ? '' : `成分 ${item.member_count} 只`,
        ]
          .filter(Boolean)
          .join('<br/>')
      },
    },
    xAxis: {
      type: 'value' as const,
      axisLabel: { ...AXIS_LABEL, formatter: '{value}' },
      splitLine: SPLIT_LINE,
      axisLine: { show: false },
    },
    yAxis: {
      type: 'category' as const,
      data: items.map((item) => item.name),
      // 榜首在最上面（ECharts 默认是从下往上排）
      inverse: true,
      axisLabel: { ...AXIS_LABEL },
      axisLine: AXIS_LINE,
      axisTick: { show: false },
    },
    series: [
      {
        type: 'bar' as const,
        barMaxWidth: 14,
        data: items.map((item) => ({
          value: item.net_amount,
          itemStyle: { color: inflow ? CHART.up : CHART.down },
        })),
        // 数值标在条的外侧：流入贴右边、流出贴左边，别盖在条上
        label: {
          show: true,
          position: inflow ? ('right' as const) : ('left' as const),
          color: CHART.fgMuted,
          fontSize: 10,
          formatter: (params: unknown) => {
            const item = params as { value: number | null }
            return item.value == null ? '—' : fmtNum(item.value, 1)
          },
        },
      },
    ],
  }
}

/** 累计曲线的窗口档位。与后端 `FUND_FLOW_SPANS` 对齐 */
export const FLOW_SPANS = [10, 20, 30]

/**
 * 累计净流入曲线：一个板块一条线，纵轴是**从窗口起点累加**的值（不是当日值）。
 *
 * - `connectNulls: false`：起点之前后端给的是 null（那时板块还没进过榜），
 *   连过去等于凭空造出一段线。
 * - **要画圆点**：这个来源没有历史，前些天库里只有一两个交易日，没有 symbol 的话
 *   一两个点根本看不见，图会像坏的。
 */
function buildHistoryOption(history: FundFlowHistoryOut): ChartOption {
  return {
    grid: { ...GRID, top: 54, bottom: 6 },
    legend: { ...LEGEND, type: 'scroll' as const, top: 4 },
    tooltip: {
      ...TOOLTIP,
      trigger: 'axis' as const,
      // 十几条线全塞进去会很乱，所以按当天累计值从大到小排，读起来就是名次
      formatter: (params: unknown) => {
        const items = (params as { dataIndex: number }[] | undefined) ?? []
        const index = items[0]?.dataIndex ?? 0
        const rows = history.series
          .map((series) => ({ name: series.name, value: series.values[index] }))
          .filter((row) => row.value != null)
          .sort((left, right) => (right.value ?? 0) - (left.value ?? 0))
        return [
          `<b>${history.dates[index] ?? ''}</b>`,
          ...rows.map((row) => `${row.name} ${fmtNum(row.value, 2, ' 亿')}`),
        ].join('<br/>')
      },
    },
    xAxis: {
      type: 'category' as const,
      data: history.dates.map(fmtShortDate),
      // `interval` 是「隔几个类目显示一个」（0 = 全显示）。用 `Math.floor(天数/10)`
      // 而不是 `ceil` —— 后者在窗口短时得 1，会把 4 个日期标签吃掉一半
      axisLabel: { ...AXIS_LABEL, interval: Math.max(0, Math.floor(history.dates.length / 10)) },
      axisLine: AXIS_LINE,
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value' as const,
      axisLabel: { ...AXIS_LABEL },
      splitLine: SPLIT_LINE,
      axisLine: { show: false },
    },
    series: history.series.map((series, index) => {
      const color = SERIES_PALETTE[index % SERIES_PALETTE.length]
      return {
        type: 'line' as const,
        name: series.name,
        data: series.values,
        connectNulls: false,
        symbol: 'circle' as const,
        symbolSize: 4,
        lineStyle: { width: 1.5, color },
        itemStyle: { color },
      }
    }),
  }
}

interface Props {
  data: SectorFundFlowOut | null
  loading: boolean
  /** 累计曲线（跨多日，另有窗口档位） */
  history: FundFlowHistoryOut | null
  historyLoading: boolean
  historyDays: number
  onHistoryDays: (days: number) => void
  taxonomy: FundFlowTaxonomy
  onTaxonomy: (taxonomy: FundFlowTaxonomy) => void
}

export default function SectorFlowPanel({
  data,
  loading,
  history,
  historyLoading,
  historyDays,
  onHistoryDays,
  taxonomy,
  onTaxonomy,
}: Props) {
  // 后端已按净额降序；这里切两头。**只用有净额的行** —— 净额为空的排不到任何一边，
  // 混进来只会顶掉真实的榜首
  const valid = (data?.items ?? []).filter(
    (item): item is FundFlowItem & { net_amount: number } => item.net_amount != null,
  )
  const inflow = valid.slice(0, TOP)
  const outflow = [...valid.slice(-TOP)].reverse()
  const label = TAXONOMIES.find((item) => item.key === taxonomy)?.label ?? ''
  const empty = !loading && valid.length === 0
  // 库里几个交易日：曲线要 2 天以上才连得起来
  const curveDays = history?.days ?? 0

  return (
    <div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-line-soft px-3 py-2">
        <Segmented value={taxonomy} items={TAXONOMIES} onChange={onTaxonomy} />
        <span className="text-[12px] text-fg-dim">
          同花顺{label}口径（与本站板块不是一套名字）· 单位亿元 · 红=净流入 绿=净流出
        </span>
        <span className="num ml-auto text-[12px] text-fg-dim">
          {loading ? '加载中…' : `${data?.total ?? 0} 个${label} · 各取前 ${TOP} 名`}
        </span>
      </div>

      {loading ? (
        <div className="px-4 py-10 text-center text-[13px] text-fg-dim">加载中…</div>
      ) : empty ? (
        <div className="px-4 py-10 text-center text-[13px] text-fg-dim">
          这一天没有资金流数据。该来源只有「即时」快照、不给历史，所以只能从采集那天
          开始累积 —— 每天收盘后（17:30）自动采一次
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-x-4 gap-y-2 p-3 md:grid-cols-2">
          <div>
            <div className="mb-1 text-[12px] text-fg-dim">净流入前 {TOP}</div>
            <EChart
              option={buildOption(inflow, true)}
              height={inflow.length * BAR_HEIGHT + 24}
            />
          </div>
          <div>
            <div className="mb-1 text-[12px] text-fg-dim">净流出前 {TOP}</div>
            <EChart
              option={buildOption(outflow, false)}
              height={outflow.length * BAR_HEIGHT + 24}
            />
          </div>
        </div>
      )}

      {/* 累计曲线：与上面的「当日排行」是同一份数据的两个看法，所以放同一个面板，
          只在中间加一条分隔线。库里还没两天数据时**不画空图**，直接说明原因 */}
      {curveDays > 0 && (
        <div className="border-t border-line-soft px-3 py-2">
          <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <span className="text-[12px] text-fg-dim">累计净流入</span>
            <Segmented
              value={historyDays}
              items={FLOW_SPANS.map((span) => ({ key: span, label: `近${span}日` }))}
              onChange={onHistoryDays}
            />
            <span className="text-[12px] text-fg-dim">
              从窗口起点累加，缺的那天累计值顺延
            </span>
            <span className="num ml-auto text-[12px] text-fg-dim">
              {historyLoading ? '加载中…' : `${curveDays} 个交易日`}
            </span>
          </div>
          {historyLoading ? (
            <div className="py-8 text-center text-[13px] text-fg-dim">加载中…</div>
          ) : history && curveDays >= 2 ? (
            <EChart option={buildHistoryOption(history)} height={360} />
          ) : (
            <div className="py-8 text-center text-[13px] text-fg-dim">
              库里只有 1 个交易日的数据，曲线至少要两天才能连起来 ——
              这个来源没有历史可补，往后每天累积
            </div>
          )}
        </div>
      )}
    </div>
  )
}
