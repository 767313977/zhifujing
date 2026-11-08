import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { act } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { Me } from '../../api/types'
import { api } from '../../api/client'
import { AuthProvider } from '../auth'
import { AUTH_EVENT_KEY, useAuth } from '../authContext'

vi.mock('../../api/client', () => ({
  api: {
    authMe: vi.fn(),
    logout: vi.fn(),
  },
  UNAUTHORIZED_EVENT: 'fupan:unauthorized',
}))

const ME: Me = { id: 1, username: 'u1', is_admin: false }

function Probe() {
  const { me, checking, logoutPending, setUser } = useAuth()
  return (
    <>
      <div data-testid="state">
        {checking
          ? 'checking'
          : `${me ? `user:${me.username}` : 'anonymous'}|pending:${String(logoutPending)}`}
      </div>
      <button type="button" onClick={() => setUser(ME)}>
        fake-login
      </button>
    </>
  )
}

function renderProvider() {
  return render(
    <MemoryRouter>
      <AuthProvider>
        <Probe />
      </AuthProvider>
    </MemoryRouter>,
  )
}

function dispatchAuthEvent(type: string) {
  window.dispatchEvent(
    new StorageEvent('storage', {
      key: AUTH_EVENT_KEY,
      newValue: JSON.stringify({ type, at: Date.now() }),
    }),
  )
}

beforeEach(() => {
  window.localStorage.clear()
  vi.mocked(api.authMe).mockReset()
  vi.mocked(api.logout).mockReset()
})

describe('AuthProvider 启动身份探测', () => {
  it('没有待注销标记时不会自动调用 /api/auth/logout', async () => {
    vi.mocked(api.authMe).mockRejectedValue(new Error('401'))
    renderProvider()
    await waitFor(() =>
      expect(screen.getByTestId('state').textContent).toBe('anonymous|pending:false'),
    )
    expect(api.logout).not.toHaveBeenCalled()
  })

  it('localStorage 不可用时也不会误判为待注销、不会自动登出', async () => {
    vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })
    vi.mocked(api.authMe).mockRejectedValue(new Error('401'))
    renderProvider()
    await waitFor(() =>
      expect(screen.getByTestId('state').textContent).toBe('anonymous|pending:false'),
    )
    expect(api.logout).not.toHaveBeenCalled()
  })

  it('正常刷新：authMe 成功恢复登录态，但不广播 login', async () => {
    vi.mocked(api.authMe).mockResolvedValue(ME)
    renderProvider()
    await waitFor(() =>
      expect(screen.getByTestId('state').textContent).toBe('user:u1|pending:false'),
    )
    expect(window.localStorage.getItem(AUTH_EVENT_KEY)).toBeNull()
  })
})

describe('P2-F 过期 authMe 响应', () => {
  it('logout-start 之后的迟到 authMe 不得恢复登录态、不得广播 login', async () => {
    let resolveMe!: (user: Me) => void
    vi.mocked(api.authMe).mockReturnValue(
      new Promise<Me>((resolve) => {
        resolveMe = resolve
      }),
    )
    renderProvider()
    await waitFor(() =>
      expect(screen.getByTestId('state').textContent).toBe('checking'),
    )

    act(() => {
      dispatchAuthEvent('logout-start')
    })
    await waitFor(() =>
      expect(screen.getByTestId('state').textContent).toBe('anonymous|pending:true'),
    )

    await act(async () => {
      resolveMe(ME)
    })
    // 迟到的响应被代次机制丢弃：仍然是未登录，也没有 login 广播
    expect(screen.getByTestId('state').textContent).toBe('anonymous|pending:true')
    expect(window.localStorage.getItem(AUTH_EVENT_KEY)).toBeNull()
  })

  it('本地注销开始时即时作废旧探测并离开 checking', async () => {
    let resolveMe!: (user: Me) => void
    vi.mocked(api.authMe).mockReturnValue(
      new Promise<Me>((resolve) => {
        resolveMe = resolve
      }),
    )
    vi.mocked(api.logout).mockResolvedValue({ ok: true })

    function LogoutButton() {
      const { logout } = useAuth()
      return (
        <button type="button" onClick={() => void logout()}>
          fake-logout
        </button>
      )
    }

    render(
      <MemoryRouter>
        <AuthProvider>
          <Probe />
          <LogoutButton />
        </AuthProvider>
      </MemoryRouter>,
    )
    await waitFor(() =>
      expect(screen.getByTestId('state').textContent).toBe('checking'),
    )
    await act(async () => {
      screen.getByText('fake-logout').click()
    })
    expect(screen.getByTestId('state').textContent).toBe('anonymous|pending:false')

    await act(async () => {
      resolveMe(ME)
    })
    expect(screen.getByTestId('state').textContent).toBe('anonymous|pending:false')
  })

  it('登录接口成功（setUser）仍然广播 login，便于其它标签页同步', async () => {
    vi.mocked(api.authMe).mockResolvedValue(ME)
    renderProvider()
    await waitFor(() =>
      expect(screen.getByTestId('state').textContent).toBe('user:u1|pending:false'),
    )
    await act(async () => {
      screen.getByText('fake-login').click()
    })
    const event = JSON.parse(window.localStorage.getItem(AUTH_EVENT_KEY) ?? '{}')
    expect(event.type).toBe('login')
  })
})
