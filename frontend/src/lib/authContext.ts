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
 * 所有在途注销都落地，再发登录请求。
 *
 * ⚠️ 2026-11-08 第四轮复审 P1-E：每个注销任务用**独立的 key**
 * （`fupan.auth.logout.inflight.<id>`），不再共用单个键 —— 否则先完成的任务会把
 * 另一个仍在途的任务的锁一起删掉，登录侧误以为「所有注销都结束了」。
 * 只存随机 id + 时间戳，不含令牌；超过 TTL 视为废弃（标签页关闭 / 崩溃），
 * 由下一次扫描清理。
 */
const LOGOUT_INFLIGHT_PREFIX = 'fupan.auth.logout.inflight.'
const INFLIGHT_TTL_MS = 35_000
/**
 * 登录前最多等多久。
 *
 * ⚠️ 必须 **小于** `INFLIGHT_TTL_MS`（2026-11-08 第四轮复审 P1-E）：否则等待期间
 * 锁会先过期并被判成「已结束」，`timeout` 分支永远走不到，又变成静默放行。
 * 25 秒 < 35 秒：真在途的任务会让登录明确失败，由用户重试；被关闭标签页留下的
 * 锁则会在 TTL 之后由下一次扫描清理。
 */
const INFLIGHT_WAIT_MS = 25_000

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

/** 「待注销标记」的三种状态；存储不可用时**不能**当成「存在」。 */
export type PendingLogoutState = 'present' | 'absent' | 'unavailable'

export interface LogoutInflightScan {
  /** localStorage 是否可读；false 时 live / stale 没有意义 */
  available: boolean
  /** 仍未过期、可能还在途的注销任务数 */
  live: number
  /** 已过期、本次扫描顺手清理掉的废弃任务数 */
  stale: number
}

export type LogoutWaitStatus = 'clear' | 'timeout' | 'unavailable'

export interface LogoutWaitResult {
  status: LogoutWaitStatus
  live: number
  stale: number
}

interface InflightEntry {
  at: number
}

function _storage(): Storage | null {
  try {
    return window.localStorage
  } catch {
    // 隐私模式 / 禁用存储：退化为「只能在当前页面提示」，不阻断登录 / 登出流程
    return null
  }
}

/**
 * 读取待注销标记的**三态**状态（2026-11-08 第四轮复审 P1-D）。
 *
 * 旧实现 `_storage()?.getItem(...) !== null` 在存储不可用时得到
 * `undefined !== null === true`，会被误判成「有待注销标记」，于是页面一加载就自动
 * 去调 `/api/auth/logout`。这里明确区分：
 * - `present`：确实有标记；
 * - `absent`：存储可读、确实没有标记；
 * - `unavailable`：存储访问 / 读取抛异常（SecurityError 等），**不能**当成有标记。
 */
export function pendingLogoutState(): PendingLogoutState {
  const storage = _storage()
  if (storage === null) return 'unavailable'
  try {
    return storage.getItem(LOGOUT_PENDING_KEY) === null ? 'absent' : 'present'
  } catch {
    // SecurityError：存储被浏览器策略禁用；不能据此自动登出
    return 'unavailable'
  }
}

export function hasPendingLogout(): boolean {
  return pendingLogoutState() === 'present'
}

export function isStorageAvailable(): boolean {
  const storage = _storage()
  if (storage === null) return false
  try {
    storage.getItem(LOGOUT_PENDING_KEY)
    return true
  } catch {
    return false
  }
}

/** 写待注销标记；返回是否真的写进存储（false = 存储不可用，仅本页内存有效）。 */
export function markLogoutPending(): boolean {
  const storage = _storage()
  if (storage === null) return false
  try {
    storage.setItem(LOGOUT_PENDING_KEY, String(Date.now()))
    return true
  } catch {
    // 配额 / 隐私模式：本页仍会显示失败提示，但不能声称跨刷新同步可靠
    return false
  }
}

/** 清待注销标记；返回是否真的从存储删除（false = 存储不可用或删除失败）。 */
export function clearPendingLogout(): boolean {
  const storage = _storage()
  if (storage === null) return false
  try {
    storage.removeItem(LOGOUT_PENDING_KEY)
    return true
  } catch {
    return false
  }
}

export function publishAuthEvent(type: AuthEventType): void {
  try {
    _storage()?.setItem(AUTH_EVENT_KEY, JSON.stringify({ type, at: Date.now() }))
  } catch {
    // 隐私模式 / 禁用存储：跨标签页同步退化为「各标签页下一次请求时自己发现」
  }
}

