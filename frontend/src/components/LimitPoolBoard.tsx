import type { LadderBoard, LadderBoardStock } from '../api/types'
import { fmtNum, fmtPct, fmtSealTime, toneOf } from '../lib/format'
import Panel from './Panel'
import StockLink from './StockLink'

interface LimitPoolBoardProps {
  /** 首屏取数期间 / 取数失败回落时为 null */
  data: LadderBoard | null
  /** 面板数据对应的交易日（= 页面所选日期）。缺省回落 `data.trade_date` */
  tradeDate?: string | null
  /** 翻到上一 / 下一交易日。不给就禁用那一侧（已到最早 / 最晚就是 undefined） */
  onPrev?: () => void
  onNext?: () => void
  delay?: number
  /** 首屏取数期间为真。不加这个的话，空行会被显示成「暂无数据」 */
  loading?: boolean
}

/**
 * 交易所前缀。**依据是代码号段**（不是名字）：
 * 60x 沪主板、68x 科创板、00x 深主板、30x 创业板、4xx/8xx/92x 北交所，其余留空。
 */
function marketOf(code: string): string {
  if (code.startsWith('60')) return '沪'
  if (code.startsWith('68')) return '科'
  if (code.startsWith('00')) return '深'
  if (code.startsWith('30')) return '创'
  if (code.startsWith('4') || code.startsWith('8') || code.startsWith('92')) return '北'
  return ''
}

/** 状态标记的中文与配色。up 要醒目（红底），broken / other 逐级变暗。 */
const STATUS: Record<LadderBoardStock['status'], { label: string; cls: string }> = {
  up: { label: '涨停', cls: 'bg-up/15 text-up' },
  broken: { label: '炸板', cls: 'bg-ink-700 text-fg-muted' },
  other: { label: '未晋级', cls: 'bg-ink-800 text-fg-dim' },
}

/** 左侧「进度」列按档位配色：高档金色强调，首板最常见、弱化处理。 */
function levelText(level: number): string {
  if (level === 0) return 'text-fg-muted'
  if (level >= 5) return 'text-accent'
  if (level >= 3) return 'text-up'
  return 'text-fg-muted'
}

/** 晋级率先看绝对高低：过半算强（红），其余弱化。低晋级率是本池要传达的主要信号。 */
function rateText(rate: number | null): string {
  if (rate == null) return 'text-fg-dim'
  return rate >= 50 ? 'text-up' : 'text-fg-dim'
}

function StockCell({ stock }: { stock: LadderBoardStock }) {
  const status = STATUS[stock.status]
  // 题材：涨停原因首段（`房地产+城市更新` 取「房地产」），取不到再回落开盘啦板块。
  // 炸板 / 未晋级的票两者多半都是 null —— 那就什么也不渲染，不拿行业之类顶上（口径不同）。
  const theme = stock.reason?.split('+')[0] || stock.board || null
  // tooltip 只拼**非空**字段，照抄 LadderBoard 的 StockChip 拼法
  const title = [
    `${stock.name ?? ''} ${stock.code}`.trim(),
    status.label,
    stock.consecutive != null ? `${stock.consecutive}连板` : null,
    stock.first_seal_time ? `首次封板 ${fmtSealTime(stock.first_seal_time)}` : null,
    stock.seal_amount != null ? `封单 ${(stock.seal_amount / 1e8).toFixed(2)}亿` : null,
    stock.turnover != null ? `换手 ${stock.turnover.toFixed(2)}%` : null,
    stock.industry ? `同花顺行业 ${stock.industry}` : null,
    stock.board ? `开盘红板块 ${stock.board}` : null,
    stock.reason ? `涨停原因 ${stock.reason}` : null,
  ]
    .filter(Boolean)
    .join(' · ')

  return (
    /* 整格可点，进个股页 */
    <StockLink
      code={stock.code}
      title={title}
      className="flex min-w-0 items-center gap-1.5 px-1 py-[2px] text-[13px] hover:bg-ink-850/60"
    >
      {marketOf(stock.code) && (
        <span className="shrink-0 text-[11px] text-fg-dim">{marketOf(stock.code)}</span>
      )}
      <span className="truncate text-fg">{stock.name ?? stock.code}</span>
      {/* 状态是本面板的核心信息：晋级 / 炸板一眼可辨 */}
      <span className={`shrink-0 rounded-[2px] px-1 text-[11px] leading-[16px] ${status.cls}`}>
        {status.label}
      </span>
      <span className={`num shrink-0 text-[12px] ${toneOf(stock.pct_chg)}`}>
        ({fmtPct(stock.pct_chg)})
      </span>
      {theme && (
        <span className="min-w-0 flex-1 truncate text-[12px] text-fg-dim">{theme}</span>
      )}
    </StockLink>
  )
}

