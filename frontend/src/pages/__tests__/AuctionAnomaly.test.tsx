import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { WatchlistContext } from '../../lib/watchlistContext'
import type { AuctionAnomaly } from '../../api/types'

const mocks = vi.hoisted(() => ({
  dates: vi.fn(),
  auctionList: vi.fn(),
  auctionBoards: vi.fn(),
  auctionAnomaly: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  api: {
    dates: mocks.dates,
    auctionList: mocks.auctionList,
    auctionBoards: mocks.auctionBoards,
    auctionAnomaly: mocks.auctionAnomaly,
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

import AuctionPage from '../Auction'

const watchValue = {
  codes: new Set<string>(),
  isWatched: () => false,
  refresh: async () => {},
}

function row(code: string, overrides: Partial<AuctionAnomaly['rows'][number]> = {}) {
  return {
    trade_date: '2026-09-25',
    prev_trade_date: '2026-09-24',
    code,
    name: `票${code}`,
    industry: '电子-半导体',
    concept: '芯片',
    concept_source: '开盘啦',
    concept_as_of: '2026-09-24',
    prev_limit_status: 'limit_up',
    prev_board_height: 1,
    auction_price: 11,
    auction_pct: 10,
    auction_amount: 20_000_000,
    yesterday_amount: 100_000_000,
    auction_amount_ratio: 20,
    post_open_price: 11.5,
    post_open_pct: 15,
    pct_change_after_auction: 5,
    auction_frame: '0926',
    post_open_frame: '0931',
    auction_quality_status: 'VALID',
    post_open_quality_status: 'VALID',
    data_quality_status: 'VALID',
    quality_reasons: [],
    ...overrides,
  }
}

function payload(overrides: Partial<AuctionAnomaly> = {}): AuctionAnomaly {
  const rows = [row('600000'), row('600001')]
  return {
    trade_date: '2026-09-25',
    prev_trade_date: '2026-09-24',
    group: 'prev_limit',
    sort: 'auction_amount',
    order: 'desc',
    limit: 300,
    total: rows.length,
    shown: rows.length,
    rows,
    groups: [
      { group: 'prev_limit', label: '昨日涨停', total: 2, valid: 2, auction_valid_count: 2, post_open_valid_count: 2, fully_valid_count: 2, quality_status: 'VALID', quality_reasons: [] },
      { group: 'prev_broken', label: '昨日炸板', total: 0, valid: 0, auction_valid_count: 0, post_open_valid_count: 0, fully_valid_count: 0, quality_status: 'INCOMPLETE', quality_reasons: ['PREV_BROKEN_POOL_MISSING'] },
      { group: 'prev_failed_relay', label: '昨日断板', total: 0, valid: 0, auction_valid_count: 0, post_open_valid_count: 0, fully_valid_count: 0, quality_status: 'INCOMPLETE', quality_reasons: ['PREV_RELAY_UNVERIFIED'] },
      { group: 'all', label: '全部', total: 2, valid: 2, auction_valid_count: 2, post_open_valid_count: 2, fully_valid_count: 2, quality_status: 'VALID', quality_reasons: [] },
    ],
    quality: {
      status: 'VALID',
      reasons: [],
      frames: [
        {
          scheduled_frame: '0926', frame_kind: 'result', late: false,
          actual_started_at: '2026-09-25T09:26:05+08:00',
          source_quote_time: '20260925092605', source_quote_time_min: '20260925092605',
          raw_source_quote_time_min: '20260925092605', raw_source_quote_time_max: '20260925092605',
          requested_codes: 2, returned_codes: 2, valid_codes: 2,
          dropped_old_quote: 0, dropped_stale_quote: 0,
          dropped_newer_quote: 0, dropped_unknown_quote: 0, source_time_unknown: 0,
          source_time_unknown_count: 0, source_time_unknown_codes_truncated: false,
          dropped_old_codes_truncated: false, dropped_stale_codes_truncated: false,
          dropped_newer_codes_truncated: false, dropped_unknown_codes_truncated: false,
          failed_batches: 0, total_batches: 1, written_codes: 2,
          already_covered_codes: 0, ignored_not_newer_codes: 0,
          fetch_requested_codes: 2, eligible_existing_codes: 2, raw_returned_codes: 2,
          missing_quote_codes: 0, filtered_out_codes: 0,
          coverage_ratio: 1, coverage_status: 'FULL', quote_times_count: 2,
          noop: false, status: 'ok', reason: null, elapsed_ms: 120,
        },
        {
          scheduled_frame: '0931', frame_kind: 'post_open', late: false,
          actual_started_at: '2026-09-25T09:31:05+08:00',
          source_quote_time: '20260925093105', source_quote_time_min: '20260925093105',
          raw_source_quote_time_min: '20260925093105', raw_source_quote_time_max: '20260925093105',
          requested_codes: 2, returned_codes: 2, valid_codes: 2,
          dropped_old_quote: 0, dropped_stale_quote: 0,
          dropped_newer_quote: 0, dropped_unknown_quote: 0, source_time_unknown: 0,
          source_time_unknown_count: 0, source_time_unknown_codes_truncated: false,
          dropped_old_codes_truncated: false, dropped_stale_codes_truncated: false,
          dropped_newer_codes_truncated: false, dropped_unknown_codes_truncated: false,
          failed_batches: 0, total_batches: 1, written_codes: 2,
          already_covered_codes: 0, ignored_not_newer_codes: 0,
          fetch_requested_codes: 2, eligible_existing_codes: 2, raw_returned_codes: 2,
          missing_quote_codes: 0, filtered_out_codes: 0,
          coverage_ratio: 1, coverage_status: 'FULL', quote_times_count: 2,
          noop: false, status: 'ok', reason: null, elapsed_ms: 110,
        },
      ],
      auction_count: 2,
      result_count: 2,
      post_open_count: 2,
      auction_coverage_ratio: 1,
      auction_coverage_status: 'FULL',
      post_open_coverage_ratio: 1,
      post_open_coverage_status: 'FULL',
    },
    ...overrides,
  }
}

function renderPage() {
  return render(
    <MemoryRouter>
      <WatchlistContext.Provider value={watchValue}>
        <AuctionPage />
      </WatchlistContext.Provider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.dates.mockResolvedValue(['2026-09-25', '2026-09-24'])
  mocks.auctionBoards.mockResolvedValue({ trade_date: '2026-09-25', items: [] })
  mocks.auctionList.mockResolvedValue({
    trade_date: '2026-09-25',
    total: 0,
    shown: 0,
    rows: [],
    summary: null,
  })
  mocks.auctionAnomaly.mockResolvedValue(payload())
})

test('keeps the old intensity page as the default tab', async () => {
  renderPage()
  expect(screen.getAllByText('竞价强度').length).toBeGreaterThan(0)
  await waitFor(() => expect(mocks.auctionList).toHaveBeenCalled())
  expect(screen.getByText(/当日还没有竞价数据/)).toBeTruthy()
})

test('opens the anomaly tab and renders summary, categories and percentage-point column', async () => {
  renderPage()
  fireEvent.click(screen.getByRole('button', { name: '竞价异动' }))
  await waitFor(() => expect(mocks.auctionAnomaly).toHaveBeenCalled())
  expect(screen.getByRole('button', { name: /9:25→9:31 涨幅变化/ })).toBeTruthy()
  expect(screen.getByText('票600000')).toBeTruthy()
  expect(screen.getAllByText('+5.00pp').length).toBeGreaterThan(0)
  expect(screen.getByText(/帧级采集质量/)).toBeTruthy()
  expect(screen.getAllByText('昨日涨停').length).toBeGreaterThan(0)
  expect(screen.getAllByText(/总 2 · 竞价 2 · 承接 2 · 全 2/).length).toBeGreaterThan(0)
  expect(screen.getAllByText(/2026-09-24/).length).toBeGreaterThan(0)
})

test('unknown coverage is shown as dash, never 0%', async () => {
  mocks.auctionAnomaly.mockResolvedValue(
    payload({
      quality: {
        ...payload().quality,
        auction_coverage_ratio: null,
        auction_coverage_status: null,
        post_open_coverage_ratio: null,
        post_open_coverage_status: null,
      },
    }),
  )
  renderPage()
  fireEvent.click(screen.getByRole('button', { name: '竞价异动' }))
  await waitFor(() => expect(mocks.auctionAnomaly).toHaveBeenCalled())
  const coverage = screen.getByText(/0926 覆盖/)
  expect(coverage.textContent).toContain('0926 覆盖 —')
  expect(coverage.textContent).not.toContain('0.0%')
})

test('switching category requests the new group', async () => {
  mocks.auctionAnomaly.mockImplementation((options: { group?: string }) => {
    if (options.group === 'prev_broken') {
      return Promise.resolve(
        payload({
          group: 'prev_broken',
          rows: [row('600001', { prev_limit_status: 'broken', auction_pct: -5 })],
        }),
      )
    }
    return Promise.resolve(payload())
  })
  renderPage()
  fireEvent.click(screen.getByRole('button', { name: '竞价异动' }))
  await waitFor(() => expect(screen.getByText('票600000')).toBeTruthy())
  fireEvent.click(screen.getByRole('button', { name: /昨日炸板/ }))
  await waitFor(() =>
    expect(mocks.auctionAnomaly).toHaveBeenLastCalledWith(
      expect.objectContaining({ group: 'prev_broken' }),
    ),
  )
  await waitFor(() => expect(screen.getByText('票600001')).toBeTruthy())
  expect(screen.queryByText('票600000')).toBeNull()
})

test('date change clears old result and a late stale response cannot overwrite', async () => {
  let resolveFirst: (value: AuctionAnomaly) => void = () => {}
  let resolveSecond: (value: AuctionAnomaly) => void = () => {}
  mocks.auctionAnomaly
    .mockImplementationOnce(() => new Promise((resolve) => (resolveFirst = resolve)))
    .mockImplementationOnce(() => new Promise((resolve) => (resolveSecond = resolve)))

  renderPage()
  fireEvent.click(screen.getByRole('button', { name: '竞价异动' }))
  await waitFor(() => expect(mocks.auctionAnomaly).toHaveBeenCalledTimes(1))
  fireEvent.change(screen.getByRole('combobox'), { target: { value: '2026-09-24' } })
  await waitFor(() => expect(mocks.auctionAnomaly).toHaveBeenCalledTimes(2))

  resolveSecond(payload({ trade_date: '2026-09-24', rows: [row('600002')] }))
  await waitFor(() => expect(screen.getByText('票600002')).toBeTruthy())
  resolveFirst(payload({ trade_date: '2026-09-25', rows: [row('600000')] }))
  await waitFor(() => expect(screen.getByText('票600002')).toBeTruthy())
  expect(screen.queryByText('票600000')).toBeNull()
})

test('API error clears old rows and shows the failure', async () => {
  mocks.auctionAnomaly
    .mockResolvedValueOnce(payload())
    .mockRejectedValueOnce(new Error('接口炸了'))
  renderPage()
  fireEvent.click(screen.getByRole('button', { name: '竞价异动' }))
  await waitFor(() => expect(screen.getByText('票600000')).toBeTruthy())
  fireEvent.click(screen.getByRole('button', { name: /昨日炸板/ }))
  await waitFor(() => expect(screen.getByText(/接口炸了/)).toBeTruthy())
  expect(screen.queryByText('票600000')).toBeNull()
})

test('missing 0926 shows dash, never -100%', async () => {
  mocks.auctionAnomaly.mockResolvedValue(
    payload({
      rows: [
        row('600003', {
          auction_price: null,
          auction_pct: null,
          auction_amount: null,
          auction_amount_ratio: null,
          post_open_price: null,
          post_open_pct: null,
          pct_change_after_auction: null,
          auction_frame: null,
          post_open_frame: null,
          auction_quality_status: 'FRAME_MISSING',
          post_open_quality_status: 'NO_QUOTE',
          data_quality_status: 'FRAME_MISSING',
          quality_reasons: ['NO_0926_QUOTE', 'NO_POST_OPEN_QUOTE'],
        }),
      ],
    }),
  )
  renderPage()
  fireEvent.click(screen.getByRole('button', { name: '竞价异动' }))
  await waitFor(() => expect(screen.getByText('票600003')).toBeTruthy())
  expect(screen.queryByText('-100.00%')).toBeNull()
  expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  expect(screen.getAllByText(/缺少 0926 竞价终值/).length).toBeGreaterThan(0)
})

test('valid 0926 with missing 0931 keeps auction result but post-open is dash', async () => {
  mocks.auctionAnomaly.mockResolvedValue(
    payload({
      rows: [
        row('600004', {
          post_open_price: null,
          post_open_pct: null,
          pct_change_after_auction: null,
          post_open_frame: null,
          post_open_quality_status: 'NO_QUOTE',
          data_quality_status: 'INCOMPLETE',
          quality_reasons: ['NO_POST_OPEN_QUOTE'],
        }),
      ],
    }),
  )
  renderPage()
  fireEvent.click(screen.getByRole('button', { name: '竞价异动' }))
  await waitFor(() => expect(screen.getByText('票600004')).toBeTruthy())
  expect(screen.getAllByText('+10.00%').length).toBeGreaterThan(0)
  expect(screen.queryByText('+15.00%')).toBeNull()
  expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  expect(screen.getAllByText(/没有 0931 开盘后报价/).length).toBeGreaterThan(0)
})

test('post-open pending is shown as 待观察, not a failure', async () => {
  mocks.auctionAnomaly.mockResolvedValue(
    payload({
      rows: [
        row('600005', {
          post_open_price: null,
          post_open_pct: null,
          pct_change_after_auction: null,
          post_open_frame: null,
          post_open_quality_status: 'PENDING',
          data_quality_status: 'PENDING',
          quality_reasons: ['POST_OPEN_PENDING'],
        }),
      ],
    }),
  )
  renderPage()
  fireEvent.click(screen.getByRole('button', { name: '竞价异动' }))
  await waitFor(() => expect(screen.getByText('票600005')).toBeTruthy())
  expect(screen.getAllByText(/待观察/).length).toBeGreaterThan(0)
  expect(screen.getAllByText(/9:31 尚未到达/).length).toBeGreaterThan(0)
  expect(screen.queryByText(/采集失败/)).toBeNull()
})

test('partial 0926 frame never gets the fully trusted label', async () => {
  mocks.auctionAnomaly.mockResolvedValue(
    payload({
      quality: {
        ...payload().quality,
        status: 'INCOMPLETE',
        reasons: ['FRAME_0926_PARTIAL'],
      },
      rows: [
        row('600006', {
          auction_quality_status: 'INCOMPLETE',
          data_quality_status: 'INCOMPLETE',
          quality_reasons: ['FRAME_0926_PARTIAL'],
        }),
      ],
    }),
  )
  renderPage()
  fireEvent.click(screen.getByRole('button', { name: '竞价异动' }))
  await waitFor(() => expect(screen.getByText('票600006')).toBeTruthy())
  expect(screen.getAllByText('部分可信').length).toBeGreaterThan(0)
  expect(screen.getAllByText(/0926 帧部分可信/).length).toBeGreaterThan(0)
  expect(screen.queryByText('完整可信')).toBeNull()
})

test('stock links point at the stock detail page', async () => {
  renderPage()
  fireEvent.click(screen.getByRole('button', { name: '竞价异动' }))
  await waitFor(() => expect(screen.getByText('票600000')).toBeTruthy())
  const link = screen.getByText('票600000').closest('a')
  expect(link?.getAttribute('href')).toBe('/stock/600000')
})

test('clicking a sortable column requests that sort both directions', async () => {
  renderPage()
  fireEvent.click(screen.getByRole('button', { name: '竞价异动' }))
  await waitFor(() => expect(mocks.auctionAnomaly).toHaveBeenCalled())
  fireEvent.click(screen.getByRole('button', { name: /竞价涨幅/ }))
  await waitFor(() =>
    expect(mocks.auctionAnomaly).toHaveBeenLastCalledWith(
      expect.objectContaining({ sort: 'auction_pct', order: 'desc' }),
    ),
  )
  fireEvent.click(screen.getByRole('button', { name: /竞价涨幅/ }))
  await waitFor(() =>
    expect(mocks.auctionAnomaly).toHaveBeenLastCalledWith(
      expect.objectContaining({ sort: 'auction_pct', order: 'asc' }),
    ),
  )
})
