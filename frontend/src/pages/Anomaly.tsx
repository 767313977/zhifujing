import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { AnomalyMap, AnomalyRow, IndustryStock, IndustryStocks } from '../api/types'
import Alert from '../components/Alert'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import SortTh from '../components/SortTh'
import { fmtAmount, fmtPct, toneOf } from '../lib/format'
import { useSort } from '../lib/sort'
import type { SortSpecs } from '../lib/sort'

/**
 * 表格最多渲染多少行。这一页最多约 2000 行、11 列，全量进 DOM 切筛选都卡；
 * 排序 / 筛选仍在全量数据上做，只截断渲染（做法同 Patterns / Wudao 页）。
 */
const RENDER_LIMIT = 200

/** 搜索防抖时长（ms）：每次敲字都 filter+sort 两千行没必要，也省得输入卡顿 */
const SEARCH_DEBOUNCE_MS = 200

/**
 * 小防抖：值停下来之后才把最新值交出去。
 * 用 useEffect 定时器而不是 useDeferredValue —— 这页的诉求是明确的「敲字后等一小下」，
 * 时间可控，也不依赖渲染调度的心情。
 */
function useDebounced<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay)
    return () => window.clearTimeout(timer)
  }, [value, delay])
  return debounced
}

/**
 * 连板列的排序值：`首板` 当 1，`N连板` 取 N，空值当缺数据（排最后）。
 *
 * 不直接拿字符串排 —— 那样 `10连板` 会排在 `2连板` 前面（字符串比较逐位来）。
 */
function boardValue(board: string): number | null {
  if (board === '首板') return 1
  const matched = /^(\d+)/.exec(board)
  return matched ? Number(matched[1]) : null
}

/** 各列的排序口径。文字列显式 `first: 'asc'`，数值列默认先看最大的（见 lib/sort.ts）。 */
const SORTS: SortSpecs<AnomalyRow> = {
  sector: { value: (row) => row.sector, first: 'asc' },
  date: { value: (row) => row.date },
  // 「--」是后端给的「没有封板时间」，归一成缺数据 —— 否则它会排到 09:xx 前面
  time: { value: (row) => (row.time === '--' ? null : row.time), first: 'asc' },
  code: { value: (row) => row.code, first: 'asc' },
  name: { value: (row) => row.name, first: 'asc' },
  type: { value: (row) => row.type, first: 'asc' },
  board: { value: (row) => boardValue(row.board) },
  pct: { value: (row) => row.pct },
  cap: { value: (row) => row.cap },
}

/**
 * 板块下钻表的排序口径。后端默认按涨幅降序，这里只是把同一套排序能力补上。
 */
const DRILL_SORTS: SortSpecs<IndustryStock> = {
  code: { value: (row) => row.code, first: 'asc' },
  name: { value: (row) => row.name ?? row.code, first: 'asc' },
  close: { value: (row) => row.close },
  pct_chg: { value: (row) => row.pct_chg },
  amount: { value: (row) => row.amount },
  cap: { value: (row) => row.cap },
}

const NO_DRILL_ITEMS: IndustryStock[] = []

/**
 * 个股异动：把最近若干个交易日的涨停 / 涨停炸板 / 中大阳线摊成一张图。
 *
 * 移植自 yangban-desk 的「豆包异动图谱」，但**数据全部来自本站库**（涨停池 +
 * 本地日线 + 同花顺行业），不联网、不花配额 —— 口径见后端 `api/anomaly_map.py`。
 *
 * 取数只发一次（整块返回），板块 / 类型 / 题材启动 / 关键词四道筛选全在**前端内存里**
 * 做：这一屏最多两千多行，重发请求既慢又会让「筛选后条数」和「标签家数」对不上
 * （沿用形态页与板块页的做法）。
 */

/** 类型下拉的选项，值与 `AnomalyRow.type` 一致。 */
const TYPES = ['涨停', '涨停炸板', '中大阳线异动']

/**
 * 类型胶囊的配色。三种类型各用一个**既有令牌**，不新增色板：
 * 涨停＝涨色、涨停炸板＝琥珀、中大阳线异动＝青色。
 */
function TypePill({ type }: { type: string }) {
  const tone =
    type === '涨停'
      ? 'text-up bg-up/10'
      : type === '涨停炸板'
        ? 'text-accent bg-accent/10'
        : 'text-down bg-down/10'
  return (
    <span className={`border border-line-soft px-1.5 py-[1px] text-[12px] ${tone}`}>
      {type}
    </span>
  )
}

