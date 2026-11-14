import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import type { StockResearchValue } from '../../lib/stockResearchContext'

const mocks = vi.hoisted(() => ({
  stockProfile: vi.fn(),
  stockThemes: vi.fn(),
  stockDde: vi.fn(),
  stockLimitHistory: vi.fn(),
  stockLhb: vi.fn(),
  stockDaily: vi.fn(),
  syncStock: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  api: {
    stockProfile: mocks.stockProfile,
    stockThemes: mocks.stockThemes,
    stockDde: mocks.stockDde,
    stockLimitHistory: mocks.stockLimitHistory,
    stockLhb: mocks.stockLhb,
    stockDaily: mocks.stockDaily,
    syncStock: mocks.syncStock,
  },
  isNoDataError: (err: { status?: number }) => err?.status === 404,
}))

vi.mock('../../components/EChart', () => ({ default: () => null }))
vi.mock('../../components/KLineChart', () => ({ default: () => <div>KLINE</div> }))

vi.mock('../../lib/authContext', () => ({
  useAuth: () => ({ me: { id: 1, username: 'alice', is_admin: false }, checking: false }),
}))

import { clearResearchCache } from '../../lib/stockResearchCache'
import { StockResearchContext } from '../../lib/stockResearchContext'
import { WatchlistContext } from '../../lib/watchlistContext'
import StockResearchDrawer from '../../components/StockResearchDrawer'

const watchValue = {
  codes: new Set<string>(),
  isWatched: () => false,
  refresh: async () => {},
}

function profile(overrides: Record<string, unknown> = {}) {
  return {
    code: '600000',
    name: '浦发银行',
    in_watchlist: false,
    note: null,
    latest: {
      trade_date: '2026-11-11',
      open: 10,
      high: 10.5,
      low: 9.9,
      close: 10.4,
      pct_chg: 2.5,
      volume: 1e6,
      amount: 1e9,
      turnover: 1.2,
      is_limit_up: false,
      is_limit_down: false,
    },
    day_count: 250,
    first_date: '2025-11-01',
    last_date: '2026-11-11',
    limit_up_dates: ['2026-11-01', '2026-10-20'],
    lhb_count: 3,
    pct_chg_5d: 4.2,
    total_mv: 3e11,
    free_float_mv: 2e11,
    actual_turnover: 0.8,
    pe_forecast: 9.5,
    total_mv_asof: '2026-11-08',
    phase: null,
    ...overrides,
  }
}

function value(overrides: Partial<StockResearchValue> = {}): StockResearchValue {
  return {
    code: '600000',
    sourcePage: '/limit-up',
    selectedAsOfDate: '2026-11-11',
    open: true,
    activeTab: 'overview',
    openStockResearch: vi.fn(),
    closeStockResearch: vi.fn(),
    selectResearchTab: vi.fn(),
    multiWindowSync: true,
    setMultiWindowSync: vi.fn(),
    ...overrides,
  }
}

function renderDrawer(overrides: Partial<StockResearchValue> = {}) {
  return render(
    <MemoryRouter initialEntries={['/limit-up']}>
      <WatchlistContext.Provider value={watchValue}>
        <StockResearchContext.Provider value={value(overrides)}>
          <StockResearchDrawer />
        </StockResearchContext.Provider>
      </WatchlistContext.Provider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  clearResearchCache()
  mocks.stockProfile.mockResolvedValue(profile())
  mocks.stockThemes.mockResolvedValue({
    code: '600000',
    name: '浦发银行',
    board_date: '2026-11-11',
    themes: [{ concept: '银行', board_code: 'BK001', pct_chg: 1.2 }],
    suppressed: [],
  })
  mocks.stockDde.mockResolvedValue({
    code: '600000',
    name: '浦发银行',
    days: 2,
    rows: [
      { trade_date: '2026-11-10', net_inflow: 1e8, dde: 2e8 },
      { trade_date: '2026-11-11', net_inflow: -5e7, dde: 1.5e8 },
    ],
    note: null,
  })
  mocks.stockLimitHistory.mockResolvedValue([
    {
      trade_date: '2026-11-01',
      pool_type: 'up',
      name: '浦发银行',
      pct_chg: 10,
      price: 10.4,
      consecutive: 2,
      seal_amount: 5e8,
      first_seal_time: '093000',
      open_times: 0,
    },
  ])
  mocks.stockLhb.mockResolvedValue([
    {
      trade_date: '2026-10-30',
      code: '600000',
      name: '浦发银行',
      reason: '日涨幅偏离值达7%',
      close: 10,
      pct_chg: 8,
      net_buy: 3e7,
      buy_amount: 5e7,
      sell_amount: 2e7,
      interpretation: null,
    },
  ])
  mocks.stockDaily.mockResolvedValue([
    {
      trade_date: '2026-11-11',
      open: 10,
      high: 10.5,
      low: 9.9,
      close: 10.4,
      pct_chg: 2.5,
      volume: 1e6,
      amount: 1e9,
      turnover: 1.2,
      is_limit_up: false,
      is_limit_down: false,
    },
  ])
})

