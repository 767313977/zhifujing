import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../api/client'
import type {
  TradePlanIn,
  TradePlanRow,
  TradePlanSuggestion,
} from '../api/types'
import Alert from '../components/Alert'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import StockLink from '../components/StockLink'
import { fmtNum, fmtPct, todayLocal, toneOf } from '../lib/format'

interface FormState {
  code: string
  name: string
  planDate: string
  buyPrice: string
  stopPrice: string
  note: string
}

function emptyForm(): FormState {
  return {
    code: '',
    name: '',
    planDate: todayLocal(),
    buyPrice: '',
    stopPrice: '',
    note: '',
  }
}

const INPUT =
  'num w-full border border-line bg-ink-850 px-2 py-[5px] text-[13px] text-fg outline-none placeholder:text-fg-dim focus:border-fg-dim'

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="flex min-w-0 flex-col gap-1">
      <span className="text-[12px] text-fg-dim">{label}</span>
      {children}
      {hint && <span className="text-[11px] text-fg-dim">{hint}</span>}
    </label>
  )
}

/** 计划状态的颜色：触发最显眼，破位 / 同日破位压暗或标红。 */
function statusClass(status: string): string {
  if (status === '触发') return 'text-accent'
  if (status === '同日触发并跌破') return 'text-danger'
  if (status === '放弃（破位）') return 'text-fg-muted'
  if (status === '未触发') return 'text-fg-dim'
  if (status.startsWith('无日线')) return 'text-fg-dim'
  return 'text-fg-dim'
}

