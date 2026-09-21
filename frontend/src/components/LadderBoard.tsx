import type { LadderLevel, LimitStock } from '../api/types'
import { fmtSealTime } from '../lib/format'
import Panel from './Panel'
import StockLink from './StockLink'

interface LadderBoardProps {
  ladder: LadderLevel[]
  total: number
  delay?: number
  /** 首屏取数期间为真。不加这个的话，空梯队会被显示成「当日无涨停」 */
  loading?: boolean
}

/** 梯队越高越醒目：5 板以上用金色强调，这是市场的「高度」。 */
function tierColor(level: number): string {
  if (level >= 5) return 'var(--color-accent)'
  if (level >= 3) return 'var(--color-up)'
  if (level === 2) return 'color-mix(in srgb, var(--color-up) 62%, transparent)'
  return 'color-mix(in srgb, var(--color-up) 32%, transparent)'
}

function tierText(level: number): string {
  if (level >= 5) return 'text-accent'
  if (level >= 3) return 'text-up'
  return 'text-fg-muted'
}

function StockChip({ stock }: { stock: LimitStock }) {
  const reopened = (stock.open_times ?? 0) > 0
  const title = [
    `${stock.name} ${stock.code}`,
    stock.industry ? `行业 ${stock.industry}` : null,
    `首次封板 ${fmtSealTime(stock.first_seal_time)}`,
    `最后封板 ${fmtSealTime(stock.last_seal_time)}`,
    `开板 ${stock.open_times ?? 0} 次`,
    stock.seal_amount != null ? `封单 ${(stock.seal_amount / 1e8).toFixed(2)}亿` : null,
    stock.turnover != null ? `换手 ${stock.turnover.toFixed(2)}%` : null,
  ]
    .filter(Boolean)
    .join(' · ')

  return (
    /* chip 本身就是入口：整块可点，进个股页看日K。
       tooltip 挂在链接上，鼠标停在 chip 上照样能看到封板细节 */
    <StockLink
      code={stock.code}
      title={title}
      className="inline-flex cursor-pointer items-baseline gap-1.5 border border-line-soft bg-ink-850 px-1.5 py-[3px] text-[12px] hover:border-line hover:bg-ink-700"
    >
      <span className="text-fg">{stock.name ?? stock.code}</span>
      {stock.industry && (
        <span className="text-[10px] text-fg-dim">{stock.industry}</span>
      )}
      {/* 开板过的标记出来：同样的板高，含金量不同 */}
      {reopened && (
        <span
          className="num text-[10px] text-fg-dim"
          title={`开板 ${stock.open_times} 次`}
        >
          ↩{stock.open_times}
        </span>
      )}
    </StockLink>
  )
}

/**
 * 涨停梯队。
 *
 * 左侧连板高度、中间家数条、右侧个股。
 * 家数条宽度按最大家数归一 —— 多板高标少、首板多，形状自然收缩成塔，
 * 一眼就能判断市场是「有高度没厚度」还是「高低都有」。
 */
export default function LadderBoard({
  ladder,
  total,
  delay = 200,
  loading = false,
}: LadderBoardProps) {
  if (loading) {
    return (
      <Panel title="涨停梯队" delay={delay}>
        <div className="px-4 py-8 text-center text-[13px] text-fg-dim">加载中…</div>
      </Panel>
    )
  }

  if (ladder.length === 0) {
    return (
      <Panel title="涨停梯队" delay={delay}>
        <div className="px-4 py-8 text-center text-[13px] text-fg-dim">
          当日无涨停
        </div>
      </Panel>
    )
  }

  const maxCount = Math.max(...ladder.map((level) => level.count), 1)

  return (
    <Panel
      title="涨停梯队"
      meta={<span className="num">共 {total} 只 · {ladder.length} 层</span>}
      delay={delay}
    >
      {/* 表头：只为对齐，视觉上极轻 */}
      <div className="flex items-center gap-4 border-b border-line-soft bg-ink-850/60 px-4 py-1.5 text-[10px] tracking-[0.1em] text-fg-dim">
        <span className="w-14 shrink-0">高度</span>
        <span className="w-28 shrink-0">家数</span>
        <span>个股</span>
      </div>

      {ladder.map((level, i) => (
        <div
          key={level.consecutive}
          className="flex items-start gap-4 border-b border-line-soft px-4 py-2.5 transition-colors last:border-b-0 hover:bg-ink-850/50"
        >
          <div className="flex w-14 shrink-0 items-baseline gap-0.5 pt-0.5">
            <span
              className={`num text-[26px] leading-none font-semibold ${tierText(level.consecutive)}`}
            >
              {level.consecutive}
            </span>
            <span className="text-[10px] text-fg-dim">板</span>
          </div>

          <div className="flex w-28 shrink-0 items-center gap-2 pt-2">
            <span className="num w-6 shrink-0 text-right text-[13px] text-fg">
              {level.count}
            </span>
            <div className="h-[6px] flex-1 bg-ink-800">
              <div
                className="wipe h-full"
                style={{
                  width: `${(level.count / maxCount) * 100}%`,
                  backgroundColor: tierColor(level.consecutive),
                  animationDelay: `${delay + 160 + i * 70}ms`,
                }}
              />
            </div>
          </div>

          <div className="flex min-w-0 flex-wrap gap-1.5">
            {level.stocks.map((stock) => (
              <StockChip key={stock.code} stock={stock} />
            ))}
          </div>
        </div>
      ))}
    </Panel>
  )
}
