import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { useAuth } from '../lib/auth'
import Alert from './Alert'
import Panel from './Panel'

interface NotePanelProps {
  tradeDate: string
  delay?: number
}

interface Draft {
  marketView: string
  nextPlan: string
}

/**
 * 本地草稿（2026-10-22 加）：切交易日 / 关页面前没保存的字，不能就这么没了。
 *
 * 为什么是 localStorage 而不是 sessionStorage：复盘常常先写一半、关掉浏览器，
 * 晚上再回来接着写；sessionStorage 一关就没了。
 * key 里带用户名：同一台电脑换人登录时，别把上一个人的笔记草稿显示给下一个人。
 */
function draftKey(user: string, tradeDate: string): string {
  return `fupan:note-draft:${user}:${tradeDate}`
}

function readDraft(key: string): Draft | null {
  try {
    const raw = window.localStorage.getItem(key)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<Draft>
    return {
      marketView: typeof parsed.marketView === 'string' ? parsed.marketView : '',
      nextPlan: typeof parsed.nextPlan === 'string' ? parsed.nextPlan : '',
    }
  } catch {
    // 隐私模式 / 存进去的是旧格式：当作没有草稿，不能因此让页面挂掉
    return null
  }
}

function writeDraft(key: string, draft: Draft): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(draft))
  } catch {
    // 配额满 / 隐私模式：草稿只是兜底，写不进去不影响保存到服务端
  }
}

function clearDraft(key: string): void {
  try {
    window.localStorage.removeItem(key)
  } catch {
    // 同上
  }
}

/**
 * 每日复盘笔记：市场观点 + 次日计划。
 *
 * 放在首页最底部——复盘的动作顺序就是「看数据 → 下结论 → 定明天的计划」，
 * 笔记是这条链路的收尾。
 */
export default function NotePanel({ tradeDate, delay = 420 }: NotePanelProps) {
  const { me } = useAuth()
  /** 草稿按用户名隔离（见 `draftKey`）；拿不到用户时用一个公共前缀 */
  const key = draftKey(me?.username ?? 'anon', tradeDate)
  const [marketView, setMarketView] = useState('')
  const [nextPlan, setNextPlan] = useState('')
  const [saved, setSaved] = useState<{ marketView: string; nextPlan: string } | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /** 切回来时从本地草稿恢复过内容（页脚要说明，否则用户会以为是服务端存的） */
  const [restored, setRestored] = useState(false)
  /**
   * 已经**载入完成**的那一天的 key。写草稿前必须确认它等于当前 key（2026-10-22 复查 P1）：
   * 切日期的一瞬间 `key` 已经变了，但 `marketView` 还是上一天的、`saved` 也还是上一天的，
   * 于是 `dirty` 仍为 true —— 不拦的话会把**上一天未保存的字**写进新一天的草稿，
   * 下次打开新一天就被「恢复」成旧日期的内容，一点保存就把新一天覆盖掉。
   */
  const [loadedKey, setLoadedKey] = useState<string | null>(null)
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
    setRestored(false)
    setLoadedKey(null)
    const draft = readDraft(key)
    api
      .note(tradeDate)
      .then((note) => {
        if (cancelled) return
        const view = note.market_view ?? ''
        const plan = note.next_plan ?? ''
        // 草稿优先：它是用户自己打的、还没保存的内容。与服务器完全一致时视为
        // 「没有草稿」（保存成功但没清掉的残留），顺手删掉，别每次进来都「恢复」一次。
        if (draft !== null && (draft.marketView !== view || draft.nextPlan !== plan)) {
          if (!editedView.current) setMarketView(draft.marketView)
          if (!editedPlan.current) setNextPlan(draft.nextPlan)
          setRestored(true)
        } else {
          if (draft !== null) clearDraft(key)
          if (!editedView.current) setMarketView(view)
          if (!editedPlan.current) setNextPlan(plan)
        }
        // 基线始终记为服务端内容，dirty 才能如实反映「改了没有」
        setSaved({ marketView: view, nextPlan: plan })
        setLoadedKey(key)
      })
      .catch((err: Error) => {
        if (cancelled) return
        // 服务端读不到时，草稿更要填回去 —— 否则用户看到空的两个框，
        // 再一刷新就以为字丢了（保存按钮仍会被 loadFailed 禁用）
        if (draft !== null && (draft.marketView || draft.nextPlan)) {
          if (!editedView.current) setMarketView(draft.marketView)
          if (!editedPlan.current) setNextPlan(draft.nextPlan)
          setRestored(true)
        }
        setLoadFailed(true)
        setError(err.message)
        // 读不到服务端内容也算「这一天载入过了」：此时草稿（如果恢复了）就是用户
        // 手上唯一的一份，必须让它继续落盘，否则刷新一下真丢了
        setLoadedKey(key)
      })
    return () => {
      cancelled = true
    }
  }, [tradeDate, key])

  // dirty：加载成功就与基线比；加载失败（saved 仍为 null）时退化成「有内容即视为改动」，
  // 但那种情况下面的保存按钮会被 loadFailed 直接禁用，不会用空值覆盖已有笔记
  const dirty =
    saved === null
      ? marketView !== '' || nextPlan !== ''
      : marketView !== saved.marketView || nextPlan !== saved.nextPlan

  // 有未保存内容时拦住关闭 / 刷新页面（2026-10-22 加）。浏览器只显示它自己的固定文案，
  // 这里的返回值改不了那句话；`preventDefault` + `returnValue` 是各浏览器的通用写法。
  useEffect(() => {
    if (!dirty) return
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [dirty])

  // 有未保存的改动就落一份草稿（本地写很小，不必防抖）；保存成功时清掉。
  // 只在 `dirty` 时写：否则「刚载入的服务器内容」也会被存成草稿，
  // 下次进来就得先判断它是不是和服务器一样（见上面那段）。
  // ⚠️ 还必须等**这一天**载入完成（`loadedKey === key`）：切日期的瞬间 key 变了、
  // 内容还是上一天的，那一笔写下去就是「旧日期的字串到新日期」（复查 P1）。
  useEffect(() => {
    if (!dirty || loadedKey !== key) return
    writeDraft(key, { marketView, nextPlan })
  }, [key, marketView, nextPlan, dirty, loadedKey])

  const save = async () => {
    // 加载失败时不知道服务端已有内容，禁止用空值覆盖（按钮已禁用，这里再兜一道）
    if (loadFailed) return
    setSaving(true)
    setError(null)
    try {
      await api.saveNote(tradeDate, marketView, nextPlan)
      setSaved({ marketView, nextPlan })
      clearDraft(key)
      setRestored(false)
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
              : restored
                ? '已恢复本地未保存的草稿（点「保存」才会上传到服务器）'
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
