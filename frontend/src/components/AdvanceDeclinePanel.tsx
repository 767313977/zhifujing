import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { AdvanceDecline } from '../api/types'
import EChart from './EChart'
import type { ChartOption } from './EChart'
import LoadState from './LoadState'
import Panel from './Panel'
import { AXIS_LABEL, CHART, TOOLTIP } from '../lib/chart'
import { fmtNum, toneOf } from '../lib/format'

/**
 * 全 A 涨跌分布：各涨跌幅区间的家数 + 平均/中位数。
 *
 * 为什么单独一块：宽度温度计说的是「有多少票在均线上方」（慢变量），
 * 这里说的是「今天的涨跌是怎么分布的」—— 同样是 3000 家下跌，
 * 全在 -1% 和全在 -5% 是完全不同的两种日子。
 *
 * 自己取数（不占父页面）是为了让它可以被放到任何页面；失败只影响本块。
 */
export default function AdvanceDeclinePanel({ delay = 0 }: { delay?: number }) {
  const [data, setData] = useState<AdvanceDecline | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let stale = false
    api
      .advanceDecline()
      .then((value) => {
        if (!stale) setData(value)
      })
      .catch((err: Error) => {
        if (!stale) setError(err.message)
      })
    return () => {
      stale = true
    }
  }, [])

  const option = useMemo<ChartOption>(() => {
    if (!data) return {}
    const buckets = data.buckets
    const total = data.total || 1
    // 横向条形：涨跌幅区间是文字（`-7~-5%`），竖排会被挤成斜标签。
    // `inverse: true` 让「>7%」出现在最上面，往下依次到「≤-7%」
    return {
      grid: { left: 8, right: 40, top: 10, bottom: 4, containLabel: true },
      tooltip: {
        ...TOOLTIP,
        trigger: 'axis' as const,
        axisPointer: { type: 'shadow' as const },
        formatter: (params: unknown) => {
          const items = params as { name: string; value: number }[]
          if (!items?.length) return ''
          const count = items[0].value
          return `${items[0].name}<br/>${count} 家 · 占 ${((count / total) * 100).toFixed(1)}%`
        },
      },
      xAxis: {
        type: 'value' as const,
        axisLabel: { ...AXIS_LABEL, formatter: (value: number) => String(value) },
        axisLine: { show: false },
        splitLine: { show: true, lineStyle: { color: CHART.soft, type: 'dashed' as const } },
      },
      yAxis: {
        type: 'category' as const,
        inverse: true,
        data: buckets.map((bucket) => bucket.label),
        axisLabel: { ...AXIS_LABEL },
        axisLine: { show: false },
        axisTick: { show: false },
      },
      series: [
        {
          type: 'bar' as const,
          barMaxWidth: 14,
          data: buckets.map((bucket) => ({
            value: bucket.count,
            itemStyle: {
              // 红涨绿跌：涨的区间用红、跌的用绿、平盘灰（与全站色义一致）
              color:
                bucket.label === '平盘'
                  ? CHART.fgDim
                  : (bucket.lower ?? -1) >= 0
                    ? CHART.up
                    : CHART.down,
            },
          })),
          label: {
            show: true,
            position: 'right' as const,
            ...AXIS_LABEL,
            // 字符串模板（`{c}` = 数值）而不是回调：ECharts 的回调签名很宽，
            // 自己写窄参数会过不了类型检查
            formatter: '{c}',
          },
        },
      ],
    }
  }, [data])

  if (!data) {
    return (
      <Panel title="全 A 涨跌分布" meta={<span>各涨跌幅区间家数</span>} delay={delay}>
        <LoadState loading={error === null} error={error} emptyText="暂无数据" />
      </Panel>
    )
  }

  return (
    <Panel
      title="全 A 涨跌分布"
      meta={
        <span className="num">
          {data.trade_date} · {data.total} 只 · 平均 {fmtNum(data.avg_pct, 2, '%')} · 中位数{' '}
          {fmtNum(data.median_pct, 2, '%')}
        </span>
      }
      delay={delay}
    >
      <div className="flex flex-wrap items-center gap-x-6 gap-y-1 px-4 pt-3 text-[13px]">
        <span className="num">
          上涨 <span className="text-up">{data.up}</span>
        </span>
        <span className="num">
          下跌 <span className="text-down">{data.down}</span>
        </span>
        <span className="num text-fg-muted">平盘 {data.flat}</span>
        <span className={`num ${toneOf(data.avg_pct)}`}>平均 {fmtNum(data.avg_pct, 2, '%')}</span>
        <span className={`num ${toneOf(data.median_pct)}`}>
          中位数 {fmtNum(data.median_pct, 2, '%')}
        </span>
      </div>
      <div className="px-2 pb-2 pt-1">
        <EChart option={option} height={300} />
      </div>
    </Panel>
  )
}
