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

export interface AuthValue {
  /** 当前用户；null = 没登录（或还在确认，见 `checking`） */
  me: Me | null
  /** 首次确认还没结束。**必须区分「没登录」与「还不知道」**，否则刷新页面会闪一下登录页 */
  checking: boolean
  /** 登录 / 注册成功后调用，把用户塞进来，省一次 /auth/me */
  setUser: (me: Me) => void
  /**
   * 登出。返回 `false` 表示**登出请求没送达服务端**（断网 / 超时）——
   * 本地状态照样清，但会话可能仍有效，调用方要如实告诉用户（见 2026-11-08 审计 P2-05）。
   */
  logout: () => Promise<boolean>
}

export const AuthContext = createContext<AuthValue | null>(null)

export function useAuth(): AuthValue {
  const value = useContext(AuthContext)
  if (value === null) {
    throw new Error('useAuth 必须在 AuthProvider 内使用')
  }
  return value
}
