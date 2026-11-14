import { useEffect, useMemo, useRef, useState } from 'react'
import { api, isNoDataError } from '../api/client'
import type {
  AuctionAnomaly as AuctionAnomalyData,
  AuctionAnomalyGroupKey,
  AuctionAnomalySort,
} from '../api/types'
import LoadState from './LoadState'
import Panel from './Panel'
import QuickResearch from './QuickResearch'
import SortTh from './SortTh'
import StockLink from './StockLink'
import { fmtAmount, fmtInt, fmtNum, fmtPct, toneOf } from '../lib/format'

const GROUP_ORDER: AuctionAnomalyGroupKey[] = [
  'prev_limit',
  'prev_broken',
  'prev_failed_relay',
  'all',
]

const GROUP_LABEL: Record<AuctionAnomalyGroupKey, string> = {
  prev_limit: '昨日涨停',
  prev_broken: '昨日炸板',
  prev_failed_relay: '昨日断板',
  all: '全部',
}

const QUALITY_LABEL: Record<string, string> = {
  VALID: '完整可信',
  INCOMPLETE: '部分可信',
  SOURCE_UNAVAILABLE: '源不可用',
  NOT_COLLECTED: '未采集',
  PENDING: '待观察',
  NO_QUOTE: '无报价',
  FRAME_MISSING: '缺帧',
  STALE_QUOTE: '旧报价',
  SOURCE_TIME_UNKNOWN: '来源时间未知',
}

const REASON_LABEL: Record<string, string> = {
  NO_AUCTION_SNAPSHOT: '当日没有竞价快照',
  NO_AUCTION_ROW: '该股当日没有竞价记录',
  NO_AUCTION_QUOTE: '没有竞价成交价',
  NO_0926_QUOTE: '缺少 0926 竞价终值',
  FRAME_MISSING_0926: '缺少 0926 帧',
  FRAME_MISSING_0931: '缺少 0931 帧',
  FRAME_LOG_MISSING_0926: '缺少 0926 帧采集证据',
  FRAME_LOG_MISSING_0931: '缺少 0931 帧采集证据',
  FRAME_0926_NOT_OK: '0926 帧采集状态异常',
  FRAME_0931_NOT_OK: '0931 帧采集状态异常',
  FRAME_0926_PARTIAL: '0926 帧部分可信',
  FRAME_0931_PARTIAL: '0931 帧部分可信',
  FRAME_0926_LAST_ATTEMPT_FAILED: '0926 最近一次尝试失败（此前有效数据仍在）',
  FRAME_0931_LAST_ATTEMPT_FAILED: '0931 最近一次尝试失败（此前有效数据仍在）',
  FRAME_0926_FILTERED: '0926 帧有被过滤报价',
  FRAME_0931_FILTERED: '0931 帧有被过滤报价',
  FRAME_0926_WRITE_GAP: '0926 帧有效报价未全部写入',
  FRAME_0931_WRITE_GAP: '0931 帧有效报价未全部写入',
  POST_OPEN_MISSING: '缺少 0931 开盘后价格',
  POST_OPEN_PENDING: '9:31 尚未到达，待观察',
  NO_POST_OPEN_QUOTE: '没有 0931 开盘后报价',
  COVERAGE_GAP: '存在未解释的覆盖缺口',
  STALE_SOURCE_QUOTE: '源报价时间不在计划区间',
  SOURCE_QUOTE_OUT_OF_WINDOW: '源报价时间晚于计划帧',
  SOURCE_TIME_UNKNOWN: '源报价时间缺失',
  BATCH_FAILED: '数据源存在失败批次',
  STALE_QUOTE: '存在被丢弃的旧报价',
  AUCTION_AMOUNT_MISSING: '缺少竞价成交金额',
  PRE_CLOSE_MISSING: '缺少昨收',
  YESTERDAY_AMOUNT_MISSING: '缺少昨日成交额',
  PREV_LIMIT_POOL_MISSING: '昨日涨停池未采集',
  PREV_LIMIT_POOL_EMPTY: '昨日涨停池异常为空',
  PREV_LIMIT_POOL_UNVERIFIED: '昨日涨停池不可信',
  PREV_BROKEN_POOL_MISSING: '昨日炸板池未采集',
  PREV_BROKEN_POOL_UNVERIFIED: '昨日炸板池不可信',
  PREV_RELAY_UNVERIFIED: '缺少可信断板证据',
  NO_PREVIOUS_TRADE_DAY: '没有上一交易日',
  NO_AUCTION_ROWS: '当日没有竞价快照',
  NON_TRADE_DAY: '非交易日',
  LATE_FRAME_UNPROVEN: '迟到帧无法证明源报价时间',
  FETCH_EXCEPTION: '抓取失败',
  SOURCE_EMPTY: '数据源返回空表',
  NO_VALID_QUOTE: '没有可用报价',
  WRITE_EXCEPTION: '写库失败',
  NO_EXISTING_AUCTION_ROWS: '没有可更新的竞价行',
  NO_NEWER_QUOTE: '没有比已接受结果更新的报价',
}

