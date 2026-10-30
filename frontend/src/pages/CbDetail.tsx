import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { CbDetail as CbDetailData, CbDetailPoint } from '../api/types'
import Alert from '../components/Alert'
import EChart from '../components/EChart'
import type { ChartOption } from '../components/EChart'
import Layout from '../components/Layout'
import LoadState from '../components/LoadState'
import Panel from '../components/Panel'
import StockLink from '../components/StockLink'
import { AXIS_LABEL, AXIS_LINE, CHART, GRID, SPLIT_LINE, TOOLTIP } from '../lib/chart'
import { fmtAmount, fmtNum, fmtPct, fmtShortDate, toneOf } from '../lib/format'
import { useWatchlist } from '../lib/watchlist'

/**
 * 单券详情（可转债-3 §3.1 的前端部分）。
 *
 * 口径（页面顶部写清楚，别被当成实时行情）：
 *
 * - 数据是**盘后快照**，`as_of` 是该券自己最近的一份（已摘牌 / 当天停牌也打得开）；
 * - 走势长度 = **上线以来采到几天**，不是固定的 250 天 —— 转债快照只能当天采、
 *   历史不可回补，所以第一天只有 1 个点，不能画线也不要假装有历史；
 * - YTM 用票息 + 到期赎回价算的复利 IRR，未计税；票息没补齐时显示「—」；
 * - 强赎计数优先用集思录的「强赎天计数」，分母是**触发所需天数**（通常 15），
 *   不是 30；窗口不完整时写「≥n」。
 */
