import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type {
  FirstBoardBoard,
  FirstBoardRow,
  FirstBoardTrack,
  FirstBoardTrackDay,
  RpsBoard,
  RpsRow,
  RpsTrack,
  RpsTrackDay,
  SectorRpsBoard,
  SectorRpsMemberRow,
  SectorRpsMembers,
  SectorRpsRow,
  SectorStockBoard,
  SectorStockRow,
  SectorStockTrack,
  SectorStockTrackDay,
  SectorTrack,
} from '../api/types'
import Layout from '../components/Layout'
import LoadState from '../components/LoadState'
import Panel from '../components/Panel'
import Segmented from '../components/Segmented'
import SortTh from '../components/SortTh'
import StockLink from '../components/StockLink'
import { fmtAmount, fmtNum, fmtPct, fmtSealTime, toneOf } from '../lib/format'
import { useSort } from '../lib/sort'
import type { SortSpecs } from '../lib/sort'

/**
 * 选股池：首板一进二候选 + RPS 相对强度榜。
 *
 * 两块都是**盘后候选池**，不是买卖指令 —— 页面底部与每块表头都写明这一点。
 * 参数放在页面上而不是写死：这些阈值（首封时间、市值、题材家数）没有「标准答案」，
 * 必须拿自己的库回测过再定；界面只负责让它们可调、可复现。
 */

const BEFORE_ITEMS = [
  { key: '100000', label: '10:00 前' },
  { key: '113000', label: '11:30 前' },
  { key: '140000', label: '14:00 前' },
]

const CAP_ITEMS = [
  { key: '20-300', label: '20~300 亿' },
  { key: '30-150', label: '30~150 亿' },
  { key: '50-500', label: '50~500 亿' },
]

const THEME_ITEMS = [
  { key: '2', label: '题材 ≥2 家' },
  { key: '3', label: '≥3 家' },
  { key: '5', label: '≥5 家' },
]

/** 封单额 / 流通市值。0.02% 那种基本等于没封住，默认不筛只是为了先看清全池。 */
const SEAL_ITEMS = [
  { key: '0', label: '封单不限' },
  { key: '0.3', label: '≥0.3%' },
  { key: '1', label: '≥1%' },
]

/** 真首板：`consecutive == 1` 只说明今天是一板，前 10 日涨停过的票要能剔掉。 */
const TRUE_FIRST_ITEMS = [
  { key: '0', label: '全部首板' },
  { key: '1', label: '只看真首板' },
]

/** 档位：回测里「双高 × 突破」只在近一年有效，5 年长历史是负的 → 默认「全部」。 */
const RPS_MODE_ITEMS = [
  { key: 'all', label: '全部' },
  { key: 'line_red', label: '一线红' },
  { key: 'dual', label: '双高' },
]

const REVERSAL_ITEMS = [
  { key: '0', label: '形态不限' },
  { key: '1', label: '只看突破形态' },
]

const TREND_ITEMS = [
  { key: '0', label: '趋势不限' },
  { key: '1', label: '趋势模板' },
]

/** 板块口径：精选板块（短线）与行业（宽基）**不可混比**。 */
const TAXONOMY_ITEMS = [
  { key: 'kph_selected', label: '精选板块' },
  { key: 'kph_industry', label: '行业' },
]

/** 首板排序：综合分是「题材/封单/首封」，因子分是「反转/低流动/低波动」。 */
const FB_ORDER_ITEMS = [
  { key: 'board', label: '板块综合分' },
  { key: 'factor', label: '量价因子分' },
]

const RPS_ORDER_ITEMS = [
  { key: 'strength', label: '强度排序' },
  { key: 'factor', label: '因子分排序' },
]

/** 首板表显示多少只，「排序」切到因子分时验证汇总也只统计这前 N 只 —— 两侧口径一致。 */
const FB_LIMIT = 50

/** 板块 → 个股：板块档（弱势档是对照，不是推荐）。 */
const STOCK_SCOPE_ITEMS = [
  { key: 'strong', label: '强势板块' },
  { key: 'weak', label: '弱势板块' },
]

const STOCK_ORDER_ITEMS = [
  { key: 'gain', label: '当日涨幅' },
  { key: 'factor', label: '量价因子分' },
]

const STOCK_SECTOR_ITEMS = [
  { key: '1', label: '1 个' },
  { key: '3', label: '3 个' },
  { key: '5', label: '5 个' },
]

const STOCK_PER_ITEMS = [
  { key: '2', label: '2 只' },
  { key: '3', label: '3 只' },
  { key: '5', label: '5 只' },
]

/** 首板候选表的排序口径。文字列从 A 开始，数值列默认先看最大的。 */
const FB_SORTS: SortSpecs<FirstBoardRow> = {
  name: { value: (row) => row.name ?? row.code, first: 'asc' },
  first_seal_time: { value: (row) => row.first_seal_time, first: 'asc' },
  seal_ratio: { value: (row) => row.seal_ratio },
  seal_amount: { value: (row) => row.seal_amount },
  seal_to_amount: { value: (row) => row.seal_to_amount },
  float_mv: { value: (row) => row.float_mv },
  amount: { value: (row) => row.amount },
  turnover: { value: (row) => row.turnover },
  recent_limit_ups: { value: (row) => row.recent_limit_ups },
  factor_score: { value: (row) => row.factor_score },
  theme: { value: (row) => row.theme, first: 'asc' },
  industry: { value: (row) => row.industry, first: 'asc' },
}

/** 首板验证的逐日明细。行本身按日期从新到旧，默认保持后端顺序。 */
const FB_DAY_SORTS: SortSpecs<FirstBoardTrackDay> = {
  trade_date: { value: (row) => row.trade_date, first: 'asc' },
  next_date: { value: (row) => row.next_date, first: 'asc' },
  candidates: { value: (row) => row.candidates },
  // 「待验证」的天没有晋级数，缺数据排最后
  promoted: { value: (row) => (row.status === 'pending' ? null : row.promoted) },
  open_premium_mean: { value: (row) => row.open_premium_mean },
  close_ret_mean: { value: (row) => row.close_ret_mean },
  unbuyable: { value: (row) => row.unbuyable },
  no_quote: { value: (row) => row.no_quote },
}

/** RPS 榜的排序口径。 */
const RPS_SORTS: SortSpecs<RpsRow> = {
  name: { value: (row) => row.name ?? row.code, first: 'asc' },
  close: { value: (row) => row.close },
  pct_chg: { value: (row) => row.pct_chg },
  rps50: { value: (row) => row.rps50 },
  ret50: { value: (row) => row.ret50 },
  rps120: { value: (row) => row.rps120 },
  ret120: { value: (row) => row.ret120 },
  rps250: { value: (row) => row.rps250 },
  ret250: { value: (row) => row.ret250 },
  off_high_pct: { value: (row) => row.off_high_pct },
  factor_score: { value: (row) => row.factor_score },
  hits: { value: (row) => row.hits.join(' · '), first: 'asc' },
  industry: { value: (row) => row.industry, first: 'asc' },
}

