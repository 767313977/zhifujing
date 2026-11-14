import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import type { SentimentNextDay as SentimentNextDayData } from '../../api/types'

const mocks = vi.hoisted(() => ({
  sentimentNextDay: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  api: { sentimentNextDay: mocks.sentimentNextDay },
  isNoDataError: (err: { status?: number }) => err?.status === 404,
}))

// jsdom 没有 ResizeObserver、也没有 canvas：图表组件不参与断言
vi.mock('../../components/EChart', () => ({ default: () => null }))

import SentimentNextDay from '../../components/SentimentNextDay'

function point(overrides: Partial<SentimentNextDayData['series'][number]> = {}) {
  return {
    sample_date: '2026-11-10',
    outcome_date: '2026-11-11',
    group: 'first_board' as const,
    sentiment_value: 50,
    limit_up_count: 50,
    broken_rate: 30,
    max_consecutive: 3,
    up_count: 3000,
    down_count: 2000,
    advance_ratio: 60,
    sample_count: 10,
    valid_count: 10,
    missing_count: 0,
    avg_pct: 2.5,
    median_pct: 1.8,
    outcome_up_count: 7,
    outcome_down_count: 3,
    outcome_flat_count: 0,
    up_ratio: 70,
    limit_up_again_count: 2,
    limit_up_again_ratio: 20,
    not_limit_up_count: 8,
    unverifiable_count: 0,
    avg_open_pct: 1.2,
    avg_close_vs_open: 0.4,
    data_quality_status: 'VERIFIED' as const,
    data_quality_reasons: [],
    data_as_of: '2026-11-11',
    ...overrides,
  }
}

function payload(overrides: Partial<SentimentNextDayData> = {}): SentimentNextDayData {
  return {
    group: 'first_board',
    group_label: 'D 日首板',
    metric: 'limit_up_count',
    metric_label: '涨停家数',
    days: 60,
    threshold_version: 'sentiment-bands-v1',
    aggregation_version: 'next-day-aggregation-v2',
    thresholds: [{ label: '30–60 家', lower: 30, upper: 60 }],
    data_as_of: '2026-11-11',
    dates: ['2026-11-10', '2026-11-09', '2026-11-06'],
    series: [
      point(),
      point({
        sample_date: '2026-11-09',
        outcome_date: '2026-11-10',
        avg_pct: null,
        median_pct: null,
        up_ratio: null,
        limit_up_again_ratio: null,
        data_quality_status: 'PENDING',
        data_quality_reasons: ['OUTCOME_PENDING'],
      }),
      point({
        sample_date: '2026-11-06',
        outcome_date: '2026-11-07',
        sample_count: 0,
        valid_count: 0,
        missing_count: 0,
        avg_pct: null,
        median_pct: null,
        up_ratio: null,
        limit_up_again_ratio: null,
        data_quality_status: 'UNVERIFIED',
        data_quality_reasons: ['UP_POOL_MISSING'],
      }),
    ],
    summary: {
      day_count: 3,
      stat_day_count: 1,
      sample_count: 10,
      valid_count: 10,
      missing_count: 0,
      security_weighted_avg_pct: 2.5,
      day_equal_weight_avg_pct: 0.5,
      avg_pct: 2.5,
      median_pct: 1.8,
      up_count: 7,
      down_count: 3,
      flat_count: 0,
      up_ratio: 70,
      limit_up_again_count: 2,
      limit_up_again_ratio: 20,
      not_limit_up_count: 8,
      unverifiable_count: 0,
      avg_open_pct: 1.2,
      avg_close_vs_open: 0.4,
    },
    bands: [
      {
        label: '30–60 家',
        lower: 30,
        upper: 60,
        day_count: 1,
        stat_day_count: 1,
        sample_count: 10,
        valid_count: 10,
        security_weighted_avg_pct: 2.5,
        day_equal_weight_avg_pct: 0.5,
        avg_pct: 2.5,
        median_pct: 1.8,
        up_ratio: 70,
        limit_up_again_ratio: 20,
      },
    ],
    quality: {
      status_counts: { VERIFIED: 1, PENDING: 1, UNVERIFIED: 1 },
      pending_days: ['2026-11-09'],
      unverified_days: ['2026-11-06'],
      metric_missing_days: [],
      group_note: '分组为分别统计',
    },
    notes: ['统计为历史描述性关联，不构成收益预测。'],
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.sentimentNextDay.mockResolvedValue(payload())
})

function renderPanel() {
  return render(
    <MemoryRouter>
      <SentimentNextDay />
    </MemoryRouter>,
  )
}

