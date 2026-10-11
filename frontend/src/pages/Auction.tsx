import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../api/client'
import type { AuctionList, AuctionRow, AuctionSort, AuctionTrend } from '../api/types'
import Alert from '../components/Alert'
import EChart from '../components/EChart'
import type { ChartOption } from '../components/EChart'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import SortTh from '../components/SortTh'
import StockLink from '../components/StockLink'
import { AXIS_LABEL, AXIS_LINE, CHART, GRID, SPLIT_LINE, TOOLTIP, withAlpha } from '../lib/chart'
import { fmtAmount, fmtInt, fmtNum, fmtPct, toneOf } from '../lib/format'

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
      key: 'ratio',
      label: '量比',
      sortKey: 'ratio',
      render: (r) => <span className="num">{fmtNum(r.volume_ratio, 2)}</span>,
    },
    {
      key: 'turnover',
      label: '竞价换手',
      sortKey: 'turnover',
      render: (r) => <span className="num">{fmtNum(r.turnover, 2, '%')}</span>,
    },
    {
      key: 'boards',
      label: '昨日连板',
      render: (r) => (
        <span className={r.consecutive ? 'num font-semibold text-up' : 'num text-fg-dim'}>
          {r.consecutive ? `${fmtInt(r.consecutive)}板` : '—'}
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

  // 总量走势只随日期变（与筛选无关，是全市场口径）
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
    // 数值列先看最大的那头，与全站表格一致
    setOrder('desc')
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
                        <td key={column.key} className={NARROW(column)}>
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
          「昨日连板」取上一交易日涨停池口径 —— 竞价时当天的池还没有。
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
