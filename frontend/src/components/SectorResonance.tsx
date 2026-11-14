/**
 * 板块主线共振（S4-C）—— 盘后只读研究工作台。
 *
 * 回答「某日已观测板块里，首板 / 二板 / 高位 / 炸板分别有哪些票，它们 D+1
 * 客观表现如何」。不是预测器，不自动选股，不产生任何 C0 信号。
 *
 * 页面必须一直带着覆盖与 PIT 提示：成员快照没有完成证明，AND 缺板块不能当成
 * 空交集，D+1 未到齐显示 PENDING 而不是 0%。
 */

import { useRef, useState } from 'react'
import { api } from '../api/client'
import type { ResonanceStats, SectorQuote, SectorResonance } from '../api/types'
import LoadState from './LoadState'
import Panel from './Panel'
import QuickResearch from './QuickResearch'
import Segmented from './Segmented'
import StockLink from './StockLink'
import { fmtInt, fmtPct, toneOf } from '../lib/format'

const MODE_ITEMS = [
  { key: 'AND' as const, label: 'AND 交集' },
  { key: 'OR' as const, label: 'OR 并集' },
]

const MARKET_ITEMS = [
  { key: 'all' as const, label: '全部' },
  { key: 'sh' as const, label: '沪' },
  { key: 'sz' as const, label: '深' },
  { key: 'bj' as const, label: '北' },
]

const STATE_ORDER = [
  ['first_board', '首板'],
  ['second_board', '二板'],
  ['third_plus', '三板及以上'],
  ['broken', '炸板'],
  ['conflict', '涨停/炸板冲突'],
  ['unknown', '连板高度未知'],
] as const

const STATUS_LABEL: Record<string, string> = {
  VERIFIED: '可验证',
  PARTIAL: '部分可信',
  UNVERIFIABLE: '不可验证',
  NOT_COLLECTED: '未采集',
  PENDING: '待观察',
}

const WARNING_LABEL: Record<string, string> = {
  NO_MEMBERS_COMPLETION_LOG: '成员快照无完成证明，比率仅限已观测样本',
  OBSERVED_ONLY: '仅已观测交集/并集，非市场全量',
  SELECTED_SECTOR_NOT_COVERED: '部分所选板块没有成员快照',
  AND_UNVERIFIABLE: 'AND 缺覆盖，不能判定为空交集',
  TAXONOMY_MIXED: '所选板块跨口径',
  POOL_UP_UNVERIFIED: 'D 日涨停池不可信',
  POOL_BROKEN_UNVERIFIED: 'D 日炸板池不可信',
  POOL_CONFLICT: '部分股票同时在涨停与炸板池',
  MARKET_FILTER_APPLIED: '已按市场过滤',
}

const KNOWN_BY_LABEL: Record<string, string> = {
  PRIOR_DAY: '观察日之前已取到',
  SAME_DAY_UNKNOWN: '同日取到，无法证明盘前',
  LATER: '观察日之后才取到',
  UNKNOWN: '来源时间未知',
}

function StatsCards({ stats }: { stats: ResonanceStats }) {
  return (
    <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
      <Card label="样本" value={`${fmtInt(stats.sample_count)}`} hint={`有效 ${stats.valid_count} · 缺 ${stats.missing_count}`} />
      <Card label="平均涨跌幅" value={fmtPct(stats.avg_pct)} hint={`中位 ${fmtPct(stats.median_pct)}`} />
      <Card
        label="上涨比例"
        value={stats.up_ratio == null ? '—' : `${stats.up_ratio.toFixed(1)}%`}
        hint={`涨 ${stats.outcome_up_count} · 跌 ${stats.outcome_down_count} · 平 ${stats.outcome_flat_count}`}
      />
      <Card
        label="次日再涨停"
        value={stats.limit_up_again_ratio == null ? '—' : `${stats.limit_up_again_ratio.toFixed(1)}%`}
        hint={
          stats.limit_up_again_count == null
            ? 'D+1 涨停池不可信'
            : `${stats.limit_up_again_count} / ${stats.valid_count}`
        }
      />
    </div>
  )
}