test('renders summary and verified row', async () => {
  renderPanel()
  await waitFor(() => expect(mocks.sentimentNextDay).toHaveBeenCalled())
  expect(screen.getByText('情绪环境 × 次日表现')).toBeTruthy()
  expect(screen.getAllByText('+2.50%').length).toBeGreaterThan(0)
  expect(screen.getByText('可验证')).toBeTruthy()
  expect(screen.getByText('sentiment-bands-v1')).toBeTruthy()
})

test('pending row shows PENDING instead of a fake 0%', async () => {
  renderPanel()
  await waitFor(() => expect(screen.getByText('待观察')).toBeTruthy())
  const row = screen.getByText('2026-11-09').closest('tr')
  expect(row).toBeTruthy()
  expect(row!.textContent).toContain('PENDING')
  expect(row!.textContent).not.toContain('0.00%')
})

test('unverified row shows UNVERIFIABLE', async () => {
  renderPanel()
  await waitFor(() => expect(screen.getByText('不可验证')).toBeTruthy())
  const row = screen.getByText('2026-11-06').closest('tr')
  expect(row!.textContent).toContain('UNVERIFIABLE')
  expect(row!.textContent).toContain('D 日涨停池未采集')
})

test('switching group refetches with the new group', async () => {
  renderPanel()
  await waitFor(() => expect(mocks.sentimentNextDay).toHaveBeenCalledTimes(1))
  fireEvent.click(screen.getByRole('radio', { name: '二板' }))
  await waitFor(() =>
    expect(mocks.sentimentNextDay).toHaveBeenLastCalledWith(
      expect.objectContaining({ group: 'second_board' }),
    ),
  )
})

test('switching range refetches with the new days', async () => {
  renderPanel()
  await waitFor(() => expect(mocks.sentimentNextDay).toHaveBeenCalledTimes(1))
  fireEvent.click(screen.getByRole('radio', { name: '120 日' }))
  await waitFor(() =>
    expect(mocks.sentimentNextDay).toHaveBeenLastCalledWith(
      expect.objectContaining({ days: 120 }),
    ),
  )
})

test('a slow earlier response cannot override the newest selection', async () => {
  let resolveSlow: (value: SentimentNextDayData) => void = () => {}
  mocks.sentimentNextDay.mockImplementationOnce(
    () => new Promise<SentimentNextDayData>((resolve) => (resolveSlow = resolve)),
  )
  mocks.sentimentNextDay.mockResolvedValue(
    payload({ group: 'third_plus', group_label: 'D 日三板及以上' }),
  )

  renderPanel()
  fireEvent.click(screen.getByRole('radio', { name: '三板+' }))
  await waitFor(() => expect(screen.getByText(/D 日三板及以上/)).toBeTruthy())

  resolveSlow(payload({ group: 'second_board', group_label: 'D 日二板' }))
  await new Promise((resolve) => setTimeout(resolve, 0))
  expect(screen.queryByText(/D 日二板/)).toBeNull()
  expect(screen.getByText(/D 日三板及以上/)).toBeTruthy()
})

test('renders the caveats from backend', async () => {
  renderPanel()
  await waitFor(() => expect(screen.getByText(/不构成收益预测/)).toBeTruthy())
})

test('shows pooled and day-equal averages as separate figures', async () => {
  renderPanel()
  await waitFor(() => expect(screen.getByText('情绪环境 × 次日表现')).toBeTruthy())
  // 逐券 pooled 均值 2.50% 与日等权 0.50% 同时出现，不做二选一
  expect(screen.getAllByText('+2.50%').length).toBeGreaterThan(0)
  expect(screen.getAllByText('+0.50%').length).toBeGreaterThan(0)
  expect(screen.getByText('日等权均值')).toBeTruthy()
  expect(screen.getByText('next-day-aggregation-v2')).toBeTruthy()
})

test('metric-missing days are called out and small samples are flagged', async () => {
  mocks.sentimentNextDay.mockResolvedValue(
    payload({
      quality: {
        status_counts: { VERIFIED: 1, PENDING: 1, UNVERIFIED: 1 },
        pending_days: ['2026-11-09'],
        unverified_days: ['2026-11-06'],
        metric_missing_days: ['2026-11-06'],
        group_note: '分组为分别统计',
      },
    }),
  )
  renderPanel()
  await waitFor(() => expect(screen.getByText(/个交易日的 D 日情绪指标缺失/)).toBeTruthy())
  expect(screen.getByText(/小样本/)).toBeTruthy()
})
