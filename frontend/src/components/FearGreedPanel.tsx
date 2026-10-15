import { useMemo } from 'react'
import type { FearGreed, FearGreedBand, FearGreedBrief as BriefData } from '../api/types'
import EChart from './EChart'
import type { ChartOption } from './EChart'
import Panel from './Panel'
import {
  AXIS_LABEL,
  AXIS_LINE,
  CHART,
  GRID,
  SPLIT_LINE,
  TOOLTIP,
  withAlpha,
} from '../lib/chart'
import { fmtNum, fmtShortDate } from '../lib/format'

/**
 * 恐惧贪婪指数面板（0–100，越高越贪婪）。
 *
 * 两个导出共用同一条五档色带与指针：
 * - `FearGreedBrief`：首页今日复盘用，只有读数 + 色带，不发第二个请求
 *   （数据来自 `overview.fear_greed`），要看拆解去「情绪周期」页；
 * - `FearGreedPanel`：情绪周期页用，读数 + 六项子指标 + 历史曲线。
 *
 * 分档阈值（25/45/55/75）**由后端给**（`bands`），这里只把 band 的 `key` 映射到颜色 ——
 * 所以后端调档位时色带、指针、图上的横线会一起动，不会出现「色带画在 45、
 * 数字却按 50 分档」这种对不上的情况。
 */

/** 五档配色：绿→灰→红，与全站「红涨绿跌」一致（恐慌=绿、贪婪=红）。 */
const BAND_COLOR: Record<string, string> = {
  // 极端两档用同色相再深一档（与 K 线里涨停/跌停标记同一套取色理由）：
  // 压在中间档上仍一眼看出是「更重的那一端」，又不引入第三种语义
  extreme_fear: CHART.limitDown,
  fear: CHART.down,
  neutral: CHART.fgDim,
  greed: CHART.up,
  extreme_greed: CHART.limitUp,
}

function bandColor(key: string): string {
  return BAND_COLOR[key] ?? CHART.fgDim
}

/** 分数落在哪一档。返回该档（含后端给的中文名），分数为 null 时返回 null。 */
function bandOf(bands: FearGreedBand[], score: number | null): FearGreedBand | null {
  if (score == null) return null
  // 与后端 `level_of` 同一判据：命中第一个「score <= upper」的档
  return bands.find((band) => score <= band.upper) ?? bands[bands.length - 1] ?? null
}

/** 读数色跟着分档走：红色贪婪 / 绿色恐慌，扫一眼就分得出方向。 */
function scoreColor(bands: FearGreedBand[], score: number | null): string {
  const band = bandOf(bands, score)
  return band ? bandColor(band.key) : 'var(--color-fg-dim)'
}

/**
 * 0–100 五档色带 + 位置指针。
 *
 * 段宽按各档的区间长度分配（25/20/10/20/25，不是等分）：这样指针位置与分数
 * 严格成比例 —— 读数 30 就落在「恐慌」段里偏右的地方，不会骗人。
 */
