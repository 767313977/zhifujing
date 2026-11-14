/**
 * 盘后数据质量（S4-B）—— **只读诊断**。
 *
 * 这里回答「今天这个榜单为什么是空的 / 为什么不能拿它做历史信号」，
 * 不提供一键修复或补采按钮：数据缺口要么等下次采集，要么由运维显式处理。
 */

import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { QualityOverview, QualitySourceItem, QualityTimeline } from '../api/types'
import LoadState from '../components/LoadState'
import Panel from '../components/Panel'

const SOURCE_LABEL: Record<string, string> = {
  stock_daily: '个股日线',
  limit_pool: '涨停 / 炸板池',
  sector_member: '板块成分快照',
  market_sentiment: '市场情绪',
  auction_snapshot: '集合竞价',
  trade_calendar: '交易日历',
}

const STATUS_LABEL: Record<string, string> = {
  VERIFIED_SOURCE: '可信（本来源口径）',
  PARTIAL: '部分可信',
  UNVERIFIABLE: '不可验证',
  NOT_COLLECTED: '未采集',
  SOURCE_ERROR: '查询异常',
  PENDING: '待观察',
  NOT_TRADE_DAY: '非交易日',
}

const STATUS_TONE: Record<string, string> = {
  VERIFIED_SOURCE: 'border-up/40 text-up',
  PARTIAL: 'border-warn/50 text-warn',
  UNVERIFIABLE: 'border-danger/50 text-danger',
  NOT_COLLECTED: 'border-line text-fg-dim',
  SOURCE_ERROR: 'border-danger/60 text-danger',
  PENDING: 'border-line text-fg-dim',
  NOT_TRADE_DAY: 'border-line text-fg-dim',
}

const TIMELINE_TONE: Record<string, string> = {
  VERIFIED_SOURCE: 'bg-up/25 border-up/40',
  PARTIAL: 'bg-warn/25 border-warn/50',
  UNVERIFIABLE: 'bg-danger/25 border-danger/50',
  NOT_COLLECTED: 'bg-ink-800 border-line',
  SOURCE_ERROR: 'bg-danger/50 border-danger',
  PENDING: 'bg-ink-800 border-line',
  NOT_TRADE_DAY: 'bg-ink-900 border-line',
}

const FIELD_CLASS =
  'num border border-line bg-ink-900 px-2 py-[3px] text-[13px] text-fg outline-none focus:border-fg-dim'

const FILTERS = [
  { key: 'all', label: '全部' },
  { key: 'trusted', label: '可信' },
  { key: 'partial', label: '部分' },
  { key: 'missing', label: '未采集/不可验证' },
] as const

type FilterKey = (typeof FILTERS)[number]['key']

const SOURCE_LINK: Record<string, { to: string; label: string }> = {
  limit_pool: { to: '/limit-up', label: '去涨停复盘' },
  sector_member: { to: '/sectors', label: '去板块题材' },
  market_sentiment: { to: '/sentiment', label: '去情绪周期' },
  auction_snapshot: { to: '/auction', label: '去竞价强度' },
}

function matches(item: QualitySourceItem, filter: FilterKey): boolean {
  if (filter === 'all') return true
  if (filter === 'trusted') return item.status === 'VERIFIED_SOURCE'
  if (filter === 'partial') return item.status === 'PARTIAL'
  return ['NOT_COLLECTED', 'UNVERIFIABLE', 'NOT_TRADE_DAY', 'PENDING', 'SOURCE_ERROR'].includes(item.status)
}

function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`border px-2 py-[2px] text-[11px] ${STATUS_TONE[status] ?? 'border-line text-fg-dim'}`}>
      {STATUS_LABEL[status] ?? status}
    </span>
  )
}

