import { BarChart, CandlestickChart, LineChart } from 'echarts/charts'
import type {
  BarSeriesOption,
  CandlestickSeriesOption,
  LineSeriesOption,
} from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from 'echarts/components'
import type {
  GridComponentOption,
  LegendComponentOption,
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
  TooltipComponent,
  CanvasRenderer,
])

export type ChartOption = echarts.ComposeOption<
  | BarSeriesOption
  | CandlestickSeriesOption
  | LineSeriesOption
  | GridComponentOption
  | LegendComponentOption
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
    return () => {
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
