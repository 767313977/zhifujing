import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import type { StockResearchContext as StockResearchContextData } from '../../api/types'

const mocks = vi.hoisted(() => ({
  stockProfile: vi.fn(),
  stockResearchContext: vi.fn(),
  syncStock: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  api: {
    stockProfile: mocks.stockProfile,
    stockResearchContext: mocks.stockResearchContext,
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
import type { StockResearchValue } from '../../lib/stockResearchContext'
import { WatchlistContext } from '../../lib/watchlistContext'
import StockResearchDrawer from '../../components/StockResearchDrawer'

const watchValue = {
  codes: new Set<string>(),
  isWatched: () => false,
  refresh: async () => {},
}

function profile() {
  return {
    code: '600000',
    name: '合成银行',
    in_watchlist: false,
    note: null,
    latest: { trade_date: '2026-09-24', close: 10, pct_chg: 10 },
    day_count: 100,
    first_date: '2026-01-01',
    last_date: '2026-09-24',
    limit_up_dates: [],
    lhb_count: 0,
    pct_chg_5d: null,
    total_mv: null,
    free_float_mv: null,
    actual_turnover: null,
    pe_forecast: null,
    total_mv_asof: null,
    phase: null,
  }
}

function contextPayload(
  overrides: Partial<StockResearchContextData> = {},
): StockResearchContextData {
  return {
    code: '600000',
    name: '合成银行',
    requested_as_of: '2026-09-24',
    resolved_trade_date: '2026-09-24',
    data_as_of: '2026-09-28',
    historical_signal_eligible: false,
    evidence_version: 'research-evidence-v2',
    observation_date: '2026-09-24',
    cutoff: 'CLOSE',
    quality: { status: 'PARTIAL', reasons: ['THEME_PIT_UNKNOWN'] },
    at_cutoff_known: {
      previous_close_date: '2026-09-23',
      pool_status: 'UP',
      board_height: 2,
      observed_pool_membership: ['up'],
      pool_quality: { up: 'VERIFIED', broken: 'VERIFIED', down: 'VERIFIED' },
      daily: { trade_date: '2026-09-24', close: 10, pct_chg: 10, known_by: 'CLOSE' },
      themes: [],
      concepts: [],
      sentiment: { limit_up_count: 50, broken_rate: 20, max_consecutive: 2, up_count: 3000, down_count: 2000 },
    },
    unknown_or_later: {
      themes: [
        {
          name: '芯片',
          sector_code: '801001',
          taxonomy: 'kph_selected',
          fetched_at: '2026-09-24T22:00:00',
          known_by: 'SAME_DAY_UNKNOWN',
        },
      ],
      concepts: [
        {
          name: '芯片',
          sector_code: null,
          taxonomy: 'stock_concept',
          fetched_at: null,
          known_by: 'POST_CLOSE_UNKNOWN',
        },
      ],
      reasons: ['THEME_PIT_UNKNOWN', 'CONCEPT_POST_CLOSE_UNKNOWN'],
    },
    as_known_on_day: {
      previous_close_date: '2026-09-23',
      limit_status: 'UP',
      board_height: 2,
      observed_pool_membership: ['up'],
      pool_quality: { up: 'VERIFIED', broken: 'VERIFIED', down: 'VERIFIED' },
      daily: { trade_date: '2026-09-24', close: 10, pct_chg: 10, known_by: 'CLOSE' },
      // 兼容子集：S4 起不得再承载 LATER / 同日未知题材
      themes: [],
      concepts: [],
      sentiment: { limit_up_count: 50, broken_rate: 20, max_consecutive: 2, up_count: 3000, down_count: 2000 },
    },
    later_outcome: {
      outcome_date: '2026-09-28',
      status: 'VERIFIED',
      pct_chg: -5,
      close: 9.5,
      limit_up_again: false,
      reason_code: null,
      known_by: 'FORWARD',
    },
    evidence: [
      {
        claim: '观察日池记录',
        source_table: 'limit_pool',
        source_trade_date: '2026-09-24',
        fetched_at: '2026-09-24T16:00:00',
        known_by: 'CLOSE',
        quality: 'VERIFIED',
        reason_code: null,
        is_forward_known: false,
        time_bucket: 'AT_CUTOFF_KNOWN',
        summary: '当日观察到 up',
      },
      {
        claim: '题材归属快照',
        source_table: 'sector_member',
        source_trade_date: '2026-09-24',
        fetched_at: '2026-09-24T22:00:00',
        known_by: 'SAME_DAY_UNKNOWN',
        quality: 'PARTIAL',
        reason_code: 'THEME_PIT_UNKNOWN',
        is_forward_known: false,
        time_bucket: 'UNKNOWN_OR_LATER',
        summary: '芯片(kph_selected)',
      },
      {
        claim: 'D+1 后验结果',
        source_table: 'stock_daily',
        source_trade_date: '2026-09-28',
        fetched_at: null,
        known_by: 'FORWARD',
        quality: 'VERIFIED',
        reason_code: null,
        is_forward_known: true,
        time_bucket: 'FORWARD_OUTCOME',
        summary: 'D+1 涨跌幅 -5',
      },
    ],
    notes: ['D+1 结果只供复盘，不反写 D 日依据。'],
    ...overrides,
  }
}

function value(overrides: Partial<StockResearchValue> = {}): StockResearchValue {
  return {
    code: '600000',
    sourcePage: '/sectors',
    selectedAsOfDate: '2026-09-24',
    open: true,
    activeTab: 'evidence',
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
    <MemoryRouter initialEntries={['/sectors']}>
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
  mocks.stockResearchContext.mockResolvedValue(contextPayload())
})

test('evidence tab separates as-known, unknown-source and forward outcome', async () => {
  renderDrawer()
  await waitFor(() => expect(screen.getByText('观察日已知（截至收盘）')).toBeTruthy())
  expect(screen.getByText('不知道 / 观察日之后才取到')).toBeTruthy()
  expect(screen.getByText('D+1 后验（只供复盘）')).toBeTruthy()

  expect(screen.getAllByText(/同日取到，无法证明盘前/).length).toBeGreaterThan(0)
  expect(screen.getByText(/历史信号资格：否/)).toBeTruthy()
  expect(screen.getByText('-5.00%')).toBeTruthy()
  expect(screen.getByText(/D\+1 后验结果/)).toBeTruthy()
  // 后验行以后验时点展示
  expect(screen.getAllByText('D+1 后验').length).toBeGreaterThan(0)
  // 时间桶列可见
  expect(screen.getByText('时间桶')).toBeTruthy()
  expect(screen.getByText('后得/未知')).toBeTruthy()

  // 同一个题材不能既出现在「已知」又出现在「后得/未知」：
  // 这里只有 unknown 区的 1 条题材 + 1 条涨停天梯，合计 2 次
  expect(screen.getAllByText('芯片').length).toBe(2)
})

test('prior-day theme is labelled as known', async () => {
  const payload = contextPayload()
  const theme = {
    name: '芯片',
    sector_code: '801001',
    taxonomy: 'kph_selected',
    fetched_at: '2026-09-22T22:00:00',
    known_by: 'PRIOR_DAY',
  }
  payload.at_cutoff_known = { ...payload.at_cutoff_known!, themes: [theme] }
  payload.unknown_or_later = { themes: [], concepts: [], reasons: [] }
  payload.as_known_on_day = { ...payload.as_known_on_day, themes: [theme] }
  mocks.stockResearchContext.mockResolvedValue(payload)
  renderDrawer()
  await waitFor(() => expect(screen.getByText('前一日已取到')).toBeTruthy())
  // 已知区只有这一条，后得区为空 → 题材名只出现 1 次
  expect(screen.getAllByText('芯片').length).toBe(1)
})

test('selected observation date is passed to the read-only endpoint', async () => {
  renderDrawer()
  await waitFor(() =>
    expect(mocks.stockResearchContext).toHaveBeenCalledWith('600000', '2026-09-24'),
  )
  expect(mocks.syncStock).not.toHaveBeenCalled()
})

test('API failure shows an error without triggering sync', async () => {
  mocks.stockResearchContext.mockRejectedValue(new Error('接口超时'))
  renderDrawer()
  await waitFor(() => expect(screen.getAllByText('加载失败').length).toBeGreaterThan(0))
  expect(mocks.syncStock).not.toHaveBeenCalled()
})

// 类型对齐：确保测试里的值确实属于侧栏上下文
const _typecheck: Partial<StockResearchValue> = value()
void _typecheck
