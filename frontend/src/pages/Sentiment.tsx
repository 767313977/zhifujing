import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { Breadth, Erp, EtfNetFlow, FearGreed, FuturesBasis, IndexHistory, IndexSeries, IndustryBreadth, MarginTemp, NhNl, Northbound, Obv, RelayTemp, Sentiment, Volatility } from '../api/types'
import Alert from '../components/Alert'
import AdvanceDeclinePanel from '../components/AdvanceDeclinePanel'
import BreadthThermometerPanel from '../components/BreadthThermometerPanel'
import EChart from '../components/EChart'
import type { ChartOption } from '../components/EChart'
import ErpPanel from '../components/ErpPanel'
import EtfFlowPanel from '../components/EtfFlowPanel'
import FearGreedPanel from '../components/FearGreedPanel'
import FuturesBasisPanel from '../components/FuturesBasisPanel'
import IndustryBreadthPanel from '../components/IndustryBreadthPanel'
import Layout from '../components/Layout'
import LoadState from '../components/LoadState'
import MarginTempPanel from '../components/MarginTempPanel'
import NhNlPanel from '../components/NhNlPanel'
import NorthboundPanel from '../components/NorthboundPanel'
import ObvPanel from '../components/ObvPanel'
import Panel from '../components/Panel'
import RelayTempPanel from '../components/RelayTempPanel'
import VolatilityPanel from '../components/VolatilityPanel'
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
import { fmtAmount, fmtShortDate } from '../lib/format'

/**
 * 各读数块的 key（失败信息按它归类，见 `panelErrors`）。
 *
 * `indexHistory` / `sentiment` 也在里面：它们原来是「主序列」，失败会连累整页
 * 12 个面板一起丢掉（见下面 effect 的注释，2026-10-22 体检 #4），现在也按 key 记错。
 */
type PanelKey =
  | 'indexHistory'
  | 'sentiment'
  | 'fearGreed'
  | 'breadth'
  | 'relayTemp'
  | 'futuresBasis'
  | 'erp'
  | 'nhNl'
  | 'obv'
  | 'etfNetFlow'
  | 'marginTemp'
  | 'industryBreadth'
  | 'northbound'
  | 'volatility'

/** 首日归一为 100，让点位数量级不同的指数能在同一轴上比较。 */
function normalize(values: (number | null)[]): (number | null)[] {
  const base = values.find((value) => value != null && value !== 0)
  if (base == null) return values.map(() => null)
  return values.map((value) =>
    value == null ? null : Number(((value / base) * 100).toFixed(2)),
  )
}

function rangeLabel(dates: string[]): string {
  if (dates.length === 0) return '暂无数据'
  return `${fmtShortDate(dates[0])} ~ ${fmtShortDate(dates[dates.length - 1])} · ${dates.length} 个交易日`
}

interface TooltipItem {
  seriesName: string
  value: number | null
  axisValue: string
}

/** 成交额是 12 位数字，必须换算成万亿才可读；其余序列按原值显示。 */
function axisTooltip(params: unknown): string {
  const items = params as TooltipItem[]
  if (!items?.length) return ''
  const rows = items.map((item) => {
    const text =
      item.seriesName === '两市成交额'
        ? // ECharts 对空值点传给 formatter 的是 '-' 而不是 null，必须按类型判断，
          // 否则 fmtAmount('-') 里 Math.abs 得 NaN、最后 .toFixed 抛异常
          typeof item.value === 'number'
          ? fmtAmount(item.value)
          : '—'
        : item.value == null
          ? '—'
          : String(item.value)
    return `${item.seriesName}：${text}`
  })
  return [items[0].axisValue, ...rows].join('<br/>')
}

