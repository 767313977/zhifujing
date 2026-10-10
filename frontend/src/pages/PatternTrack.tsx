import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type {
  PatternTrack,
  PatternTrackCohort,
  PatternTrackDetail,
  PatternTrackPoint,
} from '../api/types'
import Alert from '../components/Alert'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import Segmented from '../components/Segmented'
import { fmtNum, fmtPct, fmtShortDate, toneOf } from '../lib/format'

/**
 * 可选的循环个数（= 看最近几个筛选日）。单位是**有命中记录的交易日数**。
 *
 * ⚠️ 后端只统计 **2026-09-24 起**的循环（用户指定）：09-24 之前那几天只扫了 3032 只，
 * 「前 50 只」是在不同大小的池子里排出来的，前后不可比。所以窗口里最多也就是从那天
 * 起的天数，页面要如实写出「实际几个循环」。
 */
const COHORTS = [10, 30, 60, 90]

/** 每个循环跟踪多少个交易日。用户定的一个循环 = 30 个交易日。 */
const TRACK_DAYS = 30

/** 每天选多少只。与形态页默认视图、每天补 DDE 的那批同一口径。 */
const TOP = 50

/**
 * 汇总表里列出的里程碑天数（明细表是逐日列，汇总表只抽几个节点，免得 30 列）。
 * 30 一定在里面 —— 它就是「一个循环」的长度。
 */
const MILESTONES = [5, 10, 20, 30]

/** 从 points 里取某个里程碑那天的点（没走到就是 undefined）。 */
function pointAt(cohort: PatternTrackCohort, day: number): PatternTrackPoint | undefined {
  return cohort.points.find((item) => item.day === day)
}

/** 均值曲线上的峰 / 谷（含它出现在第几天）。 */
function extreme(
  cohort: PatternTrackCohort,
  kind: 'peak' | 'trough',
): PatternTrackPoint | undefined {
  const scored = cohort.points.filter((item) => item.mean != null)
  if (scored.length === 0) return undefined
  return scored.reduce((best, item) =>
    (kind === 'peak' ? item.mean! > best.mean! : item.mean! < best.mean!) ? item : best,
  )
}

