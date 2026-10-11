/**
 * 登录态。**全站只在这里问一次「我是谁」**（设计见文档 §8.69）。
 *
 * 为什么要有这一层：`RequireAuth` 要知道当前用户，页脚的「用户名 + 退出」和数据管理页
 * 的会员区块也要。如果每个页面各自调 `/api/auth/me`，一次页面加载会多出十几个一样的
 * 请求；更要紧的是「谁负责跳登录页」会散落各处 —— 那种分散的鉴权迟早漏掉一条路。
 *
 * 顺带解决一件事：**会话在使用中失效**（闲置 30 天、或在别处改了密码 / 被管理员停用）。
 * 那时页面已经渲染出来了，`RequireAuth` 不会再跑。所以 api 层遇到 401 会发一个事件，
 * 这里收到就把用户清掉，于是下一次渲染自动跳登录页 —— 不用每个页面各自处理。
 */

import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { api, UNAUTHORIZED_EVENT } from '../api/client'
import type { Me } from '../api/types'
import { clearStockList } from './stockNav'

interface AuthValue {
  /** 当前用户；null = 没登录（或还在确认，见 `checking`） */
  me: Me | null
  /** 首次确认还没结束。**必须区分「没登录」与「还不知道」**，否则刷新页面会闪一下登录页 */
  checking: boolean
  /** 登录 / 注册成功后调用，把用户塞进来，省一次 /auth/me */
  setUser: (me: Me) => void
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null)
  const [checking, setChecking] = useState(true)
  const navigate = useNavigate()

  const setUser = useCallback((user: Me) => {
    setMe(user)
    setChecking(false)
  }, [])

  useEffect(() => {
    let alive = true
    api
      .authMe()
      .then((user) => {
        if (alive) setUser(user)
      })
      .catch(() => {
        // 401（没登录）与网络错误都当作「没登录」：这一层只决定要不要跳登录页，
        // 不该把后端不可用也变成白屏。
        // ⚠️ 网络错误也要能走到这里 —— `authMe` 带 10 秒超时（见 api/client.ts 的
        // AUTH_CHECK_TIMEOUT_MS），否则弱网下这个 promise 永不 settle，下面的
        // `checking` 就永远是 true、RequireAuth 渲染的空白页就永远白着（2026-10-10 修）。
        if (alive) {
          setMe(null)
          setChecking(false)
        }
      })
    return () => {
      alive = false
    }
  }, [setUser])

  useEffect(() => {
    const onUnauthorized = () => setMe(null)
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
  }, [])

  const logout = useCallback(async () => {
    try {
      await api.logout()
    } catch (err) {
      // 后端失败（网断了、后台标签里登出请求 30s 超时…）只记一条：登出是**尽力而为**
      // 的，服务端那次失败不能让 rejection 冒泡成 Unhandled Promise rejection，
      // 也不能挡住下面的本地清理与跳转（不等它、不阻塞）。
      console.warn('登出请求失败，仍按已登出处理：', err)
    }
    // 无论服务端成功与否都把本地状态清掉：目标是「别再显示成已登录」——
    // 会话可能已失效、或请求根本没发出去，不该让用户卡在已登录界面。
    setMe(null)
    // 详情页「← → 切换」用的列表存在 sessionStorage 里，是上一个用户点过的上下文，
    // 登出时清掉，别带给下一个登录的人（2026-10-10 修）
    clearStockList()
    // 未登录时进登录页不能用 push，否则用户按返回又回到需要登录的页面
    navigate('/login', { replace: true })
  }, [navigate])

  return (
    <AuthContext.Provider value={{ me, checking, setUser, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext)
  if (value === null) {
    throw new Error('useAuth 必须在 AuthProvider 内使用')
  }
  return value
}

/**
 * 需要登录的页面用它包一层（在 App.tsx 的路由里）。
 *
 * 未登录时记住原来要去哪（`state.from`），登录后回到那一页 —— 否则从收藏夹点进
 * 某个页面、登完却被扔回首页，还得再找一次。
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { me, checking } = useAuth()
  const location = useLocation()

  if (checking) {
    // 刻意留白而不是「加载中…」：这一步通常几十毫秒，闪一行字比空着更烦。
    // ⚠️ 这个白屏**必须有上限** —— 网络断了时会一直停在这里。上限在 `authMe` 的
    // 10 秒超时上（见 api/client.ts 的 AUTH_CHECK_TIMEOUT_MS）：超了就落进上面的
    // catch、当成「没登录」，于是跳登录页，而不是无限白着（2026-10-10 修）。
    return <div className="min-h-screen" />
  }
  if (me === null) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  }
  return <>{children}</>
}

/**
 * 非管理员落到管理员页面时看到的简单提示页（带回首页链接）。
 *
 * 之前是**静默跳首页**：会员手输 `/settings` 后只会觉得「怎么回家了，站点是不是坏了」。
 * 刻意停在这一页把原因说清楚，而不是弹个 2-3 秒再跳 —— 少一个定时器、也不会读到一半被跳走。
 * 不用 Layout：auth 层引 Layout 会和 UserMenu → useAuth 形成循环依赖，
 * 这个无导航的居中卡片反而最贴合「你走错门了」的场景。
 */
function AdminForbidden() {
  return (
    <div className="flex min-h-screen items-center justify-center px-6">
      <div className="panel w-full max-w-md px-8 py-10 text-center">
        <div className="text-[17px] font-medium text-fg">需要管理员权限</div>
        <p className="mt-2 text-[14px] leading-relaxed text-fg-dim">
          「数据管理」仅对管理员开放，当前账号没有访问权限。
        </p>
        <Link
          to="/"
          className="mt-6 inline-block border border-accent/60 bg-accent/10 px-4 py-2 text-[13px] text-accent transition-colors hover:bg-accent/20"
        >
          返回首页
        </Link>
      </div>
    </div>
  )
}

/**
 * 管理员专属页面（目前只有「数据管理」）。
 *
 * 已登录但不是管理员：给一个明确的「需要管理员权限」提示页（见 AdminForbidden），
 * 不再静默跳首页；未登录的情况交给 RequireAuth，登录后若仍非管理员还是会落到提示页。
 */
export function RequireAdmin({ children }: { children: ReactNode }) {
  const { me, checking } = useAuth()
  // 身份还没确认时与 RequireAuth 保持一致的留白，避免登录页 / 提示页闪一下
  if (checking) return <div className="min-h-screen" />
  if (me !== null && !me.is_admin) return <AdminForbidden />
  return <RequireAuth>{children}</RequireAuth>
}
