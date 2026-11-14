/**
 * 全局股票研究侧栏（S1-C3）。
 *
 * 桌面端是 380–440px 的右侧抽屉；窄屏覆盖式（不挤压主页面内容）。
 * 只读：**不调用 `syncStock`**，也不请求任何会现取外部数据的接口
 * （资金 Tab 走 `/dde?live=0`，只读本地库）。
 *
 * 每个 Tab 打开时才请求自己的数据；同一账户 + 同一股票 + 同一数据日期
 * 在 3 分钟内复用缓存（见 `lib/stockResearchCache.ts`）。
 */

import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type {
  LhbItem,
  StockDde,
  StockLimitRecord,
  StockProfile,
  StockResearchContext,
  StockResearchEndOfDayFact,
  StockResearchTheme,
  StockThemes,
} from '../api/types'
import EChart from './EChart'
import type { ChartOption } from './EChart'
import KLineChart from './KLineChart'
import LoadState from './LoadState'
import { CHART, TOOLTIP } from '../lib/chart'
import { useAuth } from '../lib/authContext'
import { fmtAmount, fmtInt, fmtNum, fmtPct, toneOf } from '../lib/format'
import { dailyBars } from '../lib/klineBars'
import { loadCached } from '../lib/stockResearchCache'
import { useWatchlist } from '../lib/watchlistContext'
import {
  RESEARCH_TABS,
  RESEARCH_TAB_LABELS,
  useStockResearch,
} from '../lib/stockResearchContext'
import type { ResearchTab } from '../lib/stockResearchContext'

/** 通用的「按 key 缓存 + 切股票时丢弃旧响应」读法。 */
function useCached<T>(key: string | null, loader: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(Boolean(key))
  const [error, setError] = useState<string | null>(null)
  const seq = useRef(0)
  const loaderRef = useRef(loader)

  // ref 只在效应里写，避免「渲染期改 ref」（React Compiler 规则）
  useEffect(() => {
    loaderRef.current = loader
  })

  useEffect(() => {
    if (!key) {
      setData(null)
      setLoading(false)
      setError(null)
      return
    }
    const current = ++seq.current
    setLoading(true)
    setError(null)
    loadCached(key, () => loaderRef.current())
      .then((value) => {
        if (current === seq.current) setData(value)
      })
      .catch((err: Error) => {
        if (current !== seq.current) return
        setData(null)
        setError(err.message)
      })
      .finally(() => {
        if (current === seq.current) setLoading(false)
      })
    return () => {
      seq.current += 1
    }
  }, [key])

  return { data, loading, error }
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2 border-b border-line-soft py-1.5 text-[13px]">
      <span className="text-fg-dim">{label}</span>
      <span className="num text-fg">{value}</span>
    </div>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="px-4 py-3">
      <div className="mb-1 text-[12px] tracking-[0.08em] text-fg-dim">{title}</div>
      {children}
    </div>
  )
}

/**
 * 「来源观察日」与「实际数据日」不是一回事：
 * 侧栏的大多数接口只给**最新本地资料**，没有历史 as_of 快照。这里必须把两件事
 * 分开写，不能把页面翻到的那天印成「数据日」。
 */
function AsOfNotice({
  selected,
  actual,
  historicalMissing,
}: {
  selected: string | null
  actual: string | null
  historicalMissing?: string
}) {
  if (!selected) return null
  const prefix = historicalMissing ? `${historicalMissing}：` : ''
  const text =
    actual && actual !== selected
      ? `${prefix}所选观察日 ${selected}；本页显示最新本地资料（截至 ${actual}），不代表该历史日当时已知。`
      : `${prefix}所选观察日 ${selected}；本页无历史快照，显示最新本地资料。`
  return <div className="px-4 pb-2 text-[12px] leading-relaxed text-warn">{text}</div>
}