/**
 * 涨停股票池。
 *
 * 按**昨日**连板档分组（「8进9」「首板」），每档一行：左边档位、中间晋级率、
 * 右边这一档的**候选票**——行内同时含今日涨停（晋级）与今日炸板（失败），
 * 与「涨停梯队」只看今日涨停票不同，这里能看出「昨天那批票今天怎么样了」。
 *
 * 标题栏右侧的日期可以**前后翻交易日**（← →）：改的是**整页**的所选日期
 * （回调用 Dashboard 传进来，与顶部那个日期下拉是同一个状态，不另开一份）。
 */
export default function LimitPoolBoard({
  data,
  tradeDate = null,
  onPrev,
  onNext,
  delay = 200,
  loading = false,
}: LimitPoolBoardProps) {
  const rows = data?.rows ?? []
  // 只数 = 各行**实际列出的** stocks 之和，而不是 base 之和 ——
  // 首板行的 base 可能 >120（后端按行截断），拿它求和会显示一个屏幕上并不存在的只数。
  // 口径注释清楚：这是「本屏列出的只数」。
  const total = rows.reduce((sum, row) => sum + row.stocks.length, 0)
  const anchor = tradeDate ?? data?.trade_date ?? null

  // 翻页按钮：到头的那个 `disabled`（调用方不给回调就是到头）
  const navButton =
    'rounded-[2px] border border-line px-1.5 text-[13px] leading-[18px] text-fg-muted ' +
    'transition-colors enabled:hover:border-fg-dim enabled:hover:text-fg ' +
    'disabled:cursor-not-allowed disabled:opacity-30'

  return (
    <Panel
      title="涨停股票池"
      meta={
        <span className="flex items-center gap-2">
          <button
            type="button"
            onClick={onPrev}
            disabled={!onPrev}
            className={navButton}
            title="上一交易日"
          >
            ←
          </button>
          {anchor && (
            <span className="num">
              【{anchor}】
              {data ? `共 ${total} 只 · ${rows.length} 档` : ''}
            </span>
          )}
          <button
            type="button"
            onClick={onNext}
            disabled={!onNext}
            className={navButton}
            title="下一交易日"
          >
            →
          </button>
        </span>
      }
      delay={delay}
    >
      {loading ? (
        <div className="px-4 py-8 text-center text-[14px] text-fg-dim">加载中…</div>
      ) : !data || rows.length === 0 ? (
        <div className="px-4 py-8 text-center text-[14px] text-fg-dim">
          {data && data.prev_date == null ? '缺上一交易日涨停池，无法计算晋级' : '暂无数据'}
        </div>
      ) : (
        <>
          {/* 表头：只为对齐，视觉上极轻（照抄涨停梯队的表头样式） */}
          <div className="flex items-center gap-4 border-b border-line-soft bg-ink-850/60 px-4 py-1.5 text-[12px] tracking-[0.1em] text-fg-dim">
            <span className="w-14 shrink-0">进度</span>
            <span className="w-24 shrink-0">晋级率</span>
            <span>个股</span>
          </div>

          {rows.map((row) => (
            <div
              key={`${row.level}-${row.label}`}
              className="flex items-start gap-4 border-b border-line-soft px-4 py-2.5 transition-colors last:border-b-0 hover:bg-ink-850/50"
            >
              {/* 进度：3 个字（8进9 / 首板），w-14 刚好放得下 */}
              <div className={`w-14 shrink-0 pt-0.5 text-[16px] leading-none font-semibold ${levelText(row.level)}`}>
                {row.label}
              </div>

              {/* 晋级率：promoted/base=rate% */}
              <div className={`num w-24 shrink-0 pt-1 text-[14px] ${rateText(row.rate)}`}>
                {row.rate == null
                  ? '—'
                  : `${row.promoted}/${row.base}=${fmtNum(row.rate, 1)}%`}
              </div>

              {/* 个股：多列网格，限高滚动 —— 首板行可能上百只，不能把页面撑爆 */}
              <div className="min-w-0 flex-1 max-h-[640px] overflow-auto">
                <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 md:grid-cols-3 xl:grid-cols-4">
                  {row.stocks.map((stock) => (
                    <StockCell key={stock.code} stock={stock} />
                  ))}
                </div>
              </div>
            </div>
          ))}
        </>
      )}
    </Panel>
  )
}