function ScoreBand({ bands, score }: { bands: FearGreedBand[]; score: number | null }) {
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
        {/* 指针用一圈面板底色的描边，把它从深色档里挑出来 */}
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
      {/* 刻度用绝对定位按边界位置摆（用 flex 均分会与段宽对不上） */}
      <div className="relative mt-1 h-[14px]">
        {[0, ...bands.map((band) => band.upper)].map((bound, i, arr) => (
          <span
            key={bound}
            className="num absolute text-[11px] text-fg-dim"
            style={{
              left: `${bound}%`,
              // 首尾两个刻度贴着两端，各挪半格以免被裁掉一半
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

/** 读数 + 分档 + 变化 + 色带。首页与情绪周期页共用这一块。 */
function ScoreHeader({
  score,
  level,
  change,
  bands,
}: {
  score: number | null
  level: string | null
  change: number | null
  bands: FearGreedBand[]
}) {
  const color = scoreColor(bands, score)
  return (
    <div className="flex flex-wrap items-end gap-x-8 gap-y-3 px-4 py-4">
      <div className="shrink-0">
        <div className="num text-[34px] leading-none font-semibold" style={{ color }}>
          {score == null ? '—' : fmtNum(score, 1)}
        </div>
        <div className="mt-2 flex items-center gap-3">
          <span className="text-[13px] tracking-[0.1em]" style={{ color }}>
            {level ?? '数据不足'}
          </span>
          {change != null && (
            <span className="num text-[12px] text-fg-dim">
              较上一交易日 {change >= 0 ? '+' : ''}
              {change.toFixed(1)}
            </span>
          )}
        </div>
      </div>
      <div className="min-w-[220px] flex-1">
        <ScoreBand bands={bands} score={score} />
      </div>
    </div>
  )
}

/**
 * 首页的一行读数。
 *
 * 放它在这里的理由：恐贪是「今天整体冷不冷」的**总结论**，先给这一句，
 * 再看下面市场情绪面板那十几个明细数字，扫读顺序才顺。
 */
export function FearGreedBrief({ data, delay = 0 }: { data: BriefData; delay?: number }) {
  return (
    <Panel
      title="恐惧贪婪指数"
      meta={<span className="num">{data.trade_date} · 越高越贪婪</span>}
      delay={delay}
    >
      <ScoreHeader
        score={data.score}
        level={data.level}
        change={data.change}
        bands={data.bands}
      />
      <div className="px-4 pb-3.5 text-[12px] leading-relaxed text-fg-dim">
        六项分位（波动率 / RSI / 相对换手率 / 融资融券 / 市场宽度 / 涨跌停强度）等权合成，
        明细与历史曲线见「情绪周期」页
      </div>
    </Panel>
  )
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
  delay = 0,
}: {
  data: FearGreed | null
  /** 取数中：显示「加载中…」而不是「暂无数据」—— 后者会被读成「真的没有」 */
  loading?: boolean
  delay?: number
}) {
  const latest = data?.latest ?? null
  const option = useMemo<ChartOption>(() => {
    if (!data || data.dates.length === 0) return {}
    const shortDates = data.dates.map(fmtShortDate)
    const levelAt = (value: unknown) => {
      if (typeof value !== 'number') return ''
      const band = bandOf(data.bands, value)
      return band ? `　${band.label}` : ''
    }
    return {
      grid: { ...GRID, top: 24 },
      tooltip: {
        ...TOOLTIP,
        trigger: 'axis' as const,
        formatter: (params: unknown) => {
          const items = params as { axisValue: string; value: unknown }[]
          if (!items?.length) return ''
          const value = items[0].value
          return `${items[0].axisValue}<br/>恐惧贪婪指数：${
            typeof value === 'number' ? `${value.toFixed(1)}${levelAt(value)}` : '—'
          }`
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
          name: '恐惧贪婪指数',
          // 机器算不出的日子原样传 null（图上断开）：填 0 会被画成「极度恐慌」，
          // 那是个截然相反的结论
          data: data.scores,
          smooth: false,
          symbol: 'none' as const,
          lineStyle: { width: 1.8, color: CHART.fg },
          itemStyle: { color: CHART.fg },
          areaStyle: { color: withAlpha(CHART.fgMuted, 0.1) },
          // 分档横线：线画在档的**上界**上、颜色取它上面那一档 —— 于是「跌破 45
          // 这条灰线」就进了中性，看图不必回查色带图例
          markLine: {
            silent: true,
            symbol: 'none' as const,
            data: data.bands.slice(0, -1).map((band, i) => ({
              yAxis: band.upper,
              lineStyle: {
                color: withAlpha(bandColor(data.bands[i + 1].key), 0.55),
                type: 'dashed' as const,
                width: 1,
              },
            })),
          },
        },
      ],
    }
  }, [data])

  if (!data || !latest) {
    return (
      <Panel title="恐惧贪婪指数" meta={<span>0–100 · 越高越贪婪</span>} delay={delay}>
        <div className="px-4 py-8 text-center text-[14px] text-fg-dim">
          {loading ? <span className="pulse-soft">加载中…</span> : '暂无数据，请先执行采集或回补'}
        </div>
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
          <EChart option={option} height={260} />
        </div>
      )}
    </Panel>
  )
}
