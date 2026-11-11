import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, isNoDataError } from '../api/client'
import type {
  LimitPool,
  LimitThemes,
  PromotionFunnel as PromotionFunnelData,
  PromotionFunnelDateItem,
  PromotionSeries,
} from '../api/types'
import Alert from '../components/Alert'
import EChart from '../components/EChart'
import type { ChartOption } from '../components/EChart'
import LadderBoard from '../components/LadderBoard'
import Layout from '../components/Layout'
import LimitConclusion from '../components/LimitConclusion'
import LoadState from '../components/LoadState'
import LimitTable from '../components/LimitTable'
import Panel from '../components/Panel'
import PromotionFunnel from '../components/PromotionFunnel'
import {
  AXIS_LABEL,
  AXIS_LINE,
  CHART,
  GRID,
  LEGEND,
  SERIES_PALETTE,
  SPLIT_LINE,
  TOOLTIP,
} from '../lib/chart'
import { fmtInt, fmtNum, fmtPct, fmtShortDate, toneOf } from '../lib/format'

/** 首封时间分桶。首封越早说明资金越坚决，所以时间分布是强度的直接体现。 */
const TIME_BUCKETS: { label: string; from: number; to: number }[] = [
  { label: '竞价', from: 0, to: 93000 },
  { label: '09:30-10:00', from: 93000, to: 100000 },
  { label: '10:00-10:30', from: 100000, to: 103000 },
  { label: '10:30-11:30', from: 103000, to: 113000 },
  { label: '13:00-13:30', from: 130000, to: 133000 },
  { label: '13:30-14:30', from: 133000, to: 143000 },
  { label: '14:30-15:00', from: 143000, to: 240000 },
]

/** 晋级率只展示样本量足够的档位；4进5 及以上每日基数常只有 1-3 只，会 0/100 反复跳。 */
const CHART_LEVELS = [1, 2, 3]

/** 日期摘要是**池数据**可信度，逐券是否可验证由打开后的漏斗计算给出。 */
const FUNNEL_QUALITY_LABEL: Record<string, string> = {
  VERIFIED: '池数据可信',
  PARTIAL: '池数据部分可信',
  UNVERIFIED: '池数据不可信',
  NOT_COLLECTED: '未采集',
}

