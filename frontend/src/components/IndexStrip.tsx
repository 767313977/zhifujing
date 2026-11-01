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
 * 主指数（规范 v1 §5，Phase 4）：这四个用大卡片展示，其余指数收进下方的窄条。
 *
 * 为什么是这四个：上证 / 深证 / 创业板是「大盘在不在涨」的默认参照，
 * 沪深300 是机构与期指的基准。其余（上证50 / 科创50 / 中证500/1000/2000…）
 * 是风格与宽度的补充信息，排在第二行就够，不需要占同样大的面积。
 *
 * 顺序即展示顺序；某个指数库里没有（采集缺失）时跳过，不留空格。
 */
const PRIMARY_CODES = ['000001.SH', '399001.SZ', '399006.SZ', '000300.SH']

/**
 * 均线得失的一小格：`MA5 上` / `MA20 下`。
 *
 * **把「上/下」写进文字**，而不是只靠颜色区分：同一格里已经有当日涨跌在染色，
 * 再让颜色承担第二种含义，就会出现「红色到底指涨还是指跌 / 站上均线」的歧义。
 * 颜色只做强化，文字自己就能读懂。
 */
function MaTag({ window, above }: { window: number; above: boolean | null }) {
  if (above == null) {
    return <span className="num text-fg-dim">MA{window} —</span>
  }
  return (
    <span className={`num ${above ? 'text-up' : 'text-down'}`}>
      MA{window} {above ? '上' : '下'}
    </span>
  )
}

/** 主指数大卡：收盘 + 涨跌幅 + 成交额/家数 + 均线量价。 */
function PrimaryCard({ item, index }: { item: IndexQuote; index: number }) {
  return (
    <div
      className="relative -mr-px -mb-px border-r border-b border-line-soft px-4 py-3"
      style={{ animationDelay: `${60 + index * 45}ms` }}
    >
      <span
        className="absolute inset-y-3 left-0 w-[2px]"
        style={{ backgroundColor: toneColor(item.pct_chg) }}
      />

      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[13px] text-fg-muted">{item.name ?? item.code}</span>
        <span className="num text-[12px] text-fg-dim">{item.code}</span>
      </div>

      <div className="mt-1.5 flex items-baseline gap-2.5">
        <span
          className={`num text-[22px] leading-none font-semibold ${toneOf(item.pct_chg)}`}
        >
          {fmtNum(item.close, 2)}
        </span>
        <span className={`num text-[14px] ${toneOf(item.pct_chg)}`}>
          {fmtPct(item.pct_chg)}
        </span>
      </div>

      <div className="num mt-2 flex items-center gap-3 text-[13px] text-fg-dim">
        <span>额 {fmtAmount(item.amount)}</span>
        {/* 涨跌家数只有部分指数有（中证1000 等 iFinD 不返回）。**没有就整段不渲染** ——
            常驻一个「家数 —」只是占位噪音；有数的指数（上证/深证/创业板）照常显示。
            2026-11-01 按规范 v1 微调（原来固定显示「家数 —」+ tooltip 说明原因）。 */}
        {item.up_count != null && item.down_count != null && (
          <span>
            <span className="text-up">{item.up_count}</span>
            <span className="mx-0.5">/</span>
            <span className="text-down">{item.down_count}</span>
          </span>
        )}
      </div>

      {/* 均线得失与量价配合：这一行是「今天这根怎么走出来的」 */}
      <div className="num mt-1.5 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[13px]">
        <MaTag window={5} above={item.above_ma5} />
        <MaTag window={20} above={item.above_ma20} />
        {item.vol_price ? (
          <span
            className={`border border-line-soft px-1 ${toneOf(item.pct_chg)}`}
            // 量比的数值放在 tooltip：格子窄，写「放量上涨 1.42」会挤掉均线
            title={
              item.vol_ratio != null
                ? `量比 ${item.vol_ratio.toFixed(2)}（今日成交额 / 前 5 日均额）`
                : undefined
            }
          >
            {item.vol_price}
          </span>
        ) : (
          <span className="text-fg-dim" title="当日量比缺失，无法判定量价配合">
            量价 —
          </span>
        )}
      </div>
    </div>
  )
}

/** 次指数窄条：只留名称 + 涨跌幅（点位放 tooltip），一行铺满。 */
function SecondaryRow({ items }: { items: IndexQuote[] }) {
  return (
    <div className="grid grid-cols-2 border-t border-line-soft sm:grid-cols-3 xl:grid-cols-6">
      {items.map((item) => (
        <div
          key={item.code}
          className="-mr-px -mb-px flex items-baseline justify-between gap-2 border-r border-b border-line-soft px-3 py-1.5"
          title={`${item.name ?? item.code} 收盘 ${fmtNum(item.close, 2)}（${fmtPct(item.pct_chg)}）`}
        >
          <span className="truncate text-[12px] text-fg-muted">
            {item.name ?? item.code}
          </span>
          <span className={`num text-[12px] ${toneOf(item.pct_chg)}`}>
            {fmtPct(item.pct_chg)}
          </span>
        </div>
      ))}
    </div>
  )
}

/**
 * 指数条（规范 v1 §5 Phase 4：主次分层）。
 *
 * - **主指数**（上证/深证/创业板/沪深300）大卡：点位、涨跌幅、成交额、家数、均线量价；
 * - **次指数**（其余宽基/风格）窄条：只有名称与涨跌幅 —— 它们是「风格怎么切」的旁证，
 *   不该和三大指数抢同样的视觉面积。点位与收盘价放进 tooltip，不丢信息。
 *
 * 分隔线仍用「每格画右边与下边 + 负边距 + 容器裁切」实现，换行成几列外沿都不会多线。
 */
export default function IndexStrip({ indexes, tradeDate }: IndexStripProps) {
  if (indexes.length === 0) {
    return (
      <div className="panel px-4 py-6 text-center text-[14px] text-fg-dim">
        {tradeDate} 无指数数据
      </div>
    )
  }

  const primary = PRIMARY_CODES.map((code) =>
    indexes.find((item) => item.code === code),
  ).filter((item): item is IndexQuote => item != null)
  // 次指数保持后端给的顺序（`api/market.INDEX_ORDER`），不再重排
  const secondary = indexes.filter((item) => !PRIMARY_CODES.includes(item.code))
  // 库里只有次指数（某个主指数采集缺失）时，退化成原来的平铺，避免整块空白
  const head = primary.length > 0 ? primary : secondary
  const tail = primary.length > 0 ? secondary : []

  return (
    <div className="panel rise overflow-hidden">
      <div className="grid grid-cols-2 md:grid-cols-4">
        {head.map((item, index) => (
          <PrimaryCard key={item.code} item={item} index={index} />
        ))}
      </div>
      {tail.length > 0 && <SecondaryRow items={tail} />}
    </div>
  )
}