function Detail({ item }: { item: QualitySourceItem }) {
  const extra = item as Record<string, unknown>
  const taxonomies = Array.isArray(extra.taxonomies) ? extra.taxonomies : []
  const frames = Array.isArray(extra.frames) ? extra.frames : []
  const missing = Array.isArray(extra.missing_fields) ? extra.missing_fields : []
  const poolDims = ['up', 'broken', 'down']
    .map((key) => {
      const row = extra[`pool_${key}`]
      if (!row || typeof row !== 'object') return null
      const record = row as Record<string, unknown>
      return `${key.toUpperCase()} ${String(record.status ?? '—')}（${String(
        record.rows ?? 0,
      )} 行）`
    })
    .filter(Boolean)
  const auctionResult = extra.auction_result_status ? String(extra.auction_result_status) : null
  const postOpen = extra.post_open_status ? String(extra.post_open_status) : null
  const combined = extra.combined_view_status ? String(extra.combined_view_status) : null
  return (
    <div className="grid gap-2 bg-ink-850 px-4 py-3 text-[12px] lg:grid-cols-2">
      <div>
        <div className="text-fg-dim">最近一次尝试</div>
        <div className="num text-fg-muted">
          {item.latest_attempt
            ? `${String(item.latest_attempt.status ?? '—')} · ${
                String(item.latest_attempt.created_at ?? '—')
              }`
            : '—'}
        </div>
        <div className="mt-2 text-fg-dim">最近有效快照</div>
        <div className="num text-fg-muted">{item.latest_valid_snapshot ?? '—'}</div>
        <div className="mt-2 text-fg-dim">来源最新观测日</div>
        <div className="num text-fg-muted">{item.latest_observation_date ?? '—'}</div>
        {item.status === 'SOURCE_ERROR' ? (
          <div className="mt-2 text-danger">数据源查询异常，无法断言未采集</div>
        ) : null}
        {item.note ? <div className="mt-2 text-fg-dim">{item.note}</div> : null}
      </div>
      <div>
        <div className="text-fg-dim">原因码</div>
        <div className="mt-1 flex flex-wrap gap-1">
          {item.reason_codes.length ? (
            item.reason_codes.map((code) => (
              <span key={code} className="rounded border border-line-soft px-1.5 py-0.5 text-[11px]">
                {code}
              </span>
            ))
          ) : (
            <span className="text-fg-muted">—</span>
          )}
        </div>
        {combined ? (
          <div className="mt-2 text-fg-muted">
            组合视图：{STATUS_LABEL[combined] ?? combined}
            {extra.previous_snapshot_available ? ' · 有历史快照' : ''}
            {extra.current_attempt_failed ? ' · 最近尝试失败' : ''}
          </div>
        ) : null}
        {poolDims.length ? <div className="mt-2 num text-fg-muted">三池：{poolDims.join(' · ')}</div> : null}
        {auctionResult || postOpen ? (
          <div className="mt-2 num text-fg-muted">
            竞价终值：{auctionResult ?? '—'} · 开盘承接：{postOpen ?? '—'}
          </div>
        ) : null}
        {taxonomies.length ? (
          <div className="mt-2 text-fg-muted">
            分类覆盖：
            {(taxonomies as Array<Record<string, unknown>>)
              .map(
                (row) =>
                  `${String(row.taxonomy)} ${String(row.observed_sectors)}/${String(
                    row.expected_sectors,
                  )}`,
              )
              .join(' · ')}
          </div>
        ) : null}
        {frames.length ? (
          <div className="mt-2 num text-fg-muted">竞价帧：{frames.join(' · ')}</div>
        ) : null}
        {missing.length ? (
          <div className="mt-2 text-fg-muted">空字段：{missing.join(' · ')}</div>
        ) : null}
      </div>
    </div>
  )
}

