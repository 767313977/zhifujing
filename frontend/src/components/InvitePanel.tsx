import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { Invite } from '../api/types'
import Alert from './Alert'
import Panel from './Panel'
import { copyText } from '../lib/clipboard'
import { fmtDateTime } from '../lib/format'

const INPUT =
  'border border-line bg-ink-850 px-2 py-1 text-[13px] text-fg outline-none transition-colors focus:border-fg-dim'
const BUTTON =
  'border border-line px-3 py-1 text-[12px] text-fg-muted transition-colors hover:border-fg-dim hover:text-fg disabled:cursor-not-allowed disabled:opacity-40'

/**
 * 邀请码管理（只出现在管理员看得到的「数据管理」页里）。
 *
 * 一次性邀请码是这套会员系统的门：没有码连账号都建不出来（比登录限流更硬的边界）。
 * 所以这里的两件事要能看清 —— 「这个码给谁的」（备注）与「这个码用掉了没」（状态）。
 * 已用过的码**不能删**：删了就失去「谁用哪个码进来」这条记录（后端也会拒绝）。
 */
export default function InvitePanel() {
  const [invites, setInvites] = useState<Invite[] | null>(null)
  /**
   * 名单**读取**失败的原因。必须与 `error`（生成 / 删除等操作的失败）分开：
   * 列表用 `invites === null` 兼作「加载中」，读失败时 invites 会一直是 null，
   * 没有这个标志就会永久停在「加载中…」（2026-10-22 体检 #9）。
   */
  const [loadFailed, setLoadFailed] = useState<string | null>(null)
  const [note, setNote] = useState('')
  // 存字符串：数字类型会让输入框清不空（清空瞬间被 `|| 1` 顶回 1）
  const [count, setCount] = useState('1')
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState<string | null>(null)
  // 「已复制」反馈的复原定时器：卸载时清掉，避免对已卸载组件 setState
  const copiedTimer = useRef<number | null>(null)

  useEffect(() => {
    return () => {
      if (copiedTimer.current !== null) window.clearTimeout(copiedTimer.current)
    }
  }, [])

  const load = useCallback(async () => {
    try {
      setInvites(await api.invites())
      setLoadFailed(null)
    } catch (err) {
      setLoadFailed(err instanceof Error ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  /**
   * 复制到剪贴板。实现见 `lib/clipboard.copyText`（Patterns 页导自选股代码也用它）。
   * 这里只负责「已复制」的状态与定时器。
   */
  function copy(code: string) {
    if (!copyText(code)) {
      setError('复制失败：浏览器不允许，手动选中那串码吧')
      return
    }
    setCopied(code)
    // 连点不同邀请码时以最后一次为准，先清旧定时器
    if (copiedTimer.current !== null) window.clearTimeout(copiedTimer.current)
    copiedTimer.current = window.setTimeout(() => {
      copiedTimer.current = null
      setCopied(null)
    }, 1500)
  }

  async function create() {
    setBusy(true)
    setError(null)
    setMessage(null)
    try {
      // 手输 25 也要夹到 20：`max` 属性只约束步进器，不拦手输
      const made = await api.createInvites(
        note.trim(),
        Math.min(Math.max(Number(count) || 1, 1), 20),
      )
      setMessage(
        made.length === 1
          ? `已生成 ${made[0].code}`
          : `已生成 ${made.length} 个：${made.map((item) => item.code).join('、')}`,
      )
      setNote('')
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function remove(code: string) {
    if (!window.confirm(`删掉邀请码 ${code}？`)) return
    setError(null)
    setMessage(null)
    try {
      await api.deleteInvite(code)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  const unused = invites?.filter((item) => item.used_by === null).length ?? 0

  return (
    <Panel
      title="邀请码"
      meta={
        <span className="num">
          {invites === null ? '—' : `${invites.length} 个 · 未用 ${unused}`}
        </span>
      }
      delay={220}
    >
      {error && (
        <div className="px-4 pt-3">
          <Alert onClose={() => setError(null)}>{error}</Alert>
        </div>
      )}
      {message && (
        <div className="px-4 pt-3">
          <Alert tone="accent" onClose={() => setMessage(null)}>
            {message}
          </Alert>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 px-4 py-3">
        <input
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="备注：这个码给谁（可留空）"
          className={`${INPUT} min-w-[200px] flex-1`}
        />
        <input
          type="number"
          min={1}
          max={20}
          value={count}
          onChange={(event) => setCount(event.target.value)}
          className={`num w-[64px] ${INPUT}`}
          title="一次生成几个（最多 20）"
        />
        <button type="button" onClick={() => void create()} disabled={busy} className={BUTTON}>
          {busy ? '生成中…' : '生成'}
        </button>
      </div>

      {invites === null ? (
        <div className="px-4 py-8 text-center text-[14px] text-fg-dim">
          {loadFailed ? (
            // 读失败不能伪装成「加载中…」：那会让管理员一直等一个不会来的结果
            //（2026-10-22 体检 #9）
            <>
              <span className="text-danger">加载失败</span>：{loadFailed}
              <div className="mt-2">
                <button type="button" onClick={() => void load()} className={BUTTON}>
                  重试
                </button>
              </div>
            </>
          ) : (
            '加载中…'
          )}
        </div>
      ) : invites.length === 0 ? (
        <div className="px-4 py-8 text-center text-[14px] text-fg-dim">
          还没有邀请码。生成一个发给要用的人。
        </div>
      ) : (
        <div className="max-h-[320px] overflow-auto">
          <table className="grid-table">
            <thead>
              <tr>
                <th className="!text-left">邀请码</th>
                <th className="!text-left">备注</th>
                <th className="!text-left">状态</th>
                <th className="!text-left">使用者</th>
                <th>创建时间</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {invites.map((item) => (
                <tr key={item.code}>
                  <td className="!text-left">
                    <button
                      type="button"
                      onClick={() => void copy(item.code)}
                      title="点一下复制"
                      className="num text-fg underline decoration-dotted transition-colors hover:text-accent"
                    >
                      {item.code}
                    </button>
                    {copied === item.code && (
                      <span className="ml-2 text-[12px] text-accent">已复制</span>
                    )}
                  </td>
                  <td className="!text-left text-fg-muted">{item.note || '—'}</td>
                  <td className="!text-left">
                    {item.used_by !== null ? (
                      <span className="text-fg-dim">已使用</span>
                    ) : item.disabled_at ? (
                      <span className="text-fg-dim">已停用</span>
                    ) : (
                      <span className="text-ok">未使用</span>
                    )}
                  </td>
                  <td className="!text-left text-fg-muted">
                    {item.used_by_name ?? '—'}
                    {item.used_at && (
                      <span className="ml-2 text-[12px] text-fg-dim">
                        {fmtDateTime(item.used_at)}
                      </span>
                    )}
                  </td>
                  <td className="num text-fg-dim">{fmtDateTime(item.created_at)}</td>
                  <td className="!text-right">
                    {item.used_by === null && (
                      <button
                        type="button"
                        onClick={() => void remove(item.code)}
                        className="text-[12px] text-fg-dim transition-colors hover:text-danger"
                      >
                        删除
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="border-t border-line-soft px-4 py-2.5 text-[13px] leading-relaxed text-fg-dim">
        一个码只能注册一个账号，用完即废。用过的码
        <b className="font-normal text-fg-muted">故意不给删</b>
        —— 删掉会失去「这个账号是拿哪个码进来」的记录。想拦人请去下面的会员列表里
        停用账号。
      </div>
    </Panel>
  )
}