function reasonText(code: string): string {
  return REASON_LABEL[code] ?? code
}

function qualityTone(status: string): string {
  if (status === 'VALID') return 'text-up'
  if (status === 'INCOMPLETE' || status === 'PENDING') return 'text-warn'
  if (status === 'NOT_COLLECTED') return 'text-fg-muted'
  return 'text-danger'
}

function frameTone(status: string | null): string {
  if (status === 'ok') return 'text-up'
  if (status === 'partial' || status === 'skipped') return 'text-warn'
  return 'text-danger'
}

/** 覆盖率：null 一律「—」，不能把未知覆盖写成 0%。 */
function coverageText(ratio: number | null, status: string | null): string {
  if (ratio == null) return '—'
  return `${fmtNum(ratio * 100, 1, '%')}${status ? `（${status}）` : ''}`
}

/** 百分点：null 一律「—」，不写 0。 */
function fmtPp(value: number | null): string {
  if (value == null) return '—'
  return `${value > 0 ? '+' : ''}${fmtNum(value, 2)}pp`
}

function prevStatusText(status: string | null, board: number | null): string {
  if (status === 'limit_up') return board ? `${board}板` : '涨停'
  if (status === 'broken') return '炸板'
  if (status === 'failed_relay') return board ? `断板（${board}板）` : '断板'
  return '—'
}

/** 排序方向：三个可排序列都是「越大越值得看」，首次点降序。 */
function nextSort(
  key: AuctionAnomalySort,
  current: AuctionAnomalySort,
  order: 'asc' | 'desc',
): { sort: AuctionAnomalySort; order: 'asc' | 'desc' } {
  if (key === current) return { sort: current, order: order === 'desc' ? 'asc' : 'desc' }
  return { sort: key, order: 'desc' }
}

