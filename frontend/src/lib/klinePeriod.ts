import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
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

/**
 * 正在进行的「补 2 年历史」请求，按代码去重（2026-10-23 加）。
 *
 * 为什么必须有：补一次要等十几秒（iFinD），而用户在这期间切周 K→月 K 会触发两次
 * effect（`stale` 只挡 setState、挡不住已经发出的请求）。以前每次切 view 都会再发一次
 * `POST /sync`，第二次通常撞上后端 60 秒冷却返回 429，错误文案还被画到正常图表上
 * （2026-10-23 前端审计 P2，CDP 实测两次 POST）。
 *
 * 放在模块级而不是 ref：`useKLine` 在个股页与形态页各有一个实例，同一只票的补历史
 * 是「全站一次」的事，跨实例也该去重。
 */
const longSyncInFlight = new Map<string, Promise<void>>()

/**
 * 确保这只票补过长历史；已有同代码请求在途就**复用同一个 Promise**。
 * 成功才写两个「已补」标记（失败允许下次再试，见 stockNav 的说明）。
 */
function ensureLongHistory(code: string): Promise<void> {
  const running = longSyncInFlight.get(code)
  if (running) return running
  const task = api.syncStock(code, LONG_SYNC_DAYS).then(() => {
    markLongHistory(code)
    markSynced(code)
  })
  // 无论成败都从表里删掉：成功 → 后续走 hasLongHistory；失败 → 允许下次重试
  const tracked = task.finally(() => {
    longSyncInFlight.delete(code)
  })
  longSyncInFlight.set(code, tracked)
  return tracked
}

/** 429（60s 冷却）/ 409（采集正忙）是「等一会儿」的软失败，不往正常图表上盖红字。 */
function isSyncCooldown(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 409 || err.status === 429)
}

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
 *   序列，换成不复权会让形态关键位画在错误的高度）。不传则由用户在复权菜单里切，
 *   初始档位是**向前复权 + 成交量复权**（2026-09-26 用户要求：打开任何一只票都按
 *   复权口径看，不复权那档留着自己切）。
 */
export function useKLine(code: string, { fq: fixedFq }: { fq?: FqMode } = {}): KLineState {
  const [view, setView] = useState<KView>('day')
  const [fq, setFq] = useState<FqMode>('qfq')
  const [volAdjust, setVolAdjust] = useState(true)
  const [bars, setBars] = useState<KLineBar[]>([])
  // 有 code 就按「加载中」起步：effect 是在首次绘制之后才跑，初始 false 会让
  // 页面先闪一帧「没有取到该股的日线」（2026-10-21 体检）
  const [loading, setLoading] = useState(code !== '')
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
      // code 变空串（形态页拖评分滑块把当前票筛掉）时要把整套加载态一起复位 ——
      // 只清 bars 的话，loading/syncing 可能还停在 true，页面永远显示「加载日线…」
      setBars([])
      setLoading(false)
      setSyncing(false)
      setError(null)
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
          try {
            // 复用同一个在途 Promise：快速切周/月只发一次 POST（见 ensureLongHistory）
            await ensureLongHistory(code)
            rows = await api.stockDaily(code, query)
          } catch (err) {
            // 补长历史失败（60 秒冷却的 429 / 采集期间的 409）时**保留已经拿到的日线**，
            // 继续用它画图；只有连第一次取数都失败（外层 catch）时才置空。否则整张图会
            // 退化成一句报错，明明有数据可看（2026-10-10 修）。
            // 429/409 是「等一会儿就好」，不显示 —— 否则正常图表上会盖一行红字
            //（2026-10-23 前端审计 P2）。
            if (!stale && !isSyncCooldown(err)) setError((err as Error).message)
          } finally {
            if (!stale) setSyncing(false)
          }
        }
        // 周/月的横轴带上年份：2 年的跨度里 `09-30` 会撞上两个
        if (!stale) setBars(dailyBars(rows, { withYear: view !== 'day' }))
      } catch (err) {
        // 连第一次日线都没取到：这时确实没有数据可画，才置空并显示报错。
        // 404 = 这只票/这个交易日没有日线（后端语义），按空态处理，不显示成接口故障
        if (!stale) {
          setBars([])
          setError(err instanceof ApiError && err.status === 404 ? null : (err as Error).message)
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
