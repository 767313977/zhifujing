/**
 * 盘后主线研究工作台（S5-C）—— **只读研究汇总，不生成交易信号**。
 *
 * 一页看清：市场情绪、连板晋级漏斗、已观测题材共振、D+1 描述统计、
 * 各数据源可用性与原因码、以及逐券明细。
 *
 * 硬边界（与后端 `services/post_market_workbench.py` 一致）：
 * - 不触发采集 / `syncStock`，不写库；
 * - 观察日必须是交易日；
 * - D+1 未成熟一律显示「待观察」，不写 0%；
 * - 题材成员没有完成证明，所有比率都标「已观测样本」；
 * - URL 保留 date / sector_codes / mode / market / offset，便于回看与分享。
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import type { ResonanceRow, ResonanceStats, Workbench, WorkbenchLevel } from '../api/types'
import LoadState from '../components/LoadState'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import QuickResearch from '../components/QuickResearch'
import StockLink from '../components/StockLink'
import { fmtNum, fmtPct } from '../lib/format'

const STATUS_LABEL: Record<string, string> = {
  VERIFIED: '可验证',
  VERIFIED_SOURCE: '来源可信',
  PARTIAL: '部分可信',
  PENDING: '待观察',
  UNVERIFIABLE: '不可验证',
  NOT_COLLECTED: '未采集',
  NOT_SELECTED: '未选板块',
  SOURCE_ERROR: '查询异常',
  NOT_TRADE_DAY: '非交易日',
}

const STATE_LABEL: Record<string, string> = {
  first_board: '首板',
  second_board: '二板',
  third_plus: '三板+',
  broken: '炸板',
  conflict: '冲突',
  unknown: '未知',
}

const STATUS_TONE: Record<string, string> = {
  VERIFIED: 'text-up',
  VERIFIED_SOURCE: 'text-up',
  PARTIAL: 'text-warn',
  PENDING: 'text-fg-dim',
  UNVERIFIABLE: 'text-danger',
  NOT_COLLECTED: 'text-fg-dim',
  NOT_SELECTED: 'text-fg-dim',
  SOURCE_ERROR: 'text-danger',
}

const SOURCE_LABEL: Record<string, string> = {
  stock_daily: '个股日线',
  limit_pool: '涨停 / 炸板池',
  sector_member: '板块成分快照',
  market_sentiment: '市场情绪',
  auction_snapshot: '集合竞价',
  trade_calendar: '交易日历',
}

const FIELD_CLASS =
  'border border-line bg-ink-900 px-2 py-[3px] text-[13px] text-fg outline-none focus:border-fg-dim'

function Status({ value }: { value: string }) {
  return <span className={STATUS_TONE[value] ?? 'text-fg-muted'}>{STATUS_LABEL[value] ?? value}</span>
}

function safeNumber(value: number | null | undefined): string {
  return value == null ? '—' : fmtNum(value)
}

/** 行内统计：一个 null 都不映射成 0 */
function StatsLine({
  stats,
  status,
}: {
  stats: Partial<ResonanceStats> | Record<string, unknown> | null
  status: string
}) {
  if (!stats || status === 'PENDING') {
    return <span className="text-fg-dim">待观察（D+1 未成熟，不显示 0%）</span>
  }
  const record = stats as Partial<ResonanceStats>
  return (
    <span className="num text-fg-muted">
      样本 {safeNumber(record.sample_count ?? null)} · 有效{' '}
      {safeNumber(record.valid_count ?? null)} · 均值{' '}
      {fmtPct(record.avg_pct ?? null)} · 中位 {fmtPct(record.median_pct ?? null)} · 上涨占比{' '}
      {fmtPct(record.up_ratio ?? null)} · 再涨停{' '}
      {fmtPct(record.limit_up_again_ratio ?? null)}
    </span>
  )
}

