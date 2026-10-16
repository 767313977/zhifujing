import { useMemo } from 'react'
import type { Volatility } from '../api/types'
import EChart from './EChart'
import type { ChartOption } from './EChart'
import Panel from './Panel'
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
 * 中国波指（QVIX）面板：期权市场给出的**隐含波动率**，A 股市场定价出来的「恐慌指数」。
 *
 * ⚠️ **不是交易所官方 iVX**（那条 2018 年就停更了）—— 数据由**期权论坛**按方差互换
 * 思路合成，站内只负责落库与读取（见 `backend/app/jobs/collect_qvix.py` 的取舍说明）。
 * 加权方案不在我们手里，这是「直取现成指数，换 10 年历史 + 零计算」的取舍。
 *
 * 两个品种：50ETF（2015 年起）与 300ETF（2019 年底起，标的期权那年才上市）。
 * 单位是**百分数**（17.95 = 隐含波动率 17.95%）。
 *
 * 用法：波动率与指数**反向** —— 指数跌、波动率升（市场抢着买保险）；指数横盘而波动率
 * 一路下行 = 市场认为没什么好怕的。虚线是上证指数收盘点位，右轴，反向关系一眼可见。
 */

/** 两条线用金 / 紫：避开基准虚线那个蓝（accent），也避开红绿（那两色有涨跌含义） */
const LINE_COLORS = [SERIES_PALETTE[0], SERIES_PALETTE[2]]

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

/** 波动率是百分数（17.95 = 17.95%），只给数值不加 %，否则读起来像「17.95 的涨幅」 */
function volText(value: number | null | undefined): string {
  return value == null ? '—' : value.toFixed(2)
}

function signedPct(value: number | null | undefined): string {
  return value == null ? '—' : `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`
}

/** 波动率上行用红、下行用绿（与全站涨跌色一致；对波动率来说「涨」= 市场更慌） */
function chgColor(value: number | null | undefined): string | undefined {
  if (value == null) return undefined
  return value >= 0 ? CHART.up : CHART.down
}

/** 指数点位是四位数，轴标签与 tooltip 带千分位才读得快（与 NH-NL 面板同一处理） */
function withComma(value: number): string {
  return Math.round(value).toLocaleString('en-US')
}

export default function VolatilityPanel({
  data,
  loading = false,
  delay = 0,
}: {
  data: Volatility | null
  /** 取数中：显示「加载中…」而不是「暂无数据」 */
  loading?: boolean
  delay?: number
}) {
  const latest = data?.latest ?? []

  const option = useMemo<ChartOption>(() => {
    if (!data || data.dates.length === 0 || data.series.length === 0) return {}
    const benchmark = data.benchmark
    const shortDates = data.dates.map(fmtShortDate)
    const legendData = [
      ...data.series.map((item) => item.name ?? item.code),
      ...(benchmark ? [benchmark.name] : []),
    ]
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
          const rows = [items[0].axisValue]
          data.series.forEach((item, s) => {
            const color = LINE_COLORS[s % LINE_COLORS.length]
            rows.push(
              `<span style="color:${color}">●</span> ${item.name ?? item.code}: ${volText(item.close[i])}`,
            )
          })
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
          name: '波动率 %',
          nameTextStyle: { color: CHART.fgDim, fontSize: 11, fontFamily: AXIS_LABEL.fontFamily },
          // 不从 0 起：波动率在 10~40 这个区间里摆动，0 轴强拉会把形态压平
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
        ...data.series.map((item, s) => ({
          type: 'line' as const,
          name: item.name ?? item.code,
          // 300ETF 在 2019 年底之前整段是 null（标的期权还没上市），图上断开
          data: item.close,
          smooth: false,
          symbol: 'none' as const,
          z: 3,
          lineStyle: { width: 1.8, color: LINE_COLORS[s % LINE_COLORS.length] },
          itemStyle: { color: LINE_COLORS[s % LINE_COLORS.length] },
        })),
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

  if (!data || latest.length === 0) {
    return (
      <Panel title="中国波指 QVIX" meta={<span>期权隐含波动率 · 恐慌指数</span>} delay={delay}>
        <div className="px-4 py-8 text-center text-[14px] text-fg-dim">
          {loading ? <span className="pulse-soft">加载中…</span> : '暂无数据，请先执行采集或回补'}
        </div>
      </Panel>
    )
  }

  return (
    <Panel
      title="中国波指 QVIX"
      meta={
        <span className="num">
          {latest[0].trade_date} · 隐含波动率（%）· 库内 {data.coverage} 天
        </span>
      }
      delay={delay}
    >
      {/* 顶部读数：两个品种各一枚波动率 + 各一枚分位 */}
      <div className="flex flex-wrap gap-x-7 gap-y-3 px-4 py-4">
        {latest.map((item) => (
          <Chip
            key={item.code}
            label={item.name ?? item.code}
            value={volText(item.close)}
            color={chgColor(item.chg)}
            hint={`期权隐含波动率（%），单位就是百分数；较前一交易日 ${signedPct(item.chg)}`}
          />
        ))}
        {latest.map((item) => (
          <Chip
            key={`${item.code}-pct`}
            label={`${item.name ?? item.code}分位`}
            value={item.percentile == null ? '—' : item.percentile.toFixed(1)}
            hint={`当前波动率在滚动 ${data.window} 个交易日里的分位（0–100），越高 = 市场越慌`}
          />
        ))}
      </div>

      {data.dates.length > 0 && (
        <div className="border-t border-line-soft px-2 pt-2">
          <EChart option={option} height={280} />
        </div>
      )}
    </Panel>
  )
}