/** 统计卡的一格：数字大字（text-fg）、标签小字（text-fg-dim）。 */
function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex-1 basis-28 bg-ink-900 px-4 py-2.5">
      <div className="text-[13px] tracking-[0.1em] text-fg-dim">{label}</div>
      <div className="num mt-1 text-[20px] font-semibold leading-tight text-fg">
        {value}
      </div>
    </div>
  )
}

export default function AnomalyPage() {
  const [data, setData] = useState<AnomalyMap | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // 四道筛选。全部是本地状态，不发请求
  const [sector, setSector] = useState('')
  const [type, setType] = useState('')
  const [onlyTheme, setOnlyTheme] = useState(false)
  const [query, setQuery] = useState('')
  // 筛选只认防抖后的关键字：输入框照常即时响应，filter+sort 等敲字停顿后再跑
  const debouncedQuery = useDebounced(query, SEARCH_DEBOUNCE_MS)

  /**
   * 点「板块」后的下钻：该板块当天的**全部个股**（不是只有异动的那几只）。
   *
   * 带上那一行的 `date`：这一屏是 22 天的窗口，点 10-26 那行就该看 10-26 的行情，
   * 而不是「顺便」给最新一天 —— 两个日期混着看最容易把旧价当现价。
   * 再点同一个板块收起（toggle），省得再去点右上角的关闭。
   */
  const [drill, setDrill] = useState<{ sector: string; date: string } | null>(null)
  const [drillData, setDrillData] = useState<IndustryStocks | null>(null)
  const [drillLoading, setDrillLoading] = useState(false)
  const [drillError, setDrillError] = useState<string | null>(null)
  // 下钻表默认保持后端顺序（涨幅降序），点表头才重排
  const [drillSort, drillRows] = useSort(drillData?.items ?? NO_DRILL_ITEMS, DRILL_SORTS, {
    key: null,
  })

  useEffect(() => {
    if (!drill) {
      setDrillData(null)
      setDrillError(null)
      return
    }
    let stale = false
    setDrillLoading(true)
    setDrillError(null)
    // 先清空：换一个板块时留着上一个板块的名单，会闪一下错的内容
    setDrillData(null)
    api
      .anomalyIndustry(drill.sector, drill.date)
      .then((result) => {
        if (!stale) setDrillData(result)
      })
      .catch((err: Error) => {
        if (!stale) setDrillError(err.message)
      })
      .finally(() => {
        if (!stale) setDrillLoading(false)
      })
    return () => {
      stale = true
    }
  }, [drill])

  useEffect(() => {
    let stale = false
    setLoading(true)
    setError(null)
    api
      .anomalyMap(22)
      .then((result) => {
        if (!stale) setData(result)
      })
      .catch((err: Error) => {
        if (stale) return
        setData(null)
        setError(err.message)
      })
      .finally(() => {
        if (!stale) setLoading(false)
      })
    return () => {
      stale = true
    }
  }, [])

  const stats = useMemo(() => {
    const rows = data?.rows ?? []
    const count = (target: string) => rows.filter((row) => row.type === target).length
    return {
      total: rows.length,
      limitUp: count('涨停'),
      broken: count('涨停炸板'),
      bigYang: count('中大阳线异动'),
      themeStart: rows.filter((row) => row.theme_start).length,
    }
  }, [data])

  const visible = useMemo(() => {
    const rows = data?.rows ?? []
    const keyword = debouncedQuery.trim().toLowerCase()
    return rows.filter((row) => {
      if (sector && row.sector !== sector) return false
      if (type && row.type !== type) return false
      if (onlyTheme && !row.theme_start) return false
      if (!keyword) return true
      // 搜索面覆盖 代码 / 名称 / 涨停原因 / 行业 / 同批异动（同批里含邻居的名字）
      const haystack = [
        row.code,
        row.name,
        row.reason ?? '',
        row.industry ?? '',
        ...row.cohort,
      ]
        .join(' ')
        .toLowerCase()
      return haystack.includes(keyword)
    })
  }, [data, sector, type, onlyTheme, debouncedQuery])

  // 排序只作用在**筛完之后**的行上（与「显示 N / M 条」同步）。
  // 默认给 `date desc`：后端就是这个顺序（日期降序，同一天内按板块/时间/代码），
  // 而 lib/sort.ts 用的是稳定排序，同一天的行**保持后端给的顺序**，所以首屏与不排完全一致，
  // 只是「日期」列头上多一个 ▼，让人知道现在按什么排。
  const [sort, shown] = useSort(visible, SORTS, { key: 'date', dir: 'desc' })

  // 排序结果不截断，只把前 RENDER_LIMIT 行放进 DOM
  const rendered = shown.slice(0, RENDER_LIMIT)
  const hiddenRows = shown.length - rendered.length

  const allCount = data?.rows.length ?? 0
  const dirty = sector !== '' || type !== '' || onlyTheme || query !== ''
  const reset = () => {
    setSector('')
    setType('')
    setOnlyTheme(false)
    setQuery('')
  }

  const meta = data ? (
    <span className="num">
      {data.start} ~ {data.end} · {data.filter} · {data.source} · 生成于{' '}
      {data.generated_at} · 题材启动板块日 {data.theme_start_days}
    </span>
  ) : (
    '加载中…'
  )

  return (
    <Layout>
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}

      <Panel title="个股异动" meta={meta} delay={40}>
        {/* 统计卡：事件总数与三类事件的条数，外加题材启动日事件 */}
        <div className="flex flex-wrap items-stretch gap-px border-b border-line-soft bg-line-soft">
          <Stat label="事件总数" value={stats.total} />
          <Stat label="涨停" value={stats.limitUp} />
          <Stat label="涨停炸板" value={stats.broken} />
          <Stat label="中大阳线异动" value={stats.bigYang} />
          <Stat label="题材启动日事件" value={stats.themeStart} />
        </div>

        {/* 题材启动日的判定口径：这条规则不容易从表格里看出来（回看 120 天、含 20cm、
            按 10 个交易日断代），所以原样写在标题卡下面 */}
        {data?.theme_rule && (
          <div className="border-b border-line-soft px-4 py-1.5 text-[12px] leading-relaxed text-fg-dim">
            {data.theme_rule}
          </div>
        )}

        {/* 筛选行：板块下拉来自接口 sectors（已按条数降序），其余是本地条件 */}
        <div className="flex flex-wrap items-center gap-2 border-b border-line-soft px-4 py-2.5">
          <select
            value={sector}
            onChange={(event) => setSector(event.target.value)}
            className="num border border-line bg-ink-900 px-2 py-[3px] text-[13px] text-fg outline-none focus:border-fg-dim"
          >
            <option value="">全部板块</option>
            {(data?.sectors ?? []).map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
          <select
            value={type}
            onChange={(event) => setType(event.target.value)}
            className="num border border-line bg-ink-900 px-2 py-[3px] text-[13px] text-fg outline-none focus:border-fg-dim"
          >
            <option value="">全部类型</option>
            {TYPES.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
          <label className="flex cursor-pointer items-center gap-1.5 text-[13px] text-fg-muted">
            <input
              type="checkbox"
              checked={onlyTheme}
              onChange={(event) => setOnlyTheme(event.target.checked)}
              className="h-3.5 w-3.5 accent-accent"
            />
            只看题材启动日
          </label>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索 代码 / 名称 / 原因 / 行业 / 同批异动"
            className="w-64 border border-line bg-ink-900 px-2 py-[3px] text-[13px] text-fg outline-none focus:border-fg-dim"
          />
          <button
            type="button"
            onClick={reset}
            disabled={!dirty}
            className="num border border-line px-2 py-[2px] text-[13px] text-fg-dim transition-colors hover:border-fg-dim hover:text-fg disabled:opacity-40"
          >
            重置
          </button>
          <span className="num ml-auto text-[13px] text-fg-dim">
            显示 {shown.length} / {allCount} 条
            <span className="ml-2 text-fg-dim/70">点表头排序</span>
          </span>
        </div>

        {/* 板块下钻：点表格里的板块名后，这里列出该板块当天的**全部个股** */}
        {drill && (
          <div className="border-b border-line-soft">
            <div className="flex flex-wrap items-center gap-2 px-4 py-2">
              <span className="text-[13px] text-fg-dim">板块个股</span>
              <span className="text-[14px] text-fg">{drill.sector}</span>
              <span className="num text-[12px] text-fg-dim">
                {drillLoading
                  ? '加载中…'
                  : drillData
                    ? `${drillData.trade_date ?? drill.date} · ${drillData.count} 只`
                    : ''}
              </span>
              <button
                type="button"
                onClick={() => setDrill(null)}
                className="num ml-auto border border-line px-2 py-[2px] text-[12px] text-fg-dim transition-colors hover:border-fg-dim hover:text-fg"
              >
                收起
              </button>
            </div>
            {drillError ? (
              <div className="px-4 pb-3 text-[13px] text-danger">
                板块个股取数失败：{drillError}
              </div>
            ) : drillLoading ? (
              <div className="px-4 py-6 text-center text-[13px] text-fg-dim">加载中…</div>
            ) : !drillData || drillData.items.length === 0 ? (
              <div className="px-4 py-6 text-center text-[13px] text-fg-dim">
                该板块当天没有行情（停牌或未采集）
              </div>
            ) : (
              // 板块可能有上百只票（如「其他专用设备」），给一个固定高度内部滚动，
              // 不把下面那张异动表顶出屏幕
              <div className="max-h-[300px] overflow-auto">
                <table className="grid-table">
                  <thead>
                    <tr>
                      <SortTh sortKey="code" align="left" {...drillSort}>
                        代码
                      </SortTh>
                      <SortTh sortKey="name" align="left" {...drillSort}>
                        名称
                      </SortTh>
                      <SortTh sortKey="close" {...drillSort}>
                        收盘
                      </SortTh>
                      <SortTh sortKey="pct_chg" {...drillSort}>
                        涨幅%
                      </SortTh>
                      <SortTh sortKey="amount" {...drillSort}>
                        成交额
                      </SortTh>
                      <SortTh sortKey="cap" {...drillSort}>
                        总市值
                      </SortTh>
                    </tr>
                  </thead>
                  <tbody>
                    {drillRows.map((item) => (
                      <tr key={item.code}>
                        <td className="num !text-left">
                          <Link
                            to={`/stock/${encodeURIComponent(item.code)}`}
                            className="text-fg-dim hover:text-accent"
                          >
                            {item.code}
                          </Link>
                        </td>
                        <td className="!text-left">
                          <Link
                            to={`/stock/${encodeURIComponent(item.code)}`}
                            className="text-fg hover:text-accent"
                          >
                            {item.name ?? item.code}
                          </Link>
                        </td>
                        <td>
                          <span className="num">
                            {item.close == null ? '—' : item.close.toFixed(2)}
                          </span>
                        </td>
                        <td>
                          <span className={`num ${toneOf(item.pct_chg)}`}>
                            {fmtPct(item.pct_chg)}
                          </span>
                        </td>
                        <td>
                          <span className="num text-fg-muted">{fmtAmount(item.amount)}</span>
                        </td>
                        <td>
                          <span className="num text-fg-muted">
                            {item.cap == null ? '—' : `${item.cap.toFixed(1)}亿`}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {loading ? (
          <div className="px-4 py-10 text-center text-[14px] text-fg-dim">加载中…</div>
        ) : allCount === 0 ? (
          <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
            窗口内没有异动数据（涨停池为空，或该窗口还没采到数据）
          </div>
        ) : visible.length === 0 ? (
          <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
            当前筛选下没有匹配的异动
          </div>
        ) : (
          // 横向可滚动：12 列在窄屏下必然超宽，宁可内部横滑也不压扁单元格
          <div className="overflow-x-auto">
            <table className="grid-table">
              <thead>
                <tr>
                  <SortTh
                    sortKey="sector"
                    align="left"
                    {...sort}
                    title="同花顺行业路径的最后一段（如「电子-半导体-集成电路」→「集成电路」）。与原型用东财行业不同，本站取 stock_basic.industry"
                  >
                    板块
                  </SortTh>
                  <SortTh sortKey="date" align="left" {...sort}>日期</SortTh>
                  <SortTh sortKey="time" align="left" {...sort}>时间</SortTh>
                  <SortTh sortKey="code" align="left" {...sort}>代码</SortTh>
                  <SortTh sortKey="name" align="left" {...sort}>名称</SortTh>
                  <SortTh sortKey="type" align="left" {...sort}>类型</SortTh>
                  <SortTh sortKey="board" {...sort}>连板</SortTh>
                  <SortTh sortKey="pct" {...sort}>涨幅%</SortTh>
                  <SortTh sortKey="cap" {...sort} title="总市值（本站没有流通市值）">
                    总市值
                  </SortTh>
                  {/* 「涨停原因」与「同批异动」不挂排序（SortTh 不给 sortKey 就是普通表头）：
                      前者是自由文本、后者是一串标签，按拼音排没有意义，
                      挂上箭头会让人点了没反应。 */}
                  <SortTh align="left" title="同花顺涨停原因（limit_reason），只有涨停股有">
                    涨停原因
                  </SortTh>
                  <SortTh
                    align="left"
                    title="同一天、同一板块的全部异动（含自己），标签形如「名称(涨停@09:31)」"
                  >
                    同批异动
                  </SortTh>
                </tr>
              </thead>
              <tbody>
                {rendered.map((row: AnomalyRow) => (
                  <tr
                    key={`${row.date}-${row.code}-${row.type}`}
                    // 题材启动日的整行淡绿底：这一类是「这个板块刚开始动」，值得一眼捞出来
                    className={row.theme_start ? 'bg-ok/10' : ''}
                  >
                    <td className="!text-left">
                      {/* 板块＝同花顺行业路径的末段（去掉 Ⅰ/Ⅱ/Ⅲ 级别记号），
                          完整路径放悬停提示 —— 2026-10-09 用户指出原先并排的「行业」
                          列显示的就是同一个值（重复），那一列已删。
                          点一下在表格上方展开该板块当天的**全部个股**（不是只有异动的那几只），
                          再点一次收起。 */}
                      <button
                        type="button"
                        onClick={() =>
                          setDrill((current) =>
                            // 同一个板块**同一天**才收起；同一板块换一天是「看另一天的名单」，
                            // 保持展开（2026-10-28 体检：原来只比板块名，点另一天会把面板关掉）
                            current &&
                            current.sector === row.sector &&
                            current.date === row.date
                              ? null
                              : { sector: row.sector, date: row.date },
                          )
                        }
                        title={`${row.industry ?? row.sector} · 点开看该板块当天的全部个股`}
                        className={`text-left transition-colors hover:text-accent ${
                          drill?.sector === row.sector ? 'text-accent' : 'text-fg-muted'
                        }`}
                      >
                        {row.sector}
                      </button>
                      {row.theme_start && (
                        <span className="ml-1.5 border border-ok/40 px-1 py-[1px] text-[11px] text-ok">
                          题材启动日
                        </span>
                      )}
                      {/* 启动日早于窗口的板块：只在这一板块窗口内首次出现的那行提示一次，
                          不加绿底 —— 它今天不是启动日，只是「早就启动了还在动」 */}
                      {!row.theme_start && row.early_start && (
                        <span
                          className="ml-1.5 border border-line px-1 py-[1px] text-[11px] text-fg-dim"
                          title={`该板块本轮启动于 ${row.early_start}，早于当前 ${data?.days ?? 22} 天窗口`}
                        >
                          启动于 {row.early_start.slice(5)}
                        </span>
                      )}
                    </td>
                    <td className="num !text-left text-fg-muted">{row.date}</td>
                    <td className="num !text-left">{row.time}</td>
                    <td className="!text-left">
                      <Link
                        to={`/stock/${encodeURIComponent(row.code)}`}
                        className="num text-fg-dim hover:text-accent"
                      >
                        {row.code}
                      </Link>
                    </td>
                    <td className="!text-left">
                      <Link to={`/stock/${encodeURIComponent(row.code)}`} className="text-fg hover:text-accent">
                        {row.name}
                      </Link>
                    </td>
                    <td className="!text-left">
                      <TypePill type={row.type} />
                    </td>
                    <td>
                      {row.board ? (
                        <span className="num text-accent">{row.board}</span>
                      ) : (
                        <span className="text-fg-dim">—</span>
                      )}
                    </td>
                    <td>
                      <span className={`num ${toneOf(row.pct)}`}>{fmtPct(row.pct)}</span>
                    </td>
                    <td>
                      <span className="num text-fg-muted">
                        {row.cap == null ? '—' : `${row.cap.toFixed(1)}亿`}
                      </span>
                    </td>
                    <td className="!text-left">
                      {row.reason ? (
                        <span
                          className="inline-block max-w-[240px] truncate align-bottom text-[13px] text-fg-muted"
                          title={row.reason}
                        >
                          {row.reason}
                        </span>
                      ) : (
                        <span className="text-fg-dim">—</span>
                      )}
                    </td>
                    <td className="!text-left">
                      <span
                        className="inline-block max-w-[380px] truncate align-bottom text-[13px] text-fg-dim"
                        title={row.cohort.join('、')}
                      >
                        {row.cohort.join('、')}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {hiddenRows > 0 && (
              <div className="border-t border-line-soft px-4 py-2 text-[12px] text-fg-dim">
                另有 {hiddenRows} 行未显示，可用上方筛选或搜索缩小范围
              </div>
            )}
          </div>
        )}
      </Panel>
    </Layout>
  )
}
