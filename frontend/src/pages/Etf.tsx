import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type {
  EtfCategoryRow,
  EtfRow,
  EtfScreen,
  EtfScreenQuery,
  EtfTrack,
  EtfTrackDay,
  EtfTrackStat,
} from '../api/types'
import EChart from '../components/EChart'
import type { ChartOption } from '../components/EChart'
import Layout from '../components/Layout'
import LoadState from '../components/LoadState'
import Panel from '../components/Panel'
import Segmented from '../components/Segmented'
import SortTh from '../components/SortTh'
import { AXIS_LABEL, AXIS_LINE, CHART, GRID, LEGEND, SERIES_PALETTE, SPLIT_LINE, TOOLTIP } from '../lib/chart'
import { fmtAmount, fmtNum, fmtPct, fmtShortDate, toneOf } from '../lib/format'
import { useSort } from '../lib/sort'
import type { SortSpecs } from '../lib/sort'

/**
 * ETF 筛选（2026-10-31 加）。
 *
 * 口径（页面顶部必须写清，否则会被当成实时数据）：
 *
 * - **盘后数据**：日线来自通达信盘后包（每天收盘后采，可回补一年），不是实时价；
 * - **涨幅按前收复利**：除息 / 份额折算日的 `prev_close` 是交易所调整后的，序列按
 *   `pct_chg` 复利，不会在分红日出现假跌；
 * - **净申赎自 2026-09-21 起**：份额从那天才有，更早显示「—」；
 * - **折溢价这一期不做**：`t0` 预设已排除 QDII（溢价回落会直接亏，没有溢价数据时不推）。
 */

const PRESET_ITEMS = [
  { key: 'momentum', label: '动量轮动' },
  { key: 'breakout', label: '趋势突破' },
  { key: 'inflow', label: '资金流入' },
  { key: 'defensive', label: '红利低波' },
  { key: 't0', label: 'T+0' },
]

const SCREEN_SORTS: SortSpecs<EtfRow> = {
  name: { value: (row) => row.name ?? row.code, first: 'asc' },
  category: { value: (row) => row.category, first: 'asc' },
  close: { value: (row) => row.close },
  pct_chg: { value: (row) => row.pct_chg },
  ret20: { value: (row) => row.ret20 },
  ret60: { value: (row) => row.ret60 },
  rps60: { value: (row) => row.rps60 },
  scale: { value: (row) => row.scale },
  avg_amount: { value: (row) => row.avg_amount },
  flow20: { value: (row) => row.flow20 },
  volatility: { value: (row) => row.volatility, first: 'asc' },
}

function NumberField({
  label,
  value,
  onChange,
  placeholder,
  suffix,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  placeholder: string
  suffix?: string
}) {
  return (
    <label className="flex items-center gap-1.5">
      <span>{label}</span>
      <input
        type="number"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className="num w-[76px] border border-line bg-ink-850 px-1.5 py-[3px] text-[12px] text-fg outline-none focus:border-fg-dim"
      />
      {suffix && <span className="text-fg-dim">{suffix}</span>}
    </label>
  )
}

function Toggle({
  label,
  value,
  onChange,
}: {
  label: string
  value: boolean
  onChange: (value: boolean) => void
}) {
  return (
    <label className="flex cursor-pointer items-center gap-1.5">
      <input
        type="checkbox"
        checked={value}
        onChange={(event) => onChange(event.target.checked)}
        className="accent-current"
      />
      {label}
    </label>
  )
}

const NO_ROWS: EtfRow[] = []

/** 验证面板的参数选项（后端 `top` 上限 20、`days` 上限 240） */
const TRACK_TOP_ITEMS = [
  { key: '3', label: '3 只' },
  { key: '5', label: '5 只' },
  { key: '10', label: '10 只' },
]

const TRACK_HOLD_ITEMS = [
  { key: '5', label: '5 日' },
  { key: '20', label: '20 日' },
]

const TRACK_DAYS_ITEMS = [
  { key: '60', label: '60 日' },
  { key: '120', label: '120 日' },
  { key: '240', label: '240 日' },
]

