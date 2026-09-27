import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { PatternTrack, PatternTrackCohort, PatternTrackPoint } from '../api/types'
import Alert from '../components/Alert'
import EChart from '../components/EChart'
import type { ChartOption } from '../components/EChart'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import Segmented from '../components/Segmented'
import { AXIS_LABEL, AXIS_LINE, CHART, GRID, LEGEND, SPLIT_LINE, TOOLTIP, withAlpha } from '../lib/chart'
import { fmtNum, fmtPct, toneOf } from '../lib/format'

/**
 * 可选的循环个数，单位是**有命中记录的交易日数**（用户按「30 个交易日一个循环」定的）。
 *
 * ⚠️ 不是自然日、也不是日历上的最近 N 个交易日：库里 `pattern_hit` 在建站早期只有
 * 零星几天，所以「最近 30 个循环」可能横跨好几个月 —— 页面必须把「请求 30 个、实际
 * 只有 N 个」如实说出来，否则会被读成「最近一个月」的结果。
 */
const COHORTS = [10, 30, 60, 90]

/** 每个循环跟踪多少个交易日。用户定的一个循环 = 30 个交易日。 */
const TRACK_DAYS = 30

/** 每天选多少只。与形态页默认视图、每天补 DDE 的那批同一口径。 */
const TOP = 50

