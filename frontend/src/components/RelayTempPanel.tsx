import { useMemo } from 'react'
import type { RelayTemp, RelayTempBand } from '../api/types'
import EChart from './EChart'
import type { ChartOption } from './EChart'
import Panel from './Panel'
import {
  AXIS_LABEL,
  AXIS_LINE,
  CHART,
  GRID,
  LEGEND,
  SPLIT_LINE,
  TOOLTIP,
  withAlpha,
} from '../lib/chart'
import { fmtNum, fmtShortDate } from '../lib/format'

/**
 * 短线接力温度面板（0–100，越高越热）：打板生态的冷热。
 *
 * 版式与恐贪面板同一套（大读数 + 五档色带 + 六项拆解 + 历史曲线），配色语义也一致
 * （冷=绿、热=红）。与恐贪的差异只在口径：六项子指标按**固定阈值**折算
 * （封板率直接用、连板高度 8 板封顶、昨涨停溢价 [-5%,+5%] 映射等），不看历史分位 ——
 * 打板数据积累历史短，且这些量天然有刻度。
 *
 * 分档阈值（20/40/60/80）**由后端给**（`bands`），这里只把 band 的 `key` 映射到颜色。
 */

/** 五档配色：绿→灰→红，与恐贪同一套「冷绿热红」语义 */
const BAND_COLOR: Record<string, string> = {
  frozen: CHART.limitDown,
  cold: CHART.down,
  mild: CHART.fgDim,
  hot: CHART.up,
  boiling: CHART.limitUp,
}

function bandColor(key: string): string {
  return BAND_COLOR[key] ?? CHART.fgDim
}

/** 分数落在哪一档。与后端 `level_of` 同一判据：命中第一个「score <= upper」的档 */
function bandOf(bands: RelayTempBand[], score: number | null): RelayTempBand | null {
  if (score == null) return null
  return bands.find((band) => score <= band.upper) ?? bands[bands.length - 1] ?? null
}

/** 读数色跟着分档走：扫一眼就分得出冷热 */
function scoreColor(bands: RelayTempBand[], score: number | null): string {
  const band = bandOf(bands, score)
  return band ? bandColor(band.key) : 'var(--color-fg-dim)'
}

const SCORE_NAME = '接力温度'

/** tooltip 回传的值：普通折线是单个 y，带 visualMap 的折线可能包成 `[x, y]` */
function toNumber(value: unknown): number | null {
  if (typeof value === 'number') return value
  if (Array.isArray(value)) {
    const last = value[value.length - 1]
    return typeof last === 'number' ? last : null
  }
  return null
}

/** 0–100 五档色带 + 位置指针（与恐贪面板同一个版式） */
function ScoreBand({ bands, score }: { bands: RelayTempBand[]; score: number | null }) {
  const total = bands.reduce((sum, band) => sum + (band.upper - band.lower), 0) || 100
  return (
    <div>
      <div className="relative h-[10px]">
        <div className="flex h-full w-full overflow-hidden">
          {bands.map((band) => (
            <div
              key={band.key}
              style={{
                width: `${((band.upper - band.lower) / total) * 100}%`,
                backgroundColor: bandColor(band.key),
              }}
            />
          ))}
        </div>
        {score != null && (
          <div
            className="absolute top-[-4px] bottom-[-4px] w-[3px] -translate-x-1/2 bg-fg"
            style={{
              left: `${Math.min(Math.max(score, 0), 100)}%`,
              boxShadow: '0 0 0 2px var(--color-ink-900)',
            }}
          />
        )}
      </div>
      <div className="relative mt-1 h-[14px]">
        {[0, ...bands.map((band) => band.upper)].map((bound, i, arr) => (
          <span
            key={bound}
            className="num absolute text-[11px] text-fg-dim"
            style={{
              left: `${bound}%`,
              transform:
                i === 0 ? undefined : i === arr.length - 1 ? 'translateX(-100%)' : 'translateX(-50%)',
            }}
          >
            {bound}
          </span>
        ))}
      </div>
    </div>
  )
}

