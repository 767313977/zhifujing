import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../../api/client'
import type { Me } from '../../api/types'
import type { AuthValue } from '../../lib/authContext'
import { AuthContext } from '../../lib/authContext'
import Login from '../Login'

vi.mock('../../api/client', () => ({
  api: {
    login: vi.fn(),
    register: vi.fn(),
    authMe: vi.fn(),
  },
  UNAUTHORIZED_EVENT: 'fupan:unauthorized',
}))

const ME: Me = { id: 1, username: 'u1', is_admin: false }

function makeAuth(overrides: Partial<AuthValue> = {}): AuthValue {
  return {
    me: null,
    checking: false,
    setUser: vi.fn(),
    logout: vi.fn().mockResolvedValue(true),
    logoutPending: false,
    retryLogout: vi.fn().mockResolvedValue(true),
    waitForLogoutIdle: vi.fn().mockResolvedValue({ status: 'clear', live: 0, stale: 0 }),
    ...overrides,
  }
}

function renderLogin(auth: AuthValue) {
  const view = render(
    <MemoryRouter initialEntries={['/login']}>
      <AuthContext.Provider value={auth}>
        <Login />
      </AuthContext.Provider>
    </MemoryRouter>,
  )
  fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'u1' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'Test12345!' } })
  return view
}

function submit(view: ReturnType<typeof renderLogin>) {
  const button = view.container.querySelector('button[type="submit"]')
  if (!(button instanceof HTMLButtonElement)) throw new Error('找不到登录按钮')
  fireEvent.click(button)
}

beforeEach(() => {
  window.localStorage.clear()
  vi.mocked(api.login).mockReset()
  vi.mocked(api.register).mockReset()
  vi.mocked(api.authMe).mockReset()
})

describe('P1-E 登录前的注销等待结果', () => {
  it('timeout 时明确拒绝登录，不静默建立新会话', async () => {
    const auth = makeAuth({
      waitForLogoutIdle: vi.fn().mockResolvedValue({ status: 'timeout', live: 1, stale: 0 }),
    })
    const view = renderLogin(auth)
    submit(view)

    await screen.findByText(/注销请求仍未完成/)
    expect(api.login).not.toHaveBeenCalled()
  })

  it('unavailable 时给出降级提示，但允许登录并校验会话', async () => {
    const setUser = vi.fn()
    const auth = makeAuth({
      setUser,
      waitForLogoutIdle: vi
        .fn()
        .mockResolvedValue({ status: 'unavailable', live: 0, stale: 0 }),
    })
    vi.mocked(api.login).mockResolvedValue(ME)
    vi.mocked(api.authMe).mockResolvedValue(ME)
    const view = renderLogin(auth)
    submit(view)

    await screen.findByText(/本地存储不可用/)
    await waitFor(() => expect(setUser).toHaveBeenCalledWith(ME))
    expect(api.login).toHaveBeenCalledTimes(1)
    expect(api.authMe).toHaveBeenCalledTimes(1)
  })

  it('clear 时登录成功并完成 /auth/me 校验', async () => {
    const setUser = vi.fn()
    vi.mocked(api.login).mockResolvedValue(ME)
    vi.mocked(api.authMe).mockResolvedValue(ME)
    const view = renderLogin(makeAuth({ setUser }))
    submit(view)

    await waitFor(() => expect(setUser).toHaveBeenCalledWith(ME))
    expect(api.authMe).toHaveBeenCalledTimes(1)
  })

  it('登录接口成功但会话 Cookie 已被清掉时，不建立前端登录态', async () => {
    const setUser = vi.fn()
    vi.mocked(api.login).mockResolvedValue(ME)
    vi.mocked(api.authMe).mockRejectedValue(new Error('401'))
    const view = renderLogin(makeAuth({ setUser }))
    submit(view)

    await screen.findByText(/登录后会话校验失败/)
    expect(setUser).not.toHaveBeenCalled()
  })
})
