import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api, researchApi } from '../../api/client'
import type { ResearchLowStartOut, ResearchSectorOut, ResearchStrategy, ResearchTrendOut } from '../../api/types'
import { WatchlistContext } from '../../lib/watchlistContext'
import type { WatchlistValue } from '../../lib/watchlistContext'
import Research from '../Research'

vi.mock('../../api/client', () => ({
  api: { addWatchlist: vi.fn() },
  researchApi: { meta: vi.fn(), trend: vi.fn(), lowStart: vi.fn(), sectorState: vi.fn() },
}))

const STRATEGY: ResearchStrategy = {
  id: 'trend_template',
  version: 'p0.2-2026-11-09',
  status: '研究假设：统计稳定性不足，未通过正式策略有效性验证',
}

function trendOut(rows: ResearchTrendOut['rows'], latest = '2026-09-24'): ResearchTrendOut {
  return {
    trade_date: '2026-09-24',
    data_latest: latest,
    rows,
    coverage: { candidates: rows.length, with_boards: 1, bars: rows.length },
    strategy: STRATEGY,
  }
}

function lowOut(rows: ResearchLowStartOut['rows']): ResearchLowStartOut {
  return {
    trade_date: '2026-09-24',
    rows,
    coverage: { candidates: 5261, bars: 4985 },
    strategy: { ...STRATEGY, id: 'low_start_features', status: '研究特征组合：仅用于观察，未验证预测能力' },
  }
}

function sectorOut(rows: ResearchSectorOut['rows']): ResearchSectorOut {
  return {
    trade_date: '2026-09-24',
    requested_date: '2026-09-24',
    rows,
    coverage: { days: 61, boards: 273, with_members: 259, note: 'sector_member 仅覆盖已回补板块' },
    strategy: { ...STRATEGY, id: 'sector_state', status: '市场表现特征，不代表未来涨跌' },
  }
}

function trendRow(overrides: Partial<ResearchTrendOut['rows'][number]> = {}): ResearchTrendOut['rows'][number] {
  return {
    code: '000001',
    name: '测试银行',
    industry: '银行',
    trade_date: '2026-09-24',
    close: 12.3,
    ma20: 11.8,
    ma60: 11.2,
    ma120: 10.9,
    trend_days: 8,
    rps20: 90,
    rps60: 88,
    rps120: 80,
    amount_ratio20: 1.2,
    off_high60: -3.5,
    reasons: ['收盘 12.30 > MA20 11.80 > MA60 11.20'],
    unmet: [],
    missing: [],
    state: '技术特征符合',
    strategy: STRATEGY,
    ...overrides,
  }
}

function lowRow(overrides: Partial<ResearchLowStartOut['rows'][number]> = {}): ResearchLowStartOut['rows'][number] {
  return {
    code: '000002',
    name: '低位样本',
    industry: '测试行业',
    trade_date: '2026-09-24',
    close: 15.6,
    pos_pct: 12.5,
    platform_days: 40,
    platform_height: 6.2,
    platform_high: 15.2,
    vol_contraction: 0.42,
    amount_ratio20: 2.0,
    ma20: 15.1,
    ma20_slope: 0.3,
    rps20: 60,
    state: '突破确认',
    reasons: ['区间位置 12.5%', '平台 40 天、振幅 6.2%'],
    missing: [],
    strategy: { ...STRATEGY, id: 'low_start_features' },
    ...overrides,
  }
}

const WATCHLIST: WatchlistValue = {
  codes: new Set<string>(),
  isWatched: () => false,
  refresh: vi.fn(),
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/research']}>
      <WatchlistContext.Provider value={WATCHLIST}>
        <Research />
      </WatchlistContext.Provider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(researchApi.trend).mockResolvedValue(trendOut([trendRow()]))
  vi.mocked(researchApi.lowStart).mockResolvedValue(lowOut([lowRow()]))
  vi.mocked(researchApi.sectorState).mockResolvedValue(sectorOut([]))
})

