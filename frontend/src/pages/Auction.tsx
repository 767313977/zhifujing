import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../api/client'
import type {
  AuctionBoardItem,
  AuctionBoards,
  AuctionList,
  AuctionRow,
  AuctionSort,
  AuctionTrend,
} from '../api/types'
import Alert from '../components/Alert'
import EChart from '../components/EChart'
import type { ChartOption } from '../components/EChart'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import SortTh from '../components/SortTh'
import StockLink from '../components/StockLink'
import { AXIS_LABEL, AXIS_LINE, CHART, GRID, SPLIT_LINE, TOOLTIP, withAlpha } from '../lib/chart'
import { fmtAmount, fmtInt, fmtNum, fmtPct, toneOf } from '../lib/format'
import { useSort } from '../lib/sort'
import type { SortSpecs } from '../lib/sort'

/**
 * 筛选快捷档位 —— 全是「下限」，点一下生效、再点一下取消（对应参考图顶部那排按钮）。
 * 只挑短线最常用的几档，不做任意区间的输入框：这一页要看的是「谁强」，不是精确定位。
 */
const PCT_STEPS = [3, 5, 7, 9] // 竞价涨幅 ≥ n%
const AMOUNT_STEPS = [0.5, 1, 2] // 竞价金额 ≥ n 亿
const RATIO_STEPS = [2, 3] // 量比 ≥
const BOARDS_STEPS = [1, 2, 3] // 昨日连板 ≥ n 板

const ROW_LIMIT = 300

interface Column {
  key: string
  label: string
  align?: 'left' | 'right'
  /** 窄屏（< xl=1280）隐藏。表头与单元格共用同一个值，别只藏一边 */
  narrowHidden?: boolean
  /** 后端排序字段。不给 = 这列不可排序 */
  sortKey?: AuctionSort
  /**
   * 首次点这一列时的方向。默认 `desc`（绝大多数列「越大越值得看」）；
   * **排名类要 `asc`** —— 点一下先看第 1 名，而不是倒数第一。
   */
  first?: 'asc' | 'desc'
  render: (row: AuctionRow) => ReactNode
}

