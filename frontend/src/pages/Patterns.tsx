import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import type { PatternMeta, PatternStock, PatternSummary, TemplateBoard } from '../api/types'
import Alert from '../components/Alert'
import KLineChart from '../components/KLineChart'
import type { KeyLevel } from '../components/KLineChart'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import Segmented from '../components/Segmented'
import SortTh from '../components/SortTh'
import TemplatePanel from '../components/TemplatePanel'
import { fmtAmount, fmtInt, fmtPct, toneOf } from '../lib/format'
import { K_VIEWS, useKLine } from '../lib/klinePeriod'
import { useSort } from '../lib/sort'
import type { SortSpecs } from '../lib/sort'
import { rememberStockList } from '../lib/stockNav'

/** 形态分组的展示顺序。后端给的 catalog 就是按这个顺序排的，这里只做兜底。 */
const GROUP_ORDER = ['趋势', '突破', '量价', '几何']

/**
 * 「样板池」这一组在筛选条里用的 key。
 *
 * 它**不是形态**（后端 `pattern_hit` 里没有这个 key），而是同一张筛选条上的
 * 另一路数据：选中它时下面的列表换成样板池表。名字带 `_pool` 就是为了跟形态 key
 * 区分开 —— 叫 `template` 的话，将来某个形态真叫这个名字就会撞车。
 */
const TEMPLATE_KEY = 'template_pool'

/**
 * 命中列表一次取多少只。
 *
 * 500 是「够用」与「响应体积」的折中：后端按评分降序给，真正要看的也就是
 * 最前面那一批。**取不满时必须在页面上说出来** —— 否则「返回 500 行」会被
 * 读成「今天一共只有 500 只命中」，那是两个完全不同的结论。
 */
const HIT_LIMIT = 500

/**
 * 命中列表各列的排序口径。
 *
 * ⚠️ 排序**只在这 500 条之内**（后端按评分给的前 500），不是全市场排名。
 * 按成交额排序得到的是「评分最高的 500 只里成交额最大的」，这一点靠列表上方
 * 那条截断提示来说明 —— 不写清楚的话，「成交额排名第一」会被读成全市场第一。
 */
const HIT_SORTS: SortSpecs<PatternStock> = {
  score: { value: (stock) => stock.score },
  code: { value: (stock) => stock.code, first: 'asc' },
  close: { value: (stock) => stock.close },
  pct_chg: { value: (stock) => stock.pct_chg },
  amount: { value: (stock) => stock.amount },
  avg_amount: { value: (stock) => stock.avg_amount },
  total_mv: { value: (stock) => stock.total_mv },
}

/**
 * 「正在看图的那只票」存在 sessionStorage 里，**不进 URL**。
 *
 * 为什么不放 URL：点代码链接时既要记住这只票、又要让 Link 完成跳转。两者都
 * 经过 router 的话（`setSearchParams` + Link 自己的 navigate），同一个点击里会
 * 连发两次导航 —— 实测表现为「点一下只选中、要点两下才跳转」。
 * sessionStorage 是同步写入、完全不碰 router，不会干扰 Link 那一次导航。
 */
const ACTIVE_KEY = 'patterns:active-code'

function readActive(): string | null {
  try {
    return sessionStorage.getItem(ACTIVE_KEY)
  } catch {
    // 隐私模式下 sessionStorage 可能直接抛错。退化成「不记住」而不是白屏
    return null
  }
}

/**
 * 明细字段的展示名与单位：`[中文名, 是不是比率]`。
 *
 * 名字和单位必须写在同一张表里。拆成「名字表 + 百分比表」两张，是
 * `pct_chg` 被当成比率又乘 100（+10.02% 显示成 1002.00%）的直接原因 ——
 * 两张表各自演进，早晚对不上。
 *
 * 另外提醒：**字段名在全站必须唯一**。`gap` 一度在均线多头排列里表示
 * 「距 MA20 的比率」、在 W 底里表示「两个底隔了几个交易日」，同名不同义
 * 会让这里的单位判断无从下手（27 天被当比率显示成 2700%）。后端已把后者
 * 改名成 `bottom_gap`。
 */
