import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, isNoDataError } from '../api/client'
import type { PremarketLimitRow, PremarketSummary } from '../api/types'
import Alert from '../components/Alert'
import Layout from '../components/Layout'
import LoadState from '../components/LoadState'
import Panel from '../components/Panel'
import SortTh from '../components/SortTh'
import StockLink from '../components/StockLink'
import { fmtNum, fmtPct, toneOf } from '../lib/format'
import { useSort } from '../lib/sort'
import type { SortSpecs } from '../lib/sort'

/**
 * 盘前 / 开盘页（FEAT-PREMARKET，2026-10-31）。
 *
 * 服务 9:15–10:00 的短线习惯：昨天涨停的那批怎么开、悟道名单开在哪、自选什么状态。
 * **不是分时盯盘**：所有数字都是「昨天收盘 + 今天 9:25 竞价 / 9:31 那一帧」的快照，
 * 页面不轮询、不请求任何外部行情（后端 `services/premarket` 纯读库）。
 *
 * 空态分三种，文案必须能区分（验收要求「降级而不是 500」）：
 * 非交易日 / 竞价未采到 / 真的没有名单。
 */

const LIMIT_SORTS: SortSpecs<PremarketLimitRow> = {
  consecutive: { value: (row) => row.consecutive },
  auction_pct: { value: (row) => row.auction_pct },
  post_open_pct: { value: (row) => row.post_open_pct },
  code: { value: (row) => row.code, first: 'asc' },
}

const QUICK_LINKS = [
  { to: '/limit-up', label: '涨停复盘' },
  { to: '/wudao', label: '悟道之路' },
  { to: '/watchlist', label: '自选股' },
  { to: '/auction', label: '竞价强度' },
  { to: '/patterns', label: '形态选股' },
]

/** `2026-10-31T09:26:03` → `10-31 09:26`（只给人看新鲜度，不追求秒级） */
function shortTime(value: string | null): string {
  if (!value) return '—'
  const [day, rest] = value.split('T')
  return rest ? `${day.slice(5)} ${rest.slice(0, 5)}` : day
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="border border-line-soft px-3 py-2">
      <div className="text-[12px] text-fg-dim">{label}</div>
      <div className="num mt-0.5 text-[15px] text-fg">{value}</div>
      {hint && <div className="mt-0.5 text-[12px] text-fg-muted">{hint}</div>}
    </div>
  )
}

