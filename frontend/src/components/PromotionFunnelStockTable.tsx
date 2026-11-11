import { useMemo, useState } from 'react'
import type { PromotionFunnelStatus, PromotionFunnelStock } from '../api/types'
import { fmtInt, fmtNum, fmtPct, fmtSealTime, toneOf } from '../lib/format'
import StockLink from './StockLink'

const STATUS_LABEL: Record<PromotionFunnelStatus, string> = {
  PROMOTED: '晋级',
  BROKEN: '炸板',
  FAILED: '失败',
  UNVERIFIABLE: '不可验证',
}

const STATUS_CLASS: Record<PromotionFunnelStatus, string> = {
  PROMOTED: 'text-up',
  BROKEN: 'text-warn',
  FAILED: 'text-fg-muted',
  UNVERIFIABLE: 'text-danger',
}

const STATUS_ORDER: Record<PromotionFunnelStatus, number> = {
  PROMOTED: 0,
  BROKEN: 1,
  FAILED: 2,
  UNVERIFIABLE: 3,
}

type StatusFilter = 'ALL' | PromotionFunnelStatus
type SortKey = 'default' | 'pct' | 'level'

const FILTERS: { key: StatusFilter; label: string }[] = [
  { key: 'ALL', label: '全部' },
  { key: 'PROMOTED', label: '晋级' },
  { key: 'BROKEN', label: '炸板' },
  { key: 'FAILED', label: '失败' },
  { key: 'UNVERIFIABLE', label: '不可验证' },
]

export default function PromotionFunnelStockTable({
  stocks,
}: {
  stocks: PromotionFunnelStock[]
}) {
  const [filter, setFilter] = useState<StatusFilter>('ALL')
  const [sortKey, setSortKey] = useState<SortKey>('default')

  const counts = useMemo(() => {
    const result: Record<StatusFilter, number> = {
      ALL: stocks.length,
      PROMOTED: 0,
      BROKEN: 0,
      FAILED: 0,
      UNVERIFIABLE: 0,
    }
    for (const stock of stocks) result[stock.status] += 1
    return result
  }, [stocks])

  const visible = useMemo(() => {
    const filtered = filter === 'ALL' ? stocks : stocks.filter((item) => item.status === filter)
    return [...filtered].sort((a, b) => {
      if (sortKey === 'pct') {
        return (b.pct_chg ?? -Infinity) - (a.pct_chg ?? -Infinity)
      }
      if (sortKey === 'level') {
        return b.previous_level - a.previous_level || a.code.localeCompare(b.code)
      }
      return (
        STATUS_ORDER[a.status] - STATUS_ORDER[b.status] ||
        b.previous_level - a.previous_level ||
        a.code.localeCompare(b.code)
      )
    })
  }, [filter, sortKey, stocks])

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 border-b border-line-soft px-3 py-2">
        <div className="flex flex-wrap gap-1.5">
          {FILTERS.map((item) => (
            <button
              key={item.key}
              type="button"
              aria-pressed={filter === item.key}
              aria-label={`筛选${item.label}`}
              onClick={() => setFilter(item.key)}
              className={[
                'flex items-baseline gap-1.5 border px-2 py-[3px] text-[13px] transition-colors',
                filter === item.key
                  ? 'border-accent/50 text-accent'
                  : 'border-line text-fg-muted hover:text-fg',
              ].join(' ')}
            >
              <span>{item.label}</span>
              <span className="num text-fg">{counts[item.key]}</span>
            </button>
          ))}
        </div>
        <label className="ml-auto flex items-center gap-1.5 text-[12px] text-fg-dim">
          排序
          <select
            value={sortKey}
            onChange={(event) => setSortKey(event.target.value as SortKey)}
            className="border border-line bg-ink-900 px-1.5 py-[2px] text-[12px] text-fg outline-none focus:border-fg-dim"
          >
            <option value="default">状态 · 昨日板高</option>
            <option value="pct">今日涨幅</option>
            <option value="level">昨日板高</option>
          </select>
        </label>
      </div>

      <div className="overflow-auto">
        <table className="grid-table">
          <thead>
            <tr>
              <th className="text-left">结果</th>
              <th className="text-left">股票</th>
              <th>昨日板高</th>
              <th>今日连板</th>
              <th>今日涨幅</th>
              <th>收盘</th>
              <th className="text-left">题材</th>
              <th>封单</th>
              <th>首封</th>
              <th>开板</th>
              <th className="text-left">原因 / 说明</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((stock) => (
              <tr key={stock.code}>
                <td className={`text-left ${STATUS_CLASS[stock.status]}`}>
                  {STATUS_LABEL[stock.status]}
                </td>
                <td className="text-left">
                  <StockLink code={stock.code}>{stock.name ?? stock.code}</StockLink>
                  <span className="num ml-1.5 text-[12px] text-fg-dim">{stock.code}</span>
                  {stock.industry && (
                    <span className="ml-1.5 text-[12px] text-fg-dim">{stock.industry}</span>
                  )}
                </td>
                <td className="num">{stock.previous_level}</td>
                <td className="num">{fmtInt(stock.today_consecutive)}</td>
                <td className={`num ${toneOf(stock.pct_chg)}`}>{fmtPct(stock.pct_chg)}</td>
                <td className="num">{fmtNum(stock.close, 2)}</td>
                <td className="text-left text-fg-muted">{stock.board ?? '—'}</td>
                <td className="num">{fmtInt(stock.seal_amount)}</td>
                <td className="num">{fmtSealTime(stock.first_seal_time)}</td>
                <td className="num">{fmtInt(stock.open_times)}</td>
                <td className="max-w-[280px] text-left text-[12px] text-fg-dim">
                  {stock.reason_text ?? stock.reason_code ?? '—'}
                </td>
              </tr>
            ))}
            {visible.length === 0 && (
              <tr>
                <td colSpan={11} className="py-8 text-center text-[13px] text-fg-dim">
                  {stocks.length === 0 ? '该档没有可观察的股票' : '当前筛选下没有股票'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
