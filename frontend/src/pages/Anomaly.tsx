import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { AnomalyMap, AnomalyRow } from '../api/types'
import Alert from '../components/Alert'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import { fmtPct, toneOf } from '../lib/format'

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

/**
 * 行业列只显示末段；完整三级路径放标题栏（与形态页同一套处理）。
 *
 * ⚠️ 末尾的 `Ⅰ/Ⅱ/Ⅲ` 是同花顺标级别用的记号（`电子-半导体-集成电路Ⅲ`），
 * **与后端 `_sector_of` 同一规则地去掉** —— 两处不同步的话，「板块」列与「行业」列
 * 会只差一个后缀，看起来像同一个字段显示了两个不同的值。
 */
function industryLast(industry: string | null): string {
  if (!industry) return '—'
  const parts = industry.split('-')
  const raw = (parts[parts.length - 1] || '').trim()
  const trimmed = raw.replace(/[ⅠⅡⅢⅣⅤ]+$/, '').trim()
  return trimmed || raw || '—'
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
            显示 {visible.length} / {allCount} 条
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
                  <th
                    className="!text-left"
                    title="同花顺行业路径的最后一段（如「电子-半导体-集成电路」→「集成电路」）。与原型用东财行业不同，本站取 stock_basic.industry"
                  >
                    板块
                  </th>
                  <th className="!text-left">日期</th>
                  <th className="!text-left">时间</th>
                  <th className="!text-left">代码</th>
                  <th className="!text-left">名称</th>
                  <th className="!text-left">类型</th>
                  <th>连板</th>
                  <th>涨幅%</th>
                  <th>总市值</th>
                  <th className="!text-left" title="同花顺行业完整三级路径（悬停看全）">
                    行业
                  </th>
                  <th className="!text-left" title="同花顺涨停原因（limit_reason），只有涨停股有">
                    涨停原因
                  </th>
                  <th
                    className="!text-left"
                    title="同一天、同一板块的全部异动（含自己），标签形如「名称(涨停@09:31)」"
                  >
                    同批异动
                  </th>
                </tr>
              </thead>
              <tbody>
                {visible.map((row: AnomalyRow) => (
                  <tr
                    key={`${row.date}-${row.code}-${row.type}`}
                    // 题材启动日的整行淡绿底：这一类是「这个板块刚开始动」，值得一眼捞出来
                    className={row.theme_start ? 'bg-ok/10' : ''}
                  >
                    <td className="!text-left">
                      <span className="text-fg-muted">{row.sector}</span>
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
                      <span
                        className="whitespace-nowrap text-[13px] text-fg-muted"
                        title={row.industry ?? undefined}
                      >
                        {industryLast(row.industry)}
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