function LevelTable({ levels }: { levels: WorkbenchLevel[] }) {
  if (!levels.length) return <div className="px-4 py-6 text-[13px] text-fg-dim">没有晋级层数据</div>
  return (
    <div className="overflow-auto">
      <table className="grid-table min-w-full text-[12px]">
        <thead>
          <tr>
            <th>层级</th>
            <th>基数</th>
            <th>晋级</th>
            <th>炸板</th>
            <th>失败</th>
            <th>不可验证</th>
            <th>已验证晋级率</th>
            <th>旧口径</th>
          </tr>
        </thead>
        <tbody>
          {levels.map((row) => (
            <tr key={row.level}>
              <td className="!text-left">{row.label}</td>
              <td className="num">{row.base}</td>
              <td className="num">{row.promoted}</td>
              <td className="num">{row.broken}</td>
              <td className="num">{row.failed}</td>
              <td className="num">{row.unverifiable}</td>
              <td className="num">{fmtPct(row.verified_rate)}</td>
              <td className="num text-fg-dim">{fmtPct(row.legacy_rate)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function RowTable({ rows, asOf }: { rows: ResonanceRow[]; asOf: string }) {
  if (!rows.length) {
    return (
      <div className="px-4 py-6 text-[13px] text-fg-dim">
        当前筛选下没有逐券结果（可能是所选板块没有交集，或全部不可验证）。
      </div>
    )
  }
  return (
    <div className="overflow-auto">
      <table className="grid-table min-w-full text-[12px]" data-testid="workbench-rows">
        <thead>
          <tr>
            <th>代码 / 名称</th>
            <th>状态</th>
            <th>连板</th>
            <th>已观测题材</th>
            <th>D 涨跌幅</th>
            <th>D+1</th>
            <th>再涨停</th>
            <th>研究</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.code}>
              <td className="!text-left">
                <StockLink code={row.code} className="text-fg">
                  {row.name || row.code}
                </StockLink>
                <span className="num ml-1 text-[11px] text-fg-dim">{row.code}</span>
              </td>
              <td className="text-[11px]">{row.state_label || STATE_LABEL[row.state] || row.state}</td>
              <td className="num">{row.consecutive ?? '—'}</td>
              <td className="!text-left text-[11px] text-fg-muted">
                {row.sector_codes.length ? row.sector_codes.join('、') : '—'}
              </td>
              <td className="num">{fmtPct(row.pct_chg)}</td>
              <td className="num">{fmtPct(row.outcome_pct_chg)}</td>
              <td className="text-[11px]">
                {row.limit_up_again == null ? '不可验证' : row.limit_up_again ? '是' : '否'}
              </td>
              <td>
                <QuickResearch code={row.code} asOfDate={asOf} />
                <QuickResearch code={row.code} asOfDate={asOf} tab="evidence" label="据" />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function PostMarketWorkbench() {
  const [params, setParams] = useSearchParams()
  const date = params.get('date') ?? ''
  const sectorCodes = params.get('sector_codes') ?? ''
  const mode = params.get('mode') ?? 'AND'
  const market = params.get('market') ?? 'all'
  const view = params.get('view') ?? 'overview'
  const offset = Number(params.get('offset') ?? 0)

  const [sectorInput, setSectorInput] = useState(sectorCodes)
  const [data, setData] = useState<Workbench | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const requestSeq = useRef(0)

  useEffect(() => {
    setSectorInput(sectorCodes)
  }, [sectorCodes])

  useEffect(() => {
    const seq = ++requestSeq.current
    setLoading(true)
    setError(null)
    api
      .workbench({
        date: date || null,
        sectorCodes: sectorCodes || null,
        mode,
        market,
        view,
        limit: 50,
        offset,
      })
      .then((payload) => {
        // 快速切换日期/板块时，旧响应不得覆盖新状态
        if (seq === requestSeq.current) setData(payload)
      })
      .catch((err: Error) => {
        if (seq === requestSeq.current) {
          setData(null)
          setError(err.message || '接口出错')
        }
      })
      .finally(() => {
        if (seq === requestSeq.current) setLoading(false)
      })
  }, [date, sectorCodes, mode, market, view, offset])

  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    if (key !== 'offset') next.delete('offset')
    setParams(next, { replace: true })
  }

  const applySectors = () => {
    const cleaned = sectorInput
      .split(/[，,\s]+/)
      .map((item) => item.trim())
      .filter(Boolean)
      .slice(0, 10)
      .join(',')
    update('sector_codes', cleaned)
  }

  const qualityDate = data?.as_of ?? date
  const warnings = useMemo(() => data?.warnings ?? [], [data])
  const show = (key: string) => view === 'overview' || view === key

  return (
    <Layout>
      <div className="grid gap-4">
        <Panel
        title="盘后主线研究工作台"
        meta={data ? `观察日 ${data.as_of} · 数据截至 ${data.data_as_of ?? '—'} · ${data.quality_version}` : undefined}
      >
        <div className="flex flex-wrap items-end gap-3 border-b border-line-soft px-4 py-3 text-[13px]">
          <label className="grid gap-1">
            <span className="text-fg-dim">观察日</span>
            <input
              type="date"
              value={date}
              onChange={(event) => update('date', event.target.value)}
              className={`${FIELD_CLASS} num w-[150px]`}
            />
          </label>
          <label className="grid min-w-[240px] flex-1 gap-1">
            <span className="text-fg-dim">题材代码（逗号分隔，最多 10 个）</span>
            <div className="flex gap-1">
              <input
                value={sectorInput}
                onChange={(event) => setSectorInput(event.target.value)}
                onBlur={applySectors}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') applySectors()
                }}
                placeholder="例如 801001,801002"
                className={`${FIELD_CLASS} num w-full`}
              />
              <button
                type="button"
                onClick={applySectors}
                className="border border-line px-2 text-[12px] text-fg-muted hover:border-accent/60 hover:text-accent"
              >
                应用
              </button>
            </div>
          </label>
          <label className="grid gap-1">
            <span className="text-fg-dim">组合</span>
            <select
              value={mode}
              onChange={(event) => update('mode', event.target.value)}
              className={`${FIELD_CLASS} w-[90px]`}
            >
              <option value="AND">AND</option>
              <option value="OR">OR</option>
            </select>
          </label>
          <label className="grid gap-1">
            <span className="text-fg-dim">市场</span>
            <select
              value={market}
              onChange={(event) => update('market', event.target.value)}
              className={`${FIELD_CLASS} w-[100px]`}
            >
              <option value="all">全部</option>
              <option value="sh">沪</option>
              <option value="sz">深</option>
              <option value="bj">北</option>
            </select>
          </label>
          <label className="grid gap-1">
            <span className="text-fg-dim">视图</span>
            <select
              value={view}
              onChange={(event) => update('view', event.target.value)}
              className={`${FIELD_CLASS} w-[110px]`}
            >
              <option value="overview">综合</option>
              <option value="ladder">梯队</option>
              <option value="outcome">次日结果</option>
              <option value="quality">数据质量</option>
            </select>
          </label>
        </div>
        <div className="border-b border-line-soft bg-ink-850 px-4 py-2 text-[12px] leading-relaxed text-fg-muted">
          历史可知 / 事后描述的边界：题材成员快照没有完成证明，本页所有比率都是「已观测样本内」；
          D+1 表现只作复盘，不反写 D 日依据，也不构成买卖信号。
        </div>
        {warnings.length ? (
          <div className="border-b border-line-soft px-4 py-2 text-[11px] text-warn">
            {warnings.join(' · ')}
          </div>
        ) : null}

        {loading && !data ? (
          <LoadState loading className="px-4 py-10" />
        ) : error ? (
          <LoadState error={error} className="px-4 py-10" />
        ) : !data ? (
          <LoadState emptyText="暂无研究工作台数据" className="px-4 py-10" />
        ) : (
          <div className="grid gap-3 px-4 py-3 text-[12px] lg:grid-cols-3">
            <div>
              <div className="text-fg-dim">市场情绪 · {data.sentiment.as_of}</div>
              <div className="mt-1">
                <Status value={data.sentiment.status} />
              </div>
              <div className="num mt-1 text-fg-muted">
                涨停 {safeNumber(data.sentiment.summary?.limit_up_count ?? null)} · 炸板率{' '}
                {fmtPct(data.sentiment.summary?.broken_rate ?? null)} · 最高{' '}
                {safeNumber(data.sentiment.summary?.max_consecutive ?? null)} 板
              </div>
            </div>
            <div>
              <div className="text-fg-dim">晋级漏斗 · 基于 {data.promotion.prev_date ?? '—'}</div>
              <div className="mt-1">
                <Status value={data.promotion.status} />
              </div>
              <div className="mt-1 text-fg-muted">{data.promotion.reasons.join(' · ') || '—'}</div>
            </div>
            <div>
              <div className="text-fg-dim">题材共振</div>
              <div className="mt-1">
                <Status value={data.resonance.status} />
              </div>
              <div className="num mt-1 text-fg-muted">
                已观测 {data.resonance.coverage?.member_unique_count ?? 0} 只 · 快照日{' '}
                {data.resonance.member_snapshot_date ?? '—'} · 完成证明{' '}
                {data.resonance.coverage?.member_completion_proof ? '是' : '否'}
              </div>
            </div>
          </div>
        )}
      </Panel>

      {data && show('ladder') ? (
        <Panel title="连板晋级漏斗" meta={data.promotion.prev_date ? `昨日 ${data.promotion.prev_date}` : undefined}>
          <LevelTable levels={data.promotion.levels} />
        </Panel>
      ) : null}

      {data && show('overview') ? (
        <Panel title="已观测题材共振" meta={`${data.scope.mode} · ${data.scope.market}`}>
          <div className="flex flex-wrap gap-3 px-4 py-2 text-[12px]">
            {Object.entries(data.resonance.state_counts).map(([state, count]) => (
              <span key={state} className="num text-fg-muted">
                {STATE_LABEL[state] ?? state} {count}
              </span>
            ))}
            <span className="num text-fg-dim">
              已观测涨停占比 {fmtPct(data.resonance.coverage?.observed_limit_up_ratio ?? null)}
            </span>
          </div>
        </Panel>
      ) : null}

      {data && show('outcome') ? (
        <Panel title="D+1 事后描述统计" meta={data.outcome.outcome_date ?? '未到'}>
          <div className="px-4 py-3">
            <Status value={data.outcome.status} />
            <div className="mt-2">
              <StatsLine stats={data.outcome.stats} status={data.outcome.status} />
            </div>
            {data.outcome.reasons.length ? (
              <div className="mt-1 text-[11px] text-warn">{data.outcome.reasons.join(' · ')}</div>
            ) : null}
          </div>
          <div className="overflow-auto border-t border-line-soft">
            <table className="grid-table min-w-full text-[12px]">
              <thead>
                <tr>
                  <th>分桶</th>
                  <th>样本</th>
                  <th>有效</th>
                  <th>均值</th>
                  <th>上涨占比</th>
                  <th>再涨停比例</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(data.outcome.by_state).map(([state, stats]) => (
                  <tr key={state}>
                    <td className="!text-left">{stats.label ?? STATE_LABEL[state] ?? state}</td>
                    <td className="num">{stats.sample_count}</td>
                    <td className="num">{stats.valid_count}</td>
                    <td className="num">{fmtPct(stats.avg_pct)}</td>
                    <td className="num">{fmtPct(stats.up_ratio)}</td>
                    <td className="num">{fmtPct(stats.limit_up_again_ratio)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      ) : null}

      {data && show('quality') ? (
        <Panel title="数据源可用性" meta={data.quality.version}>
          <div className="overflow-auto">
            <table className="grid-table min-w-full text-[12px]">
              <thead>
                <tr>
                  <th>来源</th>
                  <th>状态</th>
                  <th>组合视图</th>
                  <th>完成证明</th>
                  <th>原因码</th>
                  <th>诊断</th>
                </tr>
              </thead>
              <tbody>
                {data.quality.sources.map((item) => (
                  <tr key={item.source}>
                    <td className="!text-left">{SOURCE_LABEL[item.source] ?? item.source}</td>
                    <td>
                      <Status value={item.status} />
                    </td>
                    <td>
                      <Status value={item.combined_view_status} />
                    </td>
                    <td className="text-[11px]">{item.completion_proof ? '是' : '否'}</td>
                    <td className="!text-left text-[11px] text-fg-muted">
                      {item.reason_codes.join(' · ') || '—'}
                    </td>
                    <td>
                      <Link
                        className="text-accent"
                        to={`/data-quality?date=${encodeURIComponent(qualityDate ?? '')}`}
                      >
                        质量台
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      ) : null}

      {data ? (
        <Panel
          title="逐券明细"
          meta={`共 ${data.pagination.total} 只 · 当前 ${data.pagination.shown} 只${
            data.scope.sector_codes.length ? ` · ${data.scope.sector_codes.join('+')}` : ' · 未选板块'
          }`}
        >
          <RowTable rows={data.rows} asOf={data.as_of} />
          <div className="flex items-center gap-2 border-t border-line-soft px-4 py-2 text-[12px]">
            <button
              type="button"
              disabled={offset <= 0}
              onClick={() => update('offset', String(Math.max(0, offset - 50)))}
              className="border border-line px-2 py-1 disabled:opacity-40"
            >
              上一页
            </button>
            <button
              type="button"
              disabled={!data.pagination.has_more}
              onClick={() => update('offset', String(offset + 50))}
              className="border border-line px-2 py-1 disabled:opacity-40"
            >
              下一页
            </button>
            <span className="num text-fg-dim">offset {offset}</span>
          </div>
          <div className="px-4 pb-3 text-[11px] leading-relaxed text-fg-dim">
            {data.notes.map((note) => (
              <div key={note}>· {note}</div>
            ))}
          </div>
        </Panel>
      ) : null}
      </div>
    </Layout>
  )
}
