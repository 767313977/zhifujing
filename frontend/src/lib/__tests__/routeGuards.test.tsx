/**
 * S6.7：路由守卫的三种状态与权限 fail-closed（2026-11-15）。
 *
 * 锁住 §4.1 的不变量：
 * - `checking`（AUTH_UNKNOWN）不能当成已登录或未登录，只能显示有界 loading；
 * - 未登录必须带 `from` 跳登录；
 * - 已登录才能挂载受保护页面；
 * - 非管理员访问管理员页必须 fail closed（提示页），不能放行。
 */

import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import type { Me } from '../../api/types'
import type { AuthValue } from '../authContext'
import { AuthContext } from '../authContext'
import { RequireAdmin, RequireAuth } from '../auth'

const ME: Me = { id: 1, username: 'u1', is_admin: false }

function makeAuth(overrides: Partial<AuthValue> = {}): AuthValue {
  return {
    me: null,
    checking: false,
    setUser: vi.fn(),
    sessionExpired: false,
    logout: vi.fn().mockResolvedValue(true),
    logoutPending: false,
    retryLogout: vi.fn().mockResolvedValue(true),
    waitForLogoutIdle: vi.fn().mockResolvedValue({ status: 'clear', live: 0, stale: 0 }),
    ...overrides,
  }
}

function LocationProbe() {
  const location = useLocation()
  const state = location.state as { from?: string } | null
  return (
    <div data-testid="location">
      {location.pathname}|{state?.from ?? ''}
    </div>
  )
}

function renderGuard(auth: AuthValue, initialEntry = '/sectors') {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <AuthContext.Provider value={auth}>
        <LocationProbe />
        <Routes>
          <Route path="/login" element={<div>LOGIN-PAGE</div>} />
          <Route
            path="/sectors"
            element={
              <RequireAuth>
                <div>SECTORS-CONTENT</div>
              </RequireAuth>
            }
          />
          <Route
            path="/settings"
            element={
              <RequireAdmin>
                <div>SETTINGS-CONTENT</div>
              </RequireAdmin>
            }
          />
        </Routes>
      </AuthContext.Provider>
    </MemoryRouter>,
  )
}

describe('S6.7 路由守卫状态语义', () => {
  it('AUTH_UNKNOWN 只显示有界 loading，不放行也不跳转', () => {
    const { container } = renderGuard(makeAuth({ checking: true }))
    expect(screen.queryByText('SECTORS-CONTENT')).toBeNull()
    expect(container.querySelector('div.min-h-screen')).not.toBeNull()
    expect(screen.getByTestId('location').textContent).toBe('/sectors|')
  })

  it('初始化完成且未登录时带 from 跳登录页', () => {
    renderGuard(makeAuth({ checking: false, me: null }))
    expect(screen.getByText('LOGIN-PAGE')).toBeTruthy()
    expect(screen.getByTestId('location').textContent).toBe('/login|/sectors')
  })

  it('已认证时才挂载受保护页面', () => {
    renderGuard(makeAuth({ checking: false, me: ME }))
    expect(screen.getByText('SECTORS-CONTENT')).toBeTruthy()
  })

  it('非管理员访问管理员页 fail closed，不渲染页面', () => {
    renderGuard(makeAuth({ checking: false, me: ME }), '/settings')
    expect(screen.getByText('需要管理员权限')).toBeTruthy()
    expect(screen.queryByText('SETTINGS-CONTENT')).toBeNull()
  })
})
