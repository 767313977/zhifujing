import { useMemo } from 'react'
import type { StockDailyRow } from '../api/types'
import EChart from './EChart'
import type { ChartOption } from './EChart'
import { AXIS_LABEL, CHART, SPLIT_LINE, TOOLTIP } from '../lib/chart'
import { fmtShortDate } from '../lib/format'

const MA_WINDOWS = [5, 10, 20]
const MA_COLORS = ['#ffb020', '#5b9dff', '#c084fc']

/**
 * 图上的一根 K。
 *
 * 三个周期的来源不同（日线读库、周/月由后端重采样），但画法完全一样，
 * 所以统一成这个形状再交给图 —— 组件不必知道自己画的是哪个周期。
 * `label` 是**已经格式化好的**横轴文字（日线 `MM-DD`，周/月 `YY-MM-DD`）。
 */
export interface KLineBar {
  label: string
  open: number | null
  high: number | null
  low: number | null
  close: number | null
  volume: number | null
  /** 决定成交量柱的颜色。周/月的**第一根**没有前一根可比，是 null
   *  （图上退回按「收 - 开」染色，与日线缺涨跌幅时的行为一致） */
  pct_chg: number | null
}

/**
 * 日线 / 周线 / 月线 → 图上的 bar（后端已经把周月聚合好了）。
 *
 * `withYear` 给周/月 K 用：它们看的是 2 年，横轴只写 `MM-DD` 会出现两个
 * `09-30`（2024 与 2025）分不清的情况。日线保持短标签，250 根挤得下。
 */
export function dailyBars(
  rows: StockDailyRow[],
  { withYear = false }: { withYear?: boolean } = {},
): KLineBar[] {
  return rows.map((row) => ({
    label: withYear ? row.trade_date.slice(2) : fmtShortDate(row.trade_date),
    open: row.open,
    high: row.high,
    low: row.low,
    close: row.close,
    volume: row.volume,
    pct_chg: row.pct_chg,
  }))
}

/** 图上的关键位标记（形态的突破价 / 支撑价、样板池的触发价 / 兜底线）。 */
export interface KeyLevel {
  /**
   * 线的名字，**只写名字、不要带数字** —— 图上显示的是「名字 价格」，
   * 价格由本组件统一补（`${label} ${value}`）。带进来就打印两遍
   * （「触发 20.18 20.18」），实测踩过。
   */
  label: string
  value: number
  kind: 'breakout' | 'support'
}

/** 移动平均。窗口不足或含空值时给 null，ECharts 会自然断线。 */
function movingAverage(values: (number | null)[], window: number): (number | null)[] {
  return values.map((_, index) => {
    if (index + 1 < window) return null
    const slice = values.slice(index + 1 - window, index + 1)
    if (slice.some((value) => value == null)) return null
    const sum = slice.reduce<number>((acc, value) => acc + (value as number), 0)
    return Number((sum / window).toFixed(2))
  })
}

interface Props {
  bars: KLineBar[]
  height?: number
  /**
   * 形态关键位，画成水平虚线。
   *
   * 注意这些值是**前复权口径**的（引擎判定时用的就是复权序列），所以调用方
   * 必须同时用 `adjust=1` 取日线，否则除权股上的标注线会画在错误的高度 ——
   * 图和线对不上，看图确认就失去意义了。
   */
  keyLevels?: KeyLevel[]
}

/**
 * K 线图（蜡烛 + 均线 + 成交量），个股详情页与形态选股页共用。
 *
 * 从 `StockDetail.tsx` 里抽出来而不是复制一份：这个图有三处容易写错的地方
 * —— A 股的红涨绿跌覆盖（ECharts 默认是欧美惯例）、主图与副图的轴联动、
 * 成交量柱跟随涨跌染色 —— 复制出去迟早会有一份忘了改。
 *
 * 均线按**行数**滚动，所以周期切换后自动变成「5/10/20 周」「5/10/20 月」，
 * 不需要为每个周期另配参数。
 */