function OverviewTab({
  code,
  cacheBase,
  selectedAsOfDate,
}: {
  code: string
  cacheBase: string
  selectedAsOfDate: string | null
}) {
  const { data, loading, error } = useCached<StockProfile>(`${cacheBase}|overview`, () =>
    api.stockProfile(code),
  )
  const { isWatched } = useWatchlist()
  if (loading) return <LoadState loading className="px-4 py-10" />
  if (error) return <LoadState error={error} className="px-4 py-10" />
  if (!data) return <LoadState emptyText="暂无该股概况" className="px-4 py-10" />
  const latest = data.latest
  return (
    <div>
      <AsOfNotice
        selected={selectedAsOfDate}
        actual={latest?.trade_date ?? data.last_date ?? null}
      />
      <Section title="行情">
        <Row label="最新本地日线" value={latest?.trade_date ?? '—'} />
        <Row label="收盘 / 涨跌" value={`${fmtNum(latest?.close)} / ${fmtPct(latest?.pct_chg)}`} />
        <Row label="近 5 日" value={fmtPct(data.pct_chg_5d)} />
        <Row label="本地日线覆盖" value={`${fmtInt(data.day_count)} 根（${data.first_date ?? '—'} ~ ${data.last_date ?? '—'}）`} />
        <Row label="自选" value={isWatched(code) || data.in_watchlist ? '已在自选' : '不在自选'} />
      </Section>
      <Section title="估值与股本（建池快照，按最新价缩放）">
        <Row label="总市值" value={fmtAmount(data.total_mv)} />
        <Row label="自由流通市值" value={fmtAmount(data.free_float_mv)} />
        <Row label="实际换手" value={fmtNum(data.actual_turnover, 2, '%')} />
        <Row label="预测 PE" value={fmtNum(data.pe_forecast, 1)} />
        <Row label="快照日期" value={data.total_mv_asof ?? '—'} />
      </Section>
      <Section title="涨停 / 龙虎榜">
        <Row label="历史涨停次数" value={fmtInt(data.limit_up_dates.length)} />
        <Row label="龙虎榜次数" value={fmtInt(data.lhb_count)} />
        <Row
          label="最近涨停"
          value={data.limit_up_dates.slice(0, 3).join('、') || '—'}
        />
      </Section>
    </div>
  )
}

function KLineTab({
  code,
  cacheBase,
  selectedAsOfDate,
}: {
  code: string
  cacheBase: string
  selectedAsOfDate: string | null
}) {
  const { data, loading, error } = useCached(
    `${cacheBase}|kline`,
    () => api.stockDaily(code, { days: 120, fq: 'qfq', volAdjust: true, period: 'day' }),
  )
  if (loading) return <LoadState loading className="px-4 py-10" />
  if (error) return <LoadState error={error} className="px-4 py-10" />
  if (!data || data.length === 0)
    return <LoadState emptyText="本地暂无 K 线，打开完整个股详情查看" className="px-4 py-10" />
  const lastDate = data[data.length - 1]?.trade_date ?? null
  return (
    <div className="px-1 pt-2">
      <AsOfNotice selected={selectedAsOfDate} actual={lastDate} />
      <KLineChart bars={dailyBars(data)} height={240} />
      <div className="px-3 pb-3 text-[12px] text-fg-dim">
        前复权日 K · 最近 {data.length} 根 · 不自动补历史
      </div>
    </div>
  )
}

