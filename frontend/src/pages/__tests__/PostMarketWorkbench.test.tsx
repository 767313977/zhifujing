import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import type { Workbench } from '../../api/types'

const mocks = vi.hoisted(() => ({
  workbench: vi.fn(),
  openStockResearch: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  api: { workbench: mocks.workbench },
  isNoDataError: (err: { status?: number }) => err?.status === 404,
}))

vi.mock('../../lib/watchlistContext', () => ({
  useWatchlist: () => ({ isWatched: () => false }),
}))

vi.mock('../../lib/authContext', () => ({
  useAuth: () => ({ me: { id: 1, username: 's5a', is_admin: true }, checking: false }),
}))

vi.mock('../../lib/stockResearchContext', () => ({
  useStockResearch: () => ({ openStockResearch: mocks.openStockResearch }),
}))

import PostMarketWorkbench from '../PostMarketWorkbench'

function row(overrides: Record<string, unknown> = {}) {
  return {
    code: '600000',
    name: '合成银行',
    sector_codes: ['A01'],
    sector_count: 1,
    state: 'first_board',
    state_label: '首板',
    consecutive: 1,
    first_seal_time: '09:35',
    pct_chg: 10,
    outcome_pct_chg: 5,
    outcome_close: 10.5,
    limit_up_again: true,
    ...overrides,
  }
}

function payload(overrides: Partial<Workbench> = {}): Workbench {
  return {
    as_of: '2026-11-04',
    data_as_of: '2026-11-05',
    quality_version: 'workbench-quality-v1',
    scope: { sector_codes: ['A01'], mode: 'AND', market: 'all', view: 'overview' },
    sentiment: {
      status: 'PARTIAL',
      as_of: '2026-11-04',
      summary: { limit_up_count: 50, broken_rate: 20, max_consecutive: 3, advance_ratio: 60 },
      outcome_date: '2026-11-05',
      outcome: { sample_count: 10, valid_count: 10, avg_pct: 1.2 },
      reasons: ['OUTCOME_PARTIAL'],
    },
    promotion: {
      status: 'VERIFIED',
      prev_date: '2026-11-03',
      levels: [
        {
          level: 1,
          label: '1进2',
          base: 10,
          promoted: 3,
          broken: 1,
          failed: 6,
          unverifiable: 0,
          valid_base: 10,
          legacy_rate: 30,
          verified_rate: 30,
        },
      ],
      reasons: [],
    },
    resonance: {
      status: 'PARTIAL',
      coverage: {
        member_observed_rows: 100,
        member_unique_count: 20,
        member_completion_proof: false,
        observed_sectors: 1,
        expected_sectors: 1,
        observed_limit_up_count: 3,
        observed_limit_up_ratio: 15,
        ratio_scope: 'OBSERVED_SAMPLE_ONLY',
      },
      state_counts: { first_board: 2, second_board: 1, broken: 0, conflict: 0, unknown: 0 },
      member_snapshot_date: '2026-11-04',
      pit: { member_snapshot_known_by: 'PRIOR_DAY' },
      reasons: [],
      warnings: [],
    },
    outcome: {
      status: 'PARTIAL',
      outcome_date: '2026-11-05',
      stats: {
        sample_count: 3,
        valid_count: 3,
        missing_count: 0,
        avg_pct: 5,
        median_pct: 5,
        outcome_up_count: 3,
        outcome_down_count: 0,
        outcome_flat_count: 0,
        up_ratio: 100,
        limit_up_again_count: 1,
        limit_up_again_ratio: 33.3,
        not_limit_up_count: 2,
        unverifiable_count: 0,
      },
      by_state: {
        first_board: {
          sample_count: 2,
          valid_count: 2,
          missing_count: 0,
          avg_pct: 3,
          median_pct: 3,
          outcome_up_count: 2,
          outcome_down_count: 0,
          outcome_flat_count: 0,
          up_ratio: 100,
          limit_up_again_count: 1,
          limit_up_again_ratio: 50,
          not_limit_up_count: 1,
          unverifiable_count: 0,
          label: '首板',
        },
      },
      reasons: ['OUTCOME_PARTIAL'],
    },
    quality: {
      version: 'data-quality-v2',
      sources: [
        {
          source: 'limit_pool',
          status: 'VERIFIED_SOURCE',
          combined_view_status: 'VERIFIED_SOURCE',
          completion_proof: true,
          reason_codes: [],
        },
        {
          source: 'auction_snapshot',
          status: 'NOT_COLLECTED',
          combined_view_status: 'NOT_COLLECTED',
          completion_proof: false,
          reason_codes: ['AUCTION_NOT_COLLECTED'],
        },
      ],
      warnings: ['auction_snapshot:AUCTION_NOT_COLLECTED'],
    },
    pagination: { total: 1, shown: 1, has_more: false },
    rows: [row()],
    warnings: ['OBSERVED_SAMPLE_ONLY'],
    notes: ['本页只是盘后研究汇总，不生成买入/卖出信号。'],
    ...overrides,
  }
}

function renderPage(url = '/research/workbench?date=2026-11-04&sector_codes=A01&mode=AND&market=all') {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <PostMarketWorkbench />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.workbench.mockResolvedValue(payload())
})

