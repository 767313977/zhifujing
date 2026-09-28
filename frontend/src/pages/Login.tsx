import { useState } from 'react'
import type { FormEvent } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import Alert from '../components/Alert'
import Footer from '../components/Footer'
import { useAuth } from '../lib/auth'

type Tab = 'login' | 'register'

const TABS: { key: Tab; label: string }[] = [
  { key: 'login', label: '登录' },
  { key: 'register', label: '注册' },
]

const INPUT =
  'w-full border border-line bg-ink-850 px-2.5 py-2 text-[14px] text-fg outline-none transition-colors focus:border-fg-dim'
const LABEL = 'mb-1 block text-[12px] text-fg-dim'

/**
 * 登录 / 注册。**全站唯一不需要登录就能打开的页面**（见设计文档 §8.69）。
 *
 * 注册要邀请码 —— 这是整套系统的门：没有码连账号都建不出来（比限流更硬的边界）。
 */
export default function Login() {
  const { me, setUser } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  // 被 RequireAuth 拦下来时会带上原来要去的地址，登完直接回去
  const from = (location.state as { from?: string } | null)?.from ?? '/'

  const [tab, setTab] = useState<Tab>('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [inviteCode, setInviteCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // 已登录就不用看这一页了（例如按了浏览器后退）
  if (me !== null) return <Navigate to={from} replace />

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const user =
        tab === 'login'
          ? await api.login(username.trim(), password)
          : await api.register(inviteCode.trim(), username.trim(), password)
      setUser(user)
      // replace：别让「后退」回到登录页
      navigate(from, { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen flex-col">
      <div className="flex flex-1 items-center justify-center px-4 py-10">
        <div className="w-full max-w-[380px]">
          <div className="mb-6 text-center">
            <div className="text-[20px] font-semibold tracking-[0.1em] text-fg">
              致富经
            </div>
            <div className="num mt-1 text-[12px] tracking-[0.28em] text-fg-dim">
              ZHIFUJING
            </div>
          </div>

          <div className="panel rise p-5">
            {/* 切换 tab 时清掉错误与密码：留着上一次的错误会让人以为这次也失败了 */}
            <div className="mb-5 flex gap-5 border-b border-line-soft">
              {TABS.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  onClick={() => {
                    setTab(item.key)
                    setError(null)
                    setPassword('')
                  }}
                  className={`relative -mb-px pb-2 text-[14px] transition-colors ${
                    tab === item.key
                      ? 'text-fg'
                      : 'text-fg-dim hover:text-fg-muted'
                  }`}
                >
                  {item.label}
                  {tab === item.key && (
                    <span className="absolute inset-x-0 bottom-0 h-[2px] bg-accent" />
                  )}
                </button>
              ))}
            </div>

            {error && <Alert onClose={() => setError(null)}>{error}</Alert>}

            <form onSubmit={submit} className="space-y-3.5">
              {tab === 'register' && (
                <div>
                  <label className={LABEL} htmlFor="invite">
                    邀请码
                  </label>
                  <input
                    id="invite"
                    value={inviteCode}
                    onChange={(event) => setInviteCode(event.target.value.toUpperCase())}
                    autoComplete="off"
                    spellCheck={false}
                    placeholder="站长给你的那串"
                    className={`num ${INPUT}`}
                    required
                  />
                </div>
              )}

              <div>
                <label className={LABEL} htmlFor="username">
                  用户名
                </label>
                <input
                  id="username"
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  autoComplete="username"
                  spellCheck={false}
                  className={INPUT}
                  required
                />
              </div>

              <div>
                <label className={LABEL} htmlFor="password">
                  密码
                </label>
                <input
                  id="password"
                  type="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  // 注册是新密码（让浏览器/密码管理器提议一个），登录是已有密码
                  autoComplete={tab === 'login' ? 'current-password' : 'new-password'}
                  className={INPUT}
                  required
                />
                {tab === 'register' && (
                  <p className="mt-1 text-[12px] text-fg-dim">至少 8 位</p>
                )}
              </div>

              <button
                type="submit"
                disabled={busy}
                className="w-full border border-accent/40 bg-accent/[0.06] py-2 text-[14px] text-fg transition-colors hover:bg-accent/[0.12] disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy ? '请稍候…' : tab === 'login' ? '登录' : '注册并进入'}
              </button>
            </form>
          </div>

          <p className="mt-4 text-center text-[12px] leading-relaxed text-fg-dim">
            {tab === 'register'
              ? '一个邀请码只能注册一个账号。'
              : '只有受邀的人才能注册 —— 需要邀请码就找站长要。'}
          </p>
        </div>
      </div>

      <Footer />
    </div>
  )
}
