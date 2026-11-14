import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import type { SectorQuote, ThemeSearch } from '../../api/types'

const mocks = vi.hoisted(() => ({
  themeSearch: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  api: { themeSearch: mocks.themeSearch },
  isNoDataError: (err: { status?: number }) => err?.status === 404,
}))

import ThemeIntersection from '../../components/ThemeIntersection'
import { WatchlistContext } from '../../lib/watchlistContext'

const watchValue = {
  codes: new Set<string>(),
  isWatched: () => false,
  refresh: async () => {},
}

function board(code: string, name: string): SectorQuote {
  return {
    code,
    name,
    taxonomy: 'kph_selected',
    strength: null,
    pct_chg: null,
    pct_chg_5d: null,
    amount: null,
    net_inflow: null,
    up_count: null,
    down_count: null,
    member_count: null,
    leader_name: null,
    leader_pct_chg: null,
    limit_up_count: null,
  }
}

const BOARDS = [board('801001', '芯片'), board('801002', '算力'), board('801003', '人工智能')]

function payload(overrides: Partial<ThemeSearch> = {}): ThemeSearch {
  return {
    requested_as_of: '2026-09-24',
    resolved_as_of: '2026-09-24',
    source_snapshot_date: '2026-09-24',
    fetched_at: '2026-09-24T22:00:00',
    is_exact_date: true,
    taxonomy: 'kph_selected',
    selected_sectors: [
      { code: '801001', name: '芯片', taxonomy: 'kph_selected', member_count: 1130, has_data: true },
      { code: '801002', name: '算力', taxonomy: 'kph_selected', member_count: 1038, has_data: true },
    ],
    mode: 'AND',
    market: 'all',
    total: 2,
    shown: 2,
    has_more: false,
    offset: 0,
    limit: 50,
    coverage: {
      sample_rows: 2168,
      distinct_stocks: 5869,
      sector_count: 260,
      expected_sectors: 270,
      has_completeness_evidence: false,
      note: null,
    },
    quality: { status: 'PARTIAL', reasons: ['NO_MEMBERS_COMPLETION_LOG'] },
    warnings: ['NO_MEMBERS_COMPLETION_LOG', 'SNAPSHOT_SECTOR_COVERAGE_LOW'],
    items: [
      {
        code: '000032',
        name: '深桑达Ａ',
        match_count: 2,
        sectors: [
          { code: '801001', name: '芯片' },
          { code: '801002', name: '算力' },
        ],
        trade_date: '2026-09-24',
        limit_up_consecutive: null,
      },
      {
        code: '000034',
        name: '神州数码',
        match_count: 2,
        sectors: [
          { code: '801001', name: '芯片' },
          { code: '801002', name: '算力' },
        ],
        trade_date: '2026-09-24',
        limit_up_consecutive: 2,
      },
    ],
    ...overrides,
  }
}

function renderPanel() {
  return render(
    <MemoryRouter initialEntries={['/sectors']}>
      <WatchlistContext.Provider value={watchValue}>
        <ThemeIntersection boards={BOARDS} pageDate="2026-09-24" />
      </WatchlistContext.Provider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.themeSearch.mockResolvedValue(payload())
})

test('starts empty and does not request until a board is selected', async () => {
  renderPanel()
  expect(screen.getByText(/先选 1~10 个题材/)).toBeTruthy()
  expect(mocks.themeSearch).not.toHaveBeenCalled()
})

test('selecting two boards requests AND and renders provenance', async () => {
  renderPanel()
  fireEvent.click(screen.getByRole('button', { name: '芯片' }))
  fireEvent.click(screen.getByRole('button', { name: '算力' }))
  await waitFor(() => expect(mocks.themeSearch).toHaveBeenCalled())
  expect(mocks.themeSearch).toHaveBeenLastCalledWith(
    expect.objectContaining({ sectorCodes: ['801001', '801002'], mode: 'AND' }),
  )
  await waitFor(() => expect(screen.getByText('深桑达Ａ')).toBeTruthy())
  expect(screen.getByText('部分覆盖')).toBeTruthy()
  expect(screen.getByText(/无成员级完成日志/)).toBeTruthy()
  expect(screen.getByText(/当日快照 2168 行/)).toBeTruthy()
  expect(screen.getByText('2 板')).toBeTruthy()
  // 原有 StockLink 跳转保留
  expect(screen.getByRole('link', { name: '深桑达Ａ' }).getAttribute('href')).toBe('/stock/000032')
  expect(screen.getByRole('button', { name: '快速研究 000032' })).toBeTruthy()
})

