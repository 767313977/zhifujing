import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import type { KPeriod } from '../api/types'
import { dailyBars } from '../components/KLineChart'
import type { KLineBar } from '../components/KLineChart'
import { hasLongHistory, markLongHistory, markSynced } from './stockNav'

/**
 * 个股页与形态页共用的「K 线周期」逻辑。
 *
 * 三件事放在一起，是因为它们必须一致：**选哪个周期**、**按周期取数**、
 * **历史不够时按需补一次**。分开写的话，两个页面的「周K 到底取多少根」
 * 迟早会不一样，同一只票在两个页面画出不同的周线，那种不一致没法解释。
 */

/** 前端能选的周期。就是后端能聚合的三个，没有第四种 ——
 *  曾经有过「5 分 K」（走新浪、约 2 个月历史），实测在图上太挤看不清，已整体撤掉 */
export type KView = KPeriod

export const K_VIEWS: { key: KView; label: string; hint: string }[] = [
  { key: 'day', label: '日K', hint: '本地日线，个股页是不复权真实价' },
  { key: 'week', label: '周K', hint: '由本地日线按 ISO 周聚合，不额外取数' },
  { key: 'month', label: '月K', hint: '由本地日线按月聚合，不额外取数' },
]

/**
 * 各周期要取多少根**日线**来聚合。
 *
 * 周/月要的是长周期视野（2 年），所以直接按 500 个交易日要 —— 后端 `days`
 * 的上限就是 500。日线保持 250：图上够密，也没必要多传。
 */
const VIEW_DAYS: Record<KView, number> = { day: 250, week: 500, month: 500 }

/** 周/月 K 想要的历史跨度（自然日）。库里没这么长就先按需补一次 */
const LONG_SPAN_DAYS = 700

/** 一次补多少根日线 ≈ 500 个交易日 ≈ 2 年（后端上限） */
const LONG_SYNC_DAYS = 500

/** 最早那根 K 离今天有多远（自然日）。用原始行而不是格式化后的标签 ——
 *  标签只有 `MM-DD`，丢了年份，跨年时会算错 */
function spanDays(rows: { trade_date: string }[]): number {
  const first = rows[0]?.trade_date
  if (!first) return 0
  const stamp = new Date(`${first}T00:00:00`).getTime()
  return Math.round((Date.now() - stamp) / 86_400_000)
}

export interface KLineState {
  view: KView
  setView: (next: KView) => void
  bars: KLineBar[]
  loading: boolean
  /** 正在按需补长历史（周/月 K 第一次打开时会走这一步，一只票一次） */
  syncing: boolean
  error: string | null
  /** 重新取一次。页面在别处补完日线后要调它，否则图还是旧的那份 */
  reload: () => void
}

export function useKLine(
  code: string,
  { adjust = false }: { adjust?: boolean } = {},
): KLineState {
  const [view, setView] = useState<KView>('day')
  const [bars, setBars] = useState<KLineBar[]>([])
  const [loading, setLoading] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [nonce, setNonce] = useState(0)
  const reload = useCallback(() => setNonce((value) => value + 1), [])

  useEffect(() => {
    if (!code) {
      setBars([])
      return
    }
    let stale = false
    setLoading(true)
    setError(null)
    ;(async () => {
      try {
        let rows = await api.stockDaily(code, { days: VIEW_DAYS[view], adjust, period: view })
        // 周/月 K：本地只有 250 天，画出来的周线才 50 根、月线 12 根。
        // 不足 2 年就按需补一次（一只票约 11 次 iFinD 调用，落库后走缓存，
        // 本会话不再重复）—— 这是「取数窗口」之外唯一会花配额的地方。
        if (view !== 'day' && spanDays(rows) < LONG_SPAN_DAYS && !hasLongHistory(code)) {
          setSyncing(true)
          await api.syncStock(code, LONG_SYNC_DAYS)
          // 成功才标记（失败时下次打开还能再试），并且顺手把日线的那份标记也打上：
          // 这次补的是「2 年」，比个股页挂载时那次 250 天的更全
          markLongHistory(code)
          markSynced(code)
          rows = await api.stockDaily(code, { days: VIEW_DAYS[view], adjust, period: view })
        }
        // 周/月的横轴带上年份：2 年的跨度里 `09-30` 会撞上两个
        if (!stale) setBars(dailyBars(rows, { withYear: view !== 'day' }))
      } catch (err) {
        // 与「这只票本来就没有日线」分开：两者都落成空数组的话，图上那句
        // 「暂无数据」会把接口报错说成「数据源没有这只票」
        if (!stale) {
          setBars([])
          setError((err as Error).message)
        }
      } finally {
        if (!stale) {
          setLoading(false)
          setSyncing(false)
        }
      }
    })()
    return () => {
      stale = true
    }
  }, [code, view, adjust, nonce])

  return { view, setView, bars, loading, syncing, error, reload }
}