test('renders all aggregated sections and passes URL scope to API', async () => {
  renderPage()
  await waitFor(() => expect(screen.getByText('盘后主线研究工作台')).toBeTruthy())
  expect(screen.getByText(/涨停 50/)).toBeTruthy()
  expect(screen.getByText('1进2')).toBeTruthy()
  expect(screen.getByText('已观测题材共振')).toBeTruthy()
  expect(screen.getByText('D+1 事后描述统计')).toBeTruthy()
  expect(screen.getByText('数据源可用性')).toBeTruthy()
  expect(screen.getByText('逐券明细')).toBeTruthy()
  expect(mocks.workbench).toHaveBeenCalledWith(
    expect.objectContaining({
      date: '2026-11-04',
      sectorCodes: 'A01',
      mode: 'AND',
      market: 'all',
      view: 'overview',
      offset: 0,
    }),
  )
})

test('shows observed-sample boundary and D+1 null as not zero', async () => {
  const data = payload()
  data.outcome.status = 'PENDING'
  data.outcome.stats = null
  mocks.workbench.mockResolvedValue(data)
  renderPage()
  await waitFor(() => expect(screen.getByText(/待观察（D\+1 未成熟，不显示 0%）/)).toBeTruthy())
  expect(screen.queryByText(/再涨停 0\.00%/)).toBeNull()
})

test('null limit_up_again is shown as unverifiable', async () => {
  const data = payload()
  data.rows = [row({ limit_up_again: null })]
  mocks.workbench.mockResolvedValue(data)
  renderPage()
  await waitFor(() => expect(screen.getAllByText('不可验证').length).toBeGreaterThan(0))
})

test('empty sector selection keeps page usable and shows NOT_SELECTED', async () => {
  const data = payload()
  data.scope.sector_codes = []
  data.resonance = { ...data.resonance, status: 'NOT_SELECTED', coverage: null }
  data.outcome.status = 'NOT_SELECTED'
  data.rows = []
  data.pagination = { total: 0, shown: 0, has_more: false }
  mocks.workbench.mockResolvedValue(data)
  renderPage('/research/workbench?date=2026-11-04')
  await waitFor(() => expect(screen.getAllByText('未选板块').length).toBeGreaterThan(0))
  expect(screen.getByText(/当前筛选下没有逐券结果/)).toBeTruthy()
})

test('API failure shows an error state without crashing', async () => {
  mocks.workbench.mockRejectedValue(new Error('接口超时'))
  renderPage()
  await waitFor(() => expect(screen.getByText(/加载失败/)).toBeTruthy())
  expect(screen.getByText(/接口超时/)).toBeTruthy()
})

test('quality view hides ladder and outcome panels', async () => {
  renderPage('/research/workbench?date=2026-11-04&view=quality')
  await waitFor(() => expect(screen.getByText('数据源可用性')).toBeTruthy())
  expect(screen.queryByText('连板晋级漏斗')).toBeNull()
  expect(screen.queryByText('D+1 事后描述统计')).toBeNull()
})

test('quality panel links to data-quality with the observation date', async () => {
  renderPage()
  await waitFor(() => expect(screen.getByText('数据源可用性')).toBeTruthy())
  const links = screen.getAllByText('质量台')
  expect(links[0].getAttribute('href')).toBe('/data-quality?date=2026-11-04')
})

test('rapid date changes never apply a stale response', async () => {
  let resolveFirst: (value: Workbench) => void = () => {}
  let resolveSecond: (value: Workbench) => void = () => {}
  mocks.workbench
    .mockImplementationOnce(
      () => new Promise<Workbench>((resolve) => { resolveFirst = resolve }),
    )
    .mockImplementationOnce(
      () => new Promise<Workbench>((resolve) => { resolveSecond = resolve }),
    )
  renderPage('/research/workbench?date=2026-11-03')
  const input = screen.getByDisplayValue('2026-11-03')
  fireEvent.change(input, { target: { value: '2026-11-04' } })

  const newer = payload({ as_of: '2026-11-04' })
  resolveSecond(newer)
  await waitFor(() => expect(screen.getByText(/观察日 2026-11-04/)).toBeTruthy())

  const older = payload({ as_of: '2026-11-03' })
  resolveFirst(older)
  await new Promise((resolve) => setTimeout(resolve, 0))
  expect(screen.getByText(/观察日 2026-11-04/)).toBeTruthy()
})

test('pagination moves offset and keeps total denominator', async () => {
  const data = payload()
  data.pagination = { total: 120, shown: 50, has_more: true }
  mocks.workbench.mockResolvedValue(data)
  renderPage()
  await waitFor(() => expect(screen.getByText(/共 120 只/)).toBeTruthy())
  fireEvent.click(screen.getByText('下一页'))
  await waitFor(() =>
    expect(mocks.workbench).toHaveBeenLastCalledWith(
      expect.objectContaining({ offset: 50 }),
    ),
  )
  expect(screen.getByText(/共 120 只/)).toBeTruthy()
})

test('stock links and quick research remain available', async () => {
  renderPage()
  await waitFor(() => expect(screen.getByText('合成银行')).toBeTruthy())
  const link = screen.getByText('合成银行').closest('a')
  expect(link?.getAttribute('href')).toBe('/stock/600000')
  fireEvent.click(screen.getByLabelText('快速研究 600000'))
  expect(mocks.openStockResearch).toHaveBeenCalledWith('600000', {
    asOfDate: '2026-11-04',
    tab: undefined,
  })
})