function buildColumns(): Column[] {
  return [
    {
      key: 'code',
      label: '代码',
      align: 'left',
      render: (r) => (
        <StockLink code={r.code} className="num text-fg-muted">
          {r.code}
        </StockLink>
      ),
    },
    {
      key: 'name',
      label: '名称',
      align: 'left',
      render: (r) => <StockLink code={r.code}>{r.name ?? r.code}</StockLink>,
    },
    {
      key: 'pct',
      label: '竞价涨幅',
      sortKey: 'pct',
      render: (r) => <span className={`num ${toneOf(r.pct_chg)}`}>{fmtPct(r.pct_chg)}</span>,
    },
    {
      key: 'rank',
      label: '排名',
      sortKey: 'rank',
      first: 'asc',
      // 全市场竞价涨幅位次；悬停看**板块内**（同花顺行业最后一级）位次
      render: (r) => (
        <span
          className="num text-fg-muted"
          title={
            r.industry_rank != null ? `板块内（同花顺行业）第 ${r.industry_rank} 名` : undefined
          }
        >
          {r.rank != null ? `#${fmtInt(r.rank)}` : '—'}
        </span>
      ),
    },
    {
      key: 'open_pct',
      label: '开盘涨幅',
      narrowHidden: true,
      render: (r) => <span className={`num ${toneOf(r.open_pct)}`}>{fmtPct(r.open_pct)}</span>,
    },
    {
      key: 'amount',
      label: '竞价金额',
      sortKey: 'amount',
      render: (r) => <span className="num">{fmtAmount(r.amount)}</span>,
    },
    {
      key: 'amount_ratio',
      label: '竞价额占比',
      sortKey: 'amount_ratio',
      // 相对比例才有可比性：大盘股竞价 1 亿看似多，占比可能极低。>5% 明显放量、>10% 超强
      render: (r) => (
        <span className="num text-fg-muted" title="竞价成交额 ÷ 昨日全天成交额（分母只覆盖流动性池）">
          {fmtNum(r.amount_ratio, 2, '%')}
        </span>
      ),
    },
    {
      key: 'ratio',
      // 改叫「开盘量比」：腾讯开盘前不填量比，这一列取的是 9:31 补采帧的值
      label: '开盘量比',
      sortKey: 'ratio',
      render: (r) => <span className="num">{fmtNum(r.volume_ratio, 2)}</span>,
    },
    {
      key: 'auction_ratio',
      // 自算：当日竞价量 ÷ 前 5 个交易日竞价量均值（同花顺口径）；历史不足 3 天时为「—」。
      label: '竞价量比',
      sortKey: 'auction_ratio',
      render: (r) => <span className="num text-fg-muted">{fmtNum(r.auction_ratio, 2)}</span>,
    },
    {
      key: 'turnover',
      label: '竞价换手',
      sortKey: 'turnover',
      render: (r) => <span className="num">{fmtNum(r.turnover, 2, '%')}</span>,
    },
    {
      key: 'free_turnover',
      label: '真实换手',
      sortKey: 'free_turnover',
      // 分母换成**自由流通股**（剔除大股东/限售等不动的筹码）—— 普通换手率会低估
      render: (r) => (
        <span
          className="num text-fg-muted"
          title={
            r.free_float_mv != null
              ? `竞价量 ÷ 自由流通股（自由流通市值 ${fmtAmount(r.free_float_mv)}）`
              : '竞价量 ÷ 自由流通股'
          }
        >
          {fmtNum(r.free_turnover, 3, '%')}
        </span>
      ),
    },
    {
      key: 'seal_ratio',
      label: '封成比',
      sortKey: 'seal_ratio',
      // 打板核心：涨停价上的封单额 ÷ 自由流通市值。只在**竞价就封在涨停价上**时有值
      render: (r) => (
        <span
          className={`num ${
            r.seal_ratio != null && r.seal_ratio >= 1 ? 'text-up' : 'text-fg-muted'
          }`}
          title="涨停价上的封单额 ÷ 自由流通市值（>1% 较强、>3% 超强）"
        >
          {fmtNum(r.seal_ratio, 2, '%')}
        </span>
      ),
    },
    {
      key: 'amplitude',
      label: '波动率',
      sortKey: 'amplitude',
      narrowHidden: true,
      // 9:15–9:26 竞价价的振幅：高开但几乎不动 = 一致性强；上下大幅波动 = 分歧大
      render: (r) => (
        <span
          className="num text-fg-muted"
          title="9:15–9:26 竞价最高价与最低价之差 ÷ 昨收"
        >
          {fmtNum(r.amplitude, 2, '%')}
        </span>
      ),
    },
    {
      key: 'cancel_rate',
      label: '撤单率',
      sortKey: 'cancel_rate',
      // 9:20 是不可撤单的截止线：撤得越少越真，负值 = 挂单还在增加
      render: (r) => (
        <span
          className="num text-fg-muted"
          title="(9:20 涨停/跌停价上的挂单量 − 9:21 的) ÷ 9:20 的；负值 = 挂单还在增加。需腾讯竞价时段给出挂单量"
        >
          {fmtNum(r.cancel_rate, 1, '%')}
        </span>
      ),
    },
    {
      key: 'committee_diff',
      label: '委差',
      sortKey: 'committee_diff',
      narrowHidden: true,
      // 未匹配量差 = 委买总量 − 委卖总量；正 = 未匹配的买盘更多。库里是股，显示成「手」
      render: (r) => (
        <span
          className="num text-fg-muted"
          title="委买总量 − 委卖总量（未匹配量差），单位：手。需腾讯竞价时段给出委差"
        >
          {r.committee_diff != null ? fmtNum(r.committee_diff / 100, 0, '手') : '—'}
        </span>
      ),
    },
    {
      key: 'prev',
      label: '昨日',
      sortKey: 'prev_boards',
      // 昨日状态：N板（含 1 板 = 首板）/ 炸板 / —；悬停看昨日全天换手率。
      // 「炸板」查的是昨日的炸板池，与涨停池互斥，所以不会有「既是 N 板又炸板」。
      render: (r) => (
        <span
          className={`num ${r.consecutive ? 'font-semibold text-up' : 'text-fg-muted'}`}
          title={r.prev_turnover != null ? `昨日全天换手 ${r.prev_turnover.toFixed(2)}%` : undefined}
        >
          {r.consecutive ? `${fmtInt(r.consecutive)}板` : r.prev_broken ? '炸板' : '—'}
        </span>
      ),
    },
    {
      key: 'price',
      label: '竞价价',
      sortKey: 'price',
      render: (r) => <span className="num">{fmtNum(r.price, 2)}</span>,
    },
    {
      key: 'pre_close',
      label: '昨收',
      narrowHidden: true,
      render: (r) => <span className="num text-fg-muted">{fmtNum(r.pre_close, 2)}</span>,
    },
    {
      key: 'float_mv',
      label: '流通市值',
      narrowHidden: true,
      sortKey: 'float_mv',
      render: (r) => <span className="num text-fg-muted">{fmtAmount(r.float_mv)}</span>,
    },
  ]
}