export default function LimitReview() {
  const [date, setDate] = useState<string | null>(null)
  const [dates, setDates] = useState<string[]>([])
  const [pool, setPool] = useState<LimitPool | null>(null)
  const [broken, setBroken] = useState<LimitPool | null>(null)
  const [promotion, setPromotion] = useState<PromotionSeries | null>(null)
  const [promotionError, setPromotionError] = useState<string | null>(null)
  const [themes, setThemes] = useState<LimitThemes | null>(null)
  // 点题材标签筛选涨停明细，null 表示不筛
  const [themeFilter, setThemeFilter] = useState<string | null>(null)
  // 点行业标签再筛一层（同花顺行业口径，与上面的开盘啦板块是两个维度），null 表示不筛
  const [industryFilter, setIndustryFilter] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [view, setView] = useState<'review' | 'funnel'>('review')
  const [funnelDate, setFunnelDate] = useState<string | null>(null)
  const [funnelDates, setFunnelDates] = useState<PromotionFunnelDateItem[]>([])
  const [funnelDatesError, setFunnelDatesError] = useState<string | null>(null)
  const [funnel, setFunnel] = useState<PromotionFunnelData | null>(null)
  const [funnelError, setFunnelError] = useState<string | null>(null)
  const [funnelLoading, setFunnelLoading] = useState(false)
  const funnelSeq = useRef(0)

  useEffect(() => {
    api.dates().then(setDates).catch(() => setDates([]))
    api
      .promotion(15)
      .then(setPromotion)
      .catch((err: Error) => {
        // 「取数失败」与「样本不够」是两件事。都落进下面那句「需要至少 2 个
        // 交易日的涨停池」的话，接口报错会被读成「数据还在攒，再等两天」。
        // 404 = 还没数据（非交易日/未采集），按空态处理（2026-10-23）。
        setPromotion(null)
        setPromotionError(isNoDataError(err) ? null : err.message)
      })
  }, [])

  /**
   * @param isStale 判断这次请求还算不算数。切日期时旧日期的响应可能晚到，
   *   没有守卫的话会把新日期的页面盖成旧日期的数据（2026-09-27 修）。
   */
  const load = useCallback(
    async (target: string | null, isStale: () => boolean = () => false) => {
      setLoading(true)
      setError(null)
      setThemeFilter(null)
      setIndustryFilter(null)
      try {
        const [up, brokenPool, themeData] = await Promise.all([
          api.limitPool('up', target),
          api.limitPool('broken', target),
          api.limitThemes(target).catch(() => null),
        ])
        if (isStale()) return
        setPool(up)
        setBroken(brokenPool)
        setThemes(themeData)
      } catch (err) {
        if (isStale()) return
        setPool(null)
        setBroken(null)
        setThemes(null)
        // 404 = 这个交易日还没有涨停池数据（非交易日 / 尚未采集）→ 空态；
        // 只有 5xx / 超时 / 断网才显示红条（2026-10-23 统一 404 语义）
        setError(isNoDataError(err) ? null : (err as Error).message)
      } finally {
        if (!isStale()) setLoading(false)
      }
    },
    [],
  )

  useEffect(() => {
    let stale = false
    // 先清空：旧日期的池留在表里会被当成新日期的数据看（与 `Funds` 同一处理）
    setPool(null)
    setBroken(null)
    setThemes(null)
    void load(date, () => stale)
    return () => {
      stale = true
    }
  }, [date, load])

  /**
   * 漏斗只在切到该 Tab 时取；日期一变先清空，再用 seq 丢弃旧日期的晚到响应，
   * 避免「页面显示新日期、内容却是旧日期」。
   */
  useEffect(() => {
    if (view !== 'funnel') return
    const seq = ++funnelSeq.current
    setFunnel(null)
    setFunnelError(null)
    setFunnelLoading(true)
    api
      .promotionFunnel(funnelDate)
      .then((data) => {
        if (seq === funnelSeq.current) setFunnel(data)
      })
      .catch((err: Error) => {
        if (seq !== funnelSeq.current) return
        setFunnel(null)
        setFunnelError(isNoDataError(err) ? null : err.message)
      })
      .finally(() => {
        if (seq === funnelSeq.current) setFunnelLoading(false)
      })
    return () => {
      funnelSeq.current += 1
    }
  }, [funnelDate, view])

  /** 漏斗日期列表按需加载，不复用旧 Tab 的 `api.dates()`（那个只覆盖有数据的日期）。 */
  useEffect(() => {
    if (view !== 'funnel' || funnelDates.length > 0) return
    let stale = false
    api
      .promotionFunnelDates(255)
      .then((data) => {
        if (!stale) setFunnelDates(data.dates)
      })
      .catch((err: Error) => {
        if (!stale) setFunnelDatesError(err.message)
      })
    return () => {
      stale = true
    }
  }, [view, funnelDates.length])

  const stocks = useMemo(() => pool?.stocks ?? [], [pool])

  /** 代码 → 全部题材（筛选与统计用，不截断）。 */
  const themeIndex = useMemo(() => {
    const map: Record<string, string[]> = {}
    for (const item of themes?.stocks ?? []) {
      map[item.code] = item.themes
    }
    return map
  }, [themes])

  /**
   * 行业筛选条的**基数**：先应用**题材**（开盘啦板块）筛选、但**不含行业**本身。
   *
   * 为什么不是「当日全部涨停股」：那样 chip 上的家数不随题材变 —— 会出现
   * 「点了题材『芯片』后，行业『银行』写着 5 只、点进去却是 0 只」。以题材筛后的
   * 集合为基数，家数就等于「点下去真能看到几只」，也顺带避免了筛出空结果。
   */
  const industryBase = useMemo(
    () =>
      themeFilter
        ? stocks.filter((stock) => themeIndex[stock.code]?.includes(themeFilter))
        : stocks,
    [stocks, themeFilter, themeIndex],
  )

  /** 按**同花顺行业**聚合今日涨停家数（给涨停明细上方的行业筛选条用）。 */
  const industryCounts = useMemo(() => {
    const map = new Map<string, number>()
    for (const stock of industryBase) {
      if (!stock.industry) continue
      map.set(stock.industry, (map.get(stock.industry) ?? 0) + 1)
    }
    return [...map.entries()].sort((a, b) => b[1] - a[1])
  }, [industryBase])

  /**
   * 筛选后要展示的涨停股。两个条件是**同时**生效的：
   * - `themeFilter` 按**开盘啦板块**（题材，`themeIndex` 那份全量归属）
   * - `industryFilter` 按**同花顺行业**（涨停明细里那一列）
   * 两个维度不互相替代，所以是「与」而不是「或」。
   */
  const visibleStocks = useMemo(
    () =>
      industryFilter
        ? industryBase.filter((stock) => stock.industry === industryFilter)
        : industryBase,
    [industryBase, industryFilter],
  )

  /**
   * 题材联动高亮（规范 v1 §5 Phase 5）：选了题材时，**涨停梯队**里命中的 chip 高亮、
   * 其余淡出 —— 与下方「涨停明细」的筛选用的是同一份 `themeIndex`，两处不会说两套话。
   * 没选题材时返回 null（不做任何视觉处理）。
   */
  const highlightCodes = useMemo(() => {
    if (!themeFilter) return null
    return new Set(
      stocks
        .filter((stock) => themeIndex[stock.code]?.includes(themeFilter))
        .map((stock) => stock.code),
    )
  }, [themeFilter, stocks, themeIndex])

  /** 首封时间分桶计数。 */
  const timeDistribution = useMemo(() => {
    const counts = TIME_BUCKETS.map(() => 0)
    for (const stock of stocks) {
      if (!stock.first_seal_time) continue
      const value = Number(stock.first_seal_time)
      const index = TIME_BUCKETS.findIndex(
        (bucket) => value >= bucket.from && value < bucket.to,
      )
      if (index >= 0) counts[index] += 1
    }
    return counts
  }, [stocks])

  /**
   * 按所属**开盘啦精选板块**聚合涨停家数。
   *
   * 原来用的是同花顺行业（`industry`），答的是「公司做什么生意」；这张图要看的是
   * 「资金在哪个方向抱团」，所以跟着梯队一起换成开盘啦口径（见设计文档 8.36）。
   * 顺带少了「行业名被上游截断成 4 字」那个毛病。
   *
   * 没归到板块的票跳掉不计（09-21 是 103 只里的 2 只），百分比因此对全池算，
   * 不会凑成 100%。
   */
  const boardRanking = useMemo(() => {
    const map = new Map<string, number>()
    for (const stock of stocks) {
      if (!stock.board) continue
      map.set(stock.board, (map.get(stock.board) ?? 0) + 1)
    }
    return [...map.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12)
  }, [stocks])

  const promotionOption = useMemo<ChartOption>(() => {
    if (!promotion || promotion.dates.length === 0) return {}
    const labels = promotion.dates.map(fmtShortDate)
    const seriesFor = (label: string, data: (number | null)[], color: string) => ({
      type: 'line' as const,
      name: label,
      data,
      symbol: 'circle',
      symbolSize: 5,
      connectNulls: false,
      lineStyle: { width: 1.6, color },
      itemStyle: { color },
    })

    const palette = [CHART.up, CHART.accent, SERIES_PALETTE[1], CHART.fgMuted]
    const series = promotion.levels
      .filter((item) => CHART_LEVELS.includes(item.level))
      .map((item, i) => seriesFor(item.label, item.rates, palette[i]))
    series.push(seriesFor('整体', promotion.overall_rates, palette[3]))

    return {
      grid: { ...GRID, top: 46 },
      legend: {
        ...LEGEND,
        top: 2,
        data: [...series.map((item) => item.name)],
      },
      tooltip: {
        ...TOOLTIP,
        trigger: 'axis' as const,
        // ECharts 对空值点传给 formatter 的是 '-'，不是 null，都要归成占位符
        valueFormatter: (value: unknown) =>
          value == null || value === '-' ? '—' : `${value}%`,
      },
      axisPointer: {
        type: 'line' as const,
        lineStyle: { color: CHART.fgDim, type: 'dashed' as const },
      },
      xAxis: {
        type: 'category' as const,
        data: labels,
        axisLabel: AXIS_LABEL,
        axisLine: AXIS_LINE,
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value' as const,
        name: '晋级率 %',
        nameTextStyle: { color: CHART.fgDim, fontSize: 11, fontFamily: AXIS_LABEL.fontFamily },
        min: 0,
        max: 100,
        axisLabel: { ...AXIS_LABEL, formatter: '{value}%' },
        splitLine: SPLIT_LINE,
        axisLine: { show: false },
      },
      series,
    }
  }, [promotion])

  const timeOption = useMemo<ChartOption>(() => {
    return {
      grid: { ...GRID, top: 18, bottom: 8 },
      tooltip: { ...TOOLTIP, trigger: 'axis' as const, axisPointer: { type: 'shadow' as const } },
      xAxis: {
        type: 'category' as const,
        data: TIME_BUCKETS.map((bucket) => bucket.label),
        axisLabel: { ...AXIS_LABEL, interval: 0, rotate: 30, fontSize: 11 },
        axisLine: AXIS_LINE,
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value' as const,
        minInterval: 1,
        axisLabel: AXIS_LABEL,
        splitLine: SPLIT_LINE,
        axisLine: { show: false },
      },
      series: [
        {
          type: 'bar' as const,
          name: '首封家数',
          data: timeDistribution,
          itemStyle: { color: CHART.up },
          barMaxWidth: 30,
        },
      ],
    }
  }, [timeDistribution])

  /**
   * 晋级率序列里「所选涨停池那一天」的下标。
   *
   * ⚠️ 不能直接取 `.at(-1)`（2026-11-01 复查 P1-1）：`promotion` 只在挂载时取一次，
   * 后端又以**库内最新交易日**为锚，翻到 10-28 时 `.at(-1)` 是「最新一天」的晋级率，
   * 会和当天的涨停家数混在同一行里。这里按 `pool.trade_date` 反查：
   * 查不到（所选日不在近 15 日窗口内）就是 -1，所有晋级率一律显示「—」。
   */
  const promotionIndex = useMemo(() => {
    if (!promotion || !pool?.trade_date) return -1
    return promotion.dates.indexOf(pool.trade_date)
  }, [promotion, pool])

  const latestOverall =
    promotionIndex >= 0 ? (promotion?.overall_rates[promotionIndex] ?? null) : null
  const latestPromoted =
    promotionIndex >= 0 ? (promotion?.overall_promoted[promotionIndex] ?? null) : null
  const latestBase =
    promotionIndex >= 0 ? (promotion?.overall_counts[promotionIndex] ?? null) : null
  /** 表里每列取「那一天」的值；下标无效时一律 null（表格显示 —）。 */
  const atDay = (values: (number | null)[]) =>
    promotionIndex >= 0 ? (values[promotionIndex] ?? null) : null

  const toolbar = (
    <>
      <div className="flex items-center gap-1">
        {(
          [
            ['review', '涨停复盘'],
            ['funnel', '晋级漏斗'],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            aria-pressed={view === key}
            onClick={() => setView(key)}
            className={[
              'border px-2 py-[3px] text-[13px] transition-colors',
              view === key
                ? 'border-accent/50 text-accent'
                : 'border-line text-fg-muted hover:text-fg',
            ].join(' ')}
          >
            {label}
          </button>
        ))}
      </div>
      <span className="num hidden text-[13px] text-fg-dim lg:inline">
        {view === 'funnel'
          ? funnelLoading
            ? '加载中…'
            : funnelError
              ? '—'
              : funnel
                ? funnel.levels.length > 0
                  ? `${funnel.levels.reduce((sum, item) => sum + item.base, 0)} 只基数`
                  : '暂无可信基数'
                : '—'
          : loading
            ? '加载中…'
            : error
              ? '—'
              : `${stocks.length} 只涨停`}
      </span>
      {view === 'funnel' ? (
        <select
          value={funnelDate ?? ''}
          onChange={(event) => setFunnelDate(event.target.value || null)}
          className="num border border-line bg-ink-900 px-2 py-[3px] text-[13px] text-fg outline-none focus:border-fg-dim"
        >
          <option value="">最新</option>
          {[...funnelDates].reverse().map((item) => (
            <option key={item.trade_date} value={item.trade_date}>
              {item.trade_date} · {FUNNEL_QUALITY_LABEL[item.quality_status] ?? item.quality_status}
            </option>
          ))}
        </select>
      ) : (
        <select
          value={date ?? ''}
          onChange={(event) => setDate(event.target.value || null)}
          className="num border border-line bg-ink-900 px-2 py-[3px] text-[13px] text-fg outline-none focus:border-fg-dim"
        >
          <option value="">最新</option>
          {/* 倒序渲染：最近的排最上面。原先是升序，展开后要一路滚到底才够得着昨天 */}
          {[...dates].reverse().map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </select>
      )}
    </>
  )

  /** 涨停明细上方的**行业**筛选条：点一下只留该行业、再点清除。与题材筛选可叠加。 */
  const industryBar = (
    <div className="flex flex-wrap gap-1.5 border-b border-line-soft px-3 py-2">
      <button
        type="button"
        aria-pressed={industryFilter === null}
        onClick={() => setIndustryFilter(null)}
        className={[
          'border px-2 py-[3px] text-[13px] transition-colors',
          industryFilter === null
            ? 'border-accent/50 text-accent'
            : 'border-line text-fg-muted hover:text-fg',
        ].join(' ')}
      >
        全部 {industryBase.length}
      </button>
      {industryCounts.map(([name, count]) => (
        <button
          key={name}
          type="button"
          aria-pressed={name === industryFilter}
          onClick={() => setIndustryFilter(name === industryFilter ? null : name)}
          title={`${name} ${count} 只涨停`}
          className={[
            'flex items-baseline gap-1.5 border px-2 py-[3px] text-[13px] transition-colors',
            name === industryFilter
              ? 'border-accent/50 text-accent'
              : 'border-line text-fg-muted hover:text-fg',
          ].join(' ')}
        >
          <span>{name}</span>
          <span className="num text-fg">{count}</span>
        </button>
      ))}
    </div>
  )

  return (
    <Layout toolbar={toolbar}>
      {error && (
        <Alert onClose={() => setError(null)}>{error}</Alert>
      )}

      {view === 'funnel' ? (
        <Panel
          title="涨停晋级漏斗"
          meta={
            <span className="num">
              按昨日连板档 · 昨日池不可信时只展示基数，不计算成功率
            </span>
          }
          delay={40}
        >
          {funnelDatesError && (
            <div className="border-b border-line-soft px-3 py-2 text-[12px] text-danger">
              漏斗日期加载失败：{funnelDatesError}
            </div>
          )}
          <PromotionFunnel data={funnel} loading={funnelLoading} error={funnelError} />
        </Panel>
      ) : (
      <div className="space-y-4">
        {/* 结论条（规范 v1 §5 Phase 5）：涨停家数 / 最高板 / 炸板 / 主线，
            全部取自本页已加载的数据。晋级率只在下面「连板晋级率」面板出现一次，
            避免同一个数字上下相邻念两遍（2026-11-01 去重） */}
        <LimitConclusion
          pool={pool}
          broken={broken}
          themes={themes}
          loading={loading}
        />

        <Panel
          title="连板晋级率"
          meta={
            <span className="num">
              {promotion && promotion.dates.length > 0
                ? `${fmtShortDate(promotion.dates[0])} ~ ${fmtShortDate(promotion.dates.at(-1)!)} · ${promotion.dates.length} 个交易日`
                : promotionError
                  ? '取数失败'
                  : '暂无数据'}
              <span className="ml-3 text-fg-dim">
                昨日 N 板股今日晋级 N+1 板的比例 · 样本不足的高档位已略去
              </span>
            </span>
          }
          delay={40}
        >
          {promotionError ? (
            <div className="px-4 py-10 text-center text-[14px] text-danger">
              {promotionError}
            </div>
          ) : promotion && promotion.dates.length > 0 ? (
            <>
              <div className="flex flex-wrap items-stretch gap-px border-b border-line-soft bg-line-soft">
                {/* 晋级率是**比率**（0–100），不是涨跌幅：用 fmtPct 会写成「+45.0%」，
                    正号会让人误读成「比上日多了 45 个点」（复查 P1-2）。也别染色 ——
                    涨跌色在本站专指价格方向。 */}
                <Stat label="整体晋级率" value={fmtNum(latestOverall, 1, '%')} />
                <Stat label="昨日涨停基数" value={fmtInt(latestBase)} unit="只" />
                <Stat label="今日晋级" value={fmtInt(latestPromoted)} unit="只" />
                <Stat
                  label="口径"
                  value="整体 = 昨日涨停股今日仍涨停"
                  muted
                />
              </div>
              <div className="px-2 pt-2">
                <EChart option={promotionOption} height={260} />
              </div>
              {/* 晋级率表（规范 v1 §5 Phase 5）：图看趋势，表看**所选日期**各档的
                  基数 / 晋级数 / 比率。数据来自同一个 `promotion` 序列，没有另算。 */}
              <div className="border-t border-line-soft">
                <table className="grid-table">
                  <thead>
                    <tr>
                      <th className="text-left">档位</th>
                      <th>昨日基数</th>
                      <th>今日晋级</th>
                      <th>晋级率</th>
                    </tr>
                  </thead>
                  <tbody>
                    {promotion.levels.map((item) => (
                      <tr key={item.level}>
                        <td className="text-left">{item.label}</td>
                        <td className="num">{fmtInt(atDay(item.counts))}</td>
                        <td className="num">{fmtInt(atDay(item.promoted))}</td>
                        <td className="num">{fmtNum(atDay(item.rates), 1, '%')}</td>
                      </tr>
                    ))}
                    <tr>
                      <td className="text-left">整体</td>
                      <td className="num">{fmtInt(atDay(promotion.overall_counts))}</td>
                      <td className="num">{fmtInt(atDay(promotion.overall_promoted))}</td>
                      <td className="num">{fmtNum(atDay(promotion.overall_rates), 1, '%')}</td>
                    </tr>
                  </tbody>
                </table>
                <p className="px-4 py-2 text-[12px] leading-relaxed text-fg-dim">
                  {promotionIndex >= 0
                    ? `所选日期为 ${promotion.dates[promotionIndex]}；档位 = 昨日所处板数，晋级 = 今日仍涨停。高档位基数只有 1–3 只时比率会在 0/100 间反复跳，图里按样本量略去。`
                    : `所选日期 ${pool?.trade_date ?? '—'} 不在晋级率窗口（近 ${promotion.dates.length} 个交易日）内，比例显示 —。`}
                </p>
              </div>
            </>
          ) : (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              暂无晋级率数据，需要至少 2 个交易日的涨停池
            </div>
          )}
        </Panel>

        <LadderBoard
          ladder={pool?.ladder ?? []}
          total={pool?.total ?? 0}
          loading={loading}
          delay={100}
          // 选了题材时，梯队里命中的票高亮、其余淡出（与明细筛选同源）
          highlightCodes={highlightCodes}
        />

        <Panel
          title="今日题材共振"
          meta={
            <span className="num">
              {themeFilter
                ? `已筛选「${themeFilter}」，点「全部」清除`
                : `${themes?.clusters.length ?? 0} 个题材有 3 只以上涨停`}
            </span>
          }
          delay={130}
        >
          {loading ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">加载中…</div>
          ) : themes && themes.clusters.length > 0 ? (
            <div className="flex flex-wrap gap-1.5 px-3 py-3">
              <button
                type="button"
                aria-pressed={themeFilter === null}
                onClick={() => setThemeFilter(null)}
                className={[
                  'border px-2 py-[3px] text-[13px] transition-colors',
                  themeFilter === null
                    ? 'border-accent/50 text-accent'
                    : 'border-line text-fg-muted hover:text-fg',
                ].join(' ')}
              >
                全部 {stocks.length}
              </button>
              {themes.clusters.map((item) => (
                <button
                  key={item.concept}
                  type="button"
                  aria-pressed={item.concept === themeFilter}
                  onClick={() =>
                    setThemeFilter(item.concept === themeFilter ? null : item.concept)
                  }
                  className={[
                    'flex items-baseline gap-1.5 border px-2 py-[3px] text-[13px] transition-colors',
                    item.concept === themeFilter
                      ? 'border-accent/50 text-accent'
                      : 'border-line text-fg-muted hover:text-fg',
                  ].join(' ')}
                  title={`${item.concept} 今日有 ${item.count} 只涨停`}
                >
                  <span>{item.concept}</span>
                  <span className="num text-[13px] text-fg">{item.count}</span>
                  <span className={`num text-[12px] ${toneOf(item.pct_chg)}`}>
                    {fmtPct(item.pct_chg)}
                  </span>
                </button>
              ))}
            </div>
          ) : (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              该交易日没有题材数据（涨停题材随每日采集开始累积）
            </div>
          )}
        </Panel>

        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <Panel title="首封时间分布" meta={<span className="num">首封越早，资金越坚决</span>} delay={160}>
            {loading ? (
              <div className="px-4 py-10 text-center text-[14px] text-fg-dim">加载中…</div>
            ) : stocks.length === 0 ? (
              <LoadState error={error} className="px-4 py-10" />
            ) : (
              <div className="px-2 pt-2">
                <EChart option={timeOption} height={240} />
              </div>
            )}
          </Panel>

          <Panel
            title="涨停板块分布"
            meta={
              <span className="num">
                {/* 全站统称「开盘红」；这里留一次出处说明（它是开盘啦团队的新版 App） */}
                按开盘红精选板块（开盘啦团队的新版 App），未归到板块的不计入
              </span>
            }
            delay={200}
          >
            {loading ? (
              <div className="px-4 py-10 text-center text-[14px] text-fg-dim">加载中…</div>
            ) : boardRanking.length === 0 ? (
              <LoadState error={error} className="px-4 py-10" />
            ) : (
              <BoardBars items={boardRanking} total={stocks.length} />
            )}
          </Panel>
        </div>

        <LimitTable
          type="up"
          stocks={visibleStocks}
          loading={loading}
          delay={240}
          // 只有当日真有涨停时才给筛选条：没有票时它给不出任何选项
          header={stocks.length > 0 ? industryBar : undefined}
          // 被筛空不是「当日无数据」，别让这两件事混成一句话
          emptyText={
            themeFilter || industryFilter
              ? '没有同时符合题材与行业的票 —— 点上方标签清除筛选'
              : '当日无数据'
          }
        />
        <LimitTable
          type="broken"
          stocks={broken?.stocks ?? []}
          loading={loading}
          delay={280}
        />
      </div>
      )}
    </Layout>
  )
}

