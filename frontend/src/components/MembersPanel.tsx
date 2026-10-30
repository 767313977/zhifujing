import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import type { Member } from '../api/types'
import { useAuth } from '../lib/auth'
import { fmtDateTime } from '../lib/format'
import { useSort } from '../lib/sort'
import type { SortSpecs } from '../lib/sort'
import Alert from './Alert'
import Panel from './Panel'
import SortTh from './SortTh'

const INPUT =
  'border border-line bg-ink-850 px-2 py-1 text-[13px] text-fg outline-none transition-colors focus:border-fg-dim'

/** 会员表的排序口径。 */
const MEMBER_SORTS: SortSpecs<Member> = {
  username: { value: (row) => row.username, first: 'asc' },
  role: { value: (row) => (row.is_admin ? '管理员' : '会员'), first: 'asc' },
  invite_code: { value: (row) => row.invite_code, first: 'asc' },
  created_at: { value: (row) => row.created_at },
  last_seen_at: { value: (row) => row.last_seen_at },
}

const NO_MEMBERS: Member[] = []

/**
 * 会员列表（只出现在管理员看得到的「数据管理」页里）。
 *
 * 三件事：看谁注册过、替他重置密码、停用/恢复。
 * - **重置密码**：先小规模不做自助找回，忘了密码由管理员改一个（顺带踢掉他的旧会话）。
 * - **停用而不是删除**：删号会失去「谁用哪个邀请码进来」的链路，而且他写的自选股
 *   与复盘笔记会变成孤儿数据。停用等于立刻踢下线且再也进不来。
 */
