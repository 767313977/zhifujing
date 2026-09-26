import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client'
import type { StockDde, StockDdeRow, StockProfile, StockThemes } from '../api/types'
import Alert from '../components/Alert'
import EChart from '../components/EChart'
import type { ChartOption } from '../components/EChart'
import KLineChart from '../components/KLineChart'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import Segmented from '../components/Segmented'
import {
  AXIS_LABEL,
  AXIS_LINE,
  CHART,
  GRID,
  SPLIT_LINE,
  TOOLTIP,
} from '../lib/chart'
import { fmtAmount, fmtNum, fmtPct, fmtShortDate, toneOf } from '../lib/format'
import { K_VIEWS, useKLine } from '../lib/klinePeriod'
import { alreadySynced, markSynced, readStockList } from '../lib/stockNav'

/**
 * 少于这么多根 K 就认为「历史不全」，打开时补一次。
 *
 * 不能只看 `day_count === 0`：池子是按流动性筛的，而**池外的票（不少涨停股都在
 * 池外）是逐日攒起来的** —— 它们库里有几根 K，但画不出均线和形态，
 * 正好被 `=== 0` 这条判据漏掉。
 */
const NEED_BARS = 120

/** K 线面板的标题按周期变 */
const VIEW_LABEL: Record<string, string> = {
  day: '日 K',
  week: '周 K',
  month: '月 K',
}

const VIEW_UNIT: Record<string, string> = {
  // 2026-09-26 起跌色是青不是绿（全站改同花顺配色，见设计文档 8.68）——
  // 这行文案就贴在 K 线标题栏上，说「绿跌」而图上是青跌，是自相矛盾
  day: '不复权真实价，红涨青跌',
  week: '不复权真实价 · 由日线按 ISO 周聚合',
  month: '不复权真实价 · 由日线按月聚合',
}

/**
 * DDE 一栏的图：**柱 = 主力净流入额**（逐条上色，正红负绿）、**线 = 5日DDE**。
 *
 * 两个指标的单位都是元、量级又相近（都是亿元上下），所以共用一根 Y 轴；
 * 柱子的红绿只表示**流入还是流出**，与「净流入额比前一天多了还是少了」无关。
 *
 * 缺值（来源没给那一天）一律留空：柱不画、线断开，不补 0、也不插值。
 */
function buildDdeOption(rows: StockDdeRow[]): ChartOption {
  return {
    // **不画图例**：柱子是逐条上色的（净流入红、净流出绿），而图例一个系列只能给一个
    // 色块 —— 画出来就是个蓝色小方块配一堆红绿柱，反而误导人。颜色规则改用图上方那行
    // 小字说明（与板块资金流面板同一套做法）。
    grid: { ...GRID },
    tooltip: {
      ...TOOLTIP,
      trigger: 'axis' as const,
      axisPointer: {
        type: 'line' as const,
        lineStyle: { color: CHART.fgDim, type: 'dashed' as const },
      },
      formatter: (params: unknown) => {
        const items = params as { dataIndex: number }[] | undefined
        const row = rows[items?.[0]?.dataIndex ?? -1]
        if (!row) return ''
        return [
          `<b>${row.trade_date}</b>`,
          `主力净流入额 ${fmtAmount(row.net_inflow)}`,
          `5日DDE ${fmtAmount(row.dde)}`,
        ].join('<br/>')
      },
    },
    xAxis: {
      type: 'category' as const,
      data: rows.map((row) => fmtShortDate(row.trade_date)),
      // `interval` 是「隔几个类目显示一个」（0 = 全显示）。用 `floor` 而不是 `ceil`
      // —— 后者在窗口短时得 1，会把一半日期标签吃掉
      axisLabel: { ...AXIS_LABEL, interval: Math.max(0, Math.floor(rows.length / 8)) },
      axisLine: AXIS_LINE,
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value' as const,
      // 不设 scale：净流入有正有负，轴必须把 0 包进来，否则看不出零基线在哪
      axisLabel: { ...AXIS_LABEL, formatter: (value: number) => fmtAmount(value) },
      splitLine: SPLIT_LINE,
      axisLine: { show: false },
    },
    series: [
      {
        type: 'bar' as const,
        name: '主力净流入额',
        data: rows.map((row) => ({
          value: row.net_inflow,
          // 逐条上色：净流入为正红、为负绿（A 股惯例）
          itemStyle: {
            color: row.net_inflow != null && row.net_inflow < 0 ? CHART.down : CHART.up,
          },
        })),
        barMaxWidth: 14,
      },
      {
        type: 'line' as const,
        name: '5日DDE',
        data: rows.map((row) => row.dde),
        symbol: 'none' as const,
        // 缺值绝不插值：连出一条假线比断开更容易被读成「数据是连续的」
        connectNulls: false,
        lineStyle: { width: 1.6, color: CHART.accent },
        itemStyle: { color: CHART.accent },
      },
    ],
  }
}

