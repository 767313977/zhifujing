import { BarChart, CandlestickChart, LineChart } from 'echarts/charts'
import type {
  BarSeriesOption,
  CandlestickSeriesOption,
  LineSeriesOption,
} from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
} from 'echarts/components'
import type {
  GridComponentOption,
  LegendComponentOption,
  MarkLineComponentOption,
  TooltipComponentOption,
} from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { useEffect, useRef } from 'react'

// 按需注册：直接 import 'echarts' 会把整包（600+ 模块）打进产物
echarts.use([
  BarChart,
  CandlestickChart,
  LineChart,
  GridComponent,
  LegendComponent,
  // MarkLine 是形态选股的关键位虚线（突破位/支撑位）用的。
  // 漏注册时 ECharts **静默忽略** markLine，不报错也不打 warning ——
  // 图表照常渲染，只是那条线永远不出现，靠肉眼看图很难发现是漏了配置
  MarkLineComponent,
  TooltipComponent,
  CanvasRenderer,
])

export type ChartOption = echarts.ComposeOption<
  | BarSeriesOption
  | CandlestickSeriesOption
  | LineSeriesOption
  | GridComponentOption
  | LegendComponentOption
  | MarkLineComponentOption
  | TooltipComponentOption
>

interface EChartProps {
  option: ChartOption
  height?: number
}

/**
 * ECharts 轻封装：负责实例生命周期与窗口自适应。
 * option 变化时整体替换（notMerge），避免残留上一份配置的序列。
 */
export default function EChart({ option, height = 300 }: EChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    const element = containerRef.current
    if (!element) return

    const chart = echarts.init(element, undefined, { renderer: 'canvas' })
    chartRef.current = chart

    const handleResize = () => chart.resize()
    window.addEventListener('resize', handleResize)
    // 容器**自身**的尺寸变化也要跟着 resize：`height` 是个 prop（板块资金流
    // 那两张条形图按行数算高度），窗口没动、容器变高时只监听 window 收不到 ——
    // 表现是图底部被裁掉或留一大片空白，直到手动拉一次窗口（2026-09-27 修）。
    // canvas 的尺寸由 `chart.resize()` 按容器算，不会反过来改容器，所以不会自激。
    const observer = new ResizeObserver(handleResize)
    observer.observe(element)
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', handleResize)
      chart.dispose()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    chartRef.current?.setOption(option, true)
  }, [option])

  return <div ref={containerRef} style={{ height }} />
}