/**
 * 扫描所有在途注销标记。
 *
 * - 每个任务一个 key，互不覆盖；
 * - 超过 `INFLIGHT_TTL_MS` 的 key 视为废弃并**尽力**清理（标签页关闭 / 崩溃）；
 * - 存储不可读时返回 `available=false`，调用方必须显式处理，不能当作「已结束」。
 */
export function scanLogoutInFlight(now = Date.now()): LogoutInflightScan {
  const storage = _storage()
  if (storage === null) return { available: false, live: 0, stale: 0 }
  // ⚠️ 不能只看 `storage.length`：存储被策略禁用时 length 可能仍返回 0，
  // 一次 getItem 都不调用就会把「不可用」误判成「没有在途任务」。先探一次读。
  if (!isStorageAvailable()) return { available: false, live: 0, stale: 0 }
  const staleKeys: string[] = []
  let live = 0
  try {
    const keys: string[] = []
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index)
      if (key !== null && key.startsWith(LOGOUT_INFLIGHT_PREFIX)) keys.push(key)
    }
    for (const key of keys) {
      const raw = storage.getItem(key)
      let at: number | null = null
      try {
        const parsed = JSON.parse(raw ?? '') as Partial<InflightEntry>
        at = typeof parsed.at === 'number' ? parsed.at : null
      } catch {
        at = null
      }
      if (at === null || now - at > INFLIGHT_TTL_MS) staleKeys.push(key)
      else live += 1
    }
    for (const key of staleKeys) {
      try {
        storage.removeItem(key)
      } catch {
        // 只读存储：清理失败，但本次仍把它当成废弃
      }
    }
    return { available: true, live, stale: staleKeys.length }
  } catch {
    // 遍历 / 读取中途抛 SecurityError：整体视为不可用
    return { available: false, live: 0, stale: 0 }
  }
}

export function markLogoutInFlight(): string {
  const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`
  try {
    _storage()?.setItem(
      `${LOGOUT_INFLIGHT_PREFIX}${id}`,
      JSON.stringify({ at: Date.now() } satisfies InflightEntry),
    )
  } catch {
    // 存不进去只能靠同标签页的 Promise 串行；跨标签页窗口会重新暴露
  }
  return id
}

export function clearLogoutInFlight(id: string): void {
  try {
    // 只删自己的 key：绝不碰其它标签页仍在途的任务
    _storage()?.removeItem(`${LOGOUT_INFLIGHT_PREFIX}${id}`)
  } catch {
    // 清不掉会在 TTL 后由扫描清理
  }
}

function _sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * 等「所有标签页的注销请求」都落地（最多等 `timeoutMs`）。
 *
 * 返回**明确结果**，不再用 void 静默放行（2026-11-08 第四轮复审 P1-E）：
 * - `clear`：没有在途任务（`stale` 是顺手清理的废弃任务数）；
 * - `timeout`：仍有未过期的在途任务，调用方必须提示用户、不得假装已结束；
 * - `unavailable`：存储不可读，无法跨标签页协调，调用方必须显式退化。
 */
export async function waitForNoLogoutInFlight(
  timeoutMs = INFLIGHT_WAIT_MS,
): Promise<LogoutWaitResult> {
  const first = scanLogoutInFlight()
  if (!first.available) return { status: 'unavailable', live: 0, stale: 0 }
  if (first.live === 0) return { status: 'clear', live: 0, stale: first.stale }

  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    await _sleep(100)
    const scan = scanLogoutInFlight()
    if (!scan.available) return { status: 'unavailable', live: 0, stale: 0 }
    if (scan.live === 0) return { status: 'clear', live: 0, stale: scan.stale }
  }
  const last = scanLogoutInFlight()
  if (!last.available) return { status: 'unavailable', live: 0, stale: 0 }
  if (last.live === 0) return { status: 'clear', live: 0, stale: last.stale }
  return { status: 'timeout', live: last.live, stale: last.stale }
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
   * 返回明确结果：`clear` 才能安全登录；`timeout` / `unavailable` 必须由调用方
   * 显式处理，不能静默当成「注销已完成」（2026-11-08 第四轮复审 P1-E）。
   */
  waitForLogoutIdle: () => Promise<LogoutWaitResult>
}

export const AuthContext = createContext<AuthValue | null>(null)

export function useAuth(): AuthValue {
  const value = useContext(AuthContext)
  if (value === null) {
    throw new Error('useAuth 必须在 AuthProvider 内使用')
  }
  return value
}