const RPS_DAY_SORTS: SortSpecs<RpsTrackDay> = {
  trade_date: { value: (row) => row.trade_date, first: 'asc' },
  picks: { value: (row) => row.picks },
  ret1: { value: (row) => row.ret1 },
  excess1: { value: (row) => row.excess1 },
  ret5: { value: (row) => row.ret5 },
  excess5: { value: (row) => row.excess5 },
}

/** 板块 RPS 榜。 */
const SECTOR_SORTS: SortSpecs<SectorRpsRow> = {
  name: { value: (row) => row.name ?? row.code, first: 'asc' },
  rps20: { value: (row) => row.rps20 },
  ret20: { value: (row) => row.ret20 },
  rps5: { value: (row) => row.rps5 },
  ret5: { value: (row) => row.ret5 },
  member_count: { value: (row) => row.member_count },
  leader_name: { value: (row) => row.leader_name, first: 'asc' },
}

/**
 * 板块成分股表。板块 / 行业是「拼成一串」再比 —— 按第一个题材排，
 * 能把同题材的票排在一起，比按代码排有用。
 */
const MEMBER_SORTS: SortSpecs<SectorRpsMemberRow> = {
  name: { value: (row) => row.name ?? row.code, first: 'asc' },
  sectors: { value: (row) => row.sectors.join(' / '), first: 'asc' },
  industry: { value: (row) => row.industry, first: 'asc' },
  close: { value: (row) => row.close },
  pct_chg: { value: (row) => row.pct_chg },
  amount: { value: (row) => row.amount },
  turnover: { value: (row) => row.turnover },
}

/** 板块 → 个股。 */
const STOCK_SORTS: SortSpecs<SectorStockRow> = {
  sector_name: { value: (row) => row.sector_name ?? row.sector_code, first: 'asc' },
  name: { value: (row) => row.name ?? row.code, first: 'asc' },
  pct_chg: { value: (row) => row.pct_chg },
  sector_pct_chg: { value: (row) => row.sector_pct_chg },
  factor_score: { value: (row) => row.factor_score },
}

const STOCK_DAY_SORTS: SortSpecs<SectorStockTrackDay> = {
  trade_date: { value: (row) => row.trade_date, first: 'asc' },
  picks: { value: (row) => row.picks },
  ret1: { value: (row) => row.ret1 },
  excess1: { value: (row) => row.excess1 },
}

/** 各表的空数组常量：`useSort` 必须在组件顶层无条件调用，数据没到时得有东西可传。 */
const NO_FB_ROWS: FirstBoardRow[] = []
const NO_FB_DAYS: FirstBoardTrackDay[] = []
const NO_RPS_ROWS: RpsRow[] = []
const NO_RPS_DAYS: RpsTrackDay[] = []
const NO_SECTOR_ROWS: SectorRpsRow[] = []
const NO_MEMBER_ROWS: SectorRpsMemberRow[] = []
const NO_STOCK_ROWS: SectorStockRow[] = []
const NO_STOCK_DAYS: SectorStockTrackDay[] = []

/** RPS 分位的颜色：只有两档极端才染色，中间一律中性（避免满屏红绿）。 */
function rpsTone(value: number | null): string {
  if (value == null) return 'text-fg-muted'
  if (value >= 90) return 'text-up'
  if (value <= 10) return 'text-down'
  return 'text-fg'
}