function ThemesTab({
  code,
  cacheBase,
  selectedAsOfDate,
}: {
  code: string
  cacheBase: string
  selectedAsOfDate: string | null
}) {
  const { data, loading, error } = useCached<StockThemes>(`${cacheBase}|themes`, () =>
    api.stockThemes(code),
  )
  if (loading) return <LoadState loading className="px-4 py-10" />
  if (error) return <LoadState error={error} className="px-4 py-10" />
  if (!data || data.themes.length === 0)
    return <LoadState emptyText="暂无题材归属" className="px-4 py-10" />
  return (
    <div className="px-4 py-3 text-[13px]">
      <AsOfNotice
        selected={selectedAsOfDate}
        actual={data.board_date}
        historicalMissing="历史题材缺失"
      />
      <div className="mb-2 text-[12px] text-fg-dim">
        板块表现日期：{data.board_date ?? '—'}
      </div>
      <ul className="space-y-1">
        {data.themes.map((item) => (
          <li key={item.concept} className="flex items-baseline justify-between gap-2">
            <span className="text-fg">{item.concept}</span>
            <span className={`num ${toneOf(item.pct_chg)}`}>{fmtPct(item.pct_chg)}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function LimitTab({ code, cacheBase }: { code: string; cacheBase: string }) {
  const { data, loading, error } = useCached<StockLimitRecord[]>(
    `${cacheBase}|limit`,
    () => api.stockLimitHistory(code, 60),
  )
  if (loading) return <LoadState loading className="px-4 py-10" />
  if (error) return <LoadState error={error} className="px-4 py-10" />
  if (!data || data.length === 0)
    return <LoadState emptyText="近期没有涨停 / 炸板记录" className="px-4 py-10" />
  return (
    <div className="overflow-auto">
      <table className="grid-table min-w-full text-[13px]">
        <thead>
          <tr>
            <th>日期</th>
            <th>类型</th>
            <th>连板</th>
            <th>封单</th>
            <th>首封</th>
          </tr>
        </thead>
        <tbody>
          {data.map((row) => (
            <tr key={`${row.trade_date}-${row.pool_type}`}>
              <td className="num">{row.trade_date}</td>
              <td>{row.pool_type === 'up' ? '涨停' : '炸板'}</td>
              <td className="num">{row.consecutive ? `${row.consecutive} 板` : '—'}</td>
              <td className="num">{fmtAmount(row.seal_amount)}</td>
              <td className="num">{row.first_seal_time ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function fundsOption(rows: StockDde['rows']): ChartOption {
  const dates = rows.map((row) => row.trade_date.slice(5))
  return {
    grid: { left: 8, right: 12, top: 28, bottom: 4, containLabel: true },
    tooltip: { ...TOOLTIP, trigger: 'axis' as const },
    xAxis: {
      type: 'category' as const,
      data: dates,
      axisLabel: { color: CHART.fgDim, fontSize: 11 },
      axisLine: { lineStyle: { color: CHART.line } },
    },
    yAxis: {
      type: 'value' as const,
      axisLabel: { color: CHART.fgDim, fontSize: 11, formatter: (v: number) => `${(v / 1e8).toFixed(1)}亿` },
      splitLine: { lineStyle: { color: CHART.soft, type: 'dashed' as const } },
    },
    series: [
      {
        type: 'bar' as const,
        name: '主力净流入',
        data: rows.map((row) => row.net_inflow),
        itemStyle: { color: CHART.accent },
        barMaxWidth: 10,
      },
    ],
  }
}

function FundsTab({ code, cacheBase }: { code: string; cacheBase: string }) {
  const { data, loading, error } = useCached<StockDde>(`${cacheBase}|funds`, () =>
    api.stockDde(code, 30, false),
  )
  if (loading) return <LoadState loading className="px-4 py-10" />
  if (error) return <LoadState error={error} className="px-4 py-10" />
  if (!data || data.rows.length === 0)
    return <LoadState emptyText="本地暂无资金数据（侧栏不触发外部取数）" className="px-4 py-10" />
  const recent = data.rows.slice(-10)
  return (
    <div>
      <div className="px-1 pt-2">
        <EChart option={fundsOption(recent)} height={180} />
      </div>
      <div className="overflow-auto">
        <table className="grid-table min-w-full text-[13px]">
          <thead>
            <tr>
              <th>日期</th>
              <th>主力净流入</th>
              <th>5日 DDE</th>
            </tr>
          </thead>
          <tbody>
            {[...data.rows].reverse().slice(0, 10).map((row) => (
              <tr key={row.trade_date}>
                <td className="num">{row.trade_date}</td>
                <td className={`num ${toneOf(row.net_inflow)}`}>{fmtAmount(row.net_inflow)}</td>
                <td className={`num ${toneOf(row.dde)}`}>{fmtAmount(row.dde)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="px-3 py-2 text-[12px] text-fg-dim">
        {data.note ?? 'iFinD 口径 · 只读本地库'}
      </div>
    </div>
  )
}

function LhbTab({ code, cacheBase }: { code: string; cacheBase: string }) {
  const { data, loading, error } = useCached<LhbItem[]>(`${cacheBase}|lhb`, () =>
    api.stockLhb(code, 30),
  )
  if (loading) return <LoadState loading className="px-4 py-10" />
  if (error) return <LoadState error={error} className="px-4 py-10" />
  if (!data || data.length === 0)
    return <LoadState emptyText="近期没有龙虎榜记录" className="px-4 py-10" />
  return (
    <div className="overflow-auto">
      <table className="grid-table min-w-full text-[13px]">
        <thead>
          <tr>
            <th>日期</th>
            <th>净买入</th>
            <th>原因</th>
          </tr>
        </thead>
        <tbody>
          {data.map((row) => (
            <tr key={`${row.trade_date}-${row.reason}`}>
              <td className="num">{row.trade_date}</td>
              <td className={`num ${toneOf(row.net_buy)}`}>{fmtAmount(row.net_buy)}</td>
              <td className="text-fg-muted">{row.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

const KNOWN_BY_LABEL: Record<string, string> = {
  CLOSE: '收盘前已取到（≤15:00）',
  PRIOR_DAY: '前一日已取到',
  SAME_DAY_UNKNOWN: '同日取到，无法证明盘前',
  LATER: '观察日之后才取到',
  POST_CLOSE_UNKNOWN: '收盘后才取到（>15:00）',
  END_OF_DAY_FACT: '日终事实（只作复盘）',
  FORWARD: 'D+1 后验',
  UNKNOWN: '来源时间未知',
}

const BUCKET_LABEL: Record<string, string> = {
  AT_CUTOFF_KNOWN: '已知',
  UNKNOWN_OR_LATER: '后得/未知',
  FORWARD_OUTCOME: '后验',
}

const CONTEXT_STATUS_LABEL: Record<string, string> = {
  VERIFIED: '可验证',
  PARTIAL: '部分可信',
  PENDING: '待观察',
  UNVERIFIABLE: '不可验证',
  NOT_COLLECTED: '未采集',
}

function knownTone(knownBy: string): string {
  if (knownBy === 'PRIOR_DAY' || knownBy === 'CLOSE') return 'text-up'
  if (knownBy === 'FORWARD') return 'text-fg-muted'
  return 'text-warn'
}

/** 日终事实没有首次入库时间，只能用于复盘，不能算「当时已知」 */
function EndOfDayFactSection({ fact }: { fact?: StockResearchEndOfDayFact | null }) {
  if (!fact) return null
  return (
    <Section title="日终事实（只作复盘）">
      <div className="mb-1 text-[12px] leading-relaxed text-fg-muted">
        {fact.note ?? '无首次入库时间，不能证明 15:00 当时已知。'}
      </div>
      <Row
        label="当日日线"
        value={fact.daily ? `${fmtNum(fact.daily.close)} / ${fmtPct(fact.daily.pct_chg)}` : '—'}
      />
      <Row
        label="市场情绪"
        value={
          fact.sentiment
            ? `涨停 ${fmtNum(fact.sentiment.limit_up_count ?? null)} 家 · 最高 ${fmtNum(
                fact.sentiment.max_consecutive ?? null,
              )} 板`
            : '—'
        }
      />
    </Section>
  )
}

function ThemeList({ items }: { items: StockResearchTheme[] }) {
  if (items.length === 0) return <div className="text-[13px] text-fg-muted">—</div>
  return (
    <ul className="space-y-1 text-[13px]">
      {items.map((item, index) => (
        <li key={`${item.name}-${item.sector_code ?? index}`} className="flex items-baseline justify-between gap-2">
          <span className="text-fg">
            {item.name}
            {item.taxonomy ? <span className="ml-1 text-[11px] text-fg-dim">{item.taxonomy}</span> : null}
          </span>
          <span className={`text-[11px] ${knownTone(item.known_by)}`}>
            {KNOWN_BY_LABEL[item.known_by] ?? item.known_by}
          </span>
        </li>
      ))}
    </ul>
  )
}

function EvidenceTab({
  code,
  cacheBase,
  selectedAsOfDate,
}: {
  code: string
  cacheBase: string
  selectedAsOfDate: string | null
}) {
  const { data, loading, error } = useCached<StockResearchContext>(
    // 缓存键带时点协议版本与 cutoff：口径升级或切换 cutoff 时不会命中旧响应
    `${cacheBase}|evidence|research-evidence-v3|CLOSE`,
    () => api.stockResearchContext(code, selectedAsOfDate),
  )
  if (loading) return <LoadState loading className="px-4 py-10" />
  if (error) return <LoadState error={error} className="px-4 py-10" />
  if (!data) return <LoadState emptyText="暂无观察日研究依据" className="px-4 py-10" />

  const legacy = data.as_known_on_day
  // 新后端返回三桶；旧后端没有 at_cutoff_known 时回落到兼容子集（且仍剔除 LATER）
  const known = data.at_cutoff_known ?? {
    previous_close_date: legacy.previous_close_date,
    pool_status: legacy.limit_status,
    board_height: legacy.board_height,
    observed_pool_membership: legacy.observed_pool_membership,
    pool_scope_complete: legacy.pool_scope_complete ?? true,
    unconfirmed_pool_sources: legacy.unconfirmed_pool_sources ?? [],
    daily: legacy.daily,
    themes: legacy.themes.filter((item) => item.known_by === 'PRIOR_DAY'),
    concepts: [],
    sentiment: legacy.sentiment,
  }
  const incompletePoolSources = known.unconfirmed_pool_sources ?? []
  const unknown = data.unknown_or_later ?? {
    themes: legacy.themes.filter((item) => item.known_by !== 'PRIOR_DAY'),
    concepts: legacy.concepts,
    reasons: [],
  }
  const outcome = data.later_outcome
  const forward = data.evidence.filter((item) =>
    item.time_bucket ? item.time_bucket === 'FORWARD_OUTCOME' : item.is_forward_known,
  )
  const backward = data.evidence.filter((item) =>
    item.time_bucket ? item.time_bucket !== 'FORWARD_OUTCOME' : !item.is_forward_known,
  )

  return (
    <div className="pb-4">
      <div className="border-b border-line-soft px-4 py-2 text-[12px] leading-relaxed">
        <div className="num text-fg-dim">
          观察日 {data.resolved_trade_date}
          {data.requested_as_of ? `（请求 ${data.requested_as_of}）` : '（最新交易日）'}
          {' · '}时点 {data.cutoff ?? 'CLOSE'}
          {' · '}截止 {data.cutoff_at ?? '—'}
          {' · '}数据截至 {data.data_as_of ?? '—'}
        </div>
        {data.time_policy ? (
          <div className="num mt-0.5 text-[11px] text-fg-dim">
            时区策略 {data.time_policy.naive_timestamp_policy}
            （历史 naive 时间{data.time_policy.naive_timezone_trusted ? '按 +08:00 解释' : '不信任，降级未知'}）
          </div>
        ) : null}
        <div className="mt-0.5">
          <span className={data.quality.status === 'PARTIAL' ? 'text-warn' : 'text-fg'}>
            {CONTEXT_STATUS_LABEL[data.quality.status] ?? data.quality.status}
          </span>
          <span className="ml-2 text-fg-muted">
            历史信号资格：{data.historical_signal_eligible ? '是' : '否（只读展示）'}
          </span>
        </div>
        {data.quality.reasons.length ? (
          <div className="mt-0.5 text-[11px] text-warn">{data.quality.reasons.join(' · ')}</div>
        ) : null}
      </div>

      <Section title="观察日已知（截至 15:00）">
        <Row label="上一交易日" value={known.previous_close_date ?? '—'} />
        <Row
          label="涨跌状态"
          value={
            known.pool_status === 'UP'
              ? `涨停${known.board_height ? ` · ${known.board_height} 板` : ''}`
              : known.pool_status
          }
        />
        <Row label="观察到的池" value={known.observed_pool_membership.join('、') || '—'} />
        {incompletePoolSources.length ? (
          <div className="mt-1 text-[11px] leading-relaxed text-warn">
            以下池来源未能在 15:00 前证明：{incompletePoolSources.join('、')}；不能据此推断该股不在其余池，也不代表无冲突。
          </div>
        ) : null}
        <div className="mt-2 text-[12px] text-fg-dim">题材（观察日之前已取到）</div>
        <ThemeList items={known.themes} />
        <div className="mt-2 text-[12px] text-fg-dim">涨停天梯题材</div>
        <ThemeList items={known.concepts} />
      </Section>

      <EndOfDayFactSection fact={data.end_of_day_fact} />

      <Section title="不知道 / 观察日之后才取到">
        {unknown.pool ? (
          <div className="mb-2 text-[12px] leading-relaxed text-warn">
            池成员/冲突未能在 15:00 前证明：
            {unknown.pool.observed_pool_membership.join('、') || '（无成员记录）'}
            {` · ${unknown.pool.status} · ${unknown.pool.reason_code}`}
          </div>
        ) : null}
        {unknown.themes.length === 0 && unknown.concepts.length === 0 ? (
          <div className="text-[13px] text-fg-muted">没有无法校准的来源记录</div>
        ) : (
          <>
            <div className="mb-1 text-[12px] leading-relaxed text-warn">
              以下资料不能证明在观察日已知，不可作为当时交易信号。
            </div>
            <ThemeList items={unknown.themes} />
            <ThemeList items={unknown.concepts} />
            {unknown.reasons.length ? (
              <div className="mt-2 text-[11px] text-fg-dim">{unknown.reasons.join(' · ')}</div>
            ) : null}
          </>
        )}
      </Section>

      <Section title="D+1 后验（只供复盘）">
        <Row label="结果日" value={outcome.outcome_date ?? '—'} />
        <Row label="状态" value={CONTEXT_STATUS_LABEL[outcome.status] ?? outcome.status} />
        <Row label="涨跌幅" value={fmtPct(outcome.pct_chg)} />
        <Row label="收盘" value={fmtNum(outcome.close)} />
        <Row
          label="是否再涨停"
          value={outcome.limit_up_again == null ? '不可验证' : outcome.limit_up_again ? '是' : '否'}
        />
      </Section>

      <Section title="证据链">
        <div className="overflow-auto">
          <table className="grid-table min-w-full text-[12px]">
            <thead>
              <tr>
                <th>断言</th>
                <th>来源</th>
                <th>日期</th>
                <th>时间桶</th>
                <th>时点</th>
                <th>质量</th>
              </tr>
            </thead>
            <tbody>
              {[...backward, ...forward].map((item) => (
                <tr key={`${item.claim}-${item.source_table}-${item.source_trade_date}-${item.known_by}`}>
                  <td className="!text-left">
                    <div>{item.claim}</div>
                    <div className="text-[11px] text-fg-muted">{item.summary ?? '—'}</div>
                  </td>
                  <td>{item.source_table}</td>
                  <td className="num">{item.source_trade_date ?? '—'}</td>
                  <td className="text-[11px] text-fg-muted">
                    {BUCKET_LABEL[item.time_bucket ?? ''] ?? '—'}
                  </td>
                  <td className={`text-[11px] ${knownTone(item.known_by)}`}>
                    {KNOWN_BY_LABEL[item.known_by] ?? item.known_by}
                  </td>
                  <td className="text-[11px]">
                    {CONTEXT_STATUS_LABEL[item.quality] ?? item.quality}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <div className="px-4 text-[12px] leading-relaxed text-fg-dim">
        {data.notes.map((note) => (
          <div key={note}>· {note}</div>
        ))}
      </div>
    </div>
  )
}

export default function StockResearchDrawer() {
  const {
    code,
    open,
    activeTab,
    selectedAsOfDate,
    closeStockResearch,
    selectResearchTab,
    multiWindowSync,
    setMultiWindowSync,
  } = useStockResearch()
  const { me } = useAuth()
  const [name, setName] = useState<string | null>(null)
  const [dataAsOf, setDataAsOf] = useState<string | null>(null)
  const panelRef = useRef<HTMLElement | null>(null)
  const closeRef = useRef<HTMLButtonElement | null>(null)

  // 打开时把焦点移进侧栏，关闭时还给触发它的「研」按钮；Tab 在侧栏内循环
  useEffect(() => {
    if (!open) return
    const previous = document.activeElement as HTMLElement | null
    const timer = window.setTimeout(() => closeRef.current?.focus(), 0)
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        closeStockResearch()
        return
      }
      if (event.key !== 'Tab') return
      const nodes = panelRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], input:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )
      if (!nodes || nodes.length === 0) return
      const first = nodes[0]
      const last = nodes[nodes.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      window.clearTimeout(timer)
      document.removeEventListener('keydown', onKeyDown)
      previous?.focus?.()
    }
  }, [open, closeStockResearch])

  // 名称与「概览」共用同一份 profile 缓存键：并发时由 cache 的在途去重合并成一次请求
  useEffect(() => {
    setName(null)
    setDataAsOf(null)
    if (!open || !code || !me) return
    let active = true
    loadCached(`${me.id}|${code}|${selectedAsOfDate ?? 'latest'}|overview`, () =>
      api.stockProfile(code),
    )
      .then((profile) => {
        if (!active) return
        setName(profile.name)
        setDataAsOf(profile.latest?.trade_date ?? profile.last_date ?? null)
      })
      .catch(() => {})
    return () => {
      active = false
    }
  }, [open, code, me, selectedAsOfDate])

  if (!open || !code) return null
  const cacheBase = me ? `${me.id}|${code}|${selectedAsOfDate ?? 'latest'}` : ''

  return (
    <div className="fixed inset-0 z-50 flex justify-end" role="dialog" aria-modal="true" aria-label="股票研究">
      <div className="absolute inset-0 bg-ink-950/40" onClick={closeStockResearch} aria-hidden />
      <aside
        ref={panelRef}
        tabIndex={-1}
        className="relative flex h-full w-full max-w-[440px] flex-col border-l border-line bg-ink-900 shadow-[-12px_0_32px_rgba(15,23,42,0.18)]"
      >
        <div className="flex items-start justify-between border-b border-line px-4 py-3">
          <div>
            <div className="flex items-baseline gap-2">
              <span className="text-[16px] font-semibold text-fg">{name ?? code}</span>
              <span className="num text-[13px] text-fg-dim">{code}</span>
            </div>
            <div className="mt-0.5 text-[12px] text-fg-dim">
              只读本地数据 ·{' '}
              {selectedAsOfDate ? `来源观察日 ${selectedAsOfDate}` : '最新本地资料'}
            </div>
            {selectedAsOfDate ? (
              <div className="mt-0.5 text-[12px] leading-relaxed text-warn">
                此页展示最新本地资料（截至 {dataAsOf ?? '—'}），不代表所选历史日当时已知
              </div>
            ) : dataAsOf ? (
              <div className="mt-0.5 text-[12px] text-fg-dim">本地数据截至 {dataAsOf}</div>
            ) : null}
          </div>
          <button
            ref={closeRef}
            type="button"
            aria-label="关闭研究侧栏"
            onClick={closeStockResearch}
            className="px-1 text-[16px] text-fg-muted transition-colors hover:text-fg"
          >
            ✕
          </button>
        </div>

        <div className="flex flex-wrap gap-1 border-b border-line-soft px-3 py-2">
          {RESEARCH_TABS.map((tab) => (
            <button
              key={tab}
              type="button"
              aria-pressed={activeTab === tab}
              onClick={() => selectResearchTab(tab as ResearchTab)}
              className={[
                'border px-2 py-[2px] text-[13px] transition-colors',
                activeTab === tab
                  ? 'border-accent/60 text-accent'
                  : 'border-line text-fg-muted hover:text-fg',
              ].join(' ')}
            >
              {RESEARCH_TAB_LABELS[tab]}
            </button>
          ))}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {!me ? (
            <LoadState emptyText="登录后才能查看个股研究" className="px-4 py-10" />
          ) : activeTab === 'overview' ? (
            <OverviewTab
              code={code}
              cacheBase={cacheBase}
              selectedAsOfDate={selectedAsOfDate}
            />
          ) : activeTab === 'kline' ? (
            <KLineTab code={code} cacheBase={cacheBase} selectedAsOfDate={selectedAsOfDate} />
          ) : activeTab === 'themes' ? (
            <ThemesTab code={code} cacheBase={cacheBase} selectedAsOfDate={selectedAsOfDate} />
          ) : activeTab === 'evidence' ? (
            <EvidenceTab
              code={code}
              cacheBase={cacheBase}
              selectedAsOfDate={selectedAsOfDate}
            />
          ) : activeTab === 'limit' ? (
            <LimitTab code={code} cacheBase={cacheBase} />
          ) : activeTab === 'funds' ? (
            <FundsTab code={code} cacheBase={cacheBase} />
          ) : (
            <LhbTab code={code} cacheBase={cacheBase} />
          )}
        </div>

        <div className="flex items-center justify-between gap-2 border-t border-line px-4 py-2.5 text-[12px] text-fg-dim">
          <label className="flex cursor-pointer items-center gap-1">
            <input
              type="checkbox"
              checked={multiWindowSync}
              onChange={(event) => setMultiWindowSync(event.target.checked)}
            />
            多窗口同步
          </label>
          <Link
            to={`/stock/${encodeURIComponent(code)}`}
            onClick={closeStockResearch}
            className="text-accent hover:underline"
          >
            打开完整个股详情 →
          </Link>
        </div>
      </aside>
    </div>
  )
}
