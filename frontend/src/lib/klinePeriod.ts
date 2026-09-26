import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import type { FqMode, KPeriod } from '../api/types'
import { dailyBars } from '../components/KLineChart'
import type { KLineBar } from '../components/KLineChart'
import { hasLongHistory, markLongHistory, markSynced } from './stockNav'

/**
 * 个股页与形态页共用的「K 线周期 + 复权」逻辑。
 *
 * 几件事放在一起，是因为它们必须一致：**选哪个周期**、**选哪种复权**、
 * **按周期取数**、**历史不够时按需补一次**。分开写的话，两个页面的「周K 到底取多少根」
 * 迟早会不一样，同一只票在两个页面画出不同的周线，那种不一致没法解释。
 */

/** 前端能选的周期。就是后端能聚合的三个，没有第四种 ——
 *  曾经有过「5 分 K」（走新浪、约 2 个月历史），实测在图上太挤看不清，已整体撤掉 */
export type KView = KPeriod

export const K_VIEWS: { key: KView; label: string; hint: string }[] = [
  { key: 'day', label: '日K', hint: '本地日线，复权方式见旁边的复权菜单' },
  { key: 'week', label: '周K', hint: '由本地日线按 ISO 周聚合，不额外取数' },
  { key: 'month', label: '月K', hint: '由本地日线按月聚合，不额外取数' },
]

/** 复权档位的中文名与说明。**菜单与图标题栏都从这里取**，别在两处各写一份
 *
 *  ⚠️ 同花顺那张菜单里「除权(不复权)」带 `Ctrl+C`，本站**故意不绑** ——
 *  浏览器里那是复制，抢掉的话页面上选不中文字就没法复制了。只绑 Ctrl+Q / Ctrl+B
 *  （见 `AdjustMenu`），不复权那项因此不显示快捷键。
 */
export const FQ_ITEMS: { key: FqMode; label: string; hint: string; shortcut?: string }[] = [
  {
    key: 'qfq',
    label: '向前复权',
    hint: '锚在最新价：图上最近的价格就是真实价，历史段按除权折算。形态引擎用的就是这条序列',
    shortcut: 'Ctrl+Q',
  },
  {
    key: 'hfq',
    label: '向后复权',
    hint: '锚在窗口首日：最早那根是真实价，之后的价格按除权放大。长周期看涨幅用这个',
    shortcut: 'Ctrl+B',
  },
  {
    key: 'none',
    label: '除权(不复权)',
    hint: '交易所真实价，与个股概况、涨跌停标记同一口径；除权日会看到跳空',
  },
]

/** 档位 → 中文名（从 FQ_ITEMS 取，别另立一份） */
export function fqLabel(mode: FqMode): string {
  return FQ_ITEMS.find((item) => item.key === mode)?.label ?? mode
}

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
  /** 当前生效的复权方式。固定模式下就是传进来的那个 */
  fq: FqMode
  /** 复权方式能不能改。形态页固定前复权，那边是 false，菜单也不渲染 */
  fqAdjustable: boolean
  setFq: (next: FqMode) => void
  volAdjust: boolean
  setVolAdjust: (next: boolean) => void
  bars: KLineBar[]
  loading: boolean
  /** 正在按需补长历史（周/月 K 第一次打开时会走这一步，一只票一次） */
  syncing: boolean
  error: string | null
  /** 重新取一次。页面在别处补完日线后要调它，否则图还是旧的那份 */
  reload: () => void
}

/**
 * @param options.fq 传了就是**固定**复权方式（形态页传 `'qfq'`：引擎判定用的就是那条
 *   序列，换成不复权会让形态关键位画在错误的高度）。不传则由用户在复权菜单里切，默认不复权。
 */
export function useKLine(code: string, { fq: fixedFq }: { fq?: FqMode } = {}): KLineState {
  const [view, setView] = useState<KView>('day')
  const [fq, setFq] = useState<FqMode>('none')
  const [volAdjust, setVolAdjust] = useState(false)
  const [bars, setBars] = useState<KLineBar[]>([])
  const [loading, setLoading] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [nonce, setNonce] = useState(0)
  const reload = useCallback(() => setNonce((value) => value + 1), [])

  // 固定模式下一切以传入值为准。成交量复权只对复权序列有意义：不复权时比例恒为 1，
  // 开关等于没开，所以那边直接按 false 发请求，省掉一次无意义的重取
  const mode = fixedFq ?? fq
  const vol = fixedFq ? false : volAdjust

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
        const query = { days: VIEW_DAYS[view], fq: mode, volAdjust: vol, period: view }
        let rows = await api.stockDaily(code, query)
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
          rows = await api.stockDaily(code, query)
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
  }, [code, view, mode, vol, nonce])

  return {
    view,
    setView,
    fq: mode,
    fqAdjustable: !fixedFq,
    setFq,
    volAdjust: vol,
    setVolAdjust,
    bars,
    loading,
    syncing,
    error,
    reload,
  }
}