export default function RelayTempPanel({
  data,
  loading = false,
  delay = 0,
}: {
  data: RelayTemp | null
  /** 取数中：显示「加载中…」而不是「暂无数据」 */
  loading?: boolean
  delay?: number
}) {
  const latest = data?.latest ?? null

  const option = useMemo<ChartOption>(() => {
    if (!data || data.dates.length === 0) return {}
    const shortDates = data.dates.map(fmtShortDate)
    return {
      grid: { ...GRID, top: 30 },
      legend: { ...LEGEND, top: 0, right: 0, data: [SCORE_NAME] },
      // 彩虹折线：线色跟着数值走（低位绿 → 中位灰 → 高位红），与五档色带同一套语义
      visualMap: {
        type: 'continuous' as const,
        show: false,
        seriesIndex: 0,
        min: 0,
        max: 100,
        inRange: {
          color: [CHART.limitDown, CHART.down, CHART.fgDim, CHART.up, CHART.limitUp],
        },
      },
      tooltip: {
        ...TOOLTIP,
        trigger: 'axis' as const,
        formatter: (params: unknown) => {
          const items = params as { axisValue: string; seriesName?: string; value: unknown }[]
          if (!items?.length) return ''
          const scoreItem = items.find((item) => item.seriesName === SCORE_NAME) ?? items[0]
          const score = toNumber(scoreItem.value)
          return [
            scoreItem.axisValue,
            `● 温度: ${score == null ? '—' : score.toFixed(1)}`,
            `状态: ${bandOf(data.bands, score)?.label ?? '—'}`,
          ].join('<br/>')
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
      yAxis: {
        type: 'value' as const,
        // 固定 0–100：分档线是绝对刻度，纵轴自适应会让「50 分」看起来忽高忽低
        min: 0,
        max: 100,
        interval: 25,
        axisLabel: { ...AXIS_LABEL, formatter: '{value}' },
        splitLine: SPLIT_LINE,
        axisLine: { show: false },
      },
      series: [
        {
          type: 'line' as const,
          name: SCORE_NAME,
          // 有效子指标不足的日子原样传 null（图上断开）：填 0 会被画成「冰点」，
          // 那是个截然相反的结论
          data: data.scores,
          smooth: false,
          symbol: 'none' as const,
          // 只给线宽不给颜色：线色由 visualMap 逐段决定
          lineStyle: { width: 2 },
          // 五档淡色带
          markArea: {
            silent: true,
            data: data.bands.map((band) => [
              { yAxis: band.lower, itemStyle: { color: withAlpha(bandColor(band.key), 0.06) } },
              { yAxis: band.upper },
            ]),
          },
          // 分档横线画在档的上界、颜色取上面那一档（与恐贪面板同一套画法）
          markLine: {
            silent: true,
            symbol: 'none' as const,
            data: data.bands.slice(0, -1).map((band, i) => {
              const above = data.bands[i + 1]
              return {
                yAxis: band.upper,
                lineStyle: {
                  color: withAlpha(bandColor(above.key), 0.55),
                  type: 'dashed' as const,
                  width: 1,
                },
                label: {
                  ...AXIS_LABEL,
                  show: true,
                  position: 'end' as const,
                  formatter: above.label,
                  color: bandColor(above.key),
                  fontSize: 11,
                },
              }
            }),
          },
        },
      ],
    }
  }, [data])

  if (!data || !latest) {
    return (
      <Panel title="短线接力温度" meta={<span>0–100 · 越高越热</span>} delay={delay}>
        <div className="px-4 py-8 text-center text-[14px] text-fg-dim">
          {loading ? <span className="pulse-soft">加载中…</span> : '暂无数据，请先执行采集或回补'}
        </div>
      </Panel>
    )
  }

  return (
    <Panel
      title="短线接力温度"
      meta={
        <span className="num">
          {latest.trade_date} · 固定阈值合成 · 库内 {data.coverage} 天
        </span>
      }
      delay={delay}
    >
      {/* 大读数 + 色带 */}
      <div className="flex flex-wrap items-end gap-x-8 gap-y-3 px-4 py-4">
        <div className="shrink-0">
          <div
            className="num text-[34px] leading-none font-semibold"
            style={{ color: scoreColor(data.bands, latest.score) }}
          >
            {latest.score == null ? '—' : fmtNum(latest.score, 1)}
          </div>
          <div className="mt-2 flex items-center gap-3">
            <span
              className="text-[13px] tracking-[0.1em]"
              style={{ color: scoreColor(data.bands, latest.score) }}
            >
              {latest.level ?? '数据不足'}
            </span>
            {latest.change != null && (
              <span className="num text-[12px] text-fg-dim">
                较上一交易日 {latest.change >= 0 ? '+' : ''}
                {latest.change.toFixed(1)}
              </span>
            )}
          </div>
        </div>
        <div className="min-w-[220px] flex-1">
          <ScoreBand bands={data.bands} score={latest.score} />
        </div>
      </div>

      {/* 五档图例 */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 px-4 pb-3 text-[12px] text-fg-dim">
        {data.bands.map((band) => (
          <span key={band.key} className="flex items-center gap-1.5">
            <span
              className="h-[8px] w-[8px] shrink-0"
              style={{ backgroundColor: bandColor(band.key) }}
            />
            {band.label}
          </span>
        ))}
      </div>

      {/* 六项拆解 */}
      <div className="border-t border-line-soft">
        {latest.subs.map((sub) => (
          <div
            key={sub.key}
            className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-line-soft px-4 py-2.5 last:border-b-0"
          >
            <span className="w-[84px] shrink-0 text-[13px] text-fg-muted">{sub.label}</span>
            <span className="num w-[96px] shrink-0 text-[13px] text-fg">
              {sub.value == null ? '—' : `${fmtNum(sub.value, 2)}${sub.unit}`}
            </span>
            <div className="flex min-w-[140px] flex-1 items-center gap-3">
              <div className="relative h-[6px] flex-1 bg-ink-800">
                <div
                  className="h-full"
                  style={{
                    width: `${sub.score == null ? 0 : Math.min(Math.max(sub.score, 0), 100)}%`,
                    backgroundColor: scoreColor(data.bands, sub.score),
                  }}
                />
              </div>
              <span className="num w-[38px] shrink-0 text-right text-[12px] text-fg-dim">
                {sub.score == null ? '—' : fmtNum(sub.score, 0)}
              </span>
            </div>
            <span className="w-full text-[12px] leading-relaxed text-fg-dim md:w-auto md:flex-1">
              {sub.hint}
            </span>
          </div>
        ))}
      </div>

      {data.dates.length > 0 && (
        <div className="border-t border-line-soft px-2 pt-2">
          <EChart option={option} height={260} />
        </div>
      )}
    </Panel>
  )
}
