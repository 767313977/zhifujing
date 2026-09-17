import type { LhbItem } from '../api/types'
import { fmtAmount, fmtNum, fmtPct, toneOf } from '../lib/format'
import Panel from './Panel'

interface LhbTableProps {
  items: LhbItem[]
  delay?: number
}

export default function LhbTable({ items, delay = 340 }: LhbTableProps) {
  return (
    <Panel
      title="龙虎榜"
      meta={
        <span className="num">
          {items.length} 条
          {/* 同一股票可能因多条上榜原因重复出现，提前说明避免误读 */}
          {items.length > 0 && <span className="ml-2 text-fg-dim">同股多原因会重复</span>}
        </span>
      }
      delay={delay}
    >
      {items.length === 0 ? (
        <div className="px-4 py-8 text-center text-[13px] text-fg-dim">当日无数据</div>
      ) : (
        <div className="max-h-[440px] overflow-auto">
          <table className="grid-table">
            <thead>
              <tr>
                <th>代码</th>
                <th className="!text-left">名称</th>
                <th>涨跌幅</th>
                <th>收盘价</th>
                <th>净买额</th>
                <th>买入额</th>
                <th>卖出额</th>
                <th className="!text-left">上榜原因</th>
                <th className="!text-left">解读</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item, index) => (
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
                    <span className="num text-fg-muted">{item.code}</span>
                  </td>
                  <td className="!text-left">
                    <span className="text-fg">{item.name ?? '—'}</span>
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
                    <span className="inline-block max-w-[140px] truncate align-bottom text-[11px] text-fg-muted">
                      {item.reason}
                    </span>
                  </td>
                  <td className="!text-left">
                    <span className="inline-block max-w-[120px] truncate align-bottom text-[11px] text-fg-dim">
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
