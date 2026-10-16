"""API 响应模型。"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------- 指数


class IndexQuote(ApiModel):
    code: str
    name: str | None
    close: float | None
    pct_chg: float | None
    amount: float | None
    up_count: int | None
    down_count: int | None
    # limit_up_count / limit_down_count 已删（2026-10-11）：那是 iFinD 高频接口
    # 透传的涨停家数口径（实测仅上证有值，services/sentiment 自己都判它「严重
    # 低估」不可用），与情绪区的东财涨停池口径并存、却从未被前端消费 —— 留着
    # 只会哪天被误当成「涨停家数」用。库里的列保留（采集顺带写入，零额外配额）。
    # --- 以下由本地算法补上（`services/index_tech.py`，不落库）---
    ma5: float | None = None
    ma20: float | None = None
    above_ma5: bool | None = None
    above_ma20: bool | None = None
    # 今日成交额 / 前 5 日均额。>1.1 放量、<0.9 缩量
    vol_ratio: float | None = None
    # 量价配合标签：放量上涨 / 缩量下跌 / 平量横盘 …
    vol_price: str | None = None


# --------------------------------------------------------------------- 情绪


class SentimentOut(ApiModel):
    trade_date: date
    limit_up_count: int | None
    limit_down_count: int | None
    broken_count: int | None
    seal_rate: float | None
    broken_rate: float | None
    max_consecutive: int | None
    up_count: int | None
    down_count: int | None
    # 全市场口径的涨跌超 5% 家数（只有当日值）
    up5_count: int | None
    down5_count: int | None
    total_amount: float | None
    yesterday_limit_today_avg: float | None


class DivergenceOut(BaseModel):
    """指数与个股广度是否背离（当日）。

    `level` 供前端上色：`weight_pull`（权重拉抬）/ `theme_active`（题材活跃）/
    `aligned`（同向）。判据与阈值写在 `services/index_tech.py` 里，
    `detail` 把依据数字（指数涨跌幅、涨跌家数）带出来，结论不自成黑盒。
    """

    level: str
    title: str
    detail: str


class TurnoverSeries(BaseModel):
    """两市成交额的近期序列，只给复盘页的柱状图用。

    不复用 `SentimentOut` 列表：这里要的是「**截至所选交易日**」的那一段
    （翻到历史日期时不能把之后的日子也画出来），且只要图表用得上的三个数组。
    """

    dates: list[date]
    # 上证指数 + 深证成指 成交额（元）。**缺数的日子必须是 null**，
    # 不能补 0 —— 图上留空隙与「当天真的没成交」是两件事
    amounts: list[float | None]
    # 当日**上证指数**涨跌幅，只用来给柱子定红绿（红=涨、绿=跌），与 amounts 等长。
    # 注意它跟成交额自己涨跌无关：这是 K 线副图的老惯例，让柱子同时说「量价配合」
    pct_chg: list[float | None]


# ------------------------------------------------------------- 恐惧贪婪指数
#
# 口径与算法见 `services/fear_greed.py`：六项原始指标各自按**滚动 250 个交易日
# 分位**折算成 0-100，再等权平均；越高越贪婪。原始值存在 `fear_greed_daily`
# 表里，分数是读取时现算的 —— 所以改窗口或改分档不必回补数据。


class FearGreedBand(BaseModel):
    """一个档位的区间与名称。`key` 给前端映射配色，中文名给图例。"""

    key: str
    label: str
    lower: float
    upper: float


class FearGreedSub(BaseModel):
    """一项子指标：原始值 + 它在滚动窗口里的分位分。"""

    key: str
    label: str
    value: float | None
    score: float | None
    # 值的单位（`%` / `倍` / 空串），前端拼在数字后面
    unit: str
    # 一句话口径说明，给 tooltip 用
    hint: str


class FearGreedLatest(BaseModel):
    """最新一天的读数 + 六项明细。`change` 是较上一个**有效**分数的变化量。"""

    trade_date: date
    score: float | None
    level: str | None
    change: float | None
    subs: list[FearGreedSub]


class FearGreedBrief(BaseModel):
    """首页用的一小块读数：读数 + 五档色带（不展开六项明细）。

    色带的阈值一并带上（而不是让前端另写一份 25/45/55/75）：分档口径只有后端
    一处，首页与情绪周期页画的色带、指针位置因此严格一致。
    """

    trade_date: date
    score: float | None
    level: str | None
    change: float | None
    bands: list[FearGreedBand]


class FearGreedBenchmark(BaseModel):
    """叠在恐贪曲线上做对照的基准指数收盘点位。

    `close` 与 `FearGreedOut.dates` **严格等长**（缺日留 null），前端才能直接按序号对齐
    两条线 —— 让前端按日期 join 的话，两边取数窗口差一天就会整体错位。
    """

    code: str
    name: str
    close: list[float | None]


class FearGreedOut(BaseModel):
    """恐贪指数序列（0-100，越高越贪婪），喂折线图。

    `scores` 与 `dates` 严格等长，算不出来的日子是 null（图上留空隙，而不是画成 0 —
    — 0 是「极度恐慌」，那是截然不同的结论）。

    `bands` 是分档阈值（后端是唯一出处），前端据此画五档色带与分档横线。
    六项明细只给最新一天：`days` 天内每天都带六个分项会让响应体从几 KB 涨到几百 KB，
    而中间那些天的分项页面上并不展示。
    """

    dates: list[date]
    scores: list[float | None]
    bands: list[FearGreedBand]
    # 分位窗口（交易日），口径说明里要写出来
    window: int
    # 表内总天数：说明这个读数背后有多少历史，也是「数据够不够用」的判据
    coverage: int
    latest: FearGreedLatest | None
    # 叠加对照的基准指数（上证指数）收盘点位。库里该区间没有指数数据时为 null
    benchmark: FearGreedBenchmark | None = None


# ----------------------------------------------------------- 市场宽度温度计
#
# 原始值由 `jobs/collect_breadth.py` 落库（`breadth_daily` 表），读取侧不做二次
# 加工 —— 与恐贪「原始值落库」同一个约定，但这里读的就是原始值本身。


class BreadthLatest(BaseModel):
    """最新一天的市场宽度读数（页面顶部的 chips）。"""

    trade_date: date
    above_ma20: float | None
    above_ma60: float | None
    above_ma250: float | None
    high60: int | None
    low60: int | None
    high250: int | None
    low250: int | None
    universe_count: int | None


class BreadthOut(BaseModel):
    """市场宽度温度计序列：站上 MA20/60/250 的占比 + 创 60/250 日新高新低家数。

    各数组与 `dates` **严格等长**，缺数的日子是 null：
    - `above_ma250` 在站内 K 线历史还不够 250 个交易日时**整列为 null**（不满 N 根
      K 线的票不进分母，见 `jobs/collect_breadth.py` 的口径说明）；
    - 新高新低是**家数**（整数），不是占比。
    """

    dates: list[date]
    above_ma20: list[float | None]
    above_ma60: list[float | None]
    above_ma250: list[float | None]
    high60: list[int | None]
    low60: list[int | None]
    high250: list[int | None]
    low250: list[int | None]
    # 表内总天数：说明这条曲线背后有多少历史
    coverage: int
    latest: BreadthLatest | None


# ------------------------------------------------------------- 短线接力温度
#
# 评分算法与分档见 `services/relay_temp.py`：六项打板生态指标各自按**固定阈值**
# 折算成 0-100 再等权平均（不用滚动分位 —— 打板数据积累历史短，且这些量天然
# 有刻度）。不建表，序列在 API 读取时现算。


class RelayTempBand(BaseModel):
    """一个档位的区间与名称（与 `FearGreedBand` 同构，前端复用配色映射）。"""

    key: str
    label: str
    lower: float
    upper: float


class RelayTempSub(BaseModel):
    """一项子指标：原始值 + 它按固定阈值折算的 0-100 分。"""

    key: str
    label: str
    value: float | None
    score: float | None
    unit: str
    hint: str


class RelayTempLatest(BaseModel):
    """最新一天的读数 + 六项明细。"""

    trade_date: date
    score: float | None
    level: str | None
    change: float | None
    subs: list[RelayTempSub]


class RelayTempOut(BaseModel):
    """短线接力温度序列（0-100，越高越热），喂折线图。

    `scores` 与 `dates` 严格等长，有效子指标不足的日子是 null（图上留空隙）。
    没有 `window`：固定阈值映射，不看历史分位，也就无所谓窗口。
    """

    dates: list[date]
    scores: list[float | None]
    bands: list[RelayTempBand]
    coverage: int
    latest: RelayTempLatest | None


# ------------------------------------------------------------- 期指年化基差
#
# 原始值由 `jobs/collect_futures_basis.py` 落库（`futures_basis_daily` 表，
# 一行 = 一天 × 一个品种），读取侧不做二次加工。


class FuturesBasisLatest(BaseModel):
    """最新一天一个品种的基差读数（页面顶部的 chips）。"""

    trade_date: date
    code: str
    name: str | None
    # (期货 − 现货) / 现货 × 100（%）。负值 = 贴水（对冲压力）
    basis_pct: float | None
    # 年化基差（%）：basis × 365 / 距交割日天数
    annual_pct: float | None
    days_to_expiry: int | None


class FuturesBasisSeries(BaseModel):
    """一个品种的年化 / 原始基差序列。各数组与 `FuturesBasisOut.dates` 严格等长，
    缺数的日子是 null（期现有一侧缺价的交易日整天没采到，图上断线、不补 0）。"""

    code: str
    name: str | None
    annual_pct: list[float | None]
    basis_pct: list[float | None]


class FuturesBasisOut(BaseModel):
    """期指年化基差序列：IF / IH / IC / IM 四品种各一条年化线。"""

    dates: list[date]
    series: list[FuturesBasisSeries]
    # 表内总天数（按 trade_date 去重）：说明曲线背后有多少历史
    coverage: int
    latest: list[FuturesBasisLatest]


# ------------------------------------------------------------------ 中国波指 QVIX
#
# 原始值由 `jobs/collect_qvix.py` 落库（`volatility_daily` 表）。**不是交易所官方
# iVX**（那条 2018 年停更）—— 数据由期权论坛按方差互换思路合成，站内只落库与读取。


class VolatilityLatest(BaseModel):
    """最新一天一个品种的波动率读数（页面顶部的 chips）。"""

    trade_date: date
    code: str
    name: str | None
    # QVIX 收盘 = 隐含波动率（百分数，17.95 表示 17.95%）
    close: float | None
    # 较前一交易日的涨跌幅（%）—— 波动率自己的跳变幅度本身就是信息
    chg: float | None
    # 在滚动 window 个交易日里的分位（0-100）
    percentile: float | None


class VolatilitySeries(BaseModel):
    """一个品种的 QVIX 序列。数组与 `VolatilityOut.dates` 严格等长。"""

    code: str
    name: str | None
    close: list[float | None]


class VolatilityOut(BaseModel):
    """中国波指（QVIX）序列：50ETF / 300ETF 各一条隐含波动率线。

    ⚠️ 数据由期权论坛合成，加权方案不在站内（见 `jobs/collect_qvix.py` 的取舍说明）。
    """

    dates: list[date]
    series: list[VolatilitySeries]
    # 分位窗口（交易日）
    window: int
    # 表内总天数（按 trade_date 去重）
    coverage: int
    latest: list[VolatilityLatest]
    # 上证指数收盘点位（与 dates 等长），叠图看「指数跌 → 波动率升」的反向关系
    benchmark: FearGreedBenchmark | None


# --------------------------------------------------------------------- ERP
#
# 原始值由 `jobs/collect_erp.py` 落库（`erp_daily` 表）；历史分位读取时现算，
# 复用恐贪的滚动 250 交易日中位秩分位（`services/fear_greed.percentile_score`）。


class ErpLatest(BaseModel):
    """最新一天的 ERP 读数（页面顶部的 chips）。"""

    trade_date: date
    erp: float | None
    # 滚动 250 交易日分位（0-100）。ERP 越高 = 股票相对债券越便宜
    percentile: float | None
    pe_ttm: float | None
    bond_yield: float | None


class ErpOut(BaseModel):
    """ERP 股债性价比序列：ERP（百分点）+ 它的滚动分位 + PE-TTM + 10Y 国债。

    各数组与 `dates` 严格等长；PE 或国债缺一项的交易日整天没落库，
    所以数组里没有半行 null 的日子（日期本身缺）。
    """

    dates: list[date]
    erp: list[float | None]
    percentile: list[float | None]
    pe_ttm: list[float | None]
    bond_yield: list[float | None]
    # 分位窗口（交易日），口径说明里要写出来
    window: int
    # 表内总天数：说明这条曲线背后有多少历史
    coverage: int
    latest: ErpLatest | None


# ---------------------------------------------------------- NH-NL 新高新低扩散
#
# 原始值（创 60/250 日收盘新高新低家数）读 `breadth_daily`（宽度温度计采集 job
# 落库的那张表），diff 与累计净值在 API 读取时现算（见 `services/nhnl.py`），不建新表。


class NhNlLatest(BaseModel):
    """最新一天的高新低读数（页面顶部的 chips）。"""

    trade_date: date
    high60: int | None
    low60: int | None
    # 新高 − 新低。任一侧缺数为 null（不补 0：0 是「打平」，是真实结论）
    diff60: int | None
    high250: int | None
    low250: int | None
    diff250: int | None


class NhNlOut(BaseModel):
    """NH-NL 新高新低扩散序列：每日差值 + 差值的累计净值（类 AD 线）。

    各数组与 `dates` 严格等长；250 日口径在站内 K 线历史还不够 250 个交易日时
    **整列为 null**（与宽度温度计同口径），图上断线展示。`benchmark` 是上证指数
    收盘点位（与 `dates` 等长），叠在净值线上看顶底背离。
    """

    dates: list[date]
    diff60: list[int | None]
    diff250: list[int | None]
    # diff 的逐日累计（从 0 起）；diff 缺数的日子为 null，累加器保持不动
    cum60: list[float | None]
    cum250: list[float | None]
    # 表内总天数：说明这条曲线背后有多少历史
    coverage: int
    latest: NhNlLatest | None
    benchmark: FearGreedBenchmark | None


# ---------------------------------------------------------------- 量能潮 OBV
#
# 原始值是基准指数（上证指数，与恐贪基准同源）的日线涨跌幅与成交额，读
# `index_daily`，OBV 与成交额分位在 API 读取时现算（见 `services/obv.py`），不建新表。


class ObvLatest(BaseModel):
    """最新一天的量能潮读数（页面顶部的 chips）。"""

    trade_date: date
    # 当日成交额（元）
    amount: float | None
    # 当日 OBV 变动额（元，带正负；平盘为 0）
    obv_change: float | None
    # OBV 累计净额（元，从 0 起累加）
    obv: float | None
    # 成交额在滚动 250 个交易日里的分位（0-100）
    percentile: float | None


class ObvOut(BaseModel):
    """量能潮 OBV 序列：累计净额 + 成交额 + 成交额的滚动 250 日分位。

    各数组与 `dates` 严格等长；成交额缺失的日子 `obv` 为 null（图上断线、
    不补 0），分位历史不足（窗口内有效值 < 20）的日子 `percentile` 为 null。
    """

    dates: list[date]
    obv: list[float | None]
    # 当日变动额（元，带正负；平盘为 0，成交额缺失为 null）
    obv_change: list[float | None]
    amount: list[float | None]
    percentile: list[float | None]
    # 分位窗口（交易日），口径说明里要写出来
    window: int
    # 表内总天数：说明这条曲线背后有多少历史
    coverage: int
    latest: ObvLatest | None


# ---------------------------------------------------------------- ETF 净申赎
#
# 原始值是 `etf_share` 的逐日份额与收盘价；净申赎 = Σ(份额变化 × 当日收盘价)，
# 读取时现算（见 `services/etf_flow.py`），不建新表。
# **金额单位一律是元**（与 `api/funds` 的约定一致，前端负责换算成亿）。
class EtfFlowLatest(BaseModel):
    """最近一个可算日的净申赎读数（页面顶部的 chips）。"""

    trade_date: date
    # 当日净申赎（元，带正负；净赎回为负）
    net: float | None
    # 这笔净额跨了几个交易日：正常相邻两天为 1；> 1 说明中间有覆盖不足的日子
    # （跨境/QDII 的滞后快照日），净额是「自上一个覆盖达标日以来」的累计
    span: int | None
    # 窗口内逐日净申赎的累计（元，从窗口首日起算）
    cumulative: float | None
    # 近 N 个有数的交易日累计净申赎（元），任一天缺数则为 null
    sum5: float | None
    sum20: float | None


class EtfFlowOut(BaseModel):
    """ETF 净申赎序列：单日净额 + 窗口内累计 + 基准指数。

    各数组与 `dates` 严格等长。**日期按交易日历铺，净额只在两个「覆盖达标」的
    交易日之间算**（达标 = 在日历里且快照不少于 `min_rows` 只）—— 覆盖不足的日子
    整点留 null（图上断线、不补 0：断口是缺数据，0 是「一分钱没进」）。
    """

    dates: list[date]
    # 单日净申赎（元，带正负）
    net: list[float | None]
    # 每一天的净额所跨越的交易日数（1 = 相邻两天）
    span: list[int | None]
    # 窗口内逐日累计（元，从 0 起；缺数的日子为 null，累加器保持不动）
    cumulative: list[float | None]
    # 「覆盖达标」的行数下限，口径说明里要写出来
    min_rows: int
    # 表内覆盖达标的交易日数（不是窗口长度）
    coverage: int
    latest: EtfFlowLatest | None
    benchmark: FearGreedBenchmark | None


# ---- 两融杠杆温度


class MarginTempLatest(BaseModel):
    """两融杠杆温度的最新一天读数。金额单位统一为**亿元**（原始口径，接口层不换算）。"""

    trade_date: date
    # 沪 + 深合计两融余额（亿元）
    balance: float | None
    # 较前一交易日的增减（亿元，带正负）
    balance_chg: float | None
    # 20 日变化率（%）
    chg20: float | None
    # 杠杆温度：20 日变化率在滚动窗口里的中位秩（0-100）
    temperature: float | None


class MarginTempOut(BaseModel):
    """两融杠杆温度序列：余额走势 + 20 日变化率 + 分位温度。

    各数组与 `dates` 严格等长。余额缺失（两所只要有一所没披露就跳过该日，避免把
    「深市待披露」当成「深市归零」）的日子整点留 null，图上断线、不补 0。
    """

    dates: list[date]
    # 沪深合计两融余额（亿元）
    balance: list[float | None]
    # 20 日变化率（%）
    chg20: list[float | None]
    # 温度：chg20 在滚动 `window` 个观测窗口里的中位秩（0-100）
    temperature: list[float | None]
    # 温度的分位窗口（交易日）
    window: int
    # 变化率回溯的观测数
    chg_days: int
    # 表内可用交易日数（不是窗口长度）
    coverage: int
    latest: MarginTempLatest | None


# ---- 行业宽度


class IndustryBreadthItem(BaseModel):
    """一个一级行业的宽度。"""

    industry: str
    # 该行业当日有效家数（有收盘价且 K 线数 ≥ ma_window）
    total: int
    # 站上均线的家数
    above: int
    # 站上占比（%）
    ratio: float | None


class IndustryBreadthOut(BaseModel):
    """行业宽度（当日横截面）：各一级行业「站上 MA20」的个股占比。

    横截面读数，不是时间序列（逐日算 250 天要好几秒，会把整页卡住，见端点说明）。
    """

    trade_date: date
    ma_window: int
    # 按占比降序；占比不可比的行业排最后
    items: list[IndustryBreadthItem]
    # 全市场站上占比（各行业合计口径）
    overall: float | None
    # 各行业占比的中位数
    median: float | None
    # 纳入统计的个股数
    coverage: int


# ---- 北向活跃度


class NorthboundLatest(BaseModel):
    """北向活跃度的最新读数。⚠️ 只有成交额、**没有净流入**（交易所 2024-08 起停披露）。"""

    trade_date: date
    # 沪深股通成交额（亿元）
    turnover: float | None
    # 占两市成交额（%）
    ratio: float | None
    # 近 5 个交易日占比均值（%）
    ratio_ma5: float | None
    # 近 20 个交易日占比均值（%）
    ratio_ma20: float | None


class NorthboundOut(BaseModel):
    """北向活跃度：沪深股通**成交额**占两市成交额的比例（%）。

    日期骨架取 `market_sentiment`；**通道关闭日没有北向数据，那个点记 null、图上断开**
    —— 断口是「没数据」，不是「北向没交易」。
    """

    dates: list[date]
    # 北向成交额 ÷ 两市成交额（%）
    ratio: list[float | None]
    # 沪深股通成交额（亿元）
    turnover: list[float | None]
    # 有北向数据的交易日数（不是窗口长度）
    coverage: int
    latest: NorthboundLatest | None
    # 上证指数收盘点位（与 dates 等长），叠图看外资参与度与点位的背离
    benchmark: FearGreedBenchmark | None


class MarketOverview(BaseModel):
    trade_date: date
    indexes: list[IndexQuote]
    sentiment: SentimentOut | None
    # 背离判断。历史日期没有涨跌家数（数据源只给当日），这里是 null
    divergence: DivergenceOut | None = None
    # 成交额柱状图的尾巴（截至 trade_date），没有情绪数据时为 null
    turnover: TurnoverSeries | None = None
    # 恐惧贪婪指数的当日简报。截至 `trade_date` 算（翻历史日期时不会泄露未来），
    # 库里还没这天数据、或算不出分时为 null
    fear_greed: FearGreedBrief | None = None


class IndexSeries(BaseModel):
    """一条指数序列。各数组与 IndexHistory.dates 一一对应，缺失为 null。"""

    code: str
    name: str | None
    close: list[float | None]
    pct_chg: list[float | None]
    amount: list[float | None]


class IndexHistory(BaseModel):
    """指数历史序列，按日期升序对齐，直接喂给图表。"""

    dates: list[date]
    series: list[IndexSeries]


# --------------------------------------------------------------- 涨停板三池


class LimitStock(ApiModel):
    code: str
    name: str | None
    pct_chg: float | None
    price: float | None
    amount: float | None
    float_mv: float | None
    total_mv: float | None
    turnover: float | None
    seal_amount: float | None
    first_seal_time: str | None
    last_seal_time: str | None
    open_times: int | None
    consecutive: int | None
    industry: str | None
    # 开盘啦**精选板块**名，来自涨停天梯的落库结果（`stock_concept`）。
    # 与 `industry` 是两个口径：industry 是 iFinD 的同花顺行业（「半导体」），
    # 这个是开盘啦 App 那张天梯的口径（「芯片」）。`stock_concept` 只覆盖涨停股，
    # 非涨停池的票恒为 None —— 不知道就留空，不拿 industry 顶上（混口径比空着更糟）。
    board: str | None = None
    # **涨停原因**，来自同花顺涨停池的 `reason_type`（`limit_reason` 表），
    # 形如「房地产+城市更新+北京国资」。原样展示，不拆分。
    reason: str | None = None
    # 「几天几板」：最近一段的**交易日跨度**与**涨停次数**（`5天3板` → 5 / 3）。
    # 只在涨停次数 ≥ 2 时给（首板由 `consecutive` 表达，这里不重复）。
    # 口径见 `api/limit._board_sequences`；目前只有涨停池（type=up）会填。
    board_days: int | None = None
    board_count: int | None = None


class LadderLevel(BaseModel):
    """涨停梯队的一层。"""

    consecutive: int
    count: int
    stocks: list[LimitStock]


class LimitPoolOut(BaseModel):
    trade_date: date
    pool_type: str
    total: int
    # 梯队分层只对涨停有意义：跌停是「连续跌停」、炸板没有连板数
    ladder: list[LadderLevel] | None
    stocks: list[LimitStock]


# ------------------------------------------------------------------ 涨停股票池


class LadderBoardStock(LimitStock):
    """涨停股票池里的一只票：比通用 `LimitStock` 多一个**今日状态**。

    行是按**昨日**连板档分组的，所以行内会同时出现「今天还在涨停的」和
    「今天炸板的」——`status` 就是用来区分它们的。
    """

    #: `up` = 今日涨停（晋级成功）｜`broken` = 今日炸板（失败）｜`other` = 今日两者都不是
    status: str


class LadderBoardRow(BaseModel):
    """涨停股票池的一行 = 「昨日该连板档」的一批候选票。

    ⚠️ 基准是**昨日**的涨停池，不是今日：
    `level=3` 这一行读作「3进4」——昨日 3 板的那批票，今天接住了几只。
    这与 `/limit/pool` 的 `ladder`（按**今日**连板高度分层、只含今日涨停票）
    是两个视角，别混。
    """

    #: 昨日连板档。**0 表示「昨日非涨停」**（今日首板行）
    level: int
    #: 展示用标签：`3进4` / 首板行固定 `首板`
    label: str
    #: 候选数 = **昨日该档全部**家数（与 `/limit/promotion` 的分母同一口径）
    base: int
    #: 其中今日涨停晋级数
    promoted: int
    #: 晋级率（百分比）。base 为 0 时 null，别写成 0 —— 0 会被读成「全军覆没」
    rate: float | None
    stocks: list[LadderBoardStock]


class LadderBoardOut(BaseModel):
    trade_date: date
    #: 对比基准日（上一交易日）。为空说明库里没有昨日涨停池，晋级无从谈起
    prev_date: date | None
    rows: list[LadderBoardRow]


class PromotionLevel(BaseModel):
    """某一档连板的晋级情况。各数组与 PromotionSeries.dates 一一对应。"""

    level: int
    label: str
    # 昨日该档的股票数
    counts: list[int]
    # 其中今日晋级到下一档的数量
    promoted: list[int]
    # 晋级率（百分比），昨日该档无票时为 null
    rates: list[float | None]


class PromotionSeries(BaseModel):
    """连板晋级率序列。打板复盘的核心指标。"""

    dates: list[date]
    levels: list[PromotionLevel]
    overall_counts: list[int]
    overall_promoted: list[int]
    overall_rates: list[float | None]


# ------------------------------------------------------------------ 龙虎榜


class LhbOut(ApiModel):
    trade_date: date
    code: str
    name: str | None
    reason: str
    close: float | None
    pct_chg: float | None
    net_buy: float | None
    buy_amount: float | None
    sell_amount: float | None
    interpretation: str | None


# -------------------------------------------------------------------- 板块


class SectorQuote(BaseModel):
    """板块排行里的一行。精选与行业一套结构，没有的字段留 null。

    ⚠️ 换开盘红之后**只有 4 个字段真的有值**：名称 / 涨跌幅 / 成交额 / 强度。
    净流入、涨跌家数、领涨股、成分股数在它的板块行里没有可反解的对应列，
    一律是 null（见 `sources/kaipanhong.py` 顶部）—— 本就没有的字段不能给 0。
    """

    code: str
    name: str
    taxonomy: str
    # 开盘啦的强度值，也是它 App 里板块榜的排序依据。**量纲不可跨口径比**
    strength: float | None
    pct_chg: float | None
    # 由已落库的历史复利算出，历史不够 N 个交易日时为 null
    pct_chg_5d: float | None
    amount: float | None
    net_inflow: float | None
    up_count: int | None
    down_count: int | None
    member_count: int | None
    leader_name: str | None
    leader_pct_chg: float | None
    # 当日涨停家数。走「个股 → 精选板块」归属聚合，只有精选口径有；
    # 行业为 null（涨停天梯只给精选板块的归属）
    limit_up_count: int | None


class SectorRanking(BaseModel):
    trade_date: date
    taxonomy: str
    total: int
    # 当日无涨跌幅（数据源还没更新）的板块数，用来提示数据不完整
    missing: int
    # 当日取自 iFinD 兜底口径（成分股加权平均）的板块数。同花顺概念指数
    # 当天拿不到，这些行的数字与同花顺官网可能差 0.5~2 个百分点，次日会订正。
    estimated: int
    boards: list[SectorQuote]


class SectorSeries(BaseModel):
    """单个板块的日线序列，用于板块详情走势图。"""

    code: str
    name: str
    taxonomy: str
    dates: list[date]
    pct_chg: list[float | None]
    amount: list[float | None]


class SectorCompareSeries(BaseModel):
    """对比图里的一条线。values 是以 base 为起点复利出来的净值，非真实指数点位。"""

    code: str
    name: str
    taxonomy: str
    values: list[float | None]


class SectorCompare(BaseModel):
    dates: list[date]
    # 归一化基准，各条线都从它起步，所以纵轴只能看相对强弱
    base: float
    series: list[SectorCompareSeries]


class SectorMemberItem(BaseModel):
    code: str
    name: str | None
    close: float | None
    pct_chg: float | None
    amount: float | None
    turnover: float | None
    # 当日连板数；没涨停时为 null（不是 0，0 会被读成「首板」）
    consecutive: int | None


class SectorMembers(BaseModel):
    trade_date: date
    sector_code: str
    sector_name: str
    taxonomy: str
    # 成分股数。开盘红一次给全量，所以就是取到几只
    member_count: int | None
    # 旧字段：iFinD 选股接口有 100 行上限，换开盘红之后不再有，恒为 false。
    # 保留是为了不动前端类型（前端仍拿它决定要不要显示「只显示前 100 只」的提示）
    truncated: bool
    # 取不到成分股时的原因，能取到就是 null。当日必然取不到 —— 开盘红的成分股
    # 接口只服务历史日期，页面要如实说明，不能显示成「该板块没有成分股」
    note: str | None = None
    members: list[SectorMemberItem]


class FundFlowItem(BaseModel):
    """板块资金流里的一行（**开盘啦板块口径**）。

    金额单位是**亿元**（库里 `sector_daily.net_inflow` 存的是元，接口层换算），
    字段名不带单位，所以口径写在这里 —— 免得有人按元去算，差 1e8 倍。

    `in_amount` / `out_amount` 恒为 None：净流入是「成分股主力净流入之和」，
    **没有「流入 / 流出」这个拆法**（见 `jobs/collect_board_flow.py`）。
    """

    name: str
    pct_chg: float | None
    in_amount: float | None
    out_amount: float | None
    net_amount: float | None
    member_count: int | None
    leader_name: str | None
    leader_pct_chg: float | None


class SectorFundFlowOut(BaseModel):
    """板块资金流向（页面上的排行榜 + 条形图）。

    `taxonomy` 与板块页**同一套**（`kph_selected` 精选 / `kph_industry` 行业）——
    2026-09-23 之前这里是同花顺口径，与站内板块不是一套名字，换掉之后
    `name` 可以直接和板块页的板块对上（但仍然只给名字、不给代码：曲线按名字画）。

    名单已剔掉两类板块：`BOARD_EXCLUDE` 关键词命中的（业绩 / 地域 / 事件类）与
    **成分股多于 `excluded_members_over` 的宽泛板块**（后者是量纲判据，阈值是配置项，
    所以随响应回传，见 `Settings.board_flow_max_members`）。
    """

    trade_date: date
    taxonomy: str
    taxonomy_label: str
    total: int
    # 成分股超过这个数的板块已被剔掉（配置项，前端据它写文案，别硬写数字）
    excluded_members_over: int
    # 已按净额降序（净流入在前），前端自己切 top / bottom
    items: list[FundFlowItem]


class FundFlowSeries(BaseModel):
    """一条累计净流入曲线（一个板块）。

    `values` 与 `FundFlowHistoryOut.dates` **等长**，单位同样是亿元，存的是**累计值**
    （从窗口起点起加总），不是当日值 —— 曲线看的就是「这十几天资金净流进/流出多少」。

    三个取值规则（后端算好，前端不要再动）：

    - **起点之前是 `null`**：该板块在窗口前几天还没进过榜（来源每天返回的板块集合会
      有出入），画成 0 就成了「那几天不流入不流出」，是假的。
    - **中间缺的那天顺延**：板块存在但那天没出现在快照里，累计值保持不变（不是归零、
      也不是断线）。
    - 一旦有第一个数据点，后面就不会再出现 `null`。
    """

    name: str
    values: list[float | None]


class FundFlowHistoryOut(BaseModel):
    """板块资金流的**多日累计**曲线（页面下方的折线图）。"""

    taxonomy: str
    taxonomy_label: str
    # 升序（左旧右新），与 series 里每条曲线的 values 一一对应
    dates: list[date]
    # 已按「窗口内累计净额的绝对值」降序 —— 动得最狠的排前面，颜色也就固定了
    series: list[FundFlowSeries]
    # 库里实际有几个交易日。**少于 2 天时前端要提示**：一个点连不成线
    days: int


class FundFlowMatrixCell(BaseModel):
    """资金流矩阵里的一格：某天净流入的第 N 名。

    `net_amount` 单位是**亿元**（与 `FundFlowItem` 同一套），正值是净流入。
    带 `code` 是为了让格子能点进板块详情 —— 与轮动矩阵的 `RotationCell` 一样。
    """

    code: str
    name: str
    net_amount: float
    pct_chg: float | None


class FundFlowMatrixColumn(BaseModel):
    """矩阵的一列 = 一个交易日的净流入前 N 名。"""

    trade_date: date
    cells: list[FundFlowMatrixCell]


class FundFlowMatrixOut(BaseModel):
    """板块资金流的**多日矩阵**：列是交易日（从新到旧）、行是当日的第 N 名。

    与 `/rotation` 那张矩阵是同一套版式（前端两块表上下对齐、同一天落在同一列），
    区别只在排序指标 —— 这里固定按 `net_inflow` **降序**（净流入榜的多日版；
    净流出就是同一张榜的另一头，前端用红绿区分方向即可）。
    """

    # **实际数据日**（可能早于请求日，同 `/fund-flow`）；库里有几天由 columns 长度体现
    trade_date: date
    taxonomy: str
    taxonomy_label: str
    top: int
    columns: list[FundFlowMatrixColumn]


class SectorHeatItem(BaseModel):
    """板块热力里的一格。只带一眼要看的信息，不带全套字段。"""

    code: str
    name: str
    pct_chg: float | None
    amount: float | None
    limit_up_count: int | None


class SectorHeatGroup(BaseModel):
    taxonomy: str
    total: int
    rising: int
    falling: int
    average: float | None
    leaders: list[SectorHeatItem]
    laggards: list[SectorHeatItem]


class SectorHeat(BaseModel):
    """首页「板块热力」：精选与行业各一组强弱概览。"""

    trade_date: date
    # 字段名从 concept 改成 selected：口径换成了开盘红的「精选板块」，
    # 继续叫 concept 会让「概念」这个已经不存在的东西留在接口里
    selected: SectorHeatGroup
    industry: SectorHeatGroup


class RotationCell(BaseModel):
    """轮动矩阵里的一格：某天排名第 N 的板块。

    `value` 是**排序所依据的那个指标**（量能=成交额、强度=涨跌幅），
    `pct_chg` 另外带上，因为按成交额排序时格子里的数字也该按涨跌上色。
    """

    code: str
    name: str
    value: float | None
    pct_chg: float | None


class RotationLeader(BaseModel):
    """某个板块当天的领涨个股（矩阵里「领涨」那一行的一只）。

    ⚠️ 口径是「该板块**当日的涨停股**」，来自开盘红的涨停天梯（`stock_concept`），
    **不是**开盘啦那种「当日涨幅前 5 名」。后者的精确做法只有逐个板块调成分股接口
    （`ZhiShuStockList_W8`），20 列就是 20 次按需请求、首次要约 7 秒 ——
    为一行次级信息不值当。涨停股对短线也更直接，而且是零额外请求（数据已在库里）。
    """

    code: str
    name: str | None
    # 当日连板数。1 = 首板；拿不到（不在涨停池里）时是 null
    consecutive: int | None


class RotationLeaderDay(BaseModel):
    """「领涨」行的一天：某板块在该交易日的涨停股（按连板数 → 封板时间排序）。

    单独一个接口（不在 `/rotation` 里）是因为这一行**跟着选中的板块走**：
    点一次格子就换一次，而矩阵本身不变。塞进 `/rotation` 的话，前端每次点格子都得
    重取整个矩阵，而那个面板一进 loading 就整块变成「加载中…」—— 点一下闪一下。
    """

    trade_date: date
    # 该板块当天没有涨停股就是空列表（页面显示「—」）
    leaders: list[RotationLeader] = []


class RotationColumn(BaseModel):
    """一列 = 一个交易日，格子按指标从高到低排。"""

    trade_date: date
    cells: list[RotationCell]


class SectorRotation(BaseModel):
    """板块轮动矩阵：每列一天、每行当天的第 N 名。

    列是**从新到旧**排的（最新在最左）—— 盯盘时先看当天，再往左回溯。
    """

    taxonomy: str
    metric: str
    metric_label: str
    top: int
    columns: list[RotationColumn]


# ------------------------------------------------------------------ 涨停题材


class LimitThemeItem(BaseModel):
    """一个题材（概念板块）当日聚集了多少只涨停股。"""

    concept: str
    count: int
    pct_chg: float | None


class LimitStockTheme(BaseModel):
    """单只涨停股挂的题材。按板块当日涨跌幅降序，最热的排前面。

    返回**全部**题材，展示时再截断 —— 前端要按这个列表做题材筛选，
    截断了就会出现「标签写着 10 只涨停、点进去只剩 1 只」。
    """

    code: str
    themes: list[str]


class LimitThemes(BaseModel):
    trade_date: date
    # 至少 MIN_CLUSTER 只涨停股共同挂着的题材，按涨停家数降序
    clusters: list[LimitThemeItem]
    stocks: list[LimitStockTheme]


# --------------------------------------------------------------- 个股异动

# 口径与三处「与原型不同」的差异见 `api/anomaly_map.py` 的模块说明。


class AnomalyRowOut(ApiModel):
    """异动图谱里的一行。

    `sector` 是**板块分组键**（同花顺行业末段），`industry` 是完整三级路径 ——
    页面按前者分组、后者放 tooltip。`date` / `time` / `generated_at` 一律给字符串：
    这一屏不做日期运算，接口直接给「显示用」的形态，前端不再自己格式化。
    """

    sector: str
    #: 交易日 YYYY-MM-DD
    date: str
    #: 封板时间（HH:MM）或「收盘」（中大阳线）
    time: str
    code: str
    name: str
    #: 涨停 / 涨停炸板 / 中大阳线异动
    type: str
    #: 连板标签：首板 / N连板；炸板与中大阳线为空串
    board: str
    pct: float | None
    #: 总市值（**亿元**，保留 1 位）
    cap: float | None
    industry: str | None
    reason: str | None
    #: 同批异动（同一天同一板块的全部行），标签形如「名称(涨停@09:31)」，含自己
    cohort: list[str]
    #: 该行所在 (交易日, 板块) 是不是窗口内该板块的题材启动日
    theme_start: bool


class AnomalyMapOut(ApiModel):
    """个股异动图谱（`/api/anomaly-map`）。"""

    start: str
    end: str
    days: int
    generated_at: str
    #: 过滤口径文案（前端原样展示，别在前端再拼一遍）
    filter: str
    #: 数据来源文案
    source: str
    #: 板块下拉的选项（已按命中条数降序）
    sectors: list[str]
    #: 被标记为题材启动日的 (交易日, 板块) 对数
    theme_start_days: int
    rows: list[AnomalyRowOut]


# --------------------------------------------------------------- 数据管理


class CollectStepResult(BaseModel):
    status: str
    rows: int
    cost: float
    message: str | None


class CollectResult(BaseModel):
    trade_date: str
    steps: dict[str, CollectStepResult]


class CollectLogOut(ApiModel):
    trade_date: date | None
    task: str
    status: str
    rows: int | None
    message: str | None
    cost_seconds: float | None
    created_at: datetime


class SchedulerStatus(BaseModel):
    """定时采集任务状态。"""

    enabled: bool
    running: bool
    collect_time: str
    # 收盘后那一趟（龙虎榜 / 机构席位 / 涨停题材 / 两融 / 北向）—— 这几类 15:05 还没发布，
    # 页面要显示它，否则「龙虎榜 0 条」会被当成故障（见 config.late_collect_hour）
    late_collect_time: str
    # 同一趟的兜底时刻（见 config.late_retry_hour）：来源当天什么时候更新不确定，
    # 只赌一个时刻会赌空，所以固定跑两趟
    late_retry_time: str
    # 当天板块成分股的预取时刻（见 config.members_collect_hour）：开盘红要到晚上才
    # 发布当天成分股，所以它比上面那两趟都晚
    members_collect_time: str
    # 它的兜底时刻（见 config.members_retry_hour）：22:00 撞上「还没发布」时再试一次；
    # 正常日子那一趟几乎不花请求（已在库的板块全跳过）
    members_retry_time: str
    catchup_on_start: bool
    next_run_time: str | None
    late_next_run_time: str | None
    last_run: str | None
    last_result: dict | None


class TableCoverage(BaseModel):
    """单张表的覆盖情况。各表能回补的范围不同，必须分别展示。"""

    label: str
    days: int
    latest: date | None


class IfindToolUsage(BaseModel):
    server: str
    tool: str
    calls: int


class IfindQuota(BaseModel):
    """iFinD 调用配额视图。

    额度是**账号级**的 —— 定时采集、形态选股、手工补数共用一个池子，
    所以这个视图不属于任何单一链路，单独给一块。

    计量窗口是**订阅周期**不是自然月：iFinD 后台「计量区间」是
    2026-09-17 ~ 2026-10-17 这种滚动窗口，起止都落在订阅日（见
    IFIND_CYCLE_START_DAY），所以字段一律用 cycle 而不是 month。
    """

    # 当前计量周期的起止日（含）。跨月，所以两个都要给前端
    cycle_start: date
    cycle_end: date
    monthly_quota: int
    cycle_calls: int
    cycle_remaining: int
    today_calls: int
    cycle_trade_days_passed: int
    cycle_trade_days_total: int
    # 计量从哪天开始。晚于周期起点时，cycle_calls 只是本周期**有记录以来**的消耗
    counting_since: date | None
    # 外推实际用到的样本交易日数
    sampled_trade_days: int
    # 按样本期的日均用量外推到整周期。样本不足（<3 个交易日）时为 None
    projected_cycle_calls: int | None
    # 已用比例。**只用于页面展示**（让路判据已改成 `need_ratio`，见下）
    usage_ratio: float | None
    # 让路判据的三个输入：余下交易日（含今天）、按满负荷估的需求、需求 ÷ 剩余额度。
    # `need_ratio` 为 None 表示剩余额度已经是 0（最紧张）
    days_left: int
    need_calls: int
    need_ratio: float | None
    # 当前档位（见 `services.usage.QuotaLevel`）：数值越大越紧张，让路顺序是
    # 「先停命中前 50 只 DDE 补齐 → ETF → 全市场日线+DDE 扫描 → 只留主线」
    level: int
    level_label: str
    by_tool: list[IfindToolUsage]


class AdminStatus(BaseModel):
    latest_sentiment_date: date | None
    latest_limit_pool_date: date | None
    latest_lhb_date: date | None
    latest_index_date: date | None
    data_days: int
    coverage: list[TableCoverage]
    scheduler: SchedulerStatus
    recent_logs: list[CollectLogOut]
    ifind_quota: IfindQuota


# -------------------------------------------------------------------- 自选股


class WatchlistIn(BaseModel):
    # 长度上限（2026-10-10 加）：这些字段以前没有上限，一个登录用户就能塞进来
    # 任意大的字符串（备注是 TEXT，只进不出地占库）。上限按「人写得出来的量」给。
    # `code` 收的是代码 / 名称 / 拼音首字母，32 够。
    code: str = Field(..., max_length=32)
    name: str | None = Field(None, max_length=64)
    note: str | None = Field(None, max_length=2000)


class WatchlistRow(BaseModel):
    """自选股一行。

    行情来自本地缓存的日线（stock_daily），不是实时行情 —— 页面因此
    不需要联网，打开即读。没有缓存的股票这几项为 null。
    """

    code: str
    name: str | None
    tags: list[str] | None
    note: str | None
    added_at: datetime
    latest_date: date | None
    close: float | None
    pct_chg: float | None


# ------------------------------------------------------------------ 个股详情


class StockDailyRow(ApiModel):
    trade_date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    pct_chg: float | None
    volume: float | None
    amount: float | None
    #: 换手率（百分数，如 2.18 表示 2.18%）。**只有日 K 有值** ——
    #: 周 / 月是几天的合并，换手率不能相加、均值也没意义，所以重采样后是 None
    turnover: float | None = None
    #: 这一天是不是涨停（按板块限幅判定，见 `services/limit_rules.py`）。
    #: **只有日 K 有值**；周 / 月 K 是 None —— 一根柱子是几天的合并，
    #: 「这天涨停」在那个粒度上不成立（周涨幅够不到 10% 不代表那一周没有涨停日）。
    is_limit_up: bool | None = None
    #: 这一天是不是跌停（同上，判据见 `is_limit_down`）。同样只有日 K 有值
    is_limit_down: bool | None = None


class StockDdeRow(ApiModel):
    """个股某一天的 DDE 与主力净流入。

    单位一律**元**（iFinD 原始单位）—— 亿/万的换算放在前端展示层，别在这里先除一遍。
    """

    trade_date: date
    # 主力净流入额
    net_inflow: float | None
    # 「5日DDE」—— 名字是来源自己的窗口口径，不是「当日 DDE」
    dde: float | None


class StockDdeOut(BaseModel):
    code: str
    name: str | None
    # 实际给了多少个交易日：少于请求的 days，说明这只票的历史（或缓存）就这么长
    days: int
    rows: list[StockDdeRow]
    # 取不到数据时的原因；取到了就是 null
    note: str | None = None


class WudaoPhase(ApiModel):
    """悟道「阶段判定」：8 个标签之一 + 一眼看懂的说明。

    判定在 `services/patterns.classify_phase`（与「明天盯 / 明天预案」共用同一批判据），
    这里只是把它搬给前端。

    ⚠️ **但不等于名单**（2026-10-09 起）：名单只收创业板 + 科创板、且出口另有一道
    `_wudao_leave_ok`（收盘离开最高 ≤ 2.9%）—— 两处都故意没进 `classify_phase`。
    所以这里显示「明天盯 / 明天预案」时，悟道页的名单里**可能没有它**（实测 09-28：
    判「明天盯」的 31 只里只有 6 只在名单）。见 `api/analysis.py` 的模块说明。
    """

    #: silent / wake / sample / start / digest / diverge / dump / weak / unknown
    phase: str
    label: str
    #: 现在该怎么做（原型 `PHASE_DO`）
    action: str
    #: 一句话结论（带量比等数字）
    text: str
    #: 样板/启动的「像不像」档（更像你要的 / 能盯略吵 / …）
    fit_label: str
    fit_text: str
    prefer: int
    #: 近 8 日的上影情况
    shadow_label: str
    shadow_text: str
    #: 左侧压力
    pressure_label: str
    pressure_text: str
    #: 明天的计划 / 主要风险
    plan_text: str
    risk_text: str
    #: 关键价位：样板=今高、启动=昨高，其余为 None
    watch_price: float | None = None
    metrics: dict[str, float | None] = Field(default_factory=dict)


class PoolHorizon(ApiModel):
    """一个收益档位的统计量（收益都是**百分数**，1.23 = +1.23%）。"""

    days: int
    samples: int = 0
    mean: float | None = None
    median: float | None = None
    up_pct: float | None = None
    #: 相对**同一天全市场等权平均**的超额（与回测脚本、形态页跟踪同一口径）
    excess: float | None = None
    #: 跑赢同日全市场的比例
    beat_pct: float | None = None


class PoolStanding(ApiModel):
    """一个悟道池子的「成绩单」：**每天照这个池子全买，之后会怎样**。"""

    pattern: str
    name: str
    #: 窗口内全部命中条数 / 去重只数 / 有命中的扫描日数
    hits: int = 0
    stocks: int = 0
    scan_days: int = 0
    returns: list[PoolHorizon] = Field(default_factory=list)
    #: 破位率的样本数（到线率那一列用 `touch_samples`，两者可能不等）
    samples: int = 0
    #: 到线率：命中后 N 日内**收盘**站上「要过的那条线」的比例。池子没有这条线时是 null
    touch: float | None = None
    touch_samples: int = 0
    #: 破位率：同期**收盘**跌破「作废位」的比例
    stop: float | None = None
    #: 破位用的是哪个位 —— 「作废位」还是「命中日最低价（代理）」。两种口径不能看起来一样
    stop_source: str = ""


class PoolStandingOut(ApiModel):
    """悟道池子的成绩单（`/api/patterns/standing`）。"""

    start: str
    #: 窗口里实际有命中的扫描日数（样本还少时会等于 1，页面要如实显示）
    cohorts: int
    scan_days: list[str] = Field(default_factory=list)
    track_days: int
    line_days: int
    pools: list[PoolStanding] = Field(default_factory=list)


class StockNewsRow(ApiModel):
    """一条个股新闻。"""

    #: 发布时间，东财原样给（"2026-08-26 09:59:14"）
    published_at: str
    title: str
    summary: str
    #: 文章来源，如「界面新闻」
    source: str
    url: str


class StockNewsOut(ApiModel):
    """个股新闻列表。`rows` 为空时看 `note` 是「真没搜到」还是「源没返回」。"""

    code: str
    name: str | None = None
    rows: list[StockNewsRow] = Field(default_factory=list)
    #: 被筛掉的「只在正文表格里提到代码」的名单类稿件条数（界面上要标出来，不静默丢）
    hidden: int = 0
    note: str | None = None


class StockAnalysis(ApiModel):
    """个股分析页（`/api/analysis/lookup`）的返回：解析结果 + 阶段判定 + 旁证。

    「旁证」是原型那个框里「技术 / 新闻 / 基本面三面合议」的站内可做部分：
    技术面＝`phase`，基本面＝`industry`（做什么生意），题材/复盘＝`sectors` + 计数。
    **新闻另走一个接口**（`GET /api/stock/{code}/news`，东财口径）—— 它要出外网、还可能取不到，
    塞进这里会让整页一起等、一起失败。

    ⚠️ 三面是**并列参考，不做自动合议**（没有哪一步把它们综合成一个判断）——
    页面文案别再写成「合议」。
    """

    code: str
    name: str | None = None
    #: 判定用的那根日线是哪天（原型的框也会把日期显示出来）
    trade_date: date | None = None
    close: float | None = None
    pct_chg: float | None = None
    #: 阶段判定；日线不足 25 根时是 None
    phase: WudaoPhase | None = None
    #: 所属同花顺三级行业（「房地产-房地产-住宅开发」这种路径），做什么生意
    industry: str | None = None
    #: 最近一次涨停时挂的开盘红精选板块 —— 做过什么题材。只覆盖涨停过的票
    sectors: list[str] = Field(default_factory=list)
    #: 复盘关联：上过涨停池 / 上过龙虎榜的**天数**（不是行数，更不是历史累计）。
    #: ⚠️ 两个都是**在库里已有数据的天数内**数的（分母＝下面两个窗口天数），页面必须把
    #: 窗口一起显示，否则会被读成「这只票一辈子涨停 12 次」。
    #: ⚠️ 两个都按**去重交易日**数：`limit_pool` 主键含 `pool_type`、`lhb` 主键含 `reason`，
    #: 直接 `count(*)` 会把「同一天多个上榜原因」算成多次（实测库里 21236 行 `lhb` 里
    #: 有 2557 个「票-日」是多行、最多一天 6 行）。
    limit_up_count: int = 0
    lhb_count: int = 0
    #: `limit_pool` / `lhb` 两张表各自覆盖的交易日数（= 上面两个计数的分母窗口）。
    #: 两张表分开给，因为它们的窗口不一定一样（补数失败、某天缺采都会造成差异）。
    limit_up_days: int = 0
    lhb_days: int = 0


class StockProfile(BaseModel):
    code: str
    name: str | None
    in_watchlist: bool
    note: str | None
    latest: StockDailyRow | None
    # 本地缓存的日线覆盖
    day_count: int
    first_date: date | None
    last_date: date | None
    # 该股历史上过涨停池的日期，便于和复盘关联
    limit_up_dates: list[date]
    lhb_count: int
    #: 近 5 个交易日涨跌幅（百分数，如 6.31 表示 +6.31%）。
    #: 日线不足 6 根（次新股 / 刚缓存）时是 None —— 前端不显示这一格，不填 0
    pct_chg_5d: float | None = None
    # 下面四个来自 `stock_basic`（建池时随选股接口取回，覆盖全 A，见 8.68.13）。
    # ⚠️ 市值与市盈率是**按 asof 那天的价格算的快照**，读取时已按最新收盘价缩放；
    #    自由流通市值 / 实际换手率只用股本（慢变）+ 最新数据推，不需要缩放。
    #: 总市值（元）
    total_mv: float | None = None
    #: 自由流通市值（元）= 自由流通股 × 最新收盘价
    free_float_mv: float | None = None
    #: 实际换手率（百分数）= 成交量 ÷ 自由流通股。口径与 iFinD 自己返回的逐位相同
    actual_turnover: float | None = None
    #: 动态市盈率 = iFinD「预测市盈率」（按分析师预测净利润算，同花顺口径）
    pe_forecast: float | None = None
    #: 上面那几个指标的数据日（缩放基准日）。None = 没有这些数据
    total_mv_asof: date | None = None
    #: 悟道「阶段判定」（8 个标签 / 左侧压力 / 上影 / 计划）—— 日线不足 25 根时是 None
    phase: WudaoPhase | None = None


class StockThemeItem(BaseModel):
    """个股挂的一个题材，附带该板块最近一个交易日的表现。

    板块表现取自 `sector_daily` 的最新交易日 —— 个股题材本身不是按日的快照，
    所以只能给「最近一个交易日」的板块涨跌幅，不能声称是某天的。
    """

    concept: str
    # 能对上板块表时给板块代码；对不上（如「沪深300样本股」）为 null
    board_code: str | None
    pct_chg: float | None


class StockThemes(BaseModel):
    code: str
    name: str | None
    # 上面的板块涨跌幅对应的交易日
    board_date: date | None
    themes: list[StockThemeItem]


# ------------------------------------------------------------------ 复盘笔记


class NoteIn(BaseModel):
    # 长度上限（2026-10-10 加，理由同 WatchlistIn）：复盘笔记是自由文本，
    # 按「一天写满两屏」量级给 20000 字，正常写根本碰不到。
    market_view: str | None = Field(None, max_length=20000)
    next_plan: str | None = Field(None, max_length=20000)


class NoteOut(ApiModel):
    trade_date: date
    market_view: str | None
    next_plan: str | None
    updated_at: datetime | None


# ------------------------------------------------------------------ 形态选股


class PatternMeta(BaseModel):
    """形态清单的一项，给前端做分组筛选。"""

    key: str
    name: str
    group: str


class PatternHitItem(BaseModel):
    """一只票命中的某一个形态。"""

    pattern: str
    pattern_name: str
    group: str
    score: float
    # 突破价 / 支撑价等关键位，用于在 K 线图上画线
    key_levels: dict
    # 平台振幅、放量倍数等明细，用于解释「凭什么说它命中了」
    detail: dict


class PatternStockOut(BaseModel):
    """一只票的形态命中汇总 —— 列表页一行。

    一只票可能同时命中多个形态（如既创 120 日新高又回踩不破），归并成一行、
    多个标签。`score` 取其中最高分，作为排序依据。

    `avg_amount` / `total_mv` 来自**股票池**（`stock_universe`），是这只票的
    **属性**而不是信号当天的快照，所以从池子实时取、不冗余存进 `pattern_hit` ——
    池子外的票（日均成交额跌到门槛以下）这两项为 null。
    `close` / `pct_chg` / `amount` 相反，是逐日快照，必须随命中记录一起存下来。
    """

    code: str
    name: str | None
    trade_date: date
    #: 窗口模式（`/patterns/hits?days=N`）下这一轮**首次**命中的日期、以及这一轮持续了几个
    #: 交易日、最后一天是哪天。单日模式下 `run_days` 恒为 1、`last_date` 等于 `trade_date`。
    #:
    #: 为什么要这个：像「堆量上涨后缩量」这种**状态型**形态，只要量能天天创新低就会天天
    #: 命中（实测中红医疗连中 6 天），十天的名单里同一只票会占五六行。窗口模式把「同一只票
    #: 同一轮」合并成一条、日期显示首次那天（2026-10-14 用户要求）。
    run_days: int = 1
    last_date: date | None = None
    close: float | None
    pct_chg: float | None
    amount: float | None
    # 近 20 日日均成交额（元），判断「这个突破能不能承接我的仓位」
    avg_amount: float | None
    # 总市值（元），判断「是题材小票还是权重」
    total_mv: float | None
    # 该股所属的**开盘红精选板块**（2026-09-29 用户要求列在「代码 / 名称」后面）。
    #
    # ⚠️ 口径就是个股页「所属题材」那一个（`stock_concept`）—— 「这只票**最近一次
    # 涨停**是因为哪个板块」，**不是**「它属于哪些概念」。开盘红没有非涨停个股的板块
    # 归属接口（见设计文档 8.32.4），所以从未涨停过的票这里是**空**的。
    # 实测：最新命中日的 1973 只里 1466 只有（**74.3%**），池内整体 75.2%。
    # 取的是「该股出现过的最近一天」而不是「命中当天」—— 当天口径只有那几十只涨停股
    # 有值，等于整列空着。
    #
    # ⚠️ **接口这一层绝不拿行业或其他口径来顶**（空就是空，口径要干净）。
    # 页面上也是**两列/两行分开**，谁都不回落：悟道页与形态页是「板块」「行业」各一列，
    # 个股页是「精选板块」「所属行业」两行。
    # （2026-10-09 曾短暂试过「板块空时在悟道页回落显示行业」这一版，看过效果后
    #   用户改成「拆成两列」，于是回落的代码已撤掉 —— 别再加回来。）
    sectors: list[str] = []
    # 所属**同花顺行业**，来自 `stock_basic.industry`（2026-09-29 起由建池那条路顺手带回来，
    # 7 天一次、覆盖全 A）。
    #
    # ⚠️ 与上面的 `sectors` **不是一个口径**，不要合并成一列：
    # - 这个答「公司做什么生意」，值是**三级路径**（`房地产-房地产-住宅开发`），
    #   页面只显示第一级，完整路径放 tooltip；
    # - 上面那个答「最近一次涨停是因为哪个板块」，是开盘红精选板块（`地产链`）。
    # 英文名撞车（都叫 industry）是来源的锅：`limit_pool.industry` 也是同花顺行业，
    # 而开盘红那套在 `sectors` 里。
    industry: str | None = None
    #: 阶段判定（`services.patterns.classify_phase`）—— 与个股页／个股分析页同一个判据
    #: （取数壳 `services.stock_phase`）。2026-10-10 用户要求列在「代码 / 名称」后面。
    #:
    #: 只带 key 与中文标签：整块 `WudaoPhase` 有十几个字段，一屏几百行太重，而列表里
    #: 只显示一个词。**日线不足 25 根时为 None**（次新 / 刚缓存），页面按惯例显示「—」。
    phase: str | None = None
    phase_label: str | None = None
    score: float
    patterns: list[PatternHitItem]


class PatternCount(BaseModel):
    pattern: str
    pattern_name: str
    group: str
    # 命中该形态的股票数
    stocks: int


class PatternSummary(BaseModel):
    """某日的形态命中概况，给首页面板与飞书简报用。"""

    trade_date: date | None
    total_hits: int
    total_stocks: int
    by_pattern: list[PatternCount]


class PatternTrackPoint(BaseModel):
    """循环里**第 `day` 个交易日**上的统计。

    ⚠️ **收益与比例都是百分数**（`mean=1.23` 表示 +1.23%、`up_pct=54.3` 表示 54.3%），
    不是 0~1 的比率 —— 免得前端再猜一次单位。
    """

    # 筛选后第几个交易日（1 起）
    day: int
    # 有完整数据的样本数（这一天该股没有行、或循环还没走到这一天时不计入）
    samples: int
    mean: float | None
    median: float | None
    # 上涨占比
    up_pct: float | None
    # 平均超额：各样本收益 − **它自己循环那天**的全市场平均
    excess: float | None
    # 跑赢当天全市场平均的比例
    beat_pct: float | None


class PatternTrackCohort(BaseModel):
    """一个循环：某个筛选日选出的前 N 只，以及它们之后每一天的表现。"""

    trade_date: date
    # 当天选出的只数（不足 `top` 就是那天命中总数不够）
    stocks: int
    # 这个循环已经能算到第几个交易日（= 已走完多少；`track_days` 就是走完了）
    progress: int
    # 长度 = `progress`：**没走到的日子不返回**，不编造数据。
    # 曲线上的峰 / 谷（最多浮盈、最深回撤）由前端从这里面取，接口不另存一份。
    points: list[PatternTrackPoint]


class PatternTrackSummary(BaseModel):
    cohorts_used: int
    # 逐循环样本合计（同一只票重复上榜会重复计）
    stocks: int
    # 去重后的只数
    unique: int
    first_date: date | None
    last_date: date | None


class PatternTrackOut(BaseModel):
    cohorts: int
    track_days: int
    top: int
    summary: PatternTrackSummary
    # 所有循环在同一个「第 n 个交易日」上摊平的统计 —— 表格底部的「合计」行用
    average: list[PatternTrackPoint]
    days: list[PatternTrackCohort]


class PatternTrackStock(BaseModel):
    """明细表里的一只票。"""

    code: str
    name: str | None
    score: float
    # 之后第 1..track_days 个交易日的**当日涨跌幅**（百分数，1.23 = +1.23%）。
    # ⚠️ 是**每天的涨跌幅**，不是从筛选日起算的累计；该股那天没有行情行时为 null
    pct: list[float | None]


class PatternTrackDetail(BaseModel):
    """某个循环选中的票的逐日明细。"""

    trade_date: date
    top: int
    track_days: int
    # 已经走到第几个交易日（0 表示还没走到任何一天）
    progress: int
    # 每一列对应的实际交易日，与每行 `pct` 一一对应（给表头做提示用）
    days: list[date]
    rows: list[PatternTrackStock]


# ------------------------------------------------------------------ 资金面


class MarginSnapshot(BaseModel):
    """单个市场的两融快照，**单位元**。"""

    financing_balance: float | None  # 融资余额
    financing_buy: float | None  # 融资买入额
    securities_balance: float | None  # 融券余额


class FundFlowOverview(BaseModel):
    """资金面总览（当日）。所有金额单位：元。"""

    trade_date: date
    sh: MarginSnapshot | None
    sz: MarginSnapshot | None
    # 两市合计。**两市都有数时才给** —— 深市常比沪市晚一天（实测），
    # 只有一个市场时相加会把「待披露」误报成「归零」
    financing_total: float | None
    financing_buy_total: float | None
    # 与上一交易日相比的融资余额变化（同口径，两边齐全才算）
    financing_change: float | None
    # 沪深股通**成交总额**。⚠️ 官方 2024-08 起不再披露买卖方向，
    # 所以这里没有「净流入」—— 只有活跃度
    hsgt_turnover: float | None
    hsgt_turnover_prev: float | None
    # 龙虎榜机构席位净买额合计（按代码去重后相加）
    institution_net: float | None
    institution_count: int
    # ETF 净申赎估算（份额变化 × 收盘价）。缺上一日数据时为 None
    etf_net_inflow: float | None


class FundsSeries(BaseModel):
    """两融与北向成交额的走势。"""

    dates: list[date]
    financing_balance: list[float | None]
    financing_buy: list[float | None]
    hsgt_turnover: list[float | None]
    # 该日两市数据是否齐全。不齐的日子上面两个序列是 None，
    # 前端据此断开曲线，而不是画成 0
    financing_complete: list[bool]


class EtfFlowItem(BaseModel):
    code: str
    name: str | None
    close: float | None
    pct_chg: float | None
    amount: float | None  # 成交额（元）
    shares: float | None  # 当日份额（份）
    share_delta: float | None  # 份额变化（份），正数 = 净申购
    net_inflow: float | None  # 净申赎估算（元）


class EtfFlowBoard(BaseModel):
    trade_date: date
    prev_date: date | None = None
    has_prev: bool
    items: list[EtfFlowItem]


class EtfIndustryItem(BaseModel):
    """一个行业 / 主题分类下的 ETF 汇总。

    **没有合计份额变化**：份额单位是「份」，不同 ETF 每份净值差几个数量级，
    相加出来的数字没有任何含义。能加的是金额 —— 净申赎与成交额。
    """

    category: str
    fund_count: int
    net_inflow: float | None  # 该分类净申赎合计（元）
    amount: float | None  # 该分类成交额合计（元）
    pct_chg: float | None  # 成交额加权平均涨跌幅
    # 该分类下的 ETF 明细，只给前几只（按净申赎降序），全量交给「按单只」视图
    funds: list[EtfFlowItem]


class EtfIndustryBoard(BaseModel):
    trade_date: date
    prev_date: date | None = None
    has_prev: bool
    items: list[EtfIndustryItem]


class InstitutionItem(BaseModel):
    code: str
    name: str | None
    close: float | None
    pct_chg: float | None
    buy_count: int | None
    sell_count: int | None
    buy_amount: float | None
    sell_amount: float | None
    net_amount: float | None
    reason: str | None


class InstitutionBoard(BaseModel):
    trade_date: date
    items: list[InstitutionItem]
    # 去重后的股票数（原始行数会因「多条上榜原因」更多）
    total: int


class DdeItem(BaseModel):
    code: str
    name: str | None
    # 收盘价与涨跌幅来自日线（`stock_daily`）。它只覆盖「流动性池 + 池外涨停」，
    # 所以池外那部分票这两列是空 —— 空的照实显示「—」，不要用 0 顶替
    close: float | None
    pct_chg: float | None
    # 5日DDE（元）。与个股页那一栏是**同一个数**（同一张表、同一口径，实测一致）
    dde: float | None


class DdeBoard(BaseModel):
    trade_date: date
    items: list[DdeItem]
    # 当天**有 DDE 的票数**，不是全市场只数 —— 来源在收盘后逐步发布，
    # 当天 18 点前后大约只覆盖七成（见设计文档 8.44/8.46）
    total: int


# --------------------------------------------------------------- 会员与登录
#
# 见设计文档 §8.69。密码永远不出现在任何响应里（连哈希也不给）。


class RegisterIn(BaseModel):
    # 长度上限（2026-10-10 加）：这些入参以前完全没有上限，而 `hash_password` 对超长
    # 输入照样跑满 scrypt —— 一个几十 MB 的「密码」就是一次白送的 DoS。
    # 口令给 128（远比正常口令宽），用户名与 `api/auth.USERNAME_MAX` 对齐。
    invite_code: str = Field(..., max_length=64)
    username: str = Field(..., max_length=32)
    password: str = Field(..., max_length=128)


class LoginIn(BaseModel):
    username: str = Field(..., max_length=32)
    password: str = Field(..., max_length=128)


class PasswordIn(BaseModel):
    old_password: str = Field(..., max_length=128)
    new_password: str = Field(..., max_length=128)


class MeOut(ApiModel):
    """「我是谁」—— 前端启动时问一次，用来决定显示什么、能不能进数据管理页。"""

    id: int
    username: str
    is_admin: bool


class InviteIn(BaseModel):
    # 长度 / 数量上限（2026-10-11 加）：以前两者都没有上限，一个管理员请求就能塞超长 note
    # 或让循环次数失控。note 按「人写得出来的量」给 2000；count 由 admin 侧再夹到 20。
    note: str | None = Field(None, max_length=2000)
    # 一次生成几个。发一群人时省得点很多次
    count: int = Field(1, ge=1, le=1000)


class InviteOut(ApiModel):
    code: str
    note: str | None
    created_at: datetime
    used_by: int | None
    # 用掉它的那个人的用户名（列表里直接看得懂，不用再去对 id）
    used_by_name: str | None = None
    used_at: datetime | None
    disabled_at: datetime | None


class MemberOut(ApiModel):
    id: int
    username: str
    is_admin: bool
    created_at: datetime
    # 「最近访问」—— 任何带会话的请求都会按 1 小时节流刷新（见 models.AppUser.last_seen_at）。
    # ⚠️ 不要理解成「最近登录」：会话是滑动续期的，会员可以一直不重新登录。
    last_seen_at: datetime | None
    disabled_at: datetime | None
    # 他是拿哪个邀请码进来的（来源可追）
    invite_code: str | None = None


class ResetPasswordIn(BaseModel):
    # 上限必须与 `LoginIn.password` **一致**（都是 128）：否则管理员能重置出一个超长密码，
    # 而登录入口在 128 就把它拒了 —— 那个账号从此永远登不进去（2026-10-11 加）。
    new_password: str = Field(..., max_length=128)


class DisabledIn(BaseModel):
    disabled: bool


# ------------------------------------------------------------------ 竞价强度
#
# 数据来自 `auction_snapshot`（腾讯批量行情，逐分钟采样），见 `jobs/collect_auction.py`。
# ⚠️ 参考图里那张表还有 净额 / 承接强度 / 北向 / 人气排名 几列，这些**数据源层面拿不到**
#（北向净流入 2024-08 起已停止披露；承接强度是外部工具专有指标），本站一律不编。


class AuctionRow(ApiModel):
    """一只票的集合竞价快照（9:25 竞价结果 + 昨日连板数）。"""

    code: str
    name: str | None
    price: float | None  # 竞价成交价（= 当日开盘价）
    pre_close: float | None
    pct_chg: float | None  # 竞价涨幅（%）
    # 今开。⚠️ 它与 `price` **恒等**（A 股开盘价 = 集合竞价成交价），所以**不提供**
    # 「开盘涨幅」—— 那一项与 `pct_chg` 恒等，留着只是重复（2026-10-12 撤）
    open: float | None
    amount: float | None  # 竞价成交额（元）
    volume: float | None  # 竞价成交量（股）
    turnover: float | None  # 竞价换手率（%）
    volume_ratio: float | None  # 竞价量比
    float_mv: float | None
    total_mv: float | None
    limit_up: float | None
    limit_down: float | None
    # 昨日连板数（当日 9:25 时今日的池还没有）。非涨停股为 None
    consecutive: int | None = None
    # **竞价量比（自算）**：当日竞价成交量 ÷ 前 5 个交易日的竞价成交量均值。
    # 口径取自同花顺对「竞价量比」的定义；历史不足 3 天时留空（我们的采样从
    # 2026-10-12 才开始，前几天必然是空的）。见 `api/auction._auction_ratios`。
    auction_ratio: float | None = None
    # --- 派生指标（见 api/auction._metrics）---
    #: **竞价价格波动率（%）** = 9:15–9:26 竞价最高价与最低价之差 ÷ 昨收。
    #: 高开但波动极小 = 资金一致性强；上下大幅波动 = 分歧大、开盘易跳水。
    amplitude: float | None = None
    #: **自由流通市值（元）** = 自由流通股 × 竞价价（同花顺自由流通口径）
    free_float_mv: float | None = None
    #: **真实竞价换手（%）** = 竞价量 ÷ 自由流通股。普通换手率的分母含大量不动的筹码，会低估
    free_turnover: float | None = None
    #: **竞价成交额占比（%）** = 竞价额 ÷ 昨日全天成交额。绝对金额在大/小盘之间不可比
    amount_ratio: float | None = None
    #: **竞价涨幅排名**：全市场位次（1 起、并列同名次）
    rank: int | None = None
    #: **板块内（同花顺行业）竞价涨幅排名**
    industry_rank: int | None = None
    #: **昨日是否炸板**（昨日在炸板池里）。与 `consecutive`（昨日涨停池的连板数）互斥
    prev_broken: bool = False
    #: **昨日全天换手率（%）**
    prev_turnover: float | None = None
    #: **未匹配量差（委差，单位「股」）** = 委买总量 − 委卖总量，取竞价终值。
    #: 正 = 未匹配的买盘更多。⚠️ 腾讯竞价时段是否填这个字段尚未核实，可能恒为空
    committee_diff: float | None = None
    #: **9:20 撤单率（%）** = (9:20 涨停/跌停价上的挂单量 − 9:21 的) ÷ 9:20 的 × 100。
    #: 9:20 之后不能撤单，撤得越少越真；**负值**表示挂单还在增加。只在两帧都取到挂单量时有值
    cancel_rate: float | None = None
    #: **封成比（%）** = 涨停价上的封单额 ÷ 自由流通市值。只在**竞价就封在涨停价上**时有值
    #: （打板核心指标：>1% 较强、>3% 超强）
    seal_ratio: float | None = None
    #: **开盘后涨幅（%）** = 9:31 现价 ÷ 昨收 − 1。判断高开之后是**站住了还是被砸**。
    #: ⚠️ 由 9:31 的补采帧提供，该帧上线（2026-10-12 之后）的日子才有；之前为空
    post_open_pct: float | None = None
    #: **自由流通盘占比（%）** = 自由流通市值 ÷ 总市值。越低越容易被资金控盘，
    #: 那种票的竞价异动参考性要打折扣
    free_float_ratio: float | None = None


class AuctionSummary(BaseModel):
    """竞价强度的**市场级**概览（页面顶部那条情绪带）。

    与主表不同：这里的口径**不受筛选 / 截断影响**，一律按全市场算。
    """

    trade_date: date
    #: 上一交易日（按交易日历）
    prev_trade_date: date | None = None
    #: 昨日涨停家数
    prev_limit_count: int = 0
    #: **短线情绪温度计**：昨日所有涨停股今日的**平均竞价涨幅（%）**。
    #: >2% 情绪亢奋（高开的胜率与溢价都高）；<0 退潮期（个股竞价再好看也易炸板）
    prev_limit_avg_pct: float | None = None
    #: 昨日连板梯队。⚠️ 竞价是**盘前**，今日的梯队还不存在，所以这里看的是**昨日**结构
    prev_board_1: int = 0
    prev_board_2: int = 0
    prev_board_3plus: int = 0
    prev_max_board: int = 0
    #: 全市场竞价涨跌家数（按竞价涨幅）
    up_count: int = 0
    down_count: int = 0
    flat_count: int = 0
    #: **竞价即涨停**家数（竞价价 ≥ 涨停价）
    limit_count: int = 0


class AuctionListOut(BaseModel):
    trade_date: date
    total: int  # 当日有竞价数据的全市场只数（不受筛选 / 截断影响）
    shown: int  # 本次实际返回的行数
    rows: list[AuctionRow]
    #: 市场级概览（情绪带）。当日没有竞价数据时为 None
    summary: AuctionSummary | None = None


class AuctionBoardItem(BaseModel):
    """一个**同花顺行业（最后一级）**的竞价强度汇总。

    ⚠️ 与页面上「开盘红板块」不是同一套分类：那个只覆盖涨停股，没法给全市场分组，
    所以板块聚合只能用同花顺行业（`stock_basic.industry`，全 A 都有）。
    """

    industry: str
    count: int  # 该行业的只数
    avg_pct: float | None  # 平均竞价涨幅（%）
    amount: float | None  # 竞价成交额合计（元）
    limit_count: int  # **竞价即涨停**的家数（竞价价 ≥ 涨停价，不是收盘是否涨停）


class AuctionBoardsOut(BaseModel):
    trade_date: date
    items: list[AuctionBoardItem]
