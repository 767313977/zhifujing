import { useEffect, useState } from 'react'
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

  useEffect(() => {
    let cancelled = false
    api
      .note(tradeDate)
      .then((note) => {
        if (cancelled) return
        setMarketView(note.market_view ?? '')
        setNextPlan(note.next_plan ?? '')
        setSaved({ marketView: note.market_view ?? '', nextPlan: note.next_plan ?? '' })
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [tradeDate])

  // 加载失败时 saved 保持 null，此时「有内容」就允许保存 —— 否则首次加载一失败，
  // dirty 恒为 false，保存按钮永远禁用，用户当天没法写笔记（除非刷新重来）
  const dirty =
    saved === null
      ? marketView !== '' || nextPlan !== ''
      : marketView !== saved.marketView || nextPlan !== saved.nextPlan

  const save = async () => {
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
            <span className="text-[11px] tracking-[0.1em] text-fg-dim">
              今日市场怎么看
            </span>
            <textarea
              value={marketView}
              onChange={(event) => setMarketView(event.target.value)}
              rows={4}
              placeholder="例如：缩量退潮，涨停从 89 降到 47，封板率跌破 70%，高位股开始松动"
              className="resize-y border border-line-soft bg-ink-850 px-2.5 py-2 text-[13px] leading-relaxed text-fg outline-none transition-colors placeholder:text-fg-dim focus:border-line"
            />
          </label>
          <label className="flex flex-col gap-1.5 bg-ink-900 px-4 py-3">
            <span className="text-[11px] tracking-[0.1em] text-fg-dim">
              明天打算怎么做
            </span>
            <textarea
              value={nextPlan}
              onChange={(event) => setNextPlan(event.target.value)}
              rows={4}
              placeholder="例如：盯 5 板以上的高度能否延续；自选里放量过前高的再看"
              className="resize-y border border-line-soft bg-ink-850 px-2.5 py-2 text-[13px] leading-relaxed text-fg outline-none transition-colors placeholder:text-fg-dim focus:border-line"
            />
          </label>
        </div>
        <div className="flex items-center justify-between border-t border-line-soft px-4 py-2.5">
          <span className="text-[11px] text-fg-dim">
            笔记按交易日保存，切到历史日期可回看当天的记录
          </span>
          <button
            type="button"
            onClick={() => void save()}
            disabled={saving || !dirty}
            className="border border-accent/60 bg-accent/10 px-4 py-1 text-[12px] text-accent transition-colors hover:bg-accent/20 disabled:cursor-not-allowed disabled:border-line-soft disabled:bg-transparent disabled:text-fg-dim"
          >
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      </Panel>
    </>
  )
}