export default function StockDetail() {
  const { code = '' } = useParams<{ code: string }>()
  const [profile, setProfile] = useState<StockProfile | null>(null)
  const [themes, setThemes] = useState<StockThemes | null>(null)
  const [dde, setDde] = useState<StockDde | null>(null)
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  // 周期、取数、按需补历史都在这个 hook 里（与形态页共用一套）
  const kline = useKLine(code)
  const { reload: reloadKline } = kline

  const navigate = useNavigate()
  /**
   * 上一次是从哪份列表点进来的（形态命中 / 自选 / 板块成分股…）。
   * 挂载时读一次就够：详情页自己不会改写它，切股票也只是在同一份列表里位移。
   */
  const [codes] = useState(() => readStockList())
  const index = codes.indexOf(code)
  const prev = index > 0 ? codes[index - 1] : null
  const next = index >= 0 && index < codes.length - 1 ? codes[index + 1] : null

  const load = useCallback(async (target: string) => {
    // 题材要现取，可能失败（比如首次打开、本地还没名称），不该拖垮整页；
    // DDE 同理 —— 它的失败原因后端会放在 note 里，这里兜住的只是传输/服务端层面的错
    const [p, themeData, ddeData] = await Promise.all([
      api.stockProfile(target),
      api.stockThemes(target).catch(() => null),
      api.stockDde(target).catch(() => null),
    ])
    setProfile(p)
    setThemes(themeData)
    setDde(ddeData)
    return p
  }, [])

  useEffect(() => {
    let cancelled = false
    setError(null)
    ;(async () => {
      try {
        const p = await load(code)
        // 本地没有缓存、或历史明显不全时自动补一次，之后走缓存。
        // alreadySynced 兜住「本来就短」的票，免得每次打开都再补一遍
        if (!cancelled && p.day_count < NEED_BARS && !alreadySynced(code)) {
          setSyncing(true)
          await api.syncStock(code, 250)
          // 标记放在**成功之后**：采集正忙（409）或 iFinD 出错时这次补采是白跑的，
          // 提前标记会让本会话内再打开这只票也不再重试 —— 表现就是「点进去没日K、
          // 刷新也没用」，只能关掉标签页重开。失败就让它下次打开再试一次
          markSynced(code)
          if (!cancelled) {
            await load(code)
            // 图的数据在 hook 里，得让它重取一次，否则补完还是空的
            reloadKline()
          }
        }
      } catch (err) {
        if (!cancelled) setError((err as Error).message)
      } finally {
        if (!cancelled) setSyncing(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [code, load, reloadKline])

  /**
   * ← → 翻上/下一只。
   *
   * `index < 0` 表示没有列表上下文（比如直接输 URL 打开），这时完全不接管
   * 方向键 —— 按了不该有任何反应。
   */
  useEffect(() => {
    if (index < 0) return
    const onKey = (event: KeyboardEvent) => {
      const el = event.target as HTMLElement | null
      // 焦点在输入框 / 下拉里时不劫持方向键：详情页目前没有输入框，
      // 但这类「后来才加的元素」最容易忽略，事后又最难查
      if (
        el &&
        (el.tagName === 'INPUT' ||
          el.tagName === 'TEXTAREA' ||
          el.tagName === 'SELECT' ||
          el.isContentEditable)
      ) {
        return
      }
      const target =
        event.key === 'ArrowLeft' ? prev : event.key === 'ArrowRight' ? next : null
      if (!target) return
      event.preventDefault()
      navigate(`/stock/${target}`)
      // 换票后回到顶部：否则会停在上只票看到一半的位置，看着却是另一只票的内容
      window.scrollTo({ top: 0 })
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [index, prev, next, navigate])

  const toggleWatch = useCallback(async () => {
    if (!profile) return
    try {
      if (profile.in_watchlist) {
        await api.removeWatchlist(profile.code)
        setNotice('已移出自选')
      } else {
        await api.addWatchlist(profile.code, profile.name ?? undefined)
        setNotice('已加入自选')
      }
      await load(profile.code)
    } catch (err) {
      setError((err as Error).message)
    }
  }, [profile, load])

  const latest = profile?.latest
  /**
   * DDE 的最新一行。序列是升序的，所以取末尾 —— 它是**最近一个交易日的收盘终值**，
   * 盘中打开时通常就是昨天（见后端 `collect_dde.CLOSE_READY`），所以界面上不写「今日」。
   */
  const latestDde = dde && dde.rows.length > 0 ? dde.rows[dde.rows.length - 1] : null
  const toolbar = (
    <>
      {/* 从列表点进来才有这份上下文；直接输 URL 打开时不显示，
          免得给出一个「按了没反应」的提示 */}
      {codes.length > 1 && index >= 0 && (
        <span className="num hidden text-[13px] text-fg-dim md:inline">
          ← → 切换 · {index + 1} / {codes.length}
        </span>
      )}
      {syncing && (
        <span className="num pulse-soft text-[13px] text-accent">同步中…</span>
      )}
      <button
        type="button"
        onClick={() => void toggleWatch()}
        disabled={!profile}
        className="num border border-line px-2.5 py-[3px] text-[13px] text-fg-muted transition-colors hover:border-accent/50 hover:text-accent disabled:cursor-not-allowed disabled:opacity-40"
      >
        {profile?.in_watchlist ? '移出自选' : '加入自选'}
      </button>
    </>
  )

  return (
    <Layout toolbar={toolbar}>
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}
      {notice && (
        <Alert tone="accent" onClose={() => setNotice(null)}>
          {notice}
        </Alert>
      )}

      <div className="space-y-4">
        <Panel
          title={`${profile?.name ?? code}  ${profile?.code ?? ''}`}
          meta={
            <span className="num">
              {profile
                ? `本地缓存 ${profile.day_count} 天 · ${profile.first_date ?? '—'} ~ ${profile.last_date ?? '—'}`
                : '加载中…'}
              <Link to="/watchlist" className="ml-3 text-fg-dim hover:text-fg">
                ← 回自选
              </Link>
            </span>
          }
          delay={40}
        >
          {/* 12 格 = 用户 2026-09-26 给的完整清单（「实际管收率」「自留流通市值」按
              实际换手率 / 自由流通市值理解）。**全部有数据源了** —— 市值 / 自由流通股 /
              预测市盈率由建池时那次选股接口一并取回（加列不加调用次数，见设计文档 8.68.13）。
              12 能被 2/3/4 整除，所以各档列数下都不会剩半行空格。
              缺值的格子（还没跑过建池、或不在全 A 名单里）按站点惯例显示「—」。 */}
          <div className="grid grid-cols-2 overflow-hidden md:grid-cols-3 xl:grid-cols-4">
            <Cell label="开盘价" value={fmtNum(latest?.open, 2)} />
            <Cell
              label="收盘价"
              value={fmtNum(latest?.close, 2)}
              tone={toneOf(latest?.pct_chg)}
            />
            <Cell label="涨跌幅" value={fmtPct(latest?.pct_chg)} tone={toneOf(latest?.pct_chg)} />
            <Cell
              label="五日涨跌幅"
              value={fmtPct(profile?.pct_chg_5d)}
              tone={toneOf(profile?.pct_chg_5d)}
            />
            <Cell label="成交量" value={`${fmtAmount(latest?.volume)}股`} />
            <Cell label="成交额" value={fmtAmount(latest?.amount)} />
            <Cell
              label="复盘关联"
              value={`涨停 ${profile?.limit_up_dates.length ?? 0} 次`}
              sub={`龙虎榜 ${profile?.lhb_count ?? 0} 次`}
            />
            <Cell label="换手率" value={fmtNum(latest?.turnover, 2, '%')} />
            <Cell label="实际换手率" value={fmtNum(profile?.actual_turnover, 2, '%')} />
            <Cell label="总市值" value={fmtAmount(profile?.total_mv)} />
            <Cell label="自由流通市值" value={fmtAmount(profile?.free_float_mv)} />
            <Cell label="动态市盈率" value={fmtNum(profile?.pe_forecast, 2)} />
          </div>

          {latest?.trade_date && (
            <div className="num border-t border-line-soft px-4 py-2 text-[13px] text-fg-dim">
              最新数据 {latest.trade_date}
              {/* 「开」不再重复列 —— 上面已经有一格「开盘价」 */}
              <span className="mx-2">·</span>
              高 {fmtNum(latest.high, 2)}
              <span className="mx-2">·</span>
              低 {fmtNum(latest.low, 2)}
            </div>
          )}
        </Panel>

        <Panel
          title="所属题材"
          meta={
            <span className="num">
              {themes && themes.themes.length > 0
                ? `${themes.themes.length} 个板块 · 板块涨跌幅为 ${
                    themes.board_date ?? '—'
                  }`
                : '开盘红精选板块'}
            </span>
          }
          delay={70}
        >
          {syncing ? (
            <div className="px-4 py-6 text-center text-[13px] text-fg-dim">
              同步日线后再取题材…
            </div>
          ) : themes && themes.themes.length > 0 ? (
            <div className="flex flex-wrap gap-1.5 px-4 py-3">
              {themes.themes.map((theme) => (
                <span
                  key={theme.concept}
                  className={[
                    'flex items-baseline gap-1.5 border px-2 py-1 text-[13px]',
                    theme.board_code
                      ? 'border-line-soft bg-ink-850'
                      : // 对不上板块表的是历史遗留的旧口径名字，弱化显示
                        'border-line-soft/60 text-fg-dim',
                  ].join(' ')}
                >
                  <span className={theme.board_code ? 'text-fg' : ''}>{theme.concept}</span>
                  {theme.pct_chg != null && (
                    <span className={`num ${toneOf(theme.pct_chg)}`}>
                      {fmtPct(theme.pct_chg)}
                    </span>
                  )}
                </span>
              ))}
            </div>
          ) : (
            <div className="px-4 py-6 text-center text-[13px] text-fg-dim">
              该股近期没有涨停过。板块归属来自开盘红的涨停天梯，只覆盖涨停股 ——
              非涨停个股没有可用的归属接口，所以这里如实留空
            </div>
          )}
        </Panel>

        <Panel
          title={`${VIEW_LABEL[kline.view]} 线`}
          meta={
            <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="num">
                {kline.loading || kline.syncing
                  ? '取数中…'
                  : `${kline.bars.length} 根 · ${VIEW_UNIT[kline.view]}`}
              </span>
              <Segmented value={kline.view} items={K_VIEWS} onChange={kline.setView} />
            </span>
          }
          delay={80}
        >
          {kline.loading || syncing ? (
            <div className="flex h-[420px] items-center justify-center text-[14px] text-fg-dim">
              <span className="pulse-soft">
                {kline.syncing ? '正在补 2 年历史（周/月 K 要的长周期）…' : '加载中…'}
              </span>
            </div>
          ) : kline.error ? (
            <div className="px-4 py-10 text-center text-[14px] text-danger">
              {kline.error}
            </div>
          ) : kline.bars.length === 0 ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              没有取到该股的日线 —— 同步失败（比如采集正忙）或该股当日无行情，稍后刷新重试
            </div>
          ) : (
            <div className="px-2 pt-2">
              <KLineChart bars={kline.bars} height={420} />
            </div>
          )}
        </Panel>

        <Panel
          title="资金流向（DDE）"
          meta={<span className="num">iFinD 口径 · 主力净流入额与 5日DDE · 单位元</span>}
          delay={100}
        >
          {/* 与 profile 同一个 Promise.all 里取回来的，所以「还没到」就等于「在加载」 */}
          {profile === null ? (
            <div className="flex h-[240px] items-center justify-center text-[14px] text-fg-dim">
              <span className="pulse-soft">加载中…</span>
            </div>
          ) : !dde || dde.rows.length === 0 ? (
            // note 有值就是取数失败的原因（iFinD 报错、或这只票本来就没数据），原样显示，
            // 不画空图；note 也是空的时候才退回到「没有数据」这句
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              {dde?.note ?? '这只票暂时没有 DDE 数据'}
            </div>
          ) : (
            <>
              {/* 有数据但 note 也有值 = 后端把上次取数失败的原因带出来了（画的是库里的
                  旧数）。这种情况不能把说明吞掉，否则会被当成最新的终值看 */}
              {dde.note && (
                <div className="border-b border-line-soft px-4 py-2 text-[13px] text-fg-dim">
                  {dde.note}
                </div>
              )}
              <div className="grid grid-cols-2 overflow-hidden">
                <Cell
                  label="主力净流入额"
                  value={fmtAmount(latestDde?.net_inflow)}
                  tone={toneOf(latestDde?.net_inflow)}
                />
                <Cell
                  label="5日DDE"
                  value={fmtAmount(latestDde?.dde)}
                  tone={toneOf(latestDde?.dde)}
                />
              </div>
              <div className="px-2 pt-2">
                <div className="mb-1 text-[12px] text-fg-dim">
                  柱 = 主力净流入额（红=净流入 绿=净流出）· 线 = 5日DDE
                </div>
                <EChart option={buildDdeOption(dde.rows)} height={240} />
              </div>
            </>
          )}
        </Panel>

        <Panel
          title="涨停记录"
          meta={<span className="num">该股上过涨停池的日期（数据源只保留最近 15 个交易日）</span>}
          delay={120}
        >
          {!profile || profile.limit_up_dates.length === 0 ? (
            <div className="px-4 py-6 text-center text-[13px] text-fg-dim">
              窗口内没有涨停记录
            </div>
          ) : (
            <div className="flex flex-wrap gap-1.5 px-4 py-3">
              {profile.limit_up_dates.map((day) => (
                <span
                  key={day}
                  className="num border border-line-soft bg-ink-850 px-2 py-1 text-[13px] text-up"
                >
                  {day}
                </span>
              ))}
            </div>
          )}
        </Panel>
      </div>
    </Layout>
  )
}

function Cell({
  label,
  value,
  tone = 'text-fg',
  sub,
}: {
  label: string
  value: string
  tone?: string
  sub?: string
}) {
  return (
    <div className="relative -mr-px -mb-px border-r border-b border-line-soft px-4 py-3">
      <div className="text-[13px] tracking-[0.1em] text-fg-dim">{label}</div>
      <div className={`num mt-1.5 text-[18px] leading-tight font-medium ${tone}`}>
        {value}
      </div>
      {sub && <div className="num mt-1 text-[13px] text-fg-dim">{sub}</div>}
    </div>
  )
}