function Stat({
  label,
  value,
  unit,
  tone = 'text-fg',
  muted = false,
}: {
  label: string
  value: string
  unit?: string
  tone?: string
  muted?: boolean
}) {
  return (
    <div className="flex-1 bg-ink-900 px-4 py-2.5">
      <div className="text-[13px] tracking-[0.1em] text-fg-dim">{label}</div>
      <div className={`num mt-1 leading-tight ${muted ? 'text-[13px] text-fg-muted' : `text-[20px] font-semibold ${tone}`}`}>
        {value}
        {unit && <span className="ml-1 text-[13px] text-fg-dim">{unit}</span>}
      </div>
    </div>
  )
}

/** 板块分布用横向条形列表而非图表：名称较长，图表轴上放不下。 */
function BoardBars({ items, total }: { items: [string, number][]; total: number }) {
  const max = Math.max(...items.map(([, count]) => count), 1)
  return (
    // 高度要容得下全部 12 行，否则最后一行会被裁掉半截
    <div className="max-h-[320px] space-y-1 overflow-auto px-4 py-3">
      {items.map(([name, count]) => (
        <div key={name} className="flex items-center gap-2.5">
          <span className="w-16 shrink-0 truncate text-[13px] text-fg-muted" title={name}>
            {name}
          </span>
          <div className="h-[10px] flex-1 bg-ink-800">
            <div
              className="h-full"
              style={{ width: `${(count / max) * 100}%`, backgroundColor: CHART.up }}
            />
          </div>
          <span className="num w-9 shrink-0 text-right text-[13px] text-fg">{count}</span>
          <span className="num w-10 shrink-0 text-right text-[13px] text-fg-dim">
            {total > 0 ? `${Math.round((count / total) * 100)}%` : '—'}
          </span>
        </div>
      ))}
    </div>
  )
}

