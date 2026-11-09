import { useEffect, useMemo, useState } from 'react'
import { api, researchApi } from '../api/client'
import type { ResearchLowStartOut, ResearchSectorOut, ResearchTrendOut } from '../api/types'
import Panel from '../components/Panel'
import Segmented from '../components/Segmented'
import StockLink from '../components/StockLink'

type Tab = 'trend' | 'low' | 'sector'

const TABS: { key: Tab; label: string; hint: string }[] = [
  { key: 'trend', label: '趋势研究', hint: '均线/相对强度/量价；未验证稳定优势' },
  { key: 'low', label: '低位启动', hint: '区间位置/平台/波动收敛；仅研究特征' },
  { key: 'sector', label: '板块状态', hint: 'RPS/成交额/广度；不代表未来涨跌' },
]

function pct(value: number | null | undefined) {
  if (value == null) return '—'
  return `${value > 0 ? '+' : ''}${value.toFixed(1)}%`
}

function num(value: number | null | undefined, digits = 1) {
  if (value == null) return '—'
  return value.toFixed(digits)
}

/**
 * 关键字过滤 + 单列排序。
 *
 * 研究表不提供「综合分」排序：只能按用户点的那一列显式排序，缺失值一律沉底
 * （不能当 0 参与比较，否则「数据不足」的票会混进最弱/最强两端）。
 */
function applyView<T>(
  rows: T[],
  keyword: string,
  text: (row: T) => string,
  sortKey: string,
  sortDir: 1 | -1,
  pick: (row: T, key: string) => number | null | undefined,
): T[] {
  const query = keyword.trim().toLowerCase()
  const out = query ? rows.filter((row) => text(row).toLowerCase().includes(query)) : [...rows]
  if (sortKey) {
    out.sort((left, right) => {
      const a = pick(left, sortKey)
      const b = pick(right, sortKey)
      if (a == null && b == null) return 0
      if (a == null) return 1
      if (b == null) return -1
      return (a - b) * sortDir
    })
  }
  return out
}

function SortTh({
  label,
  column,
  sortKey,
  sortDir,
  onSort,
}: {
  label: string
  column: string
  sortKey: string
  sortDir: 1 | -1
  onSort: (key: string) => void
}) {
  const active = sortKey === column
  return (
    <th>
      <button
        type="button"
        className="inline-flex items-center gap-1 hover:text-fg"
        title="点击切换排序"
        onClick={() => onSort(column)}
      >
        {label}
        <span className="text-[10px] text-fg-dim">{active ? (sortDir > 0 ? '▲' : '▼') : '↕'}</span>
      </button>
    </th>
  )
}

