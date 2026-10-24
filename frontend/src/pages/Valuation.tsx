import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { IndexValuationItem, ValuationSeries, ValuationSummary } from '../api/types'
import Alert from '../components/Alert'
import EChart from '../components/EChart'
import type { ChartOption } from '../components/EChart'
import Layout from '../components/Layout'
import LoadState from '../components/LoadState'
import Panel from '../components/Panel'
import { AXIS_LABEL, AXIS_LINE, CHART, GRID, SPLIT_LINE, TOOLTIP, withAlpha } from '../lib/chart'
import { fmtNum, fmtShortDate } from '../lib/format'

/** 温度档 → 颜色。冷=便宜（绿）、热=贵（红），与全站红涨绿跌的色义一致。 */
const TONE_COLOR: Record<string, string> = {
  极冷: CHART.limitDown,
  偏冷: CHART.down,
  正常: CHART.fgDim,
  偏热: CHART.up,
  极热: CHART.limitUp,
}

function toneStyle(tone: string | null): { color: string } {
  return { color: TONE_COLOR[tone ?? ''] ?? CHART.fgDim }
}

function pctText(value: number | null): string {
  return value == null ? '—' : `${value.toFixed(1)}%`
}

/** 分位值：从升序序列里取 q 分位点（线性无关紧要，取最近下标就够看位置）。 */
function quantile(sorted: number[], q: number): number | null {
  if (sorted.length === 0) return null
  return sorted[Math.min(sorted.length - 1, Math.floor(q * (sorted.length - 1)))]
}

