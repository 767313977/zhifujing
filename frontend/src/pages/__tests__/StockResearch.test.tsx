import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { ApiError } from '../../api/client'
import type { Me } from '../../api/types'

const auth = vi.hoisted(() => ({
  me: { id: 1, username: 'alice', is_admin: false } as Me | null,
  checking: false,
}))

vi.mock('../../lib/authContext', () => ({
  useAuth: () => auth,
}))

import {
  clearResearchCache,
  loadCached,
  researchCacheEpoch,
  researchCacheSize,
  researchInflightSize,
} from '../../lib/stockResearchCache'
import { StockResearchProvider } from '../../lib/StockResearchProvider'
import { useStockResearch } from '../../lib/stockResearchContext'
import QuickResearch from '../../components/QuickResearch'
import StockLink from '../../components/StockLink'
import { WatchlistContext } from '../../lib/watchlistContext'

const watchValue = {
  codes: new Set<string>(),
  isWatched: () => false,
  refresh: async () => {},
}

function Probe() {
  const ctx = useStockResearch()
  const location = useLocation()
  const navigate = useNavigate()
  return (
    <div>
      <span data-testid="code">{ctx.code ?? 'none'}</span>
      <span data-testid="open">{String(ctx.open)}</span>
      <span data-testid="tab">{ctx.activeTab}</span>
      <span data-testid="asof">{ctx.selectedAsOfDate ?? 'none'}</span>
      <span data-testid="search">{location.pathname + location.search}</span>
      <button type="button" onClick={() => ctx.openStockResearch('600000')}>
        open
      </button>
      <button
        type="button"
        onClick={() =>
          ctx.openStockResearch('600000', { asOfDate: '2026-09-24', tab: 'evidence' })
        }
      >
        open-asof
      </button>
      <button type="button" onClick={() => ctx.openStockResearch('abc')}>
        open-bad
      </button>
      <button type="button" onClick={() => ctx.openStockResearch('600001', { tab: 'kline' })}>
        open-b
      </button>
      <button type="button" onClick={ctx.closeStockResearch}>
        close
      </button>
      <button type="button" onClick={() => ctx.selectResearchTab('limit')}>
        tab-limit
      </button>
      <button type="button" onClick={() => navigate('/limit-up?date=2026-11-11')}>
        navigate-away
      </button>
      <QuickResearch code="600002" />
    </div>
  )
}

function renderProbe(initial = '/limit-up?date=2026-11-11&foo=1') {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <WatchlistContext.Provider value={watchValue}>
        <StockResearchProvider>
          <Probe />
        </StockResearchProvider>
      </WatchlistContext.Provider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  auth.me = { id: 1, username: 'alice', is_admin: false }
  auth.checking = false
  clearResearchCache()
})

test('opens a valid code and preserves other query params', async () => {
  renderProbe()
  fireEvent.click(screen.getByText('open'))
  await waitFor(() => expect(screen.getByTestId('code').textContent).toBe('600000'))
  expect(screen.getByTestId('open').textContent).toBe('true')
  const search = screen.getByTestId('search').textContent ?? ''
  expect(search).toContain('date=2026-11-11')
  expect(search).toContain('foo=1')
  expect(search).toContain('stock=600000')
})

test('invalid codes never open the drawer or touch the URL', async () => {
  renderProbe()
  const before = screen.getByTestId('search').textContent
  fireEvent.click(screen.getByText('open-bad'))
  expect(screen.getByTestId('code').textContent).toBe('none')
  expect(screen.getByTestId('open').textContent).toBe('false')
  expect(screen.getByTestId('search').textContent).toBe(before)
})

test('closing removes only the stock param', async () => {
  renderProbe()
  fireEvent.click(screen.getByText('open'))
  await waitFor(() => expect(screen.getByTestId('open').textContent).toBe('true'))
  fireEvent.click(screen.getByText('close'))
  await waitFor(() => expect(screen.getByTestId('open').textContent).toBe('false'))
  const search = screen.getByTestId('search').textContent ?? ''
  expect(search).not.toContain('stock=')
  expect(search).toContain('date=2026-11-11')
  expect(search).toContain('foo=1')
})

test('restores from ?stock= on first render', async () => {
  renderProbe('/limit-up?date=2026-11-11&stock=600000')
  await waitFor(() => expect(screen.getByTestId('code').textContent).toBe('600000'))
  expect(screen.getByTestId('open').textContent).toBe('true')
})

