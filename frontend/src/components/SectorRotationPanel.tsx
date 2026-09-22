import { useState } from 'react'
import type {
  RotationColumn,
  RotationLeader,
  RotationMetric,
  SectorRotation,
  SectorTaxonomy,
} from '../api/types'
import {
  AXIS_LABEL,
  AXIS_LINE,
  GRID,
  LEGEND,
  SERIES_PALETTE,
  SPLIT_LINE,
  TOOLTIP,
} from '../lib/chart'
import { fmtAmount, fmtNum, fmtPct, fmtShortDate, toneOf } from '../lib/format'
import EChart from './EChart'
import type { ChartOption } from './EChart'
import Segmented from './Segmented'
import StockLink from './StockLink'

/**
 * 板块轮动矩阵：**列是交易日（从新到旧）、行是当天的第 N 名**。
 *
 * 板块排行每天只给一个截面，跨天并排看才有「轮动」的意思 —— 同一行每天都在换名字，
 * 那正是资金在题材之间搬家。所以这里**不做行对齐**（不按某天的名单去对齐别的天），
 * 每一列都是独立按指标排出来的。
 *
 * 布局参考了短线侠的「板块轮动」页（`duanxianxia.com/web/platerotat`），
 * 但数据用的是本地 sector_daily（开盘红口径），不依赖那个站点。
 */

const METRICS: { key: RotationMetric; label: string; hint: string }[] = [
  {
    key: 'strength',
    label: '强度',
    hint: '开盘啦的强度值（它 App 里那张板块榜就是按这个排的），上万即强势',
  },
  { key: 'pct_chg', label: '涨幅', hint: '按当日涨跌幅排' },
  {
    key: 'amount',
    label: '量能',
    hint: '按当日成交额排，已剔掉「业绩增长 / 国有企业 / 地域」这类筛出来的板块',
  },
]

/**
 * 周期档位。上限受本地板块日线的天数限制，后端会按库里真有几天返回，
 * 不会用空列凑数。
 *
 * **到这里为止，再加就看不动了** —— 轮动是「横向扫一眼谁在换」，
 * 60 列在 430px 高的表里已经要左右拖；而走势图不一样，越长反而越能看出
 * 位置（所以走势那边开到了 250 天，见页面里的 `CURVE_SPANS`）。
 *
 * **导出**给页面用来校验 URL 里的 `rotdays` —— 档位清单只能有一份，
 * 抄第二份就会在改档位时对不上（这个教训项目里已经吃过一次，见设计文档 8.23.6）。
 */
export const ROTATION_SPANS = [10, 20, 30, 45, 60]

/** 折线图最多画几条板块的轨迹。开盘啦画 4 条，多一条颜色就更难分了 */
const LADDER_SERIES = 5

/** 「领涨」行里每只票的叫法。开盘啦用「龙一~龙五」，跟着用（龙一 = 当日该板块最强的那只） */
const LEADER_LABELS = ['龙一', '龙二', '龙三', '龙四', '龙五']

/**
 * 上榜次数榜：把上面的矩阵**转置**过来。
 *
 * 矩阵回答「某一天的前 N 名是谁」，这里回答「某个板块在这段窗口里上过几次榜、
 * 每次排第几」—— 也就是开盘啦那张图底部的折线。**不需要额外取数**，
 * 纯前端把已有的矩阵转置一下就有了。
 */
function buildLadder(columns: RotationColumn[]) {
  const byName = new Map<string, Map<string, number>>()
  for (const column of columns) {
    column.cells.forEach((cell, index) => {
      const ranks = byName.get(cell.name) ?? new Map<string, number>()
      ranks.set(column.trade_date, index + 1)
      byName.set(cell.name, ranks)
    })
  }
  return [...byName.entries()]
    .map(([name, ranks]) => ({
      name,
      count: ranks.size,
      // 与 columns 对齐：没上榜的那天是 null
      ranks: columns.map((column) => ranks.get(column.trade_date) ?? null),
    }))
    // 上榜次数多的在前；次数相同按名字 —— 不留这个次序的话每次渲染颜色会跳
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name))
}