const DETAIL_FIELDS: Record<string, [string, boolean]> = {
  // 趋势
  stack_days: ['排列天数', false],
  spread: ['MA5-MA60 乖离', true],
  gap: ['距 MA20', true],
  dip_days: ['回踩天数', false],
  dip_low_vs_ma20: ['低点距 MA20', true],
  close_break: ['收盘最深破位', true],
  vol_drag: ['回踩缩量比', true],
  // 突破
  window: ['新高窗口', false],
  excess: ['突破幅度', true],
  vol_ratio: ['量比', false],
  platform_width: ['平台振幅', true],
  // 量价
  pct_chg: ['当日涨幅', false], // 后端存的就是百分数（10.02 = +10.02%）
  pullback_days: ['回调天数', false],
  drop: ['回调幅度', true],
  // 几何
  cup_depth: ['杯深', true],
  recover: ['右杯沿恢复', true],
  handle_days: ['柄部天数', false],
  handle_ratio: ['柄部回撤/杯深', true],
  handle_vol_ratio: ['柄部缩量比', true],
  bottom_gap: ['两底间隔', false],
  tolerance: ['两底差距', true],
  rebound: ['中间反弹', true],
  converge: ['收敛比', true],
  position: ['在区间位置', true],
  pivots: ['摆动点数', false],
  shoulder_diff: ['两肩差距', true],
  neck_diff: ['颈线倾斜', true],
  head_depth: ['头深', true],
  tail: ['右肩后天数', false],
  pole_gain: ['旗杆涨幅', true],
  flag_days: ['旗面天数', false],
  flag_range: ['旗面振幅', true],
  retrace: ['回撤/旗杆', true],
  vol_drop: ['旗面缩量比', true],
  // 三段式突破
  slow_gain: ['缓涨涨幅', true],
  slow_bull: ['缓涨阳线占比', true],
  surge_gain: ['急涨涨幅', true],
  surge_days: ['急涨天数', false],
  // 量比是「倍数」不是「占比」，所以不带百分号（与旗形的 vol_ratio 一致）
  surge_vol: ['急涨量比', false],
  flat_days: ['整理天数', false],
  flat_range: ['整理振幅', true],
  // shrink = 整理段均量 / 急涨段均量，0.68 表示「缩到 68%」，按占比显示更直观
  shrink: ['缩量到', true],
  break_vol: ['突破量比', false],
  // 涨停爆量横盘（surge_gain / flat_days / flat_range / shrink / excess
  // 与三段式共用同一套字段名，语义也对得上）
  limit_vol: ['涨停量比', false],
  keep: ['守住成果', true],
  // 突破后横盘（vol_ratio / flat_days / flat_range / shrink / position 与既有字段共用）
  break_gain: ['突破涨幅', true],
  giveback: ['横盘回撤', true],
  // N 字选股（surge_gain / vol_ratio / pullback_days / shrink 与既有字段共用）
  start_gap: ['距起涨点', true],
  // 欧奈尔突破（vol_ratio / flat_days / flat_range / excess 与既有字段共用）
  from_high: ['距一年新高', true],
}

function detailText(key: string, value: number | string): string {
  if (typeof value !== 'number') return String(value)
  const [, isRatio] = DETAIL_FIELDS[key] ?? ['', false]
  if (isRatio) return `${(value * 100).toFixed(2)}%`
  // pct_chg 是百分数口径（后端存 10.02 = +10.02%），只补 % 号、不乘 100；
  // 其余 isRatio=false 的字段（天数、根数、量比）是纯计数，不带 %
  if (key === 'pct_chg') return `${value.toFixed(2)}%`
  // 计数类给整数（天数、根数），其余保留两位
  return Number.isInteger(value) ? String(value) : value.toFixed(2)
}