export default function CbDetailPage() {
  const { code = '' } = useParams<{ code: string }>()
  const [data, setData] = useState<CbDetailData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  /** 404：库里没有这只转债（区别于接口出错） */
  const [missing, setMissing] = useState(false)
  const { isWatched, refresh } = useWatchlist()
  const [watching, setWatching] = useState(false)

  useEffect(() => {
    let stale = false
    setLoading(true)
    setError(null)
    setMissing(false)
    setData(null)
    api
      .cbDetail(code)
      .then((value) => {
        if (!stale) setData(value)
      })
      .catch((err: Error) => {
        if (stale) return
        setError(err.message)
        setMissing(err instanceof ApiError && err.status === 404)
      })
      .finally(() => {
        if (!stale) setLoading(false)
      })
    return () => {
      stale = true
    }
  }, [code])

  const watched = isWatched(code)
  const toggleWatch = async () => {
    setWatching(true)
    try {
      if (watched) await api.removeWatchlist(code)
      else await api.addWatchlist(code)
      // 增删事件会让全站自重拉，这里再显式刷一次，按钮状态立刻跟上
      await refresh()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setWatching(false)
    }
  }

  const option = useMemo(
    () => (data && data.series.length > 1 ? buildOption(data.series) : null),
    [data],
  )

  const toolbar = (
    <>
      <Link to="/cb" className="text-[13px] text-fg-dim hover:text-fg">
        ← 可转债
      </Link>
      <button
        type="button"
        onClick={() => void toggleWatch()}
        disabled={!data || watching}
        className="num border border-line px-2.5 py-[3px] text-[13px] text-fg-muted transition-colors hover:border-accent/50 hover:text-accent disabled:cursor-not-allowed disabled:opacity-40"
      >
        {watched ? '移出自选' : '加入自选'}
      </button>
    </>
  )

  return (
    <Layout toolbar={toolbar}>
      {error && !missing && <Alert onClose={() => setError(null)}>{error}</Alert>}

      <div className="space-y-4">
        <Panel
          title={data ? `${data.bond_name ?? data.bond_code}  ${data.bond_code}` : code}
          meta={
            <span className="num flex flex-wrap items-center gap-x-3 gap-y-1">
              {data ? (
                <>
                  {data.stock_code && (
                    <StockLink code={data.stock_code} className="text-[13px] text-fg-muted">
                      正股 {data.stock_name ?? data.stock_code}
                    </StockLink>
                  )}
                  <span className="text-fg-dim">
                    {data.as_of ? `${data.as_of} · 盘后快照` : '还没有快照'}
                  </span>
                </>
              ) : (
                <span className="text-fg-dim">{missing ? '没有这只转债' : '加载中…'}</span>
              )}
            </span>
          }
          delay={40}
        >
          {!data ? (
            <LoadState
              loading={loading}
              error={missing ? null : error}
              emptyText={missing ? '库里没有这只转债，可能已摘牌且从未采集过' : '暂无数据'}
            />
          ) : (
            <>
              <div className="grid grid-cols-2 overflow-hidden md:grid-cols-3 xl:grid-cols-4">
                <Cell label="现价" value={fmtNum(data.close, 3)} tone={toneOf(data.pct_chg)} />
                <Cell label="涨跌幅" value={fmtPct(data.pct_chg)} tone={toneOf(data.pct_chg)} />
                <Cell label="转股溢价" value={fmtPct(data.premium_pct, 1)} />
                <Cell label="双低" value={fmtNum(data.double_low, 1)} />
                <Cell label="转股价值" value={fmtNum(data.conv_value, 1)} />
                <Cell label="转股价" value={fmtNum(data.conv_price, 2)} />
                <Cell label="到期 YTM" value={fmtPct(data.ytm_pct, 1)} tone={toneOf(data.ytm_pct)} />
                <Cell
                  label="剩余规模"
                  value={data.remain_size == null ? '—' : `${fmtNum(data.remain_size, 1)} 亿`}
                  sub={data.issue_size == null ? undefined : `发行 ${fmtNum(data.issue_size, 1)} 亿`}
                />
                <Cell label="剩余年限" value={fmtNum(data.years_left, 2)} />
                <Cell label="评级" value={data.rating ?? '—'} />
                <Cell label="成交额" value={fmtAmount(data.amount)} />
                <Cell
                  label="强赎计数"
                  value={countText(data.redeem_count, data.redeem_complete, data.redeem_required)}
                />
              </div>

              {(data.labels.length > 0 || data.risks.length > 0) && (
                <div className="border-t border-line-soft px-4 py-2 text-[12px]">
                  {data.labels.map((label) => (
                    <span
                      key={label}
                      className={`mr-1.5 whitespace-nowrap rounded border border-line-soft px-1 ${
                        label === '已公告强赎'
                          ? 'text-danger'
                          : label.startsWith('强赎临近') || label.startsWith('下修临近')
                            ? 'text-warn'
                            : 'text-fg-muted'
                      }`}
                    >
                      {label}
                    </span>
                  ))}
                  {data.risks.map((risk) => (
                    <span
                      key={risk}
                      className="mr-1.5 whitespace-nowrap rounded border border-line-soft px-1 text-fg-dim"
                      title="风险提示，不默认剔除"
                    >
                      {risk}
                    </span>
                  ))}
                </div>
              )}

              {redeemNote(data) && (
                <div
                  className={`border-t border-line-soft px-4 py-2 text-[12px] leading-relaxed ${
                    data.redeem_status?.includes('已公告') ? 'text-danger' : 'text-warn'
                  }`}
                >
                  {redeemNote(data)}
                </div>
              )}

              <div className="num border-t border-line-soft px-4 py-2 text-[13px] text-fg-dim">
                最新 {data.as_of ?? '—'}
                <span className="mx-2">·</span>高 {fmtNum(data.high, 3)}
                <span className="mx-2">·</span>低 {fmtNum(data.low, 3)}
                <span className="mx-2">·</span>振幅 {fmtNum(data.amplitude, 1, '%')}
                <span className="mx-2">·</span>换手 {fmtNum(data.turnover, 1, '%')}
                <span className="mx-2">·</span>下修计数 {countText(data.revise_count, data.revise_complete)}
                {data.source && data.source !== 'sina+em+jsl' && (
                  <>
                    <span className="mx-2">·</span>
                    <span className="text-warn">行情走了兜底源（{data.source}）</span>
                  </>
                )}
              </div>
            </>
          )}
        </Panel>

        <Panel
          title="价格与转股价值"
          meta={
            <span className="num text-fg-dim">
              {data ? `${data.series.length} 个交易日` : '—'}
            </span>
          }
          delay={60}
        >
          {!data ? (
            <LoadState loading={loading} error={missing ? null : error} />
          ) : option === null ? (
            <p className="px-4 py-8 text-center text-[13px] leading-relaxed text-fg-dim">
              转债快照只能当天采、不能回补，历史从上线当天开始累积 ——
              目前只有 {data.series.length} 天，明晚起才会有曲线。
            </p>
          ) : (
            <>
              <p className="border-b border-line-soft px-4 py-2 text-[12px] text-fg-dim">
                <span className="text-accent">实线</span> 收盘价（左轴）；
                <span className="text-fg-muted"> 虚线</span> 转股价值（右轴）。缺值断开、不插值。
              </p>
              <div className="px-2 py-2">
                <EChart option={option} height={280} />
              </div>
            </>
          )}
        </Panel>

        <Panel title="条款" meta={<span className="num text-fg-dim">来自东财 / 集思录</span>} delay={80}>
          {!data ? (
            <LoadState loading={loading} error={missing ? null : error} />
          ) : (
            <>
              <div className="grid grid-cols-2 overflow-hidden md:grid-cols-3">
                <Cell label="上市日" value={data.list_date ?? '—'} compact />
                <Cell label="到期日" value={data.maturity_date ?? '—'} compact />
                <Cell label="转股起始日" value={data.convert_start_date ?? '—'} compact />
                <Cell label="发行规模" value={data.issue_size == null ? '—' : `${fmtNum(data.issue_size, 1)} 亿`} compact />
                <Cell label="到期赎回价" value={data.maturity_call_price == null ? '—' : `${fmtNum(data.maturity_call_price, 2)} 元`} compact />
                <Cell label="评级" value={data.rating ?? '—'} compact />
                <Cell label="强赎触发" value={pctOf(data.redeem_trigger_pct)} compact />
                <Cell label="回售触发" value={pctOf(data.resale_trigger_pct)} compact />
                <Cell label="下修触发" value={pctOf(data.revise_trigger_pct)} compact />
              </div>
              <div className="border-t border-line-soft px-4 py-3 text-[12px] leading-relaxed text-fg-dim">
                <div className="mb-1 text-fg-muted">利率说明（原文）</div>
                {data.coupon_text ?? '条款还没补齐（每天补 30 只，也可跑 scripts/fill_cb_terms.py 立刻补全）'}
                {data.delist_date && (
                  <div className="mt-2 text-warn">
                    已摘牌 / 已到期：{data.delist_date}，本页显示的是它最后一份快照。
                  </div>
                )}
              </div>
            </>
          )}
        </Panel>

        <p className="px-1 text-[12px] leading-relaxed text-fg-dim">
          全部是盘后快照，不是实时价，也不构成买卖建议。YTM 是票息 + 到期赎回价算的复利 IRR（未计税）；
          「已满足强赎条件」只代表触发条件达成、公司还没表态，不等于已公告强赎。
        </p>
      </div>
    </Layout>
  )
}