export default function MembersPanel() {
  const { me } = useAuth()
  const [members, setMembers] = useState<Member[] | null>(null)
  /**
   * 名单**读取**失败的原因。不能与 `error`（重置密码 / 停用等操作的失败）混用：
   * 列表用 `members === null` 兼作「加载中」，读失败时 members 会一直是 null，
   * 没有这个标志就会永久停在「加载中…」（2026-10-22 体检 #9）。
   */
  const [loadFailed, setLoadFailed] = useState<string | null>(null)
  const [resetFor, setResetFor] = useState<number | null>(null)
  const [resetValue, setResetValue] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // 名单默认保持后端顺序，点表头才重排
  const [memberSort, memberRows] = useSort(members ?? NO_MEMBERS, MEMBER_SORTS, { key: null })

  const load = useCallback(async () => {
    try {
      setMembers(await api.members())
      setLoadFailed(null)
    } catch (err) {
      setLoadFailed(err instanceof Error ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  async function resetPassword(id: number) {
    if (resetValue.length < 8) {
      setError('新密码至少 8 位')
      return
    }
    setBusy(true)
    setError(null)
    setMessage(null)
    try {
      const result = await api.resetMemberPassword(id, resetValue)
      setMessage(`密码已重置，同时踢掉他 ${result.kicked_sessions} 个登录会话`)
      setResetFor(null)
      setResetValue('')
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function toggleDisabled(member: Member) {
    const stopping = member.disabled_at === null
    const hint = stopping
      ? `停用 ${member.username}？他会立刻被踢下线、而且再也登不进来。`
      : `恢复 ${member.username} 的访问？`
    if (!window.confirm(hint)) return
    setError(null)
    setMessage(null)
    try {
      const result = await api.setMemberDisabled(member.id, stopping)
      setMessage(
        stopping
          ? `已停用 ${member.username}，踢掉 ${result.kicked_sessions} 个会话`
          : `已恢复 ${member.username}`,
      )
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  const disabled = members?.filter((item) => item.disabled_at !== null).length ?? 0

  return (
    <Panel
      title="会员"
      meta={
        <span className="num">
          {members === null
            ? '—'
            : `${members.length} 人${disabled ? ` · 已停用 ${disabled}` : ''}`}
        </span>
      }
      delay={260}
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

      {members === null ? (
        <div className="px-4 py-8 text-center text-[14px] text-fg-dim">
          {loadFailed ? (
            // 读失败不能伪装成「加载中…」（2026-10-22 体检 #9）
            <>
              <span className="text-danger">加载失败</span>：{loadFailed}
              <div className="mt-2">
                <button
                  type="button"
                  onClick={() => void load()}
                  className="border border-line px-3 py-1 text-[12px] text-fg-muted transition-colors hover:border-fg-dim hover:text-fg"
                >
                  重试
                </button>
              </div>
            </>
          ) : (
            '加载中…'
          )}
        </div>
      ) : (
        <div className="max-h-[320px] overflow-auto">
          <table className="grid-table">
            <thead>
              <tr>
                <SortTh sortKey="username" align="left" {...memberSort}>
                  用户名
                </SortTh>
                <SortTh sortKey="role" align="left" {...memberSort}>
                  角色
                </SortTh>
                <SortTh sortKey="invite_code" align="left" {...memberSort}>
                  来源邀请码
                </SortTh>
                <SortTh sortKey="created_at" {...memberSort}>
                  注册时间
                </SortTh>
                <SortTh sortKey="last_seen_at" {...memberSort}>
                  最近访问
                </SortTh>
                <th />
              </tr>
            </thead>
            <tbody>
              {memberRows.map((member) => (
                <tr key={member.id}>
                  <td className="!text-left">
                    <span className={member.disabled_at ? 'text-fg-dim line-through' : 'text-fg'}>
                      {member.username}
                    </span>
                    {member.id === me?.id && (
                      <span className="ml-2 text-[12px] text-accent">我</span>
                    )}
                  </td>
                  <td className="!text-left text-fg-muted">
                    {member.is_admin ? '管理员' : '会员'}
                    {member.disabled_at && (
                      <span className="ml-2 text-[12px] text-danger">已停用</span>
                    )}
                  </td>
                  <td className="!text-left num text-fg-dim">
                    {member.invite_code ?? '—'}
                  </td>
                  <td className="num text-fg-dim">{fmtDateTime(member.created_at)}</td>
                  <td className="num text-fg-dim">{fmtDateTime(member.last_seen_at)}</td>
                  <td className="!text-right">
                    {resetFor === member.id ? (
                      <span className="flex items-center justify-end gap-2">
                        <input
                          type="password"
                          value={resetValue}
                          onChange={(event) => setResetValue(event.target.value)}
                          placeholder="新密码（≥8 位）"
                          className={`${INPUT} w-[150px]`}
                          autoFocus
                        />
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => void resetPassword(member.id)}
                          className="text-[12px] text-accent transition-colors hover:text-fg disabled:opacity-40"
                        >
                          确定
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            setResetFor(null)
                            setResetValue('')
                          }}
                          className="text-[12px] text-fg-dim transition-colors hover:text-fg-muted"
                        >
                          取消
                        </button>
                      </span>
                    ) : (
                      <span className="flex items-center justify-end gap-3">
                        <button
                          type="button"
                          onClick={() => {
                            setResetFor(member.id)
                            setResetValue('')
                          }}
                          className="text-[12px] text-fg-muted transition-colors hover:text-fg"
                        >
                          改密码
                        </button>
                        {/* 自己那条不给停用：一键把自己锁在门外，而这时唯一能救你的
                            账号就是你自己（后端也会拒绝，这里只是不给那个按钮） */}
                        {member.id !== me?.id && (
                          <button
                            type="button"
                            onClick={() => void toggleDisabled(member)}
                            className={`text-[12px] transition-colors ${
                              member.disabled_at
                                ? 'text-fg-muted hover:text-fg'
                                : 'text-fg-dim hover:text-danger'
                            }`}
                          >
                            {member.disabled_at ? '恢复' : '停用'}
                          </button>
                        )}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="border-t border-line-soft px-4 py-2.5 text-[13px] leading-relaxed text-fg-dim">
        行情数据是所有人共用的（本来就是同一个市场），
        <b className="font-normal text-fg-muted">按人隔离的只有自选股与复盘笔记</b>
        。停用会立刻踢掉他的登录且无法再进；自选股与笔记会留着，恢复后还在。
      </div>
    </Panel>
  )
}