export default function PlansPage() {
  const [rows, setRows] = useState<TradePlanRow[]>([])
  const [suggestions, setSuggestions] = useState<TradePlanSuggestion[]>([])
  const [form, setForm] = useState<FormState>(emptyForm)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  /**
   * 「我的计划」与「候选建议」各自的取数失败原因。
   *
   * 为什么必须分开（2026-10-22 体检 #2）：候选建议要跑当日形态命中再与自选股合并，
   * 慢且容易失败；两者塞在同一个 `Promise.all` 里时它一挂 `setRows` 就不执行，
   * 页面会把用户写过的计划说成「还没有计划」。
   */
  const [plansError, setPlansError] = useState<string | null>(null)
  const [suggestError, setSuggestError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setPlansError(null)
    setSuggestError(null)
    // 两个请求各自转成「数据 + 失败原因」，谁挂了都不影响另一块（见上面 state 处的说明）
    const [plansResult, suggestResult] = await Promise.all([
      api.plans().then(
        (data) => ({ data, error: null as string | null }),
        (err: Error) => ({ data: null, error: err.message }),
      ),
      api.planSuggestions().then(
        (data) => ({ data, error: null as string | null }),
        (err: Error) => ({ data: null, error: err.message }),
      ),
    ])
    // 只在真拿到数据时覆盖：失败时保留上一份，别把用户已看到的计划清成空表
    if (plansResult.data) setRows(plansResult.data.rows)
    if (suggestResult.data) setSuggestions(suggestResult.data.rows)
    setPlansError(plansResult.error)
    setSuggestError(suggestResult.error)
    setLoading(false)
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const set = (key: keyof FormState, value: string) => {
    setForm((prev) => ({ ...prev, [key]: value }))
  }

  const reset = () => {
    setForm(emptyForm())
    setEditingId(null)
  }

  const fill = (item: TradePlanSuggestion) => {
    setEditingId(null)
    setForm({
      code: item.code,
      name: item.name ?? '',
      planDate: form.planDate || todayLocal(),
      buyPrice: item.buy_price == null ? '' : String(item.buy_price),
      stopPrice: item.stop_price == null ? '' : String(item.stop_price),
      note: item.pattern_name ? `来自形态：${item.pattern_name}` : '',
    })
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  const submit = async () => {
    if (!form.code.trim() || !form.planDate || !form.buyPrice || !form.stopPrice) {
      setError('代码、计划日、买点、放弃位都是必填项')
      return
    }
    const payload: TradePlanIn = {
      code: form.code.trim(),
      name: form.name.trim() || null,
      plan_date: form.planDate,
      buy_price: Number(form.buyPrice),
      stop_price: Number(form.stopPrice),
      note: form.note.trim() || null,
    }
    setSaving(true)
    setError(null)
    try {
      if (editingId === null) {
        await api.createPlan(payload)
        setNotice('计划已保存')
      } else {
        await api.updatePlan(editingId, payload)
        setNotice('计划已更新')
      }
      reset()
      await load()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const edit = (row: TradePlanRow) => {
    setEditingId(row.id)
    setForm({
      code: row.code,
      name: row.name ?? '',
      planDate: row.plan_date,
      buyPrice: String(row.buy_price),
      stopPrice: String(row.stop_price),
      note: row.note ?? '',
    })
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  const remove = async (row: TradePlanRow) => {
    if (!window.confirm(`删除 ${row.plan_date} 的 ${row.name ?? row.code} 计划？`)) return
    try {
      await api.deletePlan(row.id)
      setNotice('已删除')
      if (editingId === row.id) reset()
      await load()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  /** 建议里只显示有价格或属于自选的（自选即使没命中也可以手填）。 */
  const shownSuggestions = useMemo(
    () => suggestions.filter((item) => item.in_watchlist || item.buy_price != null),
    [suggestions],
  )

  return (
    <Layout>
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}
      {notice && (
        <Alert tone="accent" onClose={() => setNotice(null)}>
          {notice}
        </Alert>
      )}

      <div className="space-y-4">
        <Panel
          title={editingId === null ? '写一条次日计划' : `编辑计划 #${editingId}`}
          meta={
            <span className="num">
              过买点才算触发 · 破放弃位就算走坏 · 结果当天收盘后自动核对
            </span>
          }
          delay={40}
        >
          <div className="grid grid-cols-2 gap-3 px-4 py-3 md:grid-cols-4">
            <Field label="代码 *">
              <input
                value={form.code}
                onChange={(event) => set('code', event.target.value)}
                placeholder="600519"
                className={INPUT}
              />
            </Field>
            <Field label="名称" hint="留空自动补">
              <input
                value={form.name}
                onChange={(event) => set('name', event.target.value)}
                placeholder="贵州茅台"
                className={INPUT}
              />
            </Field>
            <Field label="计划日 *" hint="计划针对哪个交易日">
              <input
                type="date"
                value={form.planDate}
                onChange={(event) => set('planDate', event.target.value)}
                className={INPUT}
              />
            </Field>
            <Field label="过这个价买 *">
              <input
                type="number"
                step="0.01"
                value={form.buyPrice}
                onChange={(event) => set('buyPrice', event.target.value)}
                placeholder="0.00"
                className={INPUT}
              />
            </Field>
            <Field label="破这个价放弃 *" hint="必须低于买点">
              <input
                type="number"
                step="0.01"
                value={form.stopPrice}
                onChange={(event) => set('stopPrice', event.target.value)}
                placeholder="0.00"
                className={INPUT}
              />
            </Field>
            <Field label="备注">
              <input
                value={form.note}
                onChange={(event) => set('note', event.target.value)}
                placeholder="可选"
                className={INPUT}
              />
            </Field>
            <div className="flex items-end gap-2 md:col-span-2">
              <button
                type="button"
                onClick={() => void submit()}
                disabled={saving}
                className="border border-accent/60 bg-accent/10 px-3 py-[6px] text-[13px] text-accent transition-colors hover:bg-accent/20 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {saving ? '保存中…' : editingId === null ? '保存计划' : '保存修改'}
              </button>
              {editingId !== null && (
                <button
                  type="button"
                  onClick={reset}
                  className="border border-line px-3 py-[6px] text-[13px] text-fg-muted transition-colors hover:border-fg-dim hover:text-fg"
                >
                  取消
                </button>
              )}
            </div>
          </div>
          <p className="px-4 pb-3 text-[12px] leading-relaxed text-fg-dim">
            计划日收盘后自动核对：最高价 ≥ 买点 = 触发；最低价 ≤ 放弃位 = 走坏；
            两者同日出现会标注「同日触发并跌破」（日线没有盘中顺序，不猜先后）。
            触发后还会给当日收盘与未来 1/3/5 个交易日的表现。全部本地计算，零配额。
          </p>
        </Panel>

        <Panel
          title="候选：自选股 + 当日形态命中"
          meta={<span className="num">点「带入」自动填代码与 key_levels 的买点 / 支撑</span>}
          delay={70}
        >
          {suggestError ? (
            // 候选取数失败只影响这一块；「我的计划」照常显示（2026-10-22 体检 #2）
            <div className="px-4 py-8 text-center text-[13px] text-danger">
              候选取数失败：{suggestError}
              <div className="mt-1 text-[12px] text-fg-dim">
                不是「今天没有可带入的候选」；你写好的计划仍显示在下面，刷新页面重试
              </div>
            </div>
          ) : shownSuggestions.length === 0 ? (
            <div className="px-4 py-8 text-center text-[13px] text-fg-dim">
              {loading ? '加载中…' : '自选股与当日命中里没有可带入的候选'}
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <th className="!text-left">股票</th>
                    <th className="!text-left">来源</th>
                    <th className="!text-left">形态 / 理由</th>
                    <th>建议买点</th>
                    <th>建议放弃位</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {shownSuggestions.map((item) => (
                    <tr key={`${item.source}-${item.code}`}>
                      <td className="!text-left">
                        <StockLink code={item.code}>{item.name ?? item.code}</StockLink>
                        <div className="num text-[11px] text-fg-dim">{item.code}</div>
                      </td>
                      <td className="!text-left text-[12px] text-fg-dim">
                        {item.in_watchlist ? '自选股' : '形态命中'}
                      </td>
                      <td className="!text-left text-[12px] text-fg-muted">
                        {item.pattern_name ?? '—'}
                      </td>
                      <td>
                        <span className="num">
                          {item.buy_price == null ? '—' : fmtNum(item.buy_price, 2)}
                        </span>
                      </td>
                      <td>
                        <span className="num">
                          {item.stop_price == null ? '—' : fmtNum(item.stop_price, 2)}
                        </span>
                      </td>
                      <td>
                        <button
                          type="button"
                          onClick={() => fill(item)}
                          className="text-[12px] text-accent hover:underline"
                        >
                          带入
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
          title="我的计划"
          meta={
            <span className="num">
              {loading ? '加载中…' : plansError ? '—' : `${rows.length} 条`}
            </span>
          }
          delay={100}
        >
          {plansError ? (
            // 失败态：明确说「是接口出错、不是没有计划」，别让用户以为记录被删了
            <div className="px-4 py-10 text-center text-[14px] text-danger">
              计划取数失败：{plansError}
              <div className="mt-1 text-[12px] text-fg-dim">
                这是接口出错，不是「你还没有计划」；刷新页面重试
              </div>
            </div>
          ) : rows.length === 0 ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              {loading ? '加载中…' : '还没有计划 —— 从上面写一条，或从候选里带入'}
            </div>
          ) : (
            <div className="overflow-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <th className="!text-left">计划日</th>
                    <th className="!text-left">股票</th>
                    <th>买点</th>
                    <th>放弃位</th>
                    <th className="!text-left">结果</th>
                    <th>当日收盘</th>
                    <th>次日</th>
                    <th>3 日</th>
                    <th>5 日</th>
                    <th className="!text-left">备注</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.id}>
                      <td className="!text-left">
                        <span className="num">{row.plan_date}</span>
                      </td>
                      <td className="!text-left">
                        <StockLink code={row.code}>{row.name ?? row.code}</StockLink>
                        <div className="num text-[11px] text-fg-dim">{row.code}</div>
                      </td>
                      <td>
                        <span className="num">{fmtNum(row.buy_price, 2)}</span>
                      </td>
                      <td>
                        <span className="num">{fmtNum(row.stop_price, 2)}</span>
                      </td>
                      <td className="!text-left">
                        <span className={`text-[12px] ${statusClass(row.status)}`}>
                          {row.status}
                        </span>
                        {row.status === "触发" && row.fill_price != null && (
                          <div className="num text-[11px] text-fg-dim">
                            成交 {fmtNum(row.fill_price, 2)}
                          </div>
                        )}
                        {row.high != null && (
                          <div className="num text-[11px] text-fg-dim">
                            高 {fmtNum(row.high, 2)} / 低 {fmtNum(row.low, 2)}
                          </div>
                        )}
                      </td>
                      <td>
                        <span className={`num ${toneOf(row.day_close_pct)}`}>
                          {fmtPct(row.day_close_pct)}
                        </span>
                      </td>
                      {[row.next_1d_pct, row.next_3d_pct, row.next_5d_pct].map(
                        (value, index) => (
                          <td key={index}>
                            <span className={`num ${toneOf(value)}`}>{fmtPct(value)}</span>
                          </td>
                        ),
                      )}
                      <td className="!text-left text-[12px] text-fg-dim">
                        <span title={row.note ?? undefined}>
                          {row.note
                            ? `${row.note.slice(0, 16)}${row.note.length > 16 ? '…' : ''}`
                            : '—'}
                        </span>
                      </td>
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
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="px-4 py-3 text-[12px] leading-relaxed text-fg-dim">
            次日 / 3 日 / 5 日是**触发后**从买点起算的收盘收益；还没走到对应交易日时显示
            「—」，不按 0% 编造。没触发或直接破位的计划不计算持有收益。
          </p>
        </Panel>
      </div>
    </Layout>
  )
}
