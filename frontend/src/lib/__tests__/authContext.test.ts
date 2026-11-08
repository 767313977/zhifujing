import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  clearLogoutInFlight,
  clearPendingLogout,
  hasPendingLogout,
  isStorageAvailable,
  markLogoutInFlight,
  markLogoutPending,
  pendingLogoutState,
  scanLogoutInFlight,
  waitForNoLogoutInFlight,
} from '../authContext'

const INFLIGHT_PREFIX = 'fupan.auth.logout.inflight.'

function denyStorage(): void {
  vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => {
    throw new DOMException('denied', 'SecurityError')
  })
}

beforeEach(() => {
  window.localStorage.clear()
})

describe('P1-D 待注销标记的三态读取', () => {
  it('没有标记时返回 absent，写入后 present，清除后 absent', () => {
    expect(pendingLogoutState()).toBe('absent')
    expect(hasPendingLogout()).toBe(false)
    expect(markLogoutPending()).toBe(true)
    expect(pendingLogoutState()).toBe('present')
    expect(hasPendingLogout()).toBe(true)
    expect(clearPendingLogout()).toBe(true)
    expect(pendingLogoutState()).toBe('absent')
  })

  it('getItem 返回 null 时是 absent（不是 present）', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockReturnValue(null)
    expect(pendingLogoutState()).toBe('absent')
    expect(hasPendingLogout()).toBe(false)
  })

  it('getItem 抛异常时是 unavailable，且不能当成存在标记', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })
    expect(pendingLogoutState()).toBe('unavailable')
    expect(hasPendingLogout()).toBe(false)
  })

  it('localStorage 本身不可访问时是 unavailable，写入/清除明确失败', () => {
    denyStorage()
    expect(pendingLogoutState()).toBe('unavailable')
    expect(hasPendingLogout()).toBe(false)
    expect(isStorageAvailable()).toBe(false)
    expect(markLogoutPending()).toBe(false)
    expect(clearPendingLogout()).toBe(false)
  })

  it('setItem 抛异常时写入返回 false，且不会伪造 present', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('quota', 'QuotaExceededError')
    })
    expect(markLogoutPending()).toBe(false)
    expect(pendingLogoutState()).toBe('absent')
  })
})

describe('P1-E 多任务注销锁', () => {
  it('两个任务互不覆盖：清掉一个，另一个仍然 live', () => {
    const first = markLogoutInFlight()
    const second = markLogoutInFlight()
    expect(scanLogoutInFlight().live).toBe(2)

    clearLogoutInFlight(first)
    const afterFirst = scanLogoutInFlight()
    expect(afterFirst.live).toBe(1)
    expect(window.localStorage.getItem(`${INFLIGHT_PREFIX}${second}`)).not.toBeNull()

    clearLogoutInFlight(second)
    expect(scanLogoutInFlight().live).toBe(0)
  })

  it('等待第二个任务：先完成的任务不能让它误判为已结束', async () => {
    const first = markLogoutInFlight()
    markLogoutInFlight()
    clearLogoutInFlight(first)

    const result = await waitForNoLogoutInFlight(180)
    expect(result.status).toBe('timeout')
    expect(result.live).toBe(1)
  })

  it('过期锁会被识别为 stale 并清理，不阻塞登录', async () => {
    window.localStorage.setItem(
      `${INFLIGHT_PREFIX}stale`,
      JSON.stringify({ at: Date.now() - 60_000 }),
    )
    const scan = scanLogoutInFlight()
    expect(scan).toEqual({ available: true, live: 0, stale: 1 })
    expect(window.localStorage.getItem(`${INFLIGHT_PREFIX}stale`)).toBeNull()

    const result = await waitForNoLogoutInFlight(100)
    expect(result.status).toBe('clear')
  })

  it('没有在途任务时立即返回 clear', async () => {
    await expect(waitForNoLogoutInFlight(100)).resolves.toEqual({
      status: 'clear',
      live: 0,
      stale: 0,
    })
  })

  it('存储不可用时返回 unavailable，不能静默当成 clear', async () => {
    denyStorage()
    await expect(waitForNoLogoutInFlight(100)).resolves.toEqual({
      status: 'unavailable',
      live: 0,
      stale: 0,
    })
  })

  it('存储可访问但 getItem 抛 SecurityError 时也必须是 unavailable（即使没有 key）', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })
    expect(scanLogoutInFlight()).toEqual({ available: false, live: 0, stale: 0 })
    await expect(waitForNoLogoutInFlight(100)).resolves.toEqual({
      status: 'unavailable',
      live: 0,
      stale: 0,
    })
  })
})
