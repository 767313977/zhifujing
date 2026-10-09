import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type {
  PatternTrack,
  PatternTrackCohort,
  PatternTrackDetail,
  PatternTrackPoint,
  PoolHorizon,
  PoolStandingOut,
} from '../api/types'
import Alert from '../components/Alert'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import Segmented from '../components/Segmented'
import { fmtInt, fmtNum, fmtPct, fmtShortDate, toneOf } from '../lib/format'

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
  const [standing, setStanding] = useState<PoolStandingOut | null>(null)
  const [detail, setDetail] = useState<PatternTrackDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async (count: number, isStale: () => boolean) => {
    try {
      // 两个请求并行：上面那份是「全形态混合的前 50 只」，下面那份是「按池子」
      const [result, poolResult] = await Promise.all([
        api.patternTrack(count, TOP, TRACK_DAYS),
        api.poolStanding(count),
      ])
      if (isStale()) return
      setData(result)
      setStanding(poolResult)
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
  }, [picked])

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
          title="悟道池子成绩单"
          meta={
            <span className="num text-[13px]">
              {standing
                ? `从 ${standing.start} 起 · 实际 ${standing.cohorts} 个扫描日 · 到线 / 破位按 ${standing.line_days} 个交易日`
                : '加载中…'}
            </span>
          }
          delay={30}
        >
          {!standing || standing.pools.length === 0 || !hasSamples(standing) ? (
            <div className="space-y-1 px-4 py-10 text-center text-[14px] text-fg-dim">
              <div>
                {loading
                  ? '加载中…'
                  : !standing || standing.pools.length === 0
                    ? '还没有样本 —— 池子的统计从 2026-10-08 起（那天候选范围才定稿），随每天的扫描慢慢攒'
                    : '样本还不够：命中日之后还没有交易日数据（第 1 个扫描日是 10-08），下次采集跑完这一栏就有数'}
              </div>
              <div className="text-[12px]">
                {standing && standing.pools.length > 0 && (
                  <>
                    窗口里已有 {standing.cohorts} 个扫描日、共{' '}
                    {fmtInt(standing.pools.reduce((sum, pool) => sum + pool.hits, 0))} 条命中 ——
                    明晚起逐档（1 / 3 / 5 / 10 日）就会有超额与胜率
                  </>
                )}
              </div>
            </div>
          ) : (
            <PoolStandingTable standing={standing} />
          )}
        </Panel>
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
              <div className="overflow-x-auto">
                <table className="border-collapse">
                  <thead className="sticky top-0 z-20">
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
                            to={`/stock/${row.code}`}
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

/**
 * 窗口里到底有没有**可算的样本**（任意一档收益、或到线/破位任一项）。
 *
 * 刚上线时只有今天一个扫描日、命中日之后的行情还没到 —— 表里会全是「—」，
 * 看起来像坏了。所以这种时候不画表，直接说明「样本还不够、什么时候会有」。
 */
function hasSamples(standing: PoolStandingOut): boolean {
  return standing.pools.some(
    (pool) =>
      pool.samples > 0 || pool.returns.some((item) => item.samples > 0),
  )
}

/**
 * 悟道池子成绩单。
 *
 * 两类指标混在一张表里，列很多，所以：每个收益档位只突出**超额**（按涨跌上色），
 * 胜率与样本缩在下面一行小字；到线率 / 破位率放最后两列 —— 它们回答的是
 * 「这一步到底有没有发生」「纪律有没有被打脸」，比收益更贴这套玩法的用法。
 *
 * ⚠️ 破位率有**两种口径**（纪律里的作废位 / 命中日最低价代理），页面必须显示用的是哪种，
 * 否则两个池子并排看会以为是一回事。代理口径在单元格里标「代理」，完整说明放 tooltip。
 */
function PoolStandingTable({ standing }: { standing: PoolStandingOut }) {
  const horizons = standing.pools[0]?.returns.map((item) => item.days) ?? []
  return (
    <>
      <div className="overflow-x-auto">
        <table className="grid-table">
          <thead>
            <tr>
              <th className="!text-left">池子</th>
              <th>命中 / 只</th>
              <th>扫描日</th>
              {horizons.map((days) => (
                <th key={days}>{days} 日超额</th>
              ))}
              <th>到线率</th>
              <th>破位率</th>
            </tr>
          </thead>
          <tbody>
            {standing.pools.map((pool) => (
              <tr key={pool.pattern}>
                <td className="!text-left">
                  <Link to="/wudao" className="text-fg transition-colors hover:text-accent">
                    {pool.name}
                  </Link>
                </td>
                <td>
                  <span className="num text-fg-muted">
                    {fmtInt(pool.hits)} / {fmtInt(pool.stocks)}
                  </span>
                </td>
                <td>
                  <span className="num text-fg-dim">{pool.scan_days}</span>
                </td>
                {pool.returns.map((item) => (
                  <td key={item.days}>
                    <HorizonCell item={item} />
                  </td>
                ))}
                <td>
                  <RateCell value={pool.touch} samples={pool.touch_samples} />
                </td>
                <td>
                  <RateCell
                    value={pool.stop}
                    samples={pool.samples}
                    hint={
                      pool.stop_source.includes('代理')
                        ? `这个池子的 key_levels 里没有作废位，破位率用「${pool.stop_source}」算`
                        : undefined
                    }
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="border-t border-line-soft px-4 py-2 text-[12px] leading-relaxed text-fg-dim">
        样本＝窗口内全部命中（逐日不去重）；超额＝减掉同一天全市场等权平均；收益按涨跌幅
        逐日复利（前复权口径）。到线 / 破位都用收盘：到线＝收盘站上关键线（样板池＝今高、
        洗盘池＝洗盘高；「明天预案」那条线当天已过，这里算的是站住），破位＝收盘跌破作废位
        （池子没有作废位的用命中日最低价当代理，单元格里标「代理」）。统计从 {standing.start}{' '}
        起 —— 池子的候选范围那天才定稿，更早的命中是另一套口径，混进来数字就不好解释了。
      </div>
    </>
  )
}

/** 一个收益档位：超额 + （胜率、样本）。样本为 0 时如实显示「—」，别当 0%。 */
function HorizonCell({ item }: { item: PoolHorizon }) {
  if (item.samples === 0) {
    return (
      <span className="num text-fg-dim" title="这个档位还没有走到（样本 0）">
        —
      </span>
    )
  }
  return (
    <div className="leading-tight">
      <div className={`num ${toneOf(item.excess)}`}>{fmtPct(item.excess)}</div>
      <div className="num text-[11px] text-fg-dim">
        胜率 {fmtNum(item.beat_pct, 0, '%')} · n={item.samples}
      </div>
    </div>
  )
}

/** 到线率 / 破位率那一格：比例 + 样本（+ 代理口径的提示）。 */
function RateCell({
  value,
  samples,
  hint,
}: {
  value: number | null
  samples: number
  hint?: string
}) {
  if (value == null || samples === 0) {
    return (
      <span className="num text-fg-dim" title="没有可算的样本">
        —
      </span>
    )
  }
  return (
    <div className="leading-tight" title={hint}>
      <div className={`num ${value >= 50 ? 'text-up' : 'text-fg'}`}>
        {fmtNum(value, 1, '%')}
      </div>
      <div className="num text-[11px] text-fg-dim">
        n={samples}
        {hint ? ' · 代理' : ''}
      </div>
    </div>
  )
}