const TRACK_DAY_SORTS: SortSpecs<EtfTrackDay> = {
  trade_date: { value: (row) => row.trade_date, first: 'desc' },
  entry_date: { value: (row) => row.entry_date, first: 'desc' },
  ret: { value: (row) => row.ret },
  hs300_ret: { value: (row) => row.hs300_ret },
  universe_ret: { value: (row) => row.universe_ret },
  excess_universe: { value: (row) => row.excess_universe },
}

const NO_TRACK_DAYS: EtfTrackDay[] = []

function StatBlock({ label, stat }: { label: string; stat: EtfTrackStat }) {
  return (
    <div className="min-w-[132px]">
      <div className="text-[11px] tracking-[0.06em] text-fg-dim">{label}</div>
      <div className={`num mt-0.5 text-[16px] font-semibold ${toneOf(stat.total)}`}>
        {fmtPct(stat.total, 1)}
      </div>
      <div className="num text-[11px] text-fg-dim">
        均值 {fmtPct(stat.mean)} · 胜率 {fmtNum(stat.win_rate, 1, '%')}
      </div>
    </div>
  )
}

export default function EtfPage() {
  const [tab, setTab] = useState<'screen' | 'categories' | 'verify'>('screen')
  const [preset, setPreset] = useState('momentum')
  const [scaleMin, setScaleMin] = useState('')
  const [amountMin, setAmountMin] = useState('')
  const [excludeMoney, setExcludeMoney] = useState<boolean | null>(null)
  const [excludeBond, setExcludeBond] = useState<boolean | null>(null)
  const [dedup, setDedup] = useState<boolean | null>(null)

  const [data, setData] = useState<EtfScreen | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [categories, setCategories] = useState<EtfCategoryRow[] | null>(null)
  const [categoryError, setCategoryError] = useState<string | null>(null)

  // --- 验证 Tab（ETF-4）---
  const [trackPreset, setTrackPreset] = useState('momentum')
  const [trackTop, setTrackTop] = useState(5)
  const [trackHold, setTrackHold] = useState(5)
  const [trackDays, setTrackDays] = useState(120)
  const [track, setTrack] = useState<EtfTrack | null>(null)
  const [trackError, setTrackError] = useState<string | null>(null)
  const [trackLoading, setTrackLoading] = useState(false)

  const params = useMemo<EtfScreenQuery>(() => {
    const query: EtfScreenQuery = { preset }
    if (scaleMin !== '' && Number.isFinite(Number(scaleMin))) query.scale_min = Number(scaleMin)
    // 成交额按「万」填，接口按元收
    if (amountMin !== '' && Number.isFinite(Number(amountMin))) {
      query.amount_min = Number(amountMin) * 1e4
    }
    if (excludeMoney !== null) query.exclude_money = excludeMoney
    if (excludeBond !== null) query.exclude_bond = excludeBond
    if (dedup !== null) query.dedup = dedup
    return query
  }, [preset, scaleMin, amountMin, excludeMoney, excludeBond, dedup])

  const changePreset = (value: string) => {
    setPreset(value)
    setScaleMin('')
    setAmountMin('')
    setExcludeMoney(null)
    setExcludeBond(null)
    setDedup(null)
  }

  useEffect(() => {
    if (tab !== 'screen') return
    let stale = false
    setError(null)
    api
      .etfScreen(params)
      .then((value) => {
        if (!stale) setData(value)
      })
      .catch((err: Error) => {
        if (!stale) setError(err.message)
      })
    return () => {
      stale = true
    }
  }, [tab, params])

  useEffect(() => {
    if (tab !== 'categories' || categories !== null) return
    let stale = false
    setCategoryError(null)
    api
      .etfCategories()
      .then((value) => {
        if (!stale) setCategories(value)
      })
      .catch((err: Error) => {
        if (!stale) setCategoryError(err.message)
      })
    return () => {
      stale = true
    }
  }, [tab, categories])

  useEffect(() => {
    if (tab !== 'verify') return
    let stale = false
    setTrackLoading(true)
    setTrackError(null)
    api
      .etfTrack(trackPreset, trackTop, trackHold, trackDays)
      .then((value) => {
        if (stale) return
        setTrack(value)
        setTrackLoading(false)
      })
      .catch((err: Error) => {
        if (stale) return
        setTrackError(err.message)
        setTrackLoading(false)
      })
    return () => {
      stale = true
    }
  }, [tab, trackPreset, trackTop, trackHold, trackDays])

  const [sort, rows] = useSort(data?.items ?? NO_ROWS, SCREEN_SORTS, { key: null })
  const [daySort, dayRows] = useSort(track?.rows ?? NO_TRACK_DAYS, TRACK_DAY_SORTS, {
    key: 'trade_date',
    dir: 'desc',
  })
  const asOf = data?.as_of ?? null

  /** 平均超额（对去重后等权）：后端只给逐日超额，均值在页面上算 */
  const excessMean = useMemo(() => {
    const values = (track?.rows ?? [])
      .map((row) => row.excess_universe)
      .filter((value): value is number => value != null)
    return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null
  }, [track])

  const chartOption = useMemo<ChartOption>(() => {
    if (!track || track.curve.length === 0) return {}
    const dates = track.curve.map((point) => fmtShortDate(point.trade_date))
    return {
      grid: { ...GRID, top: 30, bottom: 16 },
      legend: {
        ...LEGEND,
        top: 0,
        right: 0,
        data: ['策略', '510300', '去重后等权'],
      },
      tooltip: {
        ...TOOLTIP,
        trigger: 'axis' as const,
        formatter: (params: unknown) => {
          const items = params as { dataIndex: number }[]
          if (!items?.length) return ''
          const point = track.curve[items[0].dataIndex]
          return [
            point.trade_date,
            `● 策略：${fmtPct(point.strategy)}`,
            `● 510300：${fmtPct(point.hs300)}`,
            `● 去重后等权：${fmtPct(point.universe)}`,
          ].join('<br/>')
        },
      },
      xAxis: {
        type: 'category' as const,
        data: dates,
        axisLabel: { ...AXIS_LABEL, interval: Math.max(0, Math.floor(dates.length / 10)) },
        axisLine: AXIS_LINE,
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value' as const,
        scale: true,
        axisLabel: { ...AXIS_LABEL, formatter: '{value}%' },
        splitLine: SPLIT_LINE,
        axisLine: { show: false },
      },
      series: [
        {
          type: 'line' as const,
          name: '策略',
          data: track.curve.map((point) => point.strategy),
          symbol: 'none' as const,
          lineStyle: { width: 2, color: CHART.accent },
          itemStyle: { color: CHART.accent },
        },
        {
          type: 'line' as const,
          name: '510300',
          data: track.curve.map((point) => point.hs300),
          symbol: 'none' as const,
          lineStyle: { width: 1.4, color: SERIES_PALETTE[0] },
          itemStyle: { color: SERIES_PALETTE[0] },
        },
        {
          type: 'line' as const,
          name: '去重后等权',
          data: track.curve.map((point) => point.universe),
          symbol: 'none' as const,
          lineStyle: { width: 1.4, color: SERIES_PALETTE[2], type: 'dashed' as const },
          itemStyle: { color: SERIES_PALETTE[2] },
        },
      ],
    }
  }, [track])

  return (
    <Layout
      toolbar={
        <span className="num hidden text-[13px] text-fg-dim lg:inline">
          {asOf ? `${asOf} · 盘后快照` : '加载中…'}
        </span>
      }
    >
      <div className="space-y-4">
        <Panel
          title="ETF"
          meta={
            <span className="flex items-center gap-3">
              <Segmented
                value={tab}
                items={[
                  { key: 'screen', label: '筛选' },
                  { key: 'categories', label: '行业强弱' },
                  { key: 'verify', label: '验证' },
                ]}
                onChange={(value) => setTab(value as 'screen' | 'categories' | 'verify')}
              />
              <span className="num text-fg-dim">
                {tab === 'screen'
                  ? data
                    ? `命中 ${data.total} 只`
                    : '—'
                  : tab === 'categories'
                    ? categories
                      ? `${categories.length} 类`
                      : '—'
                    : track
                      ? `${track.rows.length} 个信号日`
                      : '—'}
              </span>
            </span>
          }
          delay={40}
        >
          <p className="border-b border-line-soft px-4 py-2 text-[12px] leading-relaxed text-fg-dim">
            盘后数据，截至 {asOf ?? '—'}；涨幅按
            <span className="text-fg-muted"> 前收复利 </span>
            （除息 / 份额折算已调整）；净申赎自 2026-09-21 起（份额从那天才有）；
            <span className="text-fg-muted"> 暂无折溢价数据，T+0 预设已排除 QDII </span>
            。规模 = 份额 × 收盘价，缺份额时显示「—」。
            <span className="ml-1">
              另见
              <Link to="/cb" className="ml-1 text-accent hover:underline">
                可转债
              </Link>
              （它的「日内 T+0」是同一用途的另一类品种）。
            </span>
          </p>

          {tab === 'screen' ? (
            <>
              <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-line-soft px-4 py-2 text-[12px] text-fg-dim">
                <Segmented value={preset} items={PRESET_ITEMS} onChange={changePreset} />
                <NumberField
                  label="规模≥"
                  value={scaleMin}
                  onChange={setScaleMin}
                  placeholder="不限"
                  suffix="亿"
                />
                <NumberField
                  label="均额≥"
                  value={amountMin}
                  onChange={setAmountMin}
                  placeholder="不限"
                  suffix="万"
                />
                <Toggle
                  label="剔除货币"
                  value={excludeMoney ?? Boolean(data?.filters.exclude_money)}
                  onChange={setExcludeMoney}
                />
                <Toggle
                  label="剔除债券"
                  value={excludeBond ?? Boolean(data?.filters.exclude_bond)}
                  onChange={setExcludeBond}
                />
                <Toggle
                  label="同指数去重"
                  value={dedup ?? (data ? data.filters.dedup !== false : true)}
                  onChange={setDedup}
                />
              </div>

              {error ? (
                <LoadState loading={false} error={error} />
              ) : data === null ? (
                <LoadState loading emptyText="加载中" />
              ) : data.items.length === 0 ? (
                <LoadState loading={false} emptyText="没有符合条件的 ETF" />
              ) : (
                <div className="overflow-x-auto">
                  <table className="grid-table">
                    <thead>
                      <tr>
                        <SortTh sortKey="name" align="left" {...sort}>
                          ETF
                        </SortTh>
                        <SortTh sortKey="category" align="left" {...sort}>
                          分类
                        </SortTh>
                        <SortTh sortKey="close" {...sort}>
                          价格
                        </SortTh>
                        <SortTh sortKey="pct_chg" {...sort}>
                          涨跌
                        </SortTh>
                        <SortTh sortKey="ret20" title="近 20 个交易日复利涨幅" {...sort}>
                          20 日
                        </SortTh>
                        <SortTh sortKey="ret60" title="近 60 个交易日复利涨幅" {...sort}>
                          60 日
                        </SortTh>
                        <SortTh sortKey="rps60" title="60 日涨幅在去重后股票类 ETF 里的分位" {...sort}>
                          RPS60
                        </SortTh>
                        <SortTh sortKey="scale" title="份额 × 收盘价" {...sort}>
                          规模
                        </SortTh>
                        <SortTh sortKey="avg_amount" title="20 日均成交额" {...sort}>
                          均额
                        </SortTh>
                        <SortTh sortKey="flow20" title="近 20 个交易日净申赎（份额 × 收盘价）" {...sort}>
                          20 日净申赎
                        </SortTh>
                        <SortTh sortKey="volatility" title="20 日收益率标准差 × √250" {...sort}>
                          波动率
                        </SortTh>
                        <th className="!text-left">标签</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row) => (
                        <tr key={row.code}>
                          <td className="!text-left">
                            <span className="text-fg">{row.name ?? row.code}</span>
                            <span className="num ml-2 text-[11px] text-fg-dim">{row.code}</span>
                          </td>
                          <td className="!text-left text-[12px] text-fg-muted">{row.category}</td>
                          <td className="num">{fmtNum(row.close, 3)}</td>
                          <td className={`num ${toneOf(row.pct_chg)}`}>{fmtPct(row.pct_chg)}</td>
                          <td className={`num ${toneOf(row.ret20)}`}>{fmtPct(row.ret20, 1)}</td>
                          <td className={`num ${toneOf(row.ret60)}`}>{fmtPct(row.ret60, 1)}</td>
                          <td className="num text-fg-muted">{fmtNum(row.rps60, 1)}</td>
                          <td className="num">
                            {row.scale == null ? '—' : `${fmtNum(row.scale, 1)} 亿`}
                          </td>
                          <td className="num text-fg-muted">{fmtAmount(row.avg_amount)}</td>
                          <td className={`num ${toneOf(row.flow20)}`}>{fmtAmount(row.flow20)}</td>
                          <td className="num text-fg-muted">{fmtNum(row.volatility, 1)}</td>
                          <td className="!text-left">
                            {row.labels.map((label) => (
                              <span
                                key={label}
                                className={`mr-1 whitespace-nowrap rounded border border-line-soft px-1 text-[11px] ${
                                  label === '创60日新高' ? 'text-danger' : 'text-fg-muted'
                                }`}
                              >
                                {label}
                              </span>
                            ))}
                            {row.risks.map((risk) => (
                              <span
                                key={risk}
                                className="mr-1 whitespace-nowrap rounded border border-line-soft px-1 text-[11px] text-fg-dim"
                                title="风险提示，不默认剔除"
                              >
                                {risk}
                              </span>
                            ))}
                            {row.labels.length === 0 && row.risks.length === 0 && (
                              <span className="text-fg-dim">—</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <p className="px-4 py-3 text-[12px] leading-relaxed text-fg-dim">
                预设口径：<span className="text-fg-muted">动量轮动</span> 只收股票类、去重，
                规模 ≥2 亿、均额 ≥5000 万、站上 MA20，按 RPS60 降序；
                <span className="text-fg-muted">趋势突破</span> 创 60 日新高且当日额 &gt; 20 日均额 ×1.5；
                <span className="text-fg-muted">资金流入</span> 20 日净申赎 ≥ 规模 5% 且 20 日涨幅 ≤0；
                <span className="text-fg-muted">红利低波</span> 分类是红利策略 / 银行，按波动率升序；
                <span className="text-fg-muted">T+0</span> 可回转品种，排除 QDII，按成交额降序。
                盘后候选，不是买卖建议。
              </p>
            </>
          ) : tab === 'categories' ? (
            categoryError ? (
              <LoadState loading={false} error={categoryError} />
            ) : categories === null ? (
              <LoadState loading emptyText="加载中" />
            ) : categories.length === 0 ? (
              <LoadState loading={false} emptyText="还没有分类数据" />
            ) : (
              <div className="overflow-x-auto">
                <table className="grid-table">
                  <thead>
                    <tr>
                      <th className="!text-left">分类</th>
                      <th>代表只数</th>
                      <th>RPS60 中位</th>
                      <th>20 日净申赎</th>
                    </tr>
                  </thead>
                  <tbody>
                    {categories.map((row) => (
                      <tr key={row.category}>
                        <td className="!text-left text-fg">{row.category}</td>
                        <td className="num text-fg-muted">{row.count}</td>
                        <td className="num">{fmtNum(row.rps60_median, 1)}</td>
                        <td className={`num ${toneOf(row.flow20)}`}>{fmtAmount(row.flow20)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-line-soft px-4 py-2 text-[12px] text-fg-dim">
                <Segmented value={trackPreset} items={PRESET_ITEMS} onChange={setTrackPreset} />
                <span className="flex items-center gap-1.5">
                  取前
                  <Segmented
                    value={String(trackTop)}
                    items={TRACK_TOP_ITEMS}
                    onChange={(value) => setTrackTop(Number(value))}
                  />
                </span>
                <span className="flex items-center gap-1.5">
                  持有
                  <Segmented
                    value={String(trackHold)}
                    items={TRACK_HOLD_ITEMS}
                    onChange={(value) => setTrackHold(Number(value))}
                  />
                </span>
                <span className="flex items-center gap-1.5">
                  回看
                  <Segmented
                    value={String(trackDays)}
                    items={TRACK_DAYS_ITEMS}
                    onChange={(value) => setTrackDays(Number(value))}
                  />
                </span>
              </div>

              {trackError ? (
                <LoadState loading={false} error={trackError} />
              ) : track === null || trackLoading ? (
                <LoadState loading emptyText="加载中（首次约 10 秒，之后切参数秒出）" />
              ) : track.rows.length === 0 ? (
                <LoadState loading={false} emptyText={track.note ?? '样本还不够'} />
              ) : (
                <>
                  <div className="flex flex-wrap items-start gap-x-8 gap-y-3 border-b border-line-soft px-4 py-3">
                    <StatBlock label={`策略累计（${track.rows.length} 个信号日）`} stat={track.stats.strategy} />
                    <StatBlock label="510300 累计" stat={track.stats.hs300} />
                    <StatBlock label="去重后等权累计" stat={track.stats.universe} />
                    <div className="min-w-[132px]">
                      <div className="text-[11px] tracking-[0.06em] text-fg-dim">平均超额（对等权）</div>
                      <div className={`num mt-0.5 text-[16px] font-semibold ${toneOf(excessMean)}`}>
                        {fmtPct(excessMean)}
                      </div>
                      <div className="num text-[11px] text-fg-dim">
                        最好 {fmtPct(track.stats.strategy.best)} · 最差{' '}
                        {fmtPct(track.stats.strategy.worst)}
                      </div>
                    </div>
                  </div>

                  {track.note && (
                    <p className="border-b border-line-soft px-4 py-2 text-[12px] leading-relaxed text-fg-dim">
                      {track.note}
                    </p>
                  )}

                  <EChart option={chartOption} height={280} />

                  <div className="overflow-x-auto">
                    <table className="grid-table">
                      <thead>
                        <tr>
                          <SortTh sortKey="trade_date" align="left" {...daySort}>
                            信号日
                          </SortTh>
                          <SortTh sortKey="entry_date" align="left" {...daySort}>
                            建仓
                          </SortTh>
                          <SortTh sortKey="ret" title="次日收盘买入、持有后收盘卖出的等权收益" {...daySort}>
                            策略
                          </SortTh>
                          <SortTh sortKey="hs300_ret" {...daySort}>
                            510300
                          </SortTh>
                          <SortTh sortKey="universe_ret" {...daySort}>
                            去重等权
                          </SortTh>
                          <SortTh sortKey="excess_universe" {...daySort}>
                            超额
                          </SortTh>
                          <th className="!text-left">当日名单（持有期收益）</th>
                        </tr>
                      </thead>
                      <tbody>
                        {dayRows.map((row) => (
                          <tr key={row.trade_date}>
                            <td className="num !text-left">{row.trade_date}</td>
                            <td className="num !text-left text-fg-dim">{row.entry_date ?? '—'}</td>
                            <td className={`num ${toneOf(row.ret)}`}>{fmtPct(row.ret)}</td>
                            <td className={`num ${toneOf(row.hs300_ret)}`}>{fmtPct(row.hs300_ret)}</td>
                            <td className={`num ${toneOf(row.universe_ret)}`}>
                              {fmtPct(row.universe_ret)}
                            </td>
                            <td className={`num ${toneOf(row.excess_universe)}`}>
                              {fmtPct(row.excess_universe)}
                            </td>
                            <td className="!text-left text-[12px] text-fg-muted">
                              {row.picks
                                .map((pick) => `${pick.name ?? pick.code} ${fmtPct(pick.ret, 1)}`)
                                .join(' · ')}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>

                  <p className="px-4 py-3 text-[12px] leading-relaxed text-fg-dim">
                    口径：信号日收盘出名单，<span className="text-fg-muted">次日收盘</span>
                    买入、持有 {trackHold} 个交易日后收盘卖出；只用当天及之前的数据
                    （去重代表按当时的 20 日均额选），没有未来函数。基准是 510300 与「同指数去重后
                    全体等权」，入场 / 出场日与被测策略相同。累计是
                    <span className="text-fg-muted">每天各持有一份、窗口相互重叠</span>
                    的复利，不是一笔可执行净值，也不含交易成本 —— 可执行的轮动净值（周 / 双周调仓、
                    扣万 0.5）见 <span className="num">scripts/backtest_etf.py</span>。
                  </p>
                </>
              )}
            </>
          )}
        </Panel>
      </div>
    </Layout>
  )
}
