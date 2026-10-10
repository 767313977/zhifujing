import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import Alert from './Alert'
import Panel from './Panel'

interface NotePanelProps {
  tradeDate: string
  delay?: number
}

/**
 * 每日复盘笔记：市场观点 + 次日计划。
 *
 * 放在首页最底部——复盘的动作顺序就是「看数据 → 下结论 → 定明天的计划」，
 * 笔记是这条链路的收尾。
 */
export default function NotePanel({ tradeDate, delay = 420 }: NotePanelProps) {
  const [marketView, setMarketView] = useState('')
  const [nextPlan, setNextPlan] = useState('')
  const [saved, setSaved] = useState<{ marketView: string; nextPlan: string } | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /** 加载失败时不知道服务端已有内容，禁止保存 —— 否则拿空值一存就把当天已写的笔记清掉 */
  const [loadFailed, setLoadFailed] = useState(false)
  // 用户是否已开始编辑某一栏。慢网下加载结果晚到，若不管这个就会把先打的字冲掉；
  // 所以加载回来只填「没动过」的那栏，动过的那栏保留用户的输入（2026-10-10 修）。
  const editedView = useRef(false)
  const editedPlan = useRef(false)

  useEffect(() => {
    let cancelled = false
    // 切交易日就重置「已编辑」标记与失败态，让新日期的内容正常载入
    editedView.current = false
    editedPlan.current = false
    setLoadFailed(false)
    api
      .note(tradeDate)
      .then((note) => {
        if (cancelled) return
        const view = note.market_view ?? ''
        const plan = note.next_plan ?? ''
        if (!editedView.current) setMarketView(view)
        if (!editedPlan.current) setNextPlan(plan)
        // 基线始终记为服务端内容，dirty 才能如实反映「改了没有」
        setSaved({ marketView: view, nextPlan: plan })
      })
      .catch((err: Error) => {
        if (cancelled) return
        setLoadFailed(true)
        setError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [tradeDate])

  // dirty：加载成功就与基线比；加载失败（saved 仍为 null）时退化成「有内容即视为改动」，
  // 但那种情况下面的保存按钮会被 loadFailed 直接禁用，不会用空值覆盖已有笔记
  const dirty =
    saved === null
      ? marketView !== '' || nextPlan !== ''
      : marketView !== saved.marketView || nextPlan !== saved.nextPlan

  const save = async () => {
    // 加载失败时不知道服务端已有内容，禁止用空值覆盖（按钮已禁用，这里再兜一道）
    if (loadFailed) return
    setSaving(true)
    setError(null)
    try {
      await api.saveNote(tradeDate, marketView, nextPlan)
      setSaved({ marketView, nextPlan })
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}
      <Panel
        title="复盘笔记"
        meta={
          <span className="num">
            {tradeDate}
            {dirty && <span className="ml-3 text-accent">有未保存的修改</span>}
            {!dirty && saved && (saved.marketView || saved.nextPlan) && (
              <span className="ml-3 text-fg-dim">已保存</span>
            )}
          </span>
        }
        delay={delay}
      >
        <div className="grid grid-cols-1 gap-px bg-line-soft lg:grid-cols-2">
          <label className="flex flex-col gap-1.5 bg-ink-900 px-4 py-3">
            <span className="text-[13px] tracking-[0.1em] text-fg-dim">
              今日市场怎么看
            </span>
            <textarea
              value={marketView}
              onChange={(event) => {
                editedView.current = true
                setMarketView(event.target.value)
              }}
              rows={4}
              maxLength={20000}
              placeholder="例如：缩量退潮，涨停从 89 降到 47，封板率跌破 70%，高位股开始松动"
              className="resize-y border border-line-soft bg-ink-850 px-2.5 py-2 text-[14px] leading-relaxed text-fg outline-none transition-colors placeholder:text-fg-dim focus:border-line"
            />
          </label>
          <label className="flex flex-col gap-1.5 bg-ink-900 px-4 py-3">
            <span className="text-[13px] tracking-[0.1em] text-fg-dim">
              明天打算怎么做
            </span>
            <textarea
              value={nextPlan}
              onChange={(event) => {
                editedPlan.current = true
                setNextPlan(event.target.value)
              }}
              rows={4}
              maxLength={20000}
              placeholder="例如：盯 5 板以上的高度能否延续；自选里放量过前高的再看"
              className="resize-y border border-line-soft bg-ink-850 px-2.5 py-2 text-[14px] leading-relaxed text-fg outline-none transition-colors placeholder:text-fg-dim focus:border-line"
            />
          </label>
        </div>
        <div className="flex items-center justify-between border-t border-line-soft px-4 py-2.5">
          <span className="text-[13px] text-fg-dim">
            {loadFailed
              ? '笔记没能加载出来，为避免覆盖当天已写的内容，暂时不能保存；请刷新重试'
              : '笔记按交易日保存，切到历史日期可回看当天的记录'}
          </span>
          <button
            type="button"
            onClick={() => void save()}
            disabled={saving || !dirty || loadFailed}
            className="border border-accent/60 bg-accent/10 px-4 py-1 text-[13px] text-accent transition-colors hover:bg-accent/20 disabled:cursor-not-allowed disabled:border-line-soft disabled:bg-transparent disabled:text-fg-dim"
          >
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      </Panel>
    </>
  )
}
