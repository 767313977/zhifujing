import type { ReactNode } from 'react'
import type { LimitStock, PoolType } from '../api/types'
import { fmtAmount, fmtInt, fmtNum, fmtPct, fmtSealTime, toneOf } from '../lib/format'
import Panel from './Panel'

interface LimitTableProps {
  type: PoolType
  stocks: LimitStock[]
  delay?: number
}

interface Column {
  key: string
  label: string
  /** 默认右对齐（数值列）；文字列显式左对齐 */
  align?: 'left' | 'right'
  render: (stock: LimitStock) => ReactNode
}

const TITLES: Record<PoolType, string> = {
  up: '涨停明细',
  down: '跌停明细',
  broken: '炸板明细',
}

/**
 * 三池的列不完全一样（跌停池是「封单资金/连续跌停/末封」，炸板池没有封板资金），
 * 这里按类型装配列，而不是硬套同一套表头。
 */
function buildColumns(type: PoolType): Column[] {
  const columns: Column[] = [
    {
      key: 'code',
      label: '代码',
      align: 'left',
      render: (s) => <span className="num text-fg-muted">{s.code}</span>,
    },
    {
      key: 'name',
      label: '名称',
      align: 'left',
      render: (s) => <span className="text-fg">{s.name ?? '—'}</span>,
    },
    {
      key: 'industry',
      label: '行业',
      align: 'left',
      render: (s) => <span className="text-[11px] text-fg-dim">{s.industry ?? '—'}</span>,
    },
    {
      key: 'pct',
      label: '涨跌幅',
      render: (s) => (
        <span className={`num ${toneOf(s.pct_chg)}`}>{fmtPct(s.pct_chg)}</span>
      ),
    },
    { key: 'price', label: '最新价', render: (s) => <span className="num">{fmtNum(s.price, 2)}</span> },
  ]

  if (type === 'up') {
    columns.push({
      key: 'consecutive',
      label: '连板',
      render: (s) => (
        <span className={`num ${(s.consecutive ?? 1) >= 3 ? 'text-accent' : 'text-fg'}`}>
          {fmtInt(s.consecutive)}
        </span>
      ),
    })
  }

  if (type !== 'broken') {
    columns.push({
      key: 'seal_amount',
      label: type === 'up' ? '封板资金' : '封单资金',
      render: (s) => <span className="num">{fmtAmount(s.seal_amount)}</span>,
    })
  }

  if (type === 'down') {
    columns.push({
      key: 'continuous',
      label: '连续跌停',
      render: (s) => <span className="num text-down">{fmtInt(s.consecutive)}</span>,
    })
    columns.push({
      key: 'last_seal',
      label: '末封',
      render: (s) => <span className="num text-fg-muted">{fmtSealTime(s.last_seal_time)}</span>,
    })
  } else {
    columns.push({
      key: 'first_seal',
      label: '首封',
      render: (s) => <span className="num text-fg-muted">{fmtSealTime(s.first_seal_time)}</span>,
    })
  }

  columns.push({
    key: 'open_times',
    label: type === 'down' ? '开板' : '炸板',
    render: (s) => (
      <span className={`num ${(s.open_times ?? 0) > 0 ? 'text-fg' : 'text-fg-dim'}`}>
        {fmtInt(s.open_times)}
      </span>
    ),
  })
  columns.push({
    key: 'turnover',
    label: '换手率',
    render: (s) => <span className="num">{fmtNum(s.turnover, 2, '%')}</span>,
  })
  columns.push({
    key: 'amount',
    label: '成交额',
    render: (s) => <span className="num text-fg-muted">{fmtAmount(s.amount)}</span>,
  })
  columns.push({
    key: 'float_mv',
    label: '流通市值',
    render: (s) => <span className="num text-fg-muted">{fmtAmount(s.float_mv)}</span>,
  })

  return columns
}

export default function LimitTable({ type, stocks, delay = 280 }: LimitTableProps) {
  const columns = buildColumns(type)

  return (
    <Panel
      title={TITLES[type]}
      meta={<span className="num">{stocks.length} 只</span>}
      delay={delay}
    >
      {stocks.length === 0 ? (
        <div className="px-4 py-8 text-center text-[13px] text-fg-dim">当日无数据</div>
      ) : (
        <div className="max-h-[440px] overflow-auto">
          <table className="grid-table">
            <thead>
              <tr>
                {columns.map((column) => (
                  <th key={column.key}>{column.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {stocks.map((stock) => (
                <tr key={stock.code}>
                  {columns.map((column) => (
                    <td
                      key={column.key}
                      className={column.align === 'left' ? '!text-left' : undefined}
                    >
                      {column.render(stock)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}