export default function Patterns() {
  /**
   * 视图状态挂在 URL 上：点进个股页再后退时这一页会**重新挂载**，
   * 不写进 URL 的话日期、形态筛选、正在看图的那只票会全部归零。
   *
   * 评分下限故意不进去：滑块拖动会每一帧改一次地址，属于噪声。
   */
  const [params, setParams] = useSearchParams()
  const [date, setDate] = useState<string | null>(() => params.get('date'))
  const [dates, setDates] = useState<string[]>([])
  const [catalog, setCatalog] = useState<PatternMeta[]>([])
  const [hits, setHits] = useState<PatternStock[]>([])
  const [summary, setSummary] = useState<PatternSummary | null>(null)
  // 样板池（量价结构选股的「明天盯」清单）。与形态命中**分开取**：
  // 它们的日期语义不同 —— 形态是「今天像什么」，样板池是「昨天那条线过了没」
  const [template, setTemplate] = useState<TemplateBoard | null>(null)
  // 形态筛选是**单选**：一次只看一个形态，不然十几个形态叠在一起没人能读
  const [picked, setPicked] = useState<string | null>(() => params.get('patterns') || null)
  const [minScore, setMinScore] = useState(0)
  const [active, setActive] = useState<string | null>(() => readActive())
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // 图上的 K 线：周期、取数、按需补长历史都在这个 hook 里（与个股页共用一套）。
  // adjust=1 拿前复权序列 —— 引擎就是在这条序列上判定的，用不复权图画线会在
  // 除权股上把关键位画错高度
  const kline = useKLine(active ?? '', { adjust: true })
  // 「复制代码」的瞬时反馈：成功后按钮文案换成「已复制」，1.6 秒复原
  const [copied, setCopied] = useState(false)

  // 写回 URL。replace 而不是 push：点一行就塞一条历史的话，退出这页要按很多次返回
  useEffect(() => {
    const next = new URLSearchParams(params)
    if (date) next.set('date', date)
    else next.delete('date')
    // 「看图的那只票」刻意不写进 URL，原因见 ACTIVE_KEY 上面的说明
    if (picked) next.set('patterns', picked)
    else next.delete('patterns')
    if (next.toString() !== params.toString()) setParams(next, { replace: true })
  }, [date, picked, params, setParams])

  useEffect(() => {
    api.dates().then(setDates).catch(() => setDates([]))
    api.patternCatalog().then(setCatalog).catch(() => setCatalog([]))
  }, [])

  const load = useCallback(async (target: string | null) => {
    setLoading(true)
    setError(null)
    try {
      // 一次取全、前端筛 —— 命中量每天几百条，拖滑块不该打接口。
      // 也让「标签上的家数」和「筛出来的行数」天然一致（都用全量口径）
      const [list, sum, tpl] = await Promise.all([
        api.patternHits(target, 0, HIT_LIMIT),
        api.patternSummary(target),
        // 样板池挂了不该把形态列表一起弄空（Promise.all 一个 reject 全 reject）——
        // 它是后加的一路数据源，不能反过来把这一页原有的功能拖下水
        api.templatePool(target).catch(() => null),
      ])
      setHits(list)
      setSummary(sum)
      setTemplate(tpl)
    } catch (err) {
      setHits([])
      setSummary(null)
      setTemplate(null)
      setError((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(date)
  }, [date, load])

  const groups = useMemo(() => {
    const map = new Map<string, PatternMeta[]>()
    for (const item of catalog) {
      const list = map.get(item.group) ?? []
      list.push(item)
      map.set(item.group, list)
    }
    return [...map.entries()].sort(
      (a, b) => GROUP_ORDER.indexOf(a[0]) - GROUP_ORDER.indexOf(b[0]),
    )
  }, [catalog])

  const countOf = useMemo(() => {
    const map: Record<string, number> = {}
    for (const item of summary?.by_pattern ?? []) map[item.pattern] = item.stocks
    return map
  }, [summary])

  const visible = useMemo(() => {
    return hits.filter((stock) => {
      if (stock.score < minScore) return false
      if (!picked) return true
      // 按**全部**命中形态筛，而不是展示用的一份截断列表 ——
      // 否则会出现「标签写着 10 家、点进去只剩 1 行」
      return stock.patterns.some((item) => item.pattern === picked)
    })
  }, [hits, picked, minScore])

  // 首屏按评分降序（与后端给的一致，所以看到的顺序没变，但列头能正确亮出
  // 当前排的是哪一列）；点列头改成别的口径。**排的是 visible，不是 hits** ——
  // 排序与筛选是两个独立动作，先筛后排才符合直觉
  const [hitSort, shown] = useSort(visible, HIT_SORTS, { key: 'score' })

  /**
   * 把当前筛选结果的代码复制下来（每行一个），方便导进同花顺的自选股。
   *
   * 没用 `navigator.clipboard`：它只在**安全上下文**（HTTPS / localhost）可用，
   * 而站点在云端是 `http://IP:8080` —— 那里调用会直接抛错。改用
   * textarea + `execCommand` 这套老办法，两种环境下都能用。
   */
  const copyCodes = useCallback(() => {
    const box = document.createElement('textarea')
    box.value = visible.map((stock) => stock.code).join('\n')
    // 放到视口外，避免复制瞬间页面跳动
    box.style.position = 'fixed'
    box.style.top = '-1000px'
    document.body.appendChild(box)
    box.select()
    let ok = false
    try {
      ok = document.execCommand('copy')
    } catch {
      ok = false
    }
    document.body.removeChild(box)
    if (!ok) {
      setError('复制失败：浏览器不允许，手动选中列表里的代码吧')
      return
    }
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1600)
  }, [visible])

  // 命中总数取自 summary 的全量口径，与列表长度一比就知道有没有被截断
  const truncated = (summary?.total_stocks ?? 0) > hits.length

  const templateItems = useMemo(() => template?.items ?? [], [template])

  // 筛完之后当前看图的票可能已经不在列表里，自动切到第一条。
  // 「在列表里」两个列表都算 —— 样板池的行也能点开看图
  useEffect(() => {
    // 取数期间 visible 必然是空的，此时判定「这只票不在列表里」会把
    // URL 上带的 code 冲掉 —— 从个股页退回本页时正好撞上这一下
    if (loading) return
    const known = (code: string) =>
      visible.some((stock) => stock.code === code) ||
      templateItems.some((row) => row.code === code)
    if (active && known(active)) return
    setActive(visible[0]?.code ?? templateItems[0]?.code ?? null)
  }, [visible, templateItems, active, loading])

  const current = useMemo(
    () => visible.find((stock) => stock.code === active) ?? null,
    [visible, active],
  )

  /** 当前看图的那只在样板池里的行 —— 触发价与兜底线要画在图上 */
  const currentTemplate = useMemo(
    () => templateItems.find((row) => row.code === active) ?? null,
    [templateItems, active],
  )

  /**
   * 记下「正在看图的那只票」（点列表面板任意一行时调用）。
   *
   * 为什么存 sessionStorage 而不写进 URL：点代码链接时既要记住这只票、
   * 又要让 Link 完成跳转 —— 两条路径都走 router 会互相打架
   * （实测表现为「点一下只选中、要点两下才跳转」）。
   */
  const selectStock = useCallback(
    (code: string) => {
      setActive(code)
      try {
        sessionStorage.setItem(ACTIVE_KEY, code)
      } catch {
        // 存不进去不影响这一页的使用，只是从个股页退回来时会回到第一只
      }
    },
    [],
  )

  /** 点样板池那一行。与命中列表同理，顺手把这份清单存下供个股页 ← → 前后翻 */
  const selectTemplateStock = useCallback(
    (code: string) => {
      selectStock(code)
      rememberStockList(templateItems.map((row) => row.code))
    },
    [selectStock, templateItems],
  )

  const keyLevels = useMemo<KeyLevel[]>(() => {
    const levels: KeyLevel[] = []
    const seen = new Set<string>()
    // 样板池的触发价 / 兜底线**优先画**：点开样板池那一行，看的就是
    // 「过没过昨天那条线」，这两条线比形态的突破位更贴当下
    if (currentTemplate?.trigger != null) {
      const key = `b${currentTemplate.trigger.toFixed(2)}`
      seen.add(key)
      levels.push({
        label: '触发',
        value: currentTemplate.trigger,
        kind: 'breakout',
      })
    }
    if (currentTemplate?.floor != null) {
      const key = `s${currentTemplate.floor.toFixed(2)}`
      seen.add(key)
      levels.push({
        label: '兜底',
        value: currentTemplate.floor,
        kind: 'support',
      })
    }
    for (const item of current?.patterns ?? []) {
      const breakout = item.key_levels.breakout
      if (breakout != null) {
        const key = `b${breakout.toFixed(2)}`
        if (!seen.has(key)) {
          seen.add(key)
          levels.push({ label: '突破', value: breakout, kind: 'breakout' })
        }
      }
      const support = item.key_levels.support
      if (support != null) {
        const key = `s${support.toFixed(2)}`
        if (!seen.has(key)) {
          seen.add(key)
          levels.push({ label: '支撑', value: support, kind: 'support' })
        }
      }
    }
    return levels
  }, [current, currentTemplate])

  /** 单选：点已选中的那个就取消，回到「全部」 */
  const toggle = (key: string) => {
    setPicked((prev) => (prev === key ? null : key))
  }

  const toolbar = (
    <>
      <span className="num hidden text-[12px] text-fg-dim lg:inline">
        {loading ? '加载中…' : `${visible.length} / ${hits.length} 只`}
      </span>
      <select
        value={date ?? ''}
        onChange={(event) => setDate(event.target.value || null)}
        className="num border border-line bg-ink-900 px-2 py-[3px] text-[12px] text-fg outline-none focus:border-fg-dim"
      >
        <option value="">最新</option>
        {/* 倒序渲染：最近的排最上面。原先是升序，展开后要一路滚到底才够得着昨天 */}
        {[...dates].reverse().map((item) => (
          <option key={item} value={item}>
            {item}
          </option>
        ))}
      </select>
    </>
  )

  return (
    <Layout toolbar={toolbar}>
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}

      <div className="space-y-4">
        <Panel
          title="形态筛选"
          meta={
            <span className="num">
              {summary?.trade_date ? `${summary.trade_date} · ` : ''}
              共 {fmtInt(summary?.total_stocks ?? 0)} 只命中
              <span className="ml-3 text-fg-dim">
                点名称筛选（单选，再点一次取消）；徽标是当天的命中家数 ·
                「样板日」不在这套形态里，它是量价结构的两日节奏，点它看次日盯盘清单
              </span>
            </span>
          }
          delay={80}
        >
          <div className="space-y-2 px-4 py-3">
            {/* 样板池这一组**排在形态之前**：它是唯一一张「今天定、次日盘中执行」、
                有效期只有一天的清单，其余形态晚看一天也还能看 */}
            <div className="flex flex-wrap items-center gap-2">
              <span className="w-8 shrink-0 text-[12px] text-fg-dim">样板</span>
              <button
                type="button"
                onClick={() => toggle(TEMPLATE_KEY)}
                title="量价结构：有量冲高又收回来（两日节奏的第一天）。点它看次日的盯盘清单"
                className={`num border px-2 py-[2px] text-[12px] transition-colors ${
                  picked === TEMPLATE_KEY
                    ? 'border-accent bg-accent/10 text-accent'
                    : 'border-line text-fg-muted hover:border-fg-dim hover:text-fg'
                }`}
              >
                样板日
                <span className="ml-1.5 text-[12px] text-fg-dim">{templateItems.length}</span>
              </button>
            </div>

            {groups.map(([group, items]) => (
              <div key={group} className="flex flex-wrap items-center gap-2">
                <span className="w-8 shrink-0 text-[12px] text-fg-dim">{group}</span>
                {items.map((item) => {
                  const on = picked === item.key
                  const count = countOf[item.key] ?? 0
                  return (
                    <button
                      key={item.key}
                      type="button"
                      onClick={() => toggle(item.key)}
                      className={`num border px-2 py-[2px] text-[12px] transition-colors ${
                        on
                          ? 'border-accent bg-accent/10 text-accent'
                          : 'border-line text-fg-muted hover:border-fg-dim hover:text-fg'
                      }`}
                    >
                      {item.name}
                      <span className="ml-1.5 text-[12px] text-fg-dim">{count}</span>
                    </button>
                  )
                })}
              </div>
            ))}
            <div className="flex flex-wrap items-center gap-3 border-t border-line-soft pt-2.5">
              {picked === TEMPLATE_KEY ? (
                // 样板池不是评分筛出来的，滑块留在那儿会让人以为「拖到 80 能过滤样板」
                <span className="text-[12px] text-fg-dim">
                  样板池不走评分：它按冲高 / 量比 / 收盘位置三个门槛筛，见下方表格上方的说明
                </span>
              ) : (
                <>
                  <span className="text-[12px] text-fg-dim">评分下限</span>
                  <input
                    type="range"
                    min={0}
                    max={100}
                    step={5}
                    value={minScore}
                    onChange={(event) => setMinScore(Number(event.target.value))}
                    className="h-[3px] w-40 accent-amber-500"
                  />
                  <span className="num w-8 text-[12px] text-fg">{minScore}</span>
                </>
              )}
              {(picked !== null || minScore > 0) && (
                <button
                  type="button"
                  onClick={() => {
                    setPicked(null)
                    setMinScore(0)
                  }}
                  className="num border border-line px-2 py-[2px] text-[12px] text-fg-dim hover:border-fg-dim hover:text-fg"
                >
                  清除筛选
                </button>
              )}
            </div>
          </div>
        </Panel>

        {/* 筛选条选「样板日」时，下面这一格换成样板池表 —— 两张表的列完全不同，
            合成一张会得到一半空列，所以整块切换而不是按行合并 */}
        {picked === TEMPLATE_KEY ? (
          <TemplatePanel
            board={template}
            loading={loading}
            active={active}
            onSelect={selectTemplateStock}
            delay={120}
          />
        ) : (
          <Panel
          title="命中列表"
          meta={
            <span className="num">
              点列头排序 · 点一行看它的 K 线与关键位
              {visible.length > 0 && (
                <button
                  type="button"
                  onClick={copyCodes}
                  title="每行一个代码，可直接粘进同花顺的「导入自选股」"
                  className="ml-3 border border-line px-2 py-[2px] text-[12px] text-fg-dim transition-colors hover:border-fg-dim hover:text-fg"
                >
                  {copied ? '已复制' : `复制 ${visible.length} 个代码`}
                </button>
              )}
            </span>
          }
          delay={120}
        >
          {truncated && (
            <div className="flex items-start gap-2.5 border-b border-accent/25 bg-accent/[0.05] px-4 py-2.5 text-[12px] leading-relaxed text-fg-muted">
              <span className="mt-[3px] h-[6px] w-[6px] shrink-0 bg-accent" />
              <span>
                <span className="font-medium text-accent">列表被截断：</span>
                当日共命中
                <span className="num text-accent"> {summary?.total_stocks} </span>
                只，这里只列出评分最高的
                <span className="num text-fg"> {HIT_LIMIT} </span>
                只，点列头排序也只在这
                <span className="num text-fg"> {HIT_LIMIT} </span>
                只里排 —— 不是全市场排名。用上面的形态筛选或评分下限把范围缩小。
              </span>
            </div>
          )}

          {visible.length === 0 ? (
            <div className="px-4 py-6 text-center text-[12px] text-fg-dim">
              {loading ? '加载中…' : '当前筛选下没有命中的个股'}
            </div>
          ) : (
            <div className="max-h-[520px] overflow-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <SortTh sortKey="score" {...hitSort}>
                      评分
                    </SortTh>
                    <SortTh sortKey="code" align="left" {...hitSort}>
                      代码 / 名称
                    </SortTh>
                    {/* 「命中形态」是一串标签，没有单一可比值，不排序 */}
                    <th className="!text-left">命中形态</th>
                    <SortTh sortKey="close" {...hitSort}>
                      最新价
                    </SortTh>
                    <SortTh sortKey="pct_chg" {...hitSort}>
                      涨跌幅
                    </SortTh>
                    <SortTh sortKey="amount" {...hitSort}>
                      成交额
                    </SortTh>
                    <SortTh
                      sortKey="avg_amount"
                      {...hitSort}
                      title="近 20 日日均成交额：今天的量相对平时放大了多少"
                    >
                      日均额
                    </SortTh>
                    <SortTh sortKey="total_mv" {...hitSort} title="总市值：是题材小票还是权重">
                      市值
                    </SortTh>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((stock) => (
                    <tr
                      key={stock.code}
                      onClick={() => {
                        selectStock(stock.code)
                        // 顺手把这份命中列表也存下，个股页就能用 ← → 前后翻。
                        // 存的是 shown（已排序）—— 翻页顺序跟着屏幕上的顺序走才不别扭
                        rememberStockList(shown.map((item) => item.code))
                      }}
                      className={`cursor-pointer ${
                        stock.code === active ? 'bg-accent/10' : ''
                      }`}
                    >
                      <td>
                        <span
                          className={`num ${
                            stock.score >= 95 ? 'text-accent' : 'text-fg-muted'
                          }`}
                        >
                          {stock.score.toFixed(1)}
                        </span>
                      </td>
                      <td className="!text-left">
                        {/* 这一层不加 onClick：让 Link 自己完成导航，记住这只票
                            交给事件冒泡到上面那行的 onClick（见 selectStock）。
                            在这里再碰一次 router 会和 Link 的导航打架 */}
                        <Link
                          to={`/stock/${stock.code}`}
                          className="hover:text-accent"
                        >
                          <span className="num text-fg-dim">{stock.code}</span>
                          <span className="ml-2 text-fg">{stock.name ?? '—'}</span>
                        </Link>
                      </td>
                      <td className="!text-left">
                        <div className="flex flex-wrap gap-1">
                          {stock.patterns.map((item) => (
                            <span
                              key={item.pattern}
                              className="num border border-line-soft px-1.5 py-[1px] text-[12px] text-fg-muted"
                            >
                              {item.pattern_name}
                              <span className="ml-1 text-fg-dim">
                                {item.score.toFixed(0)}
                              </span>
                            </span>
                          ))}
                        </div>
                      </td>
                      <td>
                        <span className="num">{stock.close?.toFixed(2) ?? '—'}</span>
                      </td>
                      <td>
                        <span className={`num ${toneOf(stock.pct_chg)}`}>
                          {fmtPct(stock.pct_chg)}
                        </span>
                      </td>
                      <td>
                        <span className="num text-fg-muted">{fmtAmount(stock.amount)}</span>
                      </td>
                      <td>
                        <span className="num text-fg-dim">{fmtAmount(stock.avg_amount)}</span>
                      </td>
                      <td>
                        <span className="num text-fg-dim">{fmtAmount(stock.total_mv)}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          </Panel>
        )}

        <Panel
          title="看图确认"
          meta={
            <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="num">
                {active
                  ? `${active} ${current?.name ?? currentTemplate?.name ?? ''} · 前复权 · ${kline.bars.length} 根`
                  : '点上面任意一行（形态命中或样板池）'}
                <span className="ml-3 text-fg-dim">
                  画的是前复权序列，与引擎判定用的完全一致；虚线是关键位（样板池的触发价 /
                  兜底线优先）
                </span>
              </span>
              <Segmented value={kline.view} items={K_VIEWS} onChange={kline.setView} />
            </span>
          }
          delay={160}
        >
          {current && (
            <div className="border-b border-line-soft px-4 py-2.5">
              {current.patterns.map((item) => (
                <div key={item.pattern} className="flex flex-wrap items-baseline gap-x-3 py-0.5">
                  <span className="w-24 shrink-0 text-[12px] text-fg">
                    {item.pattern_name}
                  </span>
                  <span className="num w-10 shrink-0 text-[12px] text-accent">
                    {item.score.toFixed(1)}
                  </span>
                  <span className="num flex flex-wrap gap-x-3 text-[12px] text-fg-dim">
                    {Object.entries(item.detail).map(([key, value]) => (
                      <span key={key}>
                        {DETAIL_FIELDS[key]?.[0] ?? key}
                        <span className="ml-1 text-fg-muted">{detailText(key, value)}</span>
                      </span>
                    ))}
                  </span>
                </div>
              ))}
            </div>
          )}
          <div className="px-2 pt-2">
            {kline.loading || kline.syncing ? (
              <div className="flex h-[380px] items-center justify-center text-[12px] text-fg-dim">
                {kline.syncing ? '正在补 2 年历史（周/月 K 要的长周期）…' : '加载日线…'}
              </div>
            ) : kline.error ? (
              <div className="flex h-[380px] items-center justify-center px-6 text-center text-[12px] text-danger">
                日线取数失败：{kline.error}
              </div>
            ) : (
              <KLineChart bars={kline.bars} height={380} keyLevels={keyLevels} />
            )}
          </div>
        </Panel>
      </div>
    </Layout>
  )
}