export default function Premarket() {
  const [date, setDate] = useState<string | null>(null)
  const [dates, setDates] = useState<string[]>([])
  const [data, setData] = useState<PremarketSummary | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.dates().then(setDates).catch(() => setDates([]))
  }, [])

  useEffect(() => {
    let stale = false
    setLoading(true)
    setError(null)
    api
      .premarketSummary(date)
      .then((value) => {
        if (!stale) setData(value)
      })
      .catch((err: unknown) => {
        if (stale) return
        setData(null)
        setError(isNoDataError(err) ? null : (err as Error).message)
      })
      .finally(() => {
        if (!stale) setLoading(false)
      })
    return () => {
      stale = true
    }
  }, [date])

  const [sort, limitRows] = useSort(data?.limit_up ?? [], LIMIT_SORTS, { key: null })

  const toolbar = (
    <select
      value={date ?? ''}
      onChange={(event) => setDate(event.target.value || null)}
      className="num border border-line bg-ink-900 px-2 py-[3px] text-[13px] text-fg outline-none focus:border-fg-dim"
      title="缺省 = 今天"
    >
      <option value="">今天</option>
      {[...dates].reverse().map((item) => (
        <option key={item} value={item}>
          {item}
        </option>
      ))}
    </select>
  )

  const notTrading = data !== null && !data.is_trade_day
  const noAuction = data !== null && data.is_trade_day && !data.auction_available

  return (
    <Layout toolbar={toolbar}>
      <div className="space-y-4">
        {error && <Alert onClose={() => setError(null)}>{error}</Alert>}

        <Panel
          title="盘前状态"
          meta={
            data ? (
              <span className="num">
                {data.trade_date}
                {data.prev_trade_date ? ` · 上一交易日 ${data.prev_trade_date}` : ''}
              </span>
            ) : null
          }
          delay={40}
        >
          {data === null ? (
            <LoadState loading={loading} error={error} emptyText="暂无盘前数据" />
          ) : (
            <>
              {data.note && <Alert tone="accent">{data.note}</Alert>}
              <div className="grid grid-cols-2 gap-2 px-4 pt-3 pb-4 sm:grid-cols-4">
                <Stat
                  label="今日"
                  value={data.is_trade_day ? '交易日' : '非交易日'}
                  hint={data.trade_date}
                />
                <Stat label="上一交易日" value={data.prev_trade_date ?? '—'} />
                <Stat
                  label="竞价数据"
                  value={data.auction_available ? '已采到' : '未采到'}
                  hint={
                    data.auction_available
                      ? `${data.auction_result_count} / ${data.auction_count} 只`
                      : '9:15–9:26 自动采样'
                  }
                />
                <Stat label="最近更新" value={shortTime(data.auction_updated_at)} />
              </div>
            </>
          )}
        </Panel>

        <Panel
          title="昨日涨停 / 炸板今开"
          meta={<span className="num">{data?.limit_up.length ?? 0} 只</span>}
          delay={80}
        >
          {data === null ? (
            <LoadState loading={loading} error={error} />
          ) : data.limit_up.length === 0 ? (
            <LoadState
              emptyText={
                notTrading
                  ? '非交易日，没有新的开盘数据'
                  : data.prev_trade_date
                    ? '上一交易日没有涨停 / 炸板记录'
                    : '交易日历里还没有上一交易日'
              }
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <th className="text-left">代码</th>
                    <th className="text-left">名称</th>
                    <th>池</th>
                    <SortTh sortKey="consecutive" {...sort}>
                      连板
                    </SortTh>
                    <th>昨收</th>
                    <SortTh sortKey="auction_pct" {...sort}>
                      竞价 / 今开
                    </SortTh>
                    <SortTh sortKey="post_open_pct" {...sort}>
                      开盘后
                    </SortTh>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {limitRows.map((row) => (
                    <tr key={`${row.code}-${row.pool_type}`}>
                      <td className="text-left">
                        <StockLink code={row.code} className="num text-fg">
                          {row.code}
                        </StockLink>
                      </td>
                      <td className="text-left">{row.name ?? '—'}</td>
                      <td className={row.pool_type === '炸板' ? 'text-fg-dim' : ''}>
                        {row.pool_type}
                      </td>
                      <td className="num">{row.consecutive ?? '—'}</td>
                      <td className="num">{fmtNum(row.prev_close)}</td>
                      <td className={`num ${toneOf(row.auction_pct)}`}>
                        {fmtPct(row.auction_pct)}
                      </td>
                      <td className={`num ${toneOf(row.post_open_pct)}`}>
                        {fmtPct(row.post_open_pct)}
                      </td>
                      <td>{row.status ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {noAuction && data?.limit_up.length ? (
            <div className="border-t border-line-soft px-4 py-2 text-[12px] text-fg-dim">
              今日竞价未采到：开盘前 9:15–9:26 会自动采样，盘前稍后再看；现在只显示昨日收盘。
            </div>
          ) : null}
        </Panel>

        <Panel
          title="明日盯 / 明日预案 · 开盘位置"
          meta={<span className="num">{data?.wudao.length ?? 0} 只</span>}
          delay={120}
        >
          {data === null ? (
            <LoadState loading={loading} error={error} />
          ) : data.wudao.length === 0 ? (
            <LoadState
              emptyText={
                data.prev_trade_date
                  ? `${data.prev_trade_date} 没有悟道名单命中`
                  : '交易日历里还没有上一交易日'
              }
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <th className="text-left">名单</th>
                    <th className="text-left">代码</th>
                    <th className="text-left">名称</th>
                    <th>评分</th>
                    <th>昨收</th>
                    <th>竞价</th>
                    <th>关键价</th>
                    <th>距关键价</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {data.wudao.map((row) => (
                    <tr key={`${row.pattern}-${row.code}`}>
                      <td className="text-left text-fg-muted">{row.pattern_name}</td>
                      <td className="text-left">
                        <StockLink code={row.code} className="num text-fg">
                          {row.code}
                        </StockLink>
                      </td>
                      <td className="text-left">{row.name ?? '—'}</td>
                      <td className="num">{fmtNum(row.score, 1)}</td>
                      <td className="num">{fmtNum(row.prev_close)}</td>
                      <td className={`num ${toneOf(row.auction_pct)}`}>
                        {fmtPct(row.auction_pct)}
                      </td>
                      <td className="num" title={row.key_label}>
                        {fmtNum(row.key_price)}
                      </td>
                      <td className={`num ${toneOf(row.vs_key_pct)}`}>
                        {fmtPct(row.vs_key_pct)}
                      </td>
                      <td>
                        {row.auction_pct === null
                          ? '未采到竞价'
                          : row.above_key
                            ? '已过关键价'
                            : '未过关键价'}
                        {row.break_state ? ` · ${row.break_state}` : ''}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel
          title="自选盘前摘要"
          meta={<span className="num">{data?.watchlist.length ?? 0} 只</span>}
          delay={160}
        >
          {data === null ? (
            <LoadState loading={loading} error={error} />
          ) : data.watchlist.length === 0 ? (
            <LoadState emptyText="自选是空的 —— 去「自选股」加几只再看" />
          ) : (
            <div className="overflow-x-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <th className="text-left">代码</th>
                    <th className="text-left">名称</th>
                    <th>最新收盘</th>
                    <th>日涨跌</th>
                    <th>竞价</th>
                    <th>开盘后</th>
                    <th className="text-left">标签</th>
                  </tr>
                </thead>
                <tbody>
                  {data.watchlist.map((row) => (
                    <tr key={row.code}>
                      <td className="text-left">
                        <StockLink code={row.code} className="num text-fg">
                          {row.code}
                        </StockLink>
                      </td>
                      <td className="text-left">{row.name ?? '—'}</td>
                      <td className="num" title={row.quote_date ?? ''}>
                        {fmtNum(row.close)}
                      </td>
                      <td className={`num ${toneOf(row.pct_chg)}`}>{fmtPct(row.pct_chg)}</td>
                      <td className={`num ${toneOf(row.auction_pct)}`}>
                        {fmtPct(row.auction_pct)}
                      </td>
                      <td className={`num ${toneOf(row.post_open_pct)}`}>
                        {fmtPct(row.post_open_pct)}
                      </td>
                      <td className="text-left text-fg-muted">
                        {row.tags.length ? row.tags.join('、') : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel title="快捷入口" delay={200}>
          <div className="flex flex-wrap gap-2 px-4 py-3">
            {QUICK_LINKS.map((item) => (
              <Link
                key={item.to}
                to={item.to}
                className="border border-line px-3 py-1.5 text-[13px] text-fg-muted transition-colors hover:border-fg-dim hover:text-fg"
              >
                {item.label} →
              </Link>
            ))}
          </div>
        </Panel>
      </div>
    </Layout>
  )
}
