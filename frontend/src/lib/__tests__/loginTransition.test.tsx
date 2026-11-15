/**
 * S6.7 回归：登录成功后的 SPA 交接（2026-11-15）。
 *
 * 这组用例锁住一个**真实浏览器里复现过 3 次**的阻断缺陷：
 * 登录接口全部 200，但页面停在 `/login`、`#root` 为空。
 *
 * 成因（见 `reports/s6_7_login/root_cause_matrix.md`）：
 * `StockResearchProvider` 的「登录态变化」effect 在登录那一刻调用了
 * `writeUrl(null)`，而 `setSearchParams` 即使参数没变也会发起一次导航；
 * 这次导航与 `Login` 的「已登录 → Navigate 到目标页」在同一次提交里竞争，
 * 后发起的 `pushState('/login')` 把路由打回登录页。`Login` 随后渲染
 * `<Navigate to={from} replace />`（DOM 为 null），但 effect 依赖没变、不会重跑，
 * 于是整棵树静默变成空 —— 没有异常、没有失败请求，只有白屏。
 *
 * 因此这里的断言必须是**可见内容**（dashboard 文本），不能只看 URL。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../../api/client'
import type { Me } from '../../api/types'
import Login from '../../pages/Login'
import { AuthProvider } from '../auth'
import { StockResearchProvider } from '../StockResearchProvider'
import { WatchlistProvider } from '../watchlist'

vi.mock('../../api/client', () => ({
  api: {
    authMe: vi.fn(),
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    watchlist: vi.fn(),
  },
  UNAUTHORIZED_EVENT: 'fupan:unauthorized',
  WATCHLIST_CHANGED_EVENT: 'fupan:watchlist-changed',
}))

const ME: Me = { id: 1, username: 'u1', is_admin: false }

function LocationProbe() {
  const location = useLocation()
  return (
    <div data-testid="location">
      {location.pathname}
      {location.search}
    </div>
  )
}

function renderApp(initialEntry: string | { pathname: string; state: unknown } = '/login') {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <AuthProvider>
        <WatchlistProvider>
          <StockResearchProvider>
            <LocationProbe />
            <Routes>
              <Route path="/login" element={<Login />} />
              <Route path="/" element={<div>DASHBOARD-CONTENT</div>} />
              <Route path="/limit-up" element={<div>LIMIT-UP-CONTENT</div>} />
            </Routes>
          </StockResearchProvider>
        </WatchlistProvider>
      </AuthProvider>
    </MemoryRouter>,
  )
}

function submitLogin(container: HTMLElement) {
  const button = container.querySelector('button[type="submit"]')
  if (!(button instanceof HTMLButtonElement)) throw new Error('找不到登录按钮')
  fireEvent.click(button)
}

beforeEach(() => {
  window.localStorage.clear()
  vi.mocked(api.authMe).mockReset()
  vi.mocked(api.login).mockReset()
  vi.mocked(api.register).mockReset()
  vi.mocked(api.logout).mockReset()
  vi.mocked(api.watchlist).mockReset()
})

describe('S6.7 登录后的 SPA 交接', () => {
  it('登录成功后渲染目标页内容，不能停在 /login 空树', async () => {
    vi.mocked(api.authMe).mockRejectedValueOnce(new Error('401'))
    vi.mocked(api.authMe).mockResolvedValue(ME)
    vi.mocked(api.login).mockResolvedValue(ME)
    vi.mocked(api.watchlist).mockResolvedValue([])

    const view = renderApp()
    const username = await screen.findByLabelText('用户名')
    fireEvent.change(username, { target: { value: 'u1' } })
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'pw-123456' } })
    submitLogin(view.container)

    // 可见内容断言：修复前这里会超时（#root 为空、URL 仍是 /login）
    expect(await screen.findByText('DASHBOARD-CONTENT')).toBeTruthy()
    expect(screen.getByTestId('location').textContent).toBe('/')
  })

  it('被守卫拦下时保留站内 return URL，登录后回到原目标页', async () => {
    vi.mocked(api.authMe).mockRejectedValueOnce(new Error('401'))
    vi.mocked(api.authMe).mockResolvedValue(ME)
    vi.mocked(api.login).mockResolvedValue(ME)
    vi.mocked(api.watchlist).mockResolvedValue([])

    const view = renderApp({ pathname: '/login', state: { from: '/limit-up?days=15' } })
    const username = await screen.findByLabelText('用户名')
    fireEvent.change(username, { target: { value: 'u1' } })
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'pw-123456' } })
    submitLogin(view.container)

    expect(await screen.findByText('LIMIT-UP-CONTENT')).toBeTruthy()
    expect(screen.getByTestId('location').textContent).toBe('/limit-up?days=15')
  })

  it('快速双击提交只发起一次有效登录', async () => {
    vi.mocked(api.authMe).mockRejectedValueOnce(new Error('401'))
    vi.mocked(api.authMe).mockResolvedValue(ME)
    vi.mocked(api.login).mockResolvedValue(ME)
    vi.mocked(api.watchlist).mockResolvedValue([])

    const view = renderApp()
    const username = await screen.findByLabelText('用户名')
    fireEvent.change(username, { target: { value: 'u1' } })
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'pw-123456' } })
    const button = view.container.querySelector('button[type="submit"]')
    if (!(button instanceof HTMLButtonElement)) throw new Error('找不到登录按钮')
    fireEvent.click(button)
    fireEvent.click(button)

    expect(await screen.findByText('DASHBOARD-CONTENT')).toBeTruthy()
    expect(api.login).toHaveBeenCalledTimes(1)
  })

  it('站外 return URL 被拒绝，登录后落回站内首页', async () => {
    vi.mocked(api.authMe).mockRejectedValueOnce(new Error('401'))
    vi.mocked(api.authMe).mockResolvedValue(ME)
    vi.mocked(api.login).mockResolvedValue(ME)
    vi.mocked(api.watchlist).mockResolvedValue([])

    const view = renderApp({
      pathname: '/login',
      state: { from: '//evil.example/steal' },
    })
    const username = await screen.findByLabelText('用户名')
    fireEvent.change(username, { target: { value: 'u1' } })
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'pw-123456' } })
    submitLogin(view.container)

    expect(await screen.findByText('DASHBOARD-CONTENT')).toBeTruthy()
    expect(screen.getByTestId('location').textContent).toBe('/')
  })
})