export default function SentimentPage() {
  const [history, setHistory] = useState<IndexHistory | null>(null)
  const [sentiment, setSentiment] = useState<Sentiment[]>([])
  const [fearGreed, setFearGreed] = useState<FearGreed | null>(null)
  const [breadth, setBreadth] = useState<Breadth | null>(null)
  const [relayTemp, setRelayTemp] = useState<RelayTemp | null>(null)
  const [futuresBasis, setFuturesBasis] = useState<FuturesBasis | null>(null)
  const [erp, setErp] = useState<Erp | null>(null)
  const [nhNl, setNhNl] = useState<NhNl | null>(null)
  const [obv, setObv] = useState<Obv | null>(null)
  const [etfNetFlow, setEtfNetFlow] = useState<EtfNetFlow | null>(null)
  const [marginTemp, setMarginTemp] = useState<MarginTemp | null>(null)
  const [industryBreadth, setIndustryBreadth] = useState<IndustryBreadth | null>(null)
  const [northbound, setNorthbound] = useState<Northbound | null>(null)
  const [volatility, setVolatility] = useState<Volatility | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  /** 增强读数各自的失败原因（404 不算失败，见 `track`） */
  const [panelErrors, setPanelErrors] = useState<Partial<Record<PanelKey, string>>>({})

  useEffect(() => {
    // 增强读数失败时**记下原因**而不是一律静默变 null（2026-10-22 体检）：
    // 以前 500 / 超时和「库里还没有这条数据」都显示成「暂无数据」，看不出是接口挂了。
    // 404 仍是正常空态 —— 这几个接口在没数据时后端就返回 404。
    const track = (key: PanelKey) => (err: Error & { status?: number }) => {
      if (err.status !== 404) {
        setPanelErrors((old) => ({ ...old, [key]: err.message }))
      }
      return null
    }
    Promise.all([
      // 这两条原来是**唯一没有兜底**的请求（2026-10-22 体检 #4）：它们一失败整个
      // `Promise.all` 就 reject，下面的 `.then` 整段不执行 —— 另外 12 个已经成功取回的
      // 面板数据一个都进不了 state，全部渲染成「暂无数据，请先执行采集」。
      // 现在与其它读数一样按 key 记错，失败只影响自己那一块。
      api.indexHistory(120).catch(track('indexHistory')),
      api.sentimentSeries(60).catch(track('sentiment')),
      api.fearGreed(250).catch(track('fearGreed')),
      api.breadth(250).catch(track('breadth')),
      api.relayTemp(120).catch(track('relayTemp')),
      api.futuresBasis(250).catch(track('futuresBasis')),
      api.erp(250).catch(track('erp')),
      api.nhNl(250).catch(track('nhNl')),
      api.obv(250).catch(track('obv')),
      api.etfNetFlow(250).catch(track('etfNetFlow')),
      api.marginTemp(250).catch(track('marginTemp')),
      api.industryBreadth().catch(track('industryBreadth')),
      api.northboundActivity(250).catch(track('northbound')),
      api.volatility(250).catch(track('volatility')),
    ])
      .then(([historyData, sentimentData, fearGreedData, breadthData, relayTempData, futuresBasisData, erpData, nhNlData, obvData, etfNetFlowData, marginTempData, industryBreadthData, northboundData, volatilityData]) => {
        setHistory(historyData)
        // 情绪指标只有数据源保留窗口内的日期才有值，其余为 null。
        // 请求失败时这里是 null（上面的 catch 兜成了 null），不是空列表
        setSentiment((sentimentData ?? []).filter((item) => item.limit_up_count != null))
        setFearGreed(fearGreedData)
        setBreadth(breadthData)
        setRelayTemp(relayTempData)
        setFuturesBasis(futuresBasisData)
        setErp(erpData)
        setNhNl(nhNlData)
        setObv(obvData)
        setEtfNetFlow(etfNetFlowData)
        setMarginTemp(marginTempData)
        setIndustryBreadth(industryBreadthData)
        setNorthbound(northboundData)
        setVolatility(volatilityData)
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false))
  }, [])

  /** 两市成交额 = 上证指数 + 深证成指，与今日复盘口径一致。 */
  const indexOption = useMemo<ChartOption>(() => {
    if (!history) return {}
    const shortDates = history.dates.map(fmtShortDate)
    const amountSeries = ['000001.SH', '399001.SZ']
      .map((code) => history.series.find((item) => item.code === code))
      .filter((item): item is IndexSeries => item != null)

    const amount = history.dates.map((_, i) => {
      const values = amountSeries
        .map((item) => item.amount[i])
        .filter((value): value is number => value != null)
      return values.length ? values.reduce((a, b) => a + b, 0) : null
    })

    return {
      // top 留 46px 给图例：否则图例画在绘图区内、会被高柱压住
      grid: { ...GRID, top: 46 },
      legend: {
        ...LEGEND,
        top: 2,
        // 成交额是柱序列，必须显式写进 legend.data，否则不显示图例
        data: [...history.series.map((item) => item.name ?? item.code), '两市成交额'],
      },
      tooltip: { ...TOOLTIP, trigger: 'axis' as const, formatter: axisTooltip },
      axisPointer: {
        type: 'line' as const,
        lineStyle: { color: CHART.fgDim, type: 'dashed' as const },
      },
      xAxis: {
        type: 'category' as const,
        data: shortDates,
        axisLabel: { ...AXIS_LABEL, interval: Math.max(0, Math.floor(shortDates.length / 10)) },
        axisLine: AXIS_LINE,
        axisTick: { show: false },
        boundaryGap: true,
      },
      yAxis: [
        {
          type: 'value' as const,
          name: '首日=100',
          nameTextStyle: { color: CHART.fgDim, fontSize: 11, fontFamily: AXIS_LABEL.fontFamily },
          scale: true,
          axisLabel: { ...AXIS_LABEL, formatter: '{value}' },
          splitLine: SPLIT_LINE,
          axisLine: { show: false },
        },
        {
          type: 'value' as const,
          name: '成交额',
          nameTextStyle: { color: CHART.fgDim, fontSize: 11, fontFamily: AXIS_LABEL.fontFamily },
          axisLabel: {
            ...AXIS_LABEL,
            formatter: (value: number) => `${(value / 1e12).toFixed(1)}万亿`,
          },
          splitLine: { show: false },
          axisLine: { show: false },
        },
      ],
      series: [
        {
          type: 'bar' as const,
          name: '两市成交额',
          yAxisIndex: 1,
          data: amount,
          itemStyle: { color: withAlpha(CHART.fgMuted, 0.16) },
          barMaxWidth: 10,
          z: 0,
        },
        ...history.series.map((item, i) => ({
          type: 'line' as const,
          name: item.name ?? item.code,
          data: normalize(item.close),
          smooth: false,
          symbol: 'none' as const,
          lineStyle: { width: 1.6, color: SERIES_PALETTE[i % SERIES_PALETTE.length] },
          itemStyle: { color: SERIES_PALETTE[i % SERIES_PALETTE.length] },
          z: 2,
        })),
      ],
    }
  }, [history])

  const emotionOption = useMemo<ChartOption>(() => {
    const dates = sentiment.map((item) => fmtShortDate(item.trade_date))
    return {
      grid: { ...GRID, top: 46 },
      legend: { ...LEGEND, top: 2, data: ['涨停', '跌停', '炸板', '封板率'] },
      tooltip: { ...TOOLTIP, trigger: 'axis' as const },
      axisPointer: { type: 'shadow' as const },
      xAxis: {
        type: 'category' as const,
        data: dates,
        axisLabel: AXIS_LABEL,
        axisLine: AXIS_LINE,
        axisTick: { show: false },
      },
      yAxis: [
        {
          type: 'value' as const,
          name: '家数',
          nameTextStyle: { color: CHART.fgDim, fontSize: 11, fontFamily: AXIS_LABEL.fontFamily },
          axisLabel: AXIS_LABEL,
          splitLine: SPLIT_LINE,
          axisLine: { show: false },
        },
        {
          type: 'value' as const,
          name: '封板率 %',
          nameTextStyle: { color: CHART.fgDim, fontSize: 11, fontFamily: AXIS_LABEL.fontFamily },
          min: 0,
          max: 100,
          axisLabel: { ...AXIS_LABEL, formatter: '{value}%' },
          splitLine: { show: false },
          axisLine: { show: false },
        },
      ],
      series: [
        {
          type: 'bar' as const,
          name: '涨停',
          data: sentiment.map((item) => item.limit_up_count),
          itemStyle: { color: CHART.up },
          barMaxWidth: 14,
        },
        {
          type: 'bar' as const,
          name: '跌停',
          data: sentiment.map((item) => item.limit_down_count),
          itemStyle: { color: CHART.down },
          barMaxWidth: 14,
        },
        {
          type: 'bar' as const,
          name: '炸板',
          data: sentiment.map((item) => item.broken_count),
          itemStyle: { color: withAlpha(CHART.fgMuted, 0.45) },
          barMaxWidth: 14,
        },
        {
          type: 'line' as const,
          name: '封板率',
          yAxisIndex: 1,
          data: sentiment.map((item) => item.seal_rate),
          symbol: 'circle',
          symbolSize: 5,
          lineStyle: { width: 1.8, color: CHART.accent },
          itemStyle: { color: CHART.accent },
        },
      ],
    }
  }, [sentiment])

  const ladderOption = useMemo<ChartOption>(() => {
    const dates = sentiment.map((item) => fmtShortDate(item.trade_date))
    return {
      grid: { ...GRID, top: 24 },
      tooltip: { ...TOOLTIP, trigger: 'axis' as const },
      xAxis: {
        type: 'category' as const,
        data: dates,
        axisLabel: AXIS_LABEL,
        axisLine: AXIS_LINE,
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value' as const,
        name: '连板',
        nameTextStyle: { color: CHART.fgDim, fontSize: 11, fontFamily: AXIS_LABEL.fontFamily },
        minInterval: 1,
        axisLabel: AXIS_LABEL,
        splitLine: SPLIT_LINE,
        axisLine: { show: false },
      },
      series: [
        {
          type: 'bar' as const,
          name: '最高连板',
          data: sentiment.map((item) => item.max_consecutive),
          itemStyle: { color: CHART.accent },
          barMaxWidth: 14,
        },
      ],
    }
  }, [sentiment])

  const indexDates = history?.dates ?? []
  const emotionDates = sentiment.map((item) => item.trade_date)

  const toolbar = (
    <span className="num hidden text-[13px] text-fg-dim lg:inline">
      {loading ? '加载中…' : `${indexDates.length} / ${emotionDates.length} 天`}
    </span>
  )

  return (
    <Layout toolbar={toolbar}>
      {error && (
        <Alert onClose={() => setError(null)}>{error}</Alert>
      )}

      <div className="space-y-4">
        {/* 恐贪是本页的总读数，排在最前：先给「今天整体冷不冷」，再展开下面拆开的
            指数走势与情绪明细 */}
        <FearGreedPanel
          data={fearGreed}
          loading={loading}
          error={panelErrors.fearGreed}
          delay={40}
        />

        {/* 中国波指（QVIX）与恐贪说的是同一件事的两个侧面：恐贪是「从个股统计出来的
            情绪」，QVIX 是「期权市场用真金白银定价出来的恐慌」。紧挨着恐贪放 */}
        <VolatilityPanel
          data={volatility}
          loading={loading}
          error={panelErrors.volatility}
          delay={45}
        />

        {/* 宽度与接力是恐贪的两块补镜：一个看「指数涨有没有广度支撑」，一个看
            「打板生态热不热」。都在恐贪之后、指数走势之前 —— 几个温度计连着读 */}
        <BreadthThermometerPanel
          data={breadth}
          loading={loading}
          error={panelErrors.breadth}
          delay={60}
        />
        {/* 涨跌分布紧跟宽度：宽度说「有多少票在均线上方」，这里说「今天的涨跌是怎么分布的」
            —— 同样是 3000 家下跌，全在 -1% 与全在 -5% 是两种完全不同的日子 */}
        <AdvanceDeclinePanel delay={65} />
        <RelayTempPanel
          data={relayTemp}
          loading={loading}
          error={panelErrors.relayTemp}
          delay={70}
        />

        {/* 基差与 ERP 是「钱怎么看后市」的两面：一个看期货对冲成本（负 = 贴水 = 谨慎），
            一个看股票相对债券便不便宜（高 = 便宜）。紧跟在温度计之后 */}
        <FuturesBasisPanel
          data={futuresBasis}
          loading={loading}
          error={panelErrors.futuresBasis}
          delay={80}
        />
        <ErpPanel data={erp} loading={loading} error={panelErrors.erp} delay={90} />

        {/* NH-NL 与量能潮是「广度与量能」的两面：一个看指数新高有没有广度支撑
            （顶底背离），一个看钱在往哪个方向走（量价背离）。都在指数走势之前 ——
            看完它们再看指数，走势就不只是一个点位了 */}
        <NhNlPanel data={nhNl} loading={loading} error={panelErrors.nhNl} delay={92} />
        <ObvPanel data={obv} loading={loading} error={panelErrors.obv} delay={94} />

        {/* ETF 净申赎是「增量资金」那一条：OBV 说成交的钱往哪个方向走（存量博弈
            也能放量），份额说场外有没有真的申购进来。紧跟量能潮之后 */}
        <EtfFlowPanel
          data={etfNetFlow}
          loading={loading}
          error={panelErrors.etfNetFlow}
          delay={96}
        />

        {/* 两融杠杆温度是「资金加到什么程度」那一条：ETF 净申赎看场内基金的申赎，
            两融看融资盘的水位与变化速度。紧跟 ETF 之后 */}
        <MarginTempPanel
          data={marginTemp}
          loading={loading}
          error={panelErrors.marginTemp}
          delay={97}
        />

        {/* 北向活跃度是「外资参与度」那一条（只有成交额、没有净流入）。
            与两融同属资金面，紧挨着 */}
        <NorthboundPanel
          data={northbound}
          loading={loading}
          error={panelErrors.northbound}
          delay={98}
        />

        {/* 行业宽度是「宽度拆到行业」那一层：前面全是时间序列，这个是当日横截面 ——
            分辨指数是靠少数行业扛着（结构行情）还是行业齐涨（共振）*/}
        <IndustryBreadthPanel
          data={industryBreadth}
          loading={loading}
          error={panelErrors.industryBreadth}
          delay={99}
        />

        <Panel
          title="指数走势"
          meta={
            <span className="num">
              {rangeLabel(indexDates)}
              <span className="ml-3 text-fg-dim">首日归一为 100 · 灰色柱为两市成交额</span>
            </span>
          }
          delay={100}
        >
          {loading ? (
            <div className="flex h-[320px] items-center justify-center text-[14px] text-fg-dim">
              <span className="pulse-soft">加载中…</span>
            </div>
          ) : panelErrors.indexHistory ? (
            // 失败与「没有数据」分开（2026-10-22 体检 #4）
            <LoadState
              error={panelErrors.indexHistory}
              emptyText="暂无指数走势数据"
              className="px-4 py-10"
            />
          ) : (
            <div className="px-2 pt-2">
              <EChart option={indexOption} height={320} />
            </div>
          )}
        </Panel>

        {/* 区间不同必须显式说明，否则会被当成与上图同一段时间读 */}
        <div className="rise flex items-start gap-2.5 border border-accent/25 bg-accent/[0.04] px-3.5 py-2.5 text-[13px] leading-relaxed text-fg-muted">
          <span className="mt-[3px] h-[6px] w-[6px] shrink-0 bg-accent" />
          <span>
            下方情绪指标的区间比指数短得多：涨停 / 跌停 / 炸板明细的数据源
            <span className="text-fg">只保留最近 15 个交易日</span>
            ，而指数有完整历史。两者是
            <span className="text-fg">并列关系，不是同一时间轴</span>
            ——上图提供长周期背景，下图看当下的情绪强弱。
          </span>
        </div>

        <Panel title="情绪指标" meta={<span className="num">{rangeLabel(emotionDates)}</span>} delay={160}>
          {loading ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">加载中…</div>
          ) : sentiment.length === 0 ? (
            <LoadState
              error={panelErrors.sentiment}
              emptyText="暂无情绪数据，请先在「今日复盘」执行采集"
              className="px-4 py-10"
            />
          ) : (
            <div className="px-2 pt-2">
              <EChart option={emotionOption} height={300} />
            </div>
          )}
        </Panel>

        <Panel title="连板高度" meta={<span className="num">市场高度 · 最高连板数</span>} delay={220}>
          {loading ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">加载中…</div>
          ) : sentiment.length === 0 ? (
            <LoadState
              error={panelErrors.sentiment}
              emptyText="暂无连板高度数据"
              className="px-4 py-10"
            />
          ) : (
            <div className="px-2 pt-2">
              <EChart option={ladderOption} height={200} />
            </div>
          )}
        </Panel>

        {/* 补一段文字口径说明，避免只看图产生误读 */}
        <div className="panel rise px-4 py-3 text-[13px] leading-relaxed text-fg-dim" style={{ animationDelay: '260ms' }}>
          <div className="mb-1.5 text-fg-muted">口径说明</div>
          <ul className="space-y-1">
            <li>
              · 涨停 / 跌停 / 炸板家数取自涨停板三池，与「今日复盘」同源；
              封板率 = 涨停 ÷（涨停 + 炸板）
            </li>
            <li>
              · 成交额 = 上证指数 + 深证成指，单位见轴标签
            </li>
            <li>
              · 指数涨跌家数与全市场涨跌家数<span className="text-fg-muted">无法回补</span>：
              前者的两个接口对同一指数同一天给出不同数值，后者数据源只提供当日值，
              因此本页不展示历史涨跌家数，避免混入口径不一致的序列
            </li>
            <li>
              · 恐惧贪婪指数 = 波动率 / RSI / 相对换手率 / 融资融券 / 市场宽度 /
              涨跌停强度，六项各自按滚动 <span className="num">250</span> 个交易日分位
              折算成 0–100 后再等权平均，越高越贪婪；开头的几天分位历史不足，
              分数为<span className="text-fg-muted">空</span>（图上断开，不是 0）。
              曲线取最近 <span className="num">250</span> 个交易日，默认只铺开最近约
              120 天，往左拖底部的缩放条可以看更早；那条随数值变色的彩虹线就是本指数，
              叠加的蓝色线是<span className="text-fg">上证指数收盘点位</span>
              （与恐贪的计算基准同一条）
            </li>
            <li>
              · 中国波指 QVIX = <span className="text-fg">期权隐含波动率</span>
              （50ETF / 300ETF 两个品种，单位是百分数）—— 恐贪是「从个股统计出来的情绪」，
              QVIX 是「期权市场用真金白银定价出来的恐慌」，两者常互相印证。
              <span className="text-fg">与指数反向</span>：指数跌、波动率升；虚线是上证
              收盘点位。⚠️ 它<span className="text-fg-muted">不是交易所官方 iVX</span>
              （那条 2018 年停更），数据由期权论坛按方差互换思路合成、站内只落库与读取；
              分位是滚动 <span className="num">250</span> 个交易日里的位置，曲线取最近
              <span className="num">250</span> 个交易日
            </li>
            <li>
              · 恐贪指数里的<span className="text-fg-muted">市场宽度</span>由全市场个股日线
              现算（上涨家数占比），与本页「指数涨跌家数」是两个口径，数值不会完全对齐
            </li>
            <li>
              · 市场宽度温度计 = 全市场个股收盘价站上 <span className="num">MA20 / MA60 /
              MA250（年线）</span>的占比，上市不满 <span className="num">N</span> 根 K 线的
              个股不进分母；上方占比越高越热。新高 / 新低为
              <span className="text-fg">收盘价创 60 / 250 日新高新低的家数</span>，
              柱为「60 日新高 − 新低」差值。年线占比（紫线）在回补初期历史不足，为
              <span className="text-fg-muted">空</span>（图上断开，不是 0）
            </li>
            <li>
              · 短线接力温度 = 封板率 / 连板高度 / 晋级率 / 昨涨停今表现 / 涨跌停强度 /
              跌停压力，六项各自按<span className="text-fg">固定阈值</span>折算成 0–100
              后等权平均（与恐贪的滚动分位不同，不看历史位置），≥3 项有效才出分；
              晋级率 = 昨日首板今日仍涨停的家数 ÷ 昨日首板家数。曲线取最近
              <span className="num">120</span> 个交易日
            </li>
            <li>
              · 期指年化基差 = IF / IH / IC / IM 主力连续相对现货指数的基差年化：
              基差 =（期货 − 现货）÷ 现货，<span className="text-fg">负值 = 贴水</span>
              （中性策略的空头对冲成本，贴水越深越谨慎）；年化 = 基差 × 365 ÷ 距交割日
              天数，交割日取交割月第三个周五，主力连续近似当月合约、
              <span className="text-fg-muted">距交割不足 8 天按下月合约算</span>
              （跟上主力按成交量提前约 7 天换月的节奏；顶部读数里的原始基差不受影响）。曲线取最近
              <span className="num">250</span> 个交易日
            </li>
            <li>
              · ERP 股债性价比 = 沪深300 盈利收益率（100 ÷ PE-TTM）− 10Y 国债收益率
              （百分点），<span className="text-fg">越高 = 股票相对债券越便宜</span>；
              分位是它在滚动 <span className="num">250</span> 个交易日里的位置
              （&gt;80% 是一年最便宜的一档，&lt;20% 最贵）。PE 来自 iFinD、
              国债收益率来自公开数据，曲线取最近 <span className="num">250</span> 个交易日
            </li>
            <li>
              · NH-NL 新高新低扩散 = 创 <span className="num">60 / 250</span> 日收盘新高家数
              − 新低家数（与宽度温度计同一份原始值）；图上的净值线是差值的
              <span className="text-fg">逐日累计</span>（类 AD 线），虚线是
              <span className="text-fg">上证指数收盘点位</span> —— 指数新高而净值线不新高 =
              <span className="text-fg">顶背离</span>，指数新低而净值线不再下探 = 底背离。
              250 日口径在站内历史不足一年时为空（图上断开，不是 0）
            </li>
            <li>
              · 量能潮 OBV = 上涨日累加成交额、下跌日扣减的
              <span className="text-fg">累计净额</span>（上证指数口径，与恐贪同一基准）——
              指数新高而 OBV 不新高 = <span className="text-fg">量能跟不上</span>；
              蓝线是成交额在滚动 <span className="num">250</span> 个交易日里的分位
              （&gt;80% 是一年量能最大的一档，&lt;20% 最冷清）。曲线取最近
              <span className="num">250</span> 个交易日
            </li>
            <li>
              · ETF 净申赎 = <span className="num">Σ(份额变化 × 当日收盘价)</span>，
              <span className="text-fg">货币现金类整类剔除</span>（场内货基的份额单位与
              覆盖都不可信，申赎本身也不代表方向性资金）；柱是单日净额、金线是它的
              逐日累计，虚线是上证指数收盘点位 —— 指数新高而累计净额不新高 =
              <span className="text-fg">增量资金跟不上</span>。
              净额<span className="text-fg">只在两个覆盖达标的交易日之间</span>算
              （快照日期取自行情源的「最新-交易日」，非交易日货基仍按自然日给净值、
              跨境/QDII 常年滞后一天，这类碎片日不能当基准日）；覆盖不足的日子图上断开，是
              <span className="text-fg">缺数据而不是零进出</span>
            </li>
            <li>
              · 两融杠杆温度 = 沪 + 深「融资余额 + 融券余额」的
              <span className="text-fg">合计余额</span>（只取两所都披露的日子 ——
              深市披露比沪市晚一天，只有一所时相加会把「深市待披露」误报成「深市归零」）；
              金线是余额（万亿刻度）、柱是余额较 <span className="num">20</span> 个观测前的
              变化率（%）。<span className="text-fg">温度 = 变化率的滚动分位（0–100）</span>，
              不是余额水平的分位 —— 余额是长期上行的慢变量，绝对水平的分位几乎恒在 100 附近，
              没有信息量；分位窗口 <span className="num">120</span> 个交易日。曲线取最近
              <span className="num">250</span> 个交易日
            </li>
            <li>
              · 行业宽度 = 每个一级行业里「<span className="text-fg">站上 MA20</span>」的
              个股占比（按同花顺行业路径第一段分 31 个行业；与宽度温度计同一套「站上均线」
              口径，不满 20 根 K 线的票不进分母）——
              <span className="text-fg">多数行业过半 = 行业共振，只有少数行业在上 = 结构性行情</span>。
              这是<span className="text-fg">当日横截面</span>（看谁强谁弱），不是时间序列：
              逐日算一年要好几秒、会把整页卡住
            </li>
            <li>
              · 北向活跃度 = 沪深股通成交额 ÷ 两市成交额（%）——
              <span className="text-fg">外资参与度</span>，越高说明外资交易越活跃。
              用占比而不是成交额绝对值：绝对值随全市场量能潮起潮落。
              <span className="text-fg">⚠️ 只有成交额、没有净流入</span> ——
              2024-08-19 起交易所不再按日披露买卖金额，「北向净流入」在数据源层面
              已经不存在，所以这条线只说「参与得多不多」、<span className="text-fg">不含方向</span>；
              虚线是上证指数收盘点位。通道关闭日没有数据，图上<span className="text-fg">断开</span>
              （是没数据，不是北向没交易）。曲线取最近
              <span className="num">250</span> 个交易日
            </li>
          </ul>
        </div>
      </div>
    </Layout>
  )
}