function Card({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="border border-line-soft px-3 py-2">
      <div className="text-[12px] text-fg-dim">{label}</div>
      <div className="num text-[18px] text-fg">{value}</div>
      {hint ? <div className="num text-[11px] text-fg-dim">{hint}</div> : null}
    </div>
  )
}

export default function SectorResonance({
  boards,
  pageDate,
}: {
  boards: SectorQuote[]
  pageDate: string | null
}) {
  const [codes, setCodes] = useState<string[]>([])
  const [mode, setMode] = useState<'AND' | 'OR'>('AND')
  const [market, setMarket] = useState<'all' | 'sh' | 'sz' | 'bj'>('all')
  const [date, setDate] = useState(pageDate ?? '')
  const [offset, setOffset] = useState(0)
  const [data, setData] = useState<SectorResonance | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const seq = useRef(0)

  const boardName = new Map(boards.map((board) => [board.code, board.name]))

  function addCode(code: string) {
    if (!code || codes.includes(code) || codes.length >= 10) return
    setCodes((current) => [...current, code])
    setData(null)
  }

  function removeCode(code: string) {
    setCodes((current) => current.filter((item) => item !== code))
    setData(null)
  }

  async function run(nextOffset = 0) {
    if (codes.length === 0) return
    const ticket = seq.current + 1
    seq.current = ticket
    setLoading(true)
    setError(null)
    try {
      const payload = await api.sectorResonance({
        sectorCodes: codes,
        mode,
        market,
        date: date || null,
        limit: 100,
        offset: nextOffset,
      })
      // 快速切换板块/日期时，旧响应不能覆盖新结果
      if (ticket !== seq.current) return
      setData(payload)
      setOffset(nextOffset)
    } catch (err) {
      if (ticket !== seq.current) return
      setData(null)
      setError((err as Error).message || '接口出错')
    } finally {
      if (ticket === seq.current) setLoading(false)
    }
  }

  return (
    <Panel
      title="板块主线共振"
      meta={
        <span className="text-[12px] font-normal text-fg-dim">
          盘后只读研究 · 成员 × 连板梯队 × D+1
        </span>
      }
      delay={220}
    >
      <div className="flex flex-wrap items-end gap-3 border-b border-line-soft px-4 py-3 text-[13px]">
        <label className="grid gap-1">
          <span className="text-fg-dim">添加板块</span>
          <select
            value=""
            onChange={(event) => addCode(event.target.value)}
            className="num border border-line bg-ink-900 px-2 py-[3px] text-[13px] text-fg outline-none focus:border-fg-dim"
            aria-label="添加板块"
          >
            <option value="">选择板块…</option>
            {boards
              .filter((board) => !codes.includes(board.code))
              .map((board) => (
                <option key={board.code} value={board.code}>
                  {board.name}（{board.code}）
                </option>
              ))}
          </select>
        </label>
        <label className="grid gap-1">
          <span className="text-fg-dim">观察日</span>
          <input
            type="date"
            value={date}
            onChange={(event) => {
              setDate(event.target.value)
              setData(null)
            }}
            className="num border border-line bg-ink-900 px-2 py-[3px] text-[13px] text-fg outline-none focus:border-fg-dim"
          />
        </label>
        <Segmented value={mode} items={[...MODE_ITEMS]} onChange={(next) => { setMode(next); setData(null) }} />
        <Segmented value={market} items={[...MARKET_ITEMS]} onChange={(next) => { setMarket(next); setData(null) }} />
        <button
          type="button"
          disabled={codes.length === 0}
          onClick={() => run(0)}
          className="border border-accent px-3 py-[3px] text-[13px] text-accent disabled:border-line disabled:text-fg-dim"
        >
          {loading ? '分析中…' : '分析'}
        </button>
        <span className="text-[12px] text-fg-dim">已选 {codes.length}/10</span>
      </div>

      <div className="flex flex-wrap gap-1 border-b border-line-soft px-4 py-2">
        {codes.length === 0 ? (
          <span className="text-[12px] text-fg-dim">先选 1~10 个板块，再点「分析」。</span>
        ) : (
          codes.map((code) => (
            <button
              key={code}
              type="button"
              onClick={() => removeCode(code)}
              className="border border-line px-2 py-[2px] text-[12px] text-fg-muted hover:text-fg"
              title="移除"
            >
              {boardName.get(code) ?? code} ×
            </button>
          ))
        )}
      </div>

      {error ? (
        <LoadState error={error} className="px-4 py-8" />
      ) : !data ? (
        <LoadState emptyText="选择板块后点「分析」查看共振结果" className="px-4 py-8" />
      ) : (
        <div className="space-y-3 px-4 py-3">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px]">
            <span
              className={
                data.quality.status === 'VERIFIED'
                  ? 'text-up'
                  : data.quality.status === 'PARTIAL'
                    ? 'text-warn'
                    : 'text-danger'
              }
            >
              {STATUS_LABEL[data.quality.status] ?? data.quality.status}
            </span>
            <span className="num text-fg-muted">
              观察日 {data.as_of} · 成员快照 {data.member_snapshot_date ?? '—'} · D+1{' '}
              {data.outcome_date ?? '—'}
            </span>
            <span className="text-fg-dim">
              快照时点：{KNOWN_BY_LABEL[data.pit.member_snapshot_known_by ?? 'UNKNOWN'] ?? '—'}
            </span>
            <span className="text-fg-dim">历史信号资格：否（只读展示）</span>
          </div>

          <div className="flex flex-wrap gap-x-3 gap-y-1 text-[12px] text-warn">
            {data.warnings.map((code) => (
              <span key={code}>· {WARNING_LABEL[code] ?? code}</span>
            ))}
          </div>

          <div className="grid grid-cols-2 gap-2 text-[12px] lg:grid-cols-5">
            <Card
              label="已观测成员"
              value={`${fmtInt(data.coverage.member_unique_count)}`}
              hint={`行 ${fmtInt(data.coverage.member_observed_rows)} · 完成证明：无`}
            />
            <Card
              label="板块覆盖"
              value={`${data.coverage.observed_sectors}/${data.coverage.expected_sectors}`}
              hint="所选板块"
            />
            <Card
              label="已观测涨停"
              value={
                data.coverage.observed_limit_up_count == null
                  ? '—'
                  : fmtInt(data.coverage.observed_limit_up_count)
              }
              hint="不含冲突股"
            />
            <Card
              label="已观测涨停占比"
              value={
                data.coverage.observed_limit_up_ratio == null
                  ? '—'
                  : `${data.coverage.observed_limit_up_ratio.toFixed(1)}%`
              }
              hint="已观测样本内，不是全板块率"
            />
            <Card
              label="市场情绪"
              value={
                data.market_sentiment?.limit_up_count == null
                  ? '—'
                  : `涨停 ${data.market_sentiment.limit_up_count}`
              }
              hint={
                data.market_sentiment
                  ? `炸板率 ${data.market_sentiment.broken_rate ?? '—'}% · 最高 ${data.market_sentiment.max_consecutive ?? '—'} 板`
                  : '无情绪行'
              }
            />
          </div>

          <div className="flex flex-wrap gap-2">
            {STATE_ORDER.map(([key, label]) => (
              <div key={key} className="border border-line-soft px-3 py-1.5">
                <span className="text-[12px] text-fg-dim">{label}</span>
                <span className="num ml-2 text-[15px] text-fg">{data.state_counts[key] ?? 0}</span>
              </div>
            ))}
          </div>

          <div className="border border-line-soft">
            <div className="flex flex-wrap items-center gap-2 border-b border-line-soft px-3 py-2 text-[12px]">
              <span className="text-fg">D+1 描述统计</span>
              <span className="text-fg-dim">{STATUS_LABEL[data.outcome_status] ?? data.outcome_status}</span>
              {data.outcome_reason_codes.length ? (
                <span className="text-warn">{data.outcome_reason_codes.join(' · ')}</span>
              ) : null}
              <span className="ml-auto text-fg-dim">不是可交易收益，不反写 D 日依据</span>
            </div>
            <div className="p-3">
              <StatsCards stats={data.outcome} />
            </div>
            <div className="overflow-auto">
              <table className="grid-table min-w-full text-[12px]">
                <thead>
                  <tr>
                    <th>分组</th>
                    <th>样本</th>
                    <th>有效</th>
                    <th>平均</th>
                    <th>中位</th>
                    <th>上涨比例</th>
                    <th>再涨停</th>
                  </tr>
                </thead>
                <tbody>
                  {STATE_ORDER.map(([key, label]) => {
                    const block = data.by_state[key]
                    if (!block) return null
                    return (
                      <tr key={key}>
                        <td className="!text-left">{label}</td>
                        <td className="num">{block.sample_count}</td>
                        <td className="num">{block.valid_count}</td>
                        <td className={`num ${toneOf(block.avg_pct)}`}>{fmtPct(block.avg_pct)}</td>
                        <td className="num">{fmtPct(block.median_pct)}</td>
                        <td className="num">
                          {block.up_ratio == null ? '—' : `${block.up_ratio.toFixed(1)}%`}
                        </td>
                        <td className="num">
                          {block.limit_up_again_ratio == null
                            ? '—'
                            : `${block.limit_up_again_ratio.toFixed(1)}%`}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </div>

          <div className="overflow-auto">
            <table className="grid-table min-w-full text-[12px]" data-testid="resonance-table">
              <thead>
                <tr>
                  <th>代码 / 名称</th>
                  <th>状态</th>
                  <th>连板</th>
                  <th>首封</th>
                  <th>D 涨跌</th>
                  <th>D+1 涨跌</th>
                  <th>D+1 再涨停</th>
                  <th>所属所选板块</th>
                </tr>
              </thead>
              <tbody>
                {data.rows.map((row) => (
                  <tr key={row.code}>
                    <td className="!text-left">
                      <span className="inline-flex items-center gap-1">
                        <StockLink code={row.code}>{row.name ?? row.code}</StockLink>
                        <QuickResearch
                          code={row.code}
                          asOfDate={data.as_of}
                          tab="evidence"
                          label="据"
                        />
                      </span>
                    </td>
                    <td className="text-[11px]">{row.state_label}</td>
                    <td className="num">{row.consecutive ?? '—'}</td>
                    <td className="num">{row.first_seal_time ?? '—'}</td>
                    <td className={`num ${toneOf(row.pct_chg)}`}>{fmtPct(row.pct_chg)}</td>
                    <td className={`num ${toneOf(row.outcome_pct_chg)}`}>
                      {fmtPct(row.outcome_pct_chg)}
                    </td>
                    <td className="text-[11px]">
                      {row.limit_up_again == null ? '不可验证' : row.limit_up_again ? '是' : '否'}
                    </td>
                    <td className="!text-left text-[11px] text-fg-muted">
                      {row.sector_codes
                        .map((code) => boardName.get(code) ?? code)
                        .join(' · ')}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="flex flex-wrap items-center gap-2 text-[12px] text-fg-dim">
            <span className="num">
              共 {fmtInt(data.total)} 只 · 当前 {data.shown}
            </span>
            <button
              type="button"
              disabled={offset <= 0 || loading}
              onClick={() => run(Math.max(0, offset - 100))}
              className="border border-line px-2 py-[2px] disabled:text-fg-dim"
            >
              上一页
            </button>
            <button
              type="button"
              disabled={!data.has_more || loading}
              onClick={() => run(offset + 100)}
              className="border border-line px-2 py-[2px] disabled:text-fg-dim"
            >
              下一页
            </button>
            <span className="ml-auto">{data.notes.join(' ')}</span>
          </div>
        </div>
      )}
    </Panel>
  )
}
