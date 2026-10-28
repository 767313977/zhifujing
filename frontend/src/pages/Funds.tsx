import { Fragment, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { api, isNoDataError } from '../api/client'
import type {
  DdeBoard,
  DdeItem,
  DdeOrder,
  EtfFlowBoard,
  EtfFlowItem,
  EtfFlowOrder,
  EtfGroup,
  EtfIndustryBoard,
  EtfIndustryItem,
  FundFlowOverview,
  FundsSeries,
  FuturesSeries,
  InstitutionBoard,
  InstitutionItem,
  InstitutionOrder,
} from '../api/types'
import EChart from '../components/EChart'
import type { ChartOption } from '../components/EChart'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import SortTh from '../components/SortTh'
import StockLink from '../components/StockLink'
import {
  AXIS_LABEL,
  AXIS_LINE,
  CHART,
  GRID,
  LEGEND,
  SERIES_PALETTE,
  SPLIT_LINE,
  TOOLTIP,
} from '../lib/chart'
import { fmtAmount, fmtInt, fmtNum, fmtPct, fmtShortDate, toneOf } from '../lib/format'
import { useSort } from '../lib/sort'
import type { SortSpecs } from '../lib/sort'

/** 走势图回看的交易日数。与后端默认值一致（上限 250，够看半年）。 */
const SERIES_DAYS = 60

/** 单只榜的行数，与后端默认值一致（上限 200）。 */
const BOARD_LIMIT = 30

/**
 * 行业榜的条数。分类一共 30 多个，这里**要取够全部** ——
 * 和单只榜不同，行业榜的条数是有限的，一旦截断就必然丢掉一头：
 * 按「净赎回」排序时被切掉的是末尾，而那恰恰是净申购最多的几个行业，
 * 是这个榜最该看的东西。
 */
const INDUSTRY_LIMIT = 60

const ETF_GROUPS: { key: EtfGroup; label: string }[] = [
  { key: 'industry', label: '按行业' },
  { key: 'fund', label: '按单只' },
]

const ETF_ORDERS: { key: EtfFlowOrder; label: string }[] = [
  { key: 'inflow', label: '净申购' },
  { key: 'outflow', label: '净赎回' },
  { key: 'amount', label: '成交额' },
]

const INSTITUTION_ORDERS: { key: InstitutionOrder; label: string }[] = [
  { key: 'net', label: '净买额' },
  { key: 'buy', label: '买入额' },
  { key: 'sell', label: '卖出额' },
]

const DDE_ORDERS: { key: DdeOrder; label: string }[] = [
  { key: 'inflow', label: '流入前列' },
  { key: 'outflow', label: '流出前列' },
]

/** DDE 榜的排序口径。
 *
 *  DDE 这一栏**不给「数值大小」之外的选项**：它本身就是 iFinD 的 5日资金指标，
 *  再按「净流入额」排会与它高度重合。 */
const DDE_SORTS: SortSpecs<DdeItem> = {
  code: { value: (item) => item.code, first: 'asc' },
  name: { value: (item) => item.name, first: 'asc' },
  pct_chg: { value: (item) => item.pct_chg },
  close: { value: (item) => item.close },
  dde: { value: (item) => item.dde },
}

/** ETF 单只榜的排序口径 */
const ETF_FLOW_SORTS: SortSpecs<EtfFlowItem> = {
  code: { value: (item) => item.code, first: 'asc' },
  name: { value: (item) => item.name, first: 'asc' },
  pct_chg: { value: (item) => item.pct_chg },
  amount: { value: (item) => item.amount },
  share_delta: { value: (item) => item.share_delta },
  net_inflow: { value: (item) => item.net_inflow },
}

/** ETF 行业榜的排序口径 */
const ETF_INDUSTRY_SORTS: SortSpecs<EtfIndustryItem> = {
  category: { value: (item) => item.category, first: 'asc' },
  fund_count: { value: (item) => item.fund_count },
  pct_chg: { value: (item) => item.pct_chg },
  amount: { value: (item) => item.amount },
  net_inflow: { value: (item) => item.net_inflow },
}

/** 机构席位榜的排序口径 */
const INSTITUTION_SORTS: SortSpecs<InstitutionItem> = {
  code: { value: (item) => item.code, first: 'asc' },
  name: { value: (item) => item.name, first: 'asc' },
  pct_chg: { value: (item) => item.pct_chg },
  buy_count: { value: (item) => item.buy_count },
  sell_count: { value: (item) => item.sell_count },
  buy_amount: { value: (item) => item.buy_amount },
  sell_amount: { value: (item) => item.sell_amount },
  net_amount: { value: (item) => item.net_amount },
  reason: { value: (item) => item.reason, first: 'asc' },
}

/**
 * 金额带符号。`fmtAmount` 只给数值本身，看不出增减方向，
 * 而资金面最要紧的信息恰恰是「多进来了还是少进来了」，所以正数手工补一个 +。
 */
function signedAmount(value: number | null | undefined): string {
  if (value == null) return '—'
  return `${value > 0 ? '+' : ''}${fmtAmount(value)}`
}

/** 整数带符号：期指是「手」，不走金额格式。 */
function signedInt(value: number): string {
  return `${value > 0 ? '+' : ''}${fmtInt(value)}`
}

/**
 * 「面板的数据日 ≠ 页面日期」时要标出来的日期，否则为 null。
 *
 * ETF 份额与 DDE 的落库日期都由来源决定、通常**比请求日早**（份额 T+1 才更新、
 * DDE 收盘后才逐步发布），所以后端会回落到最近有数据的一天并把实际日期返回。
 * 只回落不标注会出现「面板展示的是前一天的数、页面顶部写着今天」——
 * 那比空白更容易误读，所以两处都要标。
 */
function staleDateNotice(
  dataDate: string | null | undefined,
  pageDate: string | null | undefined,
  loading: boolean,
): string | null {
  if (loading || !dataDate || !pageDate || dataDate === pageDate) return null
  return dataDate
}

/** 区间说明：`09-01 ~ 09-19 · 14 个交易日` */
function rangeLabel(dates: string[]): string {
  if (dates.length === 0) return '暂无数据'
  const last = dates[dates.length - 1]
  return `${fmtShortDate(dates[0])} ~ ${fmtShortDate(last)} · ${dates.length} 个交易日`
}

export default function Funds() {
  const [date, setDate] = useState<string | null>(null)
  const [dates, setDates] = useState<string[]>([])
  const [overview, setOverview] = useState<FundFlowOverview | null>(null)
  const [overviewLoading, setOverviewLoading] = useState(true)
  const [series, setSeries] = useState<FundsSeries | null>(null)
  const [seriesLoading, setSeriesLoading] = useState(true)
  const [futuresSeries, setFuturesSeries] = useState<FuturesSeries | null>(null)
  const [futuresSeriesLoading, setFuturesSeriesLoading] = useState(true)
  const [etfGroup, setEtfGroup] = useState<EtfGroup>('industry')
  const [etfOrder, setEtfOrder] = useState<EtfFlowOrder>('inflow')
  const [etf, setEtf] = useState<EtfFlowBoard | null>(null)
  const [etfIndustry, setEtfIndustry] = useState<EtfIndustryBoard | null>(null)
  const [etfLoading, setEtfLoading] = useState(true)
  const [instOrder, setInstOrder] = useState<InstitutionOrder>('net')
  const [institutions, setInstitutions] = useState<InstitutionBoard | null>(null)
  const [instLoading, setInstLoading] = useState(true)
  const [ddeOrder, setDdeOrder] = useState<DdeOrder>('inflow')
  const [dde, setDde] = useState<DdeBoard | null>(null)
  const [ddeLoading, setDdeLoading] = useState(true)
  /**
   * 五个区块**各自的**失败原因。
   *
   * 为什么不能共用一个 `error`（2026-10-22 体检 #5）：这五条请求互相独立，共用一份时
   * 后到的失败会覆盖先到的，用户只看得到一条、也不知道是哪一块；而且这份 state 原先
   * 只有用户手点 ✕ 才会清 —— 切日期后数据都重新取成功了，红条还挂着上一次的失败。
   * 现在每块自己记、每次取数开头清掉，失败只影响自己那一块。
   */
  const [overviewError, setOverviewError] = useState<string | null>(null)
  const [seriesError, setSeriesError] = useState<string | null>(null)
  const [futuresSeriesError, setFuturesSeriesError] = useState<string | null>(null)
  const [etfError, setEtfError] = useState<string | null>(null)
  const [instError, setInstError] = useState<string | null>(null)
  const [ddeError, setDdeError] = useState<string | null>(null)

  // 日期下拉的候选来自交易日历（与其它页共用一个接口），不写死天数
  useEffect(() => {
    api
      .dates()
      .then(setDates)
      .catch(() => setDates([]))
  }, [])

  useEffect(() => {
    let stale = false
    setOverviewLoading(true)
    setOverviewError(null)
    api
      .fundsOverview(date)
      .then((data) => {
        if (!stale) setOverview(data)
      })
      .catch((err: Error) => {
        if (stale) return
        setOverview(null)
        // 404 = 这一天还没有资金面数据（非交易日/尚未采集）→ 空态；
        // 只有 5xx/超时/断网才显示「接口出错」（2026-10-23 统一 404 语义）
        setOverviewError(isNoDataError(err) ? null : err.message)
      })
      .finally(() => {
        if (!stale) setOverviewLoading(false)
      })
    // 切日期时旧请求可能后返回、用旧数据盖掉新数据，所以每个区块都加 stale 守卫
    return () => {
      stale = true
    }
  }, [date])

  useEffect(() => {
    let stale = false
    setSeriesLoading(true)
    setSeriesError(null)
    api
      .fundsSeries(SERIES_DAYS, date)
      .then((data) => {
        if (!stale) setSeries(data)
      })
      .catch((err: Error) => {
        if (stale) return
        setSeries(null)
        setSeriesError(isNoDataError(err) ? null : err.message)
      })
      .finally(() => {
        if (!stale) setSeriesLoading(false)
      })
    return () => {
      stale = true
    }
  }, [date])

  useEffect(() => {
    let stale = false
    setFuturesSeriesLoading(true)
    setFuturesSeriesError(null)
    api
      .fundsFuturesSeries(SERIES_DAYS, date)
      .then((data) => {
        if (!stale) setFuturesSeries(data)
      })
      .catch((err: Error) => {
        if (stale) return
        setFuturesSeries(null)
        setFuturesSeriesError(isNoDataError(err) ? null : err.message)
      })
      .finally(() => {
        if (!stale) setFuturesSeriesLoading(false)
      })
    return () => {
      stale = true
    }
  }, [date])

  useEffect(() => {
    let stale = false
    setEtfLoading(true)
    setEtfError(null)
    // 先清空：旧日期的榜留在表里会被当成新日期的数据看。
    // 两个视图的数据都清 —— 来回切视图时留着另一个视图的旧数据，会闪一下错的内容
    setEtf(null)
    setEtfIndustry(null)
    // 两个视图走两个接口：形状不同（一个是单只、一个是分类汇总），
    // 混在一个响应里会让类型和渲染都变别扭
    const load =
      etfGroup === 'industry'
        ? api.fundsEtfIndustry(etfOrder, INDUSTRY_LIMIT, date).then((data) => {
            if (!stale) setEtfIndustry(data)
          })
        : api.fundsEtf(etfOrder, BOARD_LIMIT, date).then((data) => {
            if (!stale) setEtf(data)
          })
    load
      .catch((err: Error) => {
        if (!stale) setEtfError(isNoDataError(err) ? null : err.message)
      })
      .finally(() => {
        if (!stale) setEtfLoading(false)
      })
    return () => {
      stale = true
    }
  }, [etfGroup, etfOrder, date])

  useEffect(() => {
    let stale = false
    setInstLoading(true)
    setInstError(null)
    setInstitutions(null)
    api
      .fundsInstitutions(instOrder, BOARD_LIMIT, date)
      .then((data) => {
        if (!stale) setInstitutions(data)
      })
      .catch((err: Error) => {
        if (!stale) setInstError(isNoDataError(err) ? null : err.message)
      })
      .finally(() => {
        if (!stale) setInstLoading(false)
      })
    return () => {
      stale = true
    }
  }, [instOrder, date])

  useEffect(() => {
    let stale = false
    setDdeLoading(true)
    setDdeError(null)
    setDde(null)
    api
      .fundsDde(ddeOrder, BOARD_LIMIT, date)
      .then((data) => {
        if (!stale) setDde(data)
      })
      .catch((err: Error) => {
        if (!stale) setDdeError(isNoDataError(err) ? null : err.message)
      })
      .finally(() => {
        if (!stale) setDdeLoading(false)
      })
    return () => {
      stale = true
    }
  }, [ddeOrder, date])

  const marginOption = useMemo<ChartOption>(
    () => (series && series.dates.length > 0 ? buildMarginOption(series) : {}),
    [series],
  )

  const hsgtOption = useMemo<ChartOption>(
    () => (series && series.dates.length > 0 ? buildHsgtOption(series) : {}),
    [series],
  )

  const futuresOption = useMemo<ChartOption>(
    () => (futuresSeries ? buildFuturesOption(futuresSeries) : {}),
    [futuresSeries],
  )

  // 下面四张卡片的小字计算先落地成常量：JSX 里写几层可选链会读不清，
  // 而且顺势把「算不出来」统一收敛成 null，交给格式化函数输出占位符
  // 期指净空与主力净流入（2026-10-19 加，放概览最前）
  const futures = overview?.futures_net_short ?? []
  const futuresTotal = futures.length
    ? futures.reduce((sum, item) => sum + item.net_short, 0)
    : null
  const futuresDetail = futures.length
    ? futures
        .map((item) => `${item.variety} ${(item.net_short / 10000).toFixed(1)}万`)
        .join(' · ')
    : '暂无数据'
  // 净空走势窗口里有数据吗。`futures_position` 从 2026-10-16 才开始采，
  // 更早的日期全是 None —— 那种情况要给空态，不能画一张全是断点的图
  const futuresHasSeries = futuresSeries?.total.some((value) => value != null) ?? false
  const mainInflow = overview?.main_net_inflow ?? null
  const mainInflowChange =
    mainInflow != null && overview?.main_net_inflow_prev != null
      ? mainInflow - overview.main_net_inflow_prev
      : null
  // 期指明细按「中信 / 主要玩家」两组分开，与第三方那张图的口径一致
  const futuresGroups = [
    {
      label: '中信期货',
      change: futures.reduce((sum, item) => sum + item.citic_change, 0),
      total: futures.reduce((sum, item) => sum + item.citic_net_short, 0),
      rows: futures.map((item) => ({
        name: item.name,
        change: item.citic_change,
        net: item.citic_net_short,
      })),
    },
    {
      label: '主要玩家（前20）',
      change: futures.reduce((sum, item) => sum + item.change, 0),
      total: futuresTotal ?? 0,
      rows: futures.map((item) => ({
        name: item.name,
        change: item.change,
        net: item.net_short,
      })),
    },
  ]
  const financingTotal = overview?.financing_total ?? null
  const buyTotal = overview?.financing_buy_total ?? null
  const financingChange = overview?.financing_change ?? null
  const hsgtTurnover = overview?.hsgt_turnover ?? null
  const hsgtPrev = overview?.hsgt_turnover_prev ?? null
  const hsgtChange =
    hsgtTurnover != null && hsgtPrev != null ? hsgtTurnover - hsgtPrev : null
  const institutionNet = overview?.institution_net ?? null
  // 两融的实际数据日。两融由交易所各自发布、常比页面日期晚一天，后端会回落到
  // 最近一个「沪深都齐全」的交易日（见 api/funds._latest_complete_margin_day），
  // 回落之后必须把日期标出来，否则会把昨天的两融读成今天的
  const marginDate = overview?.margin_date ?? null

  // 两个视图的 board 形状不同（单只明细 / 分类汇总），但「有没有可比基准」
  // 「有几条」「基准日」这三件事都要用 —— 先收敛成同一组变量，
  // 免得 JSX 里到处写三目，那种写法最容易在两处写岔
  const etfBoard = etfGroup === 'industry' ? etfIndustry : etf
  const etfHasPrev = etfBoard?.has_prev
  const etfPrevDate = etfBoard?.prev_date ?? null
  const etfCount = etfBoard?.items.length ?? 0
  const etfNote =
    etfGroup === 'industry'
      ? '净申赎 = 该行业全部 ETF 的份额变化 × 收盘价求和（份额单位不可加，所以只加金额；不含货币基金）'
      : '净申赎 = 份额变化 × 当日收盘价（近似当日净值），正数为净申购（不含货币基金）'
  // ETF 份额常在 T+1 才更新，后端会回落到「最近有份额的那一天」，所以榜上的数据日
  // 可能比页面顶部选的日期早一天 —— 这本身正常，但必须标出来，
  // 否则两个日期对着看会把前一天的申赎当成今天的
  const etfDateNotice = staleDateNotice(
    etfBoard?.trade_date,
    overview?.trade_date ?? date,
    etfLoading,
  )
  // DDE 同理：扫描数据也是收盘后才齐，后端同样回落到最近有数据的一天
  const ddeDateNotice = staleDateNotice(dde?.trade_date, overview?.trade_date ?? date, ddeLoading)

  const toolbar = (
    <>
      <select
        value={date ?? ''}
        onChange={(event) => setDate(event.target.value || null)}
        className="num border border-line bg-ink-900 px-2 py-[3px] text-[13px] text-fg outline-none focus:border-fg-dim"
      >
        <option value="">最新</option>
        {/* 倒序渲染：最近的排最上面。升序时展开要一路滚到底才够得着昨天 */}
        {[...dates].reverse().map((item) => (
          <option key={item} value={item}>
            {item}
          </option>
        ))}
      </select>
      {/* 下拉选「最新」时它显示的是空值，这里把后端解析出的实际交易日亮出来 */}
      <span className="num text-[13px] text-fg-muted">
        {overviewLoading ? '加载中…' : (overview?.trade_date ?? '—')}
      </span>
    </>
  )

  return (
    <Layout toolbar={toolbar}>
      <div className="space-y-4">
        {/* ---- 概览 ---- */}
        <div className="rise grid grid-cols-1 gap-px border border-line-soft bg-line-soft sm:grid-cols-2 xl:grid-cols-3">
          {overviewLoading ? (
            <div className="bg-ink-900 px-4 py-8 text-center text-[14px] text-fg-dim sm:col-span-2 xl:col-span-3">
              加载中…
            </div>
          ) : overviewError ? (
            // 失败态：概览六张卡原来会把「取不到」显示成一排「—」/「暂无数据」，
            // 与「今天这些指标真的都没有」分不开（2026-10-22 体检 #5）
            <div className="bg-ink-900 px-4 py-8 text-center text-[14px] text-danger sm:col-span-2 xl:col-span-3">
              概览取数失败：{overviewError}
              <div className="mt-1 text-[12px] text-fg-dim">
                这是接口出错，不是「今天这些指标都没有值」；下面各区块不受影响
              </div>
            </div>
          ) : (
            <>
              <MetricCard
                label="期指净空（前20）"
                value={futuresTotal == null ? '—' : `${fmtInt(futuresTotal)} 手`}
                muted={futuresTotal == null}
                sub={futuresDetail}
                // 中金所 16:00 才发布，日期可能早于页面选中的交易日
                note={`${overview?.futures_date ?? '—'} 收盘 · 前20席位合计`}
              />
              <MetricCard
                label="主力净流入"
                value={signedAmount(mainInflow)}
                tone={toneOf(mainInflow)}
                muted={mainInflow == null}
                sub={
                  mainInflowChange == null ? (
                    '无上一交易日数据'
                  ) : (
                    <span className={toneOf(mainInflowChange)}>
                      较上日 {signedAmount(mainInflowChange)}
                    </span>
                  )
                }
                note="全市场 DDE 汇总 · iFinD 口径"
              />
              <MetricCard
                label="两融余额"
                // 两融由交易所各自发布、常比页面日期晚一天，后端回落到最近一个
                // 「沪深都齐全」的日子并把实际日期带回来。缺数据时说「暂无数据」，
                // 写成 0 会读成两融一天归零
                value={financingTotal == null ? '暂无数据' : fmtAmount(financingTotal)}
                muted={financingTotal == null}
                sub={
                  <>
                    沪 {fmtAmount(overview?.sh?.financing_balance)} / 深{' '}
                    {fmtAmount(overview?.sz?.financing_balance)}
                    {financingChange != null && (
                      <span className={`ml-2 ${toneOf(financingChange)}`}>
                        较上日 {signedAmount(financingChange)}
                      </span>
                    )}
                  </>
                }
                note={`${marginDate ?? '—'} · 沪深合计`}
              />
              <MetricCard
                label="融资买入额"
                // 与上面同一口径：沪深都齐才给合计，数据日同样可能早于页面日期
                value={buyTotal == null ? '暂无数据' : fmtAmount(buyTotal)}
                muted={buyTotal == null}
                sub="两市合计 · 当日买入"
                note={`${marginDate ?? '—'} · 沪深合计`}
              />
              <MetricCard
                label="沪深股通成交"
                value={fmtAmount(hsgtTurnover)}
                muted={hsgtTurnover == null}
                sub={
                  hsgtChange == null ? (
                    '无上一交易日数据'
                  ) : (
                    <span className={toneOf(hsgtChange)}>较上日 {signedAmount(hsgtChange)}</span>
                  )
                }
                // 这块最容易被误读成「北向净流入」，口径必须写在脸上
                note="仅成交总额 · 官方已停披露买卖方向"
              />
              <MetricCard
                label="机构席位净买额"
                value={fmtAmount(institutionNet)}
                tone={toneOf(institutionNet)}
                sub={`${fmtInt(overview?.institution_count)} 只股票上机构榜`}
              />
            </>
          )}
        </div>

        {/* ---- 期指持仓明细：中信 / 主要玩家两组 ---- */}
        {futures.length > 0 && (
          <Panel
            title="期指持仓（净空，手）"
            meta={
              <span className="num text-fg-muted">
                {overview?.futures_date ?? '—'} 收盘
              </span>
            }
            delay={60}
          >
            <div className="grid grid-cols-1 gap-px bg-line-soft sm:grid-cols-2">
              {futuresGroups.map((group) => (
                <div key={group.label} className="bg-ink-900 px-4 py-3">
                  <div className="mb-2 text-[13px] text-fg-muted">
                    {group.label}
                  </div>
                  <div className="space-y-1">
                    {group.rows.map((row) => (
                      <div
                        key={row.name}
                        className="flex items-baseline justify-between gap-4 text-[13px]"
                      >
                        <span>{row.name}</span>
                        <span className="num">
                          <span className={toneOf(row.change)}>
                            {signedInt(row.change)}
                          </span>
                          <span className="ml-3">{fmtInt(row.net)}</span>
                        </span>
                      </div>
                    ))}
                  </div>
                  <div className="mt-2 flex items-baseline justify-between border-t border-line-soft pt-2 text-[13px]">
                    <span className="text-fg-muted">今日累计</span>
                    <span className={`num ${toneOf(group.change)}`}>
                      {signedInt(group.change)}
                    </span>
                  </div>
                  <div className="flex items-baseline justify-between text-[13px]">
                    <span className="text-fg-muted">净空单总数</span>
                    <span className="num">{fmtInt(group.total)}</span>
                  </div>
                </div>
              ))}
            </div>
          </Panel>
        )}

        {/* ---- 期指净空走势：上面那张明细卡的曲线版 ---- */}
        <Panel
          title="期指净空走势（前 20 净空，手）"
          meta={
            <span className="num">
              {futuresSeriesLoading ? '加载中…' : rangeLabel(futuresSeries?.dates ?? [])}
            </span>
          }
          delay={70}
        >
          {futuresSeriesLoading ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">加载中…</div>
          ) : futuresSeriesError ? (
            <div className="px-4 py-10 text-center text-[14px] text-danger">
              净空走势取数失败：{futuresSeriesError}
            </div>
          ) : futuresHasSeries && futuresSeries ? (
            <div className="px-2 pt-2">
              <EChart option={futuresOption} height={280} />
              <div className="px-2 pb-2 pt-1 text-[13px] text-fg-dim">
                净空 = 空单 − 多单 · 合计只在四个品种当天都齐时才给 · 断点 = 当天没有数据，不是 0
                （该数据从 2026-10-16 开始采集）
              </div>
            </div>
          ) : (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              暂无净空走势（该数据从 2026-10-16 开始采集）
            </div>
          )}
        </Panel>

        {/* ---- 走势图 ---- */}
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <Panel
            title="两融走势"
            meta={
              <span className="num">
                {seriesLoading ? '加载中…' : rangeLabel(series?.dates ?? [])}
              </span>
            }
            delay={80}
          >
            {seriesLoading ? (
              <div className="px-4 py-10 text-center text-[14px] text-fg-dim">加载中…</div>
            ) : seriesError ? (
              // 两张走势图是同一个请求，共用这一份失败原因（2026-10-22 体检 #5）
              <div className="px-4 py-10 text-center text-[14px] text-danger">
                走势取数失败：{seriesError}
              </div>
            ) : series && series.dates.length > 0 ? (
              <div className="px-2 pt-2">
                <EChart option={marginOption} height={280} />
                <div className="px-2 pb-2 pt-1 text-[13px] text-fg-dim">
                  左轴融资余额 / 右轴融资买入额 · 断点是当日深市未披露（两市合计给不出来），不是 0
                </div>
              </div>
            ) : (
              <div className="px-4 py-10 text-center text-[14px] text-fg-dim">当日无数据</div>
            )}
          </Panel>

          <Panel
            title="北向成交额走势"
            meta={
              <span className="num">
                {seriesLoading ? '加载中…' : rangeLabel(series?.dates ?? [])}
              </span>
            }
            delay={120}
          >
            {seriesLoading ? (
              <div className="px-4 py-10 text-center text-[14px] text-fg-dim">加载中…</div>
            ) : seriesError ? (
              <div className="px-4 py-10 text-center text-[14px] text-danger">
                走势取数失败：{seriesError}
              </div>
            ) : series && series.dates.length > 0 ? (
              <div className="px-2 pt-2">
                <EChart option={hsgtOption} height={280} />
                <div className="px-2 pb-2 pt-1 text-[13px] text-fg-dim">
                  只有成交总额，不含买卖方向 —— 它是活跃度指标，不是净流入
                </div>
              </div>
            ) : (
              <div className="px-4 py-10 text-center text-[14px] text-fg-dim">当日无数据</div>
            )}
          </Panel>
        </div>

        {/* ---- ETF 申赎排行 ---- */}
        <Panel
          title="ETF 申赎排行"
          meta={
            <span className="flex flex-wrap items-center justify-end gap-x-2 gap-y-1">
              <Toggle options={ETF_GROUPS} value={etfGroup} onChange={setEtfGroup} />
              <Toggle options={ETF_ORDERS} value={etfOrder} onChange={setEtfOrder} />
              {/* ETF 份额 T+1 才更新，数据日可能比页面选的日期早一天 —— 如实标出来 */}
              {etfDateNotice && (
                <span className="num text-fg-dim">数据日期 {etfDateNotice}</span>
              )}
              {/* 没有可比基准时不显示条数：那时的「0 只」会被读成
                  「今天没有 ETF 被申赎」，而事实是算不出来 */}
              {etfHasPrev !== false && (
                <span className="num">
                  {etfLoading
                    ? '加载中…'
                    : etfGroup === 'industry'
                      ? `${etfCount} 个行业 · 点列头排序`
                      : `${etfCount} 只 · 点列头排序`}
                </span>
              )}
            </span>
          }
          delay={160}
        >
          {etfLoading ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">加载中…</div>
          ) : etfError ? (
            // 失败态：与下面「还没有上一交易日的份额数据」「当日无数据」分开
            //（2026-10-22 体检 #5）
            <div className="px-4 py-10 text-center text-[14px] text-danger">
              ETF 申赎取数失败：{etfError}
            </div>
          ) : etfHasPrev === false ? (
            // 首次采集只有当天份额，没有可比的基准 —— 这是「没得比」，
            // 与「今天没人申赎」完全不同，不能共用一句空态文案
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              还没有上一交易日的份额数据，无法计算净申赎；等下一次采集后再看
            </div>
          ) : etfCount > 0 ? (
            <>
              <div className="border-b border-line-soft px-3 py-1.5 text-[13px] text-fg-dim">
                {etfNote}
                {etfPrevDate && ` · 对比基准 ${etfPrevDate}`}
              </div>
              {etfGroup === 'industry' && etfIndustry ? (
                <EtfIndustryTable items={etfIndustry.items} />
              ) : etf ? (
                <EtfTable items={etf.items} />
              ) : null}
            </>
          ) : (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">当日无数据</div>
          )}
        </Panel>

        {/* ---- 机构席位排行 ---- */}
        <Panel
          title="机构席位排行"
          meta={
            <span className="flex flex-wrap items-center justify-end gap-x-2 gap-y-1">
              <Toggle options={INSTITUTION_ORDERS} value={instOrder} onChange={setInstOrder} />
              <span className="num">
                {instLoading ? '加载中…' : `${institutions?.total ?? 0} 只 · 点列头排序`}
              </span>
            </span>
          }
          delay={200}
        >
          {instLoading ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">加载中…</div>
          ) : instError ? (
            <div className="px-4 py-10 text-center text-[14px] text-danger">
              机构席位排行取数失败：{instError}
            </div>
          ) : institutions && institutions.items.length > 0 ? (
            <InstitutionTable items={institutions.items} />
          ) : (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">当日无数据</div>
          )}
        </Panel>

        {/* ---- DDE 排名 ---- */}
        <Panel
          title="DDE 排名"
          meta={
            <span className="flex flex-wrap items-center justify-end gap-x-2 gap-y-1">
              <Toggle options={DDE_ORDERS} value={ddeOrder} onChange={setDdeOrder} />
              {/* 扫描数据收盘后才齐、后端会回落到最近有数据的一天 —— 如实标出来 */}
              {ddeDateNotice && (
                <span className="num text-fg-dim">数据日期 {ddeDateNotice}</span>
              )}
              <span className="num">
                {ddeLoading ? '加载中…' : `${dde?.total ?? 0} 只 · 点列头排序`}
              </span>
            </span>
          }
          delay={240}
        >
          {ddeLoading ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">加载中…</div>
          ) : ddeError ? (
            // 失败态：与「还没有 DDE 数据（当天扫描没跑到）」分开（2026-10-22 体检 #5）
            <div className="px-4 py-10 text-center text-[14px] text-danger">
              DDE 排名取数失败：{ddeError}
            </div>
          ) : dde && dde.items.length > 0 ? (
            <>
              <div className="border-b border-line-soft px-3 py-1.5 text-[13px] text-fg-dim">
                5日DDE = iFinD 区间dde大单净额，与个股页那一栏是同一个数 ·
                数据来自每天采集链末尾的全市场扫描，打开页面不花配额 ·
                涨跌幅与收盘价取日线，池外的票显示为「—」
              </div>
              <DdeTable items={dde.items} />
            </>
          ) : (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              还没有 DDE 数据：它由每天的采集链在收盘后扫描全市场写入；当天扫描若还没跑到，明天再看
            </div>
          )}
        </Panel>
      </div>
    </Layout>
  )
}

