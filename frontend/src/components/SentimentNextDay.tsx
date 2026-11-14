import { useEffect, useMemo, useRef, useState } from 'react'
import { api, isNoDataError } from '../api/client'
import type {
  SentimentNextDay as SentimentNextDayData,
  SentimentNextDayGroup,
  SentimentNextDayMetric,
  SentimentNextDayPoint,
} from '../api/types'
import EChart from './EChart'
import type { ChartOption } from './EChart'
import LoadState from './LoadState'
import Panel from './Panel'
import Segmented from './Segmented'
import {
  AXIS_LABEL,
  AXIS_LINE,
  CHART,
  GRID,
  LEGEND,
  SERIES_PALETTE,
  SPLIT_LINE,
  TOOLTIP,
  withAlpha,
} from '../lib/chart'
import { fmtInt, fmtNum, fmtPct, fmtShortDate } from '../lib/format'

const GROUP_ITEMS: { key: SentimentNextDayGroup; label: string }[] = [
  { key: 'first_board', label: '首板' },
  { key: 'second_board', label: '二板' },
  { key: 'third_plus', label: '三板+' },
  { key: 'all_limit_up', label: '全部涨停' },
  { key: 'broken', label: '炸板' },
  { key: 'failed_promotion', label: '断板' },
]

const METRIC_ITEMS: { key: SentimentNextDayMetric; label: string }[] = [
  { key: 'limit_up_count', label: '涨停家数' },
  { key: 'broken_rate', label: '炸板率' },
  { key: 'max_consecutive', label: '最高连板' },
  { key: 'advance_ratio', label: '上涨占比' },
]

const RANGE_ITEMS = [
  { key: 20, label: '20 日' },
  { key: 60, label: '60 日' },
  { key: 120, label: '120 日' },
  { key: 250, label: '250 日' },
]

const STATUS_LABEL: Record<string, string> = {
  VERIFIED: '可验证',
  PARTIAL: '部分缺失',
  PENDING: '待观察',
  UNVERIFIED: '不可验证',
  EMPTY: '无样本',
}

const REASON_LABEL: Record<string, string> = {
  UP_POOL_MISSING: 'D 日涨停池未采集',
  UP_POOL_UNVERIFIED: 'D 日涨停池不可信',
  UP_POOL_EMPTY: 'D 日涨停池异常为空',
  BROKEN_POOL_MISSING: 'D 日炸板池未采集',
  BROKEN_POOL_UNVERIFIED: 'D 日炸板池不可信',
  PREV_UP_POOL_MISSING: 'D-1 涨停池未采集',
  PREV_UP_POOL_UNVERIFIED: 'D-1 涨停池不可信',
  RELAY_UNVERIFIED: 'P0-B 漏斗不可信',
  NO_SAMPLES: '该分组当日无样本',
  OUTCOME_PENDING: 'D+1 行情未到齐',
  OUTCOME_PARTIAL: 'D+1 部分样本缺行情',
  OUTCOME_LIMIT_POOL_UNVERIFIED: 'D+1 涨停池不可信',
}

function reasonText(code: string): string {
  return REASON_LABEL[code] ?? code
}

function statusTone(status: string): string {
  if (status === 'VERIFIED') return 'text-up'
  if (status === 'PARTIAL' || status === 'PENDING') return 'text-warn'
  if (status === 'EMPTY') return 'text-fg-muted'
  return 'text-danger'
}

/**
 * 指标单元格：缺失时区分三种「没有数」——
 * - PENDING：D+1 还没到，不是 0%；
 * - UNVERIFIED / EMPTY：源数据不可信 / 无样本；
 * - 其它：`—`（部分缺失）。
 */
function metricCell(row: SentimentNextDayPoint, value: number | null, text: string): string {
  if (value != null) return text
  if (row.data_quality_status === 'PENDING') return 'PENDING'
  if (row.data_quality_status === 'UNVERIFIED') return 'UNVERIFIABLE'
  return '—'
}

