import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react'
import { api } from '../api/client'
import type {
  JournalReasonSuggestion,
  PatternMeta,
  TradeJournalIn,
  TradeJournalRow,
  TradeStatsOut,
  TradeStatsRow,
  TradeTIn,
  TradeTRow,
  TradeTStatsRow,
} from '../api/types'
import Alert from '../components/Alert'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import StockLink from '../components/StockLink'
import { fmtDrawdown, fmtNum, fmtPct, toneOf } from '../lib/format'

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

/** 做 T 的方向 / 触发类型。与后端 `services.trade_journal` 的常量一一对应。 */
const T_DIRECTIONS = [
  { key: 'long', label: '正T' },
  { key: 'short', label: '倒T' },
]

const T_TRIGGERS = [
  { key: 'oversold', label: '超跌' },
  { key: 'intraday_high', label: '分时冲高' },
  { key: 'theme_fade', label: '题材退潮' },
  { key: 'other', label: '其他' },
]

const T_DIRECTION_LABELS: Record<string, string> = Object.fromEntries(
  T_DIRECTIONS.map((item) => [item.key, item.label]),
)
const T_TRIGGER_LABELS: Record<string, string> = Object.fromEntries(
  T_TRIGGERS.map((item) => [item.key, item.label]),
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
  shares: string
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
    shares: '',
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
                      {fmtDrawdown(row.avg_drawdown)}
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

/** 做 T 子表里「买 → 卖」那一格：缺的那一腿写明按哪天的收盘价算。 */
function tLegText(row: TradeTRow, endDate: string | null, open: boolean): string {
  const end =
    row.end_price == null
      ? '期末价'
      : !open
        ? // 已平仓时期末价就是卖出价（正常情况下已平仓不允许留未完成的 T）
          `按卖出价 ${fmtNum(row.end_price, 2)}`
        : endDate
          ? `按 ${endDate.slice(5)} 收盘 ${fmtNum(row.end_price, 2)}`
          : `按期末 ${fmtNum(row.end_price, 2)}`
  const buy = row.buy_price == null ? `—（${end}）` : fmtNum(row.buy_price, 2)
  const sell = row.sell_price == null ? `—（${end}）` : fmtNum(row.sell_price, 2)
  return `${buy} → ${sell}`
}

interface TFormProps {
  tradeId: number
  code: string
  entryDate: string
  /** 编辑已有的一条时传入；新增为 null */
  initial: TradeTRow | null
  /** 保存前这只票已有的 T id，用来在返回的整行里认出刚新建的那条（取 warnings） */
  knownIds: number[]
  onSaved: (row: TradeJournalRow) => void
  onClose: () => void
}

/** 行内「+ 做T」表单：正T / 倒T、两腿价格与日期、比例、触发。 */
function TForm({
  tradeId,
  code,
  entryDate,
  initial,
  knownIds,
  onSaved,
  onClose,
}: TFormProps) {
  const [direction, setDirection] = useState(initial?.direction ?? 'long')
  const [tradeDate, setTradeDate] = useState(initial?.trade_date ?? todayText())
  // 第二腿默认与第一腿同一天（当日 T）；没做第二腿时价格留空，日期不会提交
  const [leg2Date, setLeg2Date] = useState(
    initial?.leg2_date ?? initial?.trade_date ?? todayText(),
  )
  // 用户手动改过第二腿日期就不再跟随第一腿；编辑一条「隔日 T」时也视为改过
  const [leg2Touched, setLeg2Touched] = useState(
    initial?.leg2_date != null && initial.leg2_date !== initial.trade_date,
  )
  const [buyPrice, setBuyPrice] = useState(
    initial?.buy_price == null ? '' : String(initial.buy_price),
  )
  const [sellPrice, setSellPrice] = useState(
    initial?.sell_price == null ? '' : String(initial.sell_price),
  )
  const [ratio, setRatio] = useState(
    initial
      ? String(initial.ratio_pct)
      : (window.localStorage.getItem('journal.lastTRatio') ?? '33'),
  )
  const [trigger, setTrigger] = useState(initial?.trigger ?? 'other')
  // 用户手动选过触发类型后，不再因日期变化而自动改回「其他」
  const [triggerTouched, setTriggerTouched] = useState(false)
  const [note, setNote] = useState(initial?.note ?? '')
  const [oversoldDev, setOversoldDev] = useState<number | null>(null)
  // 乖离是按哪天的日线算的 —— 只有 T 的日期与它相同才自动预选「超跌」
  const [oversoldDate, setOversoldDate] = useState<string | null>(null)
  // 当天日线的最低价 / 最高价，只用来做 placeholder（软提示，不拦截）
  const [range, setRange] = useState<{ low: number; high: number } | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [warnings, setWarnings] = useState<string[]>([])

  // 今天相对 20 日线的乖离：只有 T 的日期正好是乖离对应的交易日时才默认「超跌」
  useEffect(() => {
    if (initial) return
    let stale = false
    api
      .journalSuggest(code, entryDate)
      .then((data) => {
        if (stale) return
        setOversoldDev(data.oversold_dev)
        setOversoldDate(data.oversold_date)
      })
      .catch(() => {
        /* 回溯不到就不预选，不打扰 */
      })
    return () => {
      stale = true
    }
  }, [code, entryDate, initial])

  /**
   * 触发类型的默认值：只要 T 的日期 **不早于** 乖离对应的日线日，就预选「超跌」。
   * 盘中记 T 时库里的最新日线还是昨天，而「昨收已超跌 → 今天做 T」正是这个信号的
   * 用法；补记更早的 T 才不预选（复查 P2-1）。
   */
  useEffect(() => {
    if (initial || triggerTouched) return
    const usable =
      oversoldDev != null && oversoldDate != null && tradeDate >= oversoldDate
    setTrigger(usable ? 'oversold' : 'other')
  }, [initial, triggerTouched, oversoldDev, oversoldDate, tradeDate])

  useEffect(() => {
    if (!tradeDate) {
      setRange(null)
      return
    }
    let stale = false
    api
      .stockDaily(code, { days: 60 })
      .then((rows) => {
        if (stale) return
        const hit = rows.find((row) => row.trade_date === tradeDate)
        setRange(
          hit && hit.low != null && hit.high != null
            ? { low: hit.low, high: hit.high }
            : null,
        )
      })
      .catch(() => {
        if (!stale) setRange(null)
      })
    return () => {
      stale = true
    }
  }, [code, tradeDate])

  const priceHint = range
    ? `${fmtNum(range.low, 2)} ~ ${fmtNum(range.high, 2)}`
    : '当日成交价'
  // 第二腿 = 正T 的卖出 / 倒T 的买回
  const secondPrice = direction === 'long' ? sellPrice : buyPrice

  const submit = async () => {
    if (direction === 'long' && !buyPrice) {
      setError('正T 要先填买入价')
      return
    }
    if (direction === 'short' && !sellPrice) {
      setError('倒T 要先填卖出价')
      return
    }
    const value = Number(ratio)
    if (!Number.isFinite(value) || value <= 0 || value > 100) {
      setError('比例填 0-100 之间（1/3 仓填 33）')
      return
    }
    const payload: TradeTIn = {
      trade_date: tradeDate,
      leg2_date: secondPrice ? leg2Date || tradeDate : null,
      direction,
      buy_price: buyPrice ? Number(buyPrice) : null,
      sell_price: sellPrice ? Number(sellPrice) : null,
      ratio_pct: value,
      trigger,
      note: note.trim() || null,
    }
    setSaving(true)
    setError(null)
    setWarnings([])
    try {
      const row = initial
        ? await api.updateTradeT(initial.id, payload)
        : await api.createTradeT(tradeId, payload)
      window.localStorage.setItem('journal.lastTRatio', String(value))
      const saved = initial
        ? row.t_ops.find((item) => item.id === initial.id)
        : row.t_ops.find((item) => !knownIds.includes(item.id))
      onSaved(row)
      if (saved && saved.warnings.length > 0) {
        setWarnings(saved.warnings)
      } else {
        onClose()
      }
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const onEnter = (event: ReactKeyboardEvent) => {
    if (event.key === 'Enter') {
      event.preventDefault()
      void submit()
    }
  }

  return (
    <div className="mt-2 border border-line bg-ink-850 px-3 py-2">
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-[12px] text-fg-dim">方向</span>
        <div className="flex">
          {T_DIRECTIONS.map((item) => (
            <button
              key={item.key}
              type="button"
              onClick={() => setDirection(item.key)}
              className={`border px-2 py-[3px] text-[12px] transition-colors ${
                direction === item.key
                  ? 'border-accent/60 bg-accent/10 text-accent'
                  : 'border-line text-fg-muted hover:text-fg'
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
        <span className="text-[11px] text-fg-dim" title="正T = 先买后卖；倒T = 先卖后买">
          ⓘ 正T 先买后卖、倒T 先卖后买
        </span>
        {oversoldDev != null &&
          oversoldDate != null &&
          tradeDate >= oversoldDate && (
            <span className="text-[11px] text-warn">
              {oversoldDate.slice(5)} 收盘乖离 {fmtPct(oversoldDev)}
            </span>
          )}
      </div>
      <div className="mt-2 grid grid-cols-2 gap-2 md:grid-cols-6">
        <Field label="日期">
          <input
            type="date"
            value={tradeDate}
            onChange={(event) => {
              const value = event.target.value
              setTradeDate(value)
              // 没手动改过第二腿日期时跟着第一腿走：补记旧 T 不会悄悄变成隔日 T
              if (!leg2Touched) setLeg2Date(value)
            }}
            onKeyDown={onEnter}
            className={INPUT}
          />
        </Field>
        <Field label="买入价" hint={direction === 'long' ? '第一腿' : '第二腿，没接回留空'}>
          <input
            type="number"
            step="0.01"
            value={buyPrice}
            onChange={(event) => setBuyPrice(event.target.value)}
            onKeyDown={onEnter}
            placeholder={priceHint}
            className={INPUT}
          />
        </Field>
        <Field label="卖出价" hint={direction === 'short' ? '第一腿' : '第二腿，没卖出留空'}>
          <input
            type="number"
            step="0.01"
            value={sellPrice}
            onChange={(event) => setSellPrice(event.target.value)}
            onKeyDown={onEnter}
            placeholder={priceHint}
            className={INPUT}
          />
        </Field>
        <Field label="第二腿日期" hint="第二腿价格留空时忽略">
          <input
            type="date"
            value={leg2Date}
            onChange={(event) => {
              setLeg2Date(event.target.value)
              // 手动改过就不再跟随第一腿（支持隔日 T）
              setLeg2Touched(true)
            }}
            onKeyDown={onEnter}
            className={INPUT}
          />
        </Field>
        <Field label="比例 %" hint="占底仓的比例">
          <input
            type="number"
            step="1"
            value={ratio}
            onChange={(event) => setRatio(event.target.value)}
            onKeyDown={onEnter}
            className={INPUT}
          />
        </Field>
        <Field label="触发">
          <select
            value={trigger}
            onChange={(event) => {
              setTrigger(event.target.value)
              // 手动选过之后，日期变化不再改回去
              setTriggerTouched(true)
            }}
            className={INPUT}
          >
            {T_TRIGGERS.map((item) => (
              <option key={item.key} value={item.key}>
                {item.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="备注">
          <input
            value={note}
            onChange={(event) => setNote(event.target.value)}
            onKeyDown={onEnter}
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
            {saving ? '保存中…' : initial ? '保存修改' : '保存'}
          </button>
          <button
            type="button"
            onClick={onClose}
            className="border border-line px-3 py-[6px] text-[13px] text-fg-muted transition-colors hover:border-fg-dim hover:text-fg"
          >
            取消
          </button>
        </div>
      </div>
      {error && <div className="mt-1.5 text-[12px] text-danger">{error}</div>}
      {warnings.map((text) => (
        <div key={text} className="mt-1 text-[12px] text-warn">
          {text}
        </div>
      ))}
    </div>
  )
}

/** 做 T 的分组表（按方向 / 按触发）。 */
function TStatsTable({ title, rows }: { title: string; rows: TradeTStatsRow[] }) {
  return (
    <div className="min-w-0">
      <div className="mb-1.5 text-[12px] text-fg-dim">{title}</div>
      <div className="overflow-x-auto">
        <table className="grid-table">
          <thead>
            <tr>
              <th className="!text-left">分组</th>
              <th>次数</th>
              <th>已完成 / 未完成</th>
              <th>成功率</th>
              <th>平均扣费后</th>
              <th>累计降本</th>
              <th>卖飞</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.label}>
                <td className="!text-left text-fg">{row.label}</td>
                <td>
                  <span className="num">{row.count}</span>
                </td>
                <td>
                  <span className="num text-fg-dim">
                    {row.completed} / {row.open}
                  </span>
                </td>
                <td>
                  <span className="num">
                    {row.success_rate == null ? '—' : `${row.success_rate.toFixed(1)}%`}
                  </span>
                </td>
                <td>
                  <span className={`num ${toneOf(row.avg_net_pct)}`}>
                    {fmtPct(row.avg_net_pct)}
                  </span>
                </td>
                <td>
                  <span className={`num ${toneOf(row.total_contrib_pct)}`}>
                    {fmtPct(row.total_contrib_pct)}
                  </span>
                </td>
                <td>
                  <span className="num text-fg-dim">
                    {row.fly_count === 0
                      ? '—'
                      : `${row.fly_count} 次 · ${fmtPct(row.avg_fly_pct)}`}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export default function JournalPage() {
  const [rows, setRows] = useState<TradeJournalRow[]>([])
  const [stats, setStats] = useState<TradeStatsOut | null>(null)
  const [catalog, setCatalog] = useState<PatternMeta[]>([])
  const [form, setForm] = useState<FormState>(emptyForm)
  const [editingId, setEditingId] = useState<number | null>(null)
  // 录入表单默认收起（2026-10-21 清单 #10）：做 T 之后页面元素更多，默认摊开会把
  // 「我的交易」挤到首屏之外；点「记一笔」或「编辑」时再展开
  const [formOpen, setFormOpen] = useState(false)
  // 展开做 T 子表的底仓行；同一时间只开一个「+ 做T」/编辑表单
  const [expanded, setExpanded] = useState<Set<number>>(new Set())
  const [tForm, setTForm] = useState<{ tradeId: number; tId: number | null } | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  /**
   * 「我的交易」与「成绩单」各自的取数失败原因。
   *
   * 为什么必须分两个（2026-10-22 体检 #2）：成绩单要重算（按理由 / 情绪档位 / 板块聚合、
   * 再做一遍做 T 统计），是最慢也最容易超时的一块。它和交易列表塞在同一个 `Promise.all`
   * 里时，它一挂 `setRows` 就不执行，页面于是把用户已有的记录显示成
   * 「还没有交易记录」—— 把「接口出错」说成「你的数据没了」。
   */
  const [rowsError, setRowsError] = useState<string | null>(null)
  const [statsError, setStatsError] = useState<string | null>(null)
  // 「买入日那天这只票命中了什么」——按代码 + 买入日回溯（2026-10-21 用户要求）
  const [suggest, setSuggest] = useState<JournalReasonSuggestion | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setRowsError(null)
    setStatsError(null)
    // 两个请求各自转成「数据 + 失败原因」，谁挂了都不影响另一块（见上面 state 处的说明）
    const [tradesResult, statsResult] = await Promise.all([
      api.journalTrades().then(
        (data) => ({ data, error: null as string | null }),
        (err: Error) => ({ data: null, error: err.message }),
      ),
      api.journalStats().then(
        (data) => ({ data, error: null as string | null }),
        (err: Error) => ({ data: null, error: err.message }),
      ),
    ])
    // 只在真拿到数据时覆盖：失败时保留上一份，总比把已有记录清成空表好
    if (tradesResult.data) setRows(tradesResult.data.rows)
    if (statsResult.data) setStats(statsResult.data)
    setRowsError(tradesResult.error)
    setStatsError(statsResult.error)
    setLoading(false)
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

  /**
   * 回溯理由（2026-10-21 用户要求）：填好代码与买入日后，自动查**那天**这只票命中了
   * 哪些形态（库里的扫描结果，零外部请求）。
   *
   * - 只在「具体理由」还空着时自动带入最高分那个 —— 用户手填过就不覆盖；
   * - 最高分是悟道池 → 理由来源自动切「悟道池」，否则切「形态」；
   * - 查不到（那天没跑扫描 / 该票当天没命中）就什么都不填，提示里如实写没有记录。
   */
  useEffect(() => {
    const code = form.code.trim()
    if (!/^\d{6}$/.test(code) || !form.entryDate) {
      setSuggest(null)
      return
    }
    let stale = false
    const timer = window.setTimeout(() => {
      api
        .journalSuggest(code, form.entryDate)
        .then((data) => {
          if (stale) return
          setSuggest(data)
          const best = data.patterns[0]
          if (!best) return
          setForm((prev) => {
            // 只在「形态 / 悟道池」且还没选具体理由时自动带入：用户已经切到「题材 / 其他」
            // 时，哪怕还没打字也不该被切回形态（2026-10-21 复查 #8）
            const fillable = prev.reasonType === 'pattern' || prev.reasonType === 'wudao'
            if (!fillable || prev.reasonKey.trim()) return prev
            return {
              ...prev,
              reasonType: POOL_KEYS.includes(best.key) ? 'wudao' : 'pattern',
              reasonKey: best.key,
            }
          })
        })
        .catch(() => {
          if (!stale) setSuggest(null)
        })
    }, 400) // 400ms：填代码/改日期时不要每按一键就请求
    return () => {
      stale = true
      window.clearTimeout(timer)
    }
  }, [form.code, form.entryDate])

  /** 「具体理由」下方那句：优先说回溯结果，没有回溯数据时才退回原来的说明。 */
  const suggestHint = useMemo(() => {
    if (suggest) {
      if (suggest.patterns.length > 0) {
        const head = suggest.patterns
          .slice(0, 3)
          .map((item) => `${item.name}${item.score}`)
          .join(' / ')
        const more = suggest.patterns.length > 3 ? ` 等 ${suggest.patterns.length} 个` : ''
        return `当日命中：${head}${more}（已自动带入，可改）`
      }
      return suggest.themes.length > 0
        ? `当日没有形态命中；题材：${suggest.themes.join(' / ')}`
        : '当日没有命中记录（那天可能没跑扫描）'
    }
    return form.reasonType === 'theme' || form.reasonType === 'other'
      ? '直接写题材名 / 一句话'
      : undefined
  }, [suggest, form.reasonType])

  const set = (key: keyof FormState, value: string) => {
    setForm((prev) => ({ ...prev, [key]: value }))
  }

  const resetForm = () => {
    setForm(emptyForm())
    setEditingId(null)
    setFormOpen(false)
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
      shares: form.shares ? Number(form.shares) : null,
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
    setFormOpen(true)
    setForm({
      code: row.code,
      name: row.name ?? '',
      entryDate: row.entry_date,
      entryPrice: String(row.entry_price),
      shares: row.shares == null ? '' : String(row.shares),
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

  const toggleExpand = (id: number) => {
    const willOpen = !expanded.has(id)
    setExpanded((prev) => {
      const next = new Set(prev)
      if (willOpen) next.add(id)
      else next.delete(id)
      return next
    })
    // 收起时把这只票的做 T 表单一起关掉，免得下次展开还挂着半截输入
    if (!willOpen && tForm?.tradeId === id) setTForm(null)
  }

  /** 做 T 保存后：只替换这一行，再刷新成绩单（后端已返回整行含 `t_ops`）。 */
  const onTSaved = (row: TradeJournalRow) => {
    setRows((prev) => prev.map((item) => (item.id === row.id ? row : item)))
    api.journalStats().then(setStats).catch(() => {
      /* 成绩单刷新失败不影响这一行 */
    })
  }

  const removeT = async (t: TradeTRow) => {
    if (!window.confirm(`删除 ${t.trade_date} 的这次做 T？`)) return
    try {
      await api.deleteTradeT(t.id)
      setNotice('已删除这次做 T')
      if (tForm?.tId === t.id) setTForm(null)
      await load()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  const summary = stats?.summary

  // 买入金额 = 买入价 × 股数（现算，只用于表单提示与表格展示；后端也会现算一份）
  const priceValue = Number(form.entryPrice)
  const sharesValue = Number(form.shares)
  const entryAmount =
    form.entryPrice &&
    form.shares &&
    Number.isFinite(priceValue) &&
    Number.isFinite(sharesValue) &&
    priceValue > 0 &&
    sharesValue > 0
      ? priceValue * sharesValue
      : null

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
          // 标题里不放数据库 id（`#1` 这种对用户没有意义），改用股票名 / 代码
          title={
            editingId === null
              ? '记一笔交易'
              : `编辑交易 · ${form.name.trim() || form.code || '未命名'}`
          }
          meta={
            <span className="flex items-center gap-3">
              <span className="num">收益 / 回撤 / 持有天数 / 止损都用日线现算 · 不占配额</span>
              <button
                type="button"
                onClick={() => {
                  // 收起 = 放弃这次录入 / 编辑：标题跟着回到「记一笔交易」，
                  // 不然会留下一个收着表单的「编辑交易 · xxx」（2026-10-21）
                  if (formOpen) {
                    resetForm()
                  } else {
                    setFormOpen(true)
                  }
                }}
                className="text-accent hover:underline"
              >
                {formOpen ? '收起' : '记一笔'}
              </button>
            </span>
          }
          delay={40}
        >
          {!formOpen ? (
            <div className="px-4 py-3 text-[12px] text-fg-dim">
              表单默认收起（记完就折上，页面不会被录入框占满）。点右上角「记一笔」展开。
            </div>
          ) : (
            <>
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
            <Field
              label="股数"
              hint={
                entryAmount == null
                  ? '填股数后自动算金额'
                  : `金额 ¥${entryAmount.toLocaleString('zh-CN', {
                      maximumFractionDigits: 2,
                    })}`
              }
            >
              <input
                type="number"
                step="100"
                min="0"
                value={form.shares}
                onChange={(event) => set('shares', event.target.value)}
                placeholder="1000"
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
            <Field label="具体理由" hint={suggestHint}>
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
            </>
          )}
        </Panel>

        <Panel
          title="我的交易"
          meta={
            <span className="num">
              {loading ? '加载中…' : rowsError ? '—' : `${rows.length} 笔`}
              {summary && summary.closed > 0
                ? ` · 已平仓胜率 ${summary.win_rate}% · 平均收益 ${fmtPct(summary.avg_return)}`
                : ''}
            </span>
          }
          delay={70}
        >
          {rowsError ? (
            // 失败态：明确说「是接口出错、不是没有记录」，否则用户会以为数据丢了
            <div className="px-4 py-10 text-center text-[14px] text-danger">
              交易记录取数失败：{rowsError}
              <div className="mt-1 text-[12px] text-fg-dim">
                这是接口出错，不是「你的交易记录没了」；刷新页面重试
              </div>
            </div>
          ) : rows.length === 0 ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              {loading ? '加载中…' : '还没有交易记录 —— 从上面记第一笔开始'}
            </div>
          ) : (
            <div className="overflow-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <th></th>
                    <th className="!text-left">买入日</th>
                    <th className="!text-left">股票</th>
                    <th className="!text-left">理由</th>
                    <th>情绪</th>
                    <th className="!text-left">板块</th>
                    <th>买入 → 卖出</th>
                    <th>股数 / 金额</th>
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
                    // 有 T 记录、或还没平仓时才给展开钮；已平仓又没做过 T 的没有内容可展
                    const canExpand = row.t_count > 0 || row.open
                    const isOpen = expanded.has(row.id)
                    const editingT = tForm?.tradeId === row.id
                    return (
                      <Fragment key={row.id}>
                        <tr title={row.metrics_note ?? undefined}>
                          <td className="!text-left">
                            {canExpand && (
                              <button
                                type="button"
                                onClick={() => toggleExpand(row.id)}
                                title={isOpen ? '收起做 T' : '展开做 T'}
                                className="text-[12px] text-fg-dim hover:text-fg"
                              >
                                {isOpen ? '▾' : '▸'}
                              </button>
                            )}
                          </td>
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
                              {row.exit_date ??
                                (row.latest_close == null
                                  ? '—'
                                  : `最新 ${fmtNum(row.latest_close, 2)}${
                                      row.latest_date ? ` · ${row.latest_date.slice(5)}` : ''
                                    }`)}
                            </div>
                          </td>
                          <td>
                            {row.shares == null ? (
                              <span className="text-fg-dim">—</span>
                            ) : (
                              <>
                                <div className="num">
                                  {row.shares.toLocaleString('zh-CN')} 股
                                </div>
                                {row.entry_amount != null && (
                                  <div className="num text-[11px] text-fg-dim">
                                    ¥
                                    {row.entry_amount.toLocaleString('zh-CN', {
                                      maximumFractionDigits: 2,
                                    })}
                                  </div>
                                )}
                              </>
                            )}
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
                            {row.t_count > 0 && (
                              <div className="num text-[11px] text-fg-dim">
                                含T{' '}
                                <span className={toneOf(row.return_with_t_pct)}>
                                  {fmtPct(row.return_with_t_pct)}
                                </span>
                                {` · T×${row.t_count}`}
                                {row.t_open_count > 0 && (
                                  <span className="text-warn"> · {row.t_open_count} 未完成</span>
                                )}
                              </div>
                            )}
                          </td>
                          <td>
                            <span className={`num ${toneOf(row.max_drawdown_pct)}`}>
                              {fmtDrawdown(row.max_drawdown_pct)}
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
                        {isOpen && (
                          <tr>
                            <td colSpan={15} className="!text-left bg-ink-850">
                              <div className="px-1 py-2">
                                {row.t_ops.length === 0 ? (
                                  <div className="text-[12px] text-fg-dim">
                                    还没有做 T 记录
                                  </div>
                                ) : (
                                  <div className="overflow-x-auto">
                                    <table className="grid-table">
                                      <thead>
                                        <tr>
                                          <th className="!text-left">日期</th>
                                          <th>方向</th>
                                          <th>买 → 卖</th>
                                          <th>比例</th>
                                          <th>价差</th>
                                          <th>扣费后</th>
                                          <th>对底仓</th>
                                          <th>触发</th>
                                          <th className="!text-left">备注</th>
                                          <th></th>
                                        </tr>
                                      </thead>
                                      <tbody>
                                        {row.t_ops.map((item) => (
                                          <tr key={item.id}>
                                            <td className="!text-left num">
                                              {item.leg2_date != null &&
                                              item.leg2_date !== item.trade_date
                                                ? `${item.trade_date} → ${item.leg2_date}`
                                                : item.trade_date}
                                            </td>
                                            <td>
                                              <span className="text-[12px] text-fg-muted">
                                                {T_DIRECTION_LABELS[item.direction] ??
                                                  item.direction}
                                              </span>
                                            </td>
                                            <td className="num text-[12px]">
                                              {tLegText(item, row.latest_date, row.open)}
                                            </td>
                                            <td>
                                              <span className="num">
                                                {fmtNum(item.ratio_pct, 0)}%
                                              </span>
                                            </td>
                                            <td>
                                              {item.pending_close ? (
                                                <span className="text-[11px] text-warn">
                                                  等收盘
                                                </span>
                                              ) : (
                                                <span
                                                  className={`num ${toneOf(item.spread_pct)}`}
                                                >
                                                  {fmtPct(item.spread_pct)}
                                                </span>
                                              )}
                                            </td>
                                            <td>
                                              <span className={`num ${toneOf(item.net_pct)}`}>
                                                {fmtPct(item.net_pct)}
                                              </span>
                                            </td>
                                            <td>
                                              {item.pending_close ? (
                                                <span className="text-[11px] text-warn">
                                                  等收盘
                                                </span>
                                              ) : (
                                                <span
                                                  className={`num ${toneOf(item.contrib_pct)}`}
                                                >
                                                  {fmtPct(item.contrib_pct)}
                                                </span>
                                              )}
                                              {item.fly_pct != null && (
                                                <span className="ml-1 text-[11px] text-warn">
                                                  卖飞 {fmtPct(item.fly_pct)}
                                                </span>
                                              )}
                                            </td>
                                            <td>
                                              <span className="text-[12px] text-fg-muted">
                                                {T_TRIGGER_LABELS[item.trigger] ?? item.trigger}
                                              </span>
                                            </td>
                                            <td className="!text-left text-[12px] text-fg-dim">
                                              {item.note ?? '—'}
                                              {item.warnings.map((text) => (
                                                <div key={text} className="text-[11px] text-warn">
                                                  {text}
                                                </div>
                                              ))}
                                            </td>
                                            <td>
                                              <div className="flex gap-2 whitespace-nowrap">
                                                <button
                                                  type="button"
                                                  onClick={() =>
                                                    setTForm({ tradeId: row.id, tId: item.id })
                                                  }
                                                  className="text-[12px] text-accent hover:underline"
                                                >
                                                  编辑
                                                </button>
                                                <button
                                                  type="button"
                                                  onClick={() => void removeT(item)}
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
                                {editingT ? (
                                  <TForm
                                    key={`${row.id}-${tForm?.tId ?? 'new'}`}
                                    tradeId={row.id}
                                    code={row.code}
                                    entryDate={row.entry_date}
                                    initial={
                                      tForm?.tId == null
                                        ? null
                                        : (row.t_ops.find((item) => item.id === tForm.tId) ??
                                          null)
                                    }
                                    knownIds={row.t_ops.map((item) => item.id)}
                                    onSaved={onTSaved}
                                    onClose={() => setTForm(null)}
                                  />
                                ) : (
                                  <button
                                    type="button"
                                    onClick={() => setTForm({ tradeId: row.id, tId: null })}
                                    className="mt-2 text-[12px] text-accent hover:underline"
                                  >
                                    + 做T
                                  </button>
                                )}
                              </div>
                            </td>
                          </tr>
                        )}
                      </Fragment>
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
          {statsError ? (
            // 成绩单失败只影响这一块：上面的「我的交易」照常显示（2026-10-22 体检 #2）
            <div className="px-4 py-10 text-center text-[14px] text-danger">
              成绩单取数失败：{statsError}
              <div className="mt-1 text-[12px] text-fg-dim">
                你的交易记录照常显示，只是这份统计没算出来；刷新页面重试
              </div>
            </div>
          ) : !stats ? (
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
                    fmtDrawdown(summary?.avg_drawdown),
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

              <div className="border-t border-line-soft pt-4">
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <span className="text-[13px] text-fg">做 T 成绩</span>
                  <span className="text-[11px] text-fg-dim">
                    降本按各自底仓的买入价折算 · 跨票相加仅供参考
                  </span>
                </div>
                {stats.t_stats.summary.count === 0 ? (
                  <div className="text-[12px] text-fg-dim">
                    还没有做 T 记录：在持仓行点 ▸ 展开后记
                  </div>
                ) : (
                  <div className="space-y-3">
                    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
                      {[
                        [
                          '次数（已完成 / 未完成）',
                          `${stats.t_stats.summary.completed} / ${stats.t_stats.summary.open}`,
                          '',
                        ],
                        [
                          '成功率',
                          stats.t_stats.summary.success_rate == null
                            ? '—'
                            : `${stats.t_stats.summary.success_rate}%`,
                          '',
                        ],
                        [
                          '平均扣费后价差',
                          fmtPct(stats.t_stats.summary.avg_net_pct),
                          toneOf(stats.t_stats.summary.avg_net_pct),
                        ],
                        [
                          '累计降本',
                          fmtPct(stats.t_stats.summary.total_contrib_pct),
                          toneOf(stats.t_stats.summary.total_contrib_pct),
                        ],
                        ['卖飞次数', `${stats.t_stats.summary.fly_count} 次`, ''],
                        [
                          '卖飞后平均涨幅',
                          fmtPct(stats.t_stats.summary.avg_fly_pct),
                          toneOf(stats.t_stats.summary.avg_fly_pct),
                        ],
                      ].map(([label, value, className]) => (
                        <div key={label} className="border border-line-soft px-3 py-2">
                          <div className="text-[11px] text-fg-dim">{label}</div>
                          <div className={`num text-[16px] ${className}`}>{value}</div>
                        </div>
                      ))}
                    </div>
                    <div className="grid gap-4 xl:grid-cols-2">
                      <TStatsTable title="按方向" rows={stats.t_stats.by_direction} />
                      <TStatsTable
                        title="按触发"
                        // 没有记录的触发类型不占一行（这里只在有 T 时渲染，至少剩 1 行）
                        rows={stats.t_stats.by_trigger.filter((row) => row.count > 0)}
                      />
                    </div>
                    <p className="text-[12px] leading-relaxed text-fg-dim">
                      成功 = 扣掉佣金万 2.5×2 与印花税 0.05% 后价差为正；没记股数，未计最低 5
                      元。卖飞 = 倒T 没接回，按期末价算之后又涨了多少；未完成的 T
                      按期末价计贡献，但不进成功率的分母。
                    </p>
                  </div>
                )}
              </div>
            </div>
          )}
        </Panel>
      </div>
    </Layout>
  )
}