/** 计数：窗口不完整时写「≥n」，分母是触发所需天数。 */
function countText(
  value: number | null,
  complete: boolean,
  required: number | null = null,
): string {
  if (value == null) return '—'
  const head = complete ? String(value) : `≥${value}`
  return required ? `${head}/${required}` : head
}

/** 比例：130 → 130%；条款缺失时给「—」。 */
function pctOf(value: number | null): string {
  return value == null ? '—' : `${fmtNum(value, 0)}%`
}

/** 强赎提示条：只在真有话说的时候返回文案。 */
function redeemNote(data: CbDetailData): string | null {
  const status = data.redeem_status ?? ''
  if (status.includes('已公告') || status.includes('公告要强赎')) {
    const left = data.days_left
    return `已公告强赎${data.last_trade_date ? `，最后交易日 ${data.last_trade_date}` : ''}${
      left == null ? '' : left >= 0 ? `（还剩 ${left} 天）` : '（已过）'
    }；没转股会按约 100 元赎回。`
  }
  if (status.includes('已满足')) {
    return `已满足强赎条件（${countText(data.redeem_count, data.redeem_complete, data.redeem_required)}），公司还没表态 —— 不等于已公告强赎。`
  }
  if (status.includes('不强赎')) return '公司已公告不强赎（承诺期内）。'
  if (data.redeem_count != null && data.redeem_count >= 10) {
    return `强赎临近 ${countText(data.redeem_count, data.redeem_complete, data.redeem_required)}：转股价值站上触发线的天数接近条款要求。`
  }
  return null
}