/** 概览卡片。`muted` 供「数值本身缺失」时降级成文字说明，避免与真实数字同等醒目。 */
function MetricCard({
  label,
  value,
  sub,
  note,
  tone = 'text-fg',
  muted = false,
}: {
  label: string
  value: string
  /** 数值下方的小字补充（沪/深拆分、较上日变化、上榜家数等） */
  sub?: ReactNode
  /** 口径提醒，用强调色标出来 */
  note?: string
  tone?: string
  muted?: boolean
}) {
  return (
    <div className="bg-ink-900 px-4 py-3">
      <div className="text-[13px] tracking-[0.1em] text-fg-dim">{label}</div>
      <div
        className={`num mt-1 leading-tight ${
          muted ? 'text-[14px] text-fg-muted' : `text-[19px] font-semibold ${tone}`
        }`}
      >
        {value}
      </div>
      {sub != null && <div className="num mt-1 text-[13px] text-fg-muted">{sub}</div>}
      {note && <div className="mt-1 text-[13px] text-accent">{note}</div>}
    </div>
  )
}

/** 排序口径切换。与「板块题材」页的分类切换同款，做成受控组件复用。 */
function Toggle<T extends string>({
  options,
  value,
  onChange,
}: {
  options: { key: T; label: string }[]
  value: T
  onChange: (key: T) => void
}) {
  return (
    <span className="flex items-stretch border border-line">
      {options.map((item) => (
        <button
          key={item.key}
          type="button"
          onClick={() => onChange(item.key)}
          className={[
            'px-2 py-[3px] text-[13px] transition-colors',
            value === item.key ? 'bg-ink-700 text-fg' : 'text-fg-muted hover:text-fg',
          ].join(' ')}
        >
          {item.label}
        </button>
      ))}
    </span>
  )
}

