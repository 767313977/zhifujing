import { useMemo } from 'react'
import type { FearGreed } from '../api/types'
// 五档配色 / 分档判据 / 读数头都在 `FearGreedBrief.tsx`（那个文件**不引 EChart**，
// 首页只用到它，见该文件头的说明）。这里反向引用，保证两处只有一套阈值。
import { ScoreHeader, bandColor, bandOf, scoreColor } from './FearGreedBrief'
import EChart from './EChart'
import type { ChartOption } from './EChart'
import Panel from './Panel'
import LoadState from './LoadState'
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
 * 恐惧贪婪指数面板（0–100，越高越贪婪）。
 *
 * - `FearGreedBrief`（在 `FearGreedBrief.tsx`）：首页今日复盘用，只有读数 + 色带，
 *   不发第二个请求（数据来自 `overview.fear_greed`），要看拆解去「情绪周期」页；
 * - `FearGreedPanel`（本文件）：情绪周期页用，读数 + 六项子指标 + 历史曲线。
 *
 * 分档阈值（25/45/55/75）**由后端给**（`bands`），这里只把 band 的 `key` 映射到颜色 ——
 * 所以后端调档位时色带、指针、图上的横线会一起动，不会出现「色带画在 45、
 * 数字却按 50 分档」这种对不上的情况。
 */

/** 曲线与图例里的系列名。tooltip 按名字找系列，不按下标找（下标会随叠加线的有无而变）。 */
const SCORE_NAME = '恐惧贪婪指数'

/**
 * 指数点位是四位数，轴标签与 tooltip 都带千分位才读得快。
 * `lib/format.ts` 的 `fmtNum` **不带**千分位，那是全站共用的，不能为这一处改它。
 */
function withComma(value: number): string {
  return Math.round(value).toLocaleString('en-US')
}

/** tooltip 回传的值：普通折线是单个 y，带 visualMap 的折线可能包成 `[x, y]` —— 两种都收。 */
function toNumber(value: unknown): number | null {
  if (typeof value === 'number') return value
  if (Array.isArray(value)) {
    const last = value[value.length - 1]
    return typeof last === 'number' ? last : null
  }
  return null
}

/**
 * 情绪周期页的完整面板：读数 + 六项子指标 + 历史曲线。
 *
 * 六项各自给「原始值 + 0–100 分位分」，是为了让读数可拆解 —— 同样是 34 分，
 * 可能来自「成交额萎缩」，也可能来自「市场宽度塌了」，这两件事下一步动作不同。
 */
