import type { FearGreedBand, FearGreedBrief as BriefData } from '../api/types'
import { CHART } from '../lib/chart'
import { fmtNum } from '../lib/format'
import Panel from './Panel'

/**
 * 恐惧贪婪读数的**共用小块**：五档配色、色带、读数头，以及首页那一行读数。
 *
 * 为什么单独一个文件（2026-10-22 体检 #11）：首页只用到 `FearGreedBrief` 这一行 DOM，
 * 但它原来和画曲线的 `FearGreedPanel` 写在同一个文件里，而那个文件顶部就
 * `import EChart from './EChart'` —— EChart 再静态 import `echarts/core`。结果是
 * **首页（自己一个图表都不画）被强制下载整包 ECharts**：构建产物里 642 kB、
 * gzip 214 kB 的 `chart-*.js` 挂在首页 chunk 的静态依赖上。
 *
 * 所以这里**一个图表相关的东西都不能 import**（连 `EChart` 的类型都不行）。
 * 配色与分档判据仍然只在这里写一份，`FearGreedPanel` 反向从这里引用，避免两套阈值。
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

export function bandColor(key: string): string {
  return BAND_COLOR[key] ?? CHART.fgDim
}

/** 分数落在哪一档。返回该档（含后端给的中文名），分数为 null 时返回 null。 */
export function bandOf(bands: FearGreedBand[], score: number | null): FearGreedBand | null {
  if (score == null) return null
  // 与后端 `level_of` 同一判据：命中第一个「score <= upper」的档
  return bands.find((band) => score <= band.upper) ?? bands[bands.length - 1] ?? null
}

/** 读数色跟着分档走：红色贪婪 / 绿色恐慌，扫一眼就分得出方向。 */
export function scoreColor(bands: FearGreedBand[], score: number | null): string {
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
export function ScoreHeader({
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
 *
 * ⚠️ 这个文件不能引 EChart（原因见文件头），所以首页只为一句话的读数付出一点点体积。
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