test('overview shows latest local data and profile fields', async () => {
  renderDrawer()
  await waitFor(() => expect(screen.getAllByText('浦发银行').length).toBeGreaterThan(0))
  expect(screen.getAllByText('2026-11-11').length).toBeGreaterThan(0)
  expect(screen.getByText(/\+2\.50%/)).toBeTruthy()
  expect(screen.getByText('250 根（2025-11-01 ~ 2026-11-11）')).toBeTruthy()
})

test('only the active tab requests its own endpoint', async () => {
  renderDrawer()
  await waitFor(() => expect(mocks.stockProfile).toHaveBeenCalled())
  expect(mocks.stockThemes).not.toHaveBeenCalled()
  expect(mocks.stockDde).not.toHaveBeenCalled()
  expect(mocks.stockLhb).not.toHaveBeenCalled()
})

test('themes tab loads only themes', async () => {
  renderDrawer({ activeTab: 'themes' })
  await waitFor(() => expect(mocks.stockThemes).toHaveBeenCalledWith('600000'))
  expect(screen.getByText('银行')).toBeTruthy()
  expect(mocks.stockLhb).not.toHaveBeenCalled()
})

test('kline tab uses local daily data and shows empty hint when missing', async () => {
  mocks.stockDaily.mockResolvedValueOnce([])
  renderDrawer({ activeTab: 'kline' })
  await waitFor(() => expect(screen.getByText(/本地暂无 K 线/)).toBeTruthy())
  expect(mocks.stockDaily).toHaveBeenCalledWith(
    '600000',
    expect.objectContaining({ period: 'day', fq: 'qfq' }),
  )
})

test('funds tab reads DDE without triggering live fetch', async () => {
  renderDrawer({ activeTab: 'funds' })
  await waitFor(() => expect(mocks.stockDde).toHaveBeenCalledWith('600000', 30, false))
})

test('limit and lhb tabs render their read-only records', async () => {
  const view = renderDrawer({ activeTab: 'limit' })
  await waitFor(() => expect(screen.getByText('2 板')).toBeTruthy())
  view.rerender(
    <MemoryRouter initialEntries={['/limit-up']}>
      <WatchlistContext.Provider value={watchValue}>
        <StockResearchContext.Provider value={value({ activeTab: 'lhb' })}>
          <StockResearchDrawer />
        </StockResearchContext.Provider>
      </WatchlistContext.Provider>
    </MemoryRouter>,
  )
  await waitFor(() => expect(screen.getByText('日涨幅偏离值达7%')).toBeTruthy())
})

test('close button calls closeStockResearch', async () => {
  const close = vi.fn()
  renderDrawer({ closeStockResearch: close })
  await waitFor(() => expect(mocks.stockProfile).toHaveBeenCalled())
  fireEvent.click(screen.getByRole('button', { name: '关闭研究侧栏' }))
  expect(close).toHaveBeenCalledTimes(1)
})

test('reopening the same stock reuses the short-term cache', async () => {
  const view = renderDrawer()
  await waitFor(() => expect(mocks.stockProfile).toHaveBeenCalledTimes(1)) // 标题与概览合并成一次
  const callsAfterFirst = mocks.stockProfile.mock.calls.length
  view.rerender(
    <MemoryRouter initialEntries={['/limit-up']}>
      <WatchlistContext.Provider value={watchValue}>
        <StockResearchContext.Provider value={value({ open: false, code: null })}>
          <StockResearchDrawer />
        </StockResearchContext.Provider>
      </WatchlistContext.Provider>
    </MemoryRouter>,
  )
  view.rerender(
    <MemoryRouter initialEntries={['/limit-up']}>
      <WatchlistContext.Provider value={watchValue}>
        <StockResearchContext.Provider value={value()}>
          <StockResearchDrawer />
        </StockResearchContext.Provider>
      </WatchlistContext.Provider>
    </MemoryRouter>,
  )
  await waitFor(() => expect(screen.getAllByText('浦发银行').length).toBeGreaterThan(0))
  expect(mocks.stockProfile.mock.calls.length).toBe(callsAfterFirst)
})