export default function FearGreedPanel({
  data,
  loading = false,
  error = null,
  delay = 0,
}: {
  data: FearGreed | null
  /** 取数中：显示「加载中…」而不是「暂无数据」—— 后者会被读成「真的没有」 */
  loading?: boolean
  /** 请求失败原因（404 由调用方过滤，算「暂无数据」）；有值时不显示「暂无数据」 */
  error?: string | null
  delay?: number
}) {
  const latest = data?.latest ?? null
  const option = useMemo<ChartOption>(() => {
    if (!data || data.dates.length === 0) return {}
    const benchmark = data.benchmark
    const shortDates = data.dates.map(fmtShortDate)
    return {
      grid: { ...GRID, top: 30, bottom: 34 },
      legend: {
        ...LEGEND,
        top: 0,
        right: 0,
        data: benchmark ? [SCORE_NAME, benchmark.name] : [SCORE_NAME],
      },
      // 底部缩放条：250 天全铺开太挤，默认只给最近约 120 天（start 52%），
      // 往左拖能看更早。`bottom: 34` 是它跟 grid 之间让出来的高度
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
      // 彩虹折线：线色跟着数值走（低位绿 → 中位灰 → 高位红），与五档色带同一套语义。
      // 0/25/50/75/100 五个锚点正好落在各档中点，与 bands 的边界基本重合。
      // 不写 `dimension`：让它取「最后一个非计算维度」（就是 y 值），
      // 手写 1 反而可能在只有一维数据时越界
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
        // 照截图那种多行：日期 / 指数值 / 情绪状态 / 指数名（收盘点位）
        formatter: (params: unknown) => {
          const items = params as { axisValue: string; seriesName?: string; value: unknown }[]
          if (!items?.length) return ''
          const scoreItem = items.find((item) => item.seriesName === SCORE_NAME) ?? items[0]
          const score = toNumber(scoreItem.value)
          const rows = [
            scoreItem.axisValue,
            `● 指数值: ${score == null ? '—' : score.toFixed(1)}`,
            `情绪状态: ${bandOf(data.bands, score)?.label ?? '—'}`,
          ]
          if (benchmark) {
            const close = toNumber(
              items.find((item) => item.seriesName === benchmark.name)?.value,
            )
            rows.push(
              `● ${benchmark.name}（收盘点位）: ${close == null ? '—' : withComma(close)}`,
            )
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
          // 固定 0–100：分档线是绝对刻度，纵轴自适应会让「50 分」看起来忽高忽低
          min: 0,
          max: 100,
          interval: 25,
          axisLabel: { ...AXIS_LABEL, formatter: '{value}' },
          splitLine: SPLIT_LINE,
          axisLine: { show: false },
        },
        // 叠加的指数线走右轴。**没有 benchmark 时整根轴不出现** —— 空轴会白占一条刻度
        ...(benchmark
          ? [
              {
                type: 'value' as const,
                // 不从 0 起：指数动几百点在「从 0 到 4000」的轴上是一条平线，看不出对照
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
        {
          type: 'line' as const,
          name: SCORE_NAME,
          // 机器算不出的日子原样传 null（图上断开）：填 0 会被画成「极度恐慌」，
          // 那是个截然相反的结论
          data: data.scores,
          smooth: false,
          symbol: 'none' as const,
          // **只给线宽不给颜色**：线色由上面的 visualMap 逐段决定，再写死颜色会把它盖掉
          lineStyle: { width: 2 },
          // 五档淡色带：给整幅图铺出「哪一段冷、哪一段热」的底
          markArea: {
            silent: true,
            data: data.bands.map((band) => [
              { yAxis: band.lower, itemStyle: { color: withAlpha(bandColor(band.key), 0.06) } },
              { yAxis: band.upper },
            ]),
          },
          // 分档横线：线画在档的**上界**上、颜色取它上面那一档 —— 于是「跌破 45
          // 这条灰线」就进了中性，看图不必回查色带图例。行尾的档名同理
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
                lineStyle: { width: 1.6, color: CHART.accent },
                itemStyle: { color: CHART.accent },
              },
            ]
          : []),
      ],
    }
  }, [data])

  if (!data || !latest) {
    return (
      <Panel title="恐惧贪婪指数" meta={<span>0–100 · 越高越贪婪</span>} delay={delay}>
        <LoadState loading={loading} error={error} emptyText="暂无数据，请先执行采集或回补" />
      </Panel>
    )
  }

  return (
    <Panel
      title="恐惧贪婪指数"
      meta={
        <span className="num">
          {latest.trade_date} · 分位窗口 {data.window} 日 · 库内 {data.coverage} 天
        </span>
      }
      delay={delay}
    >
      <ScoreHeader
        score={latest.score}
        level={latest.level}
        change={latest.change}
        bands={data.bands}
      />

      {/* 五档图例：色带上的颜色要有名字，不然只能靠「绿→红」猜 */}
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
            <span className="w-[68px] shrink-0 text-[13px] text-fg-muted">{sub.label}</span>
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
            {/* 六项里有两项（宽度、两融）是本项目的自有口径：写清楚，
                免得被拿去和外部网站的读数逐项对比 */}
            <span className="w-full text-[12px] leading-relaxed text-fg-dim md:w-auto md:flex-1">
              {sub.hint}
            </span>
          </div>
        ))}
      </div>

      {data.dates.length > 0 && (
        <div className="border-t border-line-soft px-2 pt-2">
          <EChart option={option} height={300} />
        </div>
      )}
    </Panel>
  )
}
