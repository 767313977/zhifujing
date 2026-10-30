import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { WatchlistRow } from '../api/types'
import Alert from '../components/Alert'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import SortTh from '../components/SortTh'
import { isBondCode } from '../lib/bondCode'
import { fmtNum, fmtPct, toneOf } from '../lib/format'
import { useSort } from '../lib/sort'
import type { SortSpecs } from '../lib/sort'
import { rememberStockList } from '../lib/stockNav'

const WATCH_SORTS: SortSpecs<WatchlistRow> = {
  code: { value: (row) => row.code, first: 'asc' },
  name: { value: (row) => row.name, first: 'asc' },
  // `2026-09-18` 定长写法，按字符串排等价于按日期排
  latest_date: { value: (row) => row.latest_date, first: 'asc' },
  close: { value: (row) => row.close },
  pct_chg: { value: (row) => row.pct_chg },
}

export default function WatchlistPage() {
  const [rows, setRows] = useState<WatchlistRow[]>([])
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [code, setCode] = useState('')
  // 「加入」请求进行中：连点按钮或连按回车不能发多个 POST
  const [submitting, setSubmitting] = useState(false)
  /** 「移除」请求进行中（防止连点发两次 DELETE）；值是正在移除的代码 */
  const [removing, setRemoving] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  /**
   * 自选名单**列表本身**取数失败的原因（2026-10-23 加）。
   *
   * 与 `error`（增删改/同步等操作的失败）分开：列表失败要整块显示失败态，
   * 不能落进「自选池是空的」——那会把接口故障说成「你的自选没了」
   * （2026-10-23 前端审计 P1）。
   */
  const [rowsError, setRowsError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setRowsError(null)
    try {
      setRows(await api.watchlist())
    } catch (err) {
      setRowsError((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  const add = useCallback(async () => {
    const trimmed = code.trim()
    // busy 守卫：回车连按（按钮已禁用时回车仍会触发）走到这里也要挡住
    if (!trimmed || submitting) return
    try {
      setSubmitting(true)
      const row = await api.addWatchlist(trimmed)
      setCode('')
      // 转债的日线来自盘后快照，点「同步行情」也拉不到它，提示语不能一刀切
      setNotice(
        row.latest_date
          ? '已加入自选'
          : isBondCode(row.code)
            ? '已加入自选；转债行情每天盘后自动更新，不需要「同步行情」'
            : '已加入自选，本地还没有行情 —— 点「同步行情」拉取',
      )
      await reload()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      // 成功失败都复位，失败时用户可以改完再提交
      setSubmitting(false)
    }
  }, [code, reload, submitting])

  const remove = useCallback(
    async (row: WatchlistRow) => {
      // 防连点：同一个代码已有删除在途就不再发（第二次 DELETE 会 404，弹一条假错误）
      if (removing !== null) return
      const label = row.name ?? row.code
      if (!window.confirm(`从自选里移除 ${label}（${row.code}）？`)) return
      setRemoving(row.code)
      try {
        await api.removeWatchlist(row.code)
        await reload()
      } catch (err) {
        setError((err as Error).message)
      } finally {
        setRemoving(null)
      }
    },
    [reload, removing],
  )

  const sync = useCallback(async () => {
    setSyncing(true)
    setError(null)
    setNotice('同步中，逐只拉取日线…')
    try {
      const result = await api.syncWatchlist(250)
      setNotice(`同步完成，写入 ${result.rows} 行日线`)
      await reload()
    } catch (err) {
      setError((err as Error).message)
      setNotice(null)
    } finally {
      setSyncing(false)
    }
  }, [reload])

  // 转债没有行情时提示「去同步」是错的（同步只拉股票日线），所以只数股票
  const missing = rows.filter((row) => row.close == null && !isBondCode(row.code)).length
  // 首屏不排，保持后端顺序（按代码）；点列头才排
  const [sort, shown] = useSort(rows, WATCH_SORTS, { key: null })

  const toolbar = (
    <>
      <span className="num hidden text-[13px] text-fg-dim lg:inline">
        {loading ? '加载中…' : rowsError ? '—' : `${rows.length} 只`}
      </span>
      <button
        type="button"
        onClick={() => void sync()}
        disabled={syncing || rows.length === 0}
        className="num border border-accent/60 bg-accent/10 px-2.5 py-[3px] text-[13px] text-accent transition-colors hover:bg-accent/20 disabled:cursor-not-allowed disabled:opacity-40"
      >
        {syncing ? '同步中…' : '同步行情'}
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
          title="加入自选"
          meta={<span className="num">支持 600519 / 贵州茅台 / gzmt · 也可直接加转债代码</span>}
          delay={40}
        >
          <div className="flex flex-col gap-2 px-4 py-3.5 sm:flex-row">
            <input
              value={code}
              onChange={(event) => setCode(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') void add()
              }}
              placeholder="输入代码 / 名称 / 拼音首字母"
              maxLength={32}
              disabled={submitting}
              className="num min-w-0 flex-1 border border-line bg-ink-850 px-3 py-2 text-[14px] text-fg outline-none placeholder:text-fg-dim focus:border-fg-dim disabled:cursor-not-allowed disabled:opacity-40"
            />
            <button
              type="button"
              onClick={() => void add()}
              disabled={submitting}
              className="shrink-0 border border-line px-5 py-2 text-[14px] text-fg-muted transition-colors hover:border-fg-dim hover:text-fg disabled:cursor-not-allowed disabled:opacity-40"
            >
              {submitting ? '加入中…' : '加入'}
            </button>
          </div>
        </Panel>

        <Panel
          title="自选池"
          meta={
            <span className="num">
              {rowsError ? '—' : `${rows.length} 只`}
              {!rowsError && missing > 0 && (
                <span className="ml-3 text-accent">{missing} 只尚无行情，需同步</span>
              )}
            </span>
          }
          delay={80}
        >
          {rowsError ? (
            // 列表失败 ≠ 空池：整块显示失败态与重试，别让用户以为自选被清空了
            <div className="px-4 py-10 text-center text-[14px] text-danger">
              自选取数失败：{rowsError}
              <div className="mt-1 text-[12px] text-fg-dim">
                这是接口出错，不是「你的自选没了」；点下面重试或刷新页面
              </div>
              <button
                type="button"
                onClick={() => {
                  setLoading(true)
                  void reload()
                }}
                className="mt-3 border border-line px-3 py-1 text-[13px] text-fg-muted transition-colors hover:border-fg-dim hover:text-fg"
              >
                重试
              </button>
            </div>
          ) : rows.length === 0 ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              {loading ? '加载中…' : '自选池是空的。可以在上方按代码、名称或拼音首字母加入'}
            </div>
          ) : (
            <div className="overflow-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <SortTh sortKey="code" align="left" {...sort}>代码</SortTh>
                    <SortTh sortKey="name" align="left" {...sort}>名称</SortTh>
                    <SortTh sortKey="latest_date" {...sort}>最新日期</SortTh>
                    <SortTh sortKey="close" {...sort}>收盘价</SortTh>
                    <SortTh sortKey="pct_chg" {...sort}>涨跌幅</SortTh>
                    {/* 备注与操作没有可比的值，保持普通表头 */}
                    <th className="!text-left">备注</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((row) => (
                    <tr
                      key={row.code}
                      onClick={() => {
                        // 点行时把当前自选列表存下，个股页就能 ← → 前后翻。
                        // 转债代码要剔掉：← → 走到它只会打开一个「代码不存在」的页面
                        rememberStockList(
                          shown.filter((item) => !isBondCode(item.code)).map((item) => item.code),
                        )
                      }}
                    >
                      <td className="!text-left">
                        {isBondCode(row.code) ? (
                          // 转债没有个股详情页，不做假跳转
                          <span className="num text-fg">{row.code}</span>
                        ) : (
                          <Link
                            to={`/stock/${encodeURIComponent(row.code)}`}
                            className="num text-accent hover:underline"
                          >
                            {row.code}
                          </Link>
                        )}
                      </td>
                      <td className="!text-left">
                        {isBondCode(row.code) ? (
                          <span className="text-fg">
                            {row.name ?? '—'}
                            <span className="ml-1.5 text-[11px] text-warn">债</span>
                          </span>
                        ) : (
                          <Link
                            to={`/stock/${encodeURIComponent(row.code)}`}
                            className="text-fg hover:underline"
                          >
                            {row.name ?? '—'}
                          </Link>
                        )}
                      </td>
                      <td>
                        <span className="num text-fg-muted">{row.latest_date ?? '—'}</span>
                      </td>
                      <td>
                        <span className={`num ${toneOf(row.pct_chg)}`}>
                          {fmtNum(row.close, 2)}
                        </span>
                      </td>
                      <td>
                        <span className={`num ${toneOf(row.pct_chg)}`}>
                          {fmtPct(row.pct_chg)}
                        </span>
                      </td>
                      <td className="!text-left">
                        <NoteEditor row={row} onSaved={reload} onError={setError} />
                      </td>
                      <td>
                        <button
                          type="button"
                          onClick={() => void remove(row)}
                          disabled={removing !== null}
                          className="border border-line px-2 py-[2px] text-[13px] text-fg-dim transition-colors hover:border-danger/50 hover:text-danger disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          {removing === row.code ? '移除中…' : '移除'}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel
          title="说明"
          meta={<span className="num">行情口径</span>}
          delay={120}
        >
          <div className="px-4 py-3 text-[13px] leading-relaxed text-fg-dim">
            <ul className="space-y-1">
              <li>
                · 列表中的价格来自<span className="text-fg-muted">本地缓存的日线</span>，
                页面打开即读、不联网。实时性由每交易日收盘后的自动采集，
                或右上角「同步行情」保证。
              </li>
              <li>
                · 本站不做全市场落库（会触发数据源频控），所以只有自选股与主动看过的个股会被缓存到本地。
              </li>
              <li>
                · 可转债代码（如 113042）也能加自选，价格来自每天盘后的转债快照；
                它没有个股页，所以不做跳转。
              </li>
            </ul>
          </div>
        </Panel>
      </div>
    </Layout>
  )
}

/** 备注就地编辑：失焦或回车即保存，避免额外的弹窗。 */
function NoteEditor({
  row,
  onSaved,
  onError,
}: {
  row: WatchlistRow
  onSaved: () => Promise<void>
  onError: (message: string) => void
}) {
  const [value, setValue] = useState(row.note ?? '')
  const [state, setState] = useState<'idle' | 'saving' | 'saved'>('idle')
  // 「已保存」复原定时器：卸载（切页 / 列表刷新卸载旧行）时清掉，别对已卸载组件 setState
  const savedTimer = useRef<number | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    // ⚠️ 只在**没在输入**时跟随外部值（2026-10-28 体检）：保存后 `onSaved()` 会刷新列表、
    // `row.note` 变成刚保存的旧值；如果用户已经又点回来接着改，这里跟着覆盖就会把
    // 刚敲的字吃掉（看起来像「保存把输入回滚了」）。
    if (document.activeElement === inputRef.current) return
    setValue(row.note ?? '')
  }, [row.note])

  // 卸载清理
  useEffect(() => {
    return () => {
      if (savedTimer.current !== null) window.clearTimeout(savedTimer.current)
    }
  }, [])

  const save = async () => {
    if (value === (row.note ?? '')) return
    setState('saving')
    try {
      await api.updateWatchlistNote(row.code, value)
      await onSaved()
      setState('saved')
      // 提示只闪一下：单元格很窄，常驻会一直占着位置
      if (savedTimer.current !== null) window.clearTimeout(savedTimer.current)
      savedTimer.current = window.setTimeout(() => {
        savedTimer.current = null
        setState('idle')
      }, 1600)
    } catch (err) {
      setState('idle')
      onError((err as Error).message)
    }
  }

  return (
    <div className="relative w-full max-w-[220px]">
      <input
        ref={inputRef}
        value={value}
        onChange={(event) => setValue(event.target.value)}
        onBlur={() => void save()}
        onKeyDown={(event) => {
          if (event.key === 'Enter') event.currentTarget.blur()
        }}
        placeholder="写点备注"
        maxLength={2000}
        className="w-full border border-transparent bg-transparent py-[2px] pr-12 pl-1 text-[13px] text-fg-muted outline-none transition-colors placeholder:text-fg-dim hover:border-line focus:border-line focus:bg-ink-850"
      />
      {state !== 'idle' && (
        <span className="num pointer-events-none absolute top-1/2 right-1 -translate-y-1/2 bg-ink-850 px-1 text-[12px] text-accent">
          {state === 'saving' ? '保存中…' : '已保存'}
        </span>
      )}
    </div>
  )
}