test('as_of is persisted in the URL and restored after refresh', async () => {
  const view = renderProbe()
  fireEvent.click(screen.getByText('open-asof'))
  await waitFor(() => expect(screen.getByTestId('asof').textContent).toBe('2026-09-24'))
  expect(screen.getByTestId('tab').textContent).toBe('evidence')
  const search = screen.getByTestId('search').textContent ?? ''
  expect(search).toContain('stock=600000')
  expect(search).toContain('as_of=2026-09-24')

  view.unmount()
  renderProbe('/limit-up?stock=600000&as_of=2026-09-24')
  await waitFor(() => expect(screen.getByTestId('asof').textContent).toBe('2026-09-24'))
  expect(screen.getByTestId('open').textContent).toBe('true')
})

test('browser navigation away closes the drawer', async () => {
  renderProbe('/limit-up?date=2026-11-11&stock=600000')
  await waitFor(() => expect(screen.getByTestId('open').textContent).toBe('true'))
  fireEvent.click(screen.getByText('navigate-away'))
  await waitFor(() => expect(screen.getByTestId('open').textContent).toBe('false'))
})

test('QuickResearch opens the drawer without navigating away', async () => {
  renderProbe()
  fireEvent.click(screen.getByRole('button', { name: '快速研究 600002' }))
  await waitFor(() => expect(screen.getByTestId('code').textContent).toBe('600002'))
  expect(screen.getByTestId('search').textContent).toContain('/limit-up')
  expect(screen.getByTestId('search').textContent).toContain('stock=600002')
})

test('StockLink keeps its original navigation target', () => {
  render(
    <MemoryRouter initialEntries={['/limit-up']}>
      <WatchlistContext.Provider value={watchValue}>
        <StockLink code="600000">票600000</StockLink>
      </WatchlistContext.Provider>
    </MemoryRouter>,
  )
  expect(screen.getByRole('link').getAttribute('href')).toBe('/stock/600000')
})

test('opening with a tab selects it', async () => {
  renderProbe()
  fireEvent.click(screen.getByText('open-b'))
  await waitFor(() => expect(screen.getByTestId('tab').textContent).toBe('kline'))
})

test('tab selection only changes the active tab', async () => {
  renderProbe('/limit-up?stock=600000')
  await waitFor(() => expect(screen.getByTestId('open').textContent).toBe('true'))
  fireEvent.click(screen.getByText('tab-limit'))
  expect(screen.getByTestId('tab').textContent).toBe('limit')
})

test('switching account clears the selection and the cache', async () => {
  const view = renderProbe('/limit-up?stock=600000')
  await waitFor(() => expect(screen.getByTestId('open').textContent).toBe('true'))
  await loadCached('1|600000|latest|overview', async () => ({ ok: true }))
  expect(researchCacheSize()).toBeGreaterThan(0)

  auth.me = { id: 2, username: 'bob', is_admin: false }
  view.rerender(
    <MemoryRouter initialEntries={['/limit-up?stock=600000']}>
      <WatchlistContext.Provider value={watchValue}>
        <StockResearchProvider>
          <Probe />
        </StockResearchProvider>
      </WatchlistContext.Provider>
    </MemoryRouter>,
  )
  await waitFor(() => expect(screen.getByTestId('open').textContent).toBe('false'))
  expect(researchCacheSize()).toBe(0)
})

test('logged-out users cannot open the drawer', async () => {
  auth.me = null
  renderProbe()
  fireEvent.click(screen.getByText('open'))
  expect(screen.getByTestId('open').textContent).toBe('false')
})

test('logged-out users do not inherit ?stock= from the URL', async () => {
  auth.me = null
  renderProbe('/limit-up?date=2026-11-11&stock=600000')
  await waitFor(() => expect(screen.getByTestId('search').textContent).not.toContain('stock='))
  expect(screen.getByTestId('open').textContent).toBe('false')
  expect(screen.getByTestId('search').textContent).toContain('date=2026-11-11')
})

test('a late response after logout never repopulates the cache', async () => {
  const key = '1|600000|latest|overview-late'
  const epochBefore = researchCacheEpoch()
  let resolveSlow: (value: unknown) => void = () => {}
  const pending = loadCached(key, () => new Promise((resolve) => (resolveSlow = resolve)))

  // 登出：Provider 调 clearResearchCache → epoch 递增
  clearResearchCache()
  expect(researchCacheEpoch()).toBe(epochBefore + 1)

  resolveSlow({ ok: true })
  await expect(pending).resolves.toEqual({ ok: true })
  expect(researchCacheSize()).toBe(0)
})

test('an old finally cannot remove the new epoch inflight request', async () => {
  const key = '1|600000|latest|overview-epoch'
  let resolveOld: (value: unknown) => void = () => {}
  let resolveNew: (value: unknown) => void = () => {}

  const oldTask = loadCached(key, () => new Promise((resolve) => (resolveOld = resolve)))
  clearResearchCache()
  const newTask = loadCached(key, () => new Promise((resolve) => (resolveNew = resolve)))

  expect(researchInflightSize()).toBe(1)
  resolveOld(1)
  await expect(oldTask).resolves.toBe(1)
  // 旧请求的 finally 不能删掉新代次的在途条目
  expect(researchInflightSize()).toBe(1)

  resolveNew(2)
  await expect(newTask).resolves.toBe(2)
  expect(researchInflightSize()).toBe(0)
  expect(researchCacheSize()).toBe(1)
})