export default function PatternTrackPage() {
  const [cohorts, setCohorts] = useState(30)
  /** 明细里正在看哪个循环（点汇总表的「只数」切换） */
  const [picked, setPicked] = useState<string | null>(null)
  const [data, setData] = useState<PatternTrack | null>(null)
  const [detail, setDetail] = useState<PatternTrackDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async (count: number, isStale: () => boolean) => {
    try {
      const result = await api.patternTrack(count, TOP, TRACK_DAYS)
      if (isStale()) return
      setData(result)
      // 默认看最近那个循环（每天最关心的是「今天选出来的 50 只」）
      setPicked(result.days[0]?.trade_date ?? null)
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
    setDetail(null)
    setError(null)
    setLoading(true)
    void load(cohorts, () => stale)
    return () => {
      stale = true
    }
  }, [cohorts, load])

  useEffect(() => {
    if (!picked) return
    let stale = false
    setDetailLoading(true)
    api
      .patternTrackDetail(picked, TOP, TRACK_DAYS)
      .then((result) => {
        if (!stale) setDetail(result)
      })
      .catch((err) => {
        if (!stale) setError((err as Error).message)
      })
      .finally(() => {
        if (!stale) setDetailLoading(false)
      })
    return () => {
      stale = true
    }
    // 依赖里带上 `cohorts`：切换循环数时上面那段 effect 会把 detail 清空，若这里只依赖
    // `picked`（各档循环的「最近那个筛选日」往往是同一天、值没变），就不会重新拉取，
    // 明细会停在「这个循环还没有命中记录」的误报上（2026-10-10 修）。
  }, [picked, cohorts])

  const summary = data?.summary
  const toolbar = (
    <Segmented
      value={cohorts}
      items={COHORTS.map((item) => ({ key: item, label: `${item} 个循环` }))}
      onChange={setCohorts}
    />
  )

  return (
    <Layout toolbar={toolbar}>
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}

      <div className="space-y-4">
        <Panel
          title={picked ? `${picked} 选出的 ${TOP} 只` : '循环明细'}
          meta={
            <span className="num">
              {detailLoading
                ? '加载中…'
                : detail
                  ? `进度 ${detail.progress}/${detail.track_days} · 每列是该交易日的当日涨跌幅`
                  : '—'}
            </span>
          }
          delay={40}
        >
          {!picked || !detail || detail.rows.length === 0 ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              {loading || detailLoading ? '加载中…' : '这个循环还没有命中记录（点下面汇总表的「只数」可切换循环）'}
            </div>
          ) : (
            <>
              {/* 表头不加 `sticky top-0`：本容器只用 overflow-x，overflow-x 会让它
                  同时成为纵向的滚动容器，而这里纵向不滚动（高度随内容）→ sticky 粘不住，
                  是空操作。去掉以免误导；左侧「股票名称」列的 left-0 仍有效（横向用）。 */}
              <div className="overflow-x-auto">
                <table className="border-collapse">
                  <thead>
                    <tr>
                      <th className="sticky left-0 z-30 border-r border-b border-line-soft bg-ink-850 px-2 py-1.5 text-left font-normal whitespace-nowrap text-fg-dim">
                        股票名称
                      </th>
                      {Array.from({ length: detail.track_days }, (_, index) => (
                        <th
                          key={index}
                          className="num border-b border-line-soft bg-ink-850 px-2 py-1.5 text-right font-normal whitespace-nowrap text-fg-muted"
                        >
                          <div>第 {index + 1} 日</div>
                          <div className="text-[11px] text-fg-dim">
                            {detail.days[index] ? fmtShortDate(detail.days[index]) : '—'}
                          </div>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {detail.rows.map((row) => (
                      <tr key={row.code}>
                        <td className="sticky left-0 z-10 border-r border-b border-line-soft bg-ink-900 px-2 py-1 whitespace-nowrap">
                          <Link
                            to={`/stock/${encodeURIComponent(row.code)}`}
                            className="text-fg hover:underline"
                            title={`${row.code} · ${row.score} 分`}
                          >
                            {row.name ?? row.code}
                          </Link>
                          <div className="num text-[11px] text-fg-dim">
                            {row.code} · {row.score} 分
                          </div>
                        </td>
                        {Array.from({ length: detail.track_days }, (_, index) => {
                          const value = row.pct[index]
                          return (
                            <td
                              key={index}
                              className="num border-b border-line-soft px-2 py-1 text-right whitespace-nowrap"
                            >
                              {value == null ? (
                                <span className="text-fg-dim">—</span>
                              ) : (
                                <span className={toneOf(value)}>{fmtPct(value)}</span>
                              )}
                            </td>
                          )
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="px-4 py-3 text-[12px] leading-relaxed text-fg-dim">
                这 50 只就是当天形态命中里
                <span className="text-fg-muted">按评分排前 50</span>
                的那批（与形态页默认视图、每天补 DDE 的同一批）。每列是
                <span className="text-fg-muted">那个交易日的当日涨跌幅</span>
                ，不是从筛选日起算的累计；空着的列是
                <span className="text-fg-muted">还没走到</span>
                的交易日（每天采集完往后接一天），停牌那天也是「—」。
                要看「筛选日收盘买入后整体赚没赚」，看下面汇总表的里程碑列。
              </p>
            </>
          )}
        </Panel>

        <Panel
          title="每个循环"
          meta={
            <span className="num">
              {summary
                ? `从 2026-09-24 起统计 · 实际 ${summary.cohorts_used} 个循环 · 逐循环样本 ${summary.stocks}（去重 ${summary.unique} 只）`
                : '—'}
            </span>
          }
          delay={80}
        >
          {loading || !data || data.days.length === 0 ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              {loading ? '加载中…' : '还没有可统计的循环'}
            </div>
          ) : (
            <div className="overflow-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <th className="!text-left">筛选日</th>
                    <th>只数</th>
                    <th>进度</th>
                    {MILESTONES.map((day) => (
                      <th key={day}>第 {day} 日</th>
                    ))}
                    <th>峰值</th>
                    <th>谷值</th>
                  </tr>
                </thead>
                <tbody>
                  <tr className="text-fg-muted">
                    <td className="!text-left">合计（所有循环摊平）</td>
                    <td>
                      <span className="num">{summary?.stocks}</span>
                    </td>
                    <td>
                      <span className="num text-fg-dim">—</span>
                    </td>
                    {MILESTONES.map((day) => (
                      <td key={day}>
                        <PointCell point={data.average.find((item) => item.day === day)} />
                      </td>
                    ))}
                    <td>
                      <span className="num text-fg-dim">—</span>
                    </td>
                    <td>
                      <span className="num text-fg-dim">—</span>
                    </td>
                  </tr>
                  {data.days.map((row) => {
                    const peak = extreme(row, 'peak')
                    const trough = extreme(row, 'trough')
                    const finished = row.progress >= data.track_days
                    return (
                      <tr
                        key={row.trade_date}
                        className={picked === row.trade_date ? 'bg-ink-800/60' : ''}
                      >
                        <td className="!text-left">
                          <Link
                            to={`/patterns?date=${row.trade_date}`}
                            className="num text-accent hover:underline"
                          >
                            {row.trade_date}
                          </Link>
                        </td>
                        <td>
                          <button
                            type="button"
                            onClick={() => setPicked(row.trade_date)}
                            title="点开看这 50 只是哪些票、之后每天各涨跌多少"
                            className="num text-accent underline decoration-dotted hover:text-fg"
                          >
                            {row.stocks}
                          </button>
                        </td>
                        <td>
                          <span className={`num ${finished ? 'text-fg-muted' : 'text-accent'}`}>
                            {row.progress}/{data.track_days}
                          </span>
                        </td>
                        {MILESTONES.map((day) => (
                          <td key={day}>
                            <PointCell point={pointAt(row, day)} stocks={row.stocks} />
                          </td>
                        ))}
                        <td>
                          <ExtremeCell point={peak} stocks={row.stocks} />
                        </td>
                        <td>
                          <ExtremeCell point={trough} stocks={row.stocks} />
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
              <p className="px-4 py-3 text-[12px] leading-relaxed text-fg-dim">
                口径：<span className="text-fg-muted">筛选日收盘</span>买入、持有到第 n 个
                交易日收盘，收益按涨跌幅逐日复利（即前复权口径）；每格上面是
                <span className="text-fg-muted">平均累计收益</span>
                、小字是「涨 x% · 赢 y%」（上涨占比 / 跑赢当天全市场等权平均的比例）。
                峰谷是均值曲线上的最高 / 最低点（小字是它出现在第几天）。
                <span className="text-fg-muted">未计手续费，也没考虑涨停当天买不进</span>——
                这是理论口径，不是能实现的收益。没走到的节点显示「—」，不按 0% 计入。
              </p>
            </div>
          )}
        </Panel>
      </div>
    </Layout>
  )
}

/** 汇总表里的一格：没走到就「—」，否则两层（平均累计收益 + 两个占比）。 */
function PointCell({
  point,
  stocks,
}: {
  point: PatternTrackPoint | undefined
  /** 当天选出的只数；不传就不显示「样本不足」的 `n=`（合计行那种跨循环的场合） */
  stocks?: number
}) {
  if (!point || point.samples === 0) {
    return (
      <span className="num text-fg-dim" title="这个循环还没走到第 n 个交易日">
        —
      </span>
    )
  }
  return (
    <div className="leading-tight">
      <div className={`num ${toneOf(point.mean)}`}>{fmtPct(point.mean)}</div>
      <div className="num text-[12px] text-fg-dim">
        涨 {fmtNum(point.up_pct, 0, '%')} · 赢 {fmtNum(point.beat_pct, 0, '%')}
        {stocks != null && point.samples < stocks && ` · n=${point.samples}`}
      </div>
    </div>
  )
}

/** 峰 / 谷那一格：值 + 出现在第几天。 */
function ExtremeCell({
  point,
  stocks,
}: {
  point: PatternTrackPoint | undefined
  stocks: number
}) {
  if (!point) {
    return (
      <span className="num text-fg-dim" title="这个循环还没有可算的交易日">
        —
      </span>
    )
  }
  return (
    <div className="leading-tight">
      <div className={`num ${toneOf(point.mean)}`}>{fmtPct(point.mean)}</div>
      <div className="num text-[12px] text-fg-dim">
        第 {point.day} 日
        {point.samples < stocks && ` · n=${point.samples}`}
      </div>
    </div>
  )
}

