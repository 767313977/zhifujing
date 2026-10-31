import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { PositionIn, PositionRow, WatchlistRow } from '../api/types'
import Alert from '../components/Alert'
import Layout from '../components/Layout'
import LoadState from '../components/LoadState'
import Panel from '../components/Panel'
import StockLink from '../components/StockLink'
import { fmtNum, fmtPct, todayLocal, toneOf } from '../lib/format'

/**
 * 持仓（FEAT-POSITIONS，2026-10-31）：**手工持仓，不接券商**。
 *
 * 一行持仓 = 交易日志里一笔未平仓记录（`exit_date` 为空），所以这里新增/删除
 * 与交易日志是同一份数据，不会出现两套账。现价用本地最新 `stock_daily` 收盘，
 * 页面不联网、不轮询。
 */

interface FormState {
  code: string
  name: string
  entryDate: string
  price: string
  shares: string
  positionPct: string
  note: string
}

function emptyForm(): FormState {
  return {
    code: '',
    name: '',
    entryDate: todayLocal(),
    price: '',
    shares: '',
    positionPct: '',
    note: '',
  }
}

const INPUT =
  'num w-full border border-line bg-ink-850 px-2 py-[5px] text-[13px] text-fg outline-none placeholder:text-fg-dim focus:border-fg-dim'