export default function Pools() {
  const [before, setBefore] = useState('140000')
  const [cap, setCap] = useState('20-300')
  const [themes, setThemes] = useState('3')
  const [seal, setSeal] = useState('0')
  const [trueFirst, setTrueFirst] = useState('0')
  const [fbOrder, setFbOrder] = useState('board')
  const [rpsMode, setRpsMode] = useState('all')
  const [onlyBreakout, setOnlyBreakout] = useState('0')
  const [onlyTrend, setOnlyTrend] = useState('0')
  const [rpsOrder, setRpsOrder] = useState('strength')

  const [firstBoard, setFirstBoard] = useState<FirstBoardBoard | null>(null)
  const [firstBoardError, setFirstBoardError] = useState<string | null>(null)
  const [rps, setRps] = useState<RpsBoard | null>(null)
  const [rpsError, setRpsError] = useState<string | null>(null)
  // 次日验证：参数与上面的候选完全一致，所以「改条件 → 汇总立刻变」
  const [fbTrack, setFbTrack] = useState<FirstBoardTrack | null>(null)
  const [fbTrackOpen, setFbTrackOpen] = useState(false)
  const [rpsTrack, setRpsTrack] = useState<RpsTrack | null>(null)
  const [rpsTrackFactor, setRpsTrackFactor] = useState<RpsTrack | null>(null)
  const [rpsTrackOpen, setRpsTrackOpen] = useState(false)
  const [taxonomy, setTaxonomy] = useState('kph_selected')
  const [sectorBoard, setSectorBoard] = useState<SectorRpsBoard | null>(null)
  const [sectorTrack, setSectorTrack] = useState<SectorTrack | null>(null)
  const [sectorError, setSectorError] = useState<string | null>(null)
  // 板块 → 个股（研究视图）：历史超额为负，页面上必须如实显示
  const [stockScope, setStockScope] = useState('strong')
  const [stockOrder, setStockOrder] = useState('gain')
  const [stockSectors, setStockSectors] = useState('3')
  const [stockPer, setStockPer] = useState('3')
  const [sectorStock, setSectorStock] = useState<SectorStockBoard | null>(null)
  const [sectorStockError, setSectorStockError] = useState<string | null>(null)
  const [sectorStockTrack, setSectorStockTrack] = useState<SectorStockTrack | null>(null)
  const [sectorStockOpen, setSectorStockOpen] = useState(false)
  // 点开板块 RPS 里的板块名 → 看该板块的成分股
  const [memberSector, setMemberSector] = useState<{ code: string; name: string | null } | null>(
    null,
  )
  const [memberBoard, setMemberBoard] = useState<SectorRpsMembers | null>(null)
  const [memberError, setMemberError] = useState<string | null>(null)

  // 成分股**不传页面所选日期**：`sector_member` 只有快照（22:00 预取 + 打开时现取），
  // 没有历史回补，传一个旧日期只会拿到空表。后端回落到最近一次快照并把实际日期带回来，
  // 页面上写清「这是哪一天的名单」。
  useEffect(() => {
    if (!memberSector) {
      setMemberBoard(null)
      setMemberError(null)
      return
    }
    let stale = false
    setMemberBoard(null)
    setMemberError(null)
    api
      .sectorRpsMembers(memberSector.code)
      .then((value) => {
        if (!stale) setMemberBoard(value)
      })
      .catch((err: Error) => {
        if (!stale) setMemberError(err.message)
      })
    return () => {
      stale = true
    }
  }, [memberSector])

  useEffect(() => {
    let stale = false
    setFirstBoard(null)
    setFirstBoardError(null)
    const [minFloatMv, maxFloatMv] = cap.split('-').map(Number)
    api
      .firstBoard({
        before,
        minFloatMv,
        maxFloatMv,
        minThemes: Number(themes),
        minSealRatio: Number(seal),
        onlyTrueFirstBoard: trueFirst === '1',
        order: fbOrder as 'board' | 'factor',
        limit: FB_LIMIT,
      })
      .then((value) => {
        if (!stale) setFirstBoard(value)
      })
      .catch((err: Error) => {
        if (!stale) setFirstBoardError(err.message)
      })
    return () => {
      stale = true
    }
  }, [before, cap, themes, seal, trueFirst, fbOrder])

  useEffect(() => {
    let stale = false
    setRps(null)
    setRpsError(null)
    api
      .rpsBoard({
        mode: rpsMode as 'dual' | 'line_red' | 'all',
        limit: 100,
        breakoutOnly: onlyBreakout === '1',
        trendTemplate: onlyTrend === '1',
        order: rpsOrder as 'strength' | 'factor',
      })
      .then((value) => {
        if (!stale) setRps(value)
      })
      .catch((err: Error) => {
        if (!stale) setRpsError(err.message)
      })
    return () => {
      stale = true
    }
  }, [rpsMode, onlyBreakout, onlyTrend, rpsOrder])

  useEffect(() => {
    let stale = false
    setFbTrack(null)
    const [minFloatMv, maxFloatMv] = cap.split('-').map(Number)
    api
      .firstBoardTrack({
        before,
        minFloatMv,
        maxFloatMv,
        minThemes: Number(themes),
        minSealRatio: Number(seal),
        onlyTrueFirstBoard: trueFirst === '1',
        days: 20,
        order: fbOrder as 'board' | 'factor',
        top: FB_LIMIT,
      })
      .then((value) => {
        if (!stale) setFbTrack(value)
      })
      .catch(() => {
        // 验证行只是辅助信息，取不到就整行不显示，不弹错
        if (!stale) setFbTrack(null)
      })
    return () => {
      stale = true
    }
  }, [before, cap, themes, seal, trueFirst, fbOrder])

  useEffect(() => {
    let stale = false
    setRpsTrack(null)
    setRpsTrackFactor(null)
    api
      .rpsTrack({
        mode: rpsMode as 'dual' | 'line_red' | 'all',
        top: 50,
        days: 20,
        breakoutOnly: onlyBreakout === '1',
        trendTemplate: onlyTrend === '1',
        order: 'strength',
      })
      .then((value) => {
        if (!stale) setRpsTrack(value)
      })
      .catch(() => {
        if (!stale) setRpsTrack(null)
      })
    return () => {
      stale = true
    }
  }, [rpsMode, onlyBreakout, onlyTrend])

  // 因子档的验证要跑「近 20 天 × 全池」打分（首次约 19 秒），所以**切到因子分排序时才请求**，
  // 不要页面一打开就发 —— 默认档位用不到它。
  const wantFactorTrack = rpsOrder === 'factor'
  useEffect(() => {
    if (!wantFactorTrack) return
    let stale = false
    api
      .rpsTrack({
        mode: rpsMode as 'dual' | 'line_red' | 'all',
        top: 50,
        days: 20,
        breakoutOnly: onlyBreakout === '1',
        trendTemplate: onlyTrend === '1',
        order: 'factor',
      })
      .then((value) => {
        if (!stale) setRpsTrackFactor(value)
      })
      .catch(() => {
        if (!stale) setRpsTrackFactor(null)
      })
    return () => {
      stale = true
    }
  }, [rpsMode, onlyBreakout, onlyTrend, wantFactorTrack])

  useEffect(() => {
    let stale = false
    setSectorBoard(null)
    setSectorTrack(null)
    setSectorError(null)
    const kind = taxonomy as 'kph_selected' | 'kph_industry'
    api
      .sectorRps(kind, 30)
      .then((value) => {
        if (!stale) setSectorBoard(value)
      })
      .catch((err: Error) => {
        if (!stale) setSectorError(err.message)
      })
    api
      .sectorRpsTrack(kind, 5, 20)
      .then((value) => {
        if (!stale) setSectorTrack(value)
      })
      .catch(() => {
        if (!stale) setSectorTrack(null)
      })
    return () => {
      stale = true
    }
  }, [taxonomy])

  useEffect(() => {
    let stale = false
    setSectorStock(null)
    setSectorStockError(null)
    api
      .sectorStock({
        taxonomy: taxonomy as 'kph_selected' | 'kph_industry',
        scope: stockScope as 'strong' | 'weak',
        order: stockOrder as 'gain' | 'factor',
        sectors: Number(stockSectors),
        perSector: Number(stockPer),
      })
      .then((value) => {
        if (!stale) setSectorStock(value)
      })
      .catch((err: Error) => {
        if (!stale) setSectorStockError(err.message)
      })
    return () => {
      stale = true
    }
  }, [taxonomy, stockScope, stockOrder, stockSectors, stockPer])

  useEffect(() => {
    let stale = false
    setSectorStockTrack(null)
    api
      .sectorStockTrack({
        taxonomy: taxonomy as 'kph_selected' | 'kph_industry',
        scope: stockScope as 'strong' | 'weak',
        order: stockOrder as 'gain' | 'factor',
        sectors: Number(stockSectors),
        perSector: Number(stockPer),
        days: 20,
      })
      .then((value) => {
        if (!stale) setSectorStockTrack(value)
      })
      .catch(() => {
        // 验证行只是辅助信息，取不到就整行不显示
        if (!stale) setSectorStockTrack(null)
      })
    return () => {
      stale = true
    }
  }, [taxonomy, stockScope, stockOrder, stockSectors, stockPer])

  const tradeDate = firstBoard?.trade_date ?? rps?.trade_date ?? null
  // 逐日明细只展开当前选中档 —— 两档并排看汇总，明细看一份就够了
  const activeRpsTrack = rpsOrder === 'factor' ? rpsTrackFactor : rpsTrack

  // 排序状态：hooks 必须在顶层无条件调用，所以数据没到时传空数组常量。
  // 只有成分股表默认按涨幅排（后端本来就是这个顺序，列头能指出当前排的是哪一列），
  // 其余表首屏保持后端顺序 —— 那个顺序本身有意义（按综合分 / 首封时间 / 日期）。
  const [fbSort, fbRows] = useSort(firstBoard?.rows ?? NO_FB_ROWS, FB_SORTS, { key: null })
  const [fbDaySort, fbDayRows] = useSort(fbTrack?.days ?? NO_FB_DAYS, FB_DAY_SORTS, {
    key: null,
  })
  const [rpsSort, rpsRows] = useSort(rps?.rows ?? NO_RPS_ROWS, RPS_SORTS, { key: null })
  const [rpsDaySort, rpsDayRows] = useSort(activeRpsTrack?.rows ?? NO_RPS_DAYS, RPS_DAY_SORTS, {
    key: null,
  })
  const [sectorSort, sectorRows] = useSort(sectorBoard?.rows ?? NO_SECTOR_ROWS, SECTOR_SORTS, {
    key: null,
  })
  const [memberSort, memberRows] = useSort(memberBoard?.rows ?? NO_MEMBER_ROWS, MEMBER_SORTS, {
    key: 'pct_chg',
  })
  const [stockSort, stockRows] = useSort(sectorStock?.rows ?? NO_STOCK_ROWS, STOCK_SORTS, {
    key: null,
  })
  const [stockDaySort, stockDayRows] = useSort(
    sectorStockTrack?.rows ?? NO_STOCK_DAYS,
    STOCK_DAY_SORTS,
    { key: null },
  )

  return (
    <Layout
      toolbar={
        <span className="num hidden text-[13px] text-fg-dim lg:inline">
          {tradeDate ? `${tradeDate} · 盘后数据` : '加载中…'}
        </span>
      }
    >
      <div className="space-y-4">
        <Panel
          title="首板一进二候选"
          meta={
            <span className="num">
              首板 · 未炸板 ·{' '}
              {firstBoard?.conditions.before
                ? fmtSealTime(String(firstBoard.conditions.before))
                : '14:00:00'}{' '}
              前封板
            </span>
          }
          delay={40}
        >
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2 border-b border-line-soft px-4 py-2.5 text-[12px] text-fg-dim">
            <span className="flex items-center gap-2">
              首封
              <Segmented value={before} items={BEFORE_ITEMS} onChange={setBefore} />
            </span>
            <span className="flex items-center gap-2">
              流通市值
              <Segmented value={cap} items={CAP_ITEMS} onChange={setCap} />
            </span>
            <span className="flex items-center gap-2">
              题材
              <Segmented value={themes} items={THEME_ITEMS} onChange={setThemes} />
            </span>
            <span className="flex items-center gap-2">
              封单/流通
              <Segmented value={seal} items={SEAL_ITEMS} onChange={setSeal} />
            </span>
            <span className="flex items-center gap-2">
              <Segmented value={trueFirst} items={TRUE_FIRST_ITEMS} onChange={setTrueFirst} />
            </span>
            <span className="flex items-center gap-2">
              排序
              <Segmented value={fbOrder} items={FB_ORDER_ITEMS} onChange={setFbOrder} />
            </span>
            <span>成交额 ≥3 亿 · 已剔除 ST 与北交所</span>
          </div>

          {fbTrack && (
            <div className="border-b border-line-soft px-4 py-2 text-[12px] text-fg-dim">
              <button
                type="button"
                onClick={() => setFbTrackOpen((open) => !open)}
                className="w-full text-left leading-relaxed hover:text-fg"
              >
                <span className="num">
                  近 {fbTrack.verified_days} 日按当前条件
                  {`（${fbOrder === 'factor' ? '量价因子分' : '板块综合分'}前 ${FB_LIMIT} 只）`}
                  ：样本 {fbTrack.samples}
                  {' · '}晋级 {fmtNum(fbTrack.promoted_rate, 1, '%')}
                  {' · '}开盘溢价中位 {fmtPct(fbTrack.open_premium_median)}
                  {' · '}开盘买入收盘中位 {fmtPct(fbTrack.close_ret_median)}
                  {' · '}大面 {fmtNum(fbTrack.big_loss_rate, 1, '%')}
                  {' · '}一字买不进 {fmtNum(fbTrack.unbuyable_rate, 1, '%')}
                </span>
                {fbTrack.pending_days > 0 && (
                  <span className="ml-1">（{fbTrack.pending_days} 天待验证）</span>
                )}
                <span className="ml-2 text-fg-muted">
                  {fbTrackOpen ? '收起 ▴' : '逐日明细 ▾'}
                </span>
              </button>
              {fbTrackOpen && (
                <div className="mt-2 overflow-x-auto">
                  <table className="grid-table">
                    <thead>
                      <tr>
                        <SortTh sortKey="trade_date" align="left" {...fbDaySort}>
                          信号日
                        </SortTh>
                        <SortTh sortKey="next_date" align="left" {...fbDaySort}>
                          次日
                        </SortTh>
                        <SortTh sortKey="candidates" {...fbDaySort}>
                          候选
                        </SortTh>
                        <SortTh sortKey="promoted" {...fbDaySort}>
                          晋级
                        </SortTh>
                        <SortTh sortKey="open_premium_mean" {...fbDaySort}>
                          开盘溢价
                        </SortTh>
                        {/* 买入价 = T+1 开盘（2026-10-28 拍板），所以这一列是开盘→收盘 */}
                        <SortTh
                          sortKey="close_ret_mean"
                          title="T+1 开盘买入到收盘的收益"
                          {...fbDaySort}
                        >
                          收盘
                        </SortTh>
                        <SortTh sortKey="unbuyable" {...fbDaySort}>
                          一字
                        </SortTh>
                        <SortTh sortKey="no_quote" {...fbDaySort}>
                          缺日线
                        </SortTh>
                      </tr>
                    </thead>
                    <tbody>
                      {fbDayRows.map((row) => (
                        <tr key={row.trade_date}>
                          <td className="num !text-left">{row.trade_date}</td>
                          <td className="num !text-left text-fg-muted">
                            {row.next_date ?? '—'}
                          </td>
                          <td className="num">{row.candidates}</td>
                          <td className="num">
                            {row.status === 'pending' ? '待验证' : row.promoted}
                          </td>
                          <td className={`num ${toneOf(row.open_premium_mean)}`}>
                            {fmtPct(row.open_premium_mean)}
                          </td>
                          <td className={`num ${toneOf(row.close_ret_mean)}`}>
                            {fmtPct(row.close_ret_mean)}
                          </td>
                          <td className="num">{row.unbuyable}</td>
                          <td className="num">{row.no_quote}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {firstBoardError ? (
            <LoadState loading={false} error={firstBoardError} />
          ) : firstBoard === null ? (
            <LoadState loading emptyText="加载中" />
          ) : firstBoard.rows.length === 0 ? (
            <LoadState loading={false} emptyText="当前条件下没有候选 —— 可以放宽首封时间或市值区间" />
          ) : (
            <div className="overflow-x-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <SortTh sortKey="name" align="left" {...fbSort}>
                      名称
                    </SortTh>
                    <SortTh sortKey="first_seal_time" {...fbSort}>
                      首封
                    </SortTh>
                    <SortTh sortKey="seal_ratio" {...fbSort}>
                      封单/流通
                    </SortTh>
                    <SortTh sortKey="seal_amount" {...fbSort}>
                      封单额
                    </SortTh>
                    <SortTh sortKey="seal_to_amount" {...fbSort}>
                      封成比
                    </SortTh>
                    <SortTh sortKey="float_mv" {...fbSort}>
                      流通市值
                    </SortTh>
                    <SortTh sortKey="amount" {...fbSort}>
                      成交额
                    </SortTh>
                    <SortTh sortKey="turnover" {...fbSort}>
                      换手
                    </SortTh>
                    <SortTh sortKey="recent_limit_ups" {...fbSort}>
                      近10日涨停
                    </SortTh>
                    {fbOrder === 'factor' && (
                      <SortTh sortKey="factor_score" {...fbSort}>
                        因子分
                      </SortTh>
                    )}
                    <SortTh sortKey="theme" align="left" {...fbSort}>
                      题材（当日涨停家数 / 题材内首封排名）
                    </SortTh>
                    <SortTh sortKey="industry" align="left" {...fbSort}>
                      行业
                    </SortTh>
                  </tr>
                </thead>
                <tbody>
                  {fbRows.map((row) => (
                    <tr key={row.code}>
                      <td className="!text-left">
                        <StockLink code={row.code} className="text-fg">
                          {row.name ?? row.code}
                        </StockLink>
                        {row.board_shape && (
                          <span
                            className="ml-1 rounded border border-line-soft px-1 text-[11px] text-fg-dim"
                            title={`当天${row.board_shape}板`}
                          >
                            {row.board_shape === '一字' ? '一' : 'T'}
                          </span>
                        )}
                        <span className="num ml-2 text-[12px] text-fg-dim">{row.code}</span>
                      </td>
                      <td className="num">{fmtSealTime(row.first_seal_time)}</td>
                      <td className={`num ${toneOf(row.seal_ratio)}`}>
                        {fmtNum(row.seal_ratio, 2, '%')}
                      </td>
                      <td className="num">{fmtAmount(row.seal_amount)}</td>
                      <td className="num">{fmtNum(row.seal_to_amount, 1, '%')}</td>
                      <td className="num">{fmtAmount(row.float_mv)}</td>
                      <td className="num">{fmtAmount(row.amount)}</td>
                      <td className="num">{fmtNum(row.turnover, 2, '%')}</td>
                      {/* `0` 是**有意义的值**（0 = 近 10 日没有涨停 = 真首板），
                          不能用 `||` 把它显示成「—」—— 勾「只看真首板」后整列都会变「—」
                          （2026-10-28 体检） */}
                      <td className={`num ${row.recent_limit_ups > 0 ? 'text-fg-muted' : ''}`}>
                        {row.recent_limit_ups ?? '—'}
                      </td>
                      {fbOrder === 'factor' && (
                        <td className="num">{fmtNum(row.factor_score, 1)}</td>
                      )}
                      <td
                        className="!text-left"
                        title={
                          row.theme2
                            ? `第二题材：${row.theme2}（${row.theme2_count} 家）`
                            : undefined
                        }
                      >
                        {row.theme ? (
                          <>
                            <span className="text-fg">{row.theme}</span>
                            <span
                              className={`num ml-1 ${
                                (row.theme_rank ?? 99) <= 3
                                  ? 'font-semibold text-fg'
                                  : 'text-fg-muted'
                              }`}
                            >
                              {row.theme_rank ?? '—'}/{row.theme_count}
                            </span>
                          </>
                        ) : (
                          <span className="text-fg-dim">无精选板块</span>
                        )}
                      </td>
                      <td className="!text-left text-fg-muted">{row.industry ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel
          title="RPS 相对强度榜"
          meta={
            <span className="flex items-center gap-3">
              <Segmented value={rpsMode} items={RPS_MODE_ITEMS} onChange={setRpsMode} />
              <span className="num">{rps?.trade_date ?? '—'}</span>
            </span>
          }
          delay={60}
        >
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2 border-b border-line-soft px-4 py-2.5 text-[12px] text-fg-dim">
            <span className="flex items-center gap-2">
              <Segmented
                value={onlyBreakout}
                items={REVERSAL_ITEMS}
                onChange={setOnlyBreakout}
              />
            </span>
            <span className="flex items-center gap-2">
              <Segmented value={onlyTrend} items={TREND_ITEMS} onChange={setOnlyTrend} />
            </span>
            <span className="flex items-center gap-2">
              排序
              <Segmented value={rpsOrder} items={RPS_ORDER_ITEMS} onChange={setRpsOrder} />
            </span>
            {rps && rps.industry_top.length > 0 && (
              <span className="num">
                强势行业：
                {rps.industry_top
                  .map((item) => `${item.industry} ${item.count}`)
                  .join(' · ')}
              </span>
            )}
          </div>
          <p className="border-b border-line-soft px-4 py-2 text-[12px] leading-relaxed text-fg-dim">
            RPS = 区间涨幅在全市场的百分位（0~100，中位秩）。**RPS 单独不是买点**：
            「双高 × 突破形态」只在近一年样本有效（10 日超额 +3~4%），5 年长历史全部为负，
            所以默认按 RPS250 排、双高只当参考视图；回踩类形态别叠 RPS。
          </p>
          {(rpsTrack || rpsTrackFactor) && (
            <div className="border-b border-line-soft px-4 py-2 text-[12px] text-fg-dim">
              <button
                type="button"
                onClick={() => setRpsTrackOpen((open) => !open)}
                className="w-full text-left leading-relaxed hover:text-fg"
              >
                {(
                  [
                    { key: 'strength', label: '强度排序', track: rpsTrack },
                    { key: 'factor', label: '因子分排序', track: rpsTrackFactor },
                  ] as const
                ).map(({ key, label, track }) =>
                  track === null ? null : (
                    <span key={key} className="block">
                      <span className={rpsOrder === key ? 'text-fg' : ''}>
                        {label}前 50 只{rpsOrder === key ? '（当前）' : ''}
                      </span>
                      <span className="num ml-2">
                        近 {track.days} 日：1 日 {fmtPct(track.ret1.mean)}（超额{' '}
                        {fmtPct(track.ret1.excess)}）{' · '}5 日 {fmtPct(track.ret5.mean)}
                        （超额 {fmtPct(track.ret5.excess)}）{' · '}样本 {track.samples}
                      </span>
                      {track.ret1.days < 5 && (
                        <span className="ml-2">样本不足 5 天</span>
                      )}
                    </span>
                  ),
                )}
                <span className="ml-1 text-fg-muted">
                  {rpsTrackOpen ? '收起 ▴' : '逐日明细 ▾'}
                </span>
              </button>
              {rpsTrackOpen && activeRpsTrack && (
                <div className="mt-2 overflow-x-auto">
                  <table className="grid-table">
                    <thead>
                      <tr>
                        <SortTh sortKey="trade_date" align="left" {...rpsDaySort}>
                          日期
                        </SortTh>
                        <SortTh sortKey="picks" {...rpsDaySort}>
                          只数
                        </SortTh>
                        <SortTh sortKey="ret1" {...rpsDaySort}>
                          1 日
                        </SortTh>
                        <SortTh sortKey="excess1" {...rpsDaySort}>
                          超额
                        </SortTh>
                        <SortTh sortKey="ret5" {...rpsDaySort}>
                          5 日
                        </SortTh>
                        <SortTh sortKey="excess5" {...rpsDaySort}>
                          超额
                        </SortTh>
                        <SortTh align="left">状态</SortTh>
                      </tr>
                    </thead>
                    <tbody>
                      {rpsDayRows.map((row) => (
                        <tr key={row.trade_date}>
                          <td className="num !text-left">{row.trade_date}</td>
                          <td className="num">{row.picks}</td>
                          <td className={`num ${toneOf(row.ret1)}`}>{fmtPct(row.ret1)}</td>
                          <td className={`num ${toneOf(row.excess1)}`}>{fmtPct(row.excess1)}</td>
                          <td className={`num ${toneOf(row.ret5)}`}>{fmtPct(row.ret5)}</td>
                          <td className={`num ${toneOf(row.excess5)}`}>{fmtPct(row.excess5)}</td>
                          <td className="!text-left text-fg-muted">
                            {row.status === 'pending' ? '待验证' : '已验证'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
          {rpsError ? (
            <LoadState loading={false} error={rpsError} />
          ) : rps === null ? (
            <LoadState loading emptyText="加载中" />
          ) : rps.rows.length === 0 ? (
            <LoadState
              loading={false}
              emptyText="当前档位没有候选 —— 标签组合可能太严（「双高」还需要 RPS50 数据）"
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <th>#</th>
                    <SortTh sortKey="name" align="left" {...rpsSort}>
                      名称
                    </SortTh>
                    <SortTh sortKey="close" {...rpsSort}>
                      收盘
                    </SortTh>
                    <SortTh sortKey="pct_chg" {...rpsSort}>
                      当日
                    </SortTh>
                    <SortTh sortKey="rps50" {...rpsSort}>
                      RPS50
                    </SortTh>
                    <SortTh sortKey="ret50" {...rpsSort}>
                      50 日
                    </SortTh>
                    <SortTh sortKey="rps120" {...rpsSort}>
                      RPS120
                    </SortTh>
                    <SortTh sortKey="ret120" {...rpsSort}>
                      120 日
                    </SortTh>
                    <SortTh sortKey="rps250" {...rpsSort}>
                      RPS250
                    </SortTh>
                    <SortTh sortKey="ret250" {...rpsSort}>
                      250 日
                    </SortTh>
                    <SortTh sortKey="off_high_pct" {...rpsSort}>
                      距新高
                    </SortTh>
                    {rpsOrder === 'factor' && (
                      <SortTh sortKey="factor_score" {...rpsSort}>
                        因子分
                      </SortTh>
                    )}
                    <SortTh sortKey="hits" align="left" {...rpsSort}>
                      今日形态 / 标签
                    </SortTh>
                    <SortTh sortKey="industry" align="left" {...rpsSort}>
                      行业
                    </SortTh>
                  </tr>
                </thead>
                <tbody>
                  {rpsRows.map((row, index) => (
                    <tr key={row.code}>
                      <td className="num text-fg-dim">{index + 1}</td>
                      <td className="!text-left">
                        <StockLink code={row.code} className="text-fg">
                          {row.name ?? row.code}
                        </StockLink>
                        <span className="num ml-2 text-[12px] text-fg-dim">{row.code}</span>
                      </td>
                      <td className="num">{fmtNum(row.close, 2)}</td>
                      <td className={`num ${toneOf(row.pct_chg)}`}>{fmtPct(row.pct_chg)}</td>
                      <td className={`num ${rpsTone(row.rps50)}`}>{fmtNum(row.rps50, 1)}</td>
                      <td className={`num ${toneOf(row.ret50)}`}>{fmtPct(row.ret50)}</td>
                      <td className={`num ${rpsTone(row.rps120)}`}>{fmtNum(row.rps120, 1)}</td>
                      <td className={`num ${toneOf(row.ret120)}`}>{fmtPct(row.ret120)}</td>
                      <td className={`num ${rpsTone(row.rps250)}`}>{fmtNum(row.rps250, 1)}</td>
                      <td className={`num ${toneOf(row.ret250)}`}>{fmtPct(row.ret250)}</td>
                      <td className={`num ${toneOf(row.off_high_pct)}`}>
                        {fmtPct(row.off_high_pct)}
                      </td>
                      {rpsOrder === 'factor' && (
                        <td className="num">{fmtNum(row.factor_score, 1)}</td>
                      )}
                      <td className="!text-left">
                        {row.hits.length > 0 && (
                          <span className="mr-2 rounded border border-line-soft px-1 text-[11px] text-fg">
                            {row.hits.join(' · ')}
                          </span>
                        )}
                        {row.tags.map((tag) => (
                          <span
                            key={tag}
                            className={`num mr-1 text-[11px] ${
                              tag === '掉队' ? 'text-down' : 'text-up'
                            }`}
                          >
                            {tag}
                          </span>
                        ))}
                        {row.hits.length === 0 && row.tags.length === 0 && (
                          <span className="text-fg-dim">—</span>
                        )}
                      </td>
                      <td className="!text-left text-fg-muted">{row.industry ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel
          title="板块 RPS（20 日强度分位）"
          meta={
            <span className="flex items-center gap-3">
              <Segmented value={taxonomy} items={TAXONOMY_ITEMS} onChange={setTaxonomy} />
              <span className="num">{sectorBoard?.trade_date ?? '—'}</span>
            </span>
          }
          delay={80}
        >
          <p className="border-b border-line-soft px-4 py-2 text-[12px] leading-relaxed text-fg-dim">
            板块指数自身涨幅的横截面分位（0~100）。只用板块日线算，不拼成分 ——
            `sector_member` 只有当天快照，拿它回看历史是前视偏差。两个口径不能混比。
          </p>
          {sectorTrack && (
            <div className="border-b border-line-soft px-4 py-2 text-[12px] text-fg-dim">
              <span className="num">
                近 {sectorTrack.verified_days} 日最强 {sectorTrack.top} 个板块：5 日{' '}
                {fmtPct(sectorTrack.ret5.mean)}（超额 {fmtPct(sectorTrack.ret5.excess)}）
                {' · '}20 日 {fmtPct(sectorTrack.ret20.mean)}（超额{' '}
                {fmtPct(sectorTrack.ret20.excess)}）
              </span>
              <span className="ml-2">收益是板块指数口径，不是个股</span>
            </div>
          )}
          {sectorError ? (
            <LoadState loading={false} error={sectorError} />
          ) : sectorBoard === null ? (
            <LoadState loading emptyText="加载中" />
          ) : sectorBoard.rows.length === 0 ? (
            <LoadState loading={false} emptyText="还没有板块日线数据" />
          ) : (
            <>
              {memberSector && (
                <div className="border-b border-line">
                  <div className="flex items-center justify-between px-1 py-2 text-[12px] text-fg-muted">
                    <span>
                      {memberSector.name ?? memberSector.code} 的成分股
                      {memberBoard?.trade_date
                        ? `（快照日 ${memberBoard.trade_date}，共 ${memberBoard.rows.length} 只）`
                        : ''}
                    </span>
                    <button
                      type="button"
                      onClick={() => setMemberSector(null)}
                      className="text-accent hover:underline"
                    >
                      收起
                    </button>
                  </div>
                  {memberError ? (
                    <div className="px-1 pb-3 text-[12px] text-danger">
                      成分股取数失败：{memberError}
                    </div>
                  ) : memberBoard === null ? (
                    <div className="px-1 pb-3 text-[12px] text-fg-dim">加载中…</div>
                  ) : memberBoard.rows.length === 0 ? (
                    <div className="px-1 pb-3 text-[12px] text-fg-dim">
                      这个板块还没有成分快照（开盘红当天未发布，或该板块当天没有名单）
                    </div>
                  ) : (
                    <div className="max-h-[320px] overflow-y-auto">
                      <table className="grid-table">
                        <thead>
                          <tr>
                            <SortTh sortKey="name" align="left" {...memberSort}>
                              股票
                            </SortTh>
                            <SortTh sortKey="sectors" align="left" {...memberSort}>
                              板块
                            </SortTh>
                            <SortTh sortKey="industry" align="left" {...memberSort}>
                              行业
                            </SortTh>
                            <SortTh sortKey="close" {...memberSort}>
                              收盘
                            </SortTh>
                            <SortTh sortKey="pct_chg" {...memberSort}>
                              涨幅
                            </SortTh>
                            <SortTh sortKey="amount" {...memberSort}>
                              成交额
                            </SortTh>
                            <SortTh sortKey="turnover" {...memberSort}>
                              换手
                            </SortTh>
                          </tr>
                        </thead>
                        <tbody>
                          {memberRows.map((item) => (
                            <tr key={item.code}>
                              <td className="!text-left">
                                <StockLink code={item.code}>{item.name ?? item.code}</StockLink>
                                <div className="num text-[11px] text-fg-dim">{item.code}</div>
                              </td>
                              <td className="!text-left text-[12px] text-fg-dim">
                                {item.sectors.length === 0 ? '—' : item.sectors.join(' / ')}
                              </td>
                              <td className="!text-left text-[12px] text-fg-dim">
                                {item.industry ? (
                                  <span title={item.industry}>{item.industry.split('-')[0]}</span>
                                ) : (
                                  '—'
                                )}
                              </td>
                              <td className="num">
                                {item.close == null ? '—' : fmtNum(item.close, 2)}
                              </td>
                              <td className={`num ${toneOf(item.pct_chg)}`}>
                                {fmtPct(item.pct_chg)}
                              </td>
                              <td className="num">
                                {item.amount == null ? '—' : fmtAmount(item.amount)}
                              </td>
                              <td className="num">
                                {item.turnover == null ? '—' : fmtPct(item.turnover)}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              )}
              <div className="overflow-x-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <th>#</th>
                    <SortTh sortKey="name" align="left" {...sectorSort}>
                      板块
                    </SortTh>
                    <SortTh sortKey="rps20" {...sectorSort}>
                      RPS20
                    </SortTh>
                    <SortTh sortKey="ret20" {...sectorSort}>
                      20 日
                    </SortTh>
                    <SortTh sortKey="rps5" {...sectorSort}>
                      RPS5
                    </SortTh>
                    <SortTh sortKey="ret5" {...sectorSort}>
                      5 日
                    </SortTh>
                    <SortTh sortKey="member_count" {...sectorSort}>
                      成分
                    </SortTh>
                    <SortTh
                      sortKey="leader_name"
                      align="left"
                      title="开盘红当日成分名单 22:00 后才发布，「龙一」由那趟预取顺带写入 —— 白天看当天通常是「—」，切到更早的日期才有值"
                      {...sectorSort}
                    >
                      龙一
                    </SortTh>
                  </tr>
                </thead>
                <tbody>
                  {sectorRows.map((row, index) => (
                    <tr key={row.code}>
                      <td className="num text-fg-dim">{index + 1}</td>
                      <td className="!text-left">
                        <button
                          type="button"
                          onClick={() =>
                            setMemberSector((prev) =>
                              prev?.code === row.code
                                ? null
                                : { code: row.code, name: row.name },
                            )
                          }
                          className={`text-left hover:underline ${
                            memberSector?.code === row.code ? 'text-accent' : 'text-fg'
                          }`}
                          title="点开看这个板块的成分股"
                        >
                          {row.name ?? row.code}
                        </button>
                        <span className="num ml-2 text-[12px] text-fg-dim">{row.code}</span>
                      </td>
                      <td className={`num ${rpsTone(row.rps20)}`}>{fmtNum(row.rps20, 1)}</td>
                      <td className={`num ${toneOf(row.ret20)}`}>{fmtPct(row.ret20)}</td>
                      <td className={`num ${rpsTone(row.rps5)}`}>{fmtNum(row.rps5, 1)}</td>
                      <td className={`num ${toneOf(row.ret5)}`}>{fmtPct(row.ret5)}</td>
                      <td className="num">{row.member_count ?? '—'}</td>
                      <td className="!text-left text-fg-muted">{row.leader_name ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            </>
          )}
        </Panel>

        <Panel
          title="板块 → 个股（研究视图 · 历史负超额）"
          meta={
            <span className="flex items-center gap-3">
              <Segmented
                value={stockScope}
                items={STOCK_SCOPE_ITEMS}
                onChange={setStockScope}
              />
              <span className="num">{sectorStock?.trade_date ?? '—'}</span>
            </span>
          }
          delay={100}
        >
          <p className="border-b border-line-soft px-4 py-2 text-[12px] leading-relaxed text-fg-dim">
            {stockScope === 'strong' ? 'RPS20 ≥ 90' : 'RPS20 ≤ 10'} 的前 {stockSectors} 个板块，
            每个板块按{stockOrder === 'gain' ? '当日涨幅' : '量价因子分'}取前 {stockPer} 只
            （跨板块去重，已剔除 ST 与北交所）。
            <span className="text-down"> 历史超额为负</span>：IC −0.046、强势档 −0.17%、
            强势×突破 −0.30%（全市场突破 +0.18%）—— 这里只作「板块强 ≠ 个股强」的对照，
            不是买入建议。
          </p>
          {sectorStockTrack && (
            <div className="border-b border-line-soft px-4 py-2 text-[12px] text-fg-dim">
              <button
                type="button"
                onClick={() => setSectorStockOpen((open) => !open)}
                className="w-full text-left leading-relaxed hover:text-fg"
              >
                <span className="num">
                  近 {sectorStockTrack.verified_days} 日：1 日{' '}
                  {fmtPct(sectorStockTrack.ret1.mean)} · 中位{' '}
                  {fmtPct(sectorStockTrack.ret1.median)} · 胜率{' '}
                  {fmtNum(sectorStockTrack.ret1.up_pct, 1, '%')} · 大面{' '}
                  {fmtNum(sectorStockTrack.ret1.big_loss, 1, '%')} · 超额{' '}
                  {fmtPct(sectorStockTrack.ret1.excess)} · 样本 {sectorStockTrack.samples}
                </span>
                <span className="ml-2 text-fg-muted">
                  {sectorStockOpen ? '收起 ▴' : '逐日明细 ▾'}
                </span>
              </button>
              {sectorStockOpen && (
                <div className="mt-2 overflow-x-auto">
                  <table className="grid-table">
                    <thead>
                      <tr>
                        <SortTh sortKey="trade_date" align="left" {...stockDaySort}>
                          日期
                        </SortTh>
                        <SortTh sortKey="picks" {...stockDaySort}>
                          只数
                        </SortTh>
                        <SortTh sortKey="ret1" {...stockDaySort}>
                          1 日
                        </SortTh>
                        <SortTh sortKey="excess1" {...stockDaySort}>
                          超额
                        </SortTh>
                        <SortTh align="left">状态</SortTh>
                      </tr>
                    </thead>
                    <tbody>
                      {stockDayRows.map((row) => (
                        <tr key={row.trade_date}>
                          <td className="num !text-left">{row.trade_date}</td>
                          <td className="num">{row.picks}</td>
                          <td className={`num ${toneOf(row.ret1)}`}>{fmtPct(row.ret1)}</td>
                          <td className={`num ${toneOf(row.excess1)}`}>
                            {fmtPct(row.excess1)}
                          </td>
                          <td className="!text-left text-fg-muted">
                            {row.status === 'pending' ? '待验证' : '已验证'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
          <div className="flex flex-wrap items-center gap-3 border-b border-line-soft px-4 py-2 text-[12px] text-fg-dim">
            <span className="flex items-center gap-2">
              板块数
              <Segmented
                value={stockSectors}
                items={STOCK_SECTOR_ITEMS}
                onChange={setStockSectors}
              />
            </span>
            <span className="flex items-center gap-2">
              每板块
              <Segmented value={stockPer} items={STOCK_PER_ITEMS} onChange={setStockPer} />
            </span>
            <span className="flex items-center gap-2">
              个股排序
              <Segmented
                value={stockOrder}
                items={STOCK_ORDER_ITEMS}
                onChange={setStockOrder}
              />
            </span>
          </div>
          {sectorStockError ? (
            <LoadState loading={false} error={sectorStockError} />
          ) : sectorStock === null ? (
            <LoadState loading emptyText="加载中" />
          ) : sectorStock.rows.length === 0 ? (
            <LoadState
              loading={false}
              emptyText={
                stockScope === 'strong'
                  ? '今天没有 RPS20 ≥ 90 的板块（或成分股还没采到）'
                  : '今天没有 RPS20 ≤ 10 的板块（或成分股还没采到）'
              }
            />
          ) : (
            <>
              <div className="border-b border-line-soft px-4 py-2 text-[12px] text-fg-dim">
                选中的板块：
                {sectorStock.sectors
                  .map(
                    (item) =>
                      `${item.name ?? item.code}（RPS ${fmtNum(item.rps20, 0)} · 当日 ${fmtPct(item.pct_chg)}）`,
                  )
                  .join('、')}
              </div>
              <div className="overflow-x-auto">
                <table className="grid-table">
                  <thead>
                    <tr>
                      <th>#</th>
                      <SortTh sortKey="sector_name" align="left" {...stockSort}>
                        板块
                      </SortTh>
                      <SortTh sortKey="name" align="left" {...stockSort}>
                        个股
                      </SortTh>
                      <SortTh sortKey="pct_chg" {...stockSort}>
                        当日涨幅
                      </SortTh>
                      <SortTh sortKey="sector_pct_chg" {...stockSort}>
                        板块当日
                      </SortTh>
                      {stockOrder === 'factor' && (
                        <SortTh sortKey="factor_score" {...stockSort}>
                          因子分
                        </SortTh>
                      )}
                    </tr>
                  </thead>
                  <tbody>
                    {stockRows.map((row, index) => (
                      <tr key={row.code}>
                        <td className="num text-fg-dim">{index + 1}</td>
                        <td className="!text-left text-fg-muted">
                          {row.sector_name ?? row.sector_code}
                        </td>
                        <td className="!text-left">
                          <StockLink code={row.code} className="text-fg">
                            {row.name ?? row.code}
                          </StockLink>
                        </td>
                        <td className={`num ${toneOf(row.pct_chg)}`}>{fmtPct(row.pct_chg)}</td>
                        <td className={`num ${toneOf(row.sector_pct_chg)}`}>
                          {fmtPct(row.sector_pct_chg)}
                        </td>
                        {stockOrder === 'factor' && (
                          <td className="num">{fmtNum(row.factor_score, 1)}</td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </Panel>

        <p className="px-1 text-[12px] leading-relaxed text-fg-dim">
          四块都是盘后读数，不是买卖建议：本站没有盘中数据，打板要看次日承接。
          阈值（首封时间、市值、题材家数）只是起手参数 —— 改参数看上面那行「近 20 日」
          的样本 / 晋级率 / 开盘溢价怎么变，比凭感觉定阈值靠谱。
        </p>
      </div>
    </Layout>
  )
}
