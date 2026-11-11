import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { expect, test } from 'vitest'
import { WatchlistContext } from '../../lib/watchlistContext'
import type { PromotionFunnel as Funnel, PromotionFunnelStock } from '../../api/types'
import PromotionFunnel from '../PromotionFunnel'

const watchValue = {
  codes: new Set<string>(),
  isWatched: () => false,
  refresh: async () => {},
}

function stock(
  code: string,
  status: PromotionFunnelStock['status'],
  overrides: Partial<PromotionFunnelStock> = {},
): PromotionFunnelStock {
  return {
    code,
    name: `票${code}`,
    previous_level: 1,
    today_consecutive: status === 'PROMOTED' ? 2 : null,
    status,
    reason_code: status === 'UNVERIFIABLE' ? 'DAILY_QUOTE_MISSING' : null,
    reason_text: status === 'UNVERIFIABLE' ? '今日无有效日线' : null,
    today_pool_type: status === 'PROMOTED' ? 'up' : status === 'BROKEN' ? 'broken' : null,
    pct_chg: status === 'PROMOTED' ? 10 : status === 'FAILED' ? -2.5 : null,
    close: status === 'UNVERIFIABLE' ? null : 10,
    board: status === 'PROMOTED' ? '芯片' : null,
    industry: '半导体',
    first_seal_time: status === 'PROMOTED' ? '093101' : null,
    open_times: status === 'PROMOTED' ? 0 : null,
    seal_amount: status === 'PROMOTED' ? 100000000 : null,
    quote_date: status === 'UNVERIFIABLE' ? null : '2026-09-24',
    data_quality: status === 'UNVERIFIABLE' ? 'UNVERIFIED' : 'VERIFIED',
    ...overrides,
  }
}

const data: Funnel = {
  trade_date: '2026-09-24',
  prev_date: '2026-09-23',
  quality: {
    status: 'VERIFIED',
    reasons: [],
    prev_up: 'ok',
    today_up: 'ok',
    today_broken: 'ok',
    today_daily: 'ok',
  },
  levels: [
    {
      level: 1,
      label: '1进2',
      base: 3,
      promoted: 1,
      broken: 1,
      failed: 1,
      unverifiable: 0,
      valid_base: 3,
      legacy_rate: 33.3,
      verified_rate: 33.3,
      stocks: [
        stock('600000', 'PROMOTED'),
        stock('600001', 'BROKEN'),
        stock('600002', 'FAILED'),
      ],
    },
    {
      level: 2,
      label: '2进3',
      base: 1,
      promoted: 0,
      broken: 0,
      failed: 0,
      unverifiable: 1,
      valid_base: 0,
      legacy_rate: 0,
      verified_rate: null,
      stocks: [stock('600003', 'UNVERIFIABLE')],
    },
    {
      level: 4,
      label: '4进5',
      base: 0,
      promoted: 0,
      broken: 0,
      failed: 0,
      unverifiable: 0,
      valid_base: 0,
      legacy_rate: null,
      verified_rate: null,
      stocks: [],
    },
  ],
}

function renderFunnel(override?: Partial<Funnel>, props?: { loading?: boolean; error?: string | null }) {
  const value = override ? { ...data, ...override } : data
  return render(
    <MemoryRouter>
      <WatchlistContext.Provider value={watchValue}>
        <PromotionFunnel
          data={value}
          loading={props?.loading ?? false}
          error={props?.error ?? null}
        />
      </WatchlistContext.Provider>
    </MemoryRouter>,
  )
}

test('renders summary, quality and level cards', () => {
  renderFunnel()
  expect(screen.getByText('已验证')).toBeTruthy()
  expect(screen.getByText('观察日 2026-09-24 · 基准日 2026-09-23')).toBeTruthy()
  expect(screen.getAllByText('1进2').length).toBeGreaterThan(0)
  expect(screen.getAllByText('2进3').length).toBeGreaterThan(0)
  expect(screen.getByText('昨日基数')).toBeTruthy()
  expect(screen.getByText('原口径晋级率')).toBeTruthy()
  expect(screen.getByText('可验证口径')).toBeTruthy()
})

