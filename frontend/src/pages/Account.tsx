import { useState } from 'react'
import type { FormEvent } from 'react'
import { api } from '../api/client'
import Alert from '../components/Alert'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import { useAuth } from '../lib/auth'

const INPUT =
  'w-full max-w-[280px] border border-line bg-ink-850 px-2.5 py-1.5 text-[14px] text-fg outline-none transition-colors focus:border-fg-dim'
const LABEL = 'mb-1 block text-[13px] text-fg-dim'

/**
 * 账号页：改密码 + 联系站长。
 *
 * 单独一页而不是塞在页脚里的小弹窗 —— 它要三个输入框（旧密码、新密码、确认），
 * 而且改完会踢掉其它设备，值得有一个能写清楚后果的地方。
 *
 * 放在页脚链接进来（见 components/Footer）：会员看不到「数据管理」页，
 * 总得有个地方改密码。
 *
 * **联系站长也放这一页**（2026-09-30 用户要求加联系方式）：先加在登录页，但那等于把
 * 个人微信**公开挂出来**（登录页是唯一不用登录就能打开的页面），用户要求挪到登录后。
 * 这里是最合适的落点 —— `/account` 受 `RequireAuth` 保护，而且**所有会员都到得了**
 * （数据管理页是管理员专属的，会员看不到，放那儿等于没有）。
 */
export default function Account() {
  const { me } = useAuth()
  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [repeat, setRepeat] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (newPassword !== repeat) {
      setError('两次输入的新密码不一致')
      return
    }
    setBusy(true)
    setError(null)
    setDone(null)
    try {
      await api.changePassword(oldPassword, newPassword)
      setDone('密码已改。其它设备上的登录已被踢下线，这台不用重登。')
      setOldPassword('')
      setNewPassword('')
      setRepeat('')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Layout>
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}
      {done && (
        <Alert tone="accent" onClose={() => setDone(null)}>
          {done}
        </Alert>
      )}

      <Panel title="改密码" meta={<span className="num">{me?.username}</span>} delay={40}>
        <form onSubmit={submit} className="space-y-3.5 px-4 py-4">
          <div>
            <label className={LABEL} htmlFor="old">
              当前密码
            </label>
            <input
              id="old"
              type="password"
              value={oldPassword}
              onChange={(event) => setOldPassword(event.target.value)}
              autoComplete="current-password"
              className={INPUT}
              required
            />
          </div>
          <div>
            <label className={LABEL} htmlFor="new">
              新密码
            </label>
            <input
              id="new"
              type="password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              autoComplete="new-password"
              className={INPUT}
              required
            />
            <p className="mt-1 text-[12px] text-fg-dim">至少 8 位</p>
          </div>
          <div>
            <label className={LABEL} htmlFor="repeat">
              再输一次新密码
            </label>
            <input
              id="repeat"
              type="password"
              value={repeat}
              onChange={(event) => setRepeat(event.target.value)}
              autoComplete="new-password"
              className={INPUT}
              required
            />
          </div>
          <button
            type="submit"
            disabled={busy}
            className="border border-line px-4 py-1.5 text-[13px] text-fg-muted transition-colors hover:border-fg-dim hover:text-fg disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy ? '提交中…' : '改密码'}
          </button>
        </form>
        <div className="border-t border-line-soft px-4 py-2.5 text-[13px] leading-relaxed text-fg-dim">
          改完会
          <b className="font-normal text-fg-muted">踢掉其它设备</b>
          上的登录（怀疑密码泄露时这一步才是真正有效的动作），当前这台不用重登。
          忘记密码的话找管理员重置。
        </div>
      </Panel>

      {/* 联系站长（2026-09-30 用户要求）。图放在登录后（这里），不放登录页 ——
          登录页是唯一公开的页面，挂那儿等于把个人微信公开（见文件头说明）。
          二维码必须落在**白底**上：暗色面板上不加白底会扫不出来。 */}
      <Panel
        title="联系站长"
        meta={<span className="text-fg-dim">邀请码 · 报错 · 提需求</span>}
        delay={80}
      >
        <div className="flex flex-wrap items-center gap-4 px-4 py-4">
          <a
            href="/wechat-qr.png"
            target="_blank"
            rel="noreferrer"
            title="点开看原图（放大更好扫）"
            className="shrink-0"
          >
            <img
              src="/wechat-qr.png"
              alt="站长微信二维码"
              className="h-[132px] w-[132px] bg-white"
            />
          </a>
          <div className="min-w-[180px] flex-1 text-[13px] leading-relaxed text-fg-dim">
            <div className="text-fg-muted">微信扫码加我（知白守黑）</div>
            <p className="mt-1">
              要邀请码、发现数据不对、想加点什么，都直接找我说 —— 备注一下是从这个站来的。
            </p>
            <p className="mt-1">点二维码可以看原图，放大更好扫。</p>
          </div>
        </div>
      </Panel>
    </Layout>
  )
}