function EtfTable({ items }: { items: EtfFlowItem[] }) {
  // 首屏不排，保持后端顺序（已按当前口径排序）；点列头才排。
  // ⚠️ 排的只是**后端返回的这批**（默认净申赎前 30 只），不是全市场 ——
  // 想看「成交额最大的 ETF」要把上面的口径切到「成交额」
  const [sort, shown] = useSort(items, ETF_FLOW_SORTS, { key: null })

  return (
    <div className="max-h-[440px] overflow-auto">
      <table className="grid-table">
        <thead>
          <tr>
            <SortTh sortKey="code" {...sort}>代码</SortTh>
            <SortTh sortKey="name" align="left" {...sort}>名称</SortTh>
            <SortTh sortKey="pct_chg" {...sort}>涨跌幅</SortTh>
            <SortTh sortKey="amount" {...sort}>成交额</SortTh>
            <SortTh sortKey="share_delta" {...sort}>份额变化</SortTh>
            <SortTh sortKey="net_inflow" {...sort}>净申赎</SortTh>
          </tr>
        </thead>
        <tbody>
          {shown.map((item) => (
            <tr
              key={item.code}
              // 表格列宽有限，份额与成交额的绝对量看不到，放进行 tooltip 里补齐
              title={[
                `${item.name ?? ''} ${item.code}`,
                `成交额 ${fmtAmount(item.amount)}`,
                `当日份额 ${fmtAmount(item.shares)} 份`,
                `份额变化 ${signedAmount(item.share_delta)} 份`,
                `净申赎 ${signedAmount(item.net_inflow)}`,
              ].join('\n')}
            >
              <td>
                <span className="num text-fg-muted">{item.code}</span>
              </td>
              <td className="!text-left">
                <span className="text-fg">{item.name ?? '—'}</span>
              </td>
              <td>
                <span className={`num ${toneOf(item.pct_chg)}`}>{fmtPct(item.pct_chg)}</span>
              </td>
              <td>
                <span className="num text-fg-muted">{fmtAmount(item.amount)}</span>
              </td>
              <td>
                {/* 份额变化单位是「份」，用同一套亿/万换算保持可读 */}
                <span className={`num ${toneOf(item.share_delta)}`}>
                  {fmtAmount(item.share_delta)}
                </span>
              </td>
              <td>
                <span className={`num font-medium ${toneOf(item.net_inflow)}`}>
                  {signedAmount(item.net_inflow)}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * 行业榜。点某一行展开该分类下的 ETF 明细。
 *
 * 明细用 `div` 而不是嵌套 `<table>`：嵌套表格的表头会和外层撞在一起，
 * 而且两套列宽各算各的、对不齐。这里用固定宽度的 flex 行，视觉上从属于上面那行。
 */
function EtfIndustryTable({ items }: { items: EtfIndustryItem[] }) {
  const [open, setOpen] = useState<string | null>(null)
  // 首屏不排，保持后端顺序（已按当前口径排序）；点列头才排。
  // 行业一共 30 多个、一次全取，所以这里的排序是**完整的**，没有截断问题
  const [sort, shown] = useSort(items, ETF_INDUSTRY_SORTS, { key: null })

  return (
    <div className="max-h-[440px] overflow-auto">
      <table className="grid-table">
        <thead>
          <tr>
            <SortTh sortKey="category" align="left" {...sort}>行业 / 主题</SortTh>
            <SortTh sortKey="fund_count" {...sort}>只数</SortTh>
            <SortTh sortKey="pct_chg" {...sort}>涨跌幅</SortTh>
            <SortTh sortKey="amount" {...sort}>成交额</SortTh>
            <SortTh sortKey="net_inflow" {...sort}>净申赎</SortTh>
          </tr>
        </thead>
        <tbody>
          {shown.map((item) => {
            const expanded = open === item.category
            return (
              <Fragment key={item.category}>
                <tr
                  className="cursor-pointer"
                  title={expanded ? '点击收起' : '点击展开该分类下的 ETF'}
                  onClick={() => setOpen(expanded ? null : item.category)}
                >
                  <td>
                    <span className="mr-1 text-fg-dim">{expanded ? '▾' : '▸'}</span>
                    <span className="text-fg">{item.category}</span>
                  </td>
                  <td>
                    <span className="num text-fg-muted">{item.fund_count}</span>
                  </td>
                  <td>
                    <span className={`num ${toneOf(item.pct_chg)}`}>{fmtPct(item.pct_chg)}</span>
                  </td>
                  <td>
                    <span className="num text-fg-muted">{fmtAmount(item.amount)}</span>
                  </td>
                  <td>
                    <span className={`num font-medium ${toneOf(item.net_inflow)}`}>
                      {signedAmount(item.net_inflow)}
                    </span>
                  </td>
                </tr>
                {expanded && (
                  <tr>
                    {/* p-0 / bg-transparent：整格承载一块自定义内容，
                        不要单元格自己的内边距，也不要跟着鼠标悬停变色 */}
                    <td colSpan={5} className="p-0 bg-transparent">
                      <div className="bg-ink-800/40">
                        {item.funds.map((fund) => (
                          <div
                            key={fund.code}
                            className="flex items-center gap-2 px-3 py-1 text-[13px]"
                          >
                            <span className="num w-[52px] shrink-0 text-fg-dim">{fund.code}</span>
                            <span className="min-w-0 flex-1 truncate text-fg">
                              {fund.name ?? '—'}
                            </span>
                            <span
                              className={`num w-[60px] shrink-0 text-right ${toneOf(fund.pct_chg)}`}
                            >
                              {fmtPct(fund.pct_chg)}
                            </span>
                            <span className="num w-[72px] shrink-0 text-right text-fg-muted">
                              {fmtAmount(fund.amount)}
                            </span>
                            <span
                              className={`num w-[80px] shrink-0 text-right ${toneOf(fund.net_inflow)}`}
                            >
                              {signedAmount(fund.net_inflow)}
                            </span>
                          </div>
                        ))}
                        {item.fund_count > item.funds.length && (
                          <div className="px-3 py-1 text-[13px] text-fg-dim">
                            共 {item.fund_count} 只，此处只列净申赎前 {item.funds.length} 只；
                            完整列表见「按单只」
                          </div>
                        )}
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/**
 * 「板块」单元格：开盘红精选板块（该股最近一次涨停因哪个题材）。
 *
 * 口径与形态页 / 异动页一致：没有涨停记录就没有值（实测覆盖约 75%），所以空着是
 * 事实、不是没加载出来，用「—」表示。多个板块时列里只放第一个、其余放 tooltip。
 */
function SectorCell({ sectors }: { sectors: string[] }) {
  if (sectors.length === 0) return <span className="text-fg-dim">—</span>
  return (
    <span
      className="whitespace-nowrap text-[13px] text-fg-muted"
      title={sectors.join('、')}
    >
      {sectors[0]}
      {sectors.length > 1 && (
        <span className="text-fg-dim"> +{sectors.length - 1}</span>
      )}
    </span>
  )
}

/**
 * 「行业」单元格：同花顺三级路径（`电子-半导体-集成电路Ⅲ`）。
 *
 * 完整值最长 19 字，铺在列里太占宽 —— 只显示**第一级**，完整路径放 tooltip
 * （与形态页同一做法）。注意它与「板块」不是一套分类。
 */
function IndustryCell({ industry }: { industry: string | null }) {
  if (!industry) return <span className="text-fg-dim">—</span>
  return (
    <span className="whitespace-nowrap text-[13px] text-fg-muted" title={industry}>
      {industry.split('-')[0]}
    </span>
  )
}

function DdeTable({ items }: { items: DdeItem[] }) {
  // 首屏不排，保持后端顺序（已按当前口径排序）；点列头才排 —— 与其它榜一致
  const [sort, shown] = useSort(items, DDE_SORTS, { key: null })

  return (
    <div className="max-h-[440px] overflow-auto">
      <table className="grid-table">
        <thead>
          <tr>
            <SortTh sortKey="code" {...sort}>代码</SortTh>
            <SortTh sortKey="name" align="left" {...sort}>名称</SortTh>
            {/* 板块 / 行业是一串名字、没有单一可比值，不挂排序（与形态页同理）；
                口径见 SectorCell / IndustryCell 的说明 */}
            <SortTh align="left" title="开盘红精选板块：该股最近一次涨停因哪个题材（没涨停过就没有值）">
              板块
            </SortTh>
            <SortTh align="left" title="同花顺三级行业路径，只显示第一级，悬停看完整路径">
              行业
            </SortTh>
            <SortTh sortKey="pct_chg" {...sort}>涨跌幅</SortTh>
            <SortTh sortKey="close" {...sort}>收盘价</SortTh>
            <SortTh sortKey="dde" {...sort} title="5 日区间 dde 大单净额">5日DDE</SortTh>
          </tr>
        </thead>
        <tbody>
          {shown.map((item) => (
            <tr
              key={item.code}
              title={[
                `${item.name ?? ''} ${item.code}`,
                `5日DDE ${signedAmount(item.dde)}`,
                item.pct_chg === null ? null : `涨跌幅 ${fmtPct(item.pct_chg)}`,
              ]
                .filter(Boolean)
                .join('\n')}
            >
              <td>
                <StockLink code={item.code} className="num text-fg-muted">
                  {item.code}
                </StockLink>
              </td>
              <td className="!text-left">
                <StockLink code={item.code}>{item.name ?? item.code}</StockLink>
              </td>
              <td className="!text-left">
                <SectorCell sectors={item.sectors} />
              </td>
              <td className="!text-left">
                <IndustryCell industry={item.industry} />
              </td>
              <td>
                <span className={`num ${toneOf(item.pct_chg)}`}>{fmtPct(item.pct_chg)}</span>
              </td>
              <td>
                <span className="num text-fg-muted">{fmtNum(item.close, 2)}</span>
              </td>
              <td>
                <span className={`num ${toneOf(item.dde)}`}>{signedAmount(item.dde)}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function InstitutionTable({ items }: { items: InstitutionItem[] }) {
  // 首屏不排，保持后端顺序（已按当前口径排序）；点列头才排。
  // ⚠️ 同 ETF 单只榜：排的只是后端返回的这批（默认净买额前 30 只），
  // 想看「买入额最大的」要把上面的口径切到「买入额」
  const [sort, shown] = useSort(items, INSTITUTION_SORTS, { key: null })

  return (
    <div className="max-h-[440px] overflow-auto">
      <table className="grid-table">
        <thead>
          <tr>
            <SortTh sortKey="code" {...sort}>代码</SortTh>
            <SortTh sortKey="name" align="left" {...sort}>名称</SortTh>
            {/* 板块 / 行业不挂排序（一串名字、没有单一可比值），口径见上面两个单元格组件 */}
            <SortTh align="left" title="开盘红精选板块：该股最近一次涨停因哪个题材（没涨停过就没有值）">
              板块
            </SortTh>
            <SortTh align="left" title="同花顺三级行业路径，只显示第一级，悬停看完整路径">
              行业
            </SortTh>
            <SortTh sortKey="pct_chg" {...sort}>涨跌幅</SortTh>
            <SortTh sortKey="buy_count" {...sort} title="买方机构家数">买方机构</SortTh>
            <SortTh sortKey="sell_count" {...sort} title="卖方机构家数">卖方机构</SortTh>
            <SortTh sortKey="buy_amount" {...sort}>买入额</SortTh>
            <SortTh sortKey="sell_amount" {...sort}>卖出额</SortTh>
            <SortTh sortKey="net_amount" {...sort}>净买额</SortTh>
            <SortTh sortKey="reason" align="left" {...sort}>上榜原因</SortTh>
          </tr>
        </thead>
        <tbody>
          {shown.map((item) => (
            <tr
              key={item.code}
              // 上榜原因经常长到一屏放不下，这里是唯一能看到全文的地方
              title={[
                `${item.name ?? ''} ${item.code}`,
                item.reason ? `上榜原因：${item.reason}` : null,
                `买入额 ${fmtAmount(item.buy_amount)}（${fmtInt(item.buy_count)} 家机构）`,
                `卖出额 ${fmtAmount(item.sell_amount)}（${fmtInt(item.sell_count)} 家机构）`,
                `净买额 ${signedAmount(item.net_amount)}`,
              ]
                .filter(Boolean)
                .join('\n')}
            >
              <td>
                <StockLink code={item.code} className="num text-fg-muted">
                  {item.code}
                </StockLink>
              </td>
              <td className="!text-left">
                <StockLink code={item.code}>{item.name ?? item.code}</StockLink>
              </td>
              <td className="!text-left">
                <SectorCell sectors={item.sectors} />
              </td>
              <td className="!text-left">
                <IndustryCell industry={item.industry} />
              </td>
              <td>
                <span className={`num ${toneOf(item.pct_chg)}`}>{fmtPct(item.pct_chg)}</span>
              </td>
              <td>
                <span className={`num ${item.buy_count ? 'text-up' : 'text-fg-dim'}`}>
                  {/* 判 null 而不是判 falsy：机构**买 0 家**是有意义的事实，
                      用 `? :` 会把它显示成「—」，读起来像「未知」（2026-09-27 修） */}
                  {item.buy_count == null ? '—' : fmtInt(item.buy_count)}
                </span>
              </td>
              <td>
                <span className={`num ${item.sell_count ? 'text-down' : 'text-fg-dim'}`}>
                  {item.sell_count == null ? '—' : fmtInt(item.sell_count)}
                </span>
              </td>
              <td>
                <span className="num text-fg-muted">{fmtAmount(item.buy_amount)}</span>
              </td>
              <td>
                <span className="num text-fg-muted">{fmtAmount(item.sell_amount)}</span>
              </td>
              <td>
                <span className={`num font-medium ${toneOf(item.net_amount)}`}>
                  {signedAmount(item.net_amount)}
                </span>
              </td>
              <td className="!text-left">
                <span className="inline-block max-w-[180px] truncate align-bottom text-[13px] text-fg-muted">
                  {item.reason ?? '—'}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * 两融走势：融资余额看绝对水位（左轴），融资买入额看当日活跃度（右轴）。
 * 两者量级差一个数量级，共用一根轴会把买入额压成一条贴地直线，所以必须双轴。
 */
function buildMarginOption(data: FundsSeries): ChartOption {
  const nameStyle = {
    color: CHART.fgDim,
    fontSize: 11,
    fontFamily: AXIS_LABEL.fontFamily,
  }

  return {
    // top 留够图例的高度，否则图例画进绘图区会被折线压住
    grid: { ...GRID, top: 46 },
    legend: { ...LEGEND, top: 2, data: ['融资余额', '融资买入额'] },
    tooltip: {
      ...TOOLTIP,
      trigger: 'axis' as const,
      axisPointer: {
        type: 'line' as const,
        lineStyle: { color: CHART.fgDim, type: 'dashed' as const },
      },
      formatter: (params: unknown) => {
        const items = params as { dataIndex: number }[]
        if (!items?.length) return ''
        const index = items[0].dataIndex
        return [
          data.dates[index],
          `融资余额 ${fmtAmount(data.financing_balance[index])}`,
          `融资买入额 ${fmtAmount(data.financing_buy[index])}`,
          // 断点的成因写进 tooltip：不然看到缺口只能猜是不是采集漏了
          data.financing_complete[index] ? null : '深市当日未披露，不给两市合计',
        ]
          .filter(Boolean)
          .join('<br/>')
      },
    },
    xAxis: {
      type: 'category' as const,
      data: data.dates.map(fmtShortDate),
      axisLabel: { ...AXIS_LABEL, interval: Math.ceil(data.dates.length / 8) },
      axisLine: AXIS_LINE,
      axisTick: { show: false },
    },
    yAxis: [
      {
        type: 'value' as const,
        name: '融资余额',
        nameTextStyle: nameStyle,
        // scale 让轴不从 0 起：两融余额在 1.5 万亿上下波动，从 0 起就成一条平线了
        scale: true,
        axisLabel: { ...AXIS_LABEL, formatter: (value: number) => fmtAmount(value) },
        splitLine: SPLIT_LINE,
        axisLine: { show: false },
      },
      {
        type: 'value' as const,
        name: '融资买入额',
        nameTextStyle: nameStyle,
        scale: true,
        axisLabel: { ...AXIS_LABEL, formatter: (value: number) => fmtAmount(value) },
        splitLine: { show: false },
        axisLine: { show: false },
      },
    ],
    series: [
      {
        type: 'line' as const,
        name: '融资余额',
        data: data.financing_balance,
        symbol: 'none',
        // 缺值绝不插值：连出一条假线比断开更容易被读成「数据是连续的」
        connectNulls: false,
        lineStyle: { width: 1.6, color: SERIES_PALETTE[0] },
        itemStyle: { color: SERIES_PALETTE[0] },
      },
      {
        type: 'line' as const,
        name: '融资买入额',
        yAxisIndex: 1,
        data: data.financing_buy,
        symbol: 'none',
        connectNulls: false,
        lineStyle: { width: 1.6, color: SERIES_PALETTE[1] },
        itemStyle: { color: SERIES_PALETTE[1] },
      },
    ],
  }
}

/** 北向成交额：单序列，用中性色（红绿在本站有涨跌含义，不能拿来表示资金量大小）。 */
function buildHsgtOption(data: FundsSeries): ChartOption {
  const color = SERIES_PALETTE[2]
  return {
    grid: { ...GRID, top: 24 },
    tooltip: {
      ...TOOLTIP,
      trigger: 'axis' as const,
      axisPointer: {
        type: 'line' as const,
        lineStyle: { color: CHART.fgDim, type: 'dashed' as const },
      },
      formatter: (params: unknown) => {
        const items = params as { dataIndex: number }[]
        if (!items?.length) return ''
        const index = items[0].dataIndex
        return [
          data.dates[index],
          `沪深股通成交额 ${fmtAmount(data.hsgt_turnover[index])}`,
        ].join('<br/>')
      },
    },
    xAxis: {
      type: 'category' as const,
      data: data.dates.map(fmtShortDate),
      axisLabel: { ...AXIS_LABEL, interval: Math.ceil(data.dates.length / 8) },
      axisLine: AXIS_LINE,
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value' as const,
      name: '成交额',
      nameTextStyle: {
        color: CHART.fgDim,
        fontSize: 11,
        fontFamily: AXIS_LABEL.fontFamily,
      },
      scale: true,
      axisLabel: { ...AXIS_LABEL, formatter: (value: number) => fmtAmount(value) },
      splitLine: SPLIT_LINE,
      axisLine: { show: false },
    },
    series: [
      {
        type: 'line' as const,
        name: '沪深股通成交额',
        data: data.hsgt_turnover,
        symbol: 'none',
        connectNulls: false,
        lineStyle: { width: 1.6, color },
        itemStyle: { color },
      },
    ],
  }
}

/**
 * 期指前 20 净空走势：四个品种各一条 + 四品种合计（最粗、最深那条）。
 *
 * - 单位是「手」，不走金额格式（`fmtAmount` 会把它写成「2.2万」，那是金额的读法）。
 * - `connectNulls: false`：没采集到的日子必须断开。补 0 会把「当天没数据」
 *   画成「净空归零」，那是完全相反的结论。
 * - 合计用 `CHART.fg`（最深的中性色）且加粗：它是这张图的主线，
 *   但不用红绿、也不用强调蓝 —— 前两者在本站有涨跌/选中的含义。
 */
function buildFuturesOption(data: FuturesSeries): ChartOption {
  const nameStyle = {
    color: CHART.fgDim,
    fontSize: 11,
    fontFamily: AXIS_LABEL.fontFamily,
  }
  const series: { name: string; values: (number | null)[]; color: string; width: number }[] = [
    { name: 'IF', values: data.if_net, color: SERIES_PALETTE[0], width: 1.6 },
    { name: 'IC', values: data.ic_net, color: SERIES_PALETTE[1], width: 1.6 },
    { name: 'IH', values: data.ih_net, color: SERIES_PALETTE[2], width: 1.6 },
    { name: 'IM', values: data.im_net, color: SERIES_PALETTE[3], width: 1.6 },
    { name: '合计', values: data.total, color: CHART.fg, width: 2.4 },
  ]

  return {
    grid: { ...GRID, top: 46 },
    legend: { ...LEGEND, top: 2, data: series.map((item) => item.name) },
    tooltip: {
      ...TOOLTIP,
      trigger: 'axis' as const,
      axisPointer: {
        type: 'line' as const,
        lineStyle: { color: CHART.fgDim, type: 'dashed' as const },
      },
      formatter: (params: unknown) => {
        const items = params as { dataIndex: number }[]
        if (!items?.length) return ''
        const index = items[0].dataIndex
        const lines = series.map((item) => `${item.name} ${fmtInt(item.values[index])}`)
        // 合计为空是「当天有品种没发布」，不是 0，得说清楚
        if (data.total[index] == null) lines.push('当日品种不全，不给合计')
        return [data.dates[index], ...lines].join('<br/>')
      },
    },
    xAxis: {
      type: 'category' as const,
      data: data.dates.map(fmtShortDate),
      axisLabel: { ...AXIS_LABEL, interval: Math.ceil(data.dates.length / 8) },
      axisLine: AXIS_LINE,
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value' as const,
      name: '净空（手）',
      nameTextStyle: nameStyle,
      // 不从 0 起：净空长期在几万手，从 0 起四条线会挤成一团
      scale: true,
      axisLabel: { ...AXIS_LABEL, formatter: (value: number) => fmtInt(value) },
      splitLine: SPLIT_LINE,
      axisLine: { show: false },
    },
    series: series.map((item) => ({
      type: 'line' as const,
      name: item.name,
      data: item.values,
      symbol: 'none',
      connectNulls: false,
      lineStyle: { width: item.width, color: item.color },
      itemStyle: { color: item.color },
    })),
  }
}