export default function SentimentNextDay() {
  const [group, setGroup] = useState<SentimentNextDayGroup>('first_board')
  const [metric, setMetric] = useState<SentimentNextDayMetric>('limit_up_count')
  const [days, setDays] = useState(60)
  const [includePending, setIncludePending] = useState(true)
  const [data, setData] = useState<SentimentNextDayData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const seq = useRef(0)

  useEffect(() => {
    const current = ++seq.current
    setLoading(true)
    setError(null)
    api
      .sentimentNextDay({ days, group, metric, includePending })
      .then((value) => {
        // 快速切换分组/指标/区间时，旧响应不能覆盖新结果
        if (current === seq.current) setData(value)
      })
      .catch((err: Error) => {
        if (current !== seq.current) return
        setData(null)
        setError(isNoDataError(err) ? null : err.message)
      })
      .finally(() => {
        if (current === seq.current) setLoading(false)
      })
    return () => {
      seq.current += 1
    }
  }, [days, group, metric, includePending])

  const option = useMemo<ChartOption>(() => {
    if (!data || data.series.length === 0) return {}
    const dates = data.series.map((row) => fmtShortDate(row.sample_date))
    return {
      grid: { ...GRID, top: 46 },
      legend: {
        ...LEGEND,
        top: 2,
        data: [data.metric_label, 'D+1 平均涨跌幅', 'D+1 上涨比例'],
      },
      tooltip: { ...TOOLTIP, trigger: 'axis' as const },
      axisPointer: { type: 'shadow' as const },
      xAxis: {
        type: 'category' as const,
        data: dates,
        axisLabel: { ...AXIS_LABEL, interval: Math.max(0, Math.floor(dates.length / 12)) },
        axisLine: AXIS_LINE,
        axisTick: { show: false },
      },
      yAxis: [
        {
          type: 'value' as const,
          // 左轴只放情绪指标：单位随所选指标变化，轴名必须写明
          name: data.metric_label,
          nameTextStyle: { color: CHART.fgDim, fontSize: 11, fontFamily: AXIS_LABEL.fontFamily },
          axisLabel: AXIS_LABEL,
          splitLine: SPLIT_LINE,
          axisLine: { show: false },
        },
        {
          type: 'value' as const,
          name: '%',
          nameTextStyle: { color: CHART.fgDim, fontSize: 11, fontFamily: AXIS_LABEL.fontFamily },
          axisLabel: { ...AXIS_LABEL, formatter: '{value}%' },
          splitLine: { show: false },
          axisLine: { show: false },
        },
      ],
      series: [
        {
          type: 'bar' as const,
          name: 'D+1 平均涨跌幅',
          yAxisIndex: 1,
          // null 保持空洞：PENDING / 不可验证的日子不画成 0%
          data: data.series.map((row) => row.avg_pct),
          itemStyle: { color: withAlpha(CHART.accent, 0.42) },
          barMaxWidth: 14,
          z: 1,
        },
        {
          type: 'line' as const,
          name: data.metric_label,
          yAxisIndex: 0,
          data: data.series.map((row) => row.sentiment_value),
          symbol: 'none' as const,
          smooth: false,
          connectNulls: false,
          lineStyle: { width: 1.6, color: SERIES_PALETTE[1] },
          itemStyle: { color: SERIES_PALETTE[1] },
          z: 3,
        },
        {
          type: 'line' as const,
          name: 'D+1 上涨比例',
          yAxisIndex: 1,
          data: data.series.map((row) => row.up_ratio),
          symbol: 'none' as const,
          smooth: false,
          connectNulls: false,
          lineStyle: { width: 1.2, type: 'dashed' as const, color: CHART.fgMuted },
          itemStyle: { color: CHART.fgMuted },
          z: 2,
        },
      ],
    }
  }, [data])

  const summary = data?.summary
  const meta = data
    ? `${data.group_label} · ${data.metric_label} · 数据截至 ${data.data_as_of ?? '—'}`
    : '只读本地库 · 历史描述性统计'

  return (
    <Panel title="情绪环境 × 次日表现" meta={<span className="num">{meta}</span>} delay={240}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-line-soft px-3 py-2 text-[12px]">
        <span className="text-fg-dim">分组</span>
        <Segmented value={group} items={GROUP_ITEMS} onChange={setGroup} />
        <span className="ml-2 text-fg-dim">情绪指标</span>
        <Segmented value={metric} items={METRIC_ITEMS} onChange={setMetric} />
        <span className="ml-2 text-fg-dim">区间</span>
        <Segmented value={days} items={RANGE_ITEMS} onChange={setDays} />
        <label className="ml-2 flex cursor-pointer items-center gap-1 text-fg-dim">
          <input
            type="checkbox"
            checked={includePending}
            onChange={(event) => setIncludePending(event.target.checked)}
          />
          显示待观察日
        </label>
      </div>

      {loading ? (
        <LoadState loading className="px-4 py-10" />
      ) : error ? (
        <LoadState error={error} className="px-4 py-10" />
      ) : !data || data.series.length === 0 ? (
        <LoadState emptyText="所选区间没有可统计的日期" className="px-4 py-10" />
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-b border-line-soft px-3 py-2.5 text-[12px]">
            <span className="text-fg-dim">样本</span>
            <span className="num">{fmtInt(summary?.sample_count)}</span>
            <span className="text-fg-dim">有效 / 缺失</span>
            <span className="num">
              {fmtInt(summary?.valid_count)} / {fmtInt(summary?.missing_count)}
            </span>
            <span
              className="text-fg-dim"
              title={`逐券 pooled 均值：每个证券-交易日算一个观测。有效观测 ${fmtInt(summary?.valid_count)} 条，来自 ${fmtInt(summary?.stat_day_count)} 个有统计的交易日。`}
            >
              D+1 逐券均值 / 合并中位
            </span>
            <span
              className="num"
              title={`pooled 口径：${fmtInt(summary?.valid_count)} 条证券-交易日观测；合并中位数不是每日中位数的中位数。`}
            >
              {fmtPct(summary?.security_weighted_avg_pct)} / {fmtPct(summary?.median_pct)}
            </span>
            <span
              className="text-fg-dim"
              title={`交易日等权均值：先算每日均值再按天平均（S1 旧口径），共 ${fmtInt(summary?.stat_day_count)} 个有统计的交易日；与逐券均值不一致是正常的。`}
            >
              日等权均值
            </span>
            <span className="num">{fmtPct(summary?.day_equal_weight_avg_pct)}</span>
            <span className="text-fg-dim">上涨比例</span>
            <span className="num">{fmtNum(summary?.up_ratio, 1, '%')}</span>
            <span className="text-fg-dim">再涨停比例</span>
            <span className="num">{fmtNum(summary?.limit_up_again_ratio, 1, '%')}</span>
            {summary?.unverifiable_count ? (
              <span className="text-warn">
                无法验证是否再涨停：{fmtInt(summary.unverifiable_count)} 只
              </span>
            ) : null}
          </div>

          <div className="px-2 pt-2">
            <EChart option={option} height={300} />
          </div>

          <div className="overflow-auto border-t border-line-soft">
            <table className="grid-table min-w-full text-[13px]">
              <thead>
                <tr>
                  <th>情绪档位</th>
                  <th title="落在该档位的交易日数">交易日</th>
                  <th>有统计日</th>
                  <th>样本</th>
                  <th>有效</th>
                  <th
                    title="逐券 pooled 均值：每个证券-交易日算一个观测，样本多的交易日权重更大"
                  >
                    D+1 逐券均值
                  </th>
                  <th title="交易日等权均值：先算每日均值再按天平均（S1 旧口径）">
                    日等权
                  </th>
                  <th title="全部有效观测（证券-交易日）的合并中位数">D+1 合并中位</th>
                  <th>上涨比例</th>
                  <th>再涨停比例</th>
                </tr>
              </thead>
              <tbody>
                {data.bands.map((band) => (
                  <tr key={band.label}>
                    <td>{band.label}</td>
                    <td className="num">{fmtInt(band.day_count)}</td>
                    <td className="num">{fmtInt(band.stat_day_count)}</td>
                    <td className="num">{fmtInt(band.sample_count)}</td>
                    <td className="num">{fmtInt(band.valid_count)}</td>
                    <td className="num">{fmtPct(band.security_weighted_avg_pct)}</td>
                    <td className="num">{fmtPct(band.day_equal_weight_avg_pct)}</td>
                    <td className="num">{fmtPct(band.median_pct)}</td>
                    <td className="num">{fmtNum(band.up_ratio, 1, '%')}</td>
                    <td className="num">{fmtNum(band.limit_up_again_ratio, 1, '%')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="overflow-auto border-t border-line-soft">
            <table className="grid-table min-w-full text-[13px]">
              <thead>
                <tr>
                  <th>D 日</th>
                  <th>D+1</th>
                  <th>状态</th>
                  <th>{data.metric_label}</th>
                  <th>样本</th>
                  <th>有效</th>
                  <th>缺失</th>
                  <th>平均涨跌</th>
                  <th>中位涨跌</th>
                  <th>上涨比例</th>
                  <th>再涨停</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>
                {data.series.map((row) => (
                  <tr key={row.sample_date}>
                    <td className="num">{row.sample_date}</td>
                    <td className="num">{row.outcome_date ?? '—'}</td>
                    <td className={`num ${statusTone(row.data_quality_status)}`}>
                      {STATUS_LABEL[row.data_quality_status] ?? row.data_quality_status}
                    </td>
                    <td className="num">{fmtNum(row.sentiment_value, 2)}</td>
                    <td className="num">{fmtInt(row.sample_count)}</td>
                    <td className="num">{fmtInt(row.valid_count)}</td>
                    <td className="num">{fmtInt(row.missing_count)}</td>
                    <td className="num">{metricCell(row, row.avg_pct, fmtPct(row.avg_pct))}</td>
                    <td className="num">
                      {metricCell(row, row.median_pct, fmtPct(row.median_pct))}
                    </td>
                    <td className="num">
                      {metricCell(row, row.up_ratio, fmtNum(row.up_ratio, 1, '%'))}
                    </td>
                    <td className="num">
                      {metricCell(
                        row,
                        row.limit_up_again_ratio,
                        fmtNum(row.limit_up_again_ratio, 1, '%'),
                      )}
                    </td>
                    <td className="text-fg-muted">
                      {row.data_quality_reasons.length
                        ? row.data_quality_reasons.map(reasonText).join(' · ')
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="border-t border-line-soft px-3 py-2 text-[12px] leading-relaxed text-fg-dim">
            <div>
              口径：{data.quality.group_note} 阈值版本 <span className="num">{data.threshold_version}</span>
              ，聚合版本 <span className="num">{data.aggregation_version}</span>
              。PENDING = D+1 行情未到齐（不按 0% 计），UNVERIFIABLE = 源数据不可信。
            </div>
            <ul className="mt-1 space-y-0.5">
              {data.notes.map((note) => (
                <li key={note}>· {note}</li>
              ))}
            </ul>
          </div>
        </>
      )}
    </Panel>
  )
}