test('a slow previous stock cannot override the newly selected stock', async () => {
  let resolveSlow: (value: unknown) => void = () => {}
  mocks.stockProfile
    .mockImplementationOnce(
      () => new Promise((resolve) => (resolveSlow = resolve)),
    )
    .mockResolvedValue(profile({ code: '600001', name: '邯郸钢铁' }))

  const view = renderDrawer()
  view.rerender(
    <MemoryRouter initialEntries={['/limit-up']}>
      <WatchlistContext.Provider value={watchValue}>
        <StockResearchContext.Provider value={value({ code: '600001' })}>
          <StockResearchDrawer />
        </StockResearchContext.Provider>
      </WatchlistContext.Provider>
    </MemoryRouter>,
  )
  await waitFor(() => expect(screen.getAllByText('邯郸钢铁').length).toBeGreaterThan(0))
  resolveSlow(profile())
  await new Promise((resolve) => setTimeout(resolve, 0))
  expect(screen.queryByText('浦发银行')).toBeNull()
})

test('API failures surface an error and still allow closing', async () => {
  mocks.stockProfile.mockRejectedValue(new Error('接口超时'))
  const close = vi.fn()
  renderDrawer({ closeStockResearch: close })
  await waitFor(() => expect(screen.getAllByText('加载失败').length).toBeGreaterThan(0))
  fireEvent.click(screen.getByRole('button', { name: '关闭研究侧栏' }))
  expect(close).toHaveBeenCalled()
})

test('changing the data date refetches instead of reusing the cache', async () => {
  const view = renderDrawer()
  await waitFor(() => expect(mocks.stockProfile).toHaveBeenCalledTimes(1))
  const calls = mocks.stockProfile.mock.calls.length
  view.rerender(
    <MemoryRouter initialEntries={['/limit-up']}>
      <WatchlistContext.Provider value={watchValue}>
        <StockResearchContext.Provider value={value({ selectedAsOfDate: '2026-11-10' })}>
          <StockResearchDrawer />
        </StockResearchContext.Provider>
      </WatchlistContext.Provider>
    </MemoryRouter>,
  )
  await waitFor(() =>
    expect(mocks.stockProfile.mock.calls.length).toBeGreaterThan(calls),
  )
})

test('the drawer never triggers a history sync', async () => {
  renderDrawer()
  await waitFor(() => expect(mocks.stockProfile).toHaveBeenCalled())
  expect(mocks.syncStock).not.toHaveBeenCalled()
})

test('multi-window toggle calls the provider setter', async () => {
  const setSync = vi.fn()
  renderDrawer({ setMultiWindowSync: setSync })
  await waitFor(() => expect(mocks.stockProfile).toHaveBeenCalled())
  fireEvent.click(screen.getByRole('checkbox'))
  expect(setSync).toHaveBeenCalledWith(false)
})

test('historical observation date is not printed as the data date', async () => {
  renderDrawer({ selectedAsOfDate: '2026-09-24' })
  await waitFor(() => expect(screen.getAllByText('浦发银行').length).toBeGreaterThan(0))
  expect(screen.getByText(/来源观察日 2026-09-24/)).toBeTruthy()
  expect(screen.getByText(/不代表所选历史日当时已知/)).toBeTruthy()
  // 侧栏只有最新本地资料：绝不能出现「数据日 2026-09-24」这种伪历史标注
  expect(screen.queryByText(/数据日 2026-09-24/)).toBeNull()
  // 实际数据日单独标注
  expect(screen.getAllByText(/截至 2026-11-11/).length).toBeGreaterThan(0)
})

test('themes tab says historical snapshot is missing instead of faking it', async () => {
  renderDrawer({ activeTab: 'themes', selectedAsOfDate: '2026-09-24' })
  await waitFor(() => expect(screen.getByText('银行')).toBeTruthy())
  expect(screen.getByText(/历史题材缺失/)).toBeTruthy()
  expect(screen.getAllByText(/2026-11-11/).length).toBeGreaterThan(0)
})

test('focus moves into the drawer and Escape restores it to the trigger', async () => {
  const trigger = document.createElement('button')
  trigger.textContent = '研'
  document.body.appendChild(trigger)
  trigger.focus()

  const close = vi.fn()
  const view = renderDrawer({ closeStockResearch: close })
  await waitFor(() =>
    expect(document.activeElement).toBe(
      screen.getByRole('button', { name: '关闭研究侧栏' }),
    ),
  )

  fireEvent.keyDown(document, { key: 'Escape' })
  expect(close).toHaveBeenCalledTimes(1)

  view.rerender(
    <MemoryRouter initialEntries={['/limit-up']}>
      <WatchlistContext.Provider value={watchValue}>
        <StockResearchContext.Provider value={value({ open: false, code: null })}>
          <StockResearchDrawer />
        </StockResearchContext.Provider>
      </WatchlistContext.Provider>
    </MemoryRouter>,
  )
  await waitFor(() => expect(document.activeElement).toBe(trigger))
  trigger.remove()
})
