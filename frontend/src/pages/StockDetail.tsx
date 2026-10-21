import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client'
import type {
  ChipDistribution,
  FqMode,
  StockDde,
  StockDdeRow,
  StockNotices,
  StockProfile,
  StockReports,
  StockThemes,
  StockUnlocks,
  TrapCheck,
  TrapSignal,
} from '../api/types'
import AdjustMenu from '../components/AdjustMenu'
import Alert from '../components/Alert'
import EChart from '../components/EChart'
import type { ChartOption } from '../components/EChart'
import KLineChart from '../components/KLineChart'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import PhasePanel from '../components/PhasePanel'
import Segmented from '../components/Segmented'
import {
  AXIS_LABEL,
  AXIS_LINE,
  CHART,
  GRID,
  SPLIT_LINE,
  TOOLTIP,
} from '../lib/chart'
import { fmtAmount, fmtNum, fmtPct, fmtShortDate, toneOf } from '../lib/format'
import { K_VIEWS, fqLabel, useKLine } from '../lib/klinePeriod'
import { alreadySynced, markSynced, readStockList } from '../lib/stockNav'

/**
 * 少于这么多根 K 就认为「历史不全」，打开时补一次。
 *
 * 不能只看 `day_count === 0`：池子是按流动性筛的，而**池外的票（不少涨停股都在
 * 池外）是逐日攒起来的** —— 它们库里有几根 K，但画不出均线和形态，
 * 正好被 `=== 0` 这条判据漏掉。
 */
const NEED_BARS = 120

/**
 * 「所属题材」这一格最多列几个。
 *
 * 成分股快照覆盖全市场之后，宽泛的票能挂 20 多个板块（中国长城实测 22 个），
 * 全铺开会把概况这一行撑到七八行。这里只列前 N 个，其余用「等 N 个」收口，
 * 完整名单挂在 `title` 上悬浮可见（2026-10-21 用户要求）。
 */
const THEME_SHOW = 5

/** K 线面板的标题按周期变 */
const VIEW_LABEL: Record<string, string> = {
  day: '日 K',
  week: '周 K',
  month: '月 K',
}

/**
 * K 线标题栏那行小字。
 *
 * 复权那一段**跟着菜单走**：写死「不复权真实价」而用户在菜单里切了前复权的话，
 * 这行字就成了假话（它离菜单只有几十像素，自相矛盾最容易被看出来）。
 * 2026-09-26 起跌色是青不是绿（全站改同花顺配色，见设计文档 8.68）—— 原文案写「红涨绿跌」
 * 也是同一类问题，一并按现状写。
 */
function viewUnit(view: string, fq: FqMode, volAdjust: boolean): string {
  // 成交量复权单独标一句：勾上之后副图的量与前一根柱不可直接比（缩放过的）
  const vol = volAdjust && fq !== 'none' ? ' · 量复权' : ''
  // 不复权那档多说一句「真实价」：它与个股概况、涨跌停标记是同一口径
  const mode = fq === 'none' ? '除权(不复权)真实价' : fqLabel(fq)
  if (view === 'day') return `${mode}，红涨青跌${vol}`
  const how = view === 'week' ? '按 ISO 周' : '按月'
  return `${mode} · 由日线${how}聚合${vol}`
}

/**
 * 筹码分布（CYQ）一栏：**横向柱 = 每个价位的筹码占比**。
 *
 * 纵轴是价格（从低到高）、横轴是占比；现价及以下的筹码标红（获利盘）、
 * 之上的标绿（套牢盘），并用一条虚线标出现价。颜色只表示「在现价之上还是之下」，
 * 与涨跌无关 —— 这行说明写在图上方，避免被读成涨跌色。
 *
 * ⚠️ 不要给柱子排序：价格顺序就是分布的形状，按占比排序会把「筹码峰」压平。
 */
function buildChipOption(data: ChipDistribution): ChartOption {
  const rows = data.histogram
  const price = data.price
  // 现价在纵轴上的位置：取最接近的一格（横轴是类目轴，markLine 只能给类目下标）
  let priceIndex = -1
  if (price != null && rows.length) {
    let best = Number.POSITIVE_INFINITY
    rows.forEach((row, index) => {
      const gap = Math.abs(row.price - price)
      if (gap < best) {
        best = gap
        priceIndex = index
      }
    })
  }
  return {
    grid: { ...GRID, left: 58, right: 18, top: 14, bottom: 34 },
    tooltip: {
      ...TOOLTIP,
      trigger: 'axis' as const,
      axisPointer: { type: 'shadow' as const },
      formatter: (params: unknown) => {
        const items = params as { dataIndex: number }[] | undefined
        const row = rows[items?.[0]?.dataIndex ?? -1]
        if (!row) return ''
        return [
          `<b>${row.price.toFixed(2)} 元</b>`,
          `筹码占比 ${(row.weight * 100).toFixed(2)}%`,
        ].join('<br/>')
      },
    },
    xAxis: {
      type: 'value' as const,
      axisLabel: {
        ...AXIS_LABEL,
        formatter: (value: number) => `${(value * 100).toFixed(1)}%`,
      },
      splitLine: SPLIT_LINE,
      axisLine: { show: false },
    },
    yAxis: {
      type: 'category' as const,
      data: rows.map((row) => row.price.toFixed(2)),
      axisLabel: { ...AXIS_LABEL, interval: Math.max(0, Math.floor(rows.length / 8)) },
      axisLine: AXIS_LINE,
      axisTick: { show: false },
    },
    series: [
      {
        type: 'bar' as const,
        name: '筹码占比',
        data: rows.map((row) => ({
          value: row.weight,
          itemStyle: {
            color: price != null && row.price <= price ? CHART.up : CHART.down,
          },
        })),
        barMaxWidth: 6,
        markLine:
          priceIndex >= 0
            ? {
                symbol: 'none' as const,
                silent: true,
                label: { formatter: '现价', color: CHART.accent, fontSize: 11 },
                lineStyle: { color: CHART.accent, type: 'dashed' as const },
                data: [{ yAxis: priceIndex }],
              }
            : undefined,
      },
    ],
  }
}

/** 杀猪盘体检：三种状态的行内标记与颜色。`unknown` 必须与「未命中」明显区分。 */
const TRAP_STATUS: Record<
  TrapSignal['status'],
  { icon: string; className: string; label: string }