/**
 * 表格里列出的里程碑天数（曲线上是每一天，表格只抽几个节点，免得 30 列）。
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
  // 选中的循环（表里点一行）—— 图上会把它的曲线挑出来，其余循环退成淡灰底
  const [active, setActive] = useState<string | null>(null)
  const [data, setData] = useState<PatternTrack | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async (count: number, isStale: () => boolean) => {
    try {
      const result = await api.patternTrack(count, TOP, TRACK_DAYS)
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
    setActive(null)
    setLoading(true)
    void load(cohorts, () => stale)
    return () => {
      stale = true
    }
  }, [cohorts, load])

  const option = useMemo(() => (data ? buildChart(data, active) : null), [data, active])
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
          title="循环曲线"
          meta={
            <span className="num">
              {loading
                ? '加载中…'
                : summary
                  ? `最近 ${cohorts} 个循环（实际 ${summary.cohorts_used} 天）· 每个循环跟踪 ${data?.track_days ?? TRACK_DAYS} 个交易日`
                  : '—'}
            </span>
          }
          delay={40}
        >
          {!data || !option ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              {loading ? '加载中…' : '窗口内还没有形态命中记录'}
            </div>
          ) : (
            <>
              <div className="px-2 pt-2">
                <EChart option={option} height={320} />
              </div>
              <p className="px-4 py-3 text-[12px] leading-relaxed text-fg-dim">
                横轴 = 筛选后第几个交易日，纵轴 = 该循环前 {TOP} 只的
                <span className="text-fg-muted">平均累计收益</span>。金色粗线是
                <span className="text-accent">所有循环摊平后的主线</span>
                ，灰色细线是每个循环各自的一条；点下面表格里的某一行可以把那条循环挑出来。
                曲线只画到<span className="text-fg-muted">已经走完的</span>那天（新的循环会
                一天天接上去）—— 所以越靠上的循环线越短。
                <span className="text-fg-muted">
                  主线在每一个横坐标上只包含「走到那一步」的循环
                </span>
                ，所以靠右的那几段参与的循环更少，别把它读成「某一批 50 只的 30 天路径」。
              </p>
            </>
          )}
        </Panel>

        <Panel
          title="每个循环"
          meta={
            <span className="num">
              {summary
                ? `${summary.first_date ?? ''} ~ ${summary.last_date ?? ''} · 逐循环样本 ${summary.stocks}（去重 ${summary.unique} 只）`
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
                  {data.days.map((row) => {
                    const peak = extreme(row, 'peak')
                    const trough = extreme(row, 'trough')
                    const finished = row.progress >= (data.track_days ?? TRACK_DAYS)
                    return (
                      <tr
                        key={row.trade_date}
                        onClick={() =>
                          setActive(active === row.trade_date ? null : row.trade_date)
                        }
                        className={
                          active === row.trade_date ? 'cursor-pointer bg-ink-800/60' : 'cursor-pointer'
                        }
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
                          <span className="num text-fg-muted">{row.stocks}</span>
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
                峰谷指的是<span className="text-fg-muted">均值曲线</span>上的最高 / 最低点
                （小字是它出现在第几天）。
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

/** 里程碑那一格：没走到就「—」，否则两层（平均收益 + 两个占比）。 */
function PointCell({
  point,
  stocks,
}: {
  point: PatternTrackPoint | undefined
  stocks: number
}) {
  if (!point || point.samples === 0) {
    return (
      <span className="num text-fg-dim" title="这个循环还没走到第 n 个交易日（或该股当天无行情）">
        —
      </span>
    )
  }
  return (
    <div className="leading-tight">
      <div className={`num ${toneOf(point.mean)}`}>{fmtPct(point.mean)}</div>
      <div className="num text-[12px] text-fg-dim">
        涨 {fmtNum(point.up_pct, 0, '%')} · 赢 {fmtNum(point.beat_pct, 0, '%')}
        {point.samples < stocks && ` · n=${point.samples}`}
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

/**
 * 循环曲线：主线 + 每个循环一条细线。
 *
 * 灰色细线**不区分颜色**（不用 `SERIES_PALETTE`）：30 条线各给一个色相，图例就得列
 * 30 项、读者也认不出来。它们的用途只是「看出离散度」——这个循环比主线好还是差；
 * 要具体看某一条就点表格里那一行，它会被挑成高亮色。
 */
function buildChart(data: PatternTrack, active: string | null): ChartOption {
  const days = data.track_days
  const axis = Array.from({ length: days }, (_, index) => index + 1)
  const series: NonNullable<ChartOption['series']> = data.days.map((row) => {
    const values = new Map(row.points.map((item) => [item.day, item.mean]))
    const selected = row.trade_date === active
    return {
      type: 'line' as const,
      name: row.trade_date,
      data: axis.map((day) => values.get(day) ?? null),
      symbol: 'none',
      // 缺值绝不插值：连出一条假线比断开更容易被读成「数据是连续的」
      connectNulls: false,
      silent: true,
      lineStyle: {
        width: selected ? 1.8 : 1,
        color: selected ? CHART.fg : withAlpha(CHART.fgDim, 0.18),
      },
      itemStyle: { color: selected ? CHART.fg : withAlpha(CHART.fgDim, 0.18) },
      z: selected ? 3 : 1,
    }
  })

  const average = new Map(data.average.map((item) => [item.day, item.mean]))
  series.push({
    type: 'line' as const,
    name: '平均',
    data: axis.map((day) => average.get(day) ?? null),
    symbol: 'none',
    connectNulls: false,
    lineStyle: { width: 2.2, color: CHART.accent },
    itemStyle: { color: CHART.accent },
    z: 4,
    // 零轴：图上最该一眼看出的就是「在 0 上方还是下方」
    markLine: {
      silent: true,
      symbol: 'none',
      label: { show: false },
      lineStyle: { color: CHART.fgDim, type: 'dashed' as const, width: 1 },
      data: [{ yAxis: 0 }],
    },
  })

  // tooltip 只报主线与选中的那条：`trigger: 'axis'` 会把**所有**序列都收集进来，
  // 30 个循环全列出来等于刷屏
  const wanted = new Set(active ? [active, '平均'] : ['平均'])

  return {
    grid: { ...GRID, top: 30 },
    legend: { ...LEGEND, top: 0, right: 0, data: active ? [active, '平均'] : ['平均'] },
    tooltip: {
      ...TOOLTIP,
      trigger: 'axis' as const,
      axisPointer: {
        type: 'line' as const,
        lineStyle: { color: CHART.fgDim, type: 'dashed' as const },
      },
      formatter: (params: unknown) => {
        const items = params as { seriesName: string; value: number | null; dataIndex: number }[]
        if (!items?.length) return ''
        const lines = [`筛选后第 ${axis[items[0].dataIndex]} 个交易日`]
        for (const item of items) {
          if (item.value == null || !wanted.has(item.seriesName)) continue
          lines.push(`${item.seriesName === '平均' ? '平均' : `循环 ${item.seriesName}`}：${fmtPct(item.value)}`)
        }
        return lines.join('<br/>')
      },
    },
    xAxis: {
      type: 'category' as const,
      data: axis,
      name: '筛选后交易日',
      nameTextStyle: { color: CHART.fgDim, fontSize: 12 },
      axisLabel: AXIS_LABEL,
      axisLine: AXIS_LINE,
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value' as const,
      axisLabel: { ...AXIS_LABEL, formatter: (value: number) => `${value}%` },
      splitLine: SPLIT_LINE,
      axisLine: { show: false },
    },
    series,
  }
}