test('default selects first level and shows its stocks', () => {
  renderFunnel()
  expect(screen.getByText('票600000')).toBeTruthy()
  expect(screen.getByText('票600001')).toBeTruthy()
  expect(screen.getByText('票600002')).toBeTruthy()
  expect(screen.queryByText('票600003')).toBeNull()
})

test('clicking a level switches stock table', () => {
  renderFunnel()
  fireEvent.click(screen.getByRole('button', { name: '档位 2进3' }))
  expect(screen.getByText('票600003')).toBeTruthy()
  expect(screen.queryByText('票600000')).toBeNull()
})

test('filter by status', () => {
  renderFunnel()
  fireEvent.click(screen.getByRole('button', { name: '筛选晋级' }))
  expect(screen.getByText('票600000')).toBeTruthy()
  expect(screen.queryByText('票600001')).toBeNull()
  expect(screen.queryByText('票600002')).toBeNull()
})

test('unverifiable filter and empty state', () => {
  renderFunnel()
  fireEvent.click(screen.getByRole('button', { name: '档位 2进3' }))
  fireEvent.click(screen.getByRole('button', { name: '筛选不可验证' }))
  expect(screen.getByText('票600003')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '筛选晋级' }))
  expect(screen.getByText('当前筛选下没有股票')).toBeTruthy()
})

test('unverified reason is visible', () => {
  renderFunnel()
  fireEvent.click(screen.getByRole('button', { name: '档位 2进3' }))
  expect(screen.getByText('今日无有效日线')).toBeTruthy()
  expect(screen.getAllByText('不可验证').length).toBeGreaterThan(0)
})

test('base zero shows dashes for both rates', () => {
  renderFunnel()
  const card = screen.getByRole('button', { name: '档位 4进5' })
  expect(card.textContent).toContain('原口径 —')
  expect(card.textContent).toContain('可验证 —')
})

test('legacy and verified labels stay distinct', () => {
  renderFunnel()
  expect(screen.getByText('原口径 33.3%')).toBeTruthy()
  expect(screen.getByText('可验证 33.3%')).toBeTruthy()
  expect(screen.getAllByText(/原口径/).length).toBeGreaterThan(1)
})

test('stock link points at stock detail', () => {
  renderFunnel()
  const link = screen.getByText('票600000').closest('a')
  expect(link?.getAttribute('href')).toBe('/stock/600000')
})

test('missing board shows dash', () => {
  renderFunnel()
  expect(screen.getByText('票600001')).toBeTruthy()
  expect(screen.getAllByText('—').length).toBeGreaterThan(0)
})

test('unverified quality shows reasons', () => {
  renderFunnel({
    quality: {
      status: 'PARTIAL',
      reasons: ['TODAY_BROKEN_POOL_MISSING'],
      prev_up: 'ok',
      today_up: 'ok',
      today_broken: 'not_collected',
      today_daily: 'ok',
    },
  })
  expect(screen.getByText('部分可验证')).toBeTruthy()
  expect(screen.getByText('今日炸板池无数据')).toBeTruthy()
})

test('loading and error states', () => {
  const { unmount } = render(
    <MemoryRouter>
      <WatchlistContext.Provider value={watchValue}>
        <PromotionFunnel data={null} loading error={null} />
      </WatchlistContext.Provider>
    </MemoryRouter>,
  )
  expect(screen.getByText('加载中…')).toBeTruthy()
  unmount()
  renderFunnel(undefined, { error: '接口炸了' })
  expect(screen.getByText('接口炸了')).toBeTruthy()
})

test('no levels shows explicit message', () => {
  renderFunnel({
    levels: [],
    quality: {
      status: 'NOT_COLLECTED',
      reasons: ['PREVIOUS_UP_POOL_MISSING'],
      prev_up: 'not_collected',
      today_up: 'ok',
      today_broken: 'ok',
      today_daily: 'ok',
    },
  })
  expect(screen.getByText('未采集')).toBeTruthy()
  expect(screen.getByText(/没有可用的昨日涨停池/)).toBeTruthy()
})