export default function Valuation() {
  const [data, setData] = useState<ValuationSummary | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState('000300.SH')
  const [series, setSeries] = useState<ValuationSeries | null>(null)
  const [seriesError, setSeriesError] = useState<string | null>(null)

  useEffect(() => {
    let stale = false
    api
      .valuation()
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

  useEffect(() => {
    let stale = false
    setSeries(null)
    setSeriesError(null)
    api
      .valuationSeries(selected)
      .then((value) => {
        if (!stale) setSeries(value)
      })
      .catch((err: Error) => {
        if (!stale) setSeriesError(err.message)
      })
    return () => {
      stale = true
    }
  }, [selected])

  const pick = useCallback((item: IndexValuationItem) => {
    setSelected(item.code)
  }, [])

  const chart = useMemo<ChartOption>(() => {
    if (!series || series.points.length === 0) return {}
    const points = series.points
    const peValues = points
      .map((point) => point.pe_ttm)
      .filter((value): value is number => value != null)
      .sort((left, right) => left - right)
    const marks = [
      { q: 0.2, label: '20 分位' },
      { q: 0.5, label: '50 分位' },
      { q: 0.8, label: '80 分位' },
    ]
      .map(({ q, label }) => ({ label, value: quantile(peValues, q) }))
      .filter((mark): mark is { label: string; value: number } => mark.value != null)

    return {
      grid: { ...GRID, top: 26, bottom: 6 },
      tooltip: {
        ...TOOLTIP,
        trigger: 'axis' as const,
        formatter: (params: unknown) => {
          const items = params as { axisValue: string; dataIndex: number }[]
          if (!items?.length) return ''
          const point = points[items[0].dataIndex]
          if (!point) return ''
          return [
            point.day,
            `PE-TTM：${fmtNum(point.pe_ttm, 2)}`,
            `PB：${fmtNum(point.pb, 2)}`,
          ].join('<br/>')
        },
      },
      xAxis: {
        type: 'category' as const,
        data: points.map((point) => fmtShortDate(point.day)),
        axisLabel: {
          ...AXIS_LABEL,
          // 10 年 500 个点：只标十来个刻度，否则全糊在一起
          interval: Math.max(0, Math.floor(points.length / 10)),
        },
        axisLine: AXIS_LINE,
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value' as const,
        scale: true,
        axisLabel: { ...AXIS_LABEL },
        splitLine: SPLIT_LINE,
        axisLine: { show: false },
      },
      series: [
        {
          type: 'line' as const,
          name: 'PE-TTM',
          data: points.map((point) => point.pe_ttm),
          smooth: false,
          symbol: 'none' as const,
          lineStyle: { width: 1.6, color: CHART.accent },
          itemStyle: { color: CHART.accent },
          areaStyle: { color: withAlpha(CHART.accent, 0.08) },
          // 20/50/80 分位参考线：画在**图内**右端，别压住右侧刻度（2026-10-23 统一）
          markLine: {
            silent: true,
            symbol: 'none' as const,
            data: marks.map((mark) => ({
              yAxis: mark.value,
              lineStyle: {
                color: withAlpha(CHART.fgDim, 0.45),
                type: 'dashed' as const,
                width: 1,
              },
              label: {
                ...AXIS_LABEL,
                show: true,
                position: 'insideEndTop' as const,
                formatter: `${mark.label} ${mark.value.toFixed(1)}`,
                fontSize: 11,
              },
            })),
          },
        },
      ],
    }
  }, [series])

  const current = data?.items.find((item) => item.code === selected) ?? null

  return (
    <Layout
      toolbar={
        <span className="num hidden text-[13px] text-fg-dim lg:inline">
          {data?.as_of ? `${data.as_of} · 周频采样` : '加载中…'}
        </span>
      }
    >
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}

      <div className="space-y-4">
        <Panel
          title="A 股估值温度"
          meta={<span>核心指数 PE 分位中位数 · 越高越贵</span>}
          delay={40}
        >
          {data === null ? (
            <LoadState loading={!error} error={error} emptyText="暂无估值数据" />
          ) : (
            <>
              <div className="flex flex-wrap items-end gap-x-4 gap-y-1 px-4 pt-3">
                <span
                  className="num text-[34px] font-semibold leading-none"
                  style={toneStyle(data.market_tone)}
                >
                  {data.market_pct == null ? '—' : data.market_pct.toFixed(1)}
                  <span className="ml-1 text-[16px]">%</span>
                </span>
                <span className="num text-[15px]" style={toneStyle(data.market_tone)}>
                  {data.market_tone ?? '—'}
                </span>
                <span className="text-[12px] text-fg-dim">
                  {data.items.length} 个核心指数的 PE 近 10 年分位中位数
                </span>
              </div>
              <p className="px-4 pb-3 pt-2 text-[12px] leading-relaxed text-fg-dim">
                分位用中位秩：0 = 十年最低、100 = 十年最高。窗口为近 10 年、周频采样
                （约 500 个点），不是逐日分位；只有 PE / PB，公开源没有指数级十年 PS。
              </p>
            </>
          )}
        </Panel>

        <Panel
          title="核心指数估值分位"
          meta={<span className="num">点一行看它的 PE 十年走势</span>}
          delay={60}
        >
          <div className="overflow-x-auto">
            <table className="grid-table">
              <thead>
                <tr>
                  <th className="!text-left">指数</th>
                  <th>数据日</th>
                  <th>PE-TTM</th>
                  <th>PE 分位</th>
                  <th>PB</th>
                  <th>PB 分位</th>
                  <th>温度</th>
                </tr>
              </thead>
              <tbody>
                {(data?.items ?? []).map((item) => (
                  <tr
                    key={item.code}
                    onClick={() => pick(item)}
                    className={`cursor-pointer ${
                      item.code === selected ? 'bg-accent/[0.06]' : ''
                    }`}
                  >
                    <td className="!text-left">
                      <span className="text-fg">{item.name}</span>
                      <span className="num ml-2 text-[12px] text-fg-dim">{item.code}</span>
                    </td>
                    <td className="num text-fg-muted">{item.as_of ?? '—'}</td>
                    <td className="num">{fmtNum(item.pe_ttm, 2)}</td>
                    <td className="num">{pctText(item.pe_pct)}</td>
                    <td className="num">{fmtNum(item.pb, 2)}</td>
                    <td className="num">{pctText(item.pb_pct)}</td>
                    <td className="num" style={toneStyle(item.tone)}>
                      {item.tone ?? '—'}
                    </td>
                  </tr>
                ))}
                {data !== null && data.items.length === 0 && (
                  <tr>
                    <td colSpan={7} className="!text-center text-fg-dim">
                      还没有估值数据（等采集跑一轮）
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel
          title={`PE 十年走势 · ${current?.name ?? selected}`}
          meta={
            <span className="num">
              {current?.pe_ttm == null ? '—' : `当前 PE ${current.pe_ttm.toFixed(2)}`}
              {current?.pe_pct == null ? '' : ` · 分位 ${current.pe_pct.toFixed(1)}%`}
            </span>
          }
          delay={80}
        >
          {seriesError ? (
            <div className="px-4 py-8 text-center text-[13px] text-danger">{seriesError}</div>
          ) : series === null ? (
            <LoadState loading emptyText="暂无数据" />
          ) : series.points.length === 0 ? (
            <LoadState loading={false} emptyText="这只指数还没有估值序列" />
          ) : (
            <div className="px-2 pb-2 pt-1">
              <EChart option={chart} height={320} />
            </div>
          )}
        </Panel>

        <p className="px-1 text-[12px] leading-relaxed text-fg-dim">
          数据来源：蛋卷指数估值（周频）。上证指数没有公开的指数级 PE 历史，因此不在清单里 ——
          没有就不列，不拿别的口径顶。分位仅供参考，不构成投资建议。
        </p>
      </div>
    </Layout>
  )
}