export default function Research() {
  const [tab, setTab] = useState<Tab>('trend')
  const [date, setDate] = useState('')
  const [keyword, setKeyword] = useState('')
  const [sortKey, setSortKey] = useState('')
  const [sortDir, setSortDir] = useState<1 | -1>(-1)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [trend, setTrend] = useState<ResearchTrendOut | null>(null)
  const [low, setLow] = useState<ResearchLowStartOut | null>(null)
  const [sector, setSector] = useState<ResearchSectorOut | null>(null)
  const [added, setAdded] = useState<Record<string, boolean>>({})

  useEffect(() => {
    let alive = true
    setLoading(true)
    setError('')
    const call =
      tab === 'trend'
        ? researchApi.trend({ date: date || null, limit: 120 }).then((data) => alive && setTrend(data))
        : tab === 'low'
          ? researchApi.lowStart({ date: date || null, limit: 120 }).then((data) => alive && setLow(data))
          : researchApi.sectorState({ date: date || null, limit: 120 }).then((data) => alive && setSector(data))
    call
      .catch((err: Error) => alive && setError(err.message))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [tab, date])

  const data = tab === 'trend' ? trend : tab === 'low' ? low : sector

  const changeTab = (next: Tab) => {
    setTab(next)
    setKeyword('')
    setSortKey('')
  }

  const toggleSort = (key: string) => {
    if (sortKey === key) {
      setSortDir((prev) => (prev === 1 ? -1 : 1))
      return
    }
    setSortKey(key)
    setSortDir(-1)
  }

  const trendRows = useMemo(
    () =>
      applyView(
        trend?.rows ?? [],
        keyword,
        (row) => `${row.code} ${row.name ?? ''} ${row.industry ?? ''}`,
        sortKey,
        sortDir,
        (row, key) => {
          if (key === 'close') return row.close
          if (key === 'rps60') return row.rps60
          if (key === 'trend_days') return row.trend_days
          if (key === 'amount_ratio20') return row.amount_ratio20
          if (key === 'off_high60') return row.off_high60
          return null
        },
      ),
    [trend, keyword, sortKey, sortDir],
  )

  const lowRows = useMemo(
    () =>
      applyView(
        low?.rows ?? [],
        keyword,
        (row) => `${row.code} ${row.name ?? ''} ${row.industry ?? ''}`,
        sortKey,
        sortDir,
        (row, key) => {
          if (key === 'close') return row.close
          if (key === 'pos_pct') return row.pos_pct
          if (key === 'platform_height') return row.platform_height
          if (key === 'vol_contraction') return row.vol_contraction
          if (key === 'amount_ratio20') return row.amount_ratio20
          if (key === 'rps20') return row.rps20
          return null
        },
      ),
    [low, keyword, sortKey, sortDir],
  )

  const sectorRows = useMemo(
    () =>
      applyView(
        sector?.rows ?? [],
        keyword,
        (row) => `${row.sector_code} ${row.name}`,
        sortKey,
        sortDir,
        (row, key) => {
          if (key === 'rps20') return row.rps20
          if (key === 'ret20') return row.ret20
          if (key === 'amount_ratio20') return row.amount_ratio20
          if (key === 'up_ratio') return row.up_ratio
          if (key === 'strong_days') return row.strong_days
          return null
        },
      ),
    [sector, keyword, sortKey, sortDir],
  )

  const shown =
    tab === 'trend' ? trendRows.length : tab === 'low' ? lowRows.length : sectorRows.length

  const add = async (code: string, name: string | null) => {
    try {
      await api.addWatchlist(code, name ?? undefined)
      setAdded((prev) => ({ ...prev, [code]: true }))
    } catch {
      setError('加入自选失败')
    }
  }

  return (
    <div className="page-stack">
      <Panel
        title="盘后研究"
        meta={
          <span>
            {data?.trade_date ?? '—'} · {data?.coverage?.candidates != null ? `候选 ${data.coverage.candidates}` : ''}
            {data?.coverage?.with_members != null ? ` · 有成分 ${data.coverage.with_members}` : ''}
            {keyword ? ` · 命中 ${shown}` : ''}
          </span>
        }
      >
        <div className="flex flex-wrap items-center gap-2 text-[13px]">
          <Segmented value={tab} items={TABS} onChange={changeTab} />
          <label className="ml-2 text-fg-muted">
            交易日
            <input
              type="date"
              value={date}
              onChange={(event) => setDate(event.target.value)}
              className="ml-1 rounded border border-line bg-ink-800 px-2 py-1 text-fg"
            />
          </label>
          <label className="text-fg-muted">
            搜索
            <input
              type="search"
              value={keyword}
              placeholder="代码 / 名称 / 行业"
              onChange={(event) => setKeyword(event.target.value)}
              className="ml-1 w-40 rounded border border-line bg-ink-800 px-2 py-1 text-fg"
            />
          </label>
          {(tab === 'trend' || tab === 'low') && (
            <span className="text-fg-dim">研究指标，不是买入信号；样本与覆盖见下方说明</span>
          )}
        </div>
        <div className="mt-2 rounded border border-line bg-ink-800 px-3 py-2 text-[12px] text-fg-muted">
          {data?.strategy?.status ?? '研究指标'}；版本 {data?.strategy?.version ?? '—'}。
          {data?.coverage?.note ? ` ${data.coverage.note}` : ''}
        </div>
        {loading && <div className="py-6 text-center text-fg-muted">加载中…</div>}
        {error && <div className="py-6 text-center text-loss">{error}</div>}
        {!loading && !error && data && data.rows.length === 0 && (
          <div className="py-6 text-center text-fg-muted">当前日期没有可展示的数据（或数据覆盖不足）</div>
        )}
        {!loading && !error && data && data.rows.length > 0 && shown === 0 && (
          <div className="py-6 text-center text-fg-muted">没有匹配「{keyword}」的条目</div>
        )}

        {!loading && !error && tab === 'trend' && trend && shown > 0 && (
          <div className="mt-3 overflow-x-auto">
            <table className="data-table text-[12px]">
              <thead>
                <tr>
                  <th>代码/名称</th>
                  <th>行业</th>
                  <SortTh label="收盘" column="close" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                  <SortTh label="RPS20/60/120" column="rps60" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                  <th>MA20/60/120</th>
                  <SortTh label="趋势天数" column="trend_days" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                  <SortTh label="量比" column="amount_ratio20" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                  <SortTh label="距高点" column="off_high60" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                  <th>状态</th>
                  <th>依据 / 未满足</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {trendRows.map((row) => (
                  <tr key={row.code}>
                    <td>
                      <StockLink code={row.code}>{row.name ?? row.code}</StockLink>
                      <span className="num ml-1 text-fg-dim">{row.code}</span>
                    </td>
                    <td className="max-w-[160px] truncate" title={row.industry ?? ''}>{row.industry ?? '—'}</td>
                    <td className="num">{num(row.close, 2)}</td>
                    <td className="num">{num(row.rps20)} / {num(row.rps60)} / {num(row.rps120)}</td>
                    <td className="num">{num(row.ma20, 2)} / {num(row.ma60, 2)} / {num(row.ma120, 2)}</td>
                    <td className="num">{row.trend_days ?? '—'}</td>
                    <td className="num">{num(row.amount_ratio20, 2)}</td>
                    <td className="num">{pct(row.off_high60)}</td>
                    <td>{row.state}</td>
                    <td className="max-w-[360px]">
                      <div className="text-fg-muted" title={(row.reasons ?? []).join('；')}>
                        {(row.reasons ?? []).slice(0, 3).join('；') || '—'}
                      </div>
                      {(row.unmet ?? []).length > 0 && (
                        <div className="text-warn" title={(row.unmet ?? []).join('；')}>
                          未满足：{(row.unmet ?? []).slice(0, 3).join('；')}
                        </div>
                      )}
                      {(row.missing ?? []).length > 0 && (
                        <div className="text-loss">缺失：{(row.missing ?? []).join('；')}</div>
                      )}
                    </td>
                    <td>
                      <button
                        type="button"
                        className="text-accent disabled:text-fg-dim"
                        disabled={added[row.code]}
                        onClick={() => void add(row.code, row.name)}
                      >
                        {added[row.code] ? '已自选' : '+自选'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {!loading && !error && tab === 'low' && low && shown > 0 && (
          <div className="mt-3 overflow-x-auto">
            <table className="data-table text-[12px]">
              <thead>
                <tr>
                  <th>代码/名称</th>
                  <th>行业</th>
                  <SortTh label="收盘" column="close" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                  <SortTh label="区间位置" column="pos_pct" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                  <th>平台天数/振幅</th>
                  <SortTh label="波动收敛" column="vol_contraction" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                  <SortTh label="量比" column="amount_ratio20" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                  <SortTh label="RPS20" column="rps20" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                  <th>状态</th>
                  <th>依据</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {lowRows.map((row) => (
                  <tr key={row.code}>
                    <td>
                      <StockLink code={row.code}>{row.name ?? row.code}</StockLink>
                      <span className="num ml-1 text-fg-dim">{row.code}</span>
                    </td>
                    <td className="max-w-[160px] truncate" title={row.industry ?? ''}>{row.industry ?? '—'}</td>
                    <td className="num">{num(row.close, 2)}</td>
                    <td className="num">{row.pos_pct.toFixed(1)}%</td>
                    <td className="num">{row.platform_days} 天 / {row.platform_height.toFixed(1)}%</td>
                    <td className="num">{num(row.vol_contraction, 2)}</td>
                    <td className="num">{num(row.amount_ratio20, 2)}</td>
                    <td className="num">{num(row.rps20)}</td>
                    <td>{row.state}</td>
                    <td className="max-w-[320px] text-fg-muted" title={row.reasons.join('；')}>
                      {row.reasons.join('；')}
                    </td>
                    <td>
                      <button
                        type="button"
                        className="text-accent disabled:text-fg-dim"
                        disabled={added[row.code]}
                        onClick={() => void add(row.code, row.name)}
                      >
                        {added[row.code] ? '已自选' : '+自选'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {!loading && !error && tab === 'sector' && sector && shown > 0 && (
          <div className="mt-3 overflow-x-auto">
            <table className="data-table text-[12px]">
              <thead>
                <tr>
                  <th>板块</th>
                  <SortTh label="RPS20" column="rps20" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                  <SortTh label="5/10/20/60 日" column="ret20" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                  <SortTh label="成交额比" column="amount_ratio20" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                  <SortTh label="上涨占比" column="up_ratio" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                  <SortTh label="强势天数" column="strong_days" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                  <th>状态</th>
                  <th>核心股</th>
                </tr>
              </thead>
              <tbody>
                {sectorRows.map((row) => (
                  <tr key={row.sector_code}>
                    <td>
                      <span className="text-fg">{row.name}</span>
                      <span className="num ml-1 text-fg-dim">{row.sector_code}</span>
                    </td>
                    <td className="num">{num(row.rps20)}</td>
                    <td className="num">
                      {pct(row.ret5)} / {pct(row.ret10)} / {pct(row.ret20)} / {pct(row.ret60)}
                    </td>
                    <td className="num">{num(row.amount_ratio20, 2)}</td>
                    <td className="num">{row.up_ratio == null ? '—' : `${row.up_ratio.toFixed(1)}%`}</td>
                    <td className="num">{row.strong_days}</td>
                    <td>{row.state}</td>
                    <td>
                      {row.core.slice(0, 3).map((item) => (
                        <span key={item.code} className="mr-2 inline-block">
                          <StockLink code={item.code}>{item.code}</StockLink>
                          <span className="num ml-1 text-fg-dim">{pct(item.pct_chg)}</span>
                        </span>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  )
}
