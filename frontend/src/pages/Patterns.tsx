import { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import type {
  PatternCount,
  PatternMeta,
  PatternScorecardOut,
  PatternStock,
  PatternSummary,
} from '../api/types'
import Alert from '../components/Alert'
import KLineChart from '../components/KLineChart'
import type { KeyLevel } from '../components/KLineChart'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import { phaseTone } from '../components/PhasePanel'
import Segmented from '../components/Segmented'
import SortTh from '../components/SortTh'
import { copyText } from '../lib/clipboard'
import { boardOf, fmtAmount, fmtInt, fmtNum, fmtPct, fmtShortDate, toneOf } from '../lib/format'
import type { Board } from '../lib/format'
import { K_VIEWS, useKLine } from '../lib/klinePeriod'
import { useSort } from '../lib/sort'
import type { SortSpecs } from '../lib/sort'
import { rememberStockList } from '../lib/stockNav'

/**
 * 形态分组的展示顺序。后端给的 catalog 就是按这个顺序排的，这里只做兜底。
 *
 * ⚠️ 后端新增分组时**必须同步加到这里**：`indexOf` 找不到会给 -1，那一组会被排到
 * 最前面（「知行B1」2026-10-13 就是这么加的）。
 *
 * ⚠️ 分组名长度会直接影响筛选条那个标签列：`w-24` 是按最长的一组
 * 「单 K 蜡烛形态」（5 个汉字 + 1 个字母）留的。再加更长的分组名时要一起调，
 * 否则标签会折行、把每个分组的高度撑成两行。
 */
const GROUP_ORDER = [
  '致富',
  '悟道之路',
  '做T',
  '知行B1',
  '趋势',
  '突破',
  '量价',
  '几何',
  '单 K 蜡烛形态',
]

/**
 * 默认视图（没选形态）一次取多少只。
 *
 * **2026-09-26 用户定为 50**（原 500）：这个列表是「今天最值得看的几十只」，
 * 一天命中一两千条时前 500 名里后半段基本没人翻；取 50 条也让响应体小一个量级。
 * 代价要认：截断提示会更常出现，而且**列排序只在这 50 条之内**（见 `HIT_SORTS`）。
 *
 * **取不满时必须在页面上说出来** —— 否则「返回 50 行」会被读成「今天一共只有
 * 50 只命中」，那是两个完全不同的结论（提示文案直接用这个常量，不用再写一遍数）。
 */
const HIT_LIMIT = 50

/**
 * 选中某个形态时一次取多少只。
 *
 * **2026-09-27 用户要求**：点了形态就该看到**它的全部命中**，而不是「全市场前 50
 * 里恰好属于这个形态的那几只」（原来两者混在一起，出现过「标签写 312 家、点进去
 * 只剩 5 行」）。所以形态模式下按后端允许的最大值取，实际条数由 `countOf` 兜底
 * 校验；真超过这个数（单形态一天三千只以上）会照旧弹截断提示，不会静默少给。
 */
const PATTERN_LIMIT = 3000

/**
 * 「近 N 日」窗口的候选值（1 = 只看当天）。
 *
 * 为什么要有这个（2026-10-14 用户要求）：形态里有**状态型**的 —— 条件一直成立就天天
 * 命中。「堆量上涨后缩量」最典型：量能天天创新低就天天入选，实测中红医疗 8/14~8/25
 * 连中 6 天。看十天名单时同一只票占五六行，榜单被撑爆。选了 >1 之后由后端把窗口内
 * 同一只票的**重复命中合并成一条**、日期取**首次**那天（`run_days` 说明持续了几天）。
 *
 * ⚠️ 2026-10-15 起**没选形态（默认视图）也能用**（用户看到它们灰着，要求放开）：
 * 原先这三个按钮在没选形态时是 `disabled`（理由是「合并得先确定是哪个形态在重复」），
 * 但默认视图一样会被状态型形态撑爆 —— 去重跟选不选形态无关。
 */
const WINDOWS = [1, 5, 10, 20]

/**
 * 「信号日」列头的提示文案（那一列只在窗口模式下出现）。
 *
 * ⚠️ 默认视图（没选形态）在**长窗口**下每天只读评分最高的前若干条（后端
 * `_WINDOW_ROW_BUDGET` 按天数均分），所以少数票的「首次」会晚一两天 —— 10 日 / 20 日
 * 这两档会碰到；5 日那档每天 3000 行高于实际单日行数、切不着。选中形态时走的是那批票
 * 的全量（不受预算影响），所以那种情况下不提这句。
 *
 * 分档阈值写 10 是跟着后端的预算来的：10 日 → 每天 1500 行、20 日 → 750 行，
 * 都低于实测单日约 2650 行；5 日 → 3000 行不切。后端改预算时这里要一起看。
 */
function signalDateTitle(days: number, hasPicked: boolean): string {
  // 这是浏览器原生 title，**不要写 markdown 的 `**`** —— 会原样显示成星号
  const base = '这一轮的信号日：该票在窗口内「首次」命中那天，「+N日」表示之后连续命中到哪天'
  if (days < 10 || hasPicked) return base
  return `${base}。注意：默认视图为了少读，长窗口下每天只取评分最高的前若干条，少数票的「信号日」可能比真正的首次晚一两天（选中某个形态时不受影响）`
}

/**
 * 交易所板块筛选的候选。**依据是代码号段**（见 `lib/format.boardOf`），与后端
 * `scan_patterns.is_pattern_board`（形态落库闸门）是同一套范围。
 *
 * **默认勾主板 + 创业板 + 科创板**：2026-10-14 用户先要求「只选科创板和创业板」，
 * 当天又要求「把主板板块闸门也放开」—— 后端那道关现在是这三块（不含北交所）。
 * 这里的默认值必须跟着后端走，否则新进来的主板命中会被前端默认筛掉、用户看不到。
 */
const BOARDS: Board[] = ['主板', '创业板', '科创板', '北交所']
const DEFAULT_BOARDS: Board[] = ['主板', '创业板', '科创板']

/**
 * 阶段判定筛选的候选（2026-10-20 用户要求：在筛选区单独一行，分「明天预案 / 明天盯」）。
 *
 * 前两档的值与后端 `WUDAO_PHASE_LABELS` 的 key 一致，label 直接用那张表的展示名 ——
 * 表格里的「阶段判定」列显示的是同一个词，所以筛出来的一屏天然对得上。
 *
 * `oversold`（超跌）是第三档，**不是** 9 档阶段之一：它按用户「日线做T」公式判
 * `乖离 =(C−MA20)/MA20×100 < −15`，与阶段互不影响（一只票可以既超跌又是「明天预案」）。
 * 与前面两档一起选时后端取**并集**。
 */
const PHASES: { value: string; label: string }[] = [
  { value: 'start', label: '明天预案' },
  { value: 'sample', label: '明天盯' },
  { value: 'oversold', label: '超跌' },
]

/** 解析 URL 里的 `phase`（逗号分隔）。非法值丢掉，空列表 = 不筛。 */
function readPhases(raw: string | null): string[] {
  if (!raw) return []
  return raw
    .split(',')
    .map((item) => item.trim())
    .filter((item) => PHASES.some((phase) => phase.value === item))
}

/** 解析 URL 里的 `boards`（逗号分隔）。**空值 / 全是非法值一律回落到默认**
 * （`DEFAULT_BOARDS`，与后端那道闸门同一范围）—— 而不是空列表
 * （空列表 = 一只都不显示，最难理解）。
 */
function readBoards(raw: string | null): Board[] {
  if (raw === null) return DEFAULT_BOARDS
  const names = raw.split(',').filter((name): name is Board => BOARDS.includes(name as Board))
  return names.length > 0 ? names : DEFAULT_BOARDS
}

/**
 * 命中表**实际渲染**多少行（与 `PATTERN_LIMIT` 是两件事：那个管取数，这个管 DOM）。
 *
 * 形态视图最多取回 3000 只，全量进 DOM 时一次渲染几千行、十几列，切评分滑块都会卡。
 * 排序与筛选仍在全量 `shown` 上做，这里只截断渲染（做法照搬 Wudao 页的 RENDER_LIMIT）。
 * 这张表 11 列、行密，200 行约是 520px 滚动容器十几屏，够翻也不卡；想多看就用筛选缩小。
 */
const RENDER_LIMIT = 200

/**
 * 解析 URL 里的 `patterns`（逗号分隔的形态 key 列表）。
 *
 * 形态筛选是**多选、取交集**（只显示同时满足全部所选形态的票），所以链接参数也用列表；
 * 空串与重复项一并清掉，免得手改 URL 时多出一个匹配不上的空 key（它会让列表永远是空的）。
 */
function splitPatterns(value: string | null): string[] {
  if (!value) return []
  return Array.from(new Set(value.split(',').map((item) => item.trim()).filter(Boolean)))
}

/**
 * 命中列表各列的排序口径。
 *
 * ⚠️ **默认视图**（没选形态）下排序只在那 50 条之内（后端按评分给的前 50），
 * 不是全市场排名 —— 靠列表上方那条截断提示说明，否则「成交额排名第一」会被读成
 * 全市场第一。选中某个形态之后取的是**该形态的全部命中**，那时排序就是该形态的
 * 完整排名（评分也换成该形态自己的分数）。
 */
const HIT_SORTS: SortSpecs<PatternStock> = {
  score: { value: (stock) => stock.score },
  code: { value: (stock) => stock.code, first: 'asc' },
  close: { value: (stock) => stock.close },
  pct_chg: { value: (stock) => stock.pct_chg },
  amount: { value: (stock) => stock.amount },
  avg_amount: { value: (stock) => stock.avg_amount },
  total_mv: { value: (stock) => stock.total_mv },
}

/**
 * 「正在看图的那只票」存在 sessionStorage 里，**不进 URL**。
 *
 * 为什么不放 URL：点代码链接时既要记住这只票、又要让 Link 完成跳转。两者都
 * 经过 router 的话（`setSearchParams` + Link 自己的 navigate），同一个点击里会
 * 连发两次导航 —— 实测表现为「点一下只选中、要点两下才跳转」。
 * sessionStorage 是同步写入、完全不碰 router，不会干扰 Link 那一次导航。
 */
const ACTIVE_KEY = 'patterns:active-code'

function readActive(): string | null {
  try {
    return sessionStorage.getItem(ACTIVE_KEY)
  } catch {
    // 隐私模式下 sessionStorage 可能直接抛错。退化成「不记住」而不是白屏
    return null
  }
}

/**
 * 明细字段的展示名与单位：`[中文名, 是不是比率]`。
 *
 * 名字和单位必须写在同一张表里。拆成「名字表 + 百分比表」两张，是
 * `pct_chg` 被当成比率又乘 100（+10.02% 显示成 1002.00%）的直接原因 ——
 * 两张表各自演进，早晚对不上。
 *
 * 另外提醒：**字段名在全站必须唯一**。`gap` 一度在均线多头排列里表示
 * 「距 MA20 的比率」、在 W 底里表示「两个底隔了几个交易日」，同名不同义
 * 会让这里的单位判断无从下手（27 天被当比率显示成 2700%）。后端已把后者
 * 改名成 `bottom_gap`。
 */
const DETAIL_FIELDS: Record<string, [string, boolean]> = {
  // 趋势
  stack_days: ['排列天数', false],
  spread: ['MA5-MA60 乖离', true],
  gap: ['距 MA20', true],
  // 突破
  window: ['新高窗口', false],
  excess: ['突破幅度', true],
  vol_ratio: ['量比', false],
  platform_width: ['平台振幅', true],
  // 量价
  pct_chg: ['当日涨幅', false], // 后端存的就是百分数（10.02 = +10.02%）
  // 放量阳包阴（2026-10-19）：倍量 = 今量 / 昨量；实体比复用下面蜡烛组已有的 `body_ratio`
  volume_ratio: ['倍量（今/昨）', false],
  pullback_days: ['回调天数', false],
  drop: ['回调幅度', true],
  // 几何
  cup_depth: ['杯深', true],
  recover: ['右杯沿恢复', true],
  handle_days: ['柄部天数', false],
  handle_ratio: ['柄部回撤/杯深', true],
  handle_vol_ratio: ['柄部缩量比', true],
  bottom_gap: ['两底间隔', false],
  tolerance: ['两底差距', true],
  rebound: ['中间反弹', true],
  position: ['在区间位置', true],
  pivots: ['摆动点数', false],
  shoulder_diff: ['两肩差距', true],
  neck_diff: ['颈线倾斜', true],
  head_depth: ['头深', true],
  tail: ['右肩后天数', false],
  pole_gain: ['旗杆涨幅', true],
  flag_days: ['旗面天数', false],
  flag_range: ['旗面振幅', true],
  retrace: ['回撤/旗杆', true],
  vol_drop: ['旗面缩量比', true],
  // 三段式突破
  slow_gain: ['缓涨涨幅', true],
  slow_bull: ['缓涨阳线占比', true],
  surge_gain: ['急涨涨幅', true],
  surge_days: ['急涨天数', false],
  // 量比是「倍数」不是「占比」，所以不带百分号（与旗形的 vol_ratio 一致）
  surge_vol: ['急涨量比', false],
  flat_days: ['整理天数', false],
  flat_range: ['整理振幅', true],
  // shrink = 整理段均量 / 急涨段均量，0.68 表示「缩到 68%」，按占比显示更直观
  shrink: ['缩量到', true],
  break_vol: ['突破量比', false],
  // 涨停爆量横盘（surge_gain / flat_days / flat_range / shrink / excess
  // 与三段式共用同一套字段名，语义也对得上）
  limit_vol: ['涨停量比', false],
  keep: ['守住成果', true],
  // 突破后横盘（vol_ratio / flat_days / flat_range / shrink / position 与既有字段共用）
  break_gain: ['突破涨幅', true],
  giveback: ['横盘回撤', true],
  // N 字选股（surge_gain / vol_ratio / pullback_days / shrink 与既有字段共用）
  start_gap: ['距起涨点', true],
  // 爆量后缩量回踩（shrink 与既有字段共用：缩量日成交额 / 爆量日成交额）
  burst_mult: ['爆量倍数', false],
  burst_gap: ['爆量后间隔', false],
  // 悟道之路 · 致富（样板/启动）
  high_pct: ['冲高%', false],
  vol_ratio_20: ['量比(20日)', false],
  leave_high: ['离开最高', true],
  sample_high: ['样板高', false],

  // ---- 做T买点（t_buy）----
  // tier / trigger 是短标签（"双B" / "底背离 + 基买"），走 detailText 的字符串分支原样显示
  tier: ['档位', false],
  trigger: ['触发', false],
  ma20_gap: ['距 MA20', true],

  // ---- 2026-09-25 新增形态（键与 backend/app/services/patterns.py 的 detail 一一对应）----
  // 键是全局共用的，所以新增时要**先看有没有语义相同的旧键**（能共用就共用，
  // 不能共用就换个不会误导的名字）—— 像 `recover` 已经被杯柄占成「右杯沿恢复」，
  // 圆弧底就只能另起 `recover_pct`，否则同一个是数字却挂着别人的标签。
  // 趋势
  squeeze_width: ['粘合宽度', true],
  ma20_rise: ['MA20 涨幅', true],
  ma10_rise: ['MA10 涨幅', true],
  days: ['持续天数', false],
  cross_days_ago: ['金叉距今', false],
  // （`daily_slope` / `touches` / `weeks` / `ma5_gap` 2026-10-15 随「上升通道 / 周线多头」
  //  撤下一起删了 —— 它们只被那两个形态用到；`touches_top` / `touches_bottom` 是箱体的，
  //  名字像但不是一套，别顺手删）
  // 突破
  box_range: ['箱体振幅', true],
  touches_top: ['触顶次数', false],
  touches_bottom: ['触底次数', false],
  fill: ['回补深度', true],
  rise: ['缺口后涨幅', true],
  days_ago: ['缺口天数', false],
  // 量价
  vol_shrink: ['缩量到', true],
  drawdown: ['距高点回撤', true],
  upper_shadow: ['上影占比', true],
  gain: ['区间涨幅', true],
  // 几何
  recover_pct: ['距底收复', true],
  down_days: ['下跌段天数', false],
  up_days: ['上涨段天数', false],
  vol_boost: ['右侧量能倍数', false],
  amplitude: ['窗口振幅', true],
  low_position: ['低点位置', true],
  low_spread: ['三底差距', true],
  // 上升楔形 / 菱形 2026-10-20 撤下：expand / contract / upper_slope / lower_slope /
  // gap_ratio / converge 只被它们用过，同删
  // 单 K 蜡烛形态（2026-10-14 撤下锤子线 / 倒锤子线 / 启明星时，把只被它们用到的
  // `shadow_ratio` 与 `star_recover` 也删了；同日给揉搓线补了下面两个。
  // 同日再撤看涨吞没 / 阳包阴，`prior_drop` 只被前者用到、一并删；
  // `body_ratio` 揉搓线还在用、`close_pos` 悟道形态还在用，都保留）
  upper_ratio: ['昨上影占比', true],
  lower_ratio: ['今下影占比', true],
  body_ratio: ['实体比', false],
  close_pos: ['收盘位置', true],
  steady: ['实体不缩水', false],

  // ---- 知行超级B1（2026-10-13，7 个子信号共用同一份明细）----
  // `vol_shrink`（缩量到）复用上面已有的键，口径一致：当日量 / 20 日最高量。
  rsi3: ['3日RSI', false],
  kdj_j: ['KDJ-J', false],
  amp20: ['20日振幅', true],
  amp50: ['50日振幅', true],
  dist_white: ['距白线', true],
  dist_yellow: ['距黄线', true],
  dist_bbi: ['距BBI', true],
  day_amp: ['当日振幅', true],
  // 知行B1 那一组会给 `ago`。**2026-10-15 起整组是「当天」口径，所以它恒为 0** ——
  // 字段留着是因为后端 `pattern_track._picks` 还在按 `ago = 0` 过滤（那里会变成
  // 空操作）；哪天窗口再放宽成「近 N 日」，这里就又有意义了。
  // （10-13~10-14 那两天它确实是「最近一次距今几天」，列在表里能看到 3 / 4 这种值。）
  ago: ['信号距今', false],

  // ---- 堆量上涨后缩量（pile_up_shrink，2026-10-14 新增）----
  // `shrink`（缩量到）与 `drop`（回调幅度）与既有字段共用；
  // 涨幅那个键特意叫 `pile_rise` 而不叫 `rise` —— `rise` 已经被 gap_fill
  // （补缺口后的反弹幅度）占了，同名会串标签。
  pile_days: ['堆量天数', false],
  vol_mult: ['堆量倍数', false],
  pile_rise: ['堆量涨幅', true],
  pile_gap: ['堆量后间隔', false],

  // ---- 海龟突破 / 均线放量金叉（2026-10-17 移植自 Sequoia-X）----
  // `vol_ratio` / `vol_ratio_20` / `excess` / `pct_chg` 与既有字段共用；
  // 这两个是新增语义，别复用 `rise`（已被 gap_fill 占成「补缺口后的反弹幅度」）。
  amount_yi: ['成交额(亿)', false],
  cross_gap: ['金叉幅度', true],
}

function detailText(key: string, value: number | string): string {
  if (typeof value !== 'number') return String(value)
  const [, isRatio] = DETAIL_FIELDS[key] ?? ['', false]
  if (isRatio) return `${(value * 100).toFixed(2)}%`
  // pct_chg 是百分数口径（后端存 10.02 = +10.02%），只补 % 号、不乘 100；
  // 其余 isRatio=false 的字段（天数、根数、量比）是纯计数，不带 %
  if (key === 'pct_chg') return `${value.toFixed(2)}%`
  // 计数类给整数（天数、根数），其余保留两位
  return Number.isInteger(value) ? String(value) : value.toFixed(2)
}

export default function Patterns() {
  /**
   * 视图状态挂在 URL 上：点进个股页再后退时这一页会**重新挂载**，
   * 不写进 URL 的话日期、形态筛选、正在看图的那只票会全部归零。
   *
   * 评分下限故意不进去：滑块拖动会每一帧改一次地址，属于噪声。
   */
  const [params, setParams] = useSearchParams()
  const [date, setDate] = useState<string | null>(() => params.get('date'))
  const [dates, setDates] = useState<string[]>([])
  const [catalog, setCatalog] = useState<PatternMeta[]>([])
  const [hits, setHits] = useState<PatternStock[]>([])
  /** 这一屏**截断前**的条数（后端 `/hits` 的 `total`）—— 判断有没有更多全靠它 */
  const [hitsTotal, setHitsTotal] = useState(0)
  const [summary, setSummary] = useState<PatternSummary | null>(null)
  // chip 徽标的「再加上它会剩多少只」（后端按当前选择算好的交集家数）
  const [preview, setPreview] = useState<PatternCount[]>([])
  // 全形态成绩单：只用来给 chip 的悬停提示补一行「近 N 日线上表现」。
  // 取不到就当没有（`null`），**不影响筛选与列表** —— 它是增强信息，不是主体。
  const [scorecard, setScorecard] = useState<PatternScorecardOut | null>(null)
  // 选中的形态：**多选**（交集）。空数组 = 默认视图（全市场按评分取前 HIT_LIMIT 只）
  const [picked, setPicked] = useState<string[]>(() => splitPatterns(params.get('patterns')))
  const [minScore, setMinScore] = useState(0)
  // 「近 N 日」窗口：>1 时后端把窗口内同一只票的重复命中合并成一条（见 `WINDOWS`）
  const [days, setDays] = useState(() => {
    const raw = Number(params.get('days'))
    return WINDOWS.includes(raw) ? raw : 1
  })
  // 交易所板块筛选：默认只勾创业板 + 科创板（见 `BOARDS`）
  const [boards, setBoards] = useState<Board[]>(() => readBoards(params.get('boards')))
  // 阶段判定筛选：空数组 = 不筛（默认）。多选 = 并集（一只票只有一个阶段，
  // 所以「明天预案 + 明天盯」就是这两种都留下）
  const [phases, setPhases] = useState<string[]>(() => readPhases(params.get('phase')))
  const [active, setActive] = useState<string | null>(() => readActive())
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // 图上的 K 线：周期、取数、按需补长历史都在这个 hook 里（与个股页共用一套）。
  // fq='qfq' 固定前复权序列 —— 引擎就是在这条序列上判定的，用不复权图画线会在
  // 除权股上把关键位画错高度。**传了 fq 就不再渲染复权菜单**（`fqAdjustable` 为 false）
  const kline = useKLine(active ?? '', { fq: 'qfq' })
  // 「复制代码」的瞬时反馈：成功后按钮文案换成「已复制」，1.6 秒复原
  const [copied, setCopied] = useState(false)
  // 反馈复原定时器：卸载时清掉，否则页面已经没了还会触发一次 setState
  const copiedTimer = useRef<number | null>(null)
  useEffect(() => {
    return () => {
      if (copiedTimer.current !== null) window.clearTimeout(copiedTimer.current)
    }
  }, [])

  // 写回 URL。replace 而不是 push：点一行就塞一条历史的话，退出这页要按很多次返回
  useEffect(() => {
    const next = new URLSearchParams(params)
    if (date) next.set('date', date)
    else next.delete('date')
    // 「看图的那只票」刻意不写进 URL，原因见 ACTIVE_KEY 上面的说明
    if (picked.length > 0) next.set('patterns', picked.join(','))
    else next.delete('patterns')
    // 「近 N 日」写进地址：换了窗口的名单是可以分享的（评分下限则故意不写，见上）
    if (days > 1) next.set('days', String(days))
    else next.delete('days')
    // 板块：与默认（创业板 + 科创板）一致时不写进地址，少点噪声；改过才写
    const asKey = (list: Board[]) => [...list].sort().join(',')
    if (asKey(boards) !== asKey(DEFAULT_BOARDS)) next.set('boards', asKey(boards))
    else next.delete('boards')
    // 阶段判定筛选：空 = 不筛（默认），不写进地址
    if (phases.length > 0) next.set('phase', [...phases].sort().join(','))
    else next.delete('phase')
    if (next.toString() !== params.toString()) setParams(next, { replace: true })
  }, [date, picked, days, boards, phases, params, setParams])

  useEffect(() => {
    api.dates().then(setDates).catch(() => setDates([]))
    api.patternCatalog().then(setCatalog).catch(() => setCatalog([]))
    // 成绩单是增强信息：失败就静默当没有，不弹错（筛选条照常可用）
    api.patternScorecard().then(setScorecard).catch(() => setScorecard(null))
  }, [])

  /**
   * @param isStale 判断这次请求还算不算数。切日期/切形态时旧请求的响应可能晚到，
   *   没有守卫的话会把新的一屏盖成旧的（2026-09-27 修）。
   */
  const load = useCallback(
    async (
      target: string | null,
      only: string[],
      win: number,
      onlyPhases: string[],
      isStale: () => boolean = () => false,
    ) => {
      setLoading(true)
      setError(null)
      try {
        // 没选形态：全市场按评分取前 `HIT_LIMIT` 只（默认视图）。
        // 选了形态：取这些形态的**全部**命中（`PATTERN_LIMIT`，多个之间取**交集**），
        // 评分与排序由后端换成所选形态里的最高分 —— 见 `api.patternHits` 与后端 `/hits`
        // `win` = 窗口天数（>1 时后端合并同一只票的重复命中，见 `WINDOWS`）
        // `onlyPhases` = 阶段判定筛选（空数组 = 不筛）。传了它以后端是「先算全量命中的
        // 阶段、再筛、最后截 limit」；超跌还会把**全市场**超跌股补进来，所以这里必须
        // 取全量（`PATTERN_LIMIT`），否则纯超跌的票会被 50 只上限截掉
        const [list, sum, prev] = await Promise.all([
          api.patternHits(
            target,
            0,
            only.length > 0 || onlyPhases.length > 0 ? PATTERN_LIMIT : HIT_LIMIT,
            only,
            win,
            onlyPhases,
          ),
          api.patternSummary(target),
          // 徽标数据：跟着 `only` 与窗口走，所以每次切形态／切窗口都重算（与命中列表同一次请求里发出）
          api.patternPreview(target, 0, only, win),
        ])
        if (isStale()) return
        setHits(list.items)
        setHitsTotal(list.total)
        setSummary(sum)
        setPreview(prev)
      } catch (err) {
        if (isStale()) return
        setHits([])
        setHitsTotal(0)
        setSummary(null)
        setPreview([])
        setError((err as Error).message)
      } finally {
        if (!isStale()) setLoading(false)
      }
    },
    [],
  )

  useEffect(() => {
    let stale = false
    // 先清空：旧日期/旧形态的命中留在表里会被当成新的一屏看（与 `Funds` 同一处理）
    setHits([])
    setHitsTotal(0)
    setSummary(null)
    setPreview([])
    // 看历史日期时不带阶段筛选（后端会 400，见「阶段判定」那一行的注释）；切回最新自动恢复
    void load(date, picked, days, date !== null ? [] : phases, () => stale)
    return () => {
      stale = true
    }
    // ⚠️ `picked` 必须在依赖里：切形态要**重新取数**（默认视图取前 50、
    // 形态视图取该形态的全部命中），这是 2026-09-27 那次改动的关键一步
    // `days` 同理：换窗口要重新取（>1 时后端合并同一只票的重复命中）
    // `phases` 同理：阶段是在后端算的，换档必须重新请求（前端筛不了全市场）
  }, [date, picked, days, phases, load])

  // 2026-10-15 之前这里有一句「没选形态就把 days 拉回 1」—— 那时后端没 `pattern` 会
  // 忽略 `days`，留着「近10日」的选中态而列表其实是当天的，对不上账。现在后端两种模式
  // 都认窗口了，这句撤销。

  const groups = useMemo(() => {
    const map = new Map<string, PatternMeta[]>()
    for (const item of catalog) {
      const list = map.get(item.group) ?? []
      list.push(item)
      map.set(item.group, list)
    }
    return [...map.entries()].sort(
      (a, b) => GROUP_ORDER.indexOf(a[0]) - GROUP_ORDER.indexOf(b[0]),
    )
  }, [catalog])

  const countOf = useMemo(() => {
    const map: Record<string, number> = {}
    for (const item of summary?.by_pattern ?? []) map[item.pattern] = item.stocks
    return map
  }, [summary])

  /**
   * chip 徽标数字：优先用 `/preview` 的交集家数（「再加上它会剩多少只」），
   * 它拿不到时退回 `/summary` 的全市场家数。
   *
   * 为什么要有 fallback：`/preview` 与命中列表在同一个 `Promise.all` 里发出，
   * 正常情况下同时到；万一它单独失败（或还没到），退回全市场家数总比徽标集体空成
   * 0 好。注意 `?? ` 只对 null/undefined 生效 —— 交集真的是 0 只时 `0 ?? x` 仍是 0，
   * 那正是要显示的结果（提示这个组合没票）。
   */
  const previewOf = useMemo(() => {
    const map: Record<string, number> = {}
    for (const item of preview) map[item.pattern] = item.stocks
    return map
  }, [preview])

  /** 形态 key → 成绩单那一行（只用于 chip 的 title，缺了就不显示这段） */
  const scoreOf = useMemo(() => {
    const map = new Map<string, PatternScorecardOut['patterns'][number]>()
    for (const row of scorecard?.patterns ?? []) map.set(row.pattern, row)
    return map
  }, [scorecard])

  /** 当前选中形态的中文名（标题与提示都要用；清单还没拉到时退回 key） */
  const pickedNames = useMemo(
    () => picked.map((key) => catalog.find((item) => item.key === key)?.name ?? key),
    [picked, catalog],
  )
  /** 标题／提示里那一小串：单个就是名字，多个是「第一个 等 N 个形态」 */
  const pickedLabel =
    pickedNames.length === 0
      ? null
      : pickedNames.length === 1
        ? pickedNames[0]
        : `${pickedNames[0]} 等 ${pickedNames.length} 个形态`

  // 评分滑块拖动时 `minScore` 每一帧都在变，而下面要「过滤 3000 条 + 排序 3000 条」，
  // 每帧都做会拖出可见的卡顿。用 deferred 值：滑块与数字即时跟手，重活等 React
  // 空闲再算，拖动过程中的中间值会被自动跳过（2026-10-12）。
  const deferredMinScore = useDeferredValue(minScore)

  const visible = useMemo(() => {
    return hits.filter((stock) => {
      if (stock.score < deferredMinScore) return false
      // 板块关：先过这一道（默认只留创业板 + 科创板，见 `BOARDS`）。
      // 号段认不出的极端情况保留而不是丢掉 —— 静默少几行比多几行难查得多
      const board = boardOf(stock.code)
      if (board !== null && !boards.includes(board)) return false
      if (picked.length === 0) return true
      // 后端在形态模式下返回的就是「同时满足全部所选形态的票」，所以这里通常全过；
      // 留一道是兜底（也保证每行是按**全部**命中形态判的，不是按展示用的截断列表）
      // 窗口模式下后端放宽成「窗口内先后命中过每一个」，并把所选形态**各自首次命中**
      // 那次都塞进 `patterns`，所以这一句照样成立（2026-10-14 修口径不一致）
      return picked.every((key) => stock.patterns.some((item) => item.pattern === key))
    })
  }, [hits, picked, boards, deferredMinScore])

  // 首屏按评分降序（与后端给的一致，所以看到的顺序没变，但列头能正确亮出
  // 当前排的是哪一列）；点列头改成别的口径。**排的是 visible，不是 hits** ——
  // 排序与筛选是两个独立动作，先筛后排才符合直觉
  const [hitSort, shown] = useSort(visible, HIT_SORTS, { key: 'score' })

  // 只把前 RENDER_LIMIT 行放进 DOM（排序结果本身不截断，← → 翻票与复制代码仍用全量）
  const rendered = useMemo(() => shown.slice(0, RENDER_LIMIT), [shown])
  const hiddenCount = shown.length - rendered.length

  /**
   * 把当前筛选结果的代码复制下来（每行一个），方便导进同花顺的自选股。
   * 实现见 `lib/clipboard.copyText`，这里只负责失败提示与「已复制」状态。
   */
  const copyCodes = useCallback(() => {
    if (!copyText(visible.map((stock) => stock.code).join('\n'))) {
      setError('复制失败：浏览器不允许，手动选中列表里的代码吧')
      return
    }
    setCopied(true)
    // 连点复制时以最后一次为准：先清旧定时器，避免文案提前复原
    if (copiedTimer.current !== null) window.clearTimeout(copiedTimer.current)
    copiedTimer.current = window.setTimeout(() => {
      copiedTimer.current = null
      setCopied(false)
    }, 1600)
  }, [visible])

  /**
   * 这一屏的「应有条数」与截断判定。
   *
   * `hitsTotal` 是后端给的**截断前**条数（`/hits` 的 `total`，2026-10-22 加的契约）——
   * 以前只能靠「条数是否正好顶到上限」猜，窗口模式（`days > 1`）去重之后猜不准，
   * 猜错就把「还有更多」当成「就这些」。
   *
   * ⚠️ 阶段筛选也必须按「全量」取（2026-10-20 修）：超跌是**全市场池**（当天 288 只），
   * 若沿用 50 只的上限，纯超跌的票会被命中的那几十只挤掉 —— 实测中材科技乖离 −21.3%、
   * 排在第 70 位，用户在页面上根本看不到它。
   */
  const expected = hitsTotal
  const truncated = hitsTotal > hits.length

  // 筛完之后当前看图的票可能已经不在列表里，自动切到第一条
  useEffect(() => {
    // 取数期间 visible 必然是空的，此时判定「这只票不在列表里」会把
    // URL 上带的 code 冲掉 —— 从个股页退回本页时正好撞上这一下
    if (loading) return
    if (visible.length === 0) {
      setActive(null)
      return
    }
    if (!active || !visible.some((stock) => stock.code === active)) {
      setActive(visible[0].code)
    }
  }, [visible, active, loading])

  const current = useMemo(
    () => visible.find((stock) => stock.code === active) ?? null,
    [visible, active],
  )

  /**
   * 记下「正在看图的那只票」（点列表面板任意一行时调用）。
   *
   * 为什么存 sessionStorage 而不写进 URL：点代码链接时既要记住这只票、
   * 又要让 Link 完成跳转 —— 两条路径都走 router 会互相打架
   * （实测表现为「点一下只选中、要点两下才跳转」）。
   */
  const selectStock = useCallback(
    (code: string) => {
      setActive(code)
      try {
        sessionStorage.setItem(ACTIVE_KEY, code)
      } catch {
        // 存不进去不影响这一页的使用，只是从个股页退回来时会回到第一只
      }
    },
    [],
  )

  const keyLevels = useMemo<KeyLevel[]>(() => {
    const levels: KeyLevel[] = []
    const seen = new Set<string>()
    for (const item of current?.patterns ?? []) {
      const breakout = item.key_levels.breakout
      if (breakout != null) {
        const key = `b${breakout.toFixed(2)}`
        if (!seen.has(key)) {
          seen.add(key)
          levels.push({ label: '突破', value: breakout, kind: 'breakout' })
        }
      }
      const support = item.key_levels.support
      if (support != null) {
        const key = `s${support.toFixed(2)}`
        if (!seen.has(key)) {
          seen.add(key)
          levels.push({ label: '支撑', value: support, kind: 'support' })
        }
      }
    }
    return levels
  }, [current])

  /** 多选（并集）：点一下加入、再点一下移出；全移出就回到「全部」 */
  const toggle = (key: string) => {
    setPicked((prev) =>
      prev.includes(key) ? prev.filter((item) => item !== key) : [...prev, key],
    )
  }

  const toolbar = (
    <>
      <span className="num hidden text-[13px] text-fg-dim lg:inline">
        {loading ? '加载中…' : `${visible.length} / ${hits.length} 只`}
      </span>
      <select
        value={date ?? ''}
        onChange={(event) => setDate(event.target.value || null)}
        className="num border border-line bg-ink-900 px-2 py-[3px] text-[13px] text-fg outline-none focus:border-fg-dim"
      >
        <option value="">最新</option>
        {/* 倒序渲染：最近的排最上面。原先是升序，展开后要一路滚到底才够得着昨天 */}
        {[...dates].reverse().map((item) => (
          <option key={item} value={item}>
            {item}
          </option>
        ))}
      </select>
    </>
  )

  return (
    <Layout toolbar={toolbar}>
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}

      <div className="space-y-4">
        <Panel
          title="形态筛选"
          meta={
            <span className="num">
              {summary?.trade_date ? `${summary.trade_date} · ` : ''}
              共 {fmtInt(summary?.total_stocks ?? 0)} 只命中
              <span className="ml-3 text-fg-dim">
                点形态名筛选（可多选，只显示同时满足全部所选形态的票；再点一次取消）；
                没选形态时徽标是该形态的全市场家数，选了之后变成「再加上它会剩多少只」
                （标红＝加上它筛不到票）。致富＝悟道样板/启动
                ＋洗完（只扫创业板与科创板，且样板/启动另限「今天冲高过」的候选池），
                悟道之路＝强达型与华宝早期（同样只扫创业板与科创板）；金叉大阳（趋势）也只扫这两块；
                做T＝用户的「V5 日线做T」买点，深B/★B/双B 三档（全市场扫，档位在明细里看）；
                知行B1＝同花顺「知行超级B1」的 7 个子信号，「只认当天出的信号」（2026-10-15
                起；它们 10-13~10-14 那两天是「近 N 日出现过」的窗口口径，已改回）。注意
                「超牛股回踩白线B1」这一档很稀（全市场约 0.1 条/天），大多时候是空榜；
                <Link to="/wudao" className="text-fg-dim underline decoration-line underline-offset-2 hover:text-fg">
                  悟道之路单独一页
                </Link>
              </span>
            </span>
          }
          delay={40}
        >
          <div className="space-y-2 px-4 py-3">
            {groups.map(([group, items]) => (
              <div key={group} className="flex flex-wrap items-center gap-2">
                <span className="w-24 shrink-0 text-[13px] text-fg-dim">{group}</span>
                {items.map((item) => {
                  const on = picked.includes(item.key)
                  // 数字优先取 `/preview` 的交集家数（「再加上它会剩多少只」），
                  // 拿不到时退回 `/summary` 的全市场家数（见 `previewOf` 的说明）
                  const count = previewOf[item.key] ?? countOf[item.key] ?? 0
                  // 已选了别的形态时，徽标算的是「再点它会剩多少只」；为 0 说明这个
                  // 组合筛不到票 —— 标红，点之前就知道没戏（点进去就是空列表）
                  const empty = !on && picked.length > 0 && count === 0
                  // 悬停追加一行线上成绩：**只改提示，不动 chip 主数字** ——
                  // 那个数字是「今天命中多少只」，用户已经习惯了，换成超额会看错。
                  const stats = scoreOf.get(item.key)
                  const h10 = stats?.returns.find((row) => row.days === 10)
                  const perf = stats
                    ? `\n近 ${scorecard?.cohorts ?? 0} 个扫描日线上：10 日超额 ` +
                      `${fmtPct(h10?.excess ?? null)} · 跑赢 ${fmtNum(h10?.beat_pct ?? null, 0, '%')}` +
                      ` · 样本 ${stats.hits}`
                    : ''
                  return (
                    <button
                      key={item.key}
                      type="button"
                      onClick={() => toggle(item.key)}
                      title={
                        (picked.length > 0
                          ? `再加上「${item.name}」后剩 ${count} 只`
                          : `「${item.name}」全市场命中 ${count} 只`) + perf
                      }
                      className={`num border px-2 py-[2px] text-[13px] transition-colors ${
                        on
                          ? 'border-accent bg-accent/10 text-accent'
                          : empty
                            ? 'border-line text-fg-dim opacity-60 hover:border-fg-dim hover:text-fg'
                            : 'border-line text-fg-muted hover:border-fg-dim hover:text-fg'
                      }`}
                    >
                      {item.name}
                      {stats?.verdict === '噪声' && (
                        <span
                          className="ml-1 text-[11px] text-fg-dim"
                          title="近 N 日线上 10 日超额接近 0、样本已足够 —— 与噪声难以区分"
                        >
                          噪
                        </span>
                      )}
                      <span
                        className={`ml-1.5 text-[13px] ${
                          on ? 'text-accent' : empty ? 'text-danger' : 'text-fg-dim'
                        }`}
                      >
                        {count}
                      </span>
                    </button>
                  )
                })}
              </div>
            ))}
            <div className="flex flex-wrap items-center gap-3 border-t border-line-soft pt-2.5">
              {/* 板块：按代码号段判（见 lib/format.boardOf）。默认只勾创业板 + 科创板 —— 20cm */}
              <span className="text-[13px] text-fg-dim">板块</span>
              {BOARDS.map((name) => {
                const on = boards.includes(name)
                return (
                  <button
                    key={name}
                    type="button"
                    onClick={() =>
                      setBoards((prev) =>
                        prev.includes(name)
                          ? prev.filter((item) => item !== name)
                          : [...prev, name],
                      )
                    }
                    title={`${on ? '去掉' : '加上'}「${name}」（按代码号段判定，与悟道六池的板块关同一口径）`}
                    className={`num border px-2 py-[2px] text-[13px] transition-colors ${
                      on
                        ? 'border-accent bg-accent/10 text-accent'
                        : 'border-line text-fg-muted hover:border-fg-dim hover:text-fg'
                    }`}
                  >
                    {name}
                  </button>
                )
              })}
              {/* 窗口：>1 时后端把窗口内同一只票的重复命中合并成一条（见 `WINDOWS`）。
                  没选形态也能用（2026-10-15 起，原先这三个是禁用的） */}
              <span className="ml-4 text-[13px] text-fg-dim">窗口</span>
              {WINDOWS.map((value) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setDays(value)}
                  title={
                    value === 1
                      ? '只看当天命中的票'
                      : `把近 ${value} 个交易日内同一只票的重复命中合并成一条，日期取首次那天`
                  }
                  className={`num border px-2 py-[2px] text-[13px] transition-colors ${
                    days === value
                      ? 'border-accent bg-accent/10 text-accent'
                      : 'border-line text-fg-muted hover:border-fg-dim hover:text-fg'
                  }`}
                >
                  {value === 1 ? '当天' : `近${value}日`}
                </button>
              ))}
              <span className="ml-4 text-[13px] text-fg-dim">评分下限</span>
              <input
                type="range"
                min={0}
                max={100}
                step={5}
                value={minScore}
                onChange={(event) => setMinScore(Number(event.target.value))}
                className="range-accent h-[3px] w-40"
              />
              <span className="num w-8 text-[13px] text-fg">{minScore}</span>
              {(picked.length > 0 || minScore > 0 || phases.length > 0) && (
                <button
                  type="button"
                  onClick={() => {
                    setPicked([])
                    setMinScore(0)
                    setPhases([])
                  }}
                  className="num border border-line px-2 py-[2px] text-[13px] text-fg-dim hover:border-fg-dim hover:text-fg"
                >
                  清除筛选
                </button>
              )}
            </div>
            {/* 阶段判定（2026-10-20 用户要求：单独一行，分「明天预案 / 明天盯」）。
                多选 = 并集；筛选在**后端**做（先算全量命中的阶段再筛），不是在前端这几十行里筛。
                ⚠️ 只支持**最新交易日**：阶段与超跌都按库里最新的日线算，翻历史日期时
                候选池是那天的、判定却是今天的，两端不同日（后端会直接 400） */}
            <div className="flex flex-wrap items-center gap-3 border-t border-line-soft pt-2.5">
              <span className="text-[13px] text-fg-dim">阶段判定</span>
              {PHASES.map((item) => {
                const on = phases.includes(item.value)
                const off = date !== null
                return (
                  <button
                    key={item.value}
                    type="button"
                    disabled={off}
                    onClick={() =>
                      setPhases((prev) =>
                        prev.includes(item.value)
                          ? prev.filter((key) => key !== item.value)
                          : [...prev, item.value],
                      )
                    }
                    title={
                      off
                        ? '阶段判定 / 超跌只支持最新交易日（它们按最新日线计算）'
                        : `${on ? '去掉' : '只看'}「${item.label}」（悟道阶段判定，与个股页／表格里的「阶段判定」列同一判据）`
                    }
                    className={`border px-2 py-[2px] text-[13px] transition-colors ${
                      off
                        ? 'cursor-not-allowed border-line-soft text-fg-dim opacity-50'
                        : on
                          ? 'border-accent bg-accent/10 text-accent'
                          : 'border-line text-fg-muted hover:border-fg-dim hover:text-fg'
                    }`}
                  >
                    {item.label}
                  </button>
                )
              })}
              {date !== null ? (
                <span className="text-[12px] text-fg-dim">
                  看的是历史日期 · 阶段判定 / 超跌只能筛最新那天（点日期选「最新」）
                </span>
              ) : (
                phases.length > 0 && (
                  <span className="text-[12px] text-fg-dim">
                    {picked.length > 0
                      ? '超跌只在你选中的形态命中里筛（不补全市场）'
                      : '可多选 · 超跌取全市场（含今天没有形态的票），其余档在全市场命中里筛'}
                  </span>
                )
              )}
            </div>
          </div>
        </Panel>

        <Panel
          title={pickedLabel ? `命中列表 · ${pickedLabel}` : '命中列表'}
          meta={
            <span className="num">
              {pickedLabel && (
                <span className="text-accent">
                  {picked.length === 1
                    ? truncated
                      ? `该形态共 ${fmtInt(expected)} 只，只列出前 ${fmtInt(PATTERN_LIMIT)} 只`
                      : `该形态共 ${fmtInt(expected)} 只，已全部列出`
                    : truncated
                      ? `所选 ${picked.length} 个形态同时满足的超过 ${fmtInt(PATTERN_LIMIT)} 只，只列出前 ${fmtInt(PATTERN_LIMIT)} 只`
                      : `所选 ${picked.length} 个形态同时满足：${fmtInt(expected)} 只，已全部列出`}
                  {' · '}
                </span>
              )}
              点列头排序 · 点一行看它的 K 线与关键位
              {/* 说明 ST 是被**故意**筛掉的：不说的话，库里少了几百条命中会被当成采集出问题。
                  判据在后端 `app.services.patterns.is_st`（扫描与回测共用）。 */}
              <span
                className="cursor-help"
                title="2026-09-28 起：名称带 ST 的票不参与形态选股（扫描时就已剔除，历史命中里的也一起清掉了）"
              >
                {' '}· ST 已剔除
              </span>
              {visible.length > 0 && (
                <button
                  type="button"
                  onClick={copyCodes}
                  title="每行一个代码，可直接粘进同花顺的「导入自选股」"
                  className="ml-3 border border-line px-2 py-[2px] text-[13px] text-fg-dim transition-colors hover:border-fg-dim hover:text-fg"
                >
                  {copied ? '已复制' : `复制 ${visible.length} 个代码`}
                </button>
              )}
            </span>
          }
          delay={80}
        >
          {truncated && (
            <div className="flex items-start gap-2.5 border-b border-accent/25 bg-accent/[0.05] px-4 py-2.5 text-[13px] leading-relaxed text-fg-muted">
              <span className="mt-[3px] h-[6px] w-[6px] shrink-0 bg-accent" />
              <span>
                <span className="font-medium text-accent">列表被截断：</span>
                {pickedLabel ? (
                  <>
                    「{pickedLabel}」当日{picked.length === 1 ? '命中' : '同时满足的票'}超过
                    <span className="num text-fg"> {PATTERN_LIMIT} </span>
                    只，这里只列出前
                    <span className="num text-fg"> {PATTERN_LIMIT} </span>
                    只。
                  </>
                ) : phases.length > 0 ? (
                  <>
                    已按阶段筛选（
                    {PHASES.filter((item) => phases.includes(item.value))
                      .map((item) => item.label)
                      .join(' / ')}
                    ），筛后超过
                    <span className="num text-fg"> {PATTERN_LIMIT} </span>
                    只，这里只列出前
                    <span className="num text-fg"> {PATTERN_LIMIT} </span>
                    只。要看得更少，可以叠加形态名收窄。
                  </>
                ) : (
                  <>
                    当日共命中
                    <span className="num text-accent"> {fmtInt(expected)} </span>
                    只，这里只列出评分最高的
                    <span className="num text-fg"> {HIT_LIMIT} </span>
                    只，点列头排序也只在这
                    <span className="num text-fg"> {HIT_LIMIT} </span>
                    只里排 —— 不是全市场排名。点上面的形态名可以看该形态的
                    <span className="text-fg">全部</span>
                    命中。
                  </>
                )}
              </span>
            </div>
          )}

          {visible.length === 0 ? (
            <div className="px-4 py-6 text-center text-[13px] text-fg-dim">
              {loading ? '加载中…' : '当前筛选下没有命中的个股'}
            </div>
          ) : (
            <div className="max-h-[520px] overflow-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <SortTh sortKey="score" {...hitSort}>
                      评分
                    </SortTh>
                    <SortTh sortKey="code" align="left" {...hitSort}>
                      代码 / 名称
                    </SortTh>
                    {/* 只在窗口模式出现：那一屏各行的日期**各不相同**（取的是每只票
                        首次命中那天），日期藏在表头会看不出这一行到底是哪天 */}
                    {days > 1 && (
                      <th className="!text-left" title={signalDateTitle(days, picked.length > 0)}>
                        信号日
                      </th>
                    )}
                    {/* 「阶段判定」是 8 档枚举、没有单一可比值，不排序 */}
                    <th
                      className="!text-left"
                      title="悟道阶段判定（与个股页／个股分析页同一判据，取最近 130 根日线算）。日线不足 25 根的票留空「—」"
                    >
                      阶段判定
                    </th>
                    {/* 「板块」是一串名字、没有单一可比值，不排序（与「命中形态」同理） */}
                    <th
                      className="!text-left"
                      title="该股所属的开盘红精选板块 —— 口径与个股页「所属题材」一致：它最近一次涨停是因为哪个板块（不是它属于哪些概念）。没有涨停过就没有值，留空（覆盖约 75%）"
                    >
                      板块
                    </th>
                    {/* 「行业」同样不排序（完整值是最长 19 字的三级路径，没有单一可比值） */}
                    <th
                      className="!text-left"
                      title="同花顺行业（三级路径，如「房地产-房地产-住宅开发」）。列里只显示第一级，悬停看完整路径。它答的是「公司做什么生意」—— 与左边「板块」不是一个口径（那个答「最近一次涨停是因为哪个题材」）"
                    >
                      行业
                    </th>
                    {/* 「命中形态」是一串标签，没有单一可比值，不排序 */}
                    <th className="!text-left">命中形态</th>
                    <SortTh sortKey="close" {...hitSort}>
                      最新价
                    </SortTh>
                    <SortTh sortKey="pct_chg" {...hitSort}>
                      涨跌幅
                    </SortTh>
                    <SortTh sortKey="amount" {...hitSort}>
                      成交额
                    </SortTh>
                    <SortTh
                      sortKey="avg_amount"
                      {...hitSort}
                      title="近 20 日日均成交额：今天的量相对平时放大了多少"
                    >
                      日均额
                    </SortTh>
                    <SortTh sortKey="total_mv" {...hitSort} title="总市值：是题材小票还是权重">
                      市值
                    </SortTh>
                  </tr>
                </thead>
                <tbody>
                  {rendered.map((stock) => (
                    <tr
                      key={stock.code}
                      onClick={() => {
                        selectStock(stock.code)
                        // 顺手把这份命中列表也存下，个股页就能用 ← → 前后翻。
                        // 存的是 shown（已排序、未截断的全量）—— 翻页顺序跟着排序走，
                        // 且不会因为渲染截断而翻不到第 201 只
                        rememberStockList(shown.map((item) => item.code))
                      }}
                      className={`cursor-pointer ${
                        stock.code === active ? 'bg-accent/10' : ''
                      }`}
                    >
                      <td>
                        <span
                          className={`num ${
                            stock.score >= 95 ? 'text-accent' : 'text-fg-muted'
                          }`}
                        >
                          {stock.score.toFixed(1)}
                        </span>
                      </td>
                      <td className="!text-left">
                        {/* 这一层不加 onClick：让 Link 自己完成导航，记住这只票
                            交给事件冒泡到上面那行的 onClick（见 selectStock）。
                            在这里再碰一次 router 会和 Link 的导航打架 */}
                        <Link
                          to={`/stock/${encodeURIComponent(stock.code)}`}
                          className="hover:text-accent"
                        >
                          <span className="num text-fg-dim">{stock.code}</span>
                          <span className="ml-2 text-fg">{stock.name ?? '—'}</span>
                        </Link>
                      </td>
                      {days > 1 && (
                        <td className="!text-left">
                          <span
                            className="num text-[13px] text-fg-muted"
                            title={
                              stock.run_days > 1
                                ? `首次命中 ${stock.trade_date}，这一轮到 ${stock.last_date} 共命中 ${stock.run_days} 个交易日`
                                : `命中日 ${stock.trade_date}`
                            }
                          >
                            {fmtShortDate(stock.trade_date)}
                          </span>
                          {stock.run_days > 1 && (
                            <span className="num ml-1 text-[13px] text-fg-dim">
                              +{stock.run_days - 1}日
                            </span>
                          )}
                        </td>
                      )}
                      <td className="!text-left">
                        {/* 阶段判定只显示一个词（如「刚有人气」），颜色沿用卡片那一档
                            的色调（`phaseTone`，与 PhasePanel 同一个来源）；日线不够时留空 */}
                        {stock.phase_label ? (
                          <span
                            className={`whitespace-nowrap text-[13px] ${phaseTone(stock.phase)}`}
                            title={stock.phase ?? undefined}
                          >
                            {stock.phase_label}
                          </span>
                        ) : (
                          <span className="text-fg-dim">—</span>
                        )}
                      </td>
                      <td className="!text-left">
                        {/* 实测每只票恰好一个板块、名字最长 8 字（VR/AR/MR），所以不用截断；
                            一个都没有时留空 —— 那是「从没涨停过」，不是漏采 */}
                        {stock.sectors.length === 0 ? (
                          <span className="text-fg-dim">—</span>
                        ) : (
                          <span className="whitespace-nowrap text-[13px] text-fg-muted">
                            {stock.sectors.join(' / ')}
                          </span>
                        )}
                      </td>
                      <td className="!text-left">
                        {/* 完整值是三级路径、最长 19 字（「电力设备-其他电源设备-其他电源设备Ⅲ」），
                            铺在列里太占宽 —— 只显示**第一级**，完整路径放 tooltip。
                            建池 7 天一次、覆盖全 A，所以基本不会空 */}
                        {stock.industry ? (
                          <span
                            className="whitespace-nowrap text-[13px] text-fg-muted"
                            title={stock.industry}
                          >
                            {stock.industry.split('-')[0]}
                          </span>
                        ) : (
                          <span className="text-fg-dim">—</span>
                        )}
                      </td>
                      <td className="!text-left">
                        <div className="flex flex-wrap gap-1">
                          {stock.patterns.map((item) => (
                            <span
                              key={item.pattern}
                              className="num border border-line-soft px-1.5 py-[1px] text-[13px] text-fg-muted"
                            >
                              {item.pattern_name}
                              <span className="ml-1 text-fg-dim">
                                {item.score.toFixed(0)}
                              </span>
                            </span>
                          ))}
                        </div>
                      </td>
                      <td>
                        <span className="num">{stock.close?.toFixed(2) ?? '—'}</span>
                      </td>
                      <td>
                        <span className={`num ${toneOf(stock.pct_chg)}`}>
                          {fmtPct(stock.pct_chg)}
                        </span>
                      </td>
                      <td>
                        <span className="num text-fg-muted">{fmtAmount(stock.amount)}</span>
                      </td>
                      <td>
                        <span className="num text-fg-dim">{fmtAmount(stock.avg_amount)}</span>
                      </td>
                      <td>
                        <span className="num text-fg-dim">{fmtAmount(stock.total_mv)}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {hiddenCount > 0 && (
                <div className="border-t border-line-soft px-4 py-2 text-[12px] text-fg-dim">
                  另有 {fmtInt(hiddenCount)} 只未显示，可用上方筛选缩小范围
                </div>
              )}
            </div>
          )}
        </Panel>

        <Panel
          title="看图确认"
          meta={
            <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="num">
                {current
                  ? `${current.code} ${current.name ?? ''} · 前复权 · ${kline.bars.length} 根`
                  : '点上面任意一行'}
                <span className="ml-3 text-fg-dim">
                  画的是前复权序列，与引擎判定用的完全一致；虚线是形态关键位
                </span>
              </span>
              <Segmented value={kline.view} items={K_VIEWS} onChange={kline.setView} />
            </span>
          }
          delay={120}
        >
          {current && (
            <div className="border-b border-line-soft px-4 py-2.5">
              {current.patterns.map((item) => (
                <div key={item.pattern} className="flex flex-wrap items-baseline gap-x-3 py-0.5">
                  <span className="w-24 shrink-0 text-[13px] text-fg">
                    {item.pattern_name}
                  </span>
                  <span className="num w-10 shrink-0 text-[13px] text-accent">
                    {item.score.toFixed(1)}
                  </span>
                  {item.detail.candle_confirm && (
                    <span
                      className="shrink-0 border border-accent/50 bg-accent/10 px-1 text-[11px] text-accent"
                      title="命中日同时出现看涨吞没 —— K 线确认实验里唯一达到「10 日超额 +1pp 且样本 ≥200」的组合"
                    >
                      K线确认 · {item.detail.candle_confirm}
                    </span>
                  )}
                  <span className="num flex flex-wrap gap-x-3 text-[13px] text-fg-dim">
                    {Object.entries(item.detail)
                      // 已单独做成徽标，别再按普通明细重复列一遍
                      .filter(([key]) => key !== 'candle_confirm')
                      .map(([key, value]) => (
                        <span key={key}>
                          {DETAIL_FIELDS[key]?.[0] ?? key}
                          <span className="ml-1 text-fg-muted">{detailText(key, value)}</span>
                        </span>
                      ))}
                  </span>
                </div>
              ))}
            </div>
          )}
          <div className="px-2 pt-2">
            {kline.loading || kline.syncing ? (
              <div className="flex h-[380px] items-center justify-center text-[13px] text-fg-dim">
                {kline.syncing ? '正在补 2 年历史（周/月 K 要的长周期）…' : '加载日线…'}
              </div>
            ) : kline.error ? (
              <div className="flex h-[380px] items-center justify-center px-6 text-center text-[13px] text-danger">
                日线取数失败：{kline.error}
              </div>
            ) : (
              <KLineChart bars={kline.bars} height={380} keyLevels={keyLevels} />
            )}
          </div>
        </Panel>
      </div>
    </Layout>
  )
}