export default function KLineChart({ bars, height = 420, keyLevels = [] }: Props) {
  const option = useMemo<ChartOption>(() => {
    if (bars.length === 0) return {}
    const dates = bars.map((bar) => bar.label)
    const closes = bars.map((bar) => bar.close)
    // ECharts 蜡烛图的数据顺序是 [开, 收, 低, 高]
    const candles = bars.map((bar) => [bar.open, bar.close, bar.low, bar.high])
    const volumes = bars.map((bar) => ({
      value: bar.volume,
      // 成交量柱跟随当日涨跌染色；涨跌幅缺失时用收盘价与开盘价比较
      itemStyle: {
        color:
          (bar.pct_chg ?? (bar.close ?? 0) - (bar.open ?? 0)) >= 0
            ? 'rgba(255,77,79,0.55)'
            : 'rgba(0,185,107,0.55)',
      },
    }))

    const marks = keyLevels.filter((level) => Number.isFinite(level.value))
    // 两条关键位挨得太近时，标签会叠在一起谁也读不出来 —— 这不是偶发情况：
    // 样板池的「触发价」与「兜底线」按定义只差 2%。所以从高到低排一遍，
    // 离上一条太近的那条把标签翻到线的下方。
    // 判据用「占价格区间多少」而不是绝对价差：纵轴是自动缩放的，而 5% 的区间
    // 大约就是一行 10px 字（主网格高约 56% 的图高）
    const highs = bars.map((bar) => bar.high).filter((value): value is number => value != null)
    const lows = bars.map((bar) => bar.low).filter((value): value is number => value != null)
    const spread = highs.length && lows.length ? Math.max(...highs) - Math.min(...lows) : 0
    const flipLabel = new Set<number>()
    let lastValue: number | null = null
    for (const level of [...marks].sort((a, b) => b.value - a.value)) {
      if (lastValue !== null && (lastValue - level.value) <= spread * 0.05) {
        flipLabel.add(level.value)
      }
      lastValue = level.value
    }
    const legend = [...MA_WINDOWS.map((w) => `MA${w}`), '成交量']
    // 横轴放几个标签要看标签有多长：周月是 `25-09-30`（8 字符，比日线的 `09-21`
    // 长），同一宽度下要少放几个，否则相邻标签会贴在一起
    const wideLabels = (dates[0] ?? '').length > 6
    const interval = Math.max(0, Math.floor(dates.length / (wideLabels ? 6 : 8)))

    return {
      grid: [
        { left: 8, right: 14, top: 34, height: '56%', containLabel: true },
        { left: 8, right: 14, top: '76%', height: '16%', containLabel: true },
      ],
      legend: {
        top: 2,
        left: 8,
        icon: 'rect',
        itemWidth: 10,
        itemHeight: 10,
        itemGap: 14,
        textStyle: { color: CHART.fgMuted, fontSize: 11 },
        data: legend,
      },
      tooltip: { ...TOOLTIP, trigger: 'axis', axisPointer: { type: 'cross' } },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      xAxis: [
        {
          type: 'category',
          data: dates,
          gridIndex: 0,
          axisLabel: { ...AXIS_LABEL, interval },
          axisLine: { lineStyle: { color: CHART.line } },
          axisTick: { show: false },
        },
        {
          type: 'category',
          data: dates,
          gridIndex: 1,
          axisLabel: { show: false },
          axisLine: { lineStyle: { color: CHART.line } },
          axisTick: { show: false },
        },
      ],
      yAxis: [
        {
          scale: true,
          gridIndex: 0,
          axisLabel: AXIS_LABEL,
          splitLine: SPLIT_LINE,
          axisLine: { show: false },
        },
        {
          gridIndex: 1,
          axisLabel: { ...AXIS_LABEL, fontSize: 10 },
          splitLine: { show: false },
          axisLine: { show: false },
        },
      ],
      series: [
        {
          type: 'candlestick' as const,
          name: 'K线',
          data: candles,
          xAxisIndex: 0,
          yAxisIndex: 0,
          itemStyle: {
            // ECharts 默认是欧美惯例（绿涨红跌），必须覆盖成 A 股的红涨绿跌：
            // color = 阳线（收 ≥ 开），color0 = 阴线
            color: CHART.up,
            color0: CHART.down,
            borderColor: CHART.up,
            borderColor0: CHART.down,
          },
          // 关键位画成水平虚线。挂在蜡烛序列的 markLine 上，这样它自动跟随主图
          // 的坐标轴，价格尺度一变就跟着变，不用自己算位置
          markLine: marks.length
            ? {
                symbol: 'none',
                silent: true,
                label: {
                  position: 'insideEndTop' as const,
                  color: CHART.fgMuted,
                  fontSize: 10,
                  formatter: '{b}',
                },
                data: marks.map((level) => ({
                  yAxis: level.value,
                  name: `${level.label} ${level.value.toFixed(2)}`,
                  // 与上一条太近的标签翻到线下方（见上面 flipLabel 的说明）
                  label: {
                    position: flipLabel.has(level.value)
                      ? ('insideEndBottom' as const)
                      : ('insideEndTop' as const),
                  },
                  lineStyle: {
                    color: level.kind === 'breakout' ? CHART.accent : CHART.fgDim,
                    type: 'dashed' as const,
                    width: 1,
                  },
                })),
              }
            : undefined,
        },
        ...MA_WINDOWS.map((window, index) => ({
          type: 'line' as const,
          name: `MA${window}`,
          data: movingAverage(closes, window),
          xAxisIndex: 0,
          yAxisIndex: 0,
          smooth: true,
          symbol: 'none' as const,
          lineStyle: { width: 1.2, color: MA_COLORS[index] },
          itemStyle: { color: MA_COLORS[index] },
        })),
        {
          type: 'bar' as const,
          name: '成交量',
          data: volumes,
          xAxisIndex: 1,
          yAxisIndex: 1,
          barMaxWidth: 8,
        },
      ],
    }
  }, [bars, keyLevels])

  if (bars.length === 0) {
    return (
      <div
        className="flex items-center justify-center text-[12px] text-fg-dim"
        style={{ height }}
      >
        暂无日线数据
      </div>
    )
  }
  return <EChart option={option} height={height} />
}
