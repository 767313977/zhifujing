import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { WatchlistRow } from '../api/types'
import Alert from '../components/Alert'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import SortTh from '../components/SortTh'
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
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const reload = useCallback(async () => {
    try {
      setRows(await api.watchlist())
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  const add = useCallback(async () => {
    const trimmed = code.trim()
    if (!trimmed) return
    try {
      await api.addWatchlist(trimmed)
      setCode('')
      setNotice('已加入自选，本地还没有行情 —— 点「同步行情」拉取')
      await reload()
    } catch (err) {
      setError((err as Error).message)
    }
  }, [code, reload])

  const remove = useCallback(
    async (target: string) => {
      try {
        await api.removeWatchlist(target)
        await reload()
      } catch (err) {
        setError((err as Error).message)
      }
    },
    [reload],
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

  const missing = rows.filter((row) => row.close == null).length
  // 首屏不排，保持后端顺序（按代码）；点列头才排
  const [sort, shown] = useSort(rows, WATCH_SORTS, { key: null })

  const toolbar = (
    <>
      <span className="num hidden text-[13px] text-fg-dim lg:inline">
        {loading ? '加载中…' : `${rows.length} 只`}
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
          meta={<span className="num">支持 600519 / 贵州茅台 / gzmt</span>}
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
              className="num min-w-0 flex-1 border border-line bg-ink-850 px-3 py-2 text-[14px] text-fg outline-none placeholder:text-fg-dim focus:border-fg-dim"
            />
            <button
              type="button"
              onClick={() => void add()}
              className="shrink-0 border border-line px-5 py-2 text-[14px] text-fg-muted transition-colors hover:border-fg-dim hover:text-fg"
            >
              加入
            </button>
          </div>
        </Panel>

        <Panel
          title="自选池"
          meta={
            <span className="num">
              {rows.length} 只
              {missing > 0 && (
                <span className="ml-3 text-accent">{missing} 只尚无行情，需同步</span>
              )}
            </span>
          }
          delay={80}
        >
          {rows.length === 0 ? (
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
                        // 点行时把当前自选列表存下，个股页就能 ← → 前后翻
                        rememberStockList(shown.map((item) => item.code))
                      }}
                    >
                      <td className="!text-left">
                        <Link
                          to={`/stock/${row.code}`}
                          className="num text-accent hover:underline"
                        >
                          {row.code}
                        </Link>
                      </td>
                      <td className="!text-left">
                        <Link to={`/stock/${row.code}`} className="text-fg hover:underline">
                          {row.name ?? '—'}
                        </Link>
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
                          onClick={() => void remove(row.code)}
                          className="border border-line px-2 py-[2px] text-[13px] text-fg-dim transition-colors hover:border-danger/50 hover:text-danger"
                        >
                          移除
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

  useEffect(() => {
    setValue(row.note ?? '')
  }, [row.note])

  const save = async () => {
    if (value === (row.note ?? '')) return
    setState('saving')
    try {
      await api.updateWatchlistNote(row.code, value)
      await onSaved()
      setState('saved')
      // 提示只闪一下：单元格很窄，常驻会一直占着位置
      window.setTimeout(() => setState('idle'), 1600)
    } catch (err) {
      setState('idle')
      onError((err as Error).message)
    }
  }

  return (
    <div className="relative w-full max-w-[220px]">
      <input
        value={value}
        onChange={(event) => setValue(event.target.value)}
        onBlur={() => void save()}
        onKeyDown={(event) => {
          if (event.key === 'Enter') event.currentTarget.blur()
        }}
        placeholder="写点备注"
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
