import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, expect, test, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  dates: vi.fn(),
  promotion: vi.fn(),
  limitPool: vi.fn(),
  limitThemes: vi.fn(),
  promotionFunnel: vi.fn(),
  promotionFunnelDates: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  api: {
    dates: mocks.dates,
    promotion: mocks.promotion,
    limitPool: mocks.limitPool,
    limitThemes: mocks.limitThemes,
    promotionFunnel: mocks.promotionFunnel,
    promotionFunnelDates: mocks.promotionFunnelDates,
  },
  isNoDataError: (err: { status?: number }) => err?.status === 404,
}))

vi.mock('../../components/Layout', () => ({
  default: ({ children, toolbar }: { children: ReactNode; toolbar?: ReactNode }) => (
    <div>
      {toolbar}
      {children}
    </div>
  ),
}))
vi.mock('../../components/Panel', () => ({
  default: ({ title, children }: { title: ReactNode; children: ReactNode }) => (
    <section>
      {title}
      {children}
    </section>
  ),
}))
vi.mock('../../components/Alert', () => ({
  default: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}))
vi.mock('../../components/EChart', () => ({ default: () => null }))
vi.mock('../../components/LadderBoard', () => ({ default: () => null }))
vi.mock('../../components/LimitConclusion', () => ({ default: () => null }))
vi.mock('../../components/LoadState', () => ({ default: () => null }))
vi.mock('../../components/LimitTable', () => ({
  default: ({ type, stocks }: { type: string; stocks: unknown[] }) => (
    <div data-testid={`table-${type}`}>{stocks.length}</div>
  ),
}))
vi.mock('../../components/PromotionFunnel', () => ({
  default: ({
    data,
    loading,
    error,
  }: {
    data: { trade_date: string } | null
    loading: boolean
    error: string | null
  }) => (
    <div data-testid="funnel">
      {error ?? (loading ? 'loading' : data ? `date:${data.trade_date}` : 'empty')}
    </div>
  ),
}))

import LimitReview from '../LimitReview'

const pool = {
  trade_date: '2026-09-24',
  pool_type: 'up',
  total: 0,
  ladder: [],
  stocks: [],
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.dates.mockResolvedValue(['2026-09-24'])
  mocks.promotion.mockResolvedValue(null)
  mocks.limitPool.mockResolvedValue(pool)
  mocks.limitThemes.mockResolvedValue(null)
  mocks.promotionFunnelDates.mockResolvedValue({
    dates: [
      {
        trade_date: '2026-09-24',
        prev_date: '2026-09-23',
        has_previous_pool: true,
        has_today_pool: true,
        quality_status: 'VERIFIED',
        can_calculate_verified_rate: true,
      },
    ],
  })
})

test('switching to funnel tab fetches and renders the funnel', async () => {
  mocks.promotionFunnel.mockResolvedValue({
    trade_date: '2026-09-24',
    prev_date: '2026-09-23',
    quality: {},
    levels: [],
  })
  render(<LimitReview />)
  fireEvent.click(screen.getByRole('button', { name: '晋级漏斗' }))
  await waitFor(() => expect(mocks.promotionFunnel).toHaveBeenCalled())
  await waitFor(() => expect(screen.getByTestId('funnel').textContent).toBe('date:2026-09-24'))
})

test('stale response cannot overwrite a newer date', async () => {
  let resolveFirst: (value: unknown) => void = () => {}
  let resolveSecond: (value: unknown) => void = () => {}
  mocks.promotionFunnel
    .mockImplementationOnce(() => new Promise((resolve) => (resolveFirst = resolve)))
    .mockImplementationOnce(() => new Promise((resolve) => (resolveSecond = resolve)))

  render(<LimitReview />)
  fireEvent.click(screen.getByRole('button', { name: '晋级漏斗' }))
  await waitFor(() => expect(mocks.promotionFunnel).toHaveBeenCalledTimes(1))
  await waitFor(() =>
    expect(screen.getByRole('option', { name: /2026-09-24/ })).toBeTruthy(),
  )

  fireEvent.change(screen.getByRole('combobox'), { target: { value: '2026-09-24' } })
  await waitFor(() => expect(mocks.promotionFunnel).toHaveBeenCalledTimes(2))

  resolveSecond({ trade_date: '2026-09-24', prev_date: '2026-09-23', quality: {}, levels: [] })
  await waitFor(() => expect(screen.getByTestId('funnel').textContent).toBe('date:2026-09-24'))

  resolveFirst({ trade_date: '2026-09-23', prev_date: '2026-09-22', quality: {}, levels: [] })
  await waitFor(() => expect(screen.getByTestId('funnel').textContent).toBe('date:2026-09-24'))
})

test('API failure clears old funnel data', async () => {
  mocks.promotionFunnel
    .mockResolvedValueOnce({ trade_date: '2026-09-23', prev_date: '2026-09-22', quality: {}, levels: [] })
    .mockRejectedValueOnce(new Error('接口炸了'))

  render(<LimitReview />)
  fireEvent.click(screen.getByRole('button', { name: '晋级漏斗' }))
  await waitFor(() => expect(screen.getByTestId('funnel').textContent).toBe('date:2026-09-23'))

  fireEvent.change(screen.getByRole('combobox'), { target: { value: '2026-09-24' } })
  await waitFor(() => expect(screen.getByTestId('funnel').textContent).toBe('接口炸了'))
})
