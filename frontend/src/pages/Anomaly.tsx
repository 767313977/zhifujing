import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { AnomalyMap, AnomalyRow } from '../api/types'
import Alert from '../components/Alert'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import SortTh from '../components/SortTh'
import { fmtPct, toneOf } from '../lib/format'
import { useSort } from '../lib/sort'
import type { SortSpecs } from '../lib/sort'

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
    const keyword = query.trim().toLowerCase()
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
  }, [data, sector, type, onlyTheme, query])

  // 排序只作用在**筛完之后**的行上（与「显示 N / M 条」同步）。
  // 默认给 `date desc`：后端就是这个顺序（日期降序，同一天内按板块/时间/代码），
  // 而 lib/sort.ts 用的是稳定排序，同一天的行**保持后端给的顺序**，所以首屏与不排完全一致，
  // 只是「日期」列头上多一个 ▼，让人知道现在按什么排。
  const [sort, shown] = useSort(visible, SORTS, { key: 'date', dir: 'desc' })

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
                {shown.map((row: AnomalyRow) => (
                  <tr
                    key={`${row.date}-${row.code}-${row.type}`}
                    // 题材启动日的整行淡绿底：这一类是「这个板块刚开始动」，值得一眼捞出来
                    className={row.theme_start ? 'bg-ok/10' : ''}
                  >
                    <td className="!text-left">
                      {/* 板块＝同花顺行业路径的末段（去掉 Ⅰ/Ⅱ/Ⅲ 级别记号），
                          完整路径放悬停提示 —— 2026-10-09 用户指出原先并排的「行业」
                          列显示的就是同一个值（重复），那一列已删。 */}
                      <span className="text-fg-muted" title={row.industry ?? undefined}>
                        {row.sector}
                      </span>
                      {row.theme_start && (
                        <span className="ml-1.5 border border-ok/40 px-1 py-[1px] text-[11px] text-ok">
                          题材启动日
                        </span>
                      )}
                    </td>
                    <td className="num !text-left text-fg-muted">{row.date}</td>
                    <td className="num !text-left">{row.time}</td>
                    <td className="!text-left">
                      <Link
                        to={`/stock/${row.code}`}
                        className="num text-fg-dim hover:text-accent"
                      >
                        {row.code}
                      </Link>
                    </td>
                    <td className="!text-left">
                      <Link to={`/stock/${row.code}`} className="text-fg hover:text-accent">
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
          </div>
        )}
      </Panel>
    </Layout>
  )
}