test('cache ignores failures and does not cache 401', async () => {
  const key = '1|600000|latest|overview'
  await expect(
    loadCached(key, async () => {
      throw new Error('boom')
    }),
  ).rejects.toThrow('boom')
  expect(researchCacheSize()).toBe(0)

  await expect(
    loadCached(key, async () => {
      throw new ApiError('unauthorized', 401)
    }),
  ).rejects.toBeInstanceOf(ApiError)
  expect(researchCacheSize()).toBe(0)

  await loadCached(key, async () => ({ ok: 1 }))
  expect(researchCacheSize()).toBe(1)
})

test('cache expires after the TTL', async () => {
  vi.useFakeTimers()
  try {
    const key = '1|600000|latest|themes'
    let calls = 0
    const loader = async () => {
      calls += 1
      return calls
    }
    expect(await loadCached(key, loader, 1000)).toBe(1)
    expect(await loadCached(key, loader, 1000)).toBe(1)
    vi.advanceTimersByTime(1500)
    expect(await loadCached(key, loader, 1000)).toBe(2)
  } finally {
    vi.useRealTimers()
  }
})

test('concurrent identical loads share one request', async () => {
  const key = '1|600000|latest|overview-dedup'
  let calls = 0
  const loader = async () => {
    calls += 1
    await new Promise((resolve) => setTimeout(resolve, 0))
    return calls
  }
  const [first, second] = await Promise.all([
    loadCached(key, loader),
    loadCached(key, loader),
  ])
  expect(first).toBe(1)
  expect(second).toBe(1)
  expect(calls).toBe(1)
})

test('multi-window sync broadcasts only minimal payloads and never loops', async () => {
  class FakeChannel {
    static instances: FakeChannel[] = []
    onmessage: ((event: MessageEvent) => void) | null = null
    postMessage = vi.fn()
    close = vi.fn()
    name: string

    constructor(name: string) {
      this.name = name
      FakeChannel.instances.push(this)
    }
  }

  const original = (globalThis as { BroadcastChannel?: unknown }).BroadcastChannel
  vi.stubGlobal('BroadcastChannel', FakeChannel)
  try {
    renderProbe()
    await waitFor(() => expect(FakeChannel.instances.length).toBeGreaterThan(0))
    fireEvent.click(screen.getByText('open'))
    await waitFor(() => expect(screen.getByTestId('open').textContent).toBe('true'))
    const channel = FakeChannel.instances.find(
      (item) => item.postMessage.mock.calls.length > 0,
    )
    expect(channel).toBeTruthy()
    const payload = channel!.postMessage.mock.calls[0][0] as Record<string, unknown>
    expect(payload).toMatchObject({ type: 'open', code: '600000', tab: 'overview' })
    expect(Object.keys(payload).sort()).toEqual(['code', 'scope', 'tab', 'type'])
    expect(JSON.stringify(payload)).not.toContain('alice')

    // 打开动作后可能会重建通道（依赖变化），接收端用当前活着的那一个
    const live = FakeChannel.instances.at(-1)!
    const liveCallsBefore = live.postMessage.mock.calls.length
    // 不同作用域（另一个账户的窗口）：忽略
    live.onmessage?.(
      new MessageEvent('message', {
        data: { type: 'open', code: '600001', tab: 'overview', scope: 'other' },
      }),
    )
    expect(screen.getByTestId('code').textContent).toBe('600000')

    // 同作用域：同步过去，但**不回广播**
    live.onmessage?.(
      new MessageEvent('message', {
        data: { type: 'open', code: '600001', tab: 'kline', scope: payload.scope },
      }),
    )
    await waitFor(() => expect(screen.getByTestId('code').textContent).toBe('600001'))
    expect(screen.getByTestId('tab').textContent).toBe('kline')
    expect(live.postMessage.mock.calls.length).toBe(liveCallsBefore)

    live.onmessage?.(
      new MessageEvent('message', {
        data: { type: 'close', scope: payload.scope },
      }),
    )
    await waitFor(() => expect(screen.getByTestId('open').textContent).toBe('false'))
    expect(live.postMessage.mock.calls.length).toBe(liveCallsBefore)
  } finally {
    vi.unstubAllGlobals()
    ;(globalThis as { BroadcastChannel?: unknown }).BroadcastChannel = original
  }
})
