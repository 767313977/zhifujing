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

const TTL = 3 * 60 * 1000

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
    loadCached(key, () => loaderRef.current(), TTL)
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

function OverviewTab({ code, cacheBase }: { code: string; cacheBase: string }) {
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

function KLineTab({ code, cacheBase }: { code: string; cacheBase: string }) {
  const { data, loading, error } = useCached(
    `${cacheBase}|kline`,
    () => api.stockDaily(code, { days: 120, fq: 'qfq', volAdjust: true, period: 'day' }),
  )
  if (loading) return <LoadState loading className="px-4 py-10" />
  if (error) return <LoadState error={error} className="px-4 py-10" />
  if (!data || data.length === 0)
    return <LoadState emptyText="本地暂无 K 线，打开完整个股详情查看" className="px-4 py-10" />
  return (
    <div className="px-1 pt-2">
      <KLineChart bars={dailyBars(data)} height={240} />
      <div className="px-3 pb-3 text-[12px] text-fg-dim">
        前复权日 K · 最近 {data.length} 根 · 不自动补历史
      </div>
    </div>
  )
}

function ThemesTab({ code, cacheBase }: { code: string; cacheBase: string }) {
  const { data, loading, error } = useCached<StockThemes>(`${cacheBase}|themes`, () =>
    api.stockThemes(code),
  )
  if (loading) return <LoadState loading className="px-4 py-10" />
  if (error) return <LoadState error={error} className="px-4 py-10" />
  if (!data || data.themes.length === 0)
    return <LoadState emptyText="暂无题材归属" className="px-4 py-10" />
  return (
    <div className="px-4 py-3 text-[13px]">
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

export default function StockResearchDrawer() {
  const {
    code,
    open,
    activeTab,
    asOfDate,
    closeStockResearch,
    selectResearchTab,
    multiWindowSync,
    setMultiWindowSync,
  } = useStockResearch()
  const { me } = useAuth()
  const [name, setName] = useState<string | null>(null)

  // 关闭时用 Esc：与站内导航抽屉一致
  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeStockResearch()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [open, closeStockResearch])

  // 名称从概览的缓存里取不到时，用 profile 请求顺带补一次（仍走缓存）
  useEffect(() => {
    setName(null)
    if (!open || !code || !me) return
    let active = true
    loadCached(`research:${me.id}|${code}|name`, () => api.stockProfile(code), TTL)
      .then((profile) => {
        if (active) setName(profile.name)
      })
      .catch(() => {})
    return () => {
      active = false
    }
  }, [open, code, me])

  if (!open || !code) return null
  const cacheBase = me ? `${me.id}|${code}|${asOfDate ?? 'latest'}` : ''

  return (
    <div className="fixed inset-0 z-50 flex justify-end" role="dialog" aria-modal="true" aria-label="股票研究">
      <div className="absolute inset-0 bg-ink-950/40" onClick={closeStockResearch} aria-hidden />
      <aside className="relative flex h-full w-full max-w-[440px] flex-col border-l border-line bg-ink-900 shadow-[-12px_0_32px_rgba(15,23,42,0.18)]">
        <div className="flex items-start justify-between border-b border-line px-4 py-3">
          <div>
            <div className="flex items-baseline gap-2">
              <span className="text-[16px] font-semibold text-fg">{name ?? code}</span>
              <span className="num text-[13px] text-fg-dim">{code}</span>
            </div>
            <div className="mt-0.5 text-[12px] text-fg-dim">
              只读本地数据 · {asOfDate ? `数据日 ${asOfDate}` : '最新本地数据'}
            </div>
          </div>
          <button
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
            <OverviewTab code={code} cacheBase={cacheBase} />
          ) : activeTab === 'kline' ? (
            <KLineTab code={code} cacheBase={cacheBase} />
          ) : activeTab === 'themes' ? (
            <ThemesTab code={code} cacheBase={cacheBase} />
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