test('switching to OR refetches with the new mode', async () => {
  renderPanel()
  fireEvent.click(screen.getByRole('button', { name: '芯片' }))
  await waitFor(() => expect(mocks.themeSearch).toHaveBeenCalledTimes(1))
  fireEvent.click(screen.getByRole('radio', { name: 'OR 并集' }))
  await waitFor(() =>
    expect(mocks.themeSearch).toHaveBeenLastCalledWith(
      expect.objectContaining({ mode: 'OR' }),
    ),
  )
})

test('changing the snapshot date sends as_of', async () => {
  renderPanel()
  fireEvent.click(screen.getByRole('button', { name: '芯片' }))
  await waitFor(() => expect(mocks.themeSearch).toHaveBeenCalledTimes(1))
  fireEvent.change(screen.getByLabelText('快照日'), { target: { value: '2026-07-03' } })
  await waitFor(() =>
    expect(mocks.themeSearch).toHaveBeenLastCalledWith(
      expect.objectContaining({ asOf: '2026-07-03' }),
    ),
  )
})

test('AND without coverage never shows a fake empty intersection', async () => {
  mocks.themeSearch.mockResolvedValue(
    payload({
      total: 0,
      shown: 0,
      items: [],
      quality: { status: 'UNVERIFIABLE', reasons: ['AND_UNVERIFIABLE'] },
      warnings: ['SELECTED_SECTOR_NOT_COVERED', 'AND_UNVERIFIABLE'],
      selected_sectors: [
        { code: '801001', name: '芯片', taxonomy: 'kph_selected', member_count: 1130, has_data: true },
        { code: '801002', name: '算力', taxonomy: 'kph_selected', member_count: 0, has_data: false },
      ],
    }),
  )
  renderPanel()
  fireEvent.click(screen.getByRole('button', { name: '芯片' }))
  fireEvent.click(screen.getByRole('button', { name: '算力' }))
  await waitFor(() => expect(screen.getByText('不可验证')).toBeTruthy())
  expect(screen.getByText(/无法判定是否有交集/)).toBeTruthy()
  expect(screen.queryByText(/没有符合/)).toBeNull()
})

test('not collected shows an explicit state', async () => {
  mocks.themeSearch.mockResolvedValue(
    payload({
      resolved_as_of: null,
      source_snapshot_date: null,
      total: 0,
      shown: 0,
      items: [],
      quality: { status: 'NOT_COLLECTED', reasons: ['NO_SNAPSHOT_FOR_SELECTED_SECTORS'] },
    }),
  )
  renderPanel()
  fireEvent.click(screen.getByRole('button', { name: '芯片' }))
  await waitFor(() => expect(screen.getByText('未采集')).toBeTruthy())
  expect(screen.getByText(/没有成员快照，不显示「空交集」/)).toBeTruthy()
})

test('pagination advances by the page size', async () => {
  mocks.themeSearch.mockResolvedValue(payload({ total: 120, shown: 50, has_more: true }))
  renderPanel()
  fireEvent.click(screen.getByRole('button', { name: '芯片' }))
  await waitFor(() => expect(screen.getByText('下一页')).toBeTruthy())
  fireEvent.click(screen.getByText('下一页'))
  await waitFor(() =>
    expect(mocks.themeSearch).toHaveBeenLastCalledWith(
      expect.objectContaining({ offset: 50 }),
    ),
  )
})

test('a slow earlier response cannot override the newest selection', async () => {
  let resolveSlow: (value: ThemeSearch) => void = () => {}
  mocks.themeSearch.mockImplementationOnce(
    () => new Promise<ThemeSearch>((resolve) => (resolveSlow = resolve)),
  )
  mocks.themeSearch.mockResolvedValue(
    payload({
      items: [
        {
          code: '600001',
          name: '邯郸钢铁',
          match_count: 1,
          sectors: [{ code: '801002', name: '算力' }],
          trade_date: '2026-09-24',
          limit_up_consecutive: null,
        },
      ],
      total: 1,
      shown: 1,
    }),
  )
  renderPanel()
  fireEvent.click(screen.getByRole('button', { name: '芯片' }))
  fireEvent.click(screen.getByRole('button', { name: '算力' }))
  await waitFor(() => expect(screen.getByText('邯郸钢铁')).toBeTruthy())
  resolveSlow(payload())
  await new Promise((resolve) => setTimeout(resolve, 0))
  expect(screen.queryByText('深桑达Ａ')).toBeNull()
  expect(screen.getByText('邯郸钢铁')).toBeTruthy()
})
