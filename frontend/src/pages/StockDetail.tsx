import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client'
import type { StockProfile, StockThemes } from '../api/types'
import Alert from '../components/Alert'
import KLineChart from '../components/KLineChart'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import Segmented from '../components/Segmented'
import { fmtAmount, fmtNum, fmtPct, toneOf } from '../lib/format'
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
  day: '不复权真实价，红涨绿跌',
  week: '不复权真实价 · 由日线按 ISO 周聚合',
  month: '不复权真实价 · 由日线按月聚合',
}

export default function StockDetail() {
  const { code = '' } = useParams<{ code: string }>()
  const [profile, setProfile] = useState<StockProfile | null>(null)
  const [themes, setThemes] = useState<StockThemes | null>(null)
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
    // 题材要现取，可能失败（比如首次打开、本地还没名称），不该拖垮整页
    const [p, themeData] = await Promise.all([
      api.stockProfile(target),
      api.stockThemes(target).catch(() => null),
    ])
    setProfile(p)
    setThemes(themeData)
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
  const toolbar = (
    <>
      {/* 从列表点进来才有这份上下文；直接输 URL 打开时不显示，
          免得给出一个「按了没反应」的提示 */}
      {codes.length > 1 && index >= 0 && (
        <span className="num hidden text-[12px] text-fg-dim md:inline">
          ← → 切换 · {index + 1} / {codes.length}
        </span>
      )}
      {syncing && (
        <span className="num pulse-soft text-[12px] text-accent">同步中…</span>
      )}
      <button
        type="button"
        onClick={() => void toggleWatch()}
        disabled={!profile}
        className="num border border-line px-2.5 py-[3px] text-[12px] text-fg-muted transition-colors hover:border-accent/50 hover:text-accent disabled:cursor-not-allowed disabled:opacity-40"
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
          <div className="grid grid-cols-2 overflow-hidden md:grid-cols-5">
            <Cell label="收盘价" value={fmtNum(latest?.close, 2)} tone={toneOf(latest?.pct_chg)} />
            <Cell label="涨跌幅" value={fmtPct(latest?.pct_chg)} tone={toneOf(latest?.pct_chg)} />
            <Cell label="成交量" value={`${fmtAmount(latest?.volume)}股`} />
            <Cell label="成交额" value={fmtAmount(latest?.amount)} />
            <Cell
              label="复盘关联"
              value={`涨停 ${profile?.limit_up_dates.length ?? 0} 次`}
              sub={`龙虎榜 ${profile?.lhb_count ?? 0} 次`}
            />
          </div>

          {latest?.trade_date && (
            <div className="num border-t border-line-soft px-4 py-2 text-[12px] text-fg-dim">
              最新数据 {latest.trade_date}
              <span className="mx-2">·</span>
              开 {fmtNum(latest.open, 2)}
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
            <div className="px-4 py-6 text-center text-[12px] text-fg-dim">
              同步日线后再取题材…
            </div>
          ) : themes && themes.themes.length > 0 ? (
            <div className="flex flex-wrap gap-1.5 px-4 py-3">
              {themes.themes.map((theme) => (
                <span
                  key={theme.concept}
                  className={[
                    'flex items-baseline gap-1.5 border px-2 py-1 text-[12px]',
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
            <div className="px-4 py-6 text-center text-[12px] text-fg-dim">
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
            <div className="flex h-[420px] items-center justify-center text-[13px] text-fg-dim">
              <span className="pulse-soft">
                {kline.syncing ? '正在补 2 年历史（周/月 K 要的长周期）…' : '加载中…'}
              </span>
            </div>
          ) : kline.error ? (
            <div className="px-4 py-10 text-center text-[13px] text-danger">
              {kline.error}
            </div>
          ) : kline.bars.length === 0 ? (
            <div className="px-4 py-10 text-center text-[13px] text-fg-dim">
              没有取到该股的日线 —— 同步失败（比如采集正忙）或该股当日无行情，稍后刷新重试
            </div>
          ) : (
            <div className="px-2 pt-2">
              <KLineChart bars={kline.bars} height={420} />
            </div>
          )}
        </Panel>

        <Panel
          title="涨停记录"
          meta={<span className="num">该股上过涨停池的日期（数据源只保留最近 15 个交易日）</span>}
          delay={120}
        >
          {!profile || profile.limit_up_dates.length === 0 ? (
            <div className="px-4 py-6 text-center text-[12px] text-fg-dim">
              窗口内没有涨停记录
            </div>
          ) : (
            <div className="flex flex-wrap gap-1.5 px-4 py-3">
              {profile.limit_up_dates.map((day) => (
                <span
                  key={day}
                  className="num border border-line-soft bg-ink-850 px-2 py-1 text-[12px] text-up"
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
      <div className="text-[12px] tracking-[0.1em] text-fg-dim">{label}</div>
      <div className={`num mt-1.5 text-[18px] leading-tight font-medium ${tone}`}>
        {value}
      </div>
      {sub && <div className="num mt-1 text-[12px] text-fg-dim">{sub}</div>}
    </div>
  )
}