> = {
  hit: { icon: '●', className: 'text-danger', label: '命中' },
  miss: { icon: '○', className: 'text-fg-muted', label: '未命中' },
  unknown: { icon: '—', className: 'text-fg-dim', label: '数据不足' },
  info: { icon: '·', className: 'text-fg-dim', label: '参考' },
}

const TRAP_LEVEL_CLASS: Record<TrapCheck['level'], string> = {
  high: 'text-danger',
  orange: 'text-danger',
  medium: 'text-accent',
  low: 'text-ok',
}

/** 个股事件面板的三个 tab。 */
type EventTab = 'notices' | 'reports' | 'unlock'
const EVENT_TABS: { key: EventTab; label: string; hint: string }[] = [
  { key: 'notices', label: '公告', hint: '最近 180 天（缓存 6 小时）' },
  { key: 'reports', label: '研报', hint: '机构研报（缓存 1 天）' },
  { key: 'unlock', label: '解禁', hint: '未来的限售解禁排期（缓存 1 天）' },
]

/** 解禁股数：按亿 / 万格式化（上游给的是股数，不是金额）。 */
function fmtShares(value: number | null): string {
  if (value == null) return '—'
  if (value >= 1e8) return `${(value / 1e8).toFixed(2)} 亿股`
  if (value >= 1e4) return `${(value / 1e4).toFixed(2)} 万股`
  return `${Math.round(value)} 股`
}

/**
 * DDE 一栏的图：**柱 = 主力净流入额**（逐条上色，正红负绿）、**线 = 5日DDE**。
 *
 * 两个指标的单位都是元、量级又相近（都是亿元上下），所以共用一根 Y 轴；
 * 柱子的红绿只表示**流入还是流出**，与「净流入额比前一天多了还是少了」无关。
 *
 * 缺值（来源没给那一天）一律留空：柱不画、线断开，不补 0、也不插值。
 */
function buildDdeOption(rows: StockDdeRow[]): ChartOption {
  return {
    // **不画图例**：柱子是逐条上色的（净流入红、净流出绿），而图例一个系列只能给一个
    // 色块 —— 画出来就是个蓝色小方块配一堆红绿柱，反而误导人。颜色规则改用图上方那行
    // 小字说明（与板块资金流面板同一套做法）。
    grid: { ...GRID },
    tooltip: {
      ...TOOLTIP,
      trigger: 'axis' as const,
      axisPointer: {
        type: 'line' as const,
        lineStyle: { color: CHART.fgDim, type: 'dashed' as const },
      },
      formatter: (params: unknown) => {
        const items = params as { dataIndex: number }[] | undefined
        const row = rows[items?.[0]?.dataIndex ?? -1]
        if (!row) return ''
        return [
          `<b>${row.trade_date}</b>`,
          `主力净流入额 ${fmtAmount(row.net_inflow)}`,
          `5日DDE ${fmtAmount(row.dde)}`,
        ].join('<br/>')
      },
    },
    xAxis: {
      type: 'category' as const,
      data: rows.map((row) => fmtShortDate(row.trade_date)),
      // `interval` 是「隔几个类目显示一个」（0 = 全显示）。用 `floor` 而不是 `ceil`
      // —— 后者在窗口短时得 1，会把一半日期标签吃掉
      axisLabel: { ...AXIS_LABEL, interval: Math.max(0, Math.floor(rows.length / 8)) },
      axisLine: AXIS_LINE,
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value' as const,
      // 不设 scale：净流入有正有负，轴必须把 0 包进来，否则看不出零基线在哪
      axisLabel: { ...AXIS_LABEL, formatter: (value: number) => fmtAmount(value) },
      splitLine: SPLIT_LINE,
      axisLine: { show: false },
    },
    series: [
      {
        type: 'bar' as const,
        name: '主力净流入额',
        data: rows.map((row) => ({
          value: row.net_inflow,
          // 逐条上色：净流入为正红、为负绿（A 股惯例）
          itemStyle: {
            color: row.net_inflow != null && row.net_inflow < 0 ? CHART.down : CHART.up,
          },
        })),
        barMaxWidth: 14,
      },
      {
        type: 'line' as const,
        name: '5日DDE',
        data: rows.map((row) => row.dde),
        symbol: 'none' as const,
        // 缺值绝不插值：连出一条假线比断开更容易被读成「数据是连续的」
        connectNulls: false,
        lineStyle: { width: 1.6, color: CHART.accent },
        itemStyle: { color: CHART.accent },
      },
    ],
  }
}