export default function DataQuality() {
  const [date, setDate] = useState('')
  const [days, setDays] = useState(30)
  const [filter, setFilter] = useState<FilterKey>('all')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [source, setSource] = useState('limit_pool')
  const [data, setData] = useState<QualityOverview | null>(null)
  const [timeline, setTimeline] = useState<QualityTimeline | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [timelineError, setTimelineError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setLoading(true)
    setError(null)
    api
      .qualityOverview(date || null, days)
      .then((payload) => {
        if (active) setData(payload)
      })
      .catch((err: Error) => {
        if (active) {
          setData(null)
          setError(err.message || '接口出错')
        }
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [date, days])

  useEffect(() => {
    let active = true
    setTimelineError(null)
    api
      .qualityTimeline(source, days)
      .then((payload) => {
        if (active) setTimeline(payload)
      })
      .catch((err: Error) => {
        if (active) {
          setTimeline(null)
          setTimelineError(err.message || '接口出错')
        }
      })
    return () => {
      active = false
    }
  }, [source, days])

  const items = useMemo(
    () => (data?.items ?? []).filter((item) => matches(item, filter)),
    [data, filter],
  )

  return (
    <div className="mx-auto grid max-w-[1400px] gap-4 px-4 py-4">
      <Panel
        title="盘后数据质量"
        meta={data ? `数据库 ${data.database ?? '—'} · 观察日 ${data.as_of} · ${data.quality_version ?? 'data-quality-v2'}` : undefined}
      >
        <div className="flex flex-wrap items-end gap-3 border-b border-line-soft px-4 py-3 text-[13px]">
          <label className="grid gap-1">
            <span className="text-fg-dim">观察日</span>
            <input
              type="date"
              value={date}
              onChange={(event) => setDate(event.target.value)}
              className={`${FIELD_CLASS} w-[150px]`}
            />
          </label>
          <label className="grid gap-1">
            <span className="text-fg-dim">窗口</span>
            <select
              value={days}
              onChange={(event) => setDays(Number(event.target.value))}
              className={`${FIELD_CLASS} w-[110px]`}
            >
              {[30, 60, 120, 250].map((value) => (
                <option key={value} value={value}>
                  {value} 日
                </option>
              ))}
            </select>
          </label>
          <div className="flex flex-wrap gap-1">
            {FILTERS.map((entry) => (
              <button
                key={entry.key}
                type="button"
                onClick={() => setFilter(entry.key)}
                className={`rounded border px-2 py-1 text-[12px] ${
                  filter === entry.key
                    ? 'border-accent text-accent'
                    : 'border-line-soft text-fg-muted'
                }`}
              >
                {entry.label}
              </button>
            ))}
          </div>
          <div className="ml-auto text-[12px] text-fg-dim">
            只读诊断，不提供一键修复 / 回补
          </div>
        </div>

        {loading ? (
          <LoadState loading className="px-4 py-10" />
        ) : error ? (
          <LoadState error={error} className="px-4 py-10" />
        ) : items.length === 0 ? (
          <LoadState emptyText="当前筛选下没有数据源" className="px-4 py-10" />
        ) : (
          <div className="overflow-auto">
            <table className="grid-table min-w-full text-[12px]" data-testid="quality-table">
              <thead>
                <tr>
                  <th>数据源</th>
                  <th>状态</th>
                  <th>已观察</th>
                  <th>板块覆盖</th>
                  <th>完成证明</th>
                  <th>最新观测日</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <ItemRow
                    key={item.source}
                    item={item}
                    expanded={expanded === item.source}
                    onToggle={() =>
                      setExpanded((current) => (current === item.source ? null : item.source))
                    }
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel title="来源时间线" meta={`${source} · ${days} 日`}>
        <div className="flex flex-wrap items-center gap-3 border-b border-line-soft px-4 py-3">
          <select
            value={source}
            onChange={(event) => setSource(event.target.value)}
            className={`${FIELD_CLASS} w-[180px]`}
            aria-label="来源"
          >
            {Object.entries(SOURCE_LABEL).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
          <span className="text-[12px] text-fg-dim">
            {timeline ? `${timeline.window_start ?? '—'} ~ ${timeline.window_end ?? '—'}` : ''}
          </span>
        </div>
        {timelineError ? (
          <LoadState error={timelineError} className="px-4 py-8" />
        ) : !timeline ? (
          <LoadState loading className="px-4 py-8" />
        ) : (
          <div className="flex flex-wrap gap-1 px-4 py-3">
            {timeline.points.map((point) => (
              <span
                key={point.trade_date ?? 'none'}
                title={`${point.trade_date ?? '—'} · ${
                  STATUS_LABEL[point.status] ?? point.status
                }`}
                className={`h-4 w-4 rounded-sm border ${
                  TIMELINE_TONE[point.status] ?? 'bg-ink-800 border-line'
                }`}
              />
            ))}
          </div>
        )}
        <div className="flex flex-wrap gap-3 px-4 pb-3 text-[12px] text-fg-dim">
          <Link className="text-accent" to="/sentiment">
            情绪周期
          </Link>
          <Link className="text-accent" to="/sectors">
            板块题材
          </Link>
          <Link className="text-accent" to="/auction">
            竞价强度
          </Link>
          <Link className="text-accent" to="/limit-up">
            涨停复盘
          </Link>
        </div>
      </Panel>
    </div>
  )
}

function ItemRow({
  item,
  expanded,
  onToggle,
}: {
  item: QualitySourceItem
  expanded: boolean
  onToggle: () => void
}) {
  const link = SOURCE_LINK[item.source]
  return (
    <>
      <tr className="cursor-pointer" onClick={onToggle}>
        <td className="!text-left">
          <div className="text-fg">{SOURCE_LABEL[item.source] ?? item.source}</div>
          <div className="num text-[11px] text-fg-dim">{item.source}</div>
        </td>
        <td>
          <StatusBadge status={item.status} />
        </td>
        <td className="num">
          {item.observed_rows}
          {item.expected_rows != null ? ` / ${item.expected_rows}` : ''}
        </td>
        <td className="num">
          {item.observed_sectors != null
            ? `${item.observed_sectors} / ${item.expected_sectors ?? '—'}`
            : '—'}
        </td>
        <td className="text-[11px]">{item.completion_proof ? '是' : '否'}</td>
        <td className="num">{item.latest_observation_date ?? '—'}</td>
        <td className="!text-left text-[11px] text-fg-muted">
          {item.reason_codes.join(' · ') || '—'}
          {link ? (
            <Link className="ml-2 text-accent" to={link.to} onClick={(event) => event.stopPropagation()}>
              {link.label}
            </Link>
          ) : null}
        </td>
      </tr>
      {expanded ? (
        <tr>
          <td colSpan={7} className="p-0">
            <Detail item={item} />
          </td>
        </tr>
      ) : null}
    </>
  )
}
