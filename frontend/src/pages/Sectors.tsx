import { useCallback, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import type {
  FundFlowTaxonomy,
  RotationLeader,
  RotationMetric,
  SectorCompare,
  SectorFundFlowOut,
  SectorMembers,
  SectorQuote,
  SectorRanking,
  SectorRotation,
  SectorSeries,
  SectorTaxonomy,
} from '../api/types'
import Alert from '../components/Alert'
import EChart from '../components/EChart'
import type { ChartOption } from '../components/EChart'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import SectorFlowPanel from '../components/SectorFlowPanel'
import SectorRotationPanel, { ROTATION_SPANS } from '../components/SectorRotationPanel'
import SortTh from '../components/SortTh'
import StockLink from '../components/StockLink'
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
import { fmtAmount, fmtInt, fmtNum, fmtPct, fmtShortDate, toneOf } from '../lib/format'
import { useSort } from '../lib/sort'
import type { SortSpecs } from '../lib/sort'
import { rememberStockList } from '../lib/stockNav'

const TAXONOMIES: { key: SectorTaxonomy; label: string }[] = [
  { key: 'kph_selected', label: '精选板块' },
  { key: 'kph_industry', label: '行业板块' },
]

/**
 * 板块列表的汇总数字。**与顺序无关**，所以点列头排序不会让这几个数变化。
 *
 * `values` 是已经滤掉 null 的涨跌幅 —— 「数据源还没更新」的那些板块不能按 0 计入，
 * 否则平均涨跌幅会被拉低，而它不是真的跌了。
 */
function summaryOf(values: number[], boards: SectorQuote[]) {
  const rising = values.filter((value) => value > 0).length
  const falling = values.filter((value) => value < 0).length
  const average = values.length
    ? values.reduce((sum, value) => sum + value, 0) / values.length
    : null
  const amount = boards.reduce((sum, board) => sum + (board.amount ?? 0), 0)
  const limitUps = boards.reduce((sum, board) => sum + (board.limit_up_count ?? 0), 0)
  return { rising, falling, average, amount: amount || null, limitUps }
}

/** 板块排行各列的排序口径。
 *
 *  换开盘红之后只剩这几列：净流入 / 涨跌家数 / 领涨股在开盘红的板块行里没有可
 *  反解的对应列（见 `sources/kaipanhong.py` 顶部），两个口径都恒为空，所以
 *  那三列已经从表里删掉了 —— 留着一整列「—」只是噪音。 */
const BOARD_SORTS: SortSpecs<SectorQuote> = {
  name: { value: (board) => board.name, first: 'asc' },
  pct_chg: { value: (board) => board.pct_chg },
  limit_up_count: { value: (board) => board.limit_up_count },
  pct_chg_5d: { value: (board) => board.pct_chg_5d },
  amount: { value: (board) => board.amount },
}

/**
 * 对比线数上限，与后端 `COMPARE_LIMIT` 对齐。
 *
 * 这个数必须与后端一致：前端放得比后端宽，用户会先挑满再吃一个 400；
 * 放得比后端窄，后端的能力就白写了。`SERIES_PALETTE` 的颜色数要 ≥ 它，
 * 否则线条会绕回去和前面的同色。
 */
const COMPARE_MAX = 8

/**
 * 走势图 / 对比图的窗口档位。板块历史在 2026-09-21 深回补后到了 252 天，
 * 所以这里能开到 250（后端 `CURVE_MAX_DAYS` 与它对齐）。
 */
const CURVE_SPANS = [30, 60, 120, 250]

export default function Sectors() {
  /**
   * 视图状态挂在 URL 上。
   *
   * 点成分股进个股页再后退时，这一页的组件是**重新挂载**的（不是整页刷新，
   * 但 state 一样会归零）。不写进 URL 的话，回来一律退回「第一个板块 +
   * 最新日期」，前面选的板块就白选了。
   */
  const [params, setParams] = useSearchParams()
  const [taxonomy, setTaxonomy] = useState<SectorTaxonomy>(() =>
    params.get('taxonomy') === 'kph_industry' ? 'kph_industry' : 'kph_selected',
  )
  const [date, setDate] = useState<string | null>(() => params.get('date'))
  const [dates, setDates] = useState<string[]>([])
  const [ranking, setRanking] = useState<SectorRanking | null>(null)
  const [selected, setSelected] = useState<string | null>(() => params.get('code'))
  const [series, setSeries] = useState<SectorSeries | null>(null)
  const [seriesLoading, setSeriesLoading] = useState(false)
  const [seriesError, setSeriesError] = useState<string | null>(null)
  const [members, setMembers] = useState<SectorMembers | null>(null)
  const [membersLoading, setMembersLoading] = useState(false)
  const [compareCodes, setCompareCodes] = useState<string[]>([])
  const [compare, setCompare] = useState<SectorCompare | null>(null)
  const [compareLoading, setCompareLoading] = useState(false)
  const [compareError, setCompareError] = useState<string | null>(null)
  // 轮动矩阵的指标与周期也写进 URL：和上面那些视图状态同理，
  // 点进个股再后退时不该把选好的「强度 / 近 30 日」退回默认值。
  // 默认「强度」= 开盘啦 App 那张板块榜的口径（按成交额排是另一张榜）
  const [rotationMetric, setRotationMetric] = useState<RotationMetric>(() => {
    // URL 可以手改，先校验取值再采信
    const raw = params.get('rot')
    return raw === 'pct_chg' || raw === 'amount' ? raw : 'strength'
  })
  const [rotationDays, setRotationDays] = useState(() => {
    const raw = Number(params.get('rotdays'))
    return ROTATION_SPANS.includes(raw) ? raw : 20
  })
  // 上榜次数折线图的窗口，**与矩阵窗口独立**（矩阵看 20 天、上榜看 50 天是常见用法，
  // 开盘啦那张图也是独立档位）。同样走 URL、同样先校验档位
  const [ladderDays, setLadderDays] = useState(() => {
    const raw = Number(params.get('ladder'))
    return ROTATION_SPANS.includes(raw) ? raw : 20
  })
  const [rotation, setRotation] = useState<SectorRotation | null>(null)
  const [rotationLoading, setRotationLoading] = useState(true)
  /**
   * 「领涨」行的数据：`{交易日: 涨停股}`，取的是**当前选中板块**在各天的涨停股。
   *
   * 原来这一行是「每列各自取当天第 1 名板块」，用户点格子时它不动、看着像坏了
   * （2026-09-22 改）。现在跟着 `selected` 走，所以**单独发一个请求**而不是塞进
   * `rotation`：点一次格子就要重取一次，混在一起会让整个矩阵每点一下都进 loading。
   */
  const [leaders, setLeaders] = useState<Record<string, RotationLeader[]>>({})
  /**
   * 资金流向面板的口径。**与上面的 `taxonomy` 故意分开存** ——
   * 那个是开盘红的板块口径（精选/行业），这个是同花顺的概念/行业，两套名字对不上，
   * 共用一个 state 会让人以为切上面那个就能换资金流的口径。
   */
  const [flowTaxonomy, setFlowTaxonomy] = useState<FundFlowTaxonomy>(() =>
    params.get('flow') === 'ths_industry' ? 'ths_industry' : 'ths_concept',
  )
  const [flow, setFlow] = useState<SectorFundFlowOut | null>(null)
  const [flowLoading, setFlowLoading] = useState(true)
  // 走势窗口也写进 URL，理由同上。初值先校验档位：URL 是可以手改的，
  // 塞个 10000 进来后端会直接 400，页面看起来就像坏了
  const [curveDays, setCurveDays] = useState(() => {
    const raw = Number(params.get('curve'))
    return CURVE_SPANS.includes(raw) ? raw : 30
  })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // 把当前视图写回 URL。用 replace 而不是 push —— 否则每点一个板块都会往
  // 历史里塞一条，想退出这一页得连按十几次返回
  useEffect(() => {
    const next = new URLSearchParams(params)
    if (taxonomy === 'kph_selected') next.delete('taxonomy')
    else next.set('taxonomy', taxonomy)
    if (date) next.set('date', date)
    else next.delete('date')
    if (selected) next.set('code', selected)
    else next.delete('code')
    if (rotationMetric === 'strength') next.delete('rot')
    else next.set('rot', rotationMetric)
    if (rotationDays === 20) next.delete('rotdays')
    else next.set('rotdays', String(rotationDays))
    if (ladderDays === 20) next.delete('ladder')
    else next.set('ladder', String(ladderDays))
    if (curveDays === 30) next.delete('curve')
    else next.set('curve', String(curveDays))
    // 资金流面板的口径也是视图状态，同样不该在返回时被重置
    if (flowTaxonomy === 'ths_concept') next.delete('flow')
    else next.set('flow', flowTaxonomy)
    if (next.toString() !== params.toString()) setParams(next, { replace: true })
  }, [
    taxonomy,
    date,
    selected,
    rotationMetric,
    rotationDays,
    ladderDays,
    curveDays,
    flowTaxonomy,
    params,
    setParams,
  ])

  useEffect(() => {
    api.dates().then(setDates).catch(() => setDates([]))
  }, [])

  useEffect(() => {
    let stale = false
    setRotationLoading(true)
    api
      // top 固定 10：跟短线侠那页一样，看轮动 10 行足够，再多一屏放不下，
      // 也就没必要做成可选项。
      //
      // days 取**矩阵窗口与上榜窗口里较大的那个**：矩阵只需要 `rotationDays` 列、
      // 折线图只需要 `ladderDays` 列，多要几列不额外花请求（一次返回一个区间的所有
      // 交易日），比按两个窗口各发一次省。多出来的列在组件里切掉。
      .sectorRotation(
        taxonomy,
        { days: Math.max(rotationDays, ladderDays), top: 10, metric: rotationMetric },
        date,
      )
      .then((data) => {
        if (!stale) setRotation(data)
      })
      .catch((err: Error) => {
        // 失败要显式报出来：面板里那句「本地还没有板块历史」是给真的没数据准备的，
        // 拿它顶替错误会被读成「板块历史就是空的」
        if (!stale) {
          setRotation(null)
          setError(err.message)
        }
      })
      .finally(() => {
        if (!stale) setRotationLoading(false)
      })
    return () => {
      stale = true
    }
  }, [taxonomy, date, rotationDays, ladderDays, rotationMetric])

  // 「领涨」行：跟着选中的板块走（见 `leaders` 的说明）。
  // 窗口用**矩阵窗口**而不是上榜窗口 —— 这一行是矩阵的一部分，列要对齐
  useEffect(() => {
    if (!selected) {
      setLeaders({})
      return
    }
    let stale = false
    api
      .sectorRotationLeaders(taxonomy, { days: rotationDays, code: selected }, date)
      .then((data) => {
        if (stale) return
        setLeaders(
          Object.fromEntries(data.map((item) => [item.trade_date, item.leaders])),
        )
      })
      .catch(() => {
        // 这是次级信息，取不到就留空：**不弹错**。矩阵与详情都还在，
        // 为一行龙头股把整页标红会让人以为板块数据也挂了
        if (!stale) setLeaders({})
      })
    return () => {
      stale = true
    }
  }, [taxonomy, date, rotationDays, selected])

  // 资金流向面板：只跟日期与它自己的口径走。失败照旧显式报错 ——
  // 面板里那句空态是给「这天真的没人采」准备的，拿它顶替错误会被读成「就是没有」
  useEffect(() => {
    let stale = false
    setFlowLoading(true)
    api
      .sectorFundFlow(flowTaxonomy, date)
      .then((data) => {
        if (!stale) setFlow(data)
      })
      .catch((err: Error) => {
        if (!stale) {
          setFlow(null)
          setError(err.message)
        }
      })
      .finally(() => {
        if (!stale) setFlowLoading(false)
      })
    return () => {
      stale = true
    }
  }, [flowTaxonomy, date])

  useEffect(() => {
    let stale = false
    setLoading(true)
    setError(null)
    api
      .sectorRanking(taxonomy, date)
      .then((data) => {
        if (stale) return
        setRanking(data)
        // 默认选中榜首，右侧详情不至于空着。URL 里带了 code 就不动它 ——
        // 否则从个股页后退回来会被强行改回第一个板块
        setSelected((current) => current ?? data.boards[0]?.code ?? null)
      })
      .catch((err: Error) => {
        if (stale) return
        setRanking(null)
        setSelected(null)
        setError(err.message)
      })
      .finally(() => {
        if (!stale) setLoading(false)
      })
    // 快速切换「精选/行业」或日期时，慢的旧请求可能后返回、用旧数据覆盖新数据，
    // 所以这里和下面 series/members/compare 一样加 stale 守卫
    return () => {
      stale = true
    }
  }, [taxonomy, date])

  useEffect(() => {
    if (!selected) {
      setSeries(null)
      setSeriesLoading(false)
      setSeriesError(null)
      return
    }
    let stale = false
    setSeriesLoading(true)
    setSeriesError(null)
    api
      // 带上所选日期：图与板块排行必须看的是同一天，否则选了历史日期时
      // 「表是那天的、图却到今天」，两个数字对不上还找不到原因
      .sectorSeries(selected, curveDays, date)
      .then((data) => {
        if (!stale) setSeries(data)
      })
      .catch((err: Error) => {
        // 失败要显式写出来。面板里默认那句「暂无历史数据」是给「这个板块
        // 本来就没几天数据」准备的，拿它顶替错误会被读成「数据源没给数据」
        if (!stale) {
          setSeries(null)
          setSeriesError(err.message)
        }
      })
      .finally(() => {
        if (!stale) setSeriesLoading(false)
      })
    return () => {
      stale = true
    }
  }, [selected, date, curveDays])

  useEffect(() => {
    if (!selected) {
      setMembers(null)
      return
    }
    let stale = false
    setMembersLoading(true)
    setMembers(null)
    api
      .sectorMembers(selected, date)
      .then((data) => {
        if (!stale) setMembers(data)
      })
      .catch((err: Error) => {
        if (!stale) setError(err.message)
      })
      .finally(() => {
        if (!stale) setMembersLoading(false)
      })
    return () => {
      stale = true
    }
  }, [selected, date])

  useEffect(() => {
    if (compareCodes.length === 0) {
      setCompare(null)
      setCompareLoading(false)
      setCompareError(null)
      return
    }
    let stale = false
    setCompareLoading(true)
    setCompareError(null)
    api
      .sectorCompare(compareCodes, curveDays, date)
      .then((data) => {
        if (!stale) setCompare(data)
      })
      .catch((err: Error) => {
        if (!stale) {
          setCompare(null)
          setCompareError(err.message)
        }
      })
      .finally(() => {
        if (!stale) setCompareLoading(false)
      })
    return () => {
      stale = true
    }
  }, [compareCodes, date, curveDays])

  // 排序下沉到 BoardTable（点列头）—— 这里只做「拿到的原始列表」，
  // 下面几个统计量（涨跌家数、成交额合计）都与顺序无关，不受影响
  const boards = ranking?.boards ?? []

  // 板块统计量：涨跌家数、平均涨跌幅、成交额合计。
  // **不 memo**：`boards` 来自 `ranking?.boards ?? []`，ranking 为空时那个 `[]`
  // 每次渲染都是新数组，memo 的依赖永远失效（lint 也会报）。
  // 板块最多 375 个，这几趟遍历不到 0.1ms，直接算更清楚
  const values = boards.map((board) => board.pct_chg).filter((v): v is number => v !== null)
  const summary = summaryOf(values, boards)

  const selectedBoard = boards.find((board) => board.code === selected) ?? null
  const inCompare = selected != null && compareCodes.includes(selected)

  const onSelect = useCallback((code: string) => setSelected(code), [])

  const switchTaxonomy = useCallback((next: SectorTaxonomy) => {
    setTaxonomy(next)
    // 板块代码属于各自的分类，跨分类对比没有意义；选中的板块也要清掉，
    // 否则会停在上一个分类的代码上，右侧显示「点下方板块排行里的板块」
    setCompareCodes([])
    setSelected(null)
  }, [])

  const toggleCompare = useCallback(
    (board: SectorQuote) => {
      if (compareCodes.includes(board.code)) {
        setCompareCodes(compareCodes.filter((code) => code !== board.code))
        return
      }
      if (compareCodes.length >= COMPARE_MAX) {
        setError(`最多同时对比 ${COMPARE_MAX} 个板块`)
        return
      }
      setCompareCodes([...compareCodes, board.code])
    },
    [compareCodes],
  )

  const toolbar = (
    <>
      <div className="flex items-stretch border border-line">
        {TAXONOMIES.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => switchTaxonomy(item.key)}
            className={[
              'px-2.5 py-[3px] text-[12px] transition-colors',
              taxonomy === item.key ? 'bg-ink-700 text-fg' : 'text-fg-muted hover:text-fg',
            ].join(' ')}
          >
            {item.label}
          </button>
        ))}
      </div>
      <select
        value={date ?? ''}
        onChange={(event) => setDate(event.target.value || null)}
        className="num border border-line bg-ink-900 px-2 py-[3px] text-[12px] text-fg outline-none focus:border-fg-dim"
      >
        <option value="">最新</option>
        {/* 倒序渲染：最近的排最上面。原先是升序，展开后要一路滚到底才够得着昨天 */}
        {[...dates].reverse().map((item) => (
          <option key={item} value={item}>
            {item}
          </option>
        ))}
      </select>
    </>
  )

  return (
    <Layout toolbar={toolbar}>
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}

      {ranking && ranking.missing > 0 && (
        <Alert tone="accent">
          {`${ranking.missing} 个板块当日数据源尚未更新，涨跌幅显示为「—」而不是 0。`}
        </Alert>
      )}

      <div className="space-y-4">
        <div className="flex flex-wrap items-stretch gap-px border border-line-soft bg-line-soft">
          <Stat label="板块总数" value={fmtInt(ranking?.total ?? null)} unit="个" />
          <Stat label="上涨板块" value={fmtInt(summary.rising)} unit="个" tone="text-up" />
          <Stat label="下跌板块" value={fmtInt(summary.falling)} unit="个" tone="text-down" />
          <Stat label="平均涨跌幅" value={fmtPct(summary.average)} tone={toneOf(summary.average)} />
          <Stat label="板块总成交额" value={fmtAmount(summary.amount)} />
          <Stat
            label={taxonomy === 'kph_selected' ? '涨停股命中板块' : '口径'}
            value={taxonomy === 'kph_selected' ? fmtInt(summary.limitUps) : '开盘红板块行情'}
            unit={taxonomy === 'kph_selected' ? '次' : undefined}
            muted={taxonomy !== 'kph_selected'}
          />
        </div>

        <Panel
          title="板块轮动"
          meta={
            <span className="num">
              {rotation ? `${rotation.metric_label} · 每天前 ${rotation.top} 名` : '—'}
            </span>
          }
          delay={60}
        >
          <SectorRotationPanel
            data={rotation}
            loading={rotationLoading}
            metric={rotationMetric}
            taxonomy={taxonomy}
            days={rotationDays}
            ladderDays={ladderDays}
            leaders={leaders}
            leaderName={selectedBoard?.name ?? null}
            onMetric={setRotationMetric}
            onDays={setRotationDays}
            onLadderDays={setLadderDays}
            onSelect={onSelect}
          />
        </Panel>

        <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_400px]">
          <Panel
            title="板块成分股"
            meta={
              <span className="num">
                {members
                  ? `${members.members.length}${
                      members.member_count ? ` / ${members.member_count}` : ''
                    } 只 · 按涨跌幅降序`
                  : '—'}
              </span>
            }
            delay={40}
          >
            {membersLoading ? (
              <div className="px-4 py-10 text-center text-[13px] text-fg-dim">
                取数中…（该板块第一次打开时要向开盘红现取）
              </div>
            ) : members && members.members.length > 0 ? (
              <MemberTable members={members.members} />
            ) : (
              <div className="px-4 py-10 text-center text-[13px] text-fg-dim">
                {members?.note
                  ? members.note
                  : selected
                    ? '该板块暂无成分股数据'
                    : '选中板块后显示成分股'}
              </div>
            )}
          </Panel>

          <div className="space-y-4">
            <Panel
              title={selectedBoard?.name ?? '板块详情'}
              meta={
                <span className="num">
                  {selectedBoard ? selectedBoard.code : '点下方板块排行里的板块'}
                </span>
              }
              delay={80}
            >
              {selectedBoard ? (
                <>
                  <div className="grid grid-cols-2 gap-px bg-line-soft">
                    <Cell
                      label="今日涨跌幅"
                      value={fmtPct(selectedBoard.pct_chg)}
                      tone={toneOf(selectedBoard.pct_chg)}
                    />
                    <Cell
                      label="近 5 日"
                      value={fmtPct(selectedBoard.pct_chg_5d)}
                      tone={toneOf(selectedBoard.pct_chg_5d)}
                    />
                    <Cell label="成交额" value={fmtAmount(selectedBoard.amount)} />
                    <Cell
                      label="今日涨停"
                      value={
                        selectedBoard.limit_up_count === null
                          ? '—'
                          : `${fmtInt(selectedBoard.limit_up_count)} 只`
                      }
                      tone={selectedBoard.limit_up_count ? 'text-up' : undefined}
                    />
                  </div>
                  <div className="flex items-center justify-between gap-2 border-t border-line-soft px-3 py-2">
                    <span className="text-[11px] text-fg-dim">
                      {taxonomy === 'kph_selected'
                        ? '涨停数按「个股 → 开盘红精选板块」归属统计（来自涨停天梯）'
                        : '行业口径没有涨停归属（涨停天梯只给精选板块）'}
                    </span>
                    <button
                      type="button"
                      onClick={() => toggleCompare(selectedBoard)}
                      className={[
                        'shrink-0 border px-2 py-[3px] text-[11px] transition-colors',
                        inCompare
                          ? 'border-accent/50 text-accent'
                          : 'border-line text-fg-muted hover:text-fg',
                      ].join(' ')}
                    >
                      {inCompare ? '✓ 已在对比' : '＋ 加入对比'}
                    </button>
                  </div>
                </>
              ) : (
                <div className="px-4 py-10 text-center text-[13px] text-fg-dim">
                  点下方板块排行里的板块查看详情
                </div>
              )}
            </Panel>

            <Panel
              title="板块近期表现"
              meta={
                <span className="flex flex-wrap items-center justify-end gap-x-3 gap-y-1">
                  <span className="num">
                    {series ? `${series.dates.length} 个交易日` : '—'}
                  </span>
                  {/* 走势窗口**从顶栏挪到这里**（2026-09-21）。它只管下面这两张走势图，
                      和「口径 / 日期」那种全局切换不是一类东西；留在顶栏的结果是
                      1280px 下导航被挤到只显示 7/9 项（账见设计文档 8.33）。
                      仍然只保留**这一个**控件：对比图那边只显示当前窗口值。
                      给两处各配一个选择器的话，改了上面那个下面跟着变，反而像坏了。 */}
                  <select
                    value={curveDays}
                    onChange={(event) => setCurveDays(Number(event.target.value))}
                    className="num border border-line bg-ink-900 px-2 py-[2px] text-[11px] text-fg outline-none focus:border-fg-dim"
                    title="走势图与对比图共用的窗口长度"
                  >
                    {CURVE_SPANS.map((span) => (
                      <option key={span} value={span}>
                        走势 {span} 日
                      </option>
                    ))}
                  </select>
                </span>
              }
              delay={120}
            >
              {seriesLoading ? (
                <div className="px-4 py-10 text-center text-[13px] text-fg-dim">加载中…</div>
              ) : seriesError ? (
                <div className="px-4 py-10 text-center text-[13px] text-danger">
                  {seriesError}
                </div>
              ) : series && series.dates.length > 0 ? (
                <div className="px-2 pt-2">
                  <EChart option={buildSeriesOption(series)} height={220} />
                </div>
              ) : (
                <div className="px-4 py-10 text-center text-[13px] text-fg-dim">
                  暂无历史数据，可在「数据管理」里回补板块历史
                </div>
              )}
            </Panel>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <Panel
            title={taxonomy === 'kph_selected' ? '精选板块排行' : '行业板块排行'}
            meta={
              <span className="num">
                {loading ? '加载中…' : `${boards.length} 个 · 点列头排序`}
              </span>
            }
            delay={160}
          >
            {loading ? (
              <div className="px-4 py-10 text-center text-[13px] text-fg-dim">加载中…</div>
            ) : boards.length === 0 ? (
              <div className="px-4 py-10 text-center text-[13px] text-fg-dim">
                暂无板块数据，请先在「数据管理」中执行采集
              </div>
            ) : (
              <BoardTable
                // key 挂 taxonomy：切换精选/行业时整块重建，
                // 否则会带着上一类的排序列过来（比如按「净流入」排精选，
                // 而精选没有这个字段，整列会沉底，看起来像没排）
                key={taxonomy}
                boards={boards}
                withLimitUp={taxonomy === 'kph_selected'}
                selected={selected}
                compareCodes={compareCodes}
                onSelect={onSelect}
              />
            )}
          </Panel>

          <Panel
            title="多板块强弱对比"
            meta={
              <span className="num">
                {compareCodes.length > 0
                  ? // 只显示当前窗口、不在这儿放第二个选择器：它与上面走势图共用同一个值，
                    // 放两个的话改一个另一个跟着变，用起来像坏了
                    `${compareCodes.length} 条 · 起点归一为 100 · 窗口 ${curveDays} 日`
                  : '用上方「板块详情」的「＋ 加入对比」'}
              </span>
            }
            delay={200}
          >
            {compareLoading ? (
              <div className="px-4 py-10 text-center text-[13px] text-fg-dim">加载中…</div>
            ) : compareError ? (
              <div className="px-4 py-10 text-center text-[13px] text-danger">
                {compareError}
              </div>
            ) : compare && compare.series.length > 0 ? (
              <div className="px-2 pt-2">
                <EChart option={buildCompareOption(compare)} height={280} />
                <div className="flex flex-wrap gap-1.5 px-2 pb-2 pt-1">
                  {compare.series.map((item) => (
                    <button
                      key={item.code}
                      type="button"
                      onClick={() =>
                        setCompareCodes((current) =>
                          current.filter((code) => code !== item.code),
                        )
                      }
                      className="border border-line px-2 py-[2px] text-[11px] text-fg-muted transition-colors hover:border-danger/40 hover:text-fg"
                    >
                      {item.name} ✕
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              <div className="px-4 py-10 text-center text-[13px] text-fg-dim">
                选中的板块走势叠在一张图上比较强弱，
                纵轴是「起点 = 100」的净值，只反映相对强弱
              </div>
            )}
          </Panel>
        </div>

        {/* 放最后一块、整行宽：它是**另一种口径**（同花顺概念/行业），
            与上面的开盘红板块对不上，所以不和上面任何面板并排，
            免得被读成「同一个板块的两组数」 */}
        <Panel
          title="板块资金流向"
          meta={
            <span className="num">
              {flowLoading ? '加载中…' : `${flow?.taxonomy_label ?? ''} · 单位亿元`}
            </span>
          }
          delay={200}
        >
          <SectorFlowPanel
            data={flow}
            loading={flowLoading}
            taxonomy={flowTaxonomy}
            onTaxonomy={setFlowTaxonomy}
          />
        </Panel>
      </div>
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
      <div className="text-[11px] tracking-[0.1em] text-fg-dim">{label}</div>
      <div
        className={`num mt-1 leading-tight ${
          muted ? 'text-[12px] text-fg-muted' : `text-[19px] font-medium ${tone}`
        }`}
      >
        {value}
        {unit && <span className="ml-1 text-[11px] text-fg-dim">{unit}</span>}
      </div>
    </div>
  )
}

function Cell({
  label,
  value,
  tone = 'text-fg',
}: {
  label: string
  value: string
  tone?: string
}) {
  return (
    <div className="bg-ink-900 px-3 py-2">
      <div className="text-[11px] text-fg-dim">{label}</div>
      <div className={`num mt-0.5 truncate text-[13px] ${tone}`} title={value}>
        {value}
      </div>
    </div>
  )
}

function BoardTable({
  boards,
  withLimitUp,
  selected,
  compareCodes,
  onSelect,
}: {
  boards: SectorQuote[]
  withLimitUp: boolean
  selected: string | null
  compareCodes: string[]
  onSelect: (code: string) => void
}) {
  // 默认按涨跌幅降序 —— 这是「今天什么在涨」最直接的答案，
  // 也与后端返回的顺序一致，所以首屏看到的和以前完全一样
  const [sort, shown] = useSort(boards, BOARD_SORTS, { key: 'pct_chg' })

  return (
    <div className="max-h-[760px] overflow-auto">
      <table className="grid-table">
        <thead>
          <tr>
            <SortTh sortKey="name" align="left" {...sort}>
              板块
            </SortTh>
            <SortTh sortKey="pct_chg" {...sort}>
              涨跌幅
            </SortTh>
            {withLimitUp && (
              <SortTh sortKey="limit_up_count" {...sort}>
                涨停
              </SortTh>
            )}
            <SortTh sortKey="pct_chg_5d" {...sort}>
              近 5 日
            </SortTh>
            <SortTh sortKey="amount" {...sort}>
              成交额
            </SortTh>
          </tr>
        </thead>
        <tbody>
          {shown.map((board) => (
            <tr
              key={board.code}
              onClick={() => onSelect(board.code)}
              className={`cursor-pointer ${selected === board.code ? 'bg-ink-700' : ''}`}
            >
              <td className="!text-left">
                <span className="text-fg">{board.name}</span>
                {compareCodes.includes(board.code) && (
                  <span className="ml-1.5 text-[10px] text-accent">对比中</span>
                )}
              </td>
              <td>
                <span className={`num ${toneOf(board.pct_chg)}`}>{fmtPct(board.pct_chg)}</span>
              </td>
              {withLimitUp && (
                <td>
                  {/*
                    0 与「取不到」必须分开显示：精选口径下 270 个板块里有 9 成
                    当天就是 0 家涨停（事实），只有拿到 null 才是缺数据。
                    一律画成「—」会把「这个板块今天没有涨停股」读成「不知道」，
                    也跟右边详情卡的「0 只」自相矛盾。
                  */}
                  <span
                    className={`num ${
                      board.limit_up_count ? 'text-up' : 'text-fg-dim'
                    }`}
                  >
                    {board.limit_up_count === null ? '—' : (board.limit_up_count ?? 0)}
                  </span>
                </td>
              )}
              <td>
                <span className={`num ${toneOf(board.pct_chg_5d)}`}>
                  {fmtPct(board.pct_chg_5d)}
                </span>
              </td>
              <td>
                <span className="num text-fg-muted">{fmtAmount(board.amount)}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** 板块成分股各列的排序口径。连板为空 = 当天没涨停，按 0 参与比较 */
const MEMBER_SORTS: SortSpecs<SectorMembers['members'][number]> = {
  code: { value: (stock) => stock.code, first: 'asc' },
  name: { value: (stock) => stock.name, first: 'asc' },
  pct_chg: { value: (stock) => stock.pct_chg },
  close: { value: (stock) => stock.close },
  amount: { value: (stock) => stock.amount },
  turnover: { value: (stock) => stock.turnover },
  consecutive: { value: (stock) => stock.consecutive ?? 0 },
}

function MemberTable({ members }: { members: SectorMembers['members'] }) {
  // 首屏不排，保持后端顺序（已按涨跌幅降序）；点列头才排
  const [sort, shown] = useSort(members, MEMBER_SORTS, { key: null })

  return (
    <div className="max-h-[420px] overflow-auto">
      <table className="grid-table">
        <thead>
          <tr>
            <SortTh sortKey="code" {...sort}>代码</SortTh>
            <SortTh sortKey="name" align="left" {...sort}>名称</SortTh>
            <SortTh sortKey="pct_chg" {...sort}>涨跌幅</SortTh>
            <SortTh sortKey="close" {...sort}>最新价</SortTh>
            <SortTh sortKey="amount" {...sort}>成交额</SortTh>
            <SortTh sortKey="turnover" {...sort}>换手率</SortTh>
            <SortTh sortKey="consecutive" {...sort}>连板</SortTh>
          </tr>
        </thead>
        <tbody>
          {shown.map((stock) => (
            <tr
              key={stock.code}
              onClick={() => {
                // 点行时把当前板块成分股存下，个股页就能 ← → 前后翻
                rememberStockList(shown.map((item) => item.code))
              }}
            >
              <td className="!text-left">
                {/* 代码与名称都是入口：用户读的是名称，只有代码可点的话，
                    点名称会「什么都不发生」，看着就像页面上没有日K可看 */}
                <StockLink code={stock.code} className="num text-fg-muted">
                  {stock.code}
                </StockLink>
              </td>
              <td className="!text-left">
                <StockLink code={stock.code}>{stock.name ?? stock.code}</StockLink>
              </td>
              <td>
                <span className={`num ${toneOf(stock.pct_chg)}`}>
                  {fmtPct(stock.pct_chg)}
                </span>
              </td>
              <td>
                <span className="num">{fmtNum(stock.close, 2)}</span>
              </td>
              <td>
                <span className="num text-fg-muted">{fmtAmount(stock.amount)}</span>
              </td>
              <td>
                <span className="num text-fg-muted">
                  {fmtNum(stock.turnover, 2, '%')}
                </span>
              </td>
              <td>
                {stock.consecutive ? (
                  <span
                    className={`num ${stock.consecutive >= 3 ? 'text-accent' : 'text-up'}`}
                  >
                    {stock.consecutive} 板
                  </span>
                ) : (
                  <span className="num text-fg-dim">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * 板块指数接口只给收盘价，库里存的是逐日涨跌幅，所以详情图用**逐日涨跌幅柱状图**
 * 而不是走势线 —— 从任意起点复利出来的「指数」纵轴没有真实含义，不如直接看每天涨跌。
 */
function buildSeriesOption(series: SectorSeries): ChartOption {
  return {
    grid: { ...GRID, top: 18, bottom: 4 },
    tooltip: {
      ...TOOLTIP,
      trigger: 'axis' as const,
      axisPointer: { type: 'shadow' as const },
      // ECharts 对空值点传给 formatter 的是 '-'，不是 null，都要归成占位符
      formatter: (params: unknown) => {
        const items = params as { dataIndex: number }[]
        const index = items?.[0]?.dataIndex ?? 0
        const pct = series.pct_chg[index]
        const amount = series.amount[index]
        return [
          `${series.dates[index]}`,
          `涨跌幅 ${pct == null ? '—' : fmtPct(pct)}`,
          `成交额 ${amount == null ? '—' : fmtAmount(amount)}`,
        ].join('<br/>')
      },
    },
    xAxis: {
      type: 'category' as const,
      data: series.dates.map(fmtShortDate),
      axisLabel: { ...AXIS_LABEL, interval: Math.ceil(series.dates.length / 8) },
      axisLine: AXIS_LINE,
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value' as const,
      axisLabel: { ...AXIS_LABEL, formatter: '{value}%' },
      splitLine: SPLIT_LINE,
      axisLine: { show: false },
    },
    series: [
      {
        type: 'bar' as const,
        name: '涨跌幅',
        barMaxWidth: 16,
        data: series.pct_chg.map((value) => ({
          value,
          itemStyle: { color: value != null && value < 0 ? CHART.down : CHART.up },
        })),
      },
    ],
  }
}

/** 对比图：多条净值曲线。纵轴只能看相对强弱，所以 tooltip 直接给相对起点的涨跌幅。 */
function buildCompareOption(data: SectorCompare): ChartOption {
  const series = data.series.map((item, index) => {
    const color = SERIES_PALETTE[index % SERIES_PALETTE.length]
    return {
      type: 'line' as const,
      name: item.name,
      data: item.values,
      symbol: 'none',
      // 缺值不插值：链路断开比连出一条假线诚实
      connectNulls: false,
      lineStyle: { width: 1.6, color },
      itemStyle: { color },
    }
  })

  return {
    grid: { ...GRID, top: 44, bottom: 4 },
    legend: { ...LEGEND, top: 2, data: series.map((item) => item.name) },
    tooltip: {
      ...TOOLTIP,
      trigger: 'axis' as const,
      axisPointer: { type: 'line' as const, lineStyle: { color: CHART.fgDim, type: 'dashed' as const } },
      formatter: (params: unknown) => {
        const items = params as { dataIndex: number; seriesName: string; value?: unknown; marker?: string }[]
        if (!items?.length) return ''
        const lines = items.map((item) => {
          const value = typeof item.value === 'number' ? item.value : null
          return `${item.marker ?? ''}${item.seriesName} ${
            value == null ? '—' : fmtPct(value - data.base)
          }`
        })
        return [`${data.dates[items[0].dataIndex]}`, ...lines].join('<br/>')
      },
    },
    xAxis: {
      type: 'category' as const,
      data: data.dates.map(fmtShortDate),
      axisLabel: { ...AXIS_LABEL, interval: Math.ceil(data.dates.length / 8) },
      axisLine: AXIS_LINE,
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value' as const,
      scale: true,
      axisLabel: AXIS_LABEL,
      splitLine: SPLIT_LINE,
      axisLine: { show: false },
    },
    series,
  }
}
