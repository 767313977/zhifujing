import type { WatchlistRow } from '../api/types'
import { fmtNum, fmtPct, toneOf } from '../lib/format'
import { useSort } from '../lib/sort'
import type { SortSpecs } from '../lib/sort'
import { rememberStockList } from '../lib/stockNav'
import Panel from './Panel'
import SortTh from './SortTh'
import StockLink from './StockLink'

interface WatchlistPanelProps {
  rows: WatchlistRow[]
  delay?: number
}

const WATCH_SORTS: SortSpecs<WatchlistRow> = {
  code: { value: (row) => row.code, first: 'asc' },
  name: { value: (row) => row.name, first: 'asc' },
  close: { value: (row) => row.close },
  pct_chg: { value: (row) => row.pct_chg },
}

/**
 * 首页自选股表现。
 *
 * 放在首页最靠后的复盘内容里：先看大盘与方向（指数 / 情绪 / 板块 / 涨停），
 * 再回到自己的票，最后写笔记 —— 个人持仓放在最前面会把大盘视野挤掉。
 * 备注不在这里显示，编辑去自选股页，首页只回答「今天我的票怎么样」。
 */
export default function WatchlistPanel({ rows, delay = 0 }: WatchlistPanelProps) {
  // 首屏不排，保持后端顺序（按代码）；点列头才排
  const [sort, shown] = useSort(rows, WATCH_SORTS, { key: null })
  const latest = rows.find((row) => row.latest_date)?.latest_date ?? '—'
  const rising = rows.filter((row) => (row.pct_chg ?? 0) > 0).length
  const falling = rows.filter((row) => (row.pct_chg ?? 0) < 0).length

  return (
    <Panel
      title="自选股表现"
      meta={
        <span className="num">
          {rows.length > 0 ? `${rows.length} 只 · ${rising} 涨 / ${falling} 跌 · ${latest}` : '—'}
        </span>
      }
      delay={delay}
    >
      {rows.length === 0 ? (
        <div className="px-4 py-8 text-center text-[13px] text-fg-dim">
          还没有自选股，在个股详情页点「加入自选」
        </div>
      ) : (
        <div className="max-h-[300px] overflow-auto">
          <table className="grid-table">
            <thead>
              <tr>
                <SortTh sortKey="code" align="left" {...sort}>代码</SortTh>
                <SortTh sortKey="name" align="left" {...sort}>名称</SortTh>
                <SortTh sortKey="close" {...sort}>最新价</SortTh>
                <SortTh sortKey="pct_chg" {...sort}>涨跌幅</SortTh>
              </tr>
            </thead>
            <tbody>
              {shown.map((row) => (
                <tr
                  key={row.code}
                  onClick={() => {
                    // 点行时把自选列表存下，个股页就能 ← → 前后翻
                    rememberStockList(shown.map((item) => item.code))
                  }}
                >
                  <td className="!text-left">
                    <StockLink code={row.code} className="num text-fg-muted">
                      {row.code}
                    </StockLink>
                  </td>
                  <td className="!text-left">
                    <StockLink code={row.code}>{row.name ?? row.code}</StockLink>
                  </td>
                  <td>
                    <span className={`num ${toneOf(row.pct_chg)}`}>
                      {fmtNum(row.close, 2)}
                    </span>
                  </td>
                  <td>
                    <span className={`num ${toneOf(row.pct_chg)}`}>{fmtPct(row.pct_chg)}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}
