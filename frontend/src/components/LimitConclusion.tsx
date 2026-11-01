import type { LimitPool, LimitThemes } from '../api/types'
import { fmtInt } from '../lib/format'
import ConclusionBar, { ConclusionStat } from './ConclusionBar'

/**
 * 涨停复盘结论条（规范 v1 §5，Phase 5，2026-11-01；同日微调）。
 *
 * 全部取自本页**已经加载**的数据，没有新接口、也没有新指标：
 * - `pool`：涨停池（家数、连板梯队）
 * - `broken`：炸板池（家数）
 * - `themes`：今日题材共振（取家数前二做「主线」）
 *
 * ⚠️ **晋级率刻意不放进结论条**（2026-11-01 去重）：下方「连板晋级率」面板开头
 * 已经有「最新一日整体晋级率」的大字 + 四格统计，结论条再写一遍就是同一个数字
 * 上下相邻念两次。这里只留「家数 / 高度 / 炸板 / 主线」，晋级率只在那个面板出现。
 *
 * 数字缺失时统一显示 `—`，不拿 0 顶 —— 「没有数据」与「真的是 0 家」含义完全不同。
 */
export default function LimitConclusion({
  pool,
  broken,
  themes,
  loading = false,
}: {
  pool: LimitPool | null
  broken: LimitPool | null
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

  const topThemes = themes?.clusters.slice(0, 2) ?? []

  const parts: string[] = []
  if (upTotal != null) parts.push(`涨停 ${upTotal} 家`)
  if (maxBoard != null) parts.push(`最高 ${maxBoard} 板`)
  if (brokenTotal != null) parts.push(`炸板 ${brokenTotal} 家`)
  const headline = loading
    ? '加载中…'
    : parts.length > 0
      ? parts.join(' · ')
      : '当日涨停数据未入库'

  // 主线分三种状态（复查 P2-2）：还没取到（loading / 失败 → null）显示「—」，
  // 取到了但没有 ≥3 只共振的题材，才说「没有共振」；不要把「不知道」说成「没有」。
  const sub =
    themes == null ? (
      <span>主线 —</span>
    ) : topThemes.length > 0 ? (
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
      <span>主线 —（当日没有 ≥3 只共振的题材）</span>
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
        </>
      }
    />
  )
}
