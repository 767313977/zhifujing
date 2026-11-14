import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import type { SectorResonance as ResonanceData, SectorQuote } from '../../api/types'

const mocks = vi.hoisted(() => ({
  sectorResonance: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  api: { sectorResonance: mocks.sectorResonance },
}))

import { WatchlistContext } from '../../lib/watchlistContext'
import SectorResonance from '../SectorResonance'

const watchValue = {
  codes: new Set<string>(),
  isWatched: () => false,
  refresh: async () => {},
}

const boards: SectorQuote[] = [
  { code: '801001', name: '芯片', pct_chg: 2.1 },
  { code: '801002', name: '软件', pct_chg: 1.2 },
] as SectorQuote[]

function stats(overrides: Record<string, unknown> = {}) {
  return {
    sample_count: 2,
    valid_count: 2,
    missing_count: 0,
    avg_pct: 1.5,
    median_pct: 1.5,
    outcome_up_count: 1,
    outcome_down_count: 1,
    outcome_flat_count: 0,
    up_ratio: 50,
    limit_up_again_count: 1,
    limit_up_again_ratio: 50,
    not_limit_up_count: 1,
    unverifiable_count: 0,
    ...overrides,
  }
}

function payload(overrides: Partial<ResonanceData> = {}): ResonanceData {
  return {
    as_of: '2026-09-24',
    member_snapshot_date: '2026-09-24',
    outcome_date: '2026-09-25',
    mode: 'AND',
    market: 'all',
    taxonomy: 'kph_selected',
    selected_sectors: [
      { code: '801001', name: '芯片', taxonomy: 'kph_selected', member_count: 5, has_data: true },
    ],
    coverage: {
      member_observed_rows: 5,
      member_unique_count: 5,
      member_completion_proof: false,
      observed_sectors: 1,
      expected_sectors: 1,
      observed_limit_up_count: 2,
      observed_limit_up_ratio: 40,
      ratio_scope: 'OBSERVED_SAMPLE_ONLY',
      note: '仅已观测',
    },
    pit: {
      historical_signal_eligible: false,
      member_snapshot_known_by: 'SAME_DAY_UNKNOWN',
      known_by_observation_time: null,
      later_snapshot_for_review: false,
    },
    market_sentiment: { limit_up_count: 50, broken_rate: 20, max_consecutive: 3 },
    state_counts: {
      first_board: 1,
      second_board: 1,
      third_plus: 0,
      broken: 0,
      conflict: 0,
      unknown: 0,
    },
    outcome: stats(),
    outcome_status: 'VERIFIED',
    outcome_reason_codes: [],
    by_state: {
      first_board: { ...stats({ sample_count: 1, valid_count: 1 }), label: '首板' },
      second_board: { ...stats({ sample_count: 1, valid_count: 1 }), label: '二板' },
    } as ResonanceData['by_state'],
    total: 2,
    shown: 2,
    has_more: false,
    offset: 0,
    limit: 100,
    quality: { status: 'PARTIAL', reasons: ['NO_MEMBERS_COMPLETION_LOG'] },
    warnings: ['NO_MEMBERS_COMPLETION_LOG', 'OBSERVED_ONLY'],
    notes: ['D+1 是事后描述统计。'],
    rows: [
      {
        code: '600000',
        name: '合成银行',
        sector_codes: ['801001'],
        sector_count: 1,
        state: 'first_board',
        state_label: '首板',
        consecutive: 1,
        first_seal_time: '09:35',
        pct_chg: 10,
        outcome_pct_chg: 5,
        outcome_close: 10.5,
        limit_up_again: true,
      },
      {
        code: '600001',
        name: '合成科技',
        sector_codes: ['801001'],
        sector_count: 1,
        state: 'second_board',
        state_label: '二板',
        consecutive: 2,
        first_seal_time: '10:01',
        pct_chg: 10,
        outcome_pct_chg: -2,
        outcome_close: 9.8,
        limit_up_again: false,
      },
    ],
    ...overrides,
  } as ResonanceData
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.sectorResonance.mockResolvedValue(payload())
})

function renderPanel() {
  return render(
    <MemoryRouter>
      <WatchlistContext.Provider value={watchValue}>
        <SectorResonance boards={boards} pageDate="2026-09-24" />
      </WatchlistContext.Provider>
    </MemoryRouter>,
  )
}

async function selectAndRun() {
  fireEvent.change(screen.getByLabelText('添加板块'), { target: { value: '801001' } })
  fireEvent.click(screen.getByRole('button', { name: '分析' }))
  await waitFor(() => expect(screen.getByTestId('resonance-table')).toBeTruthy())
}

test('requires a board before running', () => {
  renderPanel()
  expect(screen.getByText(/先选 1~10 个板块/)).toBeTruthy()
  expect(mocks.sectorResonance).not.toHaveBeenCalled()
})

test('renders coverage, state buckets and D+1 stats', async () => {
  renderPanel()
  await selectAndRun()
  const table = within(screen.getByTestId('resonance-table'))
  expect(table.getByText('合成银行')).toBeTruthy()
  expect(table.getByText('合成科技')).toBeTruthy()
  expect(screen.getByText(/成员快照无完成证明/)).toBeTruthy()
  expect(screen.getAllByText('50.0%').length).toBeGreaterThan(0)
  expect(screen.getAllByText('+1.50%').length).toBeGreaterThan(0)
  expect(screen.getByText(/D\+1 是事后描述统计/)).toBeTruthy()
})

test('AND unverifiable shows no fabricated rows', async () => {
  mocks.sectorResonance.mockResolvedValue(
    payload({
      quality: { status: 'UNVERIFIABLE', reasons: ['AND_UNVERIFIABLE'] },
      warnings: ['AND_UNVERIFIABLE'],
      total: 0,
      shown: 0,
      rows: [],
    }),
  )
  renderPanel()
  await selectAndRun()
  expect(screen.getByText(/AND 缺覆盖/)).toBeTruthy()
  expect(screen.getByText('不可验证')).toBeTruthy()
})

test('pending outcome never renders zero percent', async () => {
  mocks.sectorResonance.mockResolvedValue(
    payload({
      outcome_date: null,
      outcome_status: 'PENDING',
      outcome: stats({
        avg_pct: null,
        median_pct: null,
        up_ratio: null,
        limit_up_again_count: null,
        limit_up_again_ratio: null,
      }),
      outcome_reason_codes: ['OUTCOME_PENDING'],
    }),
  )
  renderPanel()
  await selectAndRun()
  expect(screen.getByText('待观察')).toBeTruthy()
  expect(screen.getByText(/OUTCOME_PENDING/)).toBeTruthy()
  expect(screen.queryByText('0.0%')).toBeNull()
})

test('rapid re-run keeps only the latest response', async () => {
  let resolveFirst: (value: ResonanceData) => void = () => {}
  mocks.sectorResonance
    .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve }))
    .mockResolvedValueOnce(payload({ total: 9 }))
  renderPanel()
  fireEvent.change(screen.getByLabelText('添加板块'), { target: { value: '801001' } })
  fireEvent.click(screen.getByRole('button', { name: /分析/ }))
  fireEvent.click(screen.getByRole('button', { name: /分析/ }))
  await waitFor(() => expect(screen.getByText(/共 9 只/)).toBeTruthy())
  resolveFirst(payload({ total: 1 }))
  await new Promise((resolve) => setTimeout(resolve, 20))
  expect(screen.queryByText(/共 1 只/)).toBeNull()
})