function buildLadderOption(
  columns: RotationColumn[],
  ladder: { name: string; count: number; ranks: (number | null)[] }[],
  top: number,
): ChartOption {
  return {
    grid: { ...GRID, top: 30 },
    legend: { ...LEGEND, top: 0, left: 0 },
    tooltip: { ...TOOLTIP, trigger: 'item' },
    xAxis: {
      type: 'category',
      // x 轴**从左到右是从新到旧**，与上面的矩阵列顺序一致。折线图这样画不合常规，
      // 但两张图上下对齐、同一天落在同一列，对照着看省事（开盘啦也是这么排的）
      data: columns.map((column) => fmtShortDate(column.trade_date)),
      axisLabel: AXIS_LABEL,
      axisLine: AXIS_LINE,
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      // 名次 1 在顶部：往上走 = 名次前进，符合直觉
      inverse: true,
      min: 1,
      max: top,
      interval: 1,
      axisLabel: AXIS_LABEL,
      axisLine: AXIS_LINE,
      splitLine: SPLIT_LINE,
    },
    series: ladder.slice(0, LADDER_SERIES).map((item, index) => {
      const color = SERIES_PALETTE[index % SERIES_PALETTE.length]
      return {
        name: `${item.name}(${item.count}次)`,
        type: 'line',
        data: item.ranks,
        // 跨过没上榜的日子把线连起来（开盘啦也这样）。**所以线连着 ≠ 一直在榜**：
        // 判断某天在不在榜，要看那天的圆点有没有
        connectNulls: true,
        symbolSize: 6,
        itemStyle: { color },
        lineStyle: { width: 1.5, color },
      }
    }),
  }
}

interface Props {
  data: SectorRotation | null
  loading: boolean
  metric: RotationMetric
  /** 口径。**行业口径不显示「领涨」行**：涨停归属只覆盖精选板块，那行会永远是「—」 */
  taxonomy: SectorTaxonomy
  /** 矩阵显示多少列（交易日） */
  days: number
  /** 上榜次数折线图的统计窗口。**与矩阵窗口独立** —— 矩阵看 20 天、上榜看 50 天是常见用法 */
  ladderDays: number
  /** 「领涨」行的数据：`{交易日: 涨停股}`，取的是当前选中板块在各天的涨停股 */
  leaders: Record<string, RotationLeader[]>
  /** 选中板块的名字，只用于「领涨」行的 tooltip 说明 */
  leaderName: string | null
  onMetric: (metric: RotationMetric) => void
  onDays: (days: number) => void
  onLadderDays: (days: number) => void
  /** 点格子跳到那个板块的详情（右侧那块），与下方板块排行点一行是同一个动作 */
  onSelect: (code: string) => void
}

