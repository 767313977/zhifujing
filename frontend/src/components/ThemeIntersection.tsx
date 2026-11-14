/**
 * 题材 AND / OR 交叉检索（S2-C4）。
 *
 * 数据来自本地 `sector_member` 快照，**不是实时全市场成员**。因此页面必须把
 * 「所选板块当天有没有快照」「全市场覆盖了多少板块」「有没有完成证据」写清楚：
 * - AND 缺覆盖 → `UNVERIFIABLE`，不显示成「空交集」；
 * - OR 缺覆盖 → `PARTIAL`，结果保留可用部分；
 * - 没有成员级完成日志时始终带提示，不把快照覆盖当成市场事实。
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api/client'
import type { SectorQuote, ThemeSearch } from '../api/types'
import LoadState from './LoadState'
import Panel from './Panel'
import QuickResearch from './QuickResearch'
import Segmented from './Segmented'
import StockLink from './StockLink'
import { fmtInt } from '../lib/format'

const LIMIT = 50
const MAX_SECTORS = 10

const STATUS_LABEL: Record<string, string> = {
  VERIFIED: '可验证',
  PARTIAL: '部分覆盖',
  NOT_COLLECTED: '未采集',
  UNVERIFIABLE: '不可验证',
}

const WARNING_LABEL: Record<string, string> = {
  NO_MEMBERS_COMPLETION_LOG: '无成员级完成日志',
  SNAPSHOT_SECTOR_COVERAGE_LOW: '快照覆盖不全',
  SELECTED_SECTOR_NOT_COVERED: '所选板块当天无成员快照',
  AND_UNVERIFIABLE: 'AND 缺覆盖，不能判定为空交集',
  TAXONOMY_MIXED: '所选板块跨口径',
  MARKET_FILTER_APPLIED: '已按市场过滤',
  STOCK_CONCEPT_NOT_USED: '涨停天梯不参与全市场交集',
  OBSERVED_INTERSECTION_ONLY: '仅已观测交集，非全量',
  THEME_PIT_UNKNOWN: '无法证明盘前已知',
}

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

function statusTone(status: string): string {
  if (status === 'VERIFIED') return 'text-up'
  if (status === 'PARTIAL') return 'text-warn'
  if (status === 'NOT_COLLECTED') return 'text-fg-muted'
  return 'text-danger'
}

export default function ThemeIntersection({
  boards,
  pageDate,
}: {
  /** 当前页面的板块清单（来自排行接口，不含额外请求） */
  boards: SectorQuote[]
  /** 页面当前交易日；作为快照日默认值 */
  pageDate: string | null
}) {
  const [codes, setCodes] = useState<string[]>([])
  const [query, setQuery] = useState('')
  const [mode, setMode] = useState<'AND' | 'OR'>('AND')
  const [market, setMarket] = useState<'all' | 'sh' | 'sz' | 'bj'>('all')
  const [asOf, setAsOf] = useState<string>(pageDate ?? '')
  const [offset, setOffset] = useState(0)
  const [data, setData] = useState<ThemeSearch | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [hint, setHint] = useState<string | null>(null)
  const seq = useRef(0)

  // 页面换交易日时，快照日跟着回到页面日期（用户之后仍可单独改）
  useEffect(() => {
    setAsOf(pageDate ?? '')
    setOffset(0)
  }, [pageDate])

  const codeKey = codes.join(',')
  const codeList = useMemo(() => (codeKey ? codeKey.split(',') : []), [codeKey])

  useEffect(() => {
    if (codeList.length === 0) {
      setData(null)
      setError(null)
      setLoading(false)
      return
    }
    const current = ++seq.current
    setLoading(true)
    setError(null)
    api
      .themeSearch({
        sectorCodes: codeList,
        mode,
        market,
        asOf: asOf || null,
        limit: LIMIT,
        offset,
      })
      .then((value) => {
        // 快速切板块 / 切页时，旧响应不能覆盖新结果
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
  }, [codeList, mode, market, asOf, offset])

  const selected = useMemo(
    () => codes.map((code) => boards.find((item) => item.code === code)).filter(Boolean) as SectorQuote[],
    [boards, codes],
  )

  const suggestions = useMemo(() => {
    const keyword = query.trim()
    return boards
      .filter((item) => !codes.includes(item.code))
      .filter((item) => !keyword || item.name.includes(keyword) || item.code.includes(keyword))
      .slice(0, 12)
  }, [boards, codes, query])

  // 换 taxonomy / 日期后板块清单会变：已选但不在清单里的代码自动丢弃
  useEffect(() => {
    if (boards.length === 0) return
    const available = new Set(boards.map((item) => item.code))
    setCodes((prev) => {
      const next = prev.filter((code) => available.has(code))
      return next.length === prev.length ? prev : next
    })
  }, [boards])

  const toggle = (code: string) => {
    setOffset(0)
    setHint(null)
    if (!codes.includes(code) && codes.length >= MAX_SECTORS) {
      setHint(`最多同时选择 ${MAX_SECTORS} 个板块`)
      return
    }
    setCodes((prev) =>
      prev.includes(code) ? prev.filter((item) => item !== code) : [...prev, code],
    )
  }

  const changeMode = (value: 'AND' | 'OR') => {
    setMode(value)
    setOffset(0)
  }

  const changeMarket = (value: 'all' | 'sh' | 'sz' | 'bj') => {
    setMarket(value)
    setOffset(0)
  }

  const coverage = data?.coverage
  const meta = data?.resolved_as_of
    ? `快照 ${data.resolved_as_of}${data.is_exact_date ? '' : '（回落到最近可核验日）'} · ${data.quality_version}`
    : '只读本地成员快照 · 非实时'

  const pitText =
    data?.known_by_observation_time === true
      ? '可证明观察日前已知'
      : data?.known_by_observation_time === false
        ? '快照晚于观察日，仅可复盘'
        : '无法证明盘前已知'
  const fetchedText = data?.fetched_at ? data.fetched_at.slice(0, 16).replace('T', ' ') : '—'

  return (
    <Panel title="题材交叉" meta={<span className="num">{meta}</span>} delay={220}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-line-soft px-3 py-2 text-[12px]">
        <Segmented value={mode} items={MODE_ITEMS} onChange={changeMode} />
        <Segmented value={market} items={MARKET_ITEMS} onChange={changeMarket} />
        <label className="flex items-center gap-1 text-fg-dim">
          快照日
          <input
            type="date"
            value={asOf}
            onChange={(event) => {
              setAsOf(event.target.value)
              setOffset(0)
            }}
            className="border border-line bg-transparent px-1 py-[1px] text-[12px] text-fg"
          />
        </label>
        <span className="text-fg-muted">最多 {MAX_SECTORS} 个板块 · 成员来自快照，不是实时全市场</span>
        {hint ? <span className="text-warn">{hint}</span> : null}
      </div>

      <div className="border-b border-line-soft px-3 py-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索题材名称 / 代码"
            className="w-[180px] border border-line bg-transparent px-2 py-[3px] text-[13px] text-fg"
            aria-label="搜索题材"
          />
          {suggestions.map((item) => (
            <button
              key={item.code}
              type="button"
              onClick={() => toggle(item.code)}
              className="border border-line px-1.5 py-[2px] text-[12px] text-fg-dim transition-colors hover:border-accent/60 hover:text-accent"
            >
              {item.name}
            </button>
          ))}
          {boards.length === 0 ? (
            <span className="text-[12px] text-fg-muted">板块清单还没加载出来</span>
          ) : suggestions.length === 0 ? (
            <span className="text-[12px] text-fg-muted">没有匹配的题材</span>
          ) : null}
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {selected.length === 0 ? (
            <span className="text-[12px] text-fg-muted">先选 1~{MAX_SECTORS} 个题材，再按 AND / OR 查询</span>
          ) : (
            selected.map((item) => (
              <span
                key={item.code}
                className="flex items-center gap-1 border border-accent/40 px-1.5 py-[2px] text-[12px] text-accent"
              >
                {item.name}
                <button
                  type="button"
                  aria-label={`移除 ${item.name}`}
                  onClick={() => toggle(item.code)}
                  className="text-fg-muted hover:text-danger"
                >
                  ✕
                </button>
              </span>
            ))
          )}
        </div>
      </div>

      {loading ? (
        <LoadState loading className="px-4 py-8" />
      ) : error ? (
        <LoadState error={error} className="px-4 py-8" />
      ) : !data ? (
        <div className="px-4 py-8 text-center text-[13px] text-fg-dim">
          选择题材后，这里会显示同一快照日的成分股交叉结果
        </div>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-line-soft px-3 py-2 text-[12px]">
            <span className={`num ${statusTone(data.quality.status)}`}>
              {STATUS_LABEL[data.quality.status] ?? data.quality.status}
            </span>
            <span className="text-fg-dim">已观测命中</span>
            <span className="num">{fmtInt(data.total)} 只</span>
            {coverage ? (
              <>
                <span className="text-fg-dim">所选板块快照覆盖</span>
                <span className="num">
                  {fmtInt(coverage.selected_sector_observed)}/{fmtInt(coverage.selected_sector_count)}
                </span>
                <span className="text-fg-dim">全市场基础板块覆盖</span>
                <span className="num">
                  {fmtInt(coverage.observed_sector_count)}/{fmtInt(coverage.expected_sector_count)}
                </span>
                <span className="text-fg-dim">
                  样本 {fmtInt(coverage.sample_rows)} 行 / {fmtInt(coverage.distinct_stocks)} 只
                </span>
              </>
            ) : null}
            <span className="text-fg-dim">
              口径 {data.taxonomy === 'mixed' ? '混合（不同 taxonomy）' : data.taxonomy}
            </span>
            <span className="text-fg-dim">取数 {fetchedText}</span>
            {data.warnings.map((code) => (
              <span key={code} className="text-warn" title={code}>
                {WARNING_LABEL[code] ?? code}
              </span>
            ))}
          </div>

          <div className="border-b border-line-soft px-3 py-1.5 text-[12px] leading-relaxed text-fg-muted">
            <span className="text-warn">{pitText}</span>
            {' · 历史信号资格：否（本轮只读展示）'}
            {data.snapshot_age_days != null ? ` · 快照滞后 ${data.snapshot_age_days} 天` : ''}
            {coverage?.note ? ` · ${coverage.note}` : ''}
          </div>

          {data.quality.status === 'NOT_COLLECTED' ? (
            <div className="px-4 py-8 text-center text-[13px] text-fg-dim">
              所选板块在该日期没有成员快照，不显示「空交集」。
            </div>
          ) : data.quality.status === 'UNVERIFIABLE' ? (
            <div className="px-4 py-8 text-center text-[13px] text-warn">
              缺覆盖：{data.selected_sectors.filter((item) => !item.has_data).map((item) => item.name).join('、')}
              ，无法判定是否有交集（不是 0 只）。
            </div>
          ) : data.items.length === 0 ? (
            <div className="px-4 py-8 text-center text-[13px] text-fg-dim">
              该快照日没有符合 {data.mode} 条件的证券
            </div>
          ) : (
            <>
              <div className="overflow-auto">
                <table className="grid-table min-w-full text-[13px]">
                  <thead>
                    <tr>
                      <th>代码</th>
                      <th>名称</th>
                      <th>命中题材</th>
                      <th title="命中的所选板块数">命中数</th>
                      <th title="当日涨停连板；没涨停为 —">当日涨停</th>
                      <th>研究</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.map((row) => (
                      <tr key={row.code}>
                        <td className="num">{row.code}</td>
                        <td>
                          <StockLink code={row.code}>{row.name ?? row.code}</StockLink>
                        </td>
                        <td className="text-fg-muted">
                          {row.sectors.map((item) => item.name).join('、')}
                        </td>
                        <td className="num">{row.match_count}</td>
                        <td className="num">
                          {row.limit_up_consecutive ? `${row.limit_up_consecutive} 板` : '—'}
                        </td>
                        <td>
                          <QuickResearch code={row.code} asOfDate={data.resolved_as_of} />
                          <QuickResearch
                            code={row.code}
                            asOfDate={data.resolved_as_of}
                            tab="evidence"
                            label="据"
                          />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line-soft px-3 py-2 text-[12px] text-fg-dim">
                <span className="num">
                  第 {data.total === 0 ? 0 : data.offset + 1}–{data.offset + data.shown} 条 / 共{' '}
                  {fmtInt(data.total)} 条
                </span>
                <div className="flex gap-1">
                  <button
                    type="button"
                    disabled={data.offset === 0}
                    onClick={() => setOffset(Math.max(0, data.offset - LIMIT))}
                    className="border border-line px-2 py-[2px] disabled:opacity-40"
                  >
                    上一页
                  </button>
                  <button
                    type="button"
                    disabled={!data.has_more}
                    onClick={() => setOffset(data.offset + LIMIT)}
                    className="border border-line px-2 py-[2px] disabled:opacity-40"
                  >
                    下一页
                  </button>
                </div>
              </div>
            </>
          )}
        </>
      )}
    </Panel>
  )
}
