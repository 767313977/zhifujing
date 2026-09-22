import { Fragment, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../api/client'
import type {
  EtfFlowBoard,
  EtfFlowItem,
  EtfFlowOrder,
  EtfGroup,
  EtfIndustryBoard,
  EtfIndustryItem,
  FundFlowOverview,
  FundsSeries,
  InstitutionBoard,
  InstitutionItem,
  InstitutionOrder,
} from '../api/types'
import Alert from '../components/Alert'
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
import { fmtAmount, fmtInt, fmtPct, fmtShortDate, toneOf } from '../lib/format'
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
  const [etfGroup, setEtfGroup] = useState<EtfGroup>('industry')
  const [etfOrder, setEtfOrder] = useState<EtfFlowOrder>('inflow')
  const [etf, setEtf] = useState<EtfFlowBoard | null>(null)
  const [etfIndustry, setEtfIndustry] = useState<EtfIndustryBoard | null>(null)
  const [etfLoading, setEtfLoading] = useState(true)
  const [instOrder, setInstOrder] = useState<InstitutionOrder>('net')
  const [institutions, setInstitutions] = useState<InstitutionBoard | null>(null)
  const [instLoading, setInstLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

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
    api
      .fundsOverview(date)
      .then((data) => {
        if (!stale) setOverview(data)
      })
      .catch((err: Error) => {
        if (stale) return
        setOverview(null)
        setError(err.message)
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
    api
      .fundsSeries(SERIES_DAYS, date)
      .then((data) => {
        if (!stale) setSeries(data)
      })
      .catch((err: Error) => {
        if (stale) return
        setSeries(null)
        setError(err.message)
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
    setEtfLoading(true)
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
        if (!stale) setError(err.message)
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
    setInstitutions(null)
    api
      .fundsInstitutions(instOrder, BOARD_LIMIT, date)
      .then((data) => {
        if (!stale) setInstitutions(data)
      })
      .catch((err: Error) => {
        if (!stale) setError(err.message)
      })
      .finally(() => {
        if (!stale) setInstLoading(false)
      })
    return () => {
      stale = true
    }
  }, [instOrder, date])

  const marginOption = useMemo<ChartOption>(
    () => (series && series.dates.length > 0 ? buildMarginOption(series) : {}),
    [series],
  )

  const hsgtOption = useMemo<ChartOption>(
    () => (series && series.dates.length > 0 ? buildHsgtOption(series) : {}),
    [series],
  )

  // 下面四张卡片的小字计算先落地成常量：JSX 里写几层可选链会读不清，
  // 而且顺势把「算不出来」统一收敛成 null，交给格式化函数输出占位符
  const financingTotal = overview?.financing_total ?? null
  const buyTotal = overview?.financing_buy_total ?? null
  const financingChange = overview?.financing_change ?? null
  const hsgtTurnover = overview?.hsgt_turnover ?? null
  const hsgtPrev = overview?.hsgt_turnover_prev ?? null
  const hsgtChange =
    hsgtTurnover != null && hsgtPrev != null ? hsgtTurnover - hsgtPrev : null
  const institutionNet = overview?.institution_net ?? null

  // 两个视图的 board 形状不同（单只明细 / 分类汇总），但「有没有可比基准」
  // 「有几条」「基准日」这三件事都要用 —— 先收敛成同一组变量，
  // 免得 JSX 里到处写三目，那种写法最容易在两处写岔
  const etfBoard = etfGroup === 'industry' ? etfIndustry : etf
  const etfHasPrev = etfBoard?.has_prev
  const etfPrevDate = etfBoard?.prev_date ?? null
  const etfCount = etfBoard?.items.length ?? 0
  const etfNote =
    etfGroup === 'industry'
      ? '净申赎 = 该行业全部 ETF 的份额变化 × 收盘价求和（份额单位不可加，所以只加金额）'
      : '净申赎 = 份额变化 × 当日收盘价（近似当日净值），正数为净申购'
  // ETF 份额常在 T+1 才更新，后端会回落到「最近有份额的那一天」，所以榜上的数据日
  // 可能比页面顶部选的日期早一天 —— 这本身正常，但必须标出来，
  // 否则两个日期对着看会把前一天的申赎当成今天的
  const etfTradeDate = etfBoard?.trade_date ?? null
  const etfDateNotice =
    !etfLoading && etfTradeDate && etfTradeDate !== (overview?.trade_date ?? date)
      ? etfTradeDate
      : null

  const toolbar = (
    <>
      <select
        value={date ?? ''}
        onChange={(event) => setDate(event.target.value || null)}
        className="num border border-line bg-ink-900 px-2 py-[3px] text-[12px] text-fg outline-none focus:border-fg-dim"
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
      <span className="num text-[12px] text-fg-muted">
        {overviewLoading ? '加载中…' : (overview?.trade_date ?? '—')}
      </span>
    </>
  )

  return (
    <Layout toolbar={toolbar}>
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}

      <div className="space-y-4">
        {/* ---- 概览 ---- */}
        <div className="rise grid grid-cols-1 gap-px border border-line-soft bg-line-soft sm:grid-cols-2 xl:grid-cols-4">
          {overviewLoading ? (
            <div className="bg-ink-900 px-4 py-8 text-center text-[13px] text-fg-dim sm:col-span-2 xl:col-span-4">
              加载中…
            </div>
          ) : (
            <>
              <MetricCard
                label="两融余额"
                // 深市常比沪市晚一天披露，此时两市合计给不出来。
                // 这里必须说成「未披露」——写成 0 会读成两融一天归零
                value={financingTotal == null ? '深市未披露' : fmtAmount(financingTotal)}
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
              />
              <MetricCard
                label="融资买入额"
                // 与上面同一口径：两市齐全才给合计，缺一边时说「未披露」而不是 —
                value={buyTotal == null ? '深市未披露' : fmtAmount(buyTotal)}
                muted={buyTotal == null}
                sub="两市合计 · 当日买入"
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
              <div className="px-4 py-10 text-center text-[13px] text-fg-dim">加载中…</div>
            ) : series && series.dates.length > 0 ? (
              <div className="px-2 pt-2">
                <EChart option={marginOption} height={280} />
                <div className="px-2 pb-2 pt-1 text-[12px] text-fg-dim">
                  左轴融资余额 / 右轴融资买入额 · 断点是当日深市未披露（两市合计给不出来），不是 0
                </div>
              </div>
            ) : (
              <div className="px-4 py-10 text-center text-[13px] text-fg-dim">当日无数据</div>
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
              <div className="px-4 py-10 text-center text-[13px] text-fg-dim">加载中…</div>
            ) : series && series.dates.length > 0 ? (
              <div className="px-2 pt-2">
                <EChart option={hsgtOption} height={280} />
                <div className="px-2 pb-2 pt-1 text-[12px] text-fg-dim">
                  只有成交总额，不含买卖方向 —— 它是活跃度指标，不是净流入
                </div>
              </div>
            ) : (
              <div className="px-4 py-10 text-center text-[13px] text-fg-dim">当日无数据</div>
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
            <div className="px-4 py-10 text-center text-[13px] text-fg-dim">加载中…</div>
          ) : etfHasPrev === false ? (
            // 首次采集只有当天份额，没有可比的基准 —— 这是「没得比」，
            // 与「今天没人申赎」完全不同，不能共用一句空态文案
            <div className="px-4 py-10 text-center text-[13px] text-fg-dim">
              还没有上一交易日的份额数据，无法计算净申赎；等下一次采集后再看
            </div>
          ) : etfCount > 0 ? (
            <>
              <div className="border-b border-line-soft px-3 py-1.5 text-[12px] text-fg-dim">
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
            <div className="px-4 py-10 text-center text-[13px] text-fg-dim">当日无数据</div>
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
            <div className="px-4 py-10 text-center text-[13px] text-fg-dim">加载中…</div>
          ) : institutions && institutions.items.length > 0 ? (
            <InstitutionTable items={institutions.items} />
          ) : (
            <div className="px-4 py-10 text-center text-[13px] text-fg-dim">当日无数据</div>
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
      <div className="text-[12px] tracking-[0.1em] text-fg-dim">{label}</div>
      <div
        className={`num mt-1 leading-tight ${
          muted ? 'text-[13px] text-fg-muted' : `text-[19px] font-medium ${tone}`
        }`}
      >
        {value}
      </div>
      {sub != null && <div className="num mt-1 text-[12px] text-fg-muted">{sub}</div>}
      {note && <div className="mt-1 text-[12px] text-accent">{note}</div>}
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
            'px-2 py-[3px] text-[12px] transition-colors',
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
                            className="flex items-center gap-2 px-3 py-1 text-[12px]"
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
                          <div className="px-3 py-1 text-[12px] text-fg-dim">
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
              <td>
                <span className={`num ${toneOf(item.pct_chg)}`}>{fmtPct(item.pct_chg)}</span>
              </td>
              <td>
                <span className={`num ${item.buy_count ? 'text-up' : 'text-fg-dim'}`}>
                  {item.buy_count ? fmtInt(item.buy_count) : '—'}
                </span>
              </td>
              <td>
                <span className={`num ${item.sell_count ? 'text-down' : 'text-fg-dim'}`}>
                  {item.sell_count ? fmtInt(item.sell_count) : '—'}
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
                <span className="inline-block max-w-[180px] truncate align-bottom text-[12px] text-fg-muted">
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
    fontSize: 10,
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
        fontSize: 10,
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
