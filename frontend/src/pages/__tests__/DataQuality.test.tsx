import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import type { QualityOverview, QualityTimeline } from '../../api/types'

const mocks = vi.hoisted(() => ({
  qualityOverview: vi.fn(),
  qualityTimeline: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  api: {
    qualityOverview: mocks.qualityOverview,
    qualityTimeline: mocks.qualityTimeline,
  },
}))

import DataQuality from '../DataQuality'

function item(overrides: Record<string, unknown>) {
  return {
    source: 'limit_pool',
    trade_date: '2026-09-24',
    status: 'VERIFIED_SOURCE',
    reason_codes: [],
    observed_rows: 42,
    expected_rows: null,
    observed_sectors: null,
    expected_sectors: null,
    completion_proof: true,
    latest_observation_date: '2026-09-24',
    latest_valid_snapshot: '2026-09-24',
    latest_attempt: null,
    note: null,
    ...overrides,
  }
}

function overview(): QualityOverview {
  return {
    database: 'test.db',
    as_of: '2026-09-24',
    days: 30,
    window_start: '2026-08-14',
    window_end: '2026-09-24',
    sources: ['stock_daily', 'limit_pool', 'sector_member', 'market_sentiment', 'auction_snapshot', 'trade_calendar'],
    items: [
      item({ source: 'stock_daily', observed_rows: 5000 }),
      item({
        source: 'limit_pool',
        status: 'PARTIAL',
        reasons: [],
        reason_codes: ['LATEST_ATTEMPT_FAILED_SNAPSHOT_RETAINED'],
        latest_attempt: { status: 'failed', created_at: '2026-09-24T20:00:00' },
        note: '旧快照保留',
      }),
      item({
        source: 'sector_member',
        status: 'PARTIAL',
        observed_rows: 1962,
        observed_sectors: 19,
        expected_sectors: 270,
        completion_proof: false,
        reason_codes: ['NO_MEMBERS_COMPLETION_LOG'],
        taxonomies: [{ taxonomy: 'kph_selected', observed_sectors: 19, expected_sectors: 270 }],
      }),
      item({ source: 'market_sentiment', status: 'NOT_COLLECTED', observed_rows: 0, reason_codes: ['SENTIMENT_NOT_COLLECTED'] }),
      item({ source: 'auction_snapshot', status: 'NOT_COLLECTED', observed_rows: 0, reason_codes: ['AUCTION_NOT_COLLECTED'] }),
      item({ source: 'trade_calendar', status: 'VERIFIED_SOURCE', observed_rows: 1 }),
    ],
  } as QualityOverview
}

function timeline(): QualityTimeline {
  return {
    database: 'test.db',
    source: 'limit_pool',
    days: 30,
    window_start: '2026-09-22',
    window_end: '2026-09-24',
    generated_at: '2026-09-24T20:00:00',
    points: [
      item({ trade_date: '2026-09-22', status: 'VERIFIED_SOURCE' }),
      item({ trade_date: '2026-09-23', status: 'PARTIAL' }),
      item({ trade_date: '2026-09-24', status: 'NOT_COLLECTED' }),
    ],
  } as QualityTimeline
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.qualityOverview.mockResolvedValue(overview())
  mocks.qualityTimeline.mockResolvedValue(timeline())
})

function renderPage() {
  return render(
    <MemoryRouter>
      <DataQuality />
    </MemoryRouter>,
  )
}

function table() {
  return within(screen.getByTestId('quality-table'))
}

test('renders sources, statuses and reason codes', async () => {
  renderPage()
  await waitFor(() => expect(table().getByText('个股日线')).toBeTruthy())
  expect(table().getByText('涨停 / 炸板池')).toBeTruthy()
  expect(table().getByText('板块成分快照')).toBeTruthy()
  expect(table().getByText('19 / 270')).toBeTruthy()
  expect(table().getByText(/LATEST_ATTEMPT_FAILED_SNAPSHOT_RETAINED/)).toBeTruthy()
  expect(screen.getByText(/只读诊断，不提供一键修复/)).toBeTruthy()
  expect(screen.getByText(/test\.db/)).toBeTruthy()
})

test('filter hides non-matching sources', async () => {
  renderPage()
  await waitFor(() => expect(table().getByText('个股日线')).toBeTruthy())
  fireEvent.click(screen.getByRole('button', { name: '未采集/不可验证' }))
  await waitFor(() => expect(table().queryByText('个股日线')).toBeNull())
  expect(table().getByText('市场情绪')).toBeTruthy()
  expect(table().getByText('集合竞价')).toBeTruthy()
})

test('expanding a row shows snapshot details', async () => {
  renderPage()
  await waitFor(() => expect(table().getByText('涨停 / 炸板池')).toBeTruthy())
  fireEvent.click(table().getByText('涨停 / 炸板池'))
  await waitFor(() => expect(screen.getByText('最近一次尝试')).toBeTruthy())
  expect(screen.getByText(/failed/)).toBeTruthy()
  expect(screen.getByText('旧快照保留')).toBeTruthy()
})

test('timeline renders one cell per trading day', async () => {
  renderPage()
  await waitFor(() => expect(mocks.qualityTimeline).toHaveBeenCalled())
  await waitFor(() => expect(screen.getByTitle('2026-09-24 · 未采集')).toBeTruthy())
})
