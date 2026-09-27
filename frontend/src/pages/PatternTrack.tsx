import { useCallback, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { PatternTrack, PatternTrackHorizon } from '../api/types'
import Alert from '../components/Alert'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import Segmented from '../components/Segmented'
import { fmtNum, fmtPct, toneOf } from '../lib/format'

/**
 * 可选的窗口长度，单位是**有命中记录的交易日数**（用户按「30 个交易日一个周期」定的）。
 *
 * ⚠️ 不是自然日、也不是日历上的最近 N 个交易日：库里 `pattern_hit` 在建站早期只有
 * 零星几天（手动扫描），所以 30 天的窗口可能横跨好几个月 —— 页面必须把「请求 30 天、
 * 实际只有 N 天」如实说出来，否则「合计」那几个数会被读成「最近一个月」的结果。
 */
const WINDOWS = [10, 30, 60, 90]

/** 每天选多少只。与形态页默认视图、每天补 DDE 的那批同一口径。 */
const TOP = 50

export default function PatternTrackPage() {
  const [days, setDays] = useState(30)
  const [data, setData] = useState<PatternTrack | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async (window: number, isStale: () => boolean) => {
    try {
      const result = await api.patternTrack(window, TOP)
      if (!isStale()) setData(result)
    } catch (err) {
      if (!isStale()) setError((err as Error).message)
    } finally {
      if (!isStale()) setLoading(false)
    }
  }, [])

  useEffect(() => {
    let stale = false
    // 先清空：换窗口时旧结果留在表里会被当成新窗口的数据看（与 `Funds` 同一处理）
    setData(null)
    setError(null)
    setLoading(true)
    void load(days, () => stale)
    return () => {
      stale = true
    }
  }, [days, load])

  const summary = data?.summary
  const horizons = summary?.horizons ?? []
  const toolbar = (
    <Segmented
      value={days}
      items={WINDOWS.map((item) => ({ key: item, label: `${item} 日` }))}
      onChange={setDays}
    />
  )

  return (
    <Layout toolbar={toolbar}>
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}

      <div className="space-y-4">
        <Panel
          title="窗口合计"
          meta={
            <span className="num">
              {loading
                ? '加载中…'
                : summary
                  ? `最近 ${days} 日里实际 ${summary.days} 天 · 每天前 ${data?.top ?? TOP} 只`
                  : '—'}
            </span>
          }
          delay={40}
        >
          {!summary || summary.days === 0 ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              {loading ? '加载中…' : '窗口内还没有形态命中记录'}
            </div>
          ) : (
            <>
              <div className="overflow-auto">
                <table className="grid-table">
                  <thead>
                    <tr>
                      <th className="!text-left">合计（全部样本摊平）</th>
                      {horizons.map((item) => (
                        <th key={item.horizon}>持有 {item.horizon} 日</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    <MetricRow label="样本数" horizons={horizons} render={(item) => (
                      <span className="num text-fg-muted">{item.samples}</span>
                    )} />
                    <MetricRow label="平均涨幅" horizons={horizons} render={(item) => (
                      <span className={`num ${toneOf(item.mean)}`}>{fmtPct(item.mean)}</span>
                    )} />
                    <MetricRow label="中位涨幅" horizons={horizons} render={(item) => (
                      <span className={`num ${toneOf(item.median)}`}>{fmtPct(item.median)}</span>
                    )} />
                    <MetricRow label="上涨占比" horizons={horizons} render={(item) => (
                      <span className="num text-fg">{fmtNum(item.up_pct, 1, '%')}</span>
                    )} />
                    <MetricRow label="平均超额" horizons={horizons} render={(item) => (
                      <span className={`num ${toneOf(item.excess)}`}>{fmtPct(item.excess)}</span>
                    )} />
                    <MetricRow label="跑赢大盘占比" horizons={horizons} render={(item) => (
                      <span className="num text-fg">{fmtNum(item.beat_pct, 1, '%')}</span>
                    )} />
                  </tbody>
                </table>
              </div>
              <p className="px-4 py-3 text-[12px] leading-relaxed text-fg-dim">
                窗口 {summary.first_date} ~ {summary.last_date}，逐日样本 {summary.stocks} 条
                （同一只票重复上榜会重复计），去重后 <span className="num">{summary.unique}</span> 只。
                「平均超额」与「跑赢大盘占比」的基准是
                <span className="text-fg-muted">同一天全市场等权平均</span>
                —— 不这么比的话，「涨了 5%」可能只是那几天大盘在涨。
              </p>
            </>
          )}
        </Panel>

        <Panel
          title="逐日胜率"
          meta={
            <span className="num">
              {summary
                ? `${summary.first_date ?? ''} ~ ${summary.last_date ?? ''}，共 ${summary.days} 天`
                : '—'}
            </span>
          }
          delay={80}
        >
          {loading || !data || data.days.length === 0 ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              {loading ? '加载中…' : '窗口内还没有形态命中记录'}
            </div>
          ) : (
            <div className="overflow-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <th className="!text-left">扫描日</th>
                    <th>只数</th>
                    {data.horizons.map((horizon) => (
                      <th key={horizon}>持有 {horizon} 日</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.days.map((row) => (
                    <tr key={row.trade_date}>
                      <td className="!text-left">
                        <Link
                          to={`/patterns?date=${row.trade_date}`}
                          className="num text-accent hover:underline"
                        >
                          {row.trade_date}
                        </Link>
                      </td>
                      <td>
                        <span className="num text-fg-muted">{row.stocks}</span>
                      </td>
                      {row.horizons.map((item) => (
                        <td key={item.horizon}>
                          <HorizonCell item={item} stocks={row.stocks} />
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="px-4 py-3 text-[12px] leading-relaxed text-fg-dim">
                口径：<span className="text-fg-muted">命中日收盘</span>买入、持有 N 个交易日
                （到期日收盘卖出），收益按涨跌幅逐日复利（即前复权口径）；「上涨」= 收益 &gt; 0，
                「跑赢」= 收益高于当天全市场等权平均。<span className="text-fg-muted">
                未计手续费，也没考虑涨停当天买不进</span> —— 这是理论口径，不是能实现的收益。
                到期日还没走到的票不计入样本（所以越上方的行、持有期越长，样本越少，
                小字里的 <span className="num">n=</span> 就是实际样本数）。
              </p>
            </div>
          )}
        </Panel>
      </div>
    </Layout>
  )
}

/** 合计表的一行：同一档指标横着铺在所有持有期上。 */
function MetricRow({
  label,
  horizons,
  render,
}: {
  label: string
  horizons: PatternTrackHorizon[]
  render: (item: PatternTrackHorizon) => ReactNode
}) {
  return (
    <tr>
      <td className="!text-left text-fg-muted">{label}</td>
      {horizons.map((item) => (
        <td key={item.horizon}>{render(item)}</td>
      ))}
    </tr>
  )
}

/**
 * 一格里放「平均涨幅 + 上涨/跑赢占比」两层。
 *
 * 两层是刻意的：只看平均涨幅会被少数大涨的票带偏（一个 +40% 能盖过十只 −3%），
 * 只看上涨占比又看不出赚赔幅度 —— 两个一起看，才能分辨「胜率高但赔率差」和
 * 「胜率低但赔率好」这两种完全不同的形态。
 */
function HorizonCell({ item, stocks }: { item: PatternTrackHorizon; stocks: number }) {
  if (item.samples === 0) {
    return (
      <span className="num text-fg-dim" title="到期日还没走到（或该股当天无行情），不计入样本">
        —
      </span>
    )
  }
  return (
    <div className="leading-tight">
      <div className={`num ${toneOf(item.mean)}`}>{fmtPct(item.mean)}</div>
      <div className="num text-[12px] text-fg-dim">
        涨 {fmtNum(item.up_pct, 0, '%')} · 赢 {fmtNum(item.beat_pct, 0, '%')}
        {item.samples < stocks && ` · n=${item.samples}`}
      </div>
    </div>
  )
}