export default function Positions() {
  const [rows, setRows] = useState<PositionRow[]>([])
  const [watchlist, setWatchlist] = useState<WatchlistRow[]>([])
  const [form, setForm] = useState<FormState>(emptyForm)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await api.positions()
      setRows(data.rows)
      setError(null)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
    api.watchlist().then(setWatchlist).catch(() => setWatchlist([]))
  }, [load])

  const set = (key: keyof FormState, value: string) => {
    setForm((prev) => ({ ...prev, [key]: value }))
  }

  const submit = async () => {
    if (!form.code.trim() || !form.entryDate || !form.price) {
      setError('代码、建仓日、成本价是必填项')
      return
    }
    const payload: PositionIn = {
      code: form.code.trim(),
      name: form.name.trim() || null,
      entry_date: form.entryDate,
      entry_price: Number(form.price),
      shares: form.shares.trim() ? Number(form.shares) : null,
      position_pct: form.positionPct.trim() ? Number(form.positionPct) : null,
      note: form.note.trim() || null,
    }
    setSaving(true)
    setError(null)
    try {
      if (editingId === null) {
        await api.createPosition(payload)
        setNotice('已建仓')
      } else {
        await api.updatePosition(editingId, payload)
        setNotice('持仓已更新')
      }
      setForm(emptyForm())
      setEditingId(null)
      await load()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const edit = (row: PositionRow) => {
    setEditingId(row.id)
    setForm({
      code: row.code,
      name: row.name ?? '',
      entryDate: row.entry_date,
      price: String(row.entry_price),
      shares: row.shares == null ? '' : String(row.shares),
      positionPct: row.position_pct == null ? '' : String(row.position_pct),
      note: row.note ?? '',
    })
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  const remove = async (row: PositionRow) => {
    if (!window.confirm(`删除 ${row.name ?? row.code} 的持仓记录？（不会动已平仓的日志）`)) return
    try {
      await api.deletePosition(row.id)
      setNotice('已删除')
      if (editingId === row.id) {
        setEditingId(null)
        setForm(emptyForm())
      }
      await load()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  const summary = useMemo(() => {
    const values = rows.filter((row) => row.market_value != null)
    const pnls = rows.filter((row) => row.pnl != null)
    return {
      count: rows.length,
      value: values.reduce((sum, row) => sum + (row.market_value ?? 0), 0),
      pnl: pnls.reduce((sum, row) => sum + (row.pnl ?? 0), 0),
      wins: pnls.filter((row) => (row.pnl ?? 0) > 0).length,
      losses: pnls.filter((row) => (row.pnl ?? 0) < 0).length,
    }
  }, [rows])

  const pickWatch = (code: string) => {
    const item = watchlist.find((row) => row.code === code)
    if (!item) return
    setForm((prev) => ({ ...prev, code: item.code, name: item.name ?? '' }))
  }

  return (
    <Layout>
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}
      {notice && (
        <Alert tone="accent" onClose={() => setNotice(null)}>
          {notice}
        </Alert>
      )}

      <div className="space-y-4">
        <div className="flex flex-wrap items-stretch gap-px border border-line-soft bg-line-soft">
          {[
            ['持仓', String(summary.count), '只'],
            ['总市值', fmtNum(summary.value, 0), '元'],
            ['总浮盈', summary.pnl >= 0 ? `+${summary.pnl.toFixed(0)}` : summary.pnl.toFixed(0), '元'],
            ['盈 / 亏', `${summary.wins} / ${summary.losses}`, '只'],
          ].map(([label, value, unit]) => (
            <div key={label} className="flex-1 bg-ink-900 px-4 py-2.5">
              <div className="text-[12px] text-fg-dim">{label}</div>
              <div className={`num text-[15px] ${label === '总浮盈' ? toneOf(summary.pnl) : 'text-fg'}`}>
                {value}
                <span className="ml-1 text-[11px] text-fg-dim">{unit}</span>
              </div>
            </div>
          ))}
        </div>

        <Panel
          title={editingId === null ? '建一笔持仓' : `编辑持仓 #${editingId}`}
          meta={<span className="num">手工记录，不接券商；现价取本地最新收盘</span>}
          delay={40}
        >
          <div className="grid grid-cols-2 gap-3 px-4 py-3 md:grid-cols-4">
            <label className="flex flex-col gap-1">
              <span className="text-[12px] text-fg-dim">从自选填入</span>
              <select
                value=""
                onChange={(event) => pickWatch(event.target.value)}
                className={INPUT}
              >
                <option value="">选择…</option>
                {watchlist.map((row) => (
                  <option key={row.code} value={row.code}>
                    {row.name ?? row.code}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[12px] text-fg-dim">代码 *</span>
              <input
                value={form.code}
                onChange={(event) => set('code', event.target.value)}
                placeholder="600519"
                className={INPUT}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[12px] text-fg-dim">名称</span>
              <input
                value={form.name}
                onChange={(event) => set('name', event.target.value)}
                placeholder="留空自动补"
                className={INPUT}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[12px] text-fg-dim">建仓日 *</span>
              <input
                type="date"
                value={form.entryDate}
                onChange={(event) => set('entryDate', event.target.value)}
                className={INPUT}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[12px] text-fg-dim">成本价 *</span>
              <input
                type="number"
                step="0.01"
                value={form.price}
                onChange={(event) => set('price', event.target.value)}
                className={INPUT}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[12px] text-fg-dim">数量（股）</span>
              <input
                type="number"
                step="100"
                value={form.shares}
                onChange={(event) => set('shares', event.target.value)}
                placeholder="可空"
                className={INPUT}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[12px] text-fg-dim">占资金（%）</span>
              <input
                type="number"
                step="0.1"
                value={form.positionPct}
                onChange={(event) => set('positionPct', event.target.value)}
                placeholder="可空"
                className={INPUT}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[12px] text-fg-dim">备注</span>
              <input
                value={form.note}
                onChange={(event) => set('note', event.target.value)}
                placeholder="可空"
                className={INPUT}
              />
            </label>
          </div>
          <div className="flex gap-2 px-4 pb-3">
            <button
              type="button"
              onClick={() => void submit()}
              disabled={saving}
              className="border border-accent/60 bg-accent/10 px-3 py-[6px] text-[13px] text-accent transition-colors hover:bg-accent/20 disabled:opacity-40"
            >
              {saving ? '保存中…' : editingId === null ? '建仓' : '保存修改'}
            </button>
            {editingId !== null && (
              <button
                type="button"
                onClick={() => {
                  setEditingId(null)
                  setForm(emptyForm())
                }}
                className="border border-line px-3 py-[6px] text-[13px] text-fg-muted hover:text-fg"
              >
                取消
              </button>
            )}
          </div>
        </Panel>

        <Panel
          title="我的持仓"
          meta={<span className="num">{loading ? '加载中…' : `${rows.length} 只`}</span>}
          delay={80}
        >
          {rows.length === 0 ? (
            <LoadState loading={loading} emptyText="还没有持仓 —— 上面建一笔，或从「明日计划」转持仓" />
          ) : (
            <div className="overflow-x-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <th className="text-left">代码</th>
                    <th className="text-left">名称</th>
                    <th>建仓日</th>
                    <th>成本</th>
                    <th>数量</th>
                    <th>现价</th>
                    <th>浮盈</th>
                    <th>比例</th>
                    <th>仓位</th>
                    <th className="text-left">备注</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.id}>
                      <td className="text-left">
                        <StockLink code={row.code} className="num text-fg">
                          {row.code}
                        </StockLink>
                      </td>
                      <td className="text-left">
                        <StockLink code={row.code}>{row.name ?? '—'}</StockLink>
                      </td>
                      <td className="num">{row.entry_date}</td>
                      <td className="num">{fmtNum(row.entry_price)}</td>
                      <td className="num">{row.shares ?? '—'}</td>
                      <td className="num" title={row.latest_date ?? ''}>
                        {fmtNum(row.latest_close)}
                      </td>
                      <td className={`num ${toneOf(row.pnl)}`}>
                        {row.pnl == null ? '—' : fmtNum(row.pnl, 0)}
                      </td>
                      <td className={`num ${toneOf(row.pnl_pct)}`}>{fmtPct(row.pnl_pct)}</td>
                      <td className="num">{row.position_pct == null ? '—' : `${fmtNum(row.position_pct, 1)}%`}</td>
                      <td className="text-left text-[12px] text-fg-dim">{row.note ?? '—'}</td>
                      <td>
                        <div className="flex gap-2 whitespace-nowrap">
                          <button
                            type="button"
                            onClick={() => edit(row)}
                            className="text-[12px] text-accent hover:underline"
                          >
                            编辑
                          </button>
                          <button
                            type="button"
                            onClick={() => void remove(row)}
                            className="text-[12px] text-fg-dim hover:text-danger"
                          >
                            删除
                          </button>
                          {row.plan_id != null && (
                            <span className="text-[11px] text-fg-dim">计划 #{row.plan_id}</span>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="px-4 py-3 text-[12px] leading-relaxed text-fg-dim">
            持仓就是交易日志里的未平仓记录；平仓请到「交易日志」填卖出价。现价与浮盈用本地
            最新日线计算（不联网），停牌或未采集时显示「—」，不拿 0 顶。
          </p>
        </Panel>
      </div>
    </Layout>
  )
}
