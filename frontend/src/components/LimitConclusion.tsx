import type { LimitPool, LimitThemes, PromotionSeries } from '../api/types'
import { fmtInt, fmtPct } from '../lib/format'
import ConclusionBar, { ConclusionStat } from './ConclusionBar'

/**
 * 涨停复盘结论条（规范 v1 §5，Phase 5，2026-11-01）。
 *
 * 全部取自本页**已经加载**的四份数据，没有新接口、也没有新指标：
 * - `pool`：涨停池（家数、连板梯队）
 * - `broken`：炸板池（家数）
 * - `promotion`：连板晋级率序列（全局整体口径的最新一天）
 * - `themes`：今日题材共振（取家数前二做「主线」）
 *
 * 数字缺失时统一显示 `—`，不拿 0 顶 —— 「没有数据」与「真的是 0 家」在这里含义完全不同。
 */
export default function LimitConclusion({
  pool,
  broken,
  promotion,
  themes,
  loading = false,
}: {
  pool: LimitPool | null
  broken: LimitPool | null
  promotion: PromotionSeries | null
  themes: LimitThemes | null
  loading?: boolean
}) {
  const upTotal = pool?.total ?? null
  const brokenTotal = broken?.total ?? null
  const ladder = pool?.ladder ?? []
  const maxBoard = ladder.length > 0 ? Math.max(...ladder.map((row) => row.consecutive)) : null
  // 首板家数：梯队里 consecutive === 1 的那一层。后端没给这一层时就是 null
  const firstBoard = ladder.find((row) => row.consecutive === 1)?.count ?? null
  const continuous = upTotal != null && firstBoard != null ? upTotal - firstBoard : null

  const latestIndex = promotion ? promotion.dates.length - 1 : -1
  const latestRate = latestIndex >= 0 ? promotion?.overall_rates[latestIndex] : null
  const latestBase = latestIndex >= 0 ? promotion?.overall_counts[latestIndex] : null
  const latestPromoted = latestIndex >= 0 ? promotion?.overall_promoted[latestIndex] : null

  const topThemes = themes?.clusters.slice(0, 2) ?? []

  const parts: string[] = []
  if (upTotal != null) parts.push(`涨停 ${upTotal} 家`)
  if (maxBoard != null) parts.push(`最高 ${maxBoard} 板`)
  if (brokenTotal != null) parts.push(`炸板 ${brokenTotal} 家`)
  if (latestRate != null) parts.push(`整体晋级率 ${fmtPct(latestRate, 1)}`)
  const headline = loading
    ? '加载中…'
    : parts.length > 0
      ? parts.join(' · ')
      : '当日涨停数据未入库'

  const sub =
    topThemes.length > 0 ? (
      <>
        <span>主线：</span>
        {topThemes.map((item) => (
          <span key={item.concept}>
            {item.concept}
            <span className="ml-1 text-fg-muted">{item.count} 只</span>
          </span>
        ))}
      </>
    ) : (
      <span>当日没有 ≥3 只共振的题材</span>
    )

  return (
    <ConclusionBar
      headline={headline}
      sub={sub}
      stats={
        <>
          <ConclusionStat
            label="首板"
            value={firstBoard != null ? `${fmtInt(firstBoard)} 只` : '—'}
            title="涨停梯队里 1 板那一层的家数"
          />
          <ConclusionStat
            label="连板"
            value={continuous != null ? `${fmtInt(continuous)} 只` : '—'}
            title="涨停总数 − 首板家数"
          />
          <ConclusionStat
            label="昨涨停基数"
            value={latestBase != null ? `${fmtInt(latestBase)} 只` : '—'}
            title="晋级率口径：昨日涨停股的家数"
          />
          <ConclusionStat
            label="今日晋级"
            value={latestPromoted != null ? `${fmtInt(latestPromoted)} 只` : '—'}
            title="其中今日仍涨停的数量"
          />
        </>
      }
    />
  )
}