const COLUMNS = buildColumns()
const NARROW = (column: Column) => (column.narrowHidden ? 'max-xl:hidden' : '')

/** 排序键 → 首次点击方向（只收录显式声明了 `first` 的列）。 */
const SORT_FIRST: Record<string, 'asc' | 'desc'> = {}
for (const column of COLUMNS) {
  if (column.sortKey && column.first) SORT_FIRST[column.sortKey] = column.first
}

/**
 * 板块榜的排序口径。数据只有 ≤20 行，**直接在前端排**（和后端给的默认顺序一致：
 * 平均竞价涨幅降序）—— 不值得为它再跑一趟服务端排序。
 */
const BOARD_SORTS: SortSpecs<AuctionBoardItem> = {
  industry: { value: (item) => item.industry, first: 'asc' },
  count: { value: (item) => item.count },
  avg_pct: { value: (item) => item.avg_pct },
  amount: { value: (item) => item.amount },
  limit_count: { value: (item) => item.limit_count },
}

/** 一个筛选档位按钮。`active` 高亮；再点一下由调用方取消。 */
function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        'border px-2 py-[2px] text-[12px] transition-colors',
        active ? 'border-accent/60 text-accent' : 'border-line text-fg-muted hover:text-fg',
      ].join(' ')}
    >
      {children}
    </button>
  )
}