/** 价格（左轴）+ 转股价值（右轴）。缺值断开，不插值。 */
function buildOption(points: CbDetailPoint[]): ChartOption {
  return {
    grid: { ...GRID, right: 48, top: 34 },
    tooltip: {
      ...TOOLTIP,
      trigger: 'axis' as const,
      axisPointer: {
        type: 'line' as const,
        lineStyle: { color: CHART.fgDim, type: 'dashed' as const },
      },
      formatter: (params: unknown) => {
        const items = params as { dataIndex: number }[] | undefined
        const row = points[items?.[0]?.dataIndex ?? -1]
        if (!row) return ''
        return [
          `<b>${row.trade_date}</b>`,
          `收盘价 ${fmtNum(row.close, 3)}`,
          `转股价值 ${fmtNum(row.conv_value, 2)}`,
          `转股溢价 ${fmtPct(row.premium_pct, 2)}`,
        ].join('<br/>')
      },
    },
    xAxis: {
      type: 'category' as const,
      data: points.map((point) => fmtShortDate(point.trade_date)),
      axisLabel: { ...AXIS_LABEL, interval: Math.max(0, Math.floor(points.length / 8)) },
      axisLine: AXIS_LINE,
      axisTick: { show: false },
    },
    yAxis: [
      {
        type: 'value' as const,
        scale: true,
        name: '价格',
        nameTextStyle: { color: CHART.fgDim, fontSize: 11, align: 'left' as const },
        axisLabel: { ...AXIS_LABEL },
        splitLine: SPLIT_LINE,
        axisLine: { show: false },
      },
      {
        type: 'value' as const,
        scale: true,
        name: '转股价值',
        nameTextStyle: { color: CHART.fgDim, fontSize: 11, align: 'right' as const },
        axisLabel: { ...AXIS_LABEL },
        splitLine: { show: false },
        axisLine: { show: false },
      },
    ],
    series: [
      {
        type: 'line' as const,
        name: '收盘价',
        data: points.map((point) => point.close),
        symbol: 'none' as const,
        connectNulls: false,
        lineStyle: { width: 1.8, color: CHART.accent },
        itemStyle: { color: CHART.accent },
      },
      {
        type: 'line' as const,
        name: '转股价值',
        yAxisIndex: 1,
        data: points.map((point) => point.conv_value),
        symbol: 'none' as const,
        connectNulls: false,
        lineStyle: { width: 1.4, color: CHART.fgMuted, type: 'dashed' as const },
        itemStyle: { color: CHART.fgMuted },
      },
    ],
  }
}

function Cell({
  label,
  value,
  tone = 'text-fg',
  sub,
  compact = false,
}: {
  label: string
  value: string
  tone?: string
  sub?: string
  /** 条款那种「一对一对」的格子用小一号字 */
  compact?: boolean
}) {
  return (
    <div className="relative -mr-px -mb-px border-r border-b border-line-soft px-4 py-3">
      <div className="text-[13px] tracking-[0.1em] text-fg-dim">{label}</div>
      <div
        className={`num mt-1.5 font-medium ${compact ? 'text-[14px] leading-snug' : 'text-[18px] leading-tight'} ${tone}`}
      >
        {value}
      </div>
      {sub && <div className="num mt-1 text-[13px] text-fg-dim">{sub}</div>}
    </div>
  )
}