export default function SectorRotationPanel({
  data,
  loading,
  metric,
  taxonomy,
  days,
  ladderDays,
  leaders,
  leaderName,
  onMetric,
  onDays,
  onLadderDays,
  onSelect,
}: Props) {
  /**
   * 鼠标停在哪只板块上。**同一板块在别的列里的格子会一起点亮** ——
   * 轮动看的就是这一条轨迹（比如「芯片」连续五天都在榜上、而「医药」是今天刚上来的）。
   */
  const [hovered, setHovered] = useState<string | null>(null)

  const allColumns = data?.columns ?? []
  // 后端按「两个窗口里较大的那个」取数（一次请求就够），这里各切各的：
  // 矩阵看 `days` 列、上榜统计看 `ladderDays` 列。**切片比再发一个请求便宜得多**。
  const columns = allColumns.slice(0, days)
  const ladderColumns = allColumns.slice(0, ladderDays)
  const ranks = Array.from({ length: data?.top ?? 0 }, (_, index) => index)
  const hint = METRICS.find((item) => item.key === metric)?.hint ?? ''
  // 上榜次数（把矩阵转置）。数据量很小（最多 60 列 × 30 行），每次渲染重算就好，
  // 不值得为它上一次 useMemo
  const ladder = buildLadder(ladderColumns)

  return (
    <div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-line-soft px-3 py-2">
        <Segmented value={metric} items={METRICS} onChange={onMetric} />
        {/* 这个窗口只管上面的矩阵；上榜次数折线图有自己的一套档位（在面板底部），
            所以这里标一下「矩阵」，免得两套档位看着像重复的 */}
        <span className="text-[12px] text-fg-dim">矩阵</span>
        <Segmented
          value={days}
          items={ROTATION_SPANS.map((span) => ({ key: span, label: `近${span}日` }))}
          onChange={onDays}
        />
        <span className="text-[12px] text-fg-dim">{hint}</span>
        <span className="num ml-auto text-[12px] text-fg-dim">
          {loading
            ? '加载中…'
            : taxonomy === 'kph_selected'
              ? `${columns.length} 个交易日 · 点格子看板块详情，领涨行跟着选中板块`
              : `${columns.length} 个交易日 · 行业口径没有涨停归属，不显示领涨行`}
        </span>
      </div>

      {loading ? (
        <div className="px-4 py-10 text-center text-[13px] text-fg-dim">加载中…</div>
      ) : columns.length === 0 ? (
        <div className="px-4 py-10 text-center text-[13px] text-fg-dim">
          本地还没有板块历史，请先在「数据管理」里执行一次采集
        </div>
      ) : (
        <>
          {/* 不加 max-height：整块矩阵（每天前 10 名 + 领涨行）一次性显示完。
              原来卡了 430px，10 行加领涨行装不下，看第 1~4 名要滚动 —— 而「看头部」
              恰恰是这张表的主要用法。横向仍留给 `overflow-x`：20~60 列在窄屏放不下。 */}
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
                            <span
                              className={[
                                'num mt-0.5 block text-[12px]',
                                // 按成交额排时，格子里那行数字也该按涨跌上色 ——
                                // 成交额本身没有方向，红绿才是这行字要看的东西。
                                // 强度虽然也是「强度越大越好」，但它有正负（弱板块为负），
                                // 所以照旧按正负上色，与开盘啦那张图的红绿一致。
                                toneOf(metric === 'amount' ? cell.pct_chg : cell.value),
                              ].join(' ')}
                            >
                              {metric === 'amount'
                                ? fmtAmount(cell.value)
                                : metric === 'strength'
                                  ? // 强度就是个数，没有单位（开盘啦也这么显示）
                                    fmtNum(cell.value, 0)
                                  : fmtPct(cell.value)}
                            </span>
                          </button>
                        </td>
                      )
                    })}
                  </tr>
                ))}

                {/* 「领涨」行：**当前选中板块**在这些天的涨停股（跟着点格子变，见 leaders 的说明）。
                    **必须放在这个表格里**而不是表格外 —— 只有同处一张表，格子才会与上面的列
                    严格同宽、同一天落在同一列。
                    口径是「涨停股」不是「涨幅前 5」，见 types.ts 里 RotationLeader 的说明。
                    行业口径**整行不渲染**：涨停归属只覆盖精选板块，渲染出来会是一整行「—」，
                    而那正是这个项目一直在避免的噪音。 */}
                {taxonomy === 'kph_selected' && (
                  <tr>
                    <td
                      className="num sticky left-0 z-10 border-r border-t border-line-soft bg-ink-900 px-2 py-1 text-fg-dim"
                      title={
                        leaderName
                          ? `领涨 = 选中板块「${leaderName}」当天的涨停股`
                          : '领涨 = 选中板块当天的涨停股'
                      }
                    >
                      领涨
                    </td>
                    {columns.map((column) => {
                      const items = leaders[column.trade_date] ?? []
                      return (
                        <td
                          key={column.trade_date}
                          className="border-t border-line-soft px-2 py-1 align-top"
                        >
                          {items.length === 0 ? (
                            // 该板块那天没有涨停股。留「—」而不是空着：空着会被读成「没取到」
                            <span className="text-[12px] text-fg-dim">—</span>
                          ) : (
                            items.map((item, index) => (
                              <div
                                key={item.code}
                                className="text-[12px] leading-4 whitespace-nowrap"
                                title={`${LEADER_LABELS[index]} ${item.name ?? item.code}${
                                  item.consecutive && item.consecutive > 1
                                    ? ` · ${item.consecutive} 连板`
                                    : ''
                                }`}
                              >
                                <span className="text-fg-dim">{LEADER_LABELS[index]}</span>{' '}
                                <StockLink code={item.code} className="text-fg-muted">
                                  {item.name ?? item.code}
                                </StockLink>
                              </div>
                            ))
                          )}
                        </td>
                      )
                    })}
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {/* 上榜次数曲线：把上面的矩阵转置过来看「谁一直在榜、谁今天刚上来」。
              指标跟矩阵走（同一份榜单），但**窗口是独立的** —— 矩阵看 20 天、上榜看
              50 天是常见用法（开盘啦那张图也是独立档位）。 */}
          {ladder.length > 0 && (
            <div className="border-t border-line-soft px-3 py-2">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
                <span className="text-[12px] text-fg-dim">上榜次数</span>
                <Segmented
                  value={ladderDays}
                  items={ROTATION_SPANS.map((span) => ({ key: span, label: `近${span}日` }))}
                  onChange={onLadderDays}
                />
                <span className="num ml-auto text-[12px] text-fg-dim">
                  {ladderColumns.length} 列 · 圆点才是真上榜，线只是跨过没上榜的日子
                </span>
              </div>
              <EChart
                option={buildLadderOption(ladderColumns, ladder, data?.top ?? 10)}
                height={200}
              />
            </div>
          )}
        </>
      )}
    </div>
  )
}
