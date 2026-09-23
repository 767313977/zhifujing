import { useState } from 'react'
import type { FundFlowMatrixOut } from '../api/types'
import { fmtNum, fmtShortDate, toneOf } from '../lib/format'

/**
 * 资金流多日矩阵：**列是交易日（从新到旧）、行是当日的净流入第 N 名**。
 *
 * 与「板块轮动」那张矩阵同一套版式（`SectorRotationPanel`）：每天只给一个截面，
 * 跨天并排看才有「钱在板块之间搬家」的意思 —— 同一行每天都在换名字。版式一样还
 * 有个好处：两块表上下对照时，同一天落在同一列。
 *
 * **不做行对齐**（不按某天的名单去对齐别的天），每一列都独立按净额排。
 *
 * 没抽成两个面板共用的组件：轮动那张额外带「领涨行 / 上榜次数 / 指标切换」，
 * 这里只有一张纯表（指标固定为净额），共用就得为那些额外件写一堆开关。
 */
interface Props {
  data: FundFlowMatrixOut | null
  loading: boolean
  /** 点格子 = 选中那个板块（与轮动矩阵、下方板块排行是同一个动作） */
  onSelect: (code: string) => void
}

export default function SectorFlowMatrix({ data, loading, onSelect }: Props) {
  /**
   * 鼠标停在哪只板块上：**同一板块在别的列里的格子一起点亮** —— 看的就是这条轨迹
   * （比如「地产链」连着几天都在榜首，而「租售同权」只上来一天）。
   */
  const [hovered, setHovered] = useState<string | null>(null)

  const columns = data?.columns ?? []
  const ranks = Array.from({ length: data?.top ?? 0 }, (_, index) => index)

  if (loading) {
    return <div className="px-4 py-10 text-center text-[13px] text-fg-dim">加载中…</div>
  }
  if (columns.length === 0) {
    return (
      <div className="px-4 py-10 text-center text-[13px] text-fg-dim">
        还没有多日数据。净流入是每天收盘后现算的、补不了历史，所以这张表从改造那天起
        一天天攒 —— 现在是空的
      </div>
    )
  }

  return (
    <div className="overflow-x-auto">
      <table className="border-collapse">
        <thead className="sticky top-0 z-20">
          <tr>
            <th className="sticky left-0 z-30 border-r border-b border-line-soft bg-ink-850 px-2 py-1.5 text-left font-normal whitespace-nowrap text-fg-dim">
              排名
            </th>
            {columns.map((column) => (
              <th
                key={column.trade_date}
                className="num border-b border-line-soft bg-ink-850 px-2 py-1.5 text-left font-normal whitespace-nowrap text-fg-muted"
              >
                {fmtShortDate(column.trade_date)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {ranks.map((rank) => (
            <tr key={rank}>
              <td className="num sticky left-0 z-10 border-r border-b border-line-soft bg-ink-900 px-2 py-1 text-fg-dim">
                {rank + 1}
              </td>
              {columns.map((column) => {
                const cell = column.cells[rank]
                if (!cell) {
                  return (
                    <td
                      key={column.trade_date}
                      className="border-b border-line-soft px-2 py-1 text-fg-dim"
                    >
                      —
                    </td>
                  )
                }
                const active = hovered === cell.code
                return (
                  <td
                    key={column.trade_date}
                    className={[
                      'border-b border-line-soft p-0',
                      active ? 'bg-ink-700' : '',
                    ].join(' ')}
                  >
                    <button
                      type="button"
                      onClick={() => onSelect(cell.code)}
                      onMouseEnter={() => setHovered(cell.code)}
                      onMouseLeave={() => setHovered(null)}
                      className="block w-full px-2 py-1 text-left"
                    >
                      <span
                        className={`text-[12px] whitespace-nowrap ${active ? 'text-accent' : 'text-fg'}`}
                      >
                        {cell.name}
                      </span>
                      {/* 数字按正负上色（红=净流入 绿=净流出），与上面的条形图一致。
                          净流出不用另开一张表：它就是同一张榜的另一头 */}
                      <span
                        className={[
                          'num mt-0.5 block text-[12px]',
                          toneOf(cell.net_amount),
                        ].join(' ')}
                      >
                        {fmtNum(cell.net_amount, 1, ' 亿')}
                      </span>
                    </button>
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
