/**
 * 登录态的 context 与读取钩子（2026-11-01 从 `lib/auth.tsx` 拆出）。
 *
 * 为什么单独一个文件：`lib/auth.tsx` 同时导出 `AuthProvider` / `RequireAuth` 等**组件**
 * 与 `useAuth` 这个**钩子**，`react/only-export-components` 会判「整文件都当组件」，
 * 于是 Fast Refresh 只能整页重载（编辑一次就丢一次登录态与页面状态）。
 * 拆开后两个文件各自只导出一种东西，热更新可以只替换组件、保住 context 的值。
 *
 * 这里**只放不依赖路由与组件的部分**（context + 类型 + 钩子）；
 * Provider 的实现仍在 `lib/auth.tsx`。
 */

import { createContext, useContext } from 'react'
import type { Me } from '../api/types'

/**
 * 「服务端会话还没撤销」的本地标记（2026-11-08 第二轮复审 P1-NEW-02）。
 *
 * ⚠️ **只存时间戳，绝不存令牌**：登录态在 HttpOnly Cookie 里，JS 读不到也不该读。
 * 标记的作用只有两个：① 刷新时不要拿还活着的会话自动恢复到业务页；② 提醒前端
 * 在网络恢复后重试注销。用 localStorage 而不是 sessionStorage，是因为「关掉浏览器」
 * 不一定清掉持久化 Cookie，风险跨浏览器会话存在。
 */
const LOGOUT_PENDING_KEY = 'fupan.logout.pending'

/** 跨标签页的登录态事件（logout-start / logout-revoked / logout-failed / login）。 */
const AUTH_EVENT_KEY = 'fupan.auth.event'

/**
 * 「注销请求正在途中」的共享标记（2026-11-08 第三轮复审 P1-C）。
 *
 * 为什么需要跨标签页：注销响应的 `Set-Cookie: Max-Age=0` 清的是**浏览器 cookie 罐**
 * 里的同名 cookie。如果 A 标签页的注销请求还在途中、B 标签页已经登录并种下新 cookie，
 * 迟到的注销响应会把**新 cookie** 一并清掉（服务端新会话变成孤儿）。登录前等到
 * 这个标记消失，保证注销响应已经落地，再发登录请求。
 * 只存随机 id + 时间戳，不含令牌；40 秒后视为过期，避免标签页崩溃留下永久锁。
 */
const LOGOUT_INFLIGHT_KEY = 'fupan.auth.logout.inflight'
const INFLIGHT_TTL_MS = 40_000

export { AUTH_EVENT_KEY, LOGOUT_PENDING_KEY }

export type AuthEventType =
  | 'logout-start'
  | 'logout-revoked'
  | 'logout-failed'
  | 'login'

export interface AuthEvent {
  type: AuthEventType
  at: number
}

interface InflightLock {
  id: string
  at: number
}

function _storage(): Storage | null {
  try {
    return window.localStorage
  } catch {
    // 隐私模式 / 禁用存储：退化为「只能在当前页面提示」，不阻断登出流程
    return null
  }
}

export function hasPendingLogout(): boolean {
  return _storage()?.getItem(LOGOUT_PENDING_KEY) !== null
}

export function markLogoutPending(): void {
  try {
    _storage()?.setItem(LOGOUT_PENDING_KEY, String(Date.now()))
  } catch {
    // 存不进去（配额 / 隐私模式）时至少当前页面仍会显示失败提示
  }
}

export function clearPendingLogout(): void {
  try {
    _storage()?.removeItem(LOGOUT_PENDING_KEY)
  } catch {
    // 删不掉不影响登录：下次成功登录仍会覆盖同名的会话 Cookie
  }
}

export function publishAuthEvent(type: AuthEventType): void {
  try {
    _storage()?.setItem(AUTH_EVENT_KEY, JSON.stringify({ type, at: Date.now() }))
  } catch {
    // 隐私模式 / 禁用存储：跨标签页同步退化为「各标签页下一次请求时自己发现」
  }
}

function _readInflight(): InflightLock | null {
  try {
    const raw = _storage()?.getItem(LOGOUT_INFLIGHT_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<InflightLock>
    if (
      typeof parsed.id !== 'string' ||
      typeof parsed.at !== 'number' ||
      Date.now() - parsed.at > INFLIGHT_TTL_MS
    ) {
      return null
    }
    return { id: parsed.id, at: parsed.at }
  } catch {
    return null
  }
}

export function markLogoutInFlight(): string {
  const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`
  try {
    _storage()?.setItem(
      LOGOUT_INFLIGHT_KEY,
      JSON.stringify({ id, at: Date.now() } satisfies InflightLock),
    )
  } catch {
    // 存不进去只能靠同标签页的 Promise 串行；跨标签页窗口重新暴露，属可接受退化
  }
  return id
}

export function clearLogoutInFlight(id: string): void {
  try {
    if (_readInflight()?.id === id) _storage()?.removeItem(LOGOUT_INFLIGHT_KEY)
  } catch {
    // 清不掉会在 40 秒后自然过期
  }
}

/**
 * 等「任何标签页的注销请求」都落地再继续（最多等 `timeoutMs`）。
 *
 * 只用于**登录前**：保证迟到的注销响应（会清同名 cookie）不会把刚种下的新会话
 * cookie 清掉。超时后放行是不得已 —— 40 秒的 in-flight 标记本身会过期，
 * 宁可让用户偶尔重登，也不要把登录页永久锁死。
 */
export async function waitForNoLogoutInFlight(timeoutMs = 35_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (_readInflight() === null) return
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
}

export interface AuthValue {
  /** 当前用户；null = 没登录（或还在确认，见 `checking`） */
  me: Me | null
  /** 首次确认还没结束。**必须区分「没登录」与「还不知道」**，否则刷新页面会闪一下登录页 */
  checking: boolean
  /** 登录 / 注册成功后调用，把用户塞进来，省一次 /auth/me */
  setUser: (me: Me) => void
  /**
   * 登出。返回 `false` 表示**登出请求没送达服务端**（断网 / 超时 / 服务端异常）——
   * 本地状态照样清，但会话可能仍有效，调用方要如实告诉用户。
   */
  logout: () => Promise<boolean>
  /** 上一次登出请求没送达服务端：页面必须显示「服务端会话可能仍有效」的警告 */
  logoutPending: boolean
  /** 重试注销；返回 `true` 表示服务端会话已撤销、待撤销标记已清除 */
  retryLogout: () => Promise<boolean>
  /**
   * 等当前标签页与其它标签页的注销请求都落地（登录前调用）。
   *
   * 目的：避免迟到的注销响应把刚种下的新会话 cookie 清掉（2026-11-08 第三轮 P1-C）。
   */
  waitForLogoutIdle: () => Promise<void>
}

export const AuthContext = createContext<AuthValue | null>(null)

export function useAuth(): AuthValue {
  const value = useContext(AuthContext)
  if (value === null) {
    throw new Error('useAuth 必须在 AuthProvider 内使用')
  }
  return value
}
