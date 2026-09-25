import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import type { PatternMeta, PatternStock, PatternSummary } from '../api/types'
import Alert from '../components/Alert'
import KLineChart from '../components/KLineChart'
import type { KeyLevel } from '../components/KLineChart'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import Segmented from '../components/Segmented'
import SortTh from '../components/SortTh'
import { fmtAmount, fmtInt, fmtPct, toneOf } from '../lib/format'
import { K_VIEWS, useKLine } from '../lib/klinePeriod'
import { useSort } from '../lib/sort'
import type { SortSpecs } from '../lib/sort'
import { rememberStockList } from '../lib/stockNav'

/**
 * 形态分组的展示顺序。后端给的 catalog 就是按这个顺序排的，这里只做兜底。
 *
 * ⚠️ 分组名长度会直接影响筛选条那个标签列：`w-24` 是按最长的一组
 * 「单 K 蜡烛形态」（5 个汉字 + 1 个字母）留的。再加更长的分组名时要一起调，
 * 否则标签会折行、把每个分组的高度撑成两行。
 */
const GROUP_ORDER = ['致富', '趋势', '突破', '量价', '几何', '单 K 蜡烛形态']

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
  // 爆量后缩量回踩（shrink 与既有字段共用：缩量日成交额 / 爆量日成交额）
  burst_mult: ['爆量倍数', false],
  burst_gap: ['爆量后间隔', false],
  // 悟道之路 · 致富（样板/启动）
  high_pct: ['冲高%', false],
  vol_ratio_20: ['量比(20日)', false],
  leave_high: ['离开最高', true],
  sample_high: ['样板高', false],

  // ---- 2026-09-25 新增形态（键与 backend/app/services/patterns.py 的 detail 一一对应）----
  // 键是全局共用的，所以新增时要**先看有没有语义相同的旧键**（能共用就共用，
  // 不能共用就换个不会误导的名字）—— 像 `recover` 已经被杯柄占成「右杯沿恢复」，
  // 圆弧底就只能另起 `recover_pct`，否则同一个是数字却挂着别人的标签。
  // 趋势
  squeeze_width: ['粘合宽度', true],
  ma20_rise: ['MA20 涨幅', true],
  ma10_rise: ['MA10 涨幅', true],
  days: ['持续天数', false],
  daily_slope: ['轨斜率/日', true],
  touches: ['触轨次数', false],
  cross_days_ago: ['金叉距今', false],
  weeks: ['周线根数', false],
  ma5_gap: ['距周 MA5', true],
  // 突破
  box_range: ['箱体振幅', true],
  touches_top: ['触顶次数', false],
  touches_bottom: ['触底次数', false],
  fill: ['回补深度', true],
  rise: ['缺口后涨幅', true],
  days_ago: ['缺口天数', false],
  // 量价
  vol_shrink: ['缩量到', true],
  drawdown: ['距高点回撤', true],
  new_low: ['距新低', true],
  upper_shadow: ['上影占比', true],
  pile_days: ['堆量天数', false],
  vol_ratio_5_20: ['5/20 日量比', false],
  gain: ['区间涨幅', true],
  // 几何
  recover_pct: ['距底收复', true],
  down_days: ['下跌段天数', false],
  up_days: ['上涨段天数', false],
  vol_boost: ['右侧量能倍数', false],
  amplitude: ['窗口振幅', true],
  low_position: ['低点位置', true],
  low_spread: ['三底差距', true],
  expand: ['扩张比例', true],
  contract: ['收敛比例', true],
  upper_slope: ['上轨斜率', true],
  lower_slope: ['下轨斜率', true],
  gap_ratio: ['末端间距', true],
  // 单 K 蜡烛形态
  shadow_ratio: ['影线/实体', false],
  body_ratio: ['实体比', false],
  close_pos: ['收盘位置', true],
  prior_drop: ['前置跌幅', true],
  star_recover: ['收复实体', true],
  steady: ['实体不缩水', false],
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
      const [list, sum] = await Promise.all([
        api.patternHits(target, 0, HIT_LIMIT),
        api.patternSummary(target),
      ])
      setHits(list)
      setSummary(sum)
    } catch (err) {
      setHits([])
      setSummary(null)
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

  // 筛完之后当前看图的票可能已经不在列表里，自动切到第一条
  useEffect(() => {
    // 取数期间 visible 必然是空的，此时判定「这只票不在列表里」会把
    // URL 上带的 code 冲掉 —— 从个股页退回本页时正好撞上这一下
    if (loading) return
    if (visible.length === 0) {
      setActive(null)
      return
    }
    if (!active || !visible.some((stock) => stock.code === active)) {
      setActive(visible[0].code)
    }
  }, [visible, active, loading])

  const current = useMemo(
    () => visible.find((stock) => stock.code === active) ?? null,
    [visible, active],
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

  const keyLevels = useMemo<KeyLevel[]>(() => {
    const levels: KeyLevel[] = []
    const seen = new Set<string>()
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
  }, [current])

  /** 单选：点已选中的那个就取消，回到「全部」 */
  const toggle = (key: string) => {
    setPicked((prev) => (prev === key ? null : key))
  }

  const toolbar = (
    <>
      <span className="num hidden text-[13px] text-fg-dim lg:inline">
        {loading ? '加载中…' : `${visible.length} / ${hits.length} 只`}
      </span>
      <select
        value={date ?? ''}
        onChange={(event) => setDate(event.target.value || null)}
        className="num border border-line bg-ink-900 px-2 py-[3px] text-[13px] text-fg outline-none focus:border-fg-dim"
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
                点形态名筛选（单选，再点一次取消）；徽标是该形态的全市场命中家数。致富＝悟道样板/启动（只扫强势小池创业板）
              </span>
            </span>
          }
          delay={40}
        >
          <div className="space-y-2 px-4 py-3">
            {groups.map(([group, items]) => (
              <div key={group} className="flex flex-wrap items-center gap-2">
                <span className="w-24 shrink-0 text-[13px] text-fg-dim">{group}</span>
                {items.map((item) => {
                  const on = picked === item.key
                  const count = countOf[item.key] ?? 0
                  return (
                    <button
                      key={item.key}
                      type="button"
                      onClick={() => toggle(item.key)}
                      className={`num border px-2 py-[2px] text-[13px] transition-colors ${
                        on
                          ? 'border-accent bg-accent/10 text-accent'
                          : 'border-line text-fg-muted hover:border-fg-dim hover:text-fg'
                      }`}
                    >
                      {item.name}
                      <span className="ml-1.5 text-[13px] text-fg-dim">{count}</span>
                    </button>
                  )
                })}
              </div>
            ))}
            <div className="flex flex-wrap items-center gap-3 border-t border-line-soft pt-2.5">
              <span className="text-[13px] text-fg-dim">评分下限</span>
              <input
                type="range"
                min={0}
                max={100}
                step={5}
                value={minScore}
                onChange={(event) => setMinScore(Number(event.target.value))}
                className="h-[3px] w-40 accent-amber-500"
              />
              <span className="num w-8 text-[13px] text-fg">{minScore}</span>
              {(picked !== null || minScore > 0) && (
                <button
                  type="button"
                  onClick={() => {
                    setPicked(null)
                    setMinScore(0)
                  }}
                  className="num border border-line px-2 py-[2px] text-[13px] text-fg-dim hover:border-fg-dim hover:text-fg"
                >
                  清除筛选
                </button>
              )}
            </div>
          </div>
        </Panel>

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
                  className="ml-3 border border-line px-2 py-[2px] text-[13px] text-fg-dim transition-colors hover:border-fg-dim hover:text-fg"
                >
                  {copied ? '已复制' : `复制 ${visible.length} 个代码`}
                </button>
              )}
            </span>
          }
          delay={80}
        >
          {truncated && (
            <div className="flex items-start gap-2.5 border-b border-accent/25 bg-accent/[0.05] px-4 py-2.5 text-[13px] leading-relaxed text-fg-muted">
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
            <div className="px-4 py-6 text-center text-[13px] text-fg-dim">
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
                              className="num border border-line-soft px-1.5 py-[1px] text-[13px] text-fg-muted"
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

        <Panel
          title="看图确认"
          meta={
            <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="num">
                {current
                  ? `${current.code} ${current.name ?? ''} · 前复权 · ${kline.bars.length} 根`
                  : '点上面任意一行'}
                <span className="ml-3 text-fg-dim">
                  画的是前复权序列，与引擎判定用的完全一致；虚线是形态关键位
                </span>
              </span>
              <Segmented value={kline.view} items={K_VIEWS} onChange={kline.setView} />
            </span>
          }
          delay={120}
        >
          {current && (
            <div className="border-b border-line-soft px-4 py-2.5">
              {current.patterns.map((item) => (
                <div key={item.pattern} className="flex flex-wrap items-baseline gap-x-3 py-0.5">
                  <span className="w-24 shrink-0 text-[13px] text-fg">
                    {item.pattern_name}
                  </span>
                  <span className="num w-10 shrink-0 text-[13px] text-accent">
                    {item.score.toFixed(1)}
                  </span>
                  <span className="num flex flex-wrap gap-x-3 text-[13px] text-fg-dim">
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
              <div className="flex h-[380px] items-center justify-center text-[13px] text-fg-dim">
                {kline.syncing ? '正在补 2 年历史（周/月 K 要的长周期）…' : '加载日线…'}
              </div>
            ) : kline.error ? (
              <div className="flex h-[380px] items-center justify-center px-6 text-center text-[13px] text-danger">
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