export default function StockDetail() {
  const { code = '' } = useParams<{ code: string }>()
  const [profile, setProfile] = useState<StockProfile | null>(null)
  const [themes, setThemes] = useState<StockThemes | null>(null)
  const [dde, setDde] = useState<StockDde | null>(null)
  // 筹码分布（本地计算，零外部请求）。增强读数：请求失败只当 null，不拖垮整页
  const [chips, setChips] = useState<ChipDistribution | null>(null)
  // 杀猪盘体检（库内信号 + 东财新闻）。同样按增强读数处理
  const [trap, setTrap] = useState<TrapCheck | null>(null)
  // 用户补充背景（朋友推荐 / 群里说…）与重新体检的进行态
  const [trapContext, setTrapContext] = useState('')
  const [trapChecking, setTrapChecking] = useState(false)
  // 个股事件（公告 / 研报 / 解禁）：切到哪个 tab 才拉哪个，各自缓存；失败只影响这一块
  const [eventTab, setEventTab] = useState<EventTab>('notices')
  const [notices, setNotices] = useState<StockNotices | null>(null)
  const [reports, setReports] = useState<StockReports | null>(null)
  const [unlocks, setUnlocks] = useState<StockUnlocks | null>(null)
  const [eventsLoading, setEventsLoading] = useState(false)
  const [eventsError, setEventsError] = useState<string | null>(null)
  // DDE 请求本身失败的原因（网络 / 超时 / 5xx）。null 表示请求成功 —— 此时 `dde`
  // 为 null 或 rows 为空都只是「这只票没数据」，与「取数失败」是两件事（2026-10-10 修）
  const [ddeError, setDdeError] = useState<string | null>(null)
  // 「DDE / 杀猪盘这两块慢请求还在路上」——它们**不阻塞首屏**（见 `load` 的注释），
  // 各自到货后再填。没有这两个标志的话，加载途中 `dde`/`trap` 都是 null，面板会先闪
  // 一句「暂时没有数据 / 无法体检」，几百毫秒后又跳成有数据，观感像出过错。
  const [ddeLoading, setDdeLoading] = useState(true)
  const [trapLoading, setTrapLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // 只代表「概况那批请求（stockProfile / stockThemes / stockDde 的 Promise.all）失败」。
  // 补长历史（syncStock）与加自选的失败**只写上面的 error**：它们挂了不代表概况没取到，
  // 共用一个状态会让 DDE 面板误报「资金流取数失败：采集正忙」；且顶部 Alert 关闭只清
  // error，面板判据若也挂在 error 上，红条一关面板就永久「加载中…」（2026-10-11 修）
  const [profileError, setProfileError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  // 周期、取数、按需补历史都在这个 hook 里（与形态页共用一套）
  const kline = useKLine(code)
  const { reload: reloadKline } = kline

  const navigate = useNavigate()
  /**
   * 当前路由参数 code 的最新值。← → 切票只改路由参数、组件**不卸载**，而
   * `toggleWatch` 的回调只依赖 profile/load（不含 code）—— 它 `await` 之后如果用户
   * 已经切走，回调里 `profile` 仍是旧票，用这个 ref 比对「自选请求回来时是否还在
   * 同一只票」，不在就丢掉这次 `setProfile`（否则概况/题材会显示成上一只票）。
   */
  const codeRef = useRef(code)
  useEffect(() => {
    codeRef.current = code
  }, [code])
  /**
   * 上一次是从哪份列表点进来的（形态命中 / 自选 / 板块成分股…）。
   * 挂载时读一次就够：详情页自己不会改写它，切股票也只是在同一份列表里位移。
   */
  const [codes] = useState(() => readStockList())
  const index = codes.indexOf(code)
  const prev = index > 0 ? codes[index - 1] : null
  const next = index >= 0 && index < codes.length - 1 ? codes[index + 1] : null

  /**
   * @param isCancelled 判断「这次请求还算不算数」。三次请求回来的顺序不保证，
   *   ← → 快速连按时上一只票的响应可能晚于当前这只到达 —— 不判的话概况/题材/DDE
   *   会**显示成另一只票**（K 线在 hook 里有自己的守卫，不会错；这三块原来没有，
   *   见 2026-09-27 的修复）。页面内的用户动作（`toggleWatch`）也要判：它 await 期间
   *   用户可能已经切票，传 `() => codeRef.current !== target` 即可与路由参数比对。
   */
  const load = useCallback(
    async (target: string, isCancelled: () => boolean = () => false) => {
      // 题材要现取，可能失败（比如首次打开、本地还没名称），不该拖垮整页；
      // DDE 同理 —— 它的失败原因后端会放在 note 里，这里兜住的只是传输/服务端层面的错。
      // 兜住之后要**记下失败原因**：`dde` 变 null 既可能是「请求失败」也可能是「本来没数据」，
      // 不区分就会被下面同一句文案盖住（2026-10-10 修）
      const ddeRequest = api.stockDde(target).then(
        (data) => ({ data, error: null as string | null }),
        (err: Error) => ({ data: null, error: err.message }),
      )
      // 筹码分布是本地计算、很快；失败也不该拖垮整页（增强读数一律回落 null）
      const chipsRequest = api.stockChips(target).catch(() => null)
      const trapRequest = api.stockTrap(target).catch(() => null)
      // ⚠️ 首屏只等**本地就能算完**的三块（2026-10-20 改）：
      // - `stockDde` 未命中缓存时要现取 iFinD，可能十几秒；
      // - `stockTrap` 首次要现取东财个股新闻，实测 3.7 秒（第二次走缓存才 0.04s）。
      // 原先这两块一起塞进 Promise.all，导致**整页白屏等最慢的那个** —— 用户点进来
      // 感觉「加载非常慢」，而后端概况本身只要 0.01~0.13 秒。现在它们各自到货再填。
      //
      // 两块慢请求的回调要在**等首屏之前**注册（2026-10-21 复查 #7）：概况请求失败时
      // 上面那句 Promise.all 会直接抛出去，若把 `.then` 放在它后面就永远不会注册，
      // `ddeLoading` / `trapLoading` 会一直停在 true。同时在这里把它们重置为 true ——
      // 手动同步后重新调 `load` 时，第二次取数期间不该继续显示上一轮的旧数据。
      setDdeLoading(true)
      setTrapLoading(true)
      void ddeRequest.then((result) => {
        if (isCancelled()) return
        setDde(result.data)
        setDdeError(result.error)
        setDdeLoading(false)
      })
      void trapRequest.then((data) => {
        if (isCancelled()) return
        setTrap(data)
        setTrapLoading(false)
      })
      const [p, themeData, chipData] = await Promise.all([
        api.stockProfile(target),
        api.stockThemes(target).catch(() => null),
        chipsRequest,
      ])
      if (isCancelled()) return p
      setProfile(p)
      setThemes(themeData)
      setChips(chipData)
      return p
    },
    [],
  )

  useEffect(() => {
    let cancelled = false
    // 切票先清掉上一只票的概况 / 题材 / DDE / 阶段判定 —— 否则新票加载中（尤其失败时）
    // 页面上留着的还是上一只票的数据，连「加入自选」按钮操作的也会是旧票
    //（按钮在 profile 为 null 时禁用，所以顺带挡掉了误操作旧票）。2026-10-10 修。
    setProfile(null)
    setThemes(null)
    setDde(null)
    setDdeError(null)
    setChips(null)
    setTrap(null)
    // 两块异步请求的标志跟着一起复位，否则切票后新票的面板会沿用上一只票的「已加载」态
    setDdeLoading(true)
    setTrapLoading(true)
    setTrapContext('')
    setEventTab('notices')
    setNotices(null)
    setReports(null)
    setUnlocks(null)
    setEventsError(null)
    setError(null)
    setProfileError(null)
    ;(async () => {
      try {
        const p = await load(code, () => cancelled)
        // 本地没有缓存、或历史明显不全时自动补一次，之后走缓存。
        // alreadySynced 兜住「本来就短」的票，免得每次打开都再补一遍
        if (!cancelled && p.day_count < NEED_BARS && !alreadySynced(code)) {
          // 补长历史这一段**自己兜错**：失败只写页面级 error（顶部红条提示即可），
          // 不写 profileError —— 概况那批已经取到了，别让 DDE 面板跟着报
          // 「资金流取数失败：采集正忙」并藏掉图表（2026-10-11 修）
          try {
            setSyncing(true)
            await api.syncStock(code, 250)
            // 标记放在**成功之后**：采集正忙（409）或 iFinD 出错时这次补采是白跑的，
            // 提前标记会让本会话内再打开这只票也不再重试 —— 表现就是「点进去没日K、
            // 刷新也没用」，只能关掉标签页重开。失败就让它下次打开再试一次
            markSynced(code)
            if (!cancelled) {
              await load(code, () => cancelled)
              // 图的数据在 hook 里，得让它重取一次，否则补完还是空的
              reloadKline()
            }
          } catch (err) {
            if (!cancelled) setError((err as Error).message)
          }
        }
      } catch (err) {
        // 走到这里只剩 load() 失败：概况那批整体没取到，页面红条与面板失败态一起给
        if (!cancelled) {
          setError((err as Error).message)
          setProfileError((err as Error).message)
        }
      } finally {
        if (!cancelled) setSyncing(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [code, load, reloadKline])

  /**
   * 个股事件：**切到哪个 tab 才拉哪个**。
   *
   * 不跟着概况一起 `Promise.all`：这三条都要出外网，首次打开会各花一两秒，
   * 塞进首屏只会让整页更慢。已经拉过的 tab 用 state 里的结果，切回来不重复请求；
   * 失败只写 `eventsError`，不影响 K 线 / 概况 / 体检。
   */
  useEffect(() => {
    if (!code) return
    let cancelled = false
    const finish = () => {
      if (!cancelled) setEventsLoading(false)
    }
    setEventsError(null)
    if (eventTab === 'notices' && notices === null) {
      setEventsLoading(true)
      api
        .stockNotices(code)
        .then((data) => {
          if (!cancelled) setNotices(data)
        })
        .catch((err) => {
          if (!cancelled) setEventsError((err as Error).message)
        })
        .finally(finish)
      return () => {
        cancelled = true
      }
    }
    if (eventTab === 'reports' && reports === null) {
      setEventsLoading(true)
      api
        .stockReports(code)
        .then((data) => {
          if (!cancelled) setReports(data)
        })
        .catch((err) => {
          if (!cancelled) setEventsError((err as Error).message)
        })
        .finally(finish)
      return () => {
        cancelled = true
      }
    }
    if (eventTab === 'unlock' && unlocks === null) {
      setEventsLoading(true)
      api
        .stockUnlock(code)
        .then((data) => {
          if (!cancelled) setUnlocks(data)
        })
        .catch((err) => {
          if (!cancelled) setEventsError((err as Error).message)
        })
        .finally(finish)
      return () => {
        cancelled = true
      }
    }
    setEventsLoading(false)
    return () => {
      cancelled = true
    }
  }, [code, eventTab, notices, reports, unlocks])

  /**
   * ← → 翻上/下一只。
   *
   * `index < 0` 表示没有列表上下文（比如直接输 URL 打开），这时完全不接管
   * 方向键 —— 按了不该有任何反应。
   */
  useEffect(() => {
    if (index < 0) return
    const onKey = (event: KeyboardEvent) => {
      const el = event.target as HTMLElement | null
      // 焦点在输入框 / 下拉里时不劫持方向键：详情页目前没有输入框，
      // 但这类「后来才加的元素」最容易忽略，事后又最难查
      if (
        el &&
        (el.tagName === 'INPUT' ||
          el.tagName === 'TEXTAREA' ||
          el.tagName === 'SELECT' ||
          el.isContentEditable)
      ) {
        return
      }
      const target =
        event.key === 'ArrowLeft' ? prev : event.key === 'ArrowRight' ? next : null
      if (!target) return
      event.preventDefault()
      navigate(`/stock/${encodeURIComponent(target)}`)
      // 换票后回到顶部：否则会停在上只票看到一半的位置，看着却是另一只票的内容
      window.scrollTo({ top: 0 })
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [index, prev, next, navigate])

  const toggleWatch = useCallback(async () => {
    if (!profile) return
    try {
      if (profile.in_watchlist) {
        await api.removeWatchlist(profile.code)
        setNotice('已移出自选')
      } else {
        await api.addWatchlist(profile.code, profile.name ?? undefined)
        setNotice('已加入自选')
      }
      // 用 codeRef 兜住「点完自选立刻切票」：请求回来时若已不在 profile.code 这只票上，
      // load 会丢弃这次 setProfile，别把上一只票的概况/题材盖到新票上
      await load(profile.code, () => codeRef.current !== profile.code)
    } catch (err) {
      setError((err as Error).message)
    }
  }, [profile, load])

  /**
   * 带「用户补充背景」重新跑一次杀猪盘体检。
   *
   * 失败时**保留上一次结果**：网络失败不该把已经算出来的体检清空成「无法体检」；
   * 页面重试的成本很低，误清空反而让人以为数据没了。
   */
  const recheckTrap = useCallback(async () => {
    if (!code) return
    setTrapChecking(true)
    try {
      setTrap(await api.stockTrap(code, trapContext.trim()))
    } catch {
      // 保留旧结果
    } finally {
      setTrapChecking(false)
    }
  }, [code, trapContext])

  const latest = profile?.latest
  /**
   * DDE 的最新一行。序列是升序的，所以取末尾 —— 它是**最近一个交易日的收盘终值**，
   * 盘中打开时通常就是昨天（见后端 `collect_dde.CLOSE_READY`），所以界面上不写「今日」。
   */
  const latestDde = dde && dde.rows.length > 0 ? dde.rows[dde.rows.length - 1] : null
  // 缓存图表 option：否则每次父组件重渲染（如 loading 变化）都会造一份新对象，
  // EChart 的 setOption(notMerge) 就整图重建一次（2026-10-10 修）
  const ddeOption = useMemo(() => (dde ? buildDdeOption(dde.rows) : null), [dde])
  const chipOption = useMemo(
    () => (chips && chips.histogram.length ? buildChipOption(chips) : null),
    [chips],
  )
  /**
   * 所属题材（并进概况格的「所属题材」那一格，2026-10-10 用户要求）。
   *
   * 原来是一整块带板块涨跌幅的 Panel，压成两行：名称一行、**同序**对应的涨跌幅一行。
   * 数据来源 2026-10-21 改了：优先读板块成分股快照（覆盖全市场），没快照才退回
   * 「最近一次涨停所属的精选板块」—— 以前只看涨停股，非涨停股永远是「—」。
   * 一只票可能挂十几个板块（中国长城实测 22 个），这一格用 compact 字号，
   * 并且最多列前 5 个（其余收口成「等 N 个」，完整名单见 title）。
   * 全是「身份 / 业绩」标签时显示「—」，悬浮里说明被剔掉的是哪些（不冒充题材）。
   */
  const themeItems = themes?.themes ?? []
  // 被黑名单剔掉的标签：没有真题材时页面显示「—」，靠它做悬浮说明
  const themeSuppressed = themes?.suppressed ?? []
  // 最多列 THEME_SHOW 个，其余收口成「等 N 个」；完整名单放 title
  const themeShown = themeItems.slice(0, THEME_SHOW)
  const themeMore = themeItems.length - themeShown.length
  const themeNames = themeItems.length
    ? `${themeShown.map((theme) => theme.concept).join('、')}${
        themeMore > 0 ? ` 等 ${themeItems.length} 个` : ''
      }`
    : syncing
      ? '同步中…'
      : '—'
  // 涨跌幅与上面的名称同序，只给列出来的那几个
  const themePcts = themeItems.length
    ? themeShown.map((theme) => fmtPct(theme.pct_chg)).join('、')
    : undefined
  const themeTitle = themeItems.length
    ? themeItems.map((theme) => theme.concept).join('、')
    : themeSuppressed.length
      ? `仅身份/业绩标签：${themeSuppressed.join('、')}`
      : undefined
  const toolbar = (
    <>
      {/* 从列表点进来才有这份上下文；直接输 URL 打开时不显示，
          免得给出一个「按了没反应」的提示 */}
      {codes.length > 1 && index >= 0 && (
        <span className="num hidden text-[13px] text-fg-dim md:inline">
          ← → 切换 · {index + 1} / {codes.length}
        </span>
      )}
      {syncing && (
        <span className="num pulse-soft text-[13px] text-accent">同步中…</span>
      )}
      <button
        type="button"
        onClick={() => void toggleWatch()}
        disabled={!profile}
        className="num border border-line px-2.5 py-[3px] text-[13px] text-fg-muted transition-colors hover:border-accent/50 hover:text-accent disabled:cursor-not-allowed disabled:opacity-40"
      >
        {profile?.in_watchlist ? '移出自选' : '加入自选'}
      </button>
    </>
  )

  // 当前 tab 的数据。三张表结构不同，这里只用它们共有的 note / stale / fetched_at
  const eventData =
    eventTab === 'notices' ? notices : eventTab === 'reports' ? reports : unlocks

  return (
    <Layout toolbar={toolbar}>
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}
      {notice && (
        <Alert tone="accent" onClose={() => setNotice(null)}>
          {notice}
        </Alert>
      )}

      <div className="space-y-4">
        <Panel
          title={`${profile?.name ?? code}  ${profile?.code ?? ''}`}
          meta={
            <span className="num">
              {profile
                ? `本地缓存 ${profile.day_count} 天 · ${profile.first_date ?? '—'} ~ ${profile.last_date ?? '—'}`
                : // 概况接口失败时 profile 永远不会被赋值，这里看 profileError 而不是
                  // 页面级 error —— 后者还被补历史/加自选的失败共用、且会被 Alert 的关闭
                  // 清掉，拿它当判据要么误报失败、要么关掉红条后永久「加载中…」
                  profileError
                  ? '概况加载失败'
                  : '加载中…'}
              <Link to="/watchlist" className="ml-3 text-fg-dim hover:text-fg">
                ← 回自选
              </Link>
            </span>
          }
          delay={40}
        >
          {/* 12 格（仍能被 2/3/4 整除，各档列数下都不剩半行空格）。
              与 2026-09-26 那份清单相比，2026-10-10 把「实际换手率」换成了「所属题材」
              —— 实际换手率移到资金流向（DDE）面板的自由流通市值旁边，所属题材从一整块
              Panel 压进这一格。
              市值 / 自由流通股 / 预测市盈率由建池时那次选股接口一并取回（加列不加调用
              次数，见设计文档 8.68.13）。缺值的格子按站点惯例显示「—」。 */}
          <div className="grid grid-cols-2 overflow-hidden md:grid-cols-3 xl:grid-cols-4">
            <Cell label="开盘价" value={fmtNum(latest?.open, 2)} />
            <Cell
              label="收盘价"
              value={fmtNum(latest?.close, 2)}
              tone={toneOf(latest?.pct_chg)}
            />
            <Cell label="涨跌幅" value={fmtPct(latest?.pct_chg)} tone={toneOf(latest?.pct_chg)} />
            <Cell
              label="五日涨跌幅"
              value={fmtPct(profile?.pct_chg_5d)}
              tone={toneOf(profile?.pct_chg_5d)}
            />
            <Cell label="成交量" value={`${fmtAmount(latest?.volume)}股`} />
            <Cell label="成交额" value={fmtAmount(latest?.amount)} />
            <Cell
              label="复盘关联"
              value={`涨停 ${profile?.limit_up_dates.length ?? 0} 次`}
              sub={`龙虎榜 ${profile?.lhb_count ?? 0} 次`}
            />
            <Cell label="换手率" value={fmtNum(latest?.turnover, 2, '%')} />
            <Cell
              label="所属题材"
              value={themeNames}
              sub={themePcts}
              title={themeTitle}
              compact
            />
            <Cell label="总市值" value={fmtAmount(profile?.total_mv)} />
            <Cell label="自由流通市值" value={fmtAmount(profile?.free_float_mv)} />
            <Cell label="动态市盈率" value={fmtNum(profile?.pe_forecast, 2)} />
          </div>

          {latest?.trade_date && (
            <div className="num border-t border-line-soft px-4 py-2 text-[13px] text-fg-dim">
              最新数据 {latest.trade_date}
              {/* 「开」不再重复列 —— 上面已经有一格「开盘价」 */}
              <span className="mx-2">·</span>
              高 {fmtNum(latest.high, 2)}
              <span className="mx-2">·</span>
              低 {fmtNum(latest.low, 2)}
            </div>
          )}
        </Panel>

        <Panel
          title={`${VIEW_LABEL[kline.view]} 线`}
          meta={
            <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="num">
                {kline.loading || kline.syncing
                  ? '取数中…'
                  : `${kline.bars.length} 根 · ${viewUnit(kline.view, kline.fq, kline.volAdjust)}`}
              </span>
              <span className="flex items-center gap-2">
                <Segmented value={kline.view} items={K_VIEWS} onChange={kline.setView} />
                {kline.fqAdjustable && (
                  <AdjustMenu
                    fq={kline.fq}
                    volAdjust={kline.volAdjust}
                    onChange={kline.setFq}
                    onToggleVolume={kline.setVolAdjust}
                  />
                )}
              </span>
            </span>
          }
          delay={80}
        >
          {kline.loading || syncing ? (
            <div className="flex h-[420px] items-center justify-center text-[14px] text-fg-dim">
              <span className="pulse-soft">
                {kline.syncing ? '正在补 2 年历史（周/月 K 要的长周期）…' : '加载中…'}
              </span>
            </div>
          ) : kline.bars.length > 0 ? (
            <div className="px-2 pt-2">
              {/* 有数据、但补长历史那一步失败时，保留日线继续画，只在图上方说明原因 ——
                  不能因为一次补数失败就把整张图换成一句报错（2026-10-10 修） */}
              {kline.error && (
                <div className="num mb-1 px-2 text-[13px] text-danger">{kline.error}</div>
              )}
              <KLineChart bars={kline.bars} height={420} />
            </div>
          ) : kline.error ? (
            <div className="px-4 py-10 text-center text-[14px] text-danger">
              {kline.error}
            </div>
          ) : (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              没有取到该股的日线 —— 同步失败（比如采集正忙）或该股当日无行情，稍后刷新重试
            </div>
          )}
        </Panel>

        <Panel
          title="资金流向（DDE）"
          meta={<span className="num">iFinD 口径 · 主力净流入额与 5日DDE · 单位元</span>}
          delay={100}
        >
          {/* 与 profile 同一个 Promise.all 里取回来的：profile 还没到且概况无错 = 在加载；
              但 profile 失败时 Promise.all 整批 reject、setDde/setDdeError 都不会执行，
              这时要跟着显示整体失败态，不能永远转圈（也别落到「暂无 DDE 数据」那句）。
              判据用 profileError 而非页面级 error：后者被补历史（syncStock 409 采集正忙）、
              加自选的失败共用，DDE 明明取到了数据却会误报失败并藏掉图表（2026-10-11 修）。
              注意 ddeError 只代表 DDE 自己的请求失败，和 profileError 是两件事 */}
          {profileError ? (
            <div className="px-4 py-10 text-center text-[14px] text-danger">
              资金流取数失败：{profileError}（刷新页面重试）
            </div>
          ) : profile === null ? (
            <div className="flex h-[240px] items-center justify-center text-[14px] text-fg-dim">
              <span className="pulse-soft">加载中…</span>
            </div>
          ) : ddeLoading ? (
            // 首屏不等 DDE（见 `load` 的注释）：概况已出来，这块还在路上
            <PanelLoading label="资金流取数中…（DDE 需要现取，稍等几秒）" />
          ) : ddeError ? (
            // 请求本身失败（网络 / 超时 / 5xx）：与「本来就没数据」分开说 ——
            // 原来 `dde` 为 null 会落到下面那句「暂时没有 DDE 数据」，把取数失败读成了没数据
            <div className="px-4 py-10 text-center text-[14px] text-danger">
              DDE 取数失败：{ddeError}（可重试）
            </div>
          ) : !dde || dde.rows.length === 0 ? (
            // note 有值就是取数失败的原因（iFinD 报错、或这只票本来就没数据），原样显示，
            // 不画空图；note 也是空的时候才退回到「没有数据」这句
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              {dde?.note ?? '这只票暂时没有 DDE 数据'}
            </div>
          ) : (
            <>
              {/* 有数据但 note 也有值 = 后端把上次取数失败的原因带出来了（画的是库里的
                  旧数）。这种情况不能把说明吞掉，否则会被当成最新的终值看 */}
              {dde.note && (
                <div className="border-b border-line-soft px-4 py-2 text-[13px] text-fg-dim">
                  {dde.note}
                </div>
              )}
              {/* 四格：DDE 那两个数 + 自由流通市值 + 实际换手率（2026-10-10 用户要求
                  把实际换手率移到自由流通市值旁边）。后两个都不是 DDE 指标，是「这笔
                  资金流落在多大的盘子上」的旁证 —— 同样一笔净流入，流通盘小、换手高，
                  意义差很多。都来自 profile（建池时取回，加列不花调用次数）。
                  窄屏两列、宽屏一列排开（四格在手机上挤成一列会截断标签） */}
              <div className="grid grid-cols-2 overflow-hidden md:grid-cols-4">
                <Cell
                  label="主力净流入额"
                  value={fmtAmount(latestDde?.net_inflow)}
                  tone={toneOf(latestDde?.net_inflow)}
                />
                <Cell
                  label="5日DDE"
                  value={fmtAmount(latestDde?.dde)}
                  tone={toneOf(latestDde?.dde)}
                />
                <Cell label="自由流通市值" value={fmtAmount(profile?.free_float_mv)} />
                <Cell label="实际换手率" value={fmtNum(profile?.actual_turnover, 2, '%')} />
              </div>
              <div className="px-2 pt-2">
                <div className="mb-1 text-[12px] text-fg-dim">
                  柱 = 主力净流入额（红=净流入 绿=净流出）· 线 = 5日DDE
                </div>
                {ddeOption && <EChart option={ddeOption} height={240} />}
              </div>
            </>
          )}
        </Panel>

        {/* 筹码分布与阶段判定**并排**（2026-10-20 用户要求）：筹码图单占一行太宽，
            右边空着一大片；阶段判定原先在资金流向之后单独一行，挪上来正好填满。
            窄屏（<lg）自动叠成一列。`items-start` 让两块各自按内容高度对齐顶部，
            不把较矮的那块拉伸成同样高 */}
        <div className="grid items-start gap-4 lg:grid-cols-2">
          <Panel
            title="筹码分布"
            meta={
              <span className="num">
                {chips?.trade_date ? `${chips.trade_date} · ` : ''}
                本地按换手率衰减推演 · 红=获利筹码 绿=套牢筹码
              </span>
            }
            delay={105}
          >
            {profile === null ? (
              <div className="flex h-[240px] items-center justify-center text-[14px] text-fg-dim">
                <span className="pulse-soft">加载中…</span>
              </div>
            ) : !chips || chips.days === 0 ? (
              <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
                {chips?.note ?? '这只票暂时算不出筹码分布'}
              </div>
            ) : (
              <>
                <div className="grid grid-cols-2 overflow-hidden md:grid-cols-4">
                  <Cell
                    label="获利比例"
                    value={fmtNum(
                      chips.profit_ratio != null ? chips.profit_ratio * 100 : null,
                      1,
                      '%',
                    )}
                  />
                  <Cell label="平均成本" value={fmtNum(chips.avg_cost, 2)} />
                  <Cell
                    label="90% 成本区间"
                    value={
                      chips.cost_90_low != null && chips.cost_90_high != null
                        ? `${chips.cost_90_low.toFixed(2)} ~ ${chips.cost_90_high.toFixed(2)}`
                        : '—'
                    }
                  />
                  <Cell
                    label="集中度(90%)"
                    value={fmtNum(
                      chips.concentration_90 != null
                        ? chips.concentration_90 * 100
                        : null,
                      1,
                      '%',
                    )}
                  />
                </div>
                <div className="px-2 pt-2">
                  <div className="mb-1 text-[12px] text-fg-dim">
                    横轴 = 该价位的筹码占比 · 现价 {fmtNum(chips.price, 2)} 元
                    {chips.peak_price != null &&
                      ` · 筹码峰 ${fmtNum(chips.peak_price, 2)} 元`}
                  </div>
                  {chipOption && <EChart option={chipOption} height={260} />}
                </div>
              </>
            )}
          </Panel>
          {profile?.phase && <PhasePanel phase={profile.phase} delay={110} />}
        </div>

        <Panel
          title="杀猪盘体检"
          meta={
            <span className="num">
              10 信号 · 结构化证据优先 · 新闻仅作辅助 · 非投资建议
            </span>
          }
          delay={106}
        >
          <div className="flex flex-col gap-2 border-b border-line-soft px-4 py-3 sm:flex-row">
            <input
              value={trapContext}
              onChange={(event) => setTrapContext(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') void recheckTrap()
              }}
              placeholder="补充背景（如：朋友推荐 / 群里说 / 老师带），会按触发词上调等级"
              maxLength={80}
              disabled={trapChecking}
              className="num min-w-0 flex-1 border border-line bg-ink-850 px-3 py-1.5 text-[13px] text-fg outline-none placeholder:text-fg-dim focus:border-fg-dim disabled:cursor-not-allowed disabled:opacity-40"
            />
            <button
              type="button"
              onClick={() => void recheckTrap()}
              disabled={trapChecking}
              className="shrink-0 border border-accent/60 bg-accent/10 px-3 py-1.5 text-[13px] text-accent transition-colors hover:bg-accent/20 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {trapChecking ? '体检中…' : '重新体检'}
            </button>
          </div>
          {profile === null ? (
            <div className="flex h-[180px] items-center justify-center text-[14px] text-fg-dim">
              <span className="pulse-soft">加载中…</span>
            </div>
          ) : trapLoading ? (
            // 体检首次要现取东财新闻（实测 3.7s），不阻塞首屏 —— 见 `load` 的注释
            <PanelLoading label="体检中…（正在取个股新闻，稍等几秒）" height={180} />
          ) : !trap ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              暂时无法体检（接口未返回结果）
            </div>
          ) : (
            <>
              <div className="border-b border-line-soft px-4 py-3">
                <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <span
                    className={`text-[20px] leading-tight font-medium ${TRAP_LEVEL_CLASS[trap.level]}`}
                  >
                    {trap.level_label}
                  </span>
                  <span className="num text-[13px] text-fg-dim">
                    风险分 {trap.score}/100
                  </span>
                  {trap.trade_date && (
                    <span className="num text-[13px] text-fg-dim">{trap.trade_date}</span>
                  )}
                </div>
                <div className="mt-1 text-[13px] text-fg-dim">{trap.summary}</div>
              </div>
              <div className="divide-y divide-line-soft">
                {trap.signals.map((signal) => {
                  const status = TRAP_STATUS[signal.status]
                  return (
                    <div
                      key={signal.key}
                      className="flex gap-3 px-4 py-2 text-[13px]"
                    >
                      <span className={`w-4 shrink-0 ${status.className}`}>
                        {status.icon}
                      </span>
                      <span className="w-36 shrink-0 text-fg">{signal.name}</span>
                      <span className="min-w-0 flex-1 text-fg-dim">
                        {signal.evidence}
                      </span>
                      <span
                        className={`w-16 shrink-0 text-right ${status.className}`}
                      >
                        {status.label}
                      </span>
                    </div>
                  )
                })}
              </div>
            </>
          )}
        </Panel>

        <Panel
          title="公告 / 研报 / 解禁"
          meta={
            <span className="num">
              按需加载 · 公告缓存 6 小时、研报 / 解禁缓存 1 天 · 点击标题看原文
            </span>
          }
          delay={108}
        >
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line-soft px-4 py-2">
            <Segmented value={eventTab} items={EVENT_TABS} onChange={setEventTab} />
            <span className="num text-[12px] text-fg-dim">
              {eventsLoading
                ? '加载中…'
                : eventData?.fetched_at
                  ? `缓存于 ${eventData.fetched_at.replace('T', ' ')}`
                  : ''}
            </span>
          </div>
          {eventsError ? (
            <div className="px-4 py-8 text-center text-[13px] text-fg-dim">
              暂时取不到：{eventsError}
            </div>
          ) : !eventData ? (
            <div className="px-4 py-8 text-center text-[13px] text-fg-dim">
              {eventsLoading ? '加载中…' : '暂无数据'}
            </div>
          ) : eventData.rows.length === 0 ? (
            <div className="px-4 py-8 text-center text-[13px] text-fg-dim">
              {eventData.note ?? '没有数据'}
            </div>
          ) : (
            <>
              {eventTab === 'notices' && notices && (
                <div>
                  {notices.rows.map((row, index) => (
                    <div
                      key={`${row.published_at}-${index}`}
                      className="flex gap-3 border-b border-line-soft px-4 py-2 text-[13px] last:border-b-0"
                    >
                      <span className="num w-20 shrink-0 text-fg-dim">
                        {row.published_at}
                      </span>
                      {row.url ? (
                        <a
                          href={row.url}
                          target="_blank"
                          rel="noreferrer"
                          title={row.title}
                          className="min-w-0 flex-1 text-fg hover:text-accent hover:underline"
                        >
                          {row.title}
                        </a>
                      ) : (
                        <span className="min-w-0 flex-1 text-fg" title={row.title}>
                          {row.title}
                        </span>
                      )}
                      <span className="shrink-0 text-fg-dim">{row.category}</span>
                    </div>
                  ))}
                </div>
              )}
              {eventTab === 'reports' && reports && (
                <div>
                  {reports.rows.map((row, index) => (
                    <div
                      key={`${row.published_at}-${index}`}
                      className="flex gap-3 border-b border-line-soft px-4 py-2 text-[13px] last:border-b-0"
                    >
                      <span className="num w-20 shrink-0 text-fg-dim">
                        {row.published_at}
                      </span>
                      <div className="min-w-0 flex-1">
                        {row.url ? (
                          <a
                            href={row.url}
                            target="_blank"
                            rel="noreferrer"
                            title={row.title}
                            className="text-fg hover:text-accent hover:underline"
                          >
                            {row.title}
                          </a>
                        ) : (
                          <span className="text-fg">{row.title}</span>
                        )}
                        <div className="num mt-0.5 text-[12px] text-fg-dim">
                          {[row.institution, row.rating, row.forecast]
                            .filter(Boolean)
                            .join(' · ')}
                        </div>
                      </div>
                      <span className="shrink-0 text-fg-dim">{row.industry}</span>
                    </div>
                  ))}
                </div>
              )}
              {eventTab === 'unlock' && unlocks && (
                <div className="overflow-x-auto">
                  <table className="grid-table">
                    <thead>
                      <tr>
                        <th className="!text-left">解禁日</th>
                        <th title="解禁数量占**流通**市值的比例">占流通市值</th>
                        <th title="解禁数量占**总**市值的比例">占总市值</th>
                        <th>解禁市值</th>
                        <th>解禁股数</th>
                        <th className="!text-left">类型</th>
                      </tr>
                    </thead>
                    <tbody>
                      {unlocks.rows.map((row) => (
                        <tr key={`${row.unlock_date}-${row.share_type}`}>
                          <td className="!text-left">
                            <span className="num">{row.unlock_date}</span>
                          </td>
                          <td>
                            <span
                              className={`num ${
                                (row.ratio_float ?? 0) >= 0.05 ? 'text-danger' : ''
                              }`}
                            >
                              {row.ratio_float == null
                                ? '—'
                                : `${(row.ratio_float * 100).toFixed(1)}%`}
                            </span>
                          </td>
                          <td>
                            <span className="num">
                              {row.ratio_total == null
                                ? '—'
                                : `${(row.ratio_total * 100).toFixed(1)}%`}
                            </span>
                          </td>
                          <td>
                            <span className="num">{fmtAmount(row.market_value)}</span>
                          </td>
                          <td>
                            <span className="num">{fmtShares(row.shares)}</span>
                          </td>
                          <td className="!text-left text-fg-dim">{row.share_type}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {(eventData.stale || eventData.note) && (
                <p className="px-4 py-2 text-[12px] text-fg-dim">
                  {eventData.stale ? '⚠ 数据源暂时不可用，以上是上次缓存。' : ''}
                  {eventData.note ?? ''}
                </p>
              )}
            </>
          )}
          <p className="px-4 py-2.5 text-[12px] leading-relaxed text-fg-dim">
            「占流通市值」超过 <span className="text-fg-muted">5%</span> 且在未来 30
            天内的解禁，会同时记入上面的杀猪盘体检；研报里的盈利预测是机构观点，
            <span className="text-fg-muted">不是目标价</span>，也不构成投资建议。
          </p>
        </Panel>

        <Panel
          title="涨停记录"
          meta={<span className="num">该股上过涨停池的日期（数据源只保留最近 15 个交易日）</span>}
          delay={120}
        >
          {!profile || profile.limit_up_dates.length === 0 ? (
            <div className="px-4 py-6 text-center text-[13px] text-fg-dim">
              窗口内没有涨停记录
            </div>
          ) : (
            <div className="flex flex-wrap gap-1.5 px-4 py-3">
              {profile.limit_up_dates.map((day) => (
                <span
                  key={day}
                  className="num border border-line-soft bg-ink-850 px-2 py-1 text-[13px] text-up"
                >
                  {day}
                </span>
              ))}
            </div>
          )}
        </Panel>
      </div>
    </Layout>
  )
}

function Cell({
  label,
  value,
  tone = 'text-fg',
  sub,
  title,
  compact = false,
}: {
  label: string
  value: string
  tone?: string
  sub?: string
  /** 悬浮提示（如「所属题材」只列前 5 个时，完整名单放这里） */
  title?: string
  /** 多值格子（如「所属题材」可能挂好几个板块）用小一号字，别把整行撑高 */
  compact?: boolean
}) {
  return (
    <div
      className="relative -mr-px -mb-px border-r border-b border-line-soft px-4 py-3"
      title={title}
    >
      <div className="text-[13px] tracking-[0.1em] text-fg-dim">{label}</div>
      <div
        className={`num mt-1.5 font-medium ${compact ? 'text-[14px] leading-snug' : 'text-[18px] leading-tight'} ${tone}`}
      >
        {value}
      </div>
      {sub && <div className="num mt-1 text-[13px] text-fg-dim">{sub}</div>}
    </div>
  )
}

/**
 * 面板内的加载占位（2026-10-20）。
 *
 * 用它的两处都是**要现取外部数据**的块：资金流（DDE 未命中缓存时要现取 iFinD，可到
 * 十几秒）、杀猪盘体检（首次现取东财个股新闻，实测 3.7s）。原来只是一行「加载中…」，
 * 在纯白卡片上很容易被当成「这块没内容」；这里改成状态点 + 骨架条 + 扫光，
 * 一眼能看出还在取数。`height` 与最终内容大致同高，避免内容到达时页面跳一下。
 */
function PanelLoading({ label, height = 240 }: { label: string; height?: number }) {
  return (
    <div className="px-4 py-4" style={{ minHeight: height }}>
      <div className="mb-3 flex items-center gap-2">
        <span className="h-[6px] w-[6px] animate-pulse bg-accent" />
        <span className="text-[13px] text-fg-muted">{label}</span>
      </div>
      <div className="space-y-2.5">
        {[96, 88, 92, 74].map((width, index) => (
          <div key={index} className="skeleton h-[10px]" style={{ width: `${width}%` }} />
        ))}
      </div>
    </div>
  )
}