export default function AuctionPage() {
  const [date, setDate] = useState<string | null>(null)
  const [dates, setDates] = useState<string[]>([])
  const [data, setData] = useState<AuctionList | null>(null)
  const [trend, setTrend] = useState<AuctionTrend | null>(null)
  const [boards, setBoards] = useState<AuctionBoards | null>(null)

  // 板块榜在前端排（数据少）；默认按平均竞价涨幅降序，与后端给的一致
  const [boardSort, boardItems] = useSort(boards?.items ?? [], BOARD_SORTS, { key: 'avg_pct' })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // 筛选（都是下限；null = 不限）
  const [minPct, setMinPct] = useState<number | null>(null)
  const [minAmount, setMinAmount] = useState<number | null>(null) // 亿元
  const [minRatio, setMinRatio] = useState<number | null>(null)
  const [minBoards, setMinBoards] = useState<number | null>(null)
  const [sort, setSort] = useState<AuctionSort>('amount')
  const [order, setOrder] = useState<'asc' | 'desc'>('desc')

  useEffect(() => {
    // 日期候选用**交易日历**（`/market/dates`，与其它页同一个来源），不是「有竞价数据的日子」——
    // 后者在刚上线那阵子是空的、根本没得选。选了没采样的日子就显示「当日还没有竞价数据」。
    api.dates().then(setDates).catch(() => setDates([]))
  }, [])

  // 走势与板块榜都只随**日期**变（与上面的筛选无关，都是全市场口径）
  useEffect(() => {
    let stale = false
    api
      .auctionTrend(date)
      .then((value) => {
        if (!stale) setTrend(value)
      })
      .catch(() => {
        if (!stale) setTrend(null)
      })
    api
      .auctionBoards(date)
      .then((value) => {
        if (!stale) setBoards(value)
      })
      .catch(() => {
        if (!stale) setBoards(null)
      })
    return () => {
      stale = true
    }
  }, [date])

  const load = useCallback(
    async (isStale: () => boolean) => {
      setLoading(true)
      setError(null)
      try {
        const value = await api.auctionList({
          date,
          sort,
          order,
          minPct: minPct ?? undefined,
          minAmount: minAmount != null ? minAmount * 1e8 : undefined,
          minRatio: minRatio ?? undefined,
          minBoards: minBoards ?? undefined,
          limit: ROW_LIMIT,
        })
        if (isStale()) return
        setData(value)
      } catch (err) {
        if (isStale()) return
        setData(null)
        setError((err as Error).message)
      } finally {
        if (!isStale()) setLoading(false)
      }
    },
    [date, sort, order, minPct, minAmount, minRatio, minBoards],
  )

  useEffect(() => {
    let stale = false
    void load(() => stale)
    return () => {
      stale = true
    }
  }, [load])

  const reset = () => {
    setMinPct(null)
    setMinAmount(null)
    setMinRatio(null)
    setMinBoards(null)
    setSort('amount')
    setOrder('desc')
  }

  const onToggleSort = (key: string) => {
    if (key === sort) {
      setOrder((current) => (current === 'desc' ? 'asc' : 'desc'))
      return
    }
    setSort(key as AuctionSort)
    // 首次方向：默认「先看最大的那头」；排名类显式声明了 asc（先看第 1 名）
    setOrder(SORT_FIRST[key] ?? 'desc')
  }

  const trendOption = useMemo<ChartOption>(() => {
    if (!trend || trend.points.length === 0) return {}
    const labels = trend.points.map((p) => `${p.time.slice(0, 2)}:${p.time.slice(2)}`)
    const values = trend.points.map((p) => Number((p.amount / 1e8).toFixed(2)))
    return {
      grid: { ...GRID, top: 30 },
      tooltip: {
        ...TOOLTIP,
        trigger: 'axis' as const,
        valueFormatter: (value: unknown) => (value == null ? '—' : `${value} 亿`),
      },
      xAxis: {
        type: 'category' as const,
        data: labels,
        boundaryGap: false,
        axisLabel: AXIS_LABEL,
        axisLine: AXIS_LINE,
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value' as const,
        name: '累计竞价额(亿)',
        nameTextStyle: {
          color: CHART.fgDim,
          fontSize: 11,
          fontFamily: AXIS_LABEL.fontFamily,
        },
        axisLabel: AXIS_LABEL,
        splitLine: SPLIT_LINE,
        axisLine: { show: false },
      },
      series: [
        {
          type: 'line' as const,
          name: '累计竞价额',
          data: values,
          smooth: true,
          symbol: 'circle',
          symbolSize: 5,
          lineStyle: { width: 1.8, color: CHART.accent },
          itemStyle: { color: CHART.accent },
          areaStyle: { color: withAlpha(CHART.accent, 0.12) },
        },
      ],
    }
  }, [trend])

  const toolbar = (
    <>
      <span className="num hidden text-[13px] text-fg-dim lg:inline">
        {loading ? '加载中…' : data ? `${data.total} 只有竞价数据` : '—'}
      </span>
      <select
        value={date ?? ''}
        onChange={(event) => setDate(event.target.value || null)}
        className="num border border-line bg-ink-900 px-2 py-[3px] text-[13px] text-fg outline-none focus:border-fg-dim"
      >
        <option value="">最新</option>
        {[...dates].reverse().map((item) => (
          <option key={item} value={item}>
            {item}
          </option>
        ))}
      </select>
    </>
  )

  return (
    <Layout toolbar={toolbar}>
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}

      <div className="space-y-4">
        {trend && trend.points.length > 0 && (
          <Panel
            title="竞价量能走势"
            meta={<span className="num">全市场累计竞价成交额 · 读的是斜率</span>}
            delay={40}
          >
            <div className="px-2 pt-2">
              <EChart option={trendOption} height={200} />
            </div>
          </Panel>
        )}

        {/* 板块竞价强度：判断「个股独立行情」还是「板块共振」——个股高开 + 板块整体高开才有持续性 */}
        {boards && boards.items.length > 0 && (
          <Panel
            title="板块竞价强度"
            meta={<span className="num">同花顺行业（末级）· 只列 ≥3 只的 · 点列头排序</span>}
            delay={60}
          >
            <div className="max-h-[360px] overflow-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <SortTh align="left" sortKey="industry" {...boardSort}>
                      板块
                    </SortTh>
                    <SortTh sortKey="count" {...boardSort}>
                      只数
                    </SortTh>
                    <SortTh sortKey="avg_pct" {...boardSort}>
                      平均竞价涨幅
                    </SortTh>
                    <SortTh sortKey="amount" {...boardSort}>
                      竞价额
                    </SortTh>
                    <SortTh sortKey="limit_count" {...boardSort}>
                      竞价涨停
                    </SortTh>
                  </tr>
                </thead>
                <tbody>
                  {boardItems.map((item) => (
                    <tr key={item.industry}>
                      <td className="!text-left">{item.industry}</td>
                      <td className="num text-fg-muted">{item.count}</td>
                      <td className={`num ${toneOf(item.avg_pct)}`}>{fmtPct(item.avg_pct)}</td>
                      <td className="num text-fg-muted">{fmtAmount(item.amount)}</td>
                      <td className={`num ${item.limit_count > 0 ? 'text-up' : 'text-fg-dim'}`}>
                        {item.limit_count || '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
        )}

        <Panel
          title="竞价强度"
          meta={
            <span className="num">
              {data ? `显示 ${data.shown} / ${data.total} 只` : '—'}
              <span className="ml-2 text-fg-dim">点列头排序 · 点名称看日 K</span>
            </span>
          }
          delay={80}
        >
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line-soft px-3 py-2.5">
            <FilterRow label="竞价涨幅">
              {PCT_STEPS.map((step) => (
                <Chip
                  key={step}
                  active={minPct === step}
                  onClick={() => setMinPct(minPct === step ? null : step)}
                >
                  ≥{step}%
                </Chip>
              ))}
            </FilterRow>
            <FilterRow label="竞价金额">
              {AMOUNT_STEPS.map((step) => (
                <Chip
                  key={step}
                  active={minAmount === step}
                  onClick={() => setMinAmount(minAmount === step ? null : step)}
                >
                  ≥{step}亿
                </Chip>
              ))}
            </FilterRow>
            <FilterRow label="量比">
              {RATIO_STEPS.map((step) => (
                <Chip
                  key={step}
                  active={minRatio === step}
                  onClick={() => setMinRatio(minRatio === step ? null : step)}
                >
                  ≥{step}
                </Chip>
              ))}
            </FilterRow>
            <FilterRow label="昨日连板">
              {BOARDS_STEPS.map((step) => (
                <Chip
                  key={step}
                  active={minBoards === step}
                  onClick={() => setMinBoards(minBoards === step ? null : step)}
                >
                  ≥{step}板
                </Chip>
              ))}
            </FilterRow>
            <button
              type="button"
              onClick={reset}
              className="ml-auto border border-line px-2 py-[2px] text-[12px] text-fg-dim transition-colors hover:text-fg"
            >
              重置
            </button>
          </div>

          {loading ? (
            <div className="px-4 py-8 text-center text-[14px] text-fg-dim">加载中…</div>
          ) : !data || data.total === 0 ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              当日还没有竞价数据 —— 采样任务在<strong>交易日 9:15–9:26</strong>自动跑，
              跑过之后这里才有数据。
            </div>
          ) : data.shown === 0 ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              没有符合条件的票，放宽上面的筛选试试。
            </div>
          ) : (
            <div className="max-h-[600px] overflow-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    {COLUMNS.map((column) => (
                      <SortTh
                        key={column.key}
                        align={column.align}
                        className={NARROW(column)}
                        sortKey={column.sortKey}
                        activeKey={column.sortKey ? sort : null}
                        dir={order}
                        onToggle={column.sortKey ? onToggleSort : undefined}
                      >
                        {column.label}
                      </SortTh>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map((row) => (
                    <tr key={row.code}>
                      {COLUMNS.map((column) => (
                        <td
                          key={column.key}
                          // `grid-table` 的 td 默认右对齐，左对齐的列必须显式 `!text-left`
                          // （表头走 SortTh 的 align，两边都用同一个字段才不会错位）
                          className={
                            [
                              column.align === 'left' ? '!text-left' : '',
                              NARROW(column),
                            ]
                              .filter(Boolean)
                              .join(' ') || undefined
                          }
                        >
                          {column.render(row)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <p className="px-1 text-[12px] leading-relaxed text-fg-dim">
          数据来源：腾讯批量行情，交易日 9:15–9:26 逐分钟采样（零 iFinD 配额）。
          「排名」是全市场竞价涨幅位次，悬停看板块内位次；「波动率」= 竞价全程（最高−最低）÷ 昨收；
          「真实换手」分母用自由流通股（剔除大股东/限售等不动的筹码），悬停看自由流通市值；
          「竞价额占比」分母是昨日全天成交额（只覆盖流动性池约 3000 只，其余为空）；
          「昨日」是上一交易日的涨停池/炸板池状态，悬停看昨日全天换手。
          「封成比」「撤单率」「委差」三项取的是竞价挂单 / 委差（9:20 前后各一帧）——
          ⚠️ 腾讯在竞价时段是否真给这几个字段尚未核实，若为空说明它开盘前不填，届时会撤掉。
          「开盘量比」取开盘后 9:31 那一帧（腾讯开盘前不填量比）；「竞价量比」是本机自算：
          当日竞价量 ÷ 前 5 个交易日竞价量均值（口径同同花顺），采样历史不足 3 天时为空。
          参考图里的 净额 / 承接强度 / 北向 / 人气排名 本站不提供：北向净流入已停止披露、
          承接强度属外部工具专有指标，取不到就不编。
        </p>
      </div>
    </Layout>
  )
}

function FilterRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-[12px] text-fg-dim">{label}</span>
      {children}
    </div>
  )
}