describe('P0.2 研究页面', () => {
  it('展示趋势候选与策略验证状态', async () => {
    renderPage()
    await screen.findByText('测试银行')
    expect(screen.getByText(/研究假设/)).toBeTruthy()
    expect(screen.getByText(/收盘 12\.30 > MA20/)).toBeTruthy()
  })

  it('数据不足时给出缺失原因', async () => {
    vi.mocked(researchApi.trend).mockResolvedValue(
      trendOut([trendRow({ state: '数据不足', missing: ['日线不足 130 根'], reasons: [] })]),
    )
    renderPage()
    await screen.findByText('数据不足')
    expect(screen.getByText(/缺失：日线不足 130 根/)).toBeTruthy()
  })

  it('空结果渲染空态而不是表格', async () => {
    vi.mocked(researchApi.trend).mockResolvedValue(trendOut([]))
    renderPage()
    await screen.findByText(/当前日期没有可展示的数据/)
  })

  it('接口失败渲染错误态', async () => {
    vi.mocked(researchApi.trend).mockRejectedValue(new Error('请求超时（30 秒无响应）：/research/trend'))
    renderPage()
    await screen.findByText(/请求超时/)
  })

  it('切换低位启动 Tab 后拉取并展示', async () => {
    renderPage()
    await screen.findByText('测试银行')
    fireEvent.click(screen.getByRole('radio', { name: /低位启动/ }))
    await screen.findByText('低位样本')
    expect(screen.getByText(/仅用于观察/)).toBeTruthy()
    expect(screen.getByText('突破确认')).toBeTruthy()
  })

  it('搜索按代码/名称过滤', async () => {
    vi.mocked(researchApi.trend).mockResolvedValue(
      trendOut([
        trendRow({ code: '000001', name: '测试银行' }),
        trendRow({ code: '600000', name: '另一只票' }),
      ]),
    )
    renderPage()
    await screen.findByText('测试银行')
    fireEvent.change(screen.getByLabelText('搜索'), { target: { value: '600000' } })
    await waitFor(() => expect(screen.queryByText('测试银行')).toBeNull())
    expect(screen.getByText('另一只票')).toBeTruthy()
  })

  it('点击表头按趋势天数排序', async () => {
    vi.mocked(researchApi.trend).mockResolvedValue(
      trendOut([
        trendRow({ code: '000001', name: '低趋势', trend_days: 5 }),
        trendRow({ code: '600000', name: '高趋势', trend_days: 12 }),
      ]),
    )
    renderPage()
    await screen.findByText('低趋势')
    fireEvent.click(screen.getByRole('button', { name: /趋势天数/ }))
    const rows = screen.getAllByRole('row')
    expect(rows[1].textContent).toContain('高趋势')
  })

  it('切换交易日后带日期重新请求', async () => {
    renderPage()
    await screen.findByText('测试银行')
    fireEvent.change(screen.getByLabelText('交易日'), { target: { value: '2026-09-23' } })
    await waitFor(() =>
      expect(vi.mocked(researchApi.trend)).toHaveBeenLastCalledWith(
        expect.objectContaining({ date: '2026-09-23' }),
      ),
    )
  })

  it('加入自选成功后按钮变为已自选', async () => {
    vi.mocked(api.addWatchlist).mockResolvedValue(undefined as never)
    renderPage()
    await screen.findByText('测试银行')
    fireEvent.click(screen.getByRole('button', { name: '+自选' }))
    await screen.findByRole('button', { name: '已自选' })
  })

  it('所选日期早于库内最新时提示历史数据', async () => {
    vi.mocked(researchApi.trend).mockResolvedValue(trendOut([trendRow()], '2026-10-09'))
    renderPage()
    await screen.findByText(/历史数据：结果截至 2026-09-24/)
    expect(screen.getByText(/库内最新 2026-10-09/)).toBeTruthy()
  })

  it('所选日期晚于库内最新时明确警示', async () => {
    renderPage()
    await screen.findByText('测试银行')
    fireEvent.change(screen.getByLabelText('交易日'), { target: { value: '2026-10-12' } })
    await screen.findByText(/晚于库内最新数据 2026-09-24/)
  })

  it('搜索无匹配给出独立空态并提示只过滤当前结果', async () => {
    renderPage()
    await screen.findByText('测试银行')
    fireEvent.change(screen.getByLabelText('搜索'), { target: { value: '<script>!!' } })
    await screen.findByText(/没有匹配/)
    expect(screen.getByText(/搜索仅过滤当前返回的 0 条/)).toBeTruthy()
    expect(screen.queryByText('测试银行')).toBeNull()
  })

  it('排序时 null 值始终沉底', async () => {
    vi.mocked(researchApi.trend).mockResolvedValue(
      trendOut([
        trendRow({ code: '000001', name: '缺RPS', rps60: null }),
        trendRow({ code: '600000', name: '有RPS', rps60: 80 }),
      ]),
    )
    renderPage()
    await screen.findByText('缺RPS')
    const button = screen.getByRole('button', { name: /RPS20\/60\/120/ })
    fireEvent.click(button)
    let rows = screen.getAllByRole('row')
    expect(rows[1].textContent).toContain('有RPS')
    fireEvent.click(button)
    rows = screen.getAllByRole('row')
    expect(rows[1].textContent).toContain('有RPS')
  })

  it('并列值排序保持稳定顺序', async () => {
    vi.mocked(researchApi.trend).mockResolvedValue(
      trendOut([
        trendRow({ code: '000001', name: '第一名', trend_days: 8 }),
        trendRow({ code: '600000', name: '第二名', trend_days: 8 }),
      ]),
    )
    renderPage()
    await screen.findByText('第一名')
    fireEvent.click(screen.getByRole('button', { name: /趋势天数/ }))
    const rows = screen.getAllByRole('row')
    expect(rows[1].textContent).toContain('第一名')
    expect(rows[2].textContent).toContain('第二名')
  })

  it('切换 Tab 清空搜索条件', async () => {
    renderPage()
    await screen.findByText('测试银行')
    fireEvent.change(screen.getByLabelText('搜索'), { target: { value: '600000' } })
    await screen.findByText(/没有匹配/)
    fireEvent.click(screen.getByRole('radio', { name: /低位启动/ }))
    await screen.findByText('低位样本')
    expect((screen.getByLabelText('搜索') as HTMLInputElement).value).toBe('')
  })

  it('请求失败后可重试，且保留当前日期与筛选', async () => {
    vi.mocked(researchApi.trend).mockRejectedValue(new Error('网络中断或超时，请重试：/research/trend'))
    renderPage()
    await screen.findByText(/网络中断或超时/)
    fireEvent.change(screen.getByLabelText('交易日'), { target: { value: '2026-09-22' } })
    await waitFor(() =>
      expect(vi.mocked(researchApi.trend)).toHaveBeenLastCalledWith(
        expect.objectContaining({ date: '2026-09-22' }),
      ),
    )
    await screen.findByRole('button', { name: '重试' })
    vi.mocked(researchApi.trend).mockResolvedValue(trendOut([trendRow({ code: '600001', name: '重试成功' })]))
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    await screen.findByText('重试成功')
    expect(vi.mocked(researchApi.trend)).toHaveBeenLastCalledWith(
      expect.objectContaining({ date: '2026-09-22' }),
    )
  })

  it('服务繁忙时显示 503 文案而不是空结果', async () => {
    vi.mocked(researchApi.trend).mockRejectedValue(new Error('研究计算繁忙，请稍后重试'))
    renderPage()
    await screen.findByText('研究计算繁忙，请稍后重试')
    expect(screen.queryByText(/当前日期没有可展示的数据/)).toBeNull()
  })

  it('RPS 未采集时给出覆盖原因而不是「无结果」', async () => {
    const base = trendOut([])
    vi.mocked(researchApi.trend).mockResolvedValue({
      ...base,
      coverage: {
        ...base.coverage,
        data_status: 'no_rps',
        status_text: '当日个股日线已入库，但没有 RPS 记录（RPS 未采集）',
        daily_rows: 5249,
        rps_rows: 0,
        rps60_rows: 0,
      },
    })
    renderPage()
    await screen.findAllByText(/没有 RPS 记录/)
    expect(screen.getByText(/当日日线 5249 行/)).toBeTruthy()
    expect(screen.queryByText(/当前日期没有可展示的数据/)).toBeNull()
  })

  it('数据完整且无候选时显示正常空态', async () => {
    const base = trendOut([])
    vi.mocked(researchApi.trend).mockResolvedValue({
      ...base,
      coverage: {
        ...base.coverage,
        data_status: 'ok',
        status_text: '行情与 RPS 覆盖正常',
        daily_rows: 5000,
        rps_rows: 4900,
        rps60_rows: 4800,
        note: '当日数据完整，但没有 RPS60 ≥ 60 的股票',
      },
    })
    renderPage()
    await screen.findByText(/当前日期没有可展示的数据/)
    expect(screen.queryByText(/没有 RPS 记录/)).toBeNull()
  })

  it('低位启动展示行级 RPS 缺失原因', async () => {
    vi.mocked(researchApi.lowStart).mockResolvedValue({
      ...lowOut([
        lowRow({
          rps20: null,
          rps_status: 'day_missing',
          missing: ['RPS20 缺失（当日 RPS 未采集）'],
        }),
      ]),
      coverage: {
        candidates: 5261,
        bars: 4985,
        data_status: 'no_rps',
        status_text: '当日个股日线已入库，但没有 RPS 记录（RPS 未采集）',
        rps_missing_rows: 1,
      },
    })
    renderPage()
    await screen.findByText('测试银行')
    fireEvent.click(screen.getByRole('radio', { name: /低位启动/ }))
    await screen.findByText('低位样本')
    expect(screen.getByText(/缺失：RPS20 缺失（当日 RPS 未采集）/)).toBeTruthy()
    expect(screen.getByText(/没有 RPS 记录/)).toBeTruthy()
  })

  it('板块回退时显示实际数据日与计数关系', async () => {
    const row = {
      sector_code: 'A',
      name: '板块A',
      trade_date: '2026-09-24',
      rps20: 91,
      ret5: 1.2,
      ret10: 2.3,
      ret20: 3.4,
      ret60: 4.5,
      amount_ratio20: 1.1,
      up_ratio: null,
      strong_days: 3,
      state: '数据不足',
      state_daily: '新晋强势',
      breadth_status: 'missing_manifest',
      core: [],
      missing: ['成分股广度不可用：没有 manifest 记录，完整性未知'],
      strategy: STRATEGY,
    }
    vi.mocked(researchApi.sectorState).mockResolvedValue({
      trade_date: '2026-09-24',
      requested_date: '2026-10-08',
      actual_data_date: '2026-09-24',
      fallback: {
        applied: true,
        requested_date: '2026-10-08',
        actual_data_date: '2026-09-24',
        gap_trading_days: 5,
        severity: 'high',
        reason: '所选日期 2026-10-08 没有板块日线，已回退到最近可用交易日 2026-09-24（相差 5 个交易日，结果不代表所选日期的板块状态）',
      },
      rows: [row],
      coverage: {
        days: 61,
        data_status: 'fallback',
        boards_in_day: 7,
        boards_with_rps20: 6,
        excluded_short_history: 1,
        excluded_no_rps20: 0,
        matched: 6,
        returned: 1,
        truncated: 5,
        manifest_installed: false,
        breadth_complete: 0,
        breadth_mismatch: 0,
        breadth_partial: 0,
        breadth_unknown: 6,
      },
      strategy: STRATEGY,
    })
    renderPage()
    await screen.findByText('测试银行')
    fireEvent.click(screen.getByRole('radio', { name: /板块状态/ }))
    await screen.findByText('板块A')
    expect(screen.getByText(/已回退到最近可用交易日 2026-09-24/)).toBeTruthy()
    expect(screen.getByText(/实际数据日 2026-09-24/)).toBeTruthy()
    expect(screen.getByText(/历史不足 1/)).toBeTruthy()
    expect(screen.getByText(/另有 5 条被截断/)).toBeTruthy()
    expect(screen.getByText('无 manifest')).toBeTruthy()
    expect(screen.getByText(/日线强弱：新晋强势/)).toBeTruthy()
  })

  it('严格日期开关会以 allowFallback=false 重新请求', async () => {
    renderPage()
    await screen.findByText('测试银行')
    fireEvent.click(screen.getByRole('radio', { name: /板块状态/ }))
    await waitFor(() => expect(vi.mocked(researchApi.sectorState)).toHaveBeenCalled())
    fireEvent.click(screen.getByLabelText(/严格日期/))
    await waitFor(() =>
      expect(vi.mocked(researchApi.sectorState)).toHaveBeenLastCalledWith(
        expect.objectContaining({ allowFallback: false }),
      ),
    )
  })
})
