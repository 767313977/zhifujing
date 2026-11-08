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

import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { api, UNAUTHORIZED_EVENT } from '../api/client'
import type { Me } from '../api/types'
import {
  AUTH_EVENT_KEY,
  AuthContext,
  clearLogoutInFlight,
  clearPendingLogout,
  hasPendingLogout,
  LOGOUT_PENDING_KEY,
  markLogoutInFlight,
  markLogoutPending,
  publishAuthEvent,
  useAuth,
  waitForNoLogoutInFlight,
} from './authContext'
import type { AuthEvent } from './authContext'
import { clearStockList } from './stockNav'

// context / 类型 / `useAuth` 都在 `lib/authContext.ts`（拆开是为了 Fast Refresh）。
// 这里只放**组件**：Provider 与两个路由守卫。**不要再从这里导出钩子** ——
// 组件与钩子同文件导出会让整文件的热更新退化成整页重载。

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null)
  const [checking, setChecking] = useState(true)
  const navigate = useNavigate()
  /**
   * `me` 的同步副本。
   *
   * 为什么需要（2026-10-28 体检）：首屏那次 `/auth/me` 可能很慢（弱网 / 10 秒超时）。
   * 用户在这期间完成登录后，迟到的 **401** 会落进下面的 catch 把 `me` 清掉 ——
   * 刚登录成功的人立刻被踢回登录页。用 ref 记「已经有人登录过」，晚到的初始结果一律忽略。
   */
  const userRef = useRef<Me | null>(null)
  /**
   * 当前标签页正在进行的注销任务（2026-11-08 第三轮复审 P1-C）。
   *
   * 作用：① 多个入口（点退出 / 刷新后静默重试 / 人工重试）同时触发时只发一次请求；
   * ② 登录前可以 `await` 它，保证「注销响应先落地、再种新 cookie」。
   */
  const logoutTaskRef = useRef<Promise<boolean> | null>(null)
  /**
   * 服务端会话是否**还没撤销**（2026-11-08 第二轮复审 P1-NEW-02）。
   *
   * 断网 / 超时 / 服务端异常时，本地界面可以退出，但 HttpOnly Cookie 只能由服务端
   * 删除 —— 那种状态下必须如实提示，并在网络恢复后重试。初值从 localStorage 读，
   * 刷新后仍然有效。
   */
  const [logoutPending, setLogoutPending] = useState<boolean>(() => hasPendingLogout())

  const setUser = useCallback((user: Me) => {
    // 登录请求会把旧 Cookie 一起带给服务端，服务端在**同一事务**里撤销旧会话
    // （见 `api/auth._revoke_presented_session`）。到这里才允许清掉待撤销标记 ——
    // 不是「登录成功 = 旧会话已撤销」，而是「服务端确认撤销后才登录成功」。
    clearPendingLogout()
    setLogoutPending(false)
    publishAuthEvent('login')
    userRef.current = user
    setMe(user)
    setChecking(false)
  }, [])

  /**
   * 真正撤销服务端会话。成功才清掉待撤销标记；失败则保留（**不含令牌**）。
   *
   * 返回 `true` = 服务端已撤销（或本来就没有会话），`false` = 请求没送达。
   */
  const revokeSession = useCallback(() => {
    // 已经登录成功的人不要再发注销：那会把刚建立的新会话删掉
    if (userRef.current !== null) {
      clearPendingLogout()
      setLogoutPending(false)
      return Promise.resolve(true)
    }
    // 同一标签页只允许一个注销请求在途；并发调用共享同一个 Promise
    if (logoutTaskRef.current !== null) return logoutTaskRef.current
    const task = (async () => {
      // 跨标签页共享的「注销在途」标记：别的标签页登录前会等它消失
      const inflightId = markLogoutInFlight()
      try {
        await api.logout()
        clearPendingLogout()
        setLogoutPending(false)
        publishAuthEvent('logout-revoked')
        return true
      } catch (err) {
        // 断网 / 超时 / 5xx：cookie 还在浏览器里，会话可能仍然有效。
        // 只记一条日志，不把 rejection 冒泡成 Unhandled Promise rejection。
        console.warn('登出请求未送达服务端，会话可能仍然有效：', err)
        markLogoutPending()
        setLogoutPending(true)
        publishAuthEvent('logout-failed')
        return false
      } finally {
        clearLogoutInFlight(inflightId)
      }
    })()
    logoutTaskRef.current = task
    void task.finally(() => {
      if (logoutTaskRef.current === task) logoutTaskRef.current = null
    })
    return task
  }, [])

  /**
   * 登录前等所有标签页的注销请求落地。除了当前标签页的 Promise，还要轮询
   * localStorage 里的共享标记 —— 另一个标签页的注销响应同样会清同名 cookie。
   */
  const waitForLogoutIdle = useCallback(async () => {
    const task = logoutTaskRef.current
    if (task !== null) await task
    await waitForNoLogoutInFlight()
  }, [])

  useEffect(() => {
    let alive = true
    if (hasPendingLogout()) {
      // 待撤销状态：**不能**再走 /auth/me —— 即使服务端会话还活着，也不该因为
      // 刷新页面就自动恢复到已登录的业务页。直接当未登录处理，并静默重试注销。
      setChecking(false)
      void revokeSession()
      return () => {
        alive = false
      }
    }
    api
      .authMe()
      .then((user) => {
        // 期间用户已登录 → 这次「我是谁」已经过时，别覆盖
        if (alive && userRef.current === null) setUser(user)
      })
      .catch(() => {
        // 401（没登录）与网络错误都当作「没登录」：这一层只决定要不要跳登录页，
        // 不该把后端不可用也变成白屏。
        // ⚠️ 网络错误也要能走到这里 —— `authMe` 带 10 秒超时（见 api/client.ts 的
        // AUTH_CHECK_TIMEOUT_MS），否则弱网下这个 promise 永不 settle，下面的
        // `checking` 就永远是 true、RequireAuth 渲染的空白页就永远白着（2026-10-10 修）。
        // ⚠️ 只在**还没人登录**时才清（2026-10-28 修）：迟到的 401 不该踢掉刚登录的用户。
        if (alive && userRef.current === null) {
          setMe(null)
          setChecking(false)
        }
      })
    return () => {
      alive = false
    }
  }, [revokeSession, setUser])

  useEffect(() => {
    const onUnauthorized = () => {
      userRef.current = null
      setMe(null)
    }
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
  }, [])

  /**
   * 多标签页同步（2026-11-08 第三轮复审 P1-C）。
   *
   * 一个标签页退出后，其它标签页不能继续展示「已登录」的过期界面：
   * - `logout-start` 一到就先清本地用户（结果未知，先按退出处理）；
   * - `logout-revoked` 表示服务端会话真的没了；`logout-failed` 表示风险仍在；
   * - 待撤销标记本身的增删也要同步，避免两个标签页一个显示警告、一个什么都没有。
   */
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === LOGOUT_PENDING_KEY) {
        setLogoutPending(event.newValue !== null)
        if (event.newValue !== null) {
          userRef.current = null
          setMe(null)
        }
        return
      }
      if (event.key !== AUTH_EVENT_KEY || !event.newValue) return
      let data: AuthEvent
      try {
        data = JSON.parse(event.newValue) as AuthEvent
      } catch {
        return
      }
      if (data.type === 'logout-start') {
        userRef.current = null
        setMe(null)
        setLogoutPending(true)
      } else if (data.type === 'logout-revoked') {
        userRef.current = null
        setMe(null)
        setLogoutPending(false)
      } else if (data.type === 'logout-failed') {
        setLogoutPending(true)
      } else if (data.type === 'login') {
        // 另一标签页重新登录成功：服务端已在同一事务里撤销旧会话，风险解除
        setLogoutPending(false)
      }
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  const logout = useCallback(async () => {
    // 先告诉其它标签页「这个浏览器正在退出」：不能让它们继续展示过期登录态
    publishAuthEvent('logout-start')
    // 先退出**本地界面**：目标是「别再显示成已登录」——会话可能已失效、或请求根本
    // 没发出去，不该让用户卡在已登录界面。
    userRef.current = null
    setMe(null)
    // 详情页「← → 切换」用的列表存在 sessionStorage 里，是上一个用户点过的上下文，
    // 登出时清掉，别带给下一个登录的人（2026-10-10 修）
    clearStockList()
    // 再撤销**服务端会话**。失败时保留待撤销标记（不含令牌），登录页会明确警告
    // 「服务端会话可能仍然有效」，并提供重试（2026-11-08 第二轮复审 P1-NEW-02）。
    const revoked = await revokeSession()
    // 未登录时进登录页不能用 push，否则用户按返回又回到需要登录的页面。
    navigate('/login', { replace: true, state: { logoutFailed: !revoked } })
    return revoked
  }, [navigate, revokeSession])

  return (
    <AuthContext.Provider
      value={{
        me,
        checking,
        setUser,
        logout,
        logoutPending,
        retryLogout: revokeSession,
        waitForLogoutIdle,
      }}
    >
      {children}
    </AuthContext.Provider>
  )
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
