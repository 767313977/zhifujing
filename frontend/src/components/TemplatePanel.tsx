import type { TemplateBoard, TemplateItem, TemplateNextResult } from '../api/types'
import { fmtAmount, fmtNum, fmtPct, fmtShortDate, toneOf } from '../lib/format'
import { useSort } from '../lib/sort'
import type { SortSpecs } from '../lib/sort'
import Panel from './Panel'
import SortTh from './SortTh'
import StockLink from './StockLink'

/**
 * 各列的排序口径。
 *
 * 默认按量比降序（与后端给的顺序一致）：这套口径的第一道闸门是「有量」，
 * 「明天先看哪几只」按量能排最符合用法。
 */
const SORTS: SortSpecs<TemplateItem> = {
  code: { value: (row) => row.code, first: 'asc' },
  trigger: { value: (row) => row.trigger },
  floor: { value: (row) => row.floor },
  close: { value: (row) => row.close },
  pct_chg: { value: (row) => row.pct_chg },
  surge: { value: (row) => row.surge },
  vol_ratio: { value: (row) => row.vol_ratio },
  close_pos: { value: (row) => row.close_pos },
  amount: { value: (row) => row.amount },
}

/**
 * 次日结论的文案与配色。
 *
 * 「过线站住」走 `text-up`（A 股惯例：红是好的那一头），没站住与没过线走中性色 ——
 * 它们不是「跌」，只是这套口径没触发，染成绿色会让人误以为在说亏钱。
 */
const NEXT_LABEL: Record<TemplateNextResult, [string, string]> = {
  started: ['过线站住', 'text-up'],
  weak: ['过线没站住', 'text-fg-muted'],
  missed: ['未过线', 'text-fg-dim'],
}

interface TemplatePanelProps {
  board: TemplateBoard | null
  loading: boolean
  /** 正在看图的那只票（与形态命中列表共用同一个选中态） */
  active: string | null
  onSelect: (code: string) => void
  /** 首屏错峰浮现的延迟（毫秒） */
  delay?: number
}

/**
 * 样板池面板 —— 量价结构选股的**第一天**（「明天盯」清单）。
 *
 * 这是全站唯一一张「收盘后决定、次日盘中执行」的表，所以口径说明不能省：
 * 触发价要不要越过、兜底线是什么、哪些确认**不在这张表里**，都得写在表上方。
 */
