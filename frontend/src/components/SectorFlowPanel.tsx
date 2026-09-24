import type {
  FundFlowHistoryOut,
  FundFlowItem,
  FundFlowMatrixOut,
  SectorFundFlowOut,
  SectorTaxonomy,
} from '../api/types'
import EChart from './EChart'
import type { ChartOption } from './EChart'
import Segmented from './Segmented'
import SectorFlowMatrix from './SectorFlowMatrix'
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
 * 10 条是「一屏看得完」与「够看出主线」的折中：行业一共 104 个，前 10 已经能覆盖
 * 当天的主要方向；精选 270 个，前 10 只够看最强的，但再多图表就变成密密麻麻的条，
 * 反而看不出对比。
 */
const TOP = 10

/**
 * 口径切换项 —— **与上面板块页的「精选 / 行业」是同一套**（2026-09-23 统到开盘啦）。
 * 在那之前这一页是「同花顺概念 / 行业」，与站内板块对不上名，标签也就不能复用。
 */
const TAXONOMIES: { key: SectorTaxonomy; label: string }[] = [
  { key: 'kph_selected', label: '精选板块' },
  { key: 'kph_industry', label: '行业板块' },
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
        // ⚠️ `trigger: 'item'` 时 ECharts 传的是**单个对象**，不是数组（只有 `axis` 触发才是
        // 数组）。早先这里按数组解（`params[0].dataIndex`）—— `[0]` 恒为 undefined、
        // 回落成 0，于是**悬停任何一根条都显示榜首那条**（用户 2026-09-23 报的）。
        // 两种形状都接：ECharts 在不同触发方式下不一致，容错比赌一种便宜。
        const raw = Array.isArray(params) ? params[0] : params
        const index = (raw as { dataIndex?: number } | undefined)?.dataIndex ?? 0
        const item = items[index]
        if (!item) return ''
        return [
          `<b>${item.name}</b>`,
          `净额 ${fmtNum(item.net_amount, 2, ' 亿')}`,
          `涨跌幅 ${fmtPct(item.pct_chg)}`,
          // 流入 / 流出只有同花顺那套口径才有拆分，开盘啦口径下恒为空 ——
          // 空着就不显示这一行，别印成「流入 — / 流出 —」
          item.in_amount == null && item.out_amount == null
            ? ''
            : `流入 ${fmtNum(item.in_amount, 2, ' 亿')} / 流出 ${fmtNum(item.out_amount, 2, ' 亿')}`,
          item.leader_name
            ? `领涨 ${item.leader_name} ${fmtPct(item.leader_pct_chg)}`
            : '',
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
          // ⚠️ 流出那一侧**不能**贴条外。负值条从 0 往左长，最长的条左端正好顶到绘图区
          // 左边缘，标签再往左就伸进 y 轴那列板块名里，和榜首的名字印在同一处叠字
          // （2026-09-24 用户截图为证：`-264.0` 压在「逆变器」上）。放进条内
          // （`insideLeft` 锚在条的左端、向右排）就永远只压在绿条上，与数据量级无关 ——
          // 换成贴右侧也不行，负值条的右端全都在 0 轴上，标签会挤成一列。
          // 流入那侧贴条外是安全的：右边是 56px 的空白，没有坐标轴文字。
          position: inflow ? ('right' as const) : ('insideLeft' as const),
          color: inflow ? CHART.fgMuted : CHART.ink,
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

/**
 * 多日窗口的档位。**矩阵与累计曲线共用同一个值** —— 两者都是「近 N 日」的视角，
 * 页面上只放一个选择器（放在面板头部），改一个另一个跟着变是预期的。
 * 后端上限 60（自算口径补不了历史，库里有几天就几列），30 已经是够用的档。
 */
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
  /** 多日矩阵（列=交易日、行=当日净额第 N 名），与轮动矩阵同一套版式 */
  matrix: FundFlowMatrixOut | null
  matrixLoading: boolean
  /** 累计曲线（跨多日，另有窗口档位） */
  history: FundFlowHistoryOut | null
  historyLoading: boolean
  historyDays: number
  onHistoryDays: (days: number) => void
  taxonomy: SectorTaxonomy
  onTaxonomy: (taxonomy: SectorTaxonomy) => void
  /** 点矩阵格子 = 选中那个板块，与轮动矩阵同一个动作 */
  onSelect: (code: string) => void
  /**
   * 页面当前看的交易日（板块排行解析出来的那天，缺省时用页面 state）。
   * 只用来判断「面板的数据日是不是比它早」——资金流是按日累积算的，会晚一天。
   */
  pageDate: string | null
}

export default function SectorFlowPanel({
  data,
  loading,
  matrix,
  matrixLoading,
  history,
  historyLoading,
  historyDays,
  onHistoryDays,
  taxonomy,
  onTaxonomy,
  onSelect,
  pageDate,
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
  // 矩阵有几列（只算真算过净流入的日子，见后端 `/fund-flow/matrix`）
  const matrixDays = matrix?.columns.length ?? 0
  // 数据日比页面选的日期早时标出来。后端已经回落到最近有数据的一天，但**回落这件事
  // 用户看不见** —— 不标的话会把昨天算的净流入当成今天的（两者差一个交易日）
  const staleDate =
    data && pageDate && data.trade_date !== pageDate ? data.trade_date : null

  return (
    <div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-line-soft px-3 py-2">
        <Segmented value={taxonomy} items={TAXONOMIES} onChange={onTaxonomy} />
        <span className="text-[12px] text-fg-dim">
          开盘啦{label}口径（与站内板块同一套名字）· 单位亿元 ·
          净流入 = 成分股主力净流入之和 · 红=净流入 绿=净流出
        </span>
        {/* 剔掉哪些板块要写出来：否则「人工智能怎么不见了」会被当成 bug。
            阈值来自后端（配置项），这里不硬写数字 */}
        {data && (
          <span className="text-[12px] text-fg-dim">
            已剔掉成分股 &gt; {data.excluded_members_over} 只的宽泛板块与业绩 / 地域 / 事件类板块
          </span>
        )}
        {/* 多日窗口：矩阵与累计曲线共用这一个值（两处都是「近 N 日」的视角）。
            放面板头部是因为矩阵在上面、曲线在下面，放哪一头都够不着另一头 */}
        <span className="text-[12px] text-fg-dim">多日</span>
        <Segmented
          value={historyDays}
          items={FLOW_SPANS.map((span) => ({ key: span, label: `近${span}日` }))}
          onChange={onHistoryDays}
        />
        {staleDate && <span className="num text-fg-dim">数据日期 {staleDate}</span>}
        <span className="num ml-auto text-[12px] text-fg-dim">
          {loading ? '加载中…' : `${data?.total ?? 0} 个${label} · 各取前 ${TOP} 名`}
        </span>
      </div>

      {loading ? (
        <div className="px-4 py-10 text-center text-[13px] text-fg-dim">加载中…</div>
      ) : empty ? (
        <div className="px-4 py-10 text-center text-[13px] text-fg-dim">
          这一天没有资金流数据。净流入是拿「开盘啦成分股 × 逐股主力净流入」现算的，
          只算当天、补不了历史，所以要从改造那天起一天天累积 ——
          每天收盘后（17:30）自动算一次
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

      {/* 多日矩阵：把「当日排行」按天排成一列一列，与板块轮动那张矩阵同一套版式。
          放在条形图与曲线之间 —— 上是「今天谁最强」、中是「这几天每天谁最强」、
          下是「这几天谁被持续买入」，三块是同一个数据的三种看法。
          库里还没有净流入的日子时**不摆一张空表**，只在下面给出原因 */}
      {(matrixLoading || matrixDays > 0) && (
        <div className="border-t border-line-soft px-3 py-2">
          <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <span className="text-[12px] text-fg-dim">每日净流入前 {TOP}</span>
            <span className="text-[12px] text-fg-dim">
              列是交易日（从新到旧）、行是当天的第 N 名 · 点格子看板块详情
            </span>
            <span className="num ml-auto text-[12px] text-fg-dim">
              {matrixLoading ? '加载中…' : `${matrixDays} 个交易日`}
            </span>
          </div>
          <SectorFlowMatrix data={matrix} loading={matrixLoading} onSelect={onSelect} />
        </div>
      )}

      {/* 累计曲线：与上面的「当日排行」是同一份数据的两个看法，所以放同一个面板，
          只在中间加一条分隔线。库里还没两天数据时**不画空图**，直接说明原因。
          窗口档位在面板头部（与矩阵共用一个值），这里只留说明文字 */}
      {curveDays > 0 && (
        <div className="border-t border-line-soft px-3 py-2">
          <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <span className="text-[12px] text-fg-dim">累计净流入</span>
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
