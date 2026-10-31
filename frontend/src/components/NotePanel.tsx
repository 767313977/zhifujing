import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { NoteSummary } from '../api/types'
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
  mainlines: string
  mistakeTags: string[]
  goodTags: string[]
  watchPoints: string
}

/** 固定候选（仍允许自由输入主线）；与后端只做去重/截断，不校验词表 */
const MISTAKE_TAGS = ['追高', '接力失败', '情绪误判', '违背计划', '其它']
const GOOD_TAGS = ['按计划', '及时止损', '等得到位', '仓位合适', '其它']

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
    const list = (value: unknown) =>
      Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
    return {
      marketView: typeof parsed.marketView === 'string' ? parsed.marketView : '',
      nextPlan: typeof parsed.nextPlan === 'string' ? parsed.nextPlan : '',
      mainlines: typeof parsed.mainlines === 'string' ? parsed.mainlines : '',
      mistakeTags: list(parsed.mistakeTags),
      goodTags: list(parsed.goodTags),
      watchPoints: typeof parsed.watchPoints === 'string' ? parsed.watchPoints : '',
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

function sameDraft(a: Draft, b: Draft): boolean {
  return (
    a.marketView === b.marketView &&
    a.nextPlan === b.nextPlan &&
    a.mainlines === b.mainlines &&
    a.watchPoints === b.watchPoints &&
    a.mistakeTags.join('|') === b.mistakeTags.join('|') &&
    a.goodTags.join('|') === b.goodTags.join('|')
  )
}

/** "机器人、算力, 机器人" → ["机器人","算力"]（去重、去空） */
function splitTags(text: string): string[] {
  const result: string[] = []
  for (const item of text.split(/[,，、;；\s]+/)) {
    const tag = item.trim()
    if (tag && !result.includes(tag)) result.push(tag)
  }
  return result
}

function toggleTag(list: string[], tag: string): string[] {
  return list.includes(tag) ? list.filter((item) => item !== tag) : [...list, tag]
}

/**
 * 每日复盘笔记：市场观点 + 结构化标签 + 次日计划。
 *
 * 放在首页最底部——复盘的动作顺序就是「看数据 → 下结论 → 定明天的计划」，
 * 笔记是这条链路的收尾。结构化字段只是为了月末能统计「哪类错犯得最多」，
 * 自由文本仍保留、仍然优先。
 */
export default function NotePanel({ tradeDate, delay = 420 }: NotePanelProps) {
  const { me } = useAuth()
  /** 草稿按用户名隔离（见 `draftKey`）；拿不到用户时用一个公共前缀 */
  const key = draftKey(me?.username ?? 'anon', tradeDate)
  const [marketView, setMarketView] = useState('')
  const [nextPlan, setNextPlan] = useState('')
  const [mainlines, setMainlines] = useState('')
  const [mistakeTags, setMistakeTags] = useState<string[]>([])
  const [goodTags, setGoodTags] = useState<string[]>([])
  const [watchPoints, setWatchPoints] = useState('')
  const [saved, setSaved] = useState<Draft | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [summary, setSummary] = useState<NoteSummary | null>(null)
  /** 切回来时从本地草稿恢复过内容（页脚要说明，否则用户会以为是服务端存的） */
  const [restored, setRestored] = useState(false)
  /**
   * 已经**载入完成**的那一天的 key。写草稿前必须确认它等于当前 key（2026-10-22 复查 P1）：
   * 切日期的一瞬间 `key` 已经变了，但内容还是上一天的 —— 不拦的话会把上一天未保存的字
   * 写进新一天的草稿，下次打开新一天就被「恢复」成旧日期的内容。
   */
  const [loadedKey, setLoadedKey] = useState<string | null>(null)
  /** 加载失败时不知道服务端已有内容，禁止保存 —— 否则拿空值一存就把当天已写的笔记清掉 */
  const [loadFailed, setLoadFailed] = useState(false)
  // 用户是否已开始编辑。慢网下加载结果晚到，若不管这个就会把先打的字冲掉；
  // 所以加载回来只填「没动过」的内容，动过的保留用户的输入（2026-10-10 修）。
  const edited = useRef(false)

  const draft: Draft = { marketView, nextPlan, mainlines, mistakeTags, goodTags, watchPoints }

  useEffect(() => {
    let cancelled = false
    edited.current = false
    setLoadFailed(false)
    setRestored(false)
    setLoadedKey(null)
    const stored = readDraft(key)
    api
      .note(tradeDate)
      .then((note) => {
        if (cancelled) return
        const server: Draft = {
          marketView: note.market_view ?? '',
          nextPlan: note.next_plan ?? '',
          mainlines: (note.mainlines ?? []).join('、'),
          mistakeTags: note.mistake_tags ?? [],
          goodTags: note.good_tags ?? [],
          watchPoints: note.watch_points ?? '',
        }
        const source =
          stored !== null && !sameDraft(stored, server) ? stored : server
        if (stored !== null && !sameDraft(stored, server)) {
          setRestored(true)
        } else if (stored !== null) {
          clearDraft(key)
        }
        if (!edited.current) {
          setMarketView(source.marketView)
          setNextPlan(source.nextPlan)
          setMainlines(source.mainlines)
          setMistakeTags(source.mistakeTags)
          setGoodTags(source.goodTags)
          setWatchPoints(source.watchPoints)
        }
        setSaved(server)
        setLoadedKey(key)
      })
      .catch((err: Error) => {
        if (cancelled) return
        if (stored !== null) {
          if (!edited.current) {
            setMarketView(stored.marketView)
            setNextPlan(stored.nextPlan)
            setMainlines(stored.mainlines)
            setMistakeTags(stored.mistakeTags)
            setGoodTags(stored.goodTags)
            setWatchPoints(stored.watchPoints)
          }
          setRestored(true)
        }
        setLoadFailed(true)
        setError(err.message)
        setLoadedKey(key)
      })
    return () => {
      cancelled = true
    }
  }, [tradeDate, key])

  // 月末回看：当月标签统计（失败就当没有，不影响写笔记）
  useEffect(() => {
    const month = tradeDate.slice(0, 7)
    if (!/^\d{4}-\d{2}$/.test(month)) {
      setSummary(null)
      return
    }
    let cancelled = false
    api
      .noteSummary(month)
      .then((data) => {
        if (!cancelled) setSummary(data)
      })
      .catch(() => {
        if (!cancelled) setSummary(null)
      })
    return () => {
      cancelled = true
    }
  }, [tradeDate, saved])

  // dirty：加载成功就与基线比；加载失败（saved 仍为 null）时退化成「有内容即视为改动」，
  // 但那种情况下面的保存按钮会被 loadFailed 直接禁用，不会用空值覆盖已有笔记
  const dirty =
    saved === null
      ? marketView !== '' || nextPlan !== '' || mainlines !== '' || watchPoints !== '' ||
        mistakeTags.length > 0 || goodTags.length > 0
      : !sameDraft(draft, saved)

  // 有未保存内容时拦住关闭 / 刷新页面。浏览器只显示它自己的固定文案。
  useEffect(() => {
    if (!dirty) return
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [dirty])

  // 有未保存的改动就落一份草稿；保存成功时清掉。必须等这一天载入完成（见 loadedKey）。
  useEffect(() => {
    if (!dirty || loadedKey !== key) return
    writeDraft(key, draft)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, dirty, loadedKey, marketView, nextPlan, mainlines, mistakeTags, goodTags, watchPoints])

  const save = async () => {
    if (loadFailed) return
    setSaving(true)
    setError(null)
    try {
      await api.saveNote(tradeDate, {
        market_view: marketView.trim() || null,
        next_plan: nextPlan.trim() || null,
        mainlines: splitTags(mainlines),
        mistake_tags: mistakeTags,
        good_tags: goodTags,
        watch_points: watchPoints.trim() || null,
        plan_ids: [],
      })
      setSaved(draft)
      clearDraft(key)
      setRestored(false)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const tagRow = (
    label: string,
    items: string[],
    selected: string[],
    onToggle: (tag: string) => void,
    tone: string,
  ) => (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="text-[12px] text-fg-dim">{label}</span>
      {items.map((tag) => {
        const on = selected.includes(tag)
        return (
          <button
            key={tag}
            type="button"
            aria-pressed={on}
            onClick={() => onToggle(tag)}
            className={`border px-1.5 py-[1px] text-[12px] transition-colors ${
              on ? tone : 'border-line text-fg-muted hover:text-fg'
            }`}
          >
            {tag}
          </button>
        )
      })}
    </div>
  )

  return (
    <>
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}
      <Panel
        title="复盘笔记"
        meta={
          <span className="num">
            {tradeDate}
            {dirty && <span className="ml-3 text-accent">有未保存的修改</span>}
            {!dirty && saved && <span className="ml-3 text-fg-dim">已保存</span>}
          </span>
        }
        delay={delay}
      >
        <div className="grid grid-cols-1 gap-px bg-line-soft lg:grid-cols-2">
          <label className="flex flex-col gap-1.5 bg-ink-900 px-4 py-3">
            <span className="text-[13px] tracking-[0.1em] text-fg-dim">今日市场怎么看</span>
            <textarea
              value={marketView}
              onChange={(event) => {
                edited.current = true
                setMarketView(event.target.value)
              }}
              rows={4}
              maxLength={20000}
              placeholder="例如：缩量退潮，涨停从 89 降到 47，封板率跌破 70%，高位股开始松动"
              className="resize-y border border-line-soft bg-ink-850 px-2.5 py-2 text-[14px] leading-relaxed text-fg outline-none transition-colors placeholder:text-fg-dim focus:border-line"
            />
          </label>
          <label className="flex flex-col gap-1.5 bg-ink-900 px-4 py-3">
            <span className="text-[13px] tracking-[0.1em] text-fg-dim">明天打算怎么做</span>
            <textarea
              value={nextPlan}
              onChange={(event) => {
                edited.current = true
                setNextPlan(event.target.value)
              }}
              rows={4}
              maxLength={20000}
              placeholder="例如：盯 5 板以上的高度能否延续；自选里放量过前高的再看"
              className="resize-y border border-line-soft bg-ink-850 px-2.5 py-2 text-[14px] leading-relaxed text-fg outline-none transition-colors placeholder:text-fg-dim focus:border-line"
            />
          </label>
        </div>
        <div className="space-y-2.5 border-t border-line-soft px-4 py-3">
          <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2">
            <label className="flex flex-col gap-1">
              <span className="text-[12px] text-fg-dim">今日主线（顿号/逗号分隔）</span>
              <input
                value={mainlines}
                onChange={(event) => {
                  edited.current = true
                  setMainlines(event.target.value)
                }}
                placeholder="机器人、算力"
                className="border border-line-soft bg-ink-850 px-2.5 py-1.5 text-[13px] text-fg outline-none placeholder:text-fg-dim focus:border-line"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[12px] text-fg-dim">明日观察点</span>
              <input
                value={watchPoints}
                onChange={(event) => {
                  edited.current = true
                  setWatchPoints(event.target.value)
                }}
                maxLength={5000}
                placeholder="例如：5 板以上能否连板；高开能否站稳"
                className="border border-line-soft bg-ink-850 px-2.5 py-1.5 text-[13px] text-fg outline-none placeholder:text-fg-dim focus:border-line"
              />
            </label>
          </div>
          {tagRow('做错', MISTAKE_TAGS, mistakeTags, (tag) => {
            edited.current = true
            setMistakeTags((prev) => toggleTag(prev, tag))
          }, 'border-danger/60 bg-danger/10 text-danger')}
          {tagRow('做对', GOOD_TAGS, goodTags, (tag) => {
            edited.current = true
            setGoodTags((prev) => toggleTag(prev, tag))
          }, 'border-accent bg-accent/10 text-accent')}
        </div>
        {summary && summary.notes > 0 && (
          <div className="border-t border-line-soft px-4 py-2 text-[12px] text-fg-dim">
            <span className="text-fg-muted">{summary.month} 月汇总</span>：笔记 {summary.notes} 天
            {summary.mainlines.length > 0 &&
              ` · 主线 ${summary.mainlines.slice(0, 4).map((item) => `${item.tag}×${item.count}`).join('、')}`}
            {summary.mistakes.length > 0 &&
              ` · 做错 ${summary.mistakes.slice(0, 4).map((item) => `${item.tag}×${item.count}`).join('、')}`}
            {summary.goods.length > 0 &&
              ` · 做对 ${summary.goods.slice(0, 4).map((item) => `${item.tag}×${item.count}`).join('、')}`}
          </div>
        )}
        <div className="flex items-center justify-between border-t border-line-soft px-4 py-2.5">
          <span className="text-[13px] text-fg-dim">
            {loadFailed
              ? '笔记没能加载出来，为避免覆盖当天已写的内容，暂时不能保存；请刷新重试'
              : restored
                ? '已恢复本地未保存的草稿（点「保存」才会上传到服务器）'
                : '笔记按交易日保存；结构化标签用于月末统计，自由文本照旧'}
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
