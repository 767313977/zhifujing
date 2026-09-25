import type { LhbItem } from '../api/types'
import { fmtAmount, fmtNum, fmtPct, toneOf } from '../lib/format'
import { useSort } from '../lib/sort'
import type { SortSpecs } from '../lib/sort'
import Panel from './Panel'
import SortTh from './SortTh'
import StockLink from './StockLink'

interface LhbTableProps {
  items: LhbItem[]
  /** 切日期取数期间为真，空列表会显示成「当日无数据」而非「加载中」 */
  loading?: boolean
  delay?: number
}

/** 先按净买额看谁被抢、再按买卖额看谁换手最大，最后才是文字列 */
const LHB_SORTS: SortSpecs<LhbItem> = {
  code: { value: (item) => item.code, first: 'asc' },
  name: { value: (item) => item.name, first: 'asc' },
  pct_chg: { value: (item) => item.pct_chg },
  close: { value: (item) => item.close },
  net_buy: { value: (item) => item.net_buy },
  buy_amount: { value: (item) => item.buy_amount },
  sell_amount: { value: (item) => item.sell_amount },
  reason: { value: (item) => item.reason, first: 'asc' },
  interpretation: { value: (item) => item.interpretation, first: 'asc' },
}

export default function LhbTable({ items, loading = false, delay = 340 }: LhbTableProps) {
  // 首屏不排：后端已经是「净买额从大到小」，不必再排一遍
  const [sort, shown] = useSort(items, LHB_SORTS, { key: null })

  return (
    <Panel
      title="龙虎榜"
      meta={
        <span className="num">
          {loading ? '加载中…' : `${items.length} 条`}
          {/* 同一股票可能因多条上榜原因重复出现，提前说明避免误读 */}
          {!loading && items.length > 0 && (
            <span className="ml-2 text-fg-dim">同股多原因会重复 · 点列头排序</span>
          )}
        </span>
      }
      delay={delay}
    >
      {loading ? (
        <div className="px-4 py-8 text-center text-[14px] text-fg-dim">加载中…</div>
      ) : items.length === 0 ? (
        <div className="px-4 py-8 text-center text-[14px] text-fg-dim">当日无数据</div>
      ) : (
        <div className="max-h-[440px] overflow-auto">
          <table className="grid-table">
            <thead>
              <tr>
                <SortTh sortKey="code" {...sort}>代码</SortTh>
                <SortTh sortKey="name" align="left" {...sort}>名称</SortTh>
                <SortTh sortKey="pct_chg" {...sort}>涨跌幅</SortTh>
                <SortTh sortKey="close" {...sort}>收盘价</SortTh>
                <SortTh sortKey="net_buy" {...sort}>净买额</SortTh>
                <SortTh sortKey="buy_amount" {...sort}>买入额</SortTh>
                <SortTh sortKey="sell_amount" {...sort}>卖出额</SortTh>
                <SortTh sortKey="reason" align="left" {...sort}>上榜原因</SortTh>
                <SortTh sortKey="interpretation" align="left" {...sort}>解读</SortTh>
              </tr>
            </thead>
            <tbody>
              {shown.map((item, index) => (
                <tr
                  key={`${item.code}-${item.reason}-${index}`}
                  // 上榜原因与解读被截断，完整内容放在行 tooltip 里，信息不丢
                  title={[
                    `${item.name ?? ''} ${item.code}`,
                    `上榜原因：${item.reason}`,
                    item.interpretation ? `解读：${item.interpretation}` : null,
                    `净买额 ${fmtAmount(item.net_buy)}`,
                    `买入额 ${fmtAmount(item.buy_amount)}`,
                    `卖出额 ${fmtAmount(item.sell_amount)}`,
                  ]
                    .filter(Boolean)
                    .join('\n')}
                >
                  <td>
                    <StockLink code={item.code} className="num text-fg-muted">
                      {item.code}
                    </StockLink>
                  </td>
                  <td className="!text-left">
                    <StockLink code={item.code}>{item.name ?? item.code}</StockLink>
                  </td>
                  <td>
                    <span className={`num ${toneOf(item.pct_chg)}`}>
                      {fmtPct(item.pct_chg)}
                    </span>
                  </td>
                  <td>
                    <span className="num">{fmtNum(item.close, 2)}</span>
                  </td>
                  <td>
                    <span className={`num font-medium ${toneOf(item.net_buy)}`}>
                      {fmtAmount(item.net_buy)}
                    </span>
                  </td>
                  <td>
                    <span className="num text-fg-muted">{fmtAmount(item.buy_amount)}</span>
                  </td>
                  <td>
                    <span className="num text-fg-muted">{fmtAmount(item.sell_amount)}</span>
                  </td>
                  <td className="!text-left">
                    <span className="inline-block max-w-[140px] truncate align-bottom text-[13px] text-fg-muted">
                      {item.reason}
                    </span>
                  </td>
                  <td className="!text-left">
                    <span className="inline-block max-w-[120px] truncate align-bottom text-[13px] text-fg-dim">
                      {item.interpretation ?? '—'}
                    </span>
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
