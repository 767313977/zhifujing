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

/** 成分股完整性状态的简短中文（P0.9-05）。 */
const BREADTH_LABEL: Record<string, string> = {
  complete: '完整',
  partial: '部分覆盖',
  source_empty: '来源无成分',
  failed: '采集失败',
  unsupported_history: '来源不支持',
  unverified: '无法对账',
  not_collected: '未采集',
  mismatch: '与来源不一致',
  missing_manifest: '无 manifest',
  no_members: '无成分股',
}

function breadthLabel(status?: string) {
  if (!status) return '—'
  return BREADTH_LABEL[status] ?? status
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
  const [tick, setTick] = useState(0)
  const [trend, setTrend] = useState<ResearchTrendOut | null>(null)
  const [low, setLow] = useState<ResearchLowStartOut | null>(null)
  const [sector, setSector] = useState<ResearchSectorOut | null>(null)
  const [added, setAdded] = useState<Record<string, boolean>>({})
  // 严格日期（P0.9-03）：板块数据缺日时不回退，直接显示「该日不可用」。
  const [strictDate, setStrictDate] = useState(false)

  useEffect(() => {
    let alive = true
    setLoading(true)
    setError('')
    const call =
      tab === 'trend'
        ? researchApi.trend({ date: date || null, limit: 120 }).then((data) => alive && setTrend(data))
        : tab === 'low'
          ? researchApi.lowStart({ date: date || null, limit: 120 }).then((data) => alive && setLow(data))
          : researchApi
              .sectorState({ date: date || null, limit: 120, allowFallback: !strictDate })
              .then((data) => alive && setSector(data))
    call
      .catch((err: Error) => alive && setError(err.message))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [tab, date, tick, strictDate])

  const data = tab === 'trend' ? trend : tab === 'low' ? low : sector

  const coverage = data?.coverage
  const dataStatus = coverage?.data_status
  // ok / fallback 都算「有可用数据」；其余是数据缺失或覆盖不足（P0.9-01/02）
  const dataProblem = Boolean(dataStatus && dataStatus !== 'ok' && dataStatus !== 'fallback')
  const problemText = coverage?.status_text ?? coverage?.note ?? ''
  const rowCounts =
    coverage?.daily_rows != null
      ? `当日日线 ${coverage.daily_rows} 行 · RPS ${coverage.rps_rows ?? 0} 行 · RPS60 ${coverage.rps60_rows ?? 0} 行`
      : ''

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
          {tab === 'sector' && (
            <label className="text-fg-muted" title="所选日期没有板块日线时，不回退到更早的日期">
              <input
                type="checkbox"
                className="mr-1 align-middle"
                checked={strictDate}
                onChange={(event) => setStrictDate(event.target.checked)}
              />
              严格日期（不回退）
            </label>
          )}
          {(tab === 'trend' || tab === 'low') && (
            <span className="text-fg-dim">研究指标，不是买入信号；样本与覆盖见下方说明</span>
          )}
        </div>
        <div className="mt-2 rounded border border-line bg-ink-800 px-3 py-2 text-[12px] text-fg-muted">
          {data?.strategy?.status ?? '研究指标'}；版本 {data?.strategy?.version ?? '—'}。
          {data?.coverage?.note ? ` ${data.coverage.note}` : ''}
          {keyword ? ` 搜索仅过滤当前返回的 ${shown} 条，不是全市场排名。` : ''}
        </div>
        {data?.data_latest && date && date > data.data_latest && (
          <div className="mt-2 rounded border border-line bg-ink-800 px-3 py-2 text-[12px] text-warn">
            所选日期 {date} 晚于库内最新数据 {data.data_latest}，结果大概率为空。
          </div>
        )}
        {data?.data_latest && data.trade_date < data.data_latest && (
          <div className="mt-2 rounded border border-line bg-ink-800 px-3 py-2 text-[12px] text-warn">
            历史数据：结果截至 {data.trade_date}，库内最新 {data.data_latest}；这不是最新盘后结果。
          </div>
        )}
        {dataProblem && (
          <div className="mt-2 rounded border border-line bg-ink-800 px-3 py-2 text-[12px] text-warn">
            {problemText || '当前日期的研究数据不完整，无法给出可靠结论'}
            {rowCounts ? `（${rowCounts}）` : ''}
          </div>
        )}
        {tab === 'sector' && sector?.fallback?.applied && (
          <div
            className={`mt-2 rounded border border-line bg-ink-800 px-3 py-2 text-[12px] ${
              sector.fallback.severity === 'high' ? 'text-loss' : 'text-warn'
            }`}
          >
            {sector.fallback.reason}
            {sector.fallback.actual_data_date ? `；实际数据日 ${sector.fallback.actual_data_date}` : ''}
          </div>
        )}
        {tab === 'sector' && coverage?.boards_in_day != null && (
          <div className="mt-2 rounded border border-line bg-ink-800 px-3 py-2 text-[12px] text-fg-muted">
            板块 {coverage.boards_in_day} · 有 20 日 RPS {coverage.boards_with_rps20 ?? 0} · 历史不足{' '}
            {coverage.excluded_short_history ?? 0} · RPS20 不足 {coverage.excluded_no_rps20 ?? 0} · 返回{' '}
            {coverage.returned ?? 0}
            {coverage.truncated ? `（另有 ${coverage.truncated} 条被截断）` : ''}
            {' · '}
            manifest {coverage.manifest_installed ? `已安装（${coverage.manifest_boards ?? 0} 个板块）` : '未安装'}
            {' · '}广度 完整 {coverage.breadth_complete ?? 0} / 不一致 {coverage.breadth_mismatch ?? 0} /
            部分 {coverage.breadth_partial ?? 0} / 未知 {coverage.breadth_unknown ?? 0}
          </div>
        )}
        {loading && <div className="py-6 text-center text-fg-muted">加载中…</div>}
        {error && (
          <div className="py-6 text-center">
            <div className="text-loss">{error}</div>
            <button
              type="button"
              className="mt-2 text-accent disabled:text-fg-dim"
              disabled={loading}
              onClick={() => setTick((value) => value + 1)}
            >
              重试
            </button>
            <div className="mt-1 text-[12px] text-fg-dim">
              会保留当前 Tab、交易日与筛选条件重新请求
            </div>
          </div>
        )}
        {!loading && !error && data && data.rows.length === 0 && (
          <div className="py-6 text-center text-fg-muted">
            {dataProblem
              ? problemText || '当前日期无法生成结果（研究数据不完整）'
              : '当前日期没有可展示的数据（或数据覆盖不足）'}
          </div>
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
                    <td className="max-w-[320px]">
                      <div className="text-fg-muted" title={row.reasons.join('；')}>
                        {row.reasons.join('；')}
                      </div>
                      {row.missing.length > 0 && (
                        <div className="text-warn" title={row.missing.join('；')}>
                          缺失：{row.missing.join('；')}
                        </div>
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
                  <th>广度</th>
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
                    <td>
                      {row.state}
                      {row.state_daily && row.state_daily !== row.state && (
                        <div className="text-[11px] text-fg-dim">日线强弱：{row.state_daily}</div>
                      )}
                    </td>
                    <td
                      className={`text-[11px] ${
                        row.breadth_status && row.breadth_status !== 'complete'
                          ? 'text-warn'
                          : 'text-fg-muted'
                      }`}
                    >
                      {breadthLabel(row.breadth_status)}
                    </td>
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
