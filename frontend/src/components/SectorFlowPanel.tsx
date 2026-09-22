import type { FundFlowItem, FundFlowTaxonomy, SectorFundFlowOut } from '../api/types'
import EChart from './EChart'
import type { ChartOption } from './EChart'
import Segmented from './Segmented'
import { AXIS_LABEL, AXIS_LINE, CHART, GRID, SPLIT_LINE, TOOLTIP } from '../lib/chart'
import { fmtNum, fmtPct } from '../lib/format'

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

interface Props {
  data: SectorFundFlowOut | null
  loading: boolean
  taxonomy: FundFlowTaxonomy
  onTaxonomy: (taxonomy: FundFlowTaxonomy) => void
}

export default function SectorFlowPanel({ data, loading, taxonomy, onTaxonomy }: Props) {
  // 后端已按净额降序；这里切两头。**只用有净额的行** —— 净额为空的排不到任何一边，
  // 混进来只会顶掉真实的榜首
  const valid = (data?.items ?? []).filter(
    (item): item is FundFlowItem & { net_amount: number } => item.net_amount != null,
  )
  const inflow = valid.slice(0, TOP)
  const outflow = [...valid.slice(-TOP)].reverse()
  const label = TAXONOMIES.find((item) => item.key === taxonomy)?.label ?? ''
  const empty = !loading && valid.length === 0

  return (
    <div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-line-soft px-3 py-2">
        <Segmented value={taxonomy} items={TAXONOMIES} onChange={onTaxonomy} />
        <span className="text-[11px] text-fg-dim">
          同花顺{label}口径（与本站板块不是一套名字）· 单位亿元 · 红=净流入 绿=净流出
        </span>
        <span className="num ml-auto text-[11px] text-fg-dim">
          {loading ? '加载中…' : `${data?.total ?? 0} 个${label} · 各取前 ${TOP} 名`}
        </span>
      </div>

      {loading ? (
        <div className="px-4 py-10 text-center text-[13px] text-fg-dim">加载中…</div>
      ) : empty ? (
        <div className="px-4 py-10 text-center text-[13px] text-fg-dim">
          这一天没有资金流数据。该来源只有「即时」快照、不给历史，所以只能从采集那天
          开始累积 —— 每天收盘后（18:00）自动采一次
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-x-4 gap-y-2 p-3 md:grid-cols-2">
          <div>
            <div className="mb-1 text-[11px] text-fg-dim">净流入前 {TOP}</div>
            <EChart
              option={buildOption(inflow, true)}
              height={inflow.length * BAR_HEIGHT + 24}
            />
          </div>
          <div>
            <div className="mb-1 text-[11px] text-fg-dim">净流出前 {TOP}</div>
            <EChart
              option={buildOption(outflow, false)}
              height={outflow.length * BAR_HEIGHT + 24}
            />
          </div>
        </div>
      )}
    </div>
  )
}