export default function AuctionAnomaly({ date }: { date: string | null }) {
  const [group, setGroup] = useState<AuctionAnomalyGroupKey>('prev_limit')
  const [sort, setSort] = useState<AuctionAnomalySort>('auction_amount')
  const [order, setOrder] = useState<'asc' | 'desc'>('desc')
  const [data, setData] = useState<AuctionAnomalyData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const seq = useRef(0)

  useEffect(() => {
    const current = ++seq.current
    setData(null)
    setError(null)
    setLoading(true)
    api
      .auctionAnomaly({ date, group, sort, order, limit: 300 })
      .then((value) => {
        if (current === seq.current) setData(value)
      })
      .catch((err: Error) => {
        if (current !== seq.current) return
        setData(null)
        setError(isNoDataError(err) ? null : err.message)
      })
      .finally(() => {
        if (current === seq.current) setLoading(false)
      })
    return () => {
      seq.current += 1
    }
  }, [date, group, sort, order])

  const selected = useMemo(
    () => data?.groups.find((item) => item.group === group) ?? null,
    [data, group],
  )

  const onToggleSort = (key: string) => {
    const result = nextSort(key as AuctionAnomalySort, sort, order)
    setSort(result.sort)
    setOrder(result.order)
  }

  return (
    <Panel
      title="竞价异动"
      meta={
        <span className="num">
          {data
            ? `${data.trade_date} · 昨日 ${data.prev_trade_date ?? '—'} · 0926 终值 / 0931 承接`
            : '只读本地竞价数据 · 不联网补数'}
        </span>
      }
      delay={40}
    >
      {loading ? (
        <LoadState loading className="px-4 py-10" />
      ) : error ? (
        <LoadState error={error} className="px-4 py-10" />
      ) : !data ? (
        <LoadState emptyText="当日暂无竞价异动数据" className="px-4 py-10" />
      ) : (
        <>
          <div className="border-b border-line-soft px-3 py-2.5">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[12px]">
              <span className="text-fg-dim">日期质量</span>
              <span className={`num font-semibold ${qualityTone(data.quality.status)}`}>
                {QUALITY_LABEL[data.quality.status] ?? data.quality.status}
              </span>
              <span className="num text-fg-dim">
                快照 {data.quality.auction_count} · 0926 终值 {data.quality.result_count} · 0931 承接{' '}
                {data.quality.post_open_count}
              </span>
              <span className="num text-fg-dim">
                0926 覆盖{' '}
                {coverageText(
                  data.quality.auction_coverage_ratio,
                  data.quality.auction_coverage_status,
                )}
                {' · '}
                0931 覆盖{' '}
                {coverageText(
                  data.quality.post_open_coverage_ratio,
                  data.quality.post_open_coverage_status,
                )}
              </span>
              {data.quality.reasons.length > 0 && (
                <span className="text-warn">
                  {data.quality.reasons.map(reasonText).join(' · ')}
                </span>
              )}
              {(data.quality.warnings?.length ?? 0) > 0 && (
                <span className="text-fg-muted" title="仅诊断提示，不降级日期质量">
                  {(data.quality.warnings ?? []).map(reasonText).join(' · ')}
                </span>
              )}
            </div>
            {data.quality.frames.length > 0 && (
              <details className="mt-2 text-[12px] text-fg-dim">
                <summary className="cursor-pointer select-none text-fg-muted">
                  帧级采集质量（{data.quality.frames.length} 帧）
                </summary>
                <div className="mt-2 overflow-auto">
                  <table className="grid-table">
                    <thead>
                      <tr>
                        <th>计划帧</th>
                        <th>状态</th>
                        <th>迟到</th>
                        <th>源报价时间</th>
                        <th>请求</th>
                        <th>返回</th>
                        <th>有效</th>
                        <th>写入</th>
                        <th title="written + already_covered / eligible">覆盖</th>
                        <th title="逐券源时间证据数量">逐券时间</th>
                        <th>丢批</th>
                        <th>旧/越界</th>
                        <th>耗时</th>
                        <th>原因</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.quality.frames.map((frame) => (
                        <tr key={`${frame.scheduled_frame}-${frame.actual_started_at}`}>
                          <td className="num">{frame.scheduled_frame ?? '—'}</td>
                          <td className={`num ${frameTone(frame.status)}`}>
                            {frame.status ?? '—'}
                          </td>
                          <td className="num">{frame.late ? '是' : '否'}</td>
                          <td
                            className="num"
                            title={
                              frame.raw_source_quote_time_max
                                ? `已接受 ${frame.source_quote_time ?? '—'} / 原始 ${frame.raw_source_quote_time_min ?? '—'}~${frame.raw_source_quote_time_max}`
                                : undefined
                            }
                          >
                            {frame.source_quote_time ?? '—'}
                          </td>
                          <td className="num">{fmtInt(frame.requested_codes)}</td>
                          <td className="num">{fmtInt(frame.returned_codes)}</td>
                          <td className="num">{fmtInt(frame.valid_codes)}</td>
                          <td className="num">{fmtInt(frame.written_codes)}</td>
                          <td className="num">
                            {coverageText(frame.coverage_ratio, frame.coverage_status)}
                          </td>
                          <td
                            className="num"
                            title={
                              frame.source_time_unknown_codes_truncated ||
                              frame.dropped_old_codes_truncated ||
                              frame.dropped_stale_codes_truncated ||
                              frame.dropped_newer_codes_truncated ||
                              frame.dropped_unknown_codes_truncated
                                ? '代码列表已截断，完整计数见日志'
                                : undefined
                            }
                          >
                            {fmtInt(frame.quote_times_count)}
                            {frame.source_time_unknown_codes_truncated ||
                            frame.dropped_old_codes_truncated ||
                            frame.dropped_stale_codes_truncated ||
                            frame.dropped_newer_codes_truncated ||
                            frame.dropped_unknown_codes_truncated
                              ? '*'
                              : ''}
                          </td>
                          <td className="num">{fmtInt(frame.failed_batches)}</td>
                          <td className="num">
                            {fmtInt(
                              frame.dropped_old_quote +
                                frame.dropped_stale_quote +
                                frame.dropped_newer_quote +
                                frame.dropped_unknown_quote,
                            )}
                          </td>
                          <td className="num">{fmtInt(frame.elapsed_ms)}ms</td>
                          <td className="text-fg-muted">
                            {frame.noop
                              ? '重复请求（无更新）'
                              : frame.reason
                                ? reasonText(frame.reason)
                                : '—'}
                            {frame.last_attempt_failed && frame.last_effective_status ? (
                              <span className="ml-1 text-warn">
                                · 最近一次尝试失败（{frame.last_attempt_status}）
                              </span>
                            ) : null}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-1.5 border-b border-line-soft px-3 py-2">
            {GROUP_ORDER.map((key) => {
              const summary = data.groups.find((item) => item.group === key)
              const active = key === group
              return (
                <button
                  key={key}
                  type="button"
                  aria-pressed={active}
                  onClick={() => setGroup(key)}
                  className={[
                    'border px-2.5 py-[3px] text-[13px] transition-colors',
                    active
                      ? 'border-accent/60 text-accent'
                      : 'border-line text-fg-muted hover:text-fg',
                  ].join(' ')}
                  title={
                    summary?.quality_reasons.length
                      ? summary.quality_reasons.map(reasonText).join('；')
                      : undefined
                  }
                >
                  {GROUP_LABEL[key]}
                  <span className="num ml-1.5 text-fg-dim">
                    {summary
                      ? `总 ${summary.total} · 竞价 ${summary.auction_valid_count} · 承接 ${summary.post_open_valid_count} · 全 ${summary.fully_valid_count}`
                      : '—'}
                  </span>
                </button>
              )
            })}
            <span className="ml-auto text-[12px] text-fg-dim">
              {selected?.quality_reasons.length
                ? selected.quality_reasons.map(reasonText).join(' · ')
                : '分类样本与有效数据数来自本地竞价库'}
            </span>
          </div>

          {data.rows.length === 0 ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              {selected?.total
                ? '该分类有样本，但当日没有可用竞价报价。'
                : '该分类暂无可信样本。'}
              {selected?.quality_reasons.length ? (
                <div className="mt-1 text-[12px]">
                  {selected.quality_reasons.map(reasonText).join(' · ')}
                </div>
              ) : null}
            </div>
          ) : (
            <div className="max-h-[620px] overflow-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <th className="!text-left">代码</th>
                    <th className="!text-left">名称</th>
                    <th className="!text-left max-xl:hidden">昨日</th>
                    <th className="!text-left max-xl:hidden">题材</th>
                    <SortTh
                      sortKey="auction_pct"
                      activeKey={sort}
                      dir={order}
                      onToggle={onToggleSort}
                      title="0926 竞价终值 ÷ 昨收 − 1"
                    >
                      竞价涨幅
                    </SortTh>
                    <SortTh
                      sortKey="auction_amount"
                      activeKey={sort}
                      dir={order}
                      onToggle={onToggleSort}
                      title="0926 帧的累计成交额（元）"
                    >
                      竞价金额
                    </SortTh>
                    <th title="上一交易日日线成交额（元）">昨日金额</th>
                    <th title="竞价金额 ÷ 昨日全天成交额">竞价额占比</th>
                    <SortTh
                      sortKey="post_open_change"
                      activeKey={sort}
                      dir={order}
                      onToggle={onToggleSort}
                      title="post_open_pct − auction_pct，单位：百分点"
                    >
                      9:25→9:31 涨幅变化（百分点）
                    </SortTh>
                    <th>开盘后涨幅</th>
                    <th className="!text-left">质量（竞价 / 承接）</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map((row) => (
                    <tr key={row.code}>
                      <td className="!text-left">
                        <StockLink code={row.code} className="num text-fg-muted">
                          {row.code}
                        </StockLink>
                      </td>
                      <td className="!text-left">
                        <StockLink code={row.code}>{row.name ?? row.code}</StockLink>
                        <QuickResearch code={row.code} asOfDate={data.trade_date} />
                      </td>
                      <td className="!text-left max-xl:hidden">
                        {prevStatusText(row.prev_limit_status, row.prev_board_height)}
                      </td>
                      <td className="!text-left max-xl:hidden" title={row.industry ?? undefined}>
                        {row.concept ?? '—'}
                        {row.concept_source && (
                          <span className="ml-1 text-[11px] text-fg-dim">
                            {row.concept_source}
                            {row.concept_as_of ? ` · ${row.concept_as_of}` : ''}
                          </span>
                        )}
                      </td>
                      <td className={`num ${toneOf(row.auction_pct)}`}>{fmtPct(row.auction_pct)}</td>
                      <td className="num">{fmtAmount(row.auction_amount)}</td>
                      <td className="num text-fg-muted">{fmtAmount(row.yesterday_amount)}</td>
                      <td className="num">{fmtNum(row.auction_amount_ratio, 2, '%')}</td>
                      <td className={`num ${toneOf(row.pct_change_after_auction)}`}>
                        {fmtPp(row.pct_change_after_auction)}
                      </td>
                      <td className={`num ${toneOf(row.post_open_pct)}`}>
                        {fmtPct(row.post_open_pct)}
                      </td>
                      <td className="!text-left">
                        <div
                          className="flex flex-col gap-0.5"
                          title={
                            row.quality_reasons.length
                              ? row.quality_reasons.map(reasonText).join('；')
                              : undefined
                          }
                        >
                          <span className={qualityTone(row.auction_quality_status)}>
                            竞价 {QUALITY_LABEL[row.auction_quality_status] ?? row.auction_quality_status}
                          </span>
                          <span className={qualityTone(row.post_open_quality_status)}>
                            承接 {QUALITY_LABEL[row.post_open_quality_status] ?? row.post_open_quality_status}
                          </span>
                        </div>
                        {row.quality_reasons.length > 0 && (
                          <span className="mt-0.5 block text-[11px] text-fg-dim">
                            {row.quality_reasons.slice(0, 3).map(reasonText).join(' · ')}
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <p className="border-t border-line-soft px-3 py-2 text-[12px] leading-relaxed text-fg-dim">
            竞价金额取 0926 帧的累计成交额，昨日金额取上一交易日日线；两者都可信才计算占比。
            「竞价质量」与「承接质量」独立：0926 可信但 0931 缺失时，竞价涨幅/金额仍展示，
            开盘承接与「9:25→9:31 涨幅变化」为「—」；9:31 尚未到达时承接显示「待观察」。
            「9:25→9:31 涨幅变化」是<span className="text-fg">百分点</span>（
            <span className="num">post_open_pct − auction_pct</span>），不是成交量或成交额变化。
            缺 0926 时竞价字段为「—」，不用 0% 或 -100% 代替；覆盖率未知时也显示「—」。
          </p>
        </>
      )}
    </Panel>
  )
}
