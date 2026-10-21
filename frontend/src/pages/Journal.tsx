import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../api/client'
import type {
  PatternMeta,
  TradeJournalIn,
  TradeJournalRow,
  TradeStatsOut,
  TradeStatsRow,
} from '../api/types'
import Alert from '../components/Alert'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import StockLink from '../components/StockLink'
import { fmtNum, fmtPct, toneOf } from '../lib/format'

/** 理由来源。与后端 `services/trade_journal.REASON_TYPES` 一一对应。 */
const REASON_TYPES = [
  { key: 'pattern', label: '形态' },
  { key: 'wudao', label: '悟道池' },
  { key: 'theme', label: '题材' },
  { key: 'other', label: '其他' },
]

/** 悟道六池的 key（与后端 `pattern_track.POOL_KEYS` 一致，只用于筛选下拉项）。 */
const POOL_KEYS = [
  'wudao_sample',
  'wudao_start',
  'wudao_wash2',
  'huabao_early',
  'pile_wash_ready',
  'pile_wash_wash',
]

const REASON_LABELS: Record<string, string> = Object.fromEntries(
  REASON_TYPES.map((item) => [item.key, item.label]),
)

function todayText(): string {
  const now = new Date()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${now.getFullYear()}-${month}-${day}`
}

interface FormState {
  code: string
  name: string
  entryDate: string
  entryPrice: string
  exitDate: string
  exitPrice: string
  position: string
  reasonType: string
  reasonKey: string
  reasonText: string
  planStop: string
  note: string
}

function emptyForm(): FormState {
  return {
    code: '',
    name: '',
    entryDate: todayText(),
    entryPrice: '',
    exitDate: '',
    exitPrice: '',
    position: '',
    reasonType: 'pattern',
    reasonKey: '',
    reasonText: '',
    planStop: '',
    note: '',
  }
}

/** 表单里的一格：标签 + 控件 + 可选提示。 */
function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: ReactNode
}) {
  return (
    <label className="flex min-w-0 flex-col gap-1">
      <span className="text-[12px] text-fg-dim">{label}</span>
      {children}
      {hint && <span className="text-[11px] text-fg-dim">{hint}</span>}
    </label>
  )
}

const INPUT =
  'num w-full border border-line bg-ink-850 px-2 py-[5px] text-[13px] text-fg outline-none placeholder:text-fg-dim focus:border-fg-dim'

/** 止损那一格：把「没设 / 未触发 / 已执行 / 未执行」说清楚，不用看数字猜。 */
function stopText(row: TradeJournalRow): { text: string; className: string; title: string } {
  if (row.plan_stop == null) return { text: '—', className: 'text-fg-dim', title: '没设计划止损位' }
  if (row.stop_hit == null) return { text: '—', className: 'text-fg-dim', title: '日线不足，无法判断' }
  if (!row.stop_hit) {
    return {
      text: '未触发',
      className: 'text-fg-muted',
      title: `持仓期间收盘价没有跌破止损位 ${row.plan_stop}`,
    }
  }
  if (row.stop_executed) {
    return {
      text: '已执行',
      className: 'text-ok',
      title: `跌破止损位 ${row.plan_stop} 后，卖出价 ${row.exit_price} 不高于止损位`,
    }
  }
  return {
    text: '未执行',
    className: 'text-danger',
    title: row.open
      ? `已跌破止损位 ${row.plan_stop}，但还没平仓`
      : `跌破止损位 ${row.plan_stop}，但卖出价高于止损位`,
  }
}

/** 一张分组统计表。 */
function StatsTable({ title, rows }: { title: string; rows: TradeStatsRow[] }) {
  return (
    <div className="min-w-0">
      <div className="mb-1.5 text-[12px] text-fg-dim">{title}</div>
      {rows.length === 0 ? (
        <div className="border border-line-soft px-3 py-4 text-center text-[12px] text-fg-dim">
          还没有已平仓的交易
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="grid-table">
            <thead>
              <tr>
                <th className="!text-left">分组</th>
                <th>笔数</th>
                <th>已平 / 未平</th>
                <th>胜率</th>
                <th>平均收益</th>
                <th>平均回撤</th>
                <th>平均持有</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.label}>
                  <td className="!text-left text-fg">{row.label}</td>
                  <td>
                    <span className="num">{row.trades}</span>
                  </td>
                  <td>
                    <span className="num text-fg-dim">
                      {row.closed} / {row.open}
                    </span>
                  </td>
                  <td>
                    <span className="num">
                      {row.win_rate == null ? '—' : `${row.win_rate.toFixed(1)}%`}
                    </span>
                  </td>
                  <td>
                    <span className={`num ${toneOf(row.avg_return)}`}>
                      {fmtPct(row.avg_return)}
                    </span>
                  </td>
                  <td>
                    <span className={`num ${toneOf(row.avg_drawdown)}`}>
                      {fmtPct(row.avg_drawdown)}
                    </span>
                  </td>
                  <td>
                    <span className="num">
                      {row.avg_holding_days == null ? '—' : `${row.avg_holding_days} 天`}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export default function JournalPage() {
  const [rows, setRows] = useState<TradeJournalRow[]>([])
  const [stats, setStats] = useState<TradeStatsOut | null>(null)
  const [catalog, setCatalog] = useState<PatternMeta[]>([])
  const [form, setForm] = useState<FormState>(emptyForm)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [trades, summary] = await Promise.all([
        api.journalTrades(),
        api.journalStats(),
      ])
      setRows(trades.rows)
      setStats(summary)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
    // 理由下拉：形态目录与悟道池共用同一份 catalog（池子也是注册表里的形态）。
    api.patternCatalog().then(setCatalog).catch(() => setCatalog([]))
  }, [load])

  const options = useMemo(() => {
    if (form.reasonType === 'pattern') return catalog
    if (form.reasonType === 'wudao') {
      return catalog.filter((item) => POOL_KEYS.includes(item.key))
    }
    return []
  }, [catalog, form.reasonType])

  const set = (key: keyof FormState, value: string) => {
    setForm((prev) => ({ ...prev, [key]: value }))
  }

  const resetForm = () => {
    setForm(emptyForm())
    setEditingId(null)
  }

  /**
   * 名称框失焦 / 回车时按名称反查代码（2026-10-21 用户要求：先写名字，代码自动带出）。
   *
   * 解析用后端的 `stockResolve`（与个股分析同一套：名称全等 → 拼音 → 模糊唯一）。
   * **解析不出来就静默保持原样** —— 名称本来可以手填，为它弹红条只会打断录入；
   * 拼音重名时同理，用户继续按代码填即可。
   */
  const resolveByName = async () => {
    const key = form.name.trim()
    if (!key) return
    try {
      const hit = await api.stockResolve(key)
      setForm((prev) => ({ ...prev, code: hit.code, name: hit.name ?? prev.name }))
    } catch {
      // 没解析出来：保留用户输入，不打扰
    }
  }

  const submit = async () => {
    if (!form.code.trim() || !form.entryDate || !form.entryPrice) {
      setError('股票代码、买入日、买入价是必填项')
      return
    }
    const payload: TradeJournalIn = {
      code: form.code.trim(),
      name: form.name.trim() || null,
      entry_date: form.entryDate,
      entry_price: Number(form.entryPrice),
      exit_date: form.exitDate || null,
      exit_price: form.exitPrice ? Number(form.exitPrice) : null,
      position_pct: form.position ? Number(form.position) : null,
      reason_type: form.reasonType,
      reason_key: form.reasonKey.trim() || null,
      reason_text: form.reasonText.trim() || null,
      plan_stop: form.planStop ? Number(form.planStop) : null,
      note: form.note.trim() || null,
    }
    setSaving(true)
    setError(null)
    try {
      if (editingId === null) {
        await api.createTrade(payload)
        setNotice('已记下这笔交易')
      } else {
        await api.updateTrade(editingId, payload)
        setNotice('已更新这笔交易')
      }
      resetForm()
      await load()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const startEdit = (row: TradeJournalRow) => {
    setEditingId(row.id)
    setForm({
      code: row.code,
      name: row.name ?? '',
      entryDate: row.entry_date,
      entryPrice: String(row.entry_price),
      exitDate: row.exit_date ?? '',
      exitPrice: row.exit_price == null ? '' : String(row.exit_price),
      position: row.position_pct == null ? '' : String(row.position_pct),
      reasonType: row.reason_type,
      reasonKey: row.reason_key ?? '',
      reasonText: row.reason_text ?? '',
      planStop: row.plan_stop == null ? '' : String(row.plan_stop),
      note: row.note ?? '',
    })
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  const remove = async (row: TradeJournalRow) => {
    if (!window.confirm(`删除 ${row.entry_date} 的 ${row.name ?? row.code} 这笔交易？`)) return
    try {
      await api.deleteTrade(row.id)
      setNotice('已删除')
      if (editingId === row.id) resetForm()
      await load()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  const summary = stats?.summary

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
          title={editingId === null ? '记一笔交易' : `编辑交易 #${editingId}`}
          meta={<span className="num">收益 / 回撤 / 持有天数 / 止损都用日线现算 · 不占配额</span>}
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
            <Field label="名称" hint="填名称后自动带出代码（也认拼音）；留空保存时按代码补">
              <input
                value={form.name}
                onChange={(event) => set('name', event.target.value)}
                onBlur={() => void resolveByName()}
                onKeyDown={(event) => {
                  // 回车 = 立即解析，不用切到别的格子等失焦
                  if (event.key === 'Enter') {
                    event.preventDefault()
                    void resolveByName()
                  }
                }}
                placeholder="贵州茅台 / gzmt"
                className={INPUT}
              />
            </Field>
            <Field label="买入日 *">
              <input
                type="date"
                value={form.entryDate}
                onChange={(event) => set('entryDate', event.target.value)}
                className={INPUT}
              />
            </Field>
            <Field label="买入价 *">
              <input
                type="number"
                step="0.01"
                value={form.entryPrice}
                onChange={(event) => set('entryPrice', event.target.value)}
                placeholder="0.00"
                className={INPUT}
              />
            </Field>

            <Field label="卖出日" hint="未平仓留空">
              <input
                type="date"
                value={form.exitDate}
                onChange={(event) => set('exitDate', event.target.value)}
                className={INPUT}
              />
            </Field>
            <Field label="卖出价">
              <input
                type="number"
                step="0.01"
                value={form.exitPrice}
                onChange={(event) => set('exitPrice', event.target.value)}
                placeholder="0.00"
                className={INPUT}
              />
            </Field>
            <Field label="仓位 %">
              <input
                type="number"
                step="1"
                value={form.position}
                onChange={(event) => set('position', event.target.value)}
                placeholder="20"
                className={INPUT}
              />
            </Field>
            <Field label="计划止损位">
              <input
                type="number"
                step="0.01"
                value={form.planStop}
                onChange={(event) => set('planStop', event.target.value)}
                placeholder="0.00"
                className={INPUT}
              />
            </Field>

            <Field label="理由来源">
              <select
                value={form.reasonType}
                onChange={(event) => {
                  // 切换来源时清空具体理由，避免「形态 key 留在题材里」这种脏数据
                  setForm((prev) => ({
                    ...prev,
                    reasonType: event.target.value,
                    reasonKey: '',
                  }))
                }}
                className={INPUT}
              >
                {REASON_TYPES.map((item) => (
                  <option key={item.key} value={item.key}>
                    {item.label}
                  </option>
                ))}
              </select>
            </Field>
            <Field
              label="具体理由"
              hint={
                form.reasonType === 'theme' || form.reasonType === 'other'
                  ? '直接写题材名 / 一句话'
                  : undefined
              }
            >
              {form.reasonType === 'pattern' || form.reasonType === 'wudao' ? (
                <select
                  value={form.reasonKey}
                  onChange={(event) => set('reasonKey', event.target.value)}
                  className={INPUT}
                >
                  <option value="">请选择</option>
                  {options.map((item) => (
                    <option key={item.key} value={item.key}>
                      {item.name}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  value={form.reasonKey}
                  onChange={(event) => set('reasonKey', event.target.value)}
                  placeholder={form.reasonType === 'theme' ? '如：固态电池' : '如：朋友推荐'}
                  className={INPUT}
                />
              )}
            </Field>
            <Field label="备注" hint="当时的想法 / 执行偏差">
              <input
                value={form.note}
                onChange={(event) => set('note', event.target.value)}
                placeholder="可选"
                className={INPUT}
              />
            </Field>
            <div className="flex items-end gap-2">
              <button
                type="button"
                onClick={() => void submit()}
                disabled={saving}
                className="border border-accent/60 bg-accent/10 px-3 py-[6px] text-[13px] text-accent transition-colors hover:bg-accent/20 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {saving ? '保存中…' : editingId === null ? '保存' : '保存修改'}
              </button>
              {editingId !== null && (
                <button
                  type="button"
                  onClick={resetForm}
                  className="border border-line px-3 py-[6px] text-[13px] text-fg-muted transition-colors hover:border-fg-dim hover:text-fg"
                >
                  取消
                </button>
              )}
            </div>
          </div>
          <p className="px-4 pb-3 text-[12px] leading-relaxed text-fg-dim">
            一行 = 一笔完整回合（暂不支持分批买卖）。持有天数含买入日与卖出日；
            最大回撤是持仓期间<span className="text-fg-muted">收盘价相对买入价</span>
            的最大跌幅；止损按「收盘跌破计划止损位」判定，卖出价不高于止损位才算
            <span className="text-fg-muted">已执行</span>。
          </p>
        </Panel>

        <Panel
          title="我的交易"
          meta={
            <span className="num">
              {loading ? '加载中…' : `${rows.length} 笔`}
              {summary && summary.closed > 0
                ? ` · 已平仓胜率 ${summary.win_rate}% · 平均收益 ${fmtPct(summary.avg_return)}`
                : ''}
            </span>
          }
          delay={70}
        >
          {rows.length === 0 ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              {loading ? '加载中…' : '还没有交易记录 —— 从上面记第一笔开始'}
            </div>
          ) : (
            <div className="overflow-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <th className="!text-left">买入日</th>
                    <th className="!text-left">股票</th>
                    <th className="!text-left">理由</th>
                    <th>情绪</th>
                    <th className="!text-left">板块</th>
                    <th>买入 → 卖出</th>
                    <th>仓位</th>
                    <th>收益</th>
                    <th>最大回撤</th>
                    <th>持有</th>
                    <th>止损</th>
                    <th className="!text-left">备注</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => {
                    const stop = stopText(row)
                    return (
                      <tr key={row.id} title={row.metrics_note ?? undefined}>
                        <td className="!text-left">
                          <span className="num">{row.entry_date}</span>
                        </td>
                        <td className="!text-left">
                          <StockLink code={row.code} className="text-fg">
                            {row.name ?? row.code}
                          </StockLink>
                          <div className="num text-[11px] text-fg-dim">{row.code}</div>
                        </td>
                        <td className="!text-left">
                          <div className="text-fg">{row.reason_label}</div>
                          <div className="text-[11px] text-fg-dim">
                            {REASON_LABELS[row.reason_type] ?? row.reason_type}
                          </div>
                        </td>
                        <td>
                          <span className="text-[12px] text-fg-muted">
                            {row.sentiment_level ?? '—'}
                          </span>
                        </td>
                        <td className="!text-left text-[12px] text-fg-muted">
                          {row.sector ?? '—'}
                        </td>
                        <td>
                          <div className="num text-[12px]">
                            {fmtNum(row.entry_price, 2)}
                            {row.exit_price != null
                              ? ` → ${fmtNum(row.exit_price, 2)}`
                              : ' → 持有中'}
                          </div>
                          <div className="num text-[11px] text-fg-dim">
                            {row.exit_date ?? (row.latest_date ? `最新 ${row.latest_date}` : '—')}
                          </div>
                        </td>
                        <td>
                          <span className="num">
                            {row.position_pct == null ? '—' : `${fmtNum(row.position_pct, 0)}%`}
                          </span>
                        </td>
                        <td>
                          <span className={`num ${toneOf(row.return_pct)}`}>
                            {fmtPct(row.return_pct)}
                          </span>
                          {row.open && (
                            <div className="text-[11px] text-fg-dim">浮动</div>
                          )}
                        </td>
                        <td>
                          <span className={`num ${toneOf(row.max_drawdown_pct)}`}>
                            {fmtPct(row.max_drawdown_pct)}
                          </span>
                        </td>
                        <td>
                          <span className="num">
                            {row.holding_days == null ? '—' : `${row.holding_days} 天`}
                          </span>
                        </td>
                        <td>
                          <span className={`text-[12px] ${stop.className}`} title={stop.title}>
                            {stop.text}
                          </span>
                        </td>
                        <td className="!text-left text-[12px] text-fg-dim">
                          <span title={row.note ?? undefined}>
                            {row.note ? `${row.note.slice(0, 18)}${row.note.length > 18 ? '…' : ''}` : '—'}
                          </span>
                          {row.metrics_note && (
                            <div className="text-[11px] text-danger">{row.metrics_note}</div>
                          )}
                        </td>
                        <td>
                          <div className="flex gap-2 whitespace-nowrap">
                            <button
                              type="button"
                              onClick={() => startEdit(row)}
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
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel
          title="我自己的成绩单"
          meta={
            <span className="num">
              {summary
                ? `共 ${summary.trades} 笔 · 已平仓 ${summary.closed} · 未平仓 ${summary.open}`
                : '—'}
            </span>
          }
          delay={100}
        >
          {!stats ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              {loading ? '加载中…' : '还没有统计数据'}
            </div>
          ) : (
            <div className="space-y-4 px-4 py-3">
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
                {[
                  ['总笔数', String(summary?.trades ?? 0), ''],
                  ['已平仓', String(summary?.closed ?? 0), ''],
                  [
                    '胜率',
                    summary?.win_rate == null ? '—' : `${summary.win_rate}%`,
                    '',
                  ],
                  ['平均收益', fmtPct(summary?.avg_return), toneOf(summary?.avg_return)],
                  [
                    '平均最大回撤',
                    fmtPct(summary?.avg_drawdown),
                    toneOf(summary?.avg_drawdown),
                  ],
                ].map(([label, value, className]) => (
                  <div key={label} className="border border-line-soft px-3 py-2">
                    <div className="text-[11px] text-fg-dim">{label}</div>
                    <div className={`num text-[16px] ${className}`}>{value}</div>
                  </div>
                ))}
              </div>
              <div className="grid gap-4 xl:grid-cols-3">
                <StatsTable title="按买入理由" rows={stats.by_reason} />
                <StatsTable title="按买入时情绪档位" rows={stats.by_sentiment} />
                <StatsTable title="按板块" rows={stats.by_sector} />
              </div>
              <p className="text-[12px] leading-relaxed text-fg-dim">
                只统计<span className="text-fg-muted">已平仓</span>
                的交易；未平仓的浮动盈亏会把「这类票到底赚不赚钱」搅浑，笔数单列在
                「已平 / 未平」里。情绪档位取买入当天的接力温度（冰点 / 偏冷 / 温和 /
                活跃 / 沸腾）；板块取同花顺三级行业路径的末段。
              </p>
            </div>
          )}
        </Panel>
      </div>
    </Layout>
  )
}