export default function TemplatePanel({
  board,
  loading,
  active,
  onSelect,
  delay = 0,
}: TemplatePanelProps) {
  const items = board?.items ?? []
  const [sort, shown] = useSort(items, SORTS, { key: 'vol_ratio' })

  // 次日结论的家数。它本身就是这套口径最有用的一个数字：56 只样板里有多少
  // 真的过线站住了 —— 不给出来，用户只会记住挑中的那几只
  const tally = items.reduce<Record<string, number>>((acc, row) => {
    if (row.next_result) acc[row.next_result] = (acc[row.next_result] ?? 0) + 1
    return acc
  }, {})

  const nextDate = board?.next_date ?? null
  const meta = (
    <span className="num">
      {board?.trade_date ? `样板日 ${board.trade_date} · ` : ''}
      {loading ? '加载中…' : `${items.length} 只`}
      {nextDate ? (
        <span className="ml-3 text-fg-dim">
          次日 {fmtShortDate(nextDate)}：{tally.started ?? 0} 站住 / {tally.weak ?? 0} 没站住 /{' '}
          {tally.missed ?? 0} 未过线
        </span>
      ) : (
        items.length > 0 && <span className="ml-3 text-fg-dim">次日结论要等下一个交易日的日线</span>
      )}
    </span>
  )

  return (
    <Panel title="样板池 · 明天盯" meta={meta} delay={delay}>
      <div className="border-b border-line-soft px-4 py-2.5 text-[12px] leading-relaxed text-fg-dim">
        口径：最高价对<b className="text-fg-muted">昨收</b>冲高 ≥ 7% · 量比（当日量 ÷ 前 20
        日均量）≥ 1.3 · 收盘位置 ≤ 0.7（<b className="text-fg-muted">离开最高</b>，不封死单边）；
        排除收盘涨幅 ≥ 12% / 冲高 ≥ 18% / 跌幅 &lt; -3%。
        <span className="ml-1 text-fg-muted">
          触发价 = 今高，兜底线 = 触发价 × 0.98（次日收盘站上它才算站住）。
        </span>
        <span className="ml-1">
          次日结论只判日线能判的三条（过线 / 站上兜底线 / 收红）——
          <b className="text-fg-muted">量比 ≥ 1.5 不在这里</b>
          ：它是全天量口径，而买点在盘中刚过线那一刻，这条确认只能在盘中做。
        </span>
      </div>

      {items.length === 0 ? (
        <div className="px-4 py-6 text-center text-[12px] text-fg-dim">
          {loading
            ? '加载中…'
            : '这个交易日没有样板记录：它由收盘后的采集链扫描写入（零配额、纯本地日线），只有扫过的交易日才有'}
        </div>
      ) : (
        <div className="max-h-[420px] overflow-auto">
          <table className="grid-table">
            <thead>
              <tr>
                <SortTh sortKey="code" {...sort} align="left">
                  代码 / 名称
                </SortTh>
                <SortTh sortKey="trigger" {...sort} title="今高：次日盘中越过它才谈买点">
                  触发价
                </SortTh>
                <SortTh sortKey="floor" {...sort} title="触发价 × 0.98：次日收盘站上它才算站住">
                  兜底线
                </SortTh>
                <SortTh sortKey="close" {...sort}>
                  收盘
                </SortTh>
                <SortTh sortKey="pct_chg" {...sort}>
                  涨跌幅
                </SortTh>
                <SortTh sortKey="surge" {...sort} title="最高价相对昨收的涨幅（不是收盘涨幅）">
                  冲高
                </SortTh>
                <SortTh sortKey="vol_ratio" {...sort} title="当日成交量 ÷ 前 20 个交易日平均量">
                  量比
                </SortTh>
                <SortTh
                  sortKey="close_pos"
                  {...sort}
                  title="收盘在当日区间里的位置：0 = 收在最低，1 = 收在最高。≤ 0.7 才算离开最高"
                >
                  收盘位置
                </SortTh>
                <SortTh sortKey="amount" {...sort}>
                  成交额
                </SortTh>
                {/* 「次日结果」是三种状态，没有可比的大小，不排序 */}
                <th
                  className="!text-left"
                  title={
                    nextDate
                      ? `次日（${nextDate}）的日线结论：过线 = 次日最高价 > 触发价`
                      : '次日的日线结论，要等下一个交易日的数据'
                  }
                >
                  次日结果
                </th>
              </tr>
            </thead>
            <tbody>
              {shown.map((row) => {
                const next = row.next_result ? NEXT_LABEL[row.next_result] : null
                return (
                  <tr
                    key={row.code}
                    onClick={() => onSelect(row.code)}
                    className={`cursor-pointer ${row.code === active ? 'bg-accent/10' : ''}`}
                  >
                    <td className="!text-left">
                      {/* 这一层不加 onClick：让 Link 自己完成导航，
                          记住「正在看图的那只票」交给冒泡到行上的 onSelect */}
                      <StockLink code={row.code} className="num text-fg-dim">
                        {row.code}
                      </StockLink>
                      <span className="ml-2 text-fg">{row.name ?? '—'}</span>
                    </td>
                    <td>
                      <span className="num font-medium text-accent">
                        {fmtNum(row.trigger, 2)}
                      </span>
                    </td>
                    <td>
                      <span className="num text-fg-muted">{fmtNum(row.floor, 2)}</span>
                    </td>
                    <td>
                      <span className="num text-fg-muted">{fmtNum(row.close, 2)}</span>
                    </td>
                    <td>
                      <span className={`num ${toneOf(row.pct_chg)}`}>{fmtPct(row.pct_chg)}</span>
                    </td>
                    <td>
                      <span className="num text-fg-muted">
                        {row.surge == null ? '—' : fmtPct(row.surge * 100)}
                      </span>
                    </td>
                    <td>
                      <span className="num">{fmtNum(row.vol_ratio, 2)}</span>
                    </td>
                    <td>
                      <span className="num text-fg-dim">{fmtNum(row.close_pos, 2)}</span>
                    </td>
                    <td>
                      <span className="num text-fg-dim">{fmtAmount(row.amount)}</span>
                    </td>
                    <td className="!text-left">
                      {next ? (
                        <span className="num whitespace-nowrap">
                          <span className={next[1]}>{next[0]}</span>
                          <span className={`ml-1.5 ${toneOf(row.next_pct_chg)}`}>
                            {fmtPct(row.next_pct_chg)}
                          </span>
                        </span>
                      ) : (
                        <span className="num text-fg-dim">—</span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}
