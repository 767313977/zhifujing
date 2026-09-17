import type { IndexQuote } from '../api/types'
import { fmtAmount, fmtNum, fmtPct, toneOf } from '../lib/format'

interface IndexStripProps {
  indexes: IndexQuote[]
  tradeDate: string
}

/** 方向色：0 与空值走中性色，不误染色。 */
function toneColor(value: number | null | undefined): string {
  if (value == null || value === 0) return 'var(--color-fg-dim)'
  return value > 0 ? 'var(--color-up)' : 'var(--color-down)'
}

/**
 * 指数条：核心指数并排，靠发丝线分隔而非卡片间距。
 * 左侧 2px 色条是唯一的方向提示，避免整块染色的噪音。
 *
 * 分隔线用「每格画右边与下边 + 负边距 + 容器裁切」实现，
 * 这样无论换行成几列，外沿都不会多出一条线。
 */
export default function IndexStrip({ indexes, tradeDate }: IndexStripProps) {
  if (indexes.length === 0) {
    return (
      <div className="panel px-4 py-6 text-center text-[13px] text-fg-dim">
        {tradeDate} 无指数数据
      </div>
    )
  }

  return (
    <div className="panel rise grid grid-cols-2 overflow-hidden md:grid-cols-3 xl:grid-cols-5">
      {indexes.map((item, i) => (
        <div
          key={item.code}
          className="relative -mr-px -mb-px border-r border-b border-line-soft px-4 py-3"
          style={{ animationDelay: `${60 + i * 45}ms` }}
        >
          <span
            className="absolute inset-y-3 left-0 w-[2px]"
            style={{ backgroundColor: toneColor(item.pct_chg) }}
          />

          <div className="flex items-baseline justify-between gap-2">
            <span className="text-[12px] text-fg-muted">{item.name ?? item.code}</span>
            <span className="num text-[10px] text-fg-dim">{item.code}</span>
          </div>

          <div className="mt-1.5 flex items-baseline gap-2.5">
            <span
              className={`num text-[22px] leading-none font-medium ${toneOf(item.pct_chg)}`}
            >
              {fmtNum(item.close, 2)}
            </span>
            <span className={`num text-[13px] ${toneOf(item.pct_chg)}`}>
              {fmtPct(item.pct_chg)}
            </span>
          </div>

          <div className="num mt-2 flex items-center gap-3 text-[11px] text-fg-dim">
            <span>额 {fmtAmount(item.amount)}</span>
            {item.up_count != null && item.down_count != null ? (
              <span>
                <span className="text-up">{item.up_count}</span>
                <span className="mx-0.5">/</span>
                <span className="text-down">{item.down_count}</span>
              </span>
            ) : (
              // 中证1000 等指数的涨跌家数 iFinD 返回 null，如实留空而非填 0
              <span className="text-fg-dim">家数 n/a</span>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}
