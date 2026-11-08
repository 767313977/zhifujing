import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import Alert from '../components/Alert'
import ContactQr from '../components/ContactQr'
import Footer from '../components/Footer'
import { useAuth } from '../lib/authContext'

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
  const { me, setUser, logoutPending, retryLogout } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  // 被 RequireAuth 拦下来时会带上原来要去的地址，登完直接回去
  const state = location.state as { from?: string; logoutFailed?: boolean } | null
  const from = state?.from ?? '/'

  const [tab, setTab] = useState<Tab>('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [inviteCode, setInviteCode] = useState('')
  // 上一次登出请求没送达服务端（断网 / 超时 / 5xx）：HttpOnly cookie 只能由服务端删，
  // 那意味着**服务端会话可能还活着**。必须如实提示，不能显示成「已安全退出」
  // （2026-11-08 第二轮复审 P1-NEW-02）。
  const [logoutWarning, setLogoutWarning] = useState(Boolean(state?.logoutFailed))
  const [retrying, setRetrying] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    // 刷新后标记仍在（Provider 已静默重试过）：照样把警告显示出来，别因为
    // 路由 state 丢了就静默。
    if (logoutPending) setLogoutWarning(true)
  }, [logoutPending])

  if (me !== null) return <Navigate to={from} replace />

  async function retry() {
    setRetrying(true)
    const revoked = await retryLogout()
    setRetrying(false)
    // 只有服务端真的撤销了才撤掉警告
    setLogoutWarning(!revoked)
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    // 注册的密码长度客户端就能判，不合规则不发请求（后端 400 仍会兜底，这里省一次
    // 无谓请求 + 反馈更快）。登录不校验：老账号的密码多短也是它
    if (tab === 'register' && password.length < 8) {
      setError('密码至少 8 位')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const user =
        tab === 'login'
          ? await api.login(username.trim(), password)
          : await api.register(inviteCode.trim(), username.trim(), password)
      setUser(user)
      // 登录成功 = 新会话已建立，旧的待撤销状态不再成立（setUser 内部也会清标记）
      setLogoutWarning(false)
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

            {logoutWarning && (
              <Alert onClose={() => setLogoutWarning(false)}>
                <div className="space-y-2">
                  <div>
                    上次登出没有完成：<b>本地界面已退出，但服务端会话可能仍然有效</b>
                    。关闭浏览器不一定清除持久化 Cookie。
                  </div>
                  <div>
                    请恢复网络后重试注销；如果这是公共设备，也可以用浏览器的「清除网站数据」
                    手动删除该站点的 Cookie。
                  </div>
                  <button
                    type="button"
                    onClick={() => void retry()}
                    disabled={retrying}
                    className="border border-danger/40 px-2.5 py-1 text-[13px] transition-colors hover:bg-danger/10 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {retrying ? '重试中…' : '重试注销'}
                  </button>
                </div>
              </Alert>
            )}

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
                    maxLength={64}
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
                  maxLength={32}
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
                  maxLength={128}
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

          {/* 联系方式**只在「注册」这一页显示**（2026-09-30 用户要求，原话「我本来就是让想
              注册的人联系我」）：想注册的人正是卡在「要邀请码」这一步，所以只在注册 tab 给
              联系方式，登录 tab 不给（已经有账号的人不需要）。
              ⚠️ 因此它在**未登录**时也可见 —— 这是**用户明确要的**，别再「顺手挪走」。 */}
          {tab === 'register' && (
            <div className="mt-4 flex items-center gap-3 border border-line-soft px-3 py-2.5">
              <ContactQr size={104} />
              <div className="text-[12px] leading-relaxed text-fg-dim">
                <div className="text-[13px] text-fg-muted">还没邀请码？</div>
                <p className="mt-1">微信扫码加我（知白守黑），说明来意即可。</p>
                <p className="mt-1">点二维码可以看原图，放大更好扫。</p>
              </div>
            </div>
          )}
        </div>
      </div>

      <Footer />
    </div>
  )
}
