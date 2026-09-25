"""形态引擎：从日线序列里找出符合经典技术形态的票。

## 输入必须是**前复权**序列

`build_bars` 负责把不复权原始价 + 真实涨跌幅复利成前复权序列，本模块只处理
复权后的价格。除权跳空会让「回踩不破」「平台突破」这类形态在错误的位置触发 ——
这是最容易埋雷的地方：形态图上看着不像，但引擎认为命中了，而且没人能一眼看出来。

## 输出是评分而不是布尔值

「像不像」比「是不是」有用得多：排序、筛选、复盘全靠它。每个形态给 0~100 的
连续分，外加关键位（突破价 / 支撑价）与明细指标（平台振幅、放量倍数…），
后者用于在页面上解释「为什么它入选了」—— 没有明细的分数字毫无说服力。

## 阈值集中在本文件顶部

散落在各函数体里的话，调参时要翻遍全文才能拼出全貌。
"""

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

import numpy as np

logger = logging.getLogger(__name__)

# 低于这个分不进库。全部形态都会先做布尔前提，再连续打分，所以它是「像不像」的门槛
MIN_SCORE = 50.0

# 理想区间两端的分值系数。见 `_band_score`：中心 1.0，边缘这个值
EDGE_SCORE = 0.8

# ---------------------------------------------------------------- 各形态阈值

# 均线多头排列：四条均线要同时向上，看 20 日斜率；排列够这么多天给满分
MA_PERIODS = (5, 10, 20, 60)
MA_SLOPE_LOOKBACK = 20
MA_STACK_FULL_DAYS = 20
# MA5 相对 MA60 的乖离：太小说明还没走出来，太大说明已经涨过头
MA_SPREAD_IDEAL = (0.005, 0.05)
MA_SPREAD_CAP = 0.08
# 收盘距 MA20 的乖离：贴着均线说明刚启动，太远是追高
MA_GAP_IDEAL = (0.01, 0.08)
MA_GAP_CAP = 0.15

# 回踩不破
PULLBACK_LOOKBACK = 15  # 在最近多少天里找「先在上方、再回踩」
PULLBACK_ABOVE_GAP = 0.02  # 高于 MA20 这么多才算「在上方」
PULLBACK_TOLERANCE = 0.03  # 最低价距 MA20 多近算「踩到了」（允许下影线扎破）
PULLBACK_MAX_VOL_DRAG = 0.75  # 回踩期均量 / 上涨期均量 的上限
# 缩到这个比例以下算「缩量到位」，满分
PULLBACK_IDEAL_VOL_DRAG = 0.4
# 回踩至少要持续这么多天。只回调一天的话，那一天的量对比一段均量毫无意义 ——
# 实测放进来一大批「昨天在均线上方、今天碰一下均线」的日线噪声
PULLBACK_MIN_DIP_DAYS = 2
# **收盘**最多允许跌破 MA20 这么多。下影线扎破（`PULLBACK_TOLERANCE`）算回踩，
# 收盘站不回去就是破位了。少了这一条会误判：实测 002906 华阳集团连续 5 天收盘
# 跌破 MA20、最深 −3.11%，却因为「最低价碰到过均线」被打了 99.7 分
PULLBACK_MAX_CLOSE_BREAK = 0.02

# 创 N 日新高：从长周期往短周期试，取命中的最长那个
NEW_HIGH_WINDOWS = (250, 120, 60)
NEW_HIGH_BASE = {250: 92.0, 120: 75.0, 60: 55.0}
# 最长的窗口不能超过「可用 K 线数 - 1」：判 N 日新高要留出今天之外的 N 根。
# 实测库里的历史正好是 250 个交易日，所以 250 日新高实际按 249 日判 ——
# 差这一天对「创近一年新高」这个语义毫无影响，但代码与 detail 里要说实话
NEW_HIGH_MIN_WINDOW = 60

# 平台突破
PLATFORM_DAYS = 30
# 30 日振幅上限。实测全市场分位：10% 分位 = 12.9%、20% 分位 = 17.2%，
# 取 15% 只留 421 只候选、当天只有 6 只命中，偏严；放宽到 18% 留 681 只
PLATFORM_MAX_RANGE = 0.18
PLATFORM_VOL_MULT = 1.5
# 收盘高出平台上沿太多就不再是「突破」了，那是追高。
# 没有这条的话，一只票突破后会连着好几天留在榜上，越涨分越高（其实越危险）
PLATFORM_MAX_EXCESS = 0.08

# 放量突破前高
PRIOR_HIGH_DAYS = 60
PRIOR_HIGH_VOL_MULT = 2.0
PRIOR_HIGH_VOL_WINDOW = 20

# 放量上涨（量比按 5 日均量算，这是通行口径）
SURGE_MIN_PCT = 3.0
SURGE_MIN_VOL_RATIO = 2.0
SURGE_PCT_IDEAL = (3.0, 10.0)
SURGE_PCT_CAP = 21.0
SURGE_RATIO_IDEAL = (2.0, 6.0)
SURGE_RATIO_CAP = 12.0

# 缩量回踩
DRY_PULLBACK_MIN_DAYS = 3
DRY_PULLBACK_MAX_DAYS = 10
DRY_PULLBACK_DROP = (0.03, 0.15)
DRY_PULLBACK_MAX_VOL_RATIO = 0.6
# 缩到这个比例以下算「缩量到位」，满分
DRY_PULLBACK_IDEAL_VOL_RATIO = 0.35
DRY_PULLBACK_UP_WINDOW = 10  # 回调之前用多少天算「上涨期均量」

# ---- 几何形态（共用摆动点骨架）----

# 摆动点的最小反向幅度，按该股近一年的**日均振幅中位数**自适应再乘这个倍数。
# 固定百分比的问题：3% 对低波动股是一个大摆动，对高波动股是日常噪声，
# 同一套阈值在两端的严格程度能差一个数量级
SWING_AMP_MULT = 3.0
SWING_MIN = 0.06
SWING_MAX = 0.18
# 算日振幅中位数用多少根 K 线
SWING_AMP_WINDOW = 250

# 杯柄
CUP_WINDOW = 110
CUP_LEFT_END = 0.40  # 前 40% 里找左杯沿
CUP_RIGHT_START = 0.70  # 后 30% 才开始算右杯沿，保证杯底在中间
CUP_DEPTH = (0.15, 0.35)
CUP_RECOVER = 0.85  # 右杯沿至少回到「杯深」的多少比例
CUP_HANDLE_MIN_DAYS = 2
CUP_HANDLE_MAX_DAYS = 25
# 柄部回撤不能超过杯深的多少。O'Neil 的原始说法是「柄部落在基底上半部」，
# 理论上限 1/2、理想值 1/3。实测全市场 331 只候选里，柄部回撤/杯深的中位数
# 高达 74%、只有 19 只在 1/3 以内 —— 用 1/3 当门槛会一只都不剩，
# 所以**门槛放到 1/2，把「越接近 1/3 分越高」交给打分**
CUP_HANDLE_DEPTH_RATIO = 0.5
# 回撤到杯深的这个比例以内算「柄部很浅」，满分
CUP_HANDLE_IDEAL_RATIO = 0.2
CUP_HANDLE_VOL_RATIO = 0.9  # 柄部均量 / 右杯沿上涨段均量
# 缩到这个比例以下算「柄部缩量到位」，满分
CUP_HANDLE_IDEAL_VOL_RATIO = 0.4

# W 底（双底）
DOUBLE_BOTTOM_WINDOW = 130
DOUBLE_BOTTOM_MIN_BARS = 90
DOUBLE_BOTTOM_TOLERANCE = 0.05  # 两个低点相差
DOUBLE_BOTTOM_GAP = (15, 70)  # 两底间隔的交易日数
DOUBLE_BOTTOM_REBOUND = 0.10  # 中间反弹幅度
DOUBLE_BOTTOM_MAX_EXCESS = 0.06  # 收在颈线上方太多就不算「刚突破」了

# 三角收敛
TRIANGLE_WINDOW = 110
TRIANGLE_MIN_PIVOTS = 3  # 高点、低点各至少这么多个才谈得上「递降/递升」
TRIANGLE_CONVERGE = 0.5  # 末端振幅收敛到起点的一半以内
TRIANGLE_APEX_GAP = 0.6  # 收盘要在上下边界之间；离上边界多近算「贴着待突破」

# 头肩底
HS_WINDOW = 150
HS_MIN_BARS = 100
HS_SHOULDER_TOLERANCE = 0.10  # 两肩低点相差
HS_NECK_TOLERANCE = 0.08  # 两个颈线高点相差，也就是颈线有多「平」
HS_MIN_DEPTH = 0.08  # 头比颈线低多少才算一个头，低于这个数只是小抖动
HS_MAX_EXCESS = 0.06  # 收在颈线上方太多就不算「刚突破」
HS_MAX_TAIL = 20  # 右肩之后最多再等这么多根 K 线，再久这形态就翻篇了

# 旗形（急涨之后的窄幅整理）
FLAG_POLE_DAYS = 10  # 旗杆长度
FLAG_MIN_POLE = 0.15  # 旗杆至少涨这么多才算「急涨」
FLAG_MIN_DAYS = 3  # 旗面最短 / 最长——太短不叫整理，太长那叫趋势
FLAG_MAX_DAYS = 20
FLAG_MAX_RANGE = 0.12  # 旗面振幅，收窄才算整理
FLAG_MAX_RETRACE = 0.5  # 回撤占旗杆的比例
FLAG_MAX_VOL_RATIO = 0.85  # 旗面均量 / 旗杆均量
FLAG_MIN_POSITION = 0.55  # 收盘在旗面区间里的位置
FLAG_MAX_EXCESS = 0.05  # 收在旗面上沿上方太多就不算「贴着待突破」

# 三段式突破（缓涨 → 急涨 → 缩量整理 → 突破整理段上沿）
#
# 这一组阈值是**回测出来的**，不是照着某一只票定的。回测脚本见
# `scripts/backtest_three_stage.py`：全市场 250 个交易日、2995 只票，
# 同一只票 20 个交易日内的重复信号已去重，收益按**同一天全市场平均**做基准。
#
# | 口径 | 信号数 | 5 日超额 | 10 日超额 |
# | --- | --- | --- | --- |
# | 宽（初版） | 87 | +3.4% | +4.7% |
# | **本组（中间）** | **36** | **+5.7%** | **+4.9%** |
# | 严（照抄单只票特征） | 8 | +7.9% | +2.9% |
#
# 取中间这组的原因：放宽到 87 个时超额明显下滑（阈值形同虚设），收紧到 8 个
# 时样本少到说明不了问题。
#
# ⚠️ **2026-09-19 复核：上面那张表已作废，别再引用它的数字。**
# 那张表是回测脚本**自带的一套草案实现**跑出来的 —— 阈值与这里的 THREE_* 各写各的，
# 两边早就漂移了；而且当时的取数窗口还被交易日历（预置到年底）截掉了最早三个月。
# 改成直接调生产函数 `_three_stage` 重跑后，真实数字是：
#
# | 口径 | 信号数 | 5 日超额 | 10 日超额 | 20 日超额 |
# | --- | --- | --- | --- | --- |
# | **生产（复核后）** | **72** | **+2.00%** | **+2.48%** | **+4.12%** |
#
# 也就是说原表把 10 日超额**高估了整整一倍**。
#
# ⚠️ 它的局限仍然要一起看，别只记超额：10 日**中位数 −0.10%**、20 日中位数
# **−2.44%** —— 赚的是少数大涨（最好 +102.7%、最差 −44.2%）。
# 这是低胜率高赔率的形态，不是「看到就买」。
THREE_SLOW_DAYS = 15  # 缓涨段取急涨之前的这么多天（不枚举长度，涨幅区间已能覆盖）
THREE_SLOW_GAIN = (0.06, 0.45)
THREE_SLOW_BULL = 0.52  # 缓涨段的阳线占比
THREE_SURGE_DAYS = (2, 3)
THREE_SURGE_GAIN = 0.11
THREE_SURGE_VOL = 1.5  # 急涨段均量 / 急涨前 5 日均量
THREE_FLAT_DAYS = (4, 15)
THREE_FLAT_RANGE = 0.14  # 整理段振幅，相对整理段均价
THREE_FLAT_VOL = 0.68  # 整理段均量 / 急涨段均量
THREE_FLAT_DROP = 0.50  # 从急涨段高点回落，不超过急涨涨幅的多少
THREE_BREAK_VOL = 1.3  # 突破日量 / 整理段均量

# 涨停爆量横盘（涨停爆量 → 突破前期平台 → 缩量横盘，当前仍在横盘段里）
#
# 来自用户举的一只真实票（共达电声 002655，2026-02 那段）：
#   02-10 涨停 + 7.55 倍量，收 14.08 越过 1 月下旬 13.65 的平台
#   02-13 冲到 16.00（自 12.80 起 +25%）
#   02-24~26 缩量横盘，区间 15.31~16.15（振幅 5.4%）、量能降到上冲段的 0.61 倍
#   02-27 再拉一个涨停
#
# 与「旗形」的区别：旗形只要「急涨 + 窄幅整理」，这里额外要求急涨**由涨停爆量
# 启动**、且**站上了前期平台** —— 多这两条把「一根阳线拉起、没有资金进场痕迹」
# 的票挡在外面。
# 与「三段式突破」的区别：三段式找的是**今天刚突破**的瞬间，这里突破已经完成，
# 找的是**正在横盘**的票（等二次启动），两者的买点完全不同。
#
# ⚠️ 下面这组阈值起步时是按**单只票**定的，靠全市场扫描 + 回测校准过
# （过程与实测数据见设计文档 8.16.3），别直接改成某只票身上的数字。
LS_LOOKBACK = 30  # 在最近这么多天里找涨停爆量日
LS_LIMIT_PCT = 9.5  # 「涨停」的下限，留出主板 10% 的统计余量（20cm 板也一并覆盖）
LS_SURGE_VOL = 2.5  # 涨停日量 / 前 5 日均量
LS_BREAK_DAYS = 3  # 涨停后这么多天内要完成对平台的收盘突破
LS_PLATFORM_DAYS = 40  # 「前期平台」回看多少个交易日
LS_MIN_RUN = 0.15  # 涨停前收盘 → 上冲段最高点，至少要涨这么多
LS_FLAT_DAYS = (2, 15)  # 横盘段长度（含今天）
LS_FLAT_RANGE = 0.12  # 横盘段振幅上限，相对横盘段均价
LS_FLAT_VOL = 0.75  # 横盘段均量 / 上冲段均量
LS_FLAT_KEEP = 0.5  # 横盘低点至少守住「平台上沿 → 上冲最高点」这段的多少

# 突破后横盘（放量突破 → 缩量横着 → 今天仍在横盘区间里，等二次启动）
#
# 与「涨停爆量横盘」的区别：那个要求突破**由涨停爆量启动**、且站上**前期平台**；
# 这里只要「放量突破 60 日新高」、之后横着不跌回突破位 —— 门槛低得多，
# 频率也低得多（涨停爆量横盘每天十几只，这里平均每 4 个交易日才一只）。
#
# 阈值与三段式一样是**回测出来的**（`scripts/backtest_three_stage.py
# --pattern breakout_flat`，现在直接调本函数跑）。250 个交易日、2995 只票：
#
# | 口径 | 信号 | 5 日超额 | 10 日超额 | 20 日超额 | 10 日胜率 | 10 日中位 |
# | --- | --- | --- | --- | --- | --- | --- |
# | **本组（放宽）** | **63** | **+3.53%** | **+4.03%** | +1.58% | **61.9%** | **+2.04%** |
# | 收紧（振幅 8% / 缩量 50%） | 12 | −1.69% | −4.91% | −8.21% | 25.0% | −5.80% |
#
# 收紧那组是**负超额** —— 这个形态不是越严越好，所以取了上面这组。
# 它曾经被否掉过（当时的记录是「比三段式弱」），那是**取数窗口被日历表截断**
# 造成的误判，详见设计文档 8.16.3。
#
# ⚠️ 爆发力集中在 5~10 日，20 日之后开始回吐（60 日中位 −5.18%）—— 短打标的。
BF_MIN_FLAT = 3  # 横盘至少几天
BF_MAX_GAP = 12  # 突破日距今天最多几天
BF_MIN_GAIN = 0.08  # 突破日涨幅
BF_MIN_VOL = 2.0  # 突破日量 / 前 5 日均量
BF_PRIOR_HIGH = 60  # 突破要创这么多日的新高，否则只是「涨了一天」
BF_MAX_RANGE = 0.12  # 横盘振幅，相对横盘均价
BF_MAX_GIVEBACK = 0.03  # 横盘最低点允许比突破日收盘低多少
BF_MAX_SHRINK = 0.60  # 横盘均量 / 突破日量
# 今天仍在横盘区间里的容差：越过上沿 / 下沿这么多就不算了（已经突破或已经破位）
BF_EDGE_TOLERANCE = 0.02

# N 字选股（放量大涨 → 缩量回调到起涨点附近）
#
# 名字是用户起的，形态就是他说的那三步：**一笔上（放量大涨）→ 一折下（缩量回踩起点）
# → 等再一笔上**。起涨点取**大涨那天的起点**（开盘价与前一日收盘取低的，含跳空），
# 这是它与「缩量回踩」「回踩不破」最大的区别 —— 那两个看均线或涨幅比例，
# 这里看的是**涨势的起点有没有被回踩确认**。
#
# ⚠️ 它**不是回测出来的形态，也没有回测支持**：用户明确说「后续涨跌无所谓」，
# 要的就是一份清单。实测一年 8000+ 个信号、**平均每天 70 个**（占全市场 2.4%），
# 拿 N 日收益去衡量时超额接近 0（5 日 +0.11%、10 日 −0.16%）。
# 所以它的定位是「按这个形状捞一批票自己看」，不是高胜率信号 —— **别按信号去理解它**。
NS_SURGE_PCT = 5.0  # 「放量大涨」：单日涨幅
NS_SURGE_VOL = 2.0  # 放量：量 / 前 5 日均量
NS_PULL_DAYS = (1, 5)  # 缩量回调的天数
NS_LOW_BAND = (0.95, 1.03)  # 回调最低点 / 起涨点：既不能破位太多，也得真踩到
NS_NOW_BAND = (0.96, 1.08)  # 今天的收盘 / 起涨点
NS_MAX_SHRINK = 0.95  # 回调期均量 / 大涨日量

# 爆量后缩量回踩（爆量日成交额 ≥ 前一日 2 倍且阳线 → 之后某天缩到爆量日一半以下）
#
# 用户举了 7 只票定义这个形态（昆船智能 / 奥佳华 / 内蒙新华 / 博通集成 / 新强联 /
# 继峰股份 / 金丹科技），规则完全一致：
#
#   爆量日：成交额 ≥ 上一交易日成交额 × 2（「为上一交易日两倍以上」），**且是阳线**
#   （收 > 开。2026-09-25 补：放量下跌的「爆量」是出货，不是「缩量回踩」）
#   缩量日：爆量日**之后**的某天，成交额 ≤ 爆量日成交额 × 0.5（「为爆量日一半以下」）
#   **要选出来的就是缩量日**（不是爆量日）
#
# 爆量日看成交额 + 阳线，缩量日只看成交额。爆量日到缩量日的间隔在案例里从 1 到
# 7 个交易日不等（继峰股份 1 天、博通集成 7 天）。
BURST_SHRINK_MULT = 2.0  # 爆量日成交额 / 前一日成交额 的下限（「两倍以上」）
BURST_SHRINK_RATIO = 0.5  # 缩量日成交额 / 爆量日成交额 的上限（「一半以下」）
BURST_SHRINK_LOOKBACK = 10  # 往前找爆量日最多看几个交易日（案例最长 7 天）
# 打分里「爆量倍数」的理想值：≥ 这个倍数的爆量算满分（越猛越醒目，越大不扣分）
BURST_SHRINK_MULT_IDEAL = 5.0
BURST_SHRINK_IDEAL_RATIO = 0.3  # 缩到爆量日的 30% 算「缩量到位」，满分
BURST_SHRINK_GAP_IDEAL = (1.0, 5.0)  # 爆量日到缩量日间隔（交易日）的理想区间
BURST_SHRINK_GAP_CAP = 10.0  # 隔得太久说明「缩量回踩」这层关系已经散了

# 欧奈尔突破（O'Neil 的「枢轴点买入」）
#
# 来自《笑傲股市》那套买点，但只取**技术面**部分 —— 我们只有价量数据，
# 没有盈利（C/A）与机构持仓（I），做不了完整的 CANSLIM。剩下四条对应：
#
#   N（新高）：  基底上沿要贴近一年最高价（5% 以内）
#   整理：       基底 3 周~3 个月（20/30/45/65/90 日候选），期间回撤 ≤15%
#   S（量价）：  突破日成交量 ≥ 50 日均量的 1.4 倍（他说的「高出 40% 以上」）
#   L（强度）：  RS 评级 ≥ 80（全市场横截面百分位，见 `compute_rs`）
#
# ⚠️ **它只能当清单工具，不是信号。** 根源是缺了 CANSLIM 的发动机（C/A 盈利项）：
# 光靠「贴新高 + 低回撤 + 高 RS + 长均线之上」，在 A 股筛出来的是**银行、红利、
# 公用事业**这类稳健股（实测：建行、工行、招行、伊利、格力、粤高速…），
# 不是《笑傲股市》要的爆发股。回测同样不支持它 —— 各口径超额都在负区间，
# 而样本受 RS 预热限制只有十几到几十个、还集中在两个月，本来也说明不了问题。
#
# 试过并放弃的补救：加「日均振幅 ≥2.5%」挡红利股 —— 实测没挡住（那阵银行自己
# 波动就大），反而把样本从 32 砍到 14。**缺基本面，靠技术面补不上。**
#
# 想要爆发股请用「突破后横盘」（10 日超额 +4.03%、胜率 61.9%，那个有回测支持）；
# 这条线等有了财务数据（盈利增速、机构持仓）再谈。
ON_BASE_LENGTHS = (20, 30, 45, 65, 90)  # 候选基底长度（交易日）
# 为什么只取这 5 个而不是逐日枚举：逐日枚举一年要跑 1.7 亿次切片，实测会让
# 全市场扫描从 5 秒涨到一分钟以上 —— 而 5 个候选已经覆盖了 O'Neil 说的
# 「5~7 周到几个月」这个区间
ON_BASE_MAX_DROP = 0.15  # 基底内回撤上限（O'Neil：正常基底 15% 以内，25% 是熊市底）
ON_NEAR_HIGH = 0.05  # 基底上沿距一年最高价的上限（「贴近新高」）
ON_BREAK_VOL = 1.4  # 突破日量 / 50 日均量
ON_MAX_EXCESS = 0.05  # 突破幅度上限：追太多就不叫买点了
ON_MIN_GAIN = 0.0  # 近 120 个交易日涨幅下限
ON_MIN_RS = 80.0  # O'Neil 的建议：只买 RS 评级 80 以上（他偏好 87+）
# 近 60 个交易日的日均振幅下限：把低波动的红利股挡在清单外。
#
# ⚠️ 它**不是 O'Neil 的条件，而且挡不干净** —— 实测 2026-07 那阵银行自己波动也大，
# 2.5% 这道门槛仍然放进来建行、齐鲁银行。它的作用只是**让清单窄一点、好挑一点**，
# 别指望它区分「成长股 / 红利股」—— 那需要盈利数据。
# 取 2% 而不是最初试的 2.5%：这个形态定位是清单工具（见上），**太严会把清单筛空**，
# 宁可多看几只。它挡不掉所有红利股（上面的限制仍在），只是把最「静止」的那批排除掉。
ON_MIN_AMPLITUDE = 0.02

# 悟道之路 · 致富选股（样板 → 启动）
# 扫描侧只扫「强势/涨停/昨涨停/涨幅榜」创业板小池，与悟道名单对齐（见 jobs/scan_patterns.py）
# 量比口径与本地悟道之路一致：当日量 / 近 20 日均量（不是 5 日）
WUDAO_SAMPLE_HIGH_PCT = 7.0
WUDAO_SAMPLE_VOL = 1.3
WUDAO_START_VOL = 1.5
WUDAO_DIVERGE_VOL = 2.5  # 与悟道：爆量冲高不收 → 吵/出货，不当明天盯
WUDAO_MIN_BARS = 22  # 昨收 + 近 20 日均量 + 余量

# 欧奈尔 RS 评级：加权涨幅的窗口（交易日）与权重，照 IBD 的原始口径 ——
# 近 3 个月权重 0.4，其余三段各 0.2。最近这一段给双倍权重，是因为它最能
# 反映「当下有没有资金在做」，这也是 RS 评级比「近一年涨幅」灵敏的原因。
RS_WEIGHTS = ((63, 0.4), (126, 0.2), (189, 0.2), (252, 0.2))
# 参与排名的最低 K 线数。取 200 而不是 252：库里的历史是 250 个交易日，
# 卡 252 会让**一只票都进不来**（实测踩到：RS 全为 0、欧奈尔形态一个都不出）。
# 拿不到的窗口按下面的逻辑跳过并把权重归一化，排名依然是可比的。
RS_MIN_BARS = 200

# 引擎至少要这么多根 K 线才动手（MA60 + 斜率窗口）
MIN_BARS = MA_PERIODS[-1] + MA_SLOPE_LOOKBACK


# ------------------------------------------------- 新增形态阈值（2026-09-25）
#
# ⚠️ 这一批是**按经典定义实现的形状判定，没有回测支持** —— 与「N 字选股」同一定位：
# 作用是「按这个形状捞一批票自己看」，不是高胜率信号。已回测的那几个（三段式突破、
# 突破后横盘）仍以各自注释里那张表为准，别把这一批的分数当成同一回事。
#
# 也不要照着某一只票去调这里的数字。三段式与突破后横盘那两组是全市场扫描 + 回测
# 校准出来的；这一批至少先跑一遍全市场（`scripts/check_day.py` 会打印各形态家数），
# 确认命中量级合理再谈调参。

# 均线粘合（四条均线收敛到极窄区间、横住 → 蓄势）
SQUEEZE_DAYS = 5  # 要求粘合持续这么多天，只收敛一天是噪声
SQUEEZE_MAX_SPREAD = 0.03  # MA5/10/20/60 的 (max/min − 1) 上限
SQUEEZE_IDEAL_SPREAD = 0.008  # 收到这个宽度满分
SQUEEZE_SLOPE_MAX = 0.02  # 粘合期内 MA20 的涨幅上限（已经在拉就不是粘合）

# 上升通道
CHANNEL_DAYS = 40
CHANNEL_MIN_DAILY = 0.003  # 下轨斜率换成「每天涨百分之几」的下限（缓涨不算通道）
CHANNEL_PARALLEL_TOL = 0.45  # 上下轨斜率差 / 上轨斜率 的上限，再大就是喇叭形
CHANNEL_POSITION_IDEAL = (0.15, 0.55)  # 收盘在通道内的相对位置：贴近下轨才是买点
CHANNEL_POSITION_MAX = 0.75  # 超过这个位置就不算「回踩到位」了（已在通道上部）
CHANNEL_TOUCH_TOL = 0.03  # 轨被触碰的容差
CHANNEL_MIN_TOUCHES = 2  # 每条轨至少被碰这么多次（不然只是"两条线正好夹住"）
CHANNEL_TOUCH_FULL = 8  # 触碰这么多（上下合计）算通道很规整

# 金叉共振（MA5 上穿 MA10，且 MA10 / MA20 同向）
CROSS_LOOKBACK = 3  # 金叉要发生在最近这么多天内
CROSS_MIN_MA10_RISE = 0.0  # MA10 五日斜率下限
CROSS_MIN_MA20_RISE = -0.002  # MA20 允许略走平，但不能明显向下

# 周线多头（日线聚合成周线后套均线多头）
WEEKLY_BULL_MIN_BARS = 110  # 周线 20 根 + 斜率 4 根 ≈ 日线 110 根
WEEKLY_BULL_SLOPE_WEEKS = 4
WEEKLY_BULL_RISE_IDEAL = (0.005, 0.04)  # 周 MA10 四周涨幅的理想区间（太陡是加速末端）
WEEKLY_BULL_RISE_CAP = 0.10
WEEKLY_BULL_GAP_IDEAL = (0.005, 0.06)  # 当前价距周 MA5 的乖离

# 下降趋势线突破
TRENDLINE_DAYS = 60
TRENDLINE_MIN_PIVOTS = 2  # 至少两个下降的摆动高点才画得出趋势线
TRENDLINE_MAX_EXCESS = 0.08  # 突破幅度上限：追太多就不是买点

# 向上跳空缺口突破
GAP_MIN = 0.005  # 缺口宽度（今低 / 昨高 − 1）下限
GAP_IDEAL = (0.01, 0.05)
GAP_CAP = 0.09
GAP_BREAK_HIGH_DAYS = 20  # 缺口当日还要突破这么多天的收盘高点

# 缺口回补（向上缺口回踩不破 → 缺口变支撑）
GAP_FILL_LOOKBACK = 20  # 在最近这么多天里找还没被补掉的向上缺口
GAP_FILL_MIN_FILL = 0.2  # 今日最低至少扎进缺口这么多（占缺口宽度）
GAP_FILL_MAX_FILL = 0.95  # 全补满就不算「支撑有效」了

# 箱体突破（长箱体 + 上下沿反复验证）
BOX_DAYS = 60
BOX_MIN_RANGE = 0.08  # 太窄是平台，不是箱体
BOX_MAX_RANGE = 0.35
BOX_TOUCH_TOL = 0.025
BOX_MIN_TOUCHES = 2  # 上下沿各至少被碰这么多次
BOX_VOL_MULT = 1.3
BOX_MAX_EXCESS = 0.06

# 地量见底
DRY_BOTTOM_DAYS = 60  # 「地量」与「低位」的回看窗口
DRY_BOTTOM_VOL_MAX = 1.05  # 当日量 ≤ 窗口最低量 × 这个系数
DRY_BOTTOM_MIN_DRAWDOWN = 0.2  # 距窗口最高收盘的回撤下限（够低才算底）

# 量价底背离（价创新低、量能反而萎缩）
VP_DIVERGE_DAYS = 40
VP_DIVERGE_NEW_LOW = 0.01  # 收盘距窗口最低收盘在这个比例以内算「贴着新低」
VP_DIVERGE_VOL_DROP = 0.7  # 本次跌段均量 / 上次跌段均量 的上限

# 缩量涨停（涨停却没量 = 惜售）
SHRINK_LIMIT_VOL_MAX = 0.8  # 涨停日量 / 前 5 日均量 的上限

# 放量滞涨（放量了但价格没动、还留了长上影）
#
# ⚠️ 它是**警示类**形态，不是买点：高位放量滞涨多半是派发，低位才是吸筹嫌疑。
# 所以「距 60 日高点的回撤」只写进 detail、只参与打分，**不做否决** ——
# 一度把它当门槛（要求回撤 ≥15%），实测全市场只剩 0.2 次/天，等于页面上永远看不到；
# 而且那是我替用户做的解读，名字本身并没有「只在低位」的意思。位置自己看明细判断。
STALL_VOL_MULT = 1.8
STALL_PCT_BAND = (-0.015, 0.025)  # 当日涨幅落在这一段里才算「滞涨」
STALL_SHADOW_MIN = 0.28  # 上影线占全日振幅的比例下限
STALL_MIN_DRAWDOWN = 0.1  # 低于这个回撤就不给「低位」那部分加分

# 量堆（连续放量、价格重心同步上移）
PILE_DAYS = 5
PILE_MIN_DAYS = 4  # 这 5 天里至少几天量比达标
PILE_VOL_MULT = 1.5
PILE_RATIO_5_20 = 1.5  # 5 日均量 / 20 日均量

# V 型底
V_BOTTOM_DAYS = 40
V_BOTTOM_MIN_DROP = 0.18  # 左侧跌幅下限
V_BOTTOM_MIN_REBOUND = 0.75  # 右侧收复左侧跌幅的比例
V_BOTTOM_SYMMETRY = 1.8  # 左右两段天数之比的上限（V 要基本对称）

# 圆弧底
ROUND_DAYS = 90
ROUND_MIN_DAYS = 60
ROUND_MAX_AMPLITUDE = 0.35  # 窗口内 (最高 − 最低) / 最低 的上限：圆弧是平缓的
ROUND_MIN_RECOVER = 0.6  # 当前价距窗口低点的收复比例

# 三重底
TRIPLE_DAYS = 90
TRIPLE_LOW_TOL = 0.06  # 三个低点两两差距上限
TRIPLE_MAX_EXCESS = 0.06  # 突破颈线的幅度上限

# 矩形箱体（长矩形里贴着上沿，尚未突破）
RECT_DAYS = 80
RECT_MIN_RANGE = 0.1
RECT_MAX_RANGE = 0.45
RECT_POSITION_MIN = 0.7  # 收盘在箱体内的相对位置下限

# 楔形（收敛三角形；上升楔形偏空、下降楔形偏多）
WEDGE_DAYS = 50
WEDGE_MIN_DAYS = 30
WEDGE_MIN_SLOPE = 0.0004  # 两轨斜率绝对值的下限，再平就是箱体
WEDGE_MIN_CONVERGE = 0.55  # 收敛比例下限 (起始间距 − 结束间距) / 起始间距
WEDGE_TOUCH_TOL = 0.03  # 轨被触碰的容差
WEDGE_MIN_TOUCHES = 3  # 每条轨至少被碰这么多次（真楔形的高/低点会反复贴轨）
WEDGE_FALL_POSITION_MIN = 0.35  # 下降楔形要求收盘在楔形上部（跌势末端、准备向上突）
WEDGE_FALL_GAP_MAX = 0.08  # 下降楔形末端两轨的间距上限（占均价）—— 成熟楔形快汇合了

# 菱形（先扩张后收敛）
DIAMOND_DAYS = 70
DIAMOND_MIN_DAYS = 45
DIAMOND_MIN_EXPAND = 0.25  # 前半段振幅扩张比例下限
DIAMOND_MIN_CONTRACT = 0.4  # 后半段振幅收敛比例下限
DIAMOND_MIDDLE_EDGE = 1.6  # 中段振幅至少要达到两端的这个倍数（不然只是普通波动）

# 蜡烛形态（单根 / 两根 / 三根）
CANDLE_BODY_MAX = 0.35  # 小实体：实体 / 全日振幅 的上限
CANDLE_SHADOW_MULT = 2.0  # 长影线至少是实体的多少倍
CANDLE_SHORT_SHADOW = 0.3  # 另一侧影线占振幅的上限
CANDLE_PRIOR_DAYS = 5
CANDLE_PRIOR_DROP = 0.03  # 前置下跌幅度：反转类蜡烛要在跌势之后出现才有意义
ENGULF_MIN_BODY = 1.3  # 今日实体 / 昨日实体 的下限
ENGULF_MIN_CLOSE_POS = 0.6  # 今日收盘要落在全日振幅的这个位置以上（收在上半部）
STAR_BIG_BODY = 0.015  # 启明星两侧大实体的最小幅度（相对价格）
STAR_SMALL_BODY = 0.5  # 中间小实体 / 左侧实体 的上限
STAR_RECOVER = 0.5  # 右侧收盘要收复左侧实体这么多
SOLDIER_MIN_BODY = 0.3  # 红三兵每根实体占振幅的下限
SOLDIER_MAX_SHADOW = 0.35  # 每根上影线占振幅的上限
SOLDIER_MIN_GAIN = 0.03  # 三根累计涨幅下限


# ---------------------------------------------------------------- 数据结构


@dataclass(slots=True)
class Bars:
    """一只票的日线序列，价格**已前复权**、按交易日升序。"""

    dates: list[date]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    amount: np.ndarray
    pct_chg: np.ndarray
    # 欧奈尔 RS 评级（1~99，全市场横截面百分位）。
    #
    # 它是**横截面量** —— 单只票自己算不出来，必须等全市场的 K 线都建好之后
    # 统一排名，所以由 `compute_rs` 事后写进来，而不是 `build_bars` 里算。
    #
    # 默认 0 表示「没算过」（个股页只取一只票、单元测试、回测切片都会走到这里）。
    # 依赖它的形态会**主动跳过**，而不是把 0 当成「最弱」误杀 —— 这个区别很重要：
    # 拿 0 当最弱的话，个股页单独跑形态时会永远出不了信号，且没人看得出为什么。
    rs: float = 0.0

    def __len__(self) -> int:
        return int(self.close.size)


@dataclass(slots=True)
class Signal:
    """一个形态的命中结果。"""

    pattern: str
    score: float
    key_levels: dict[str, float] = field(default_factory=dict)
    detail: dict[str, float | int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Pattern:
    key: str
    name: str
    # 趋势 / 突破 / 量价 / 几何
    group: str
    detect: Callable[[Bars], Signal | None]


# ---------------------------------------------------------------- 复权


def build_bars(records: list[dict]) -> Bars:
    """把一只票的**不复权**日线记录转成前复权序列。

    `records` 必须按交易日升序，每项含 `date / open / high / low / close /
    volume / amount / pct_chg`（值为 None 的请调用方先过滤掉）。

    ## 为什么用涨跌幅而不是「前复权价」

    iFinD 不给前复权价，但 `涨跌幅` 是**已按除权调整的真实收益率**（实测验证过
    21 个银行除权日）。所以：

    - 收盘序列：`net[i] = net[i-1] * (1 + pct[i]/100)`，即真实收益的复利
    - 当日 OHLC：用**当天收盘价**做基准换算 —— `adj_x[i] = x[i] * net[i] / close[i]`

    第二步只能按天内的比例换算，不能乘一个累计因子。因为除权调整发生在
    当日开盘相对昨日收盘之间，**同一根 K 线内部的四个价格本来就在同一个基准上**；
    乘累计因子会把整根 K 线一起挪走，反而制造出一个假的跳空。

    锚点取「最新一根的收盘价」：前复权的通行定义就是以最新价为基准，
    这样最近的关键位（突破价等）可以直接当作真实价格看。
    """
    if not records:
        raise ValueError("build_bars 收到空序列")

    close = np.array([float(r["close"]) for r in records], dtype=float)
    pct = np.array([float(r["pct_chg"] or 0.0) for r in records], dtype=float)

    # 首日没有涨跌幅，净值从 1 起算
    net = np.empty(len(records), dtype=float)
    net[0] = 1.0
    if len(records) > 1:
        net[1:] = np.cumprod(1.0 + pct[1:] / 100.0)
    # 锚到最新价：adj[-1] == 原始收盘价
    net *= close[-1] / net[-1]

    # 日内比例换算。close 理论上不会为 0，真出现就整根丢掉意义，直接置 1 避免除零
    ratio = np.divide(net, close, out=np.ones_like(net), where=close != 0)

    return Bars(
        dates=[r["date"] for r in records],
        open=np.array([float(r["open"]) for r in records]) * ratio,
        high=np.array([float(r["high"]) for r in records]) * ratio,
        low=np.array([float(r["low"]) for r in records]) * ratio,
        close=net,
        volume=np.array([float(r["volume"] or 0.0) for r in records]),
        amount=np.array([float(r["amount"] or 0.0) for r in records]),
        pct_chg=pct,
    )


def compute_rs(bars_by_code: dict[str, Bars]) -> None:
    """算欧奈尔 RS 评级（1~99），**就地写进每只票的 `bars.rs`**。

    口径照 IBD 的原始定义：先算加权涨幅
        RS_raw = 0.4×近 3 月 + 0.2×近 6 月 + 0.2×近 9 月 + 0.2×近 12 月
    再按**全市场横截面**排名取百分位。

    关键是「横截面」这三个字：它衡量的是**比多少票强**，不是涨了多少。
    所以它不随大盘涨跌漂移 —— 熊市里普跌 30% 而它只跌 10%，RS 照样很高，
    这正是 O'Neil 要的「相对强度」（他一句名言就是「买最强的票，别买最便宜的」）。

    全市场 K 线已经都在手上，一次排序就够，比逐票查库便宜得多。
    """
    raw: dict[str, float] = {}
    for code, bars in bars_by_code.items():
        if len(bars) < RS_MIN_BARS:
            continue
        close = bars.close
        latest = float(close[-1])
        if latest <= 0:
            continue
        piece = 0.0
        weight_sum = 0.0
        for window, weight in RS_WEIGHTS:
            if len(close) <= window:
                # 这一年窗口拿不到（历史不够长）。**跳过并把权重归一化**，
                # 而不是当成 0 涨幅 —— 后者会让所有票一起被拉向中间值
                continue
            base = float(close[-1 - window])
            if base <= 0:
                continue
            piece += weight * (latest / base - 1)
            weight_sum += weight
        if weight_sum <= 0:
            continue
        raw[code] = piece / weight_sum

    if not raw:
        return
    order = sorted(raw, key=lambda item: raw[item])
    total = len(order)
    for rank, code in enumerate(order):
        # 映射到 1~99：最弱的 1、最强的 99（IBD 的评级就是这个量纲）
        bars_by_code[code].rs = round(rank / max(total - 1, 1) * 98) + 1


# ---------------------------------------------------------------- 打分工具


def _gate_score(value: float, gate: float, ideal: float) -> float:
    """「过了门槛就给分，到理想值满分」的线性映射，方向由 `gate` 与 `ideal` 的大小决定。

    - `ideal < gate`：越小越好（缩量幅度、回撤深度、两底差距…）
    - `ideal > gate`：越大越好（恢复比例、收回力度…）

    和 `_band_score` 的分工：有两个方向都扣分的用 `_band_score`（乖离率、放量倍数
    —— 太弱不行、太强也不行）；只有一个方向的用这个。

    **不要把 `gate` 当成满分点**：那样刚过门槛的样本会直接拿 0 分，等于把
    「能接受的最差值」和「理想值」混成一件事。实测这么写会让「回踩不破」
    从 80 只掉到 16 只 —— 分数低到进不了库，而不是判定变严了。
    """
    if gate == ideal:
        return 1.0
    if ideal < gate:  # 越小越好
        if value >= gate:
            return 0.0
        if value <= ideal:
            return 1.0
        return (gate - value) / (gate - ideal)
    if value <= gate:  # 越大越好
        return 0.0
    if value >= ideal:
        return 1.0
    return (value - gate) / (ideal - gate)


def _band_score(value: float, low: float, high: float, cap: float) -> float:
    """把「理想区间」映射到 0~1：区间**中心给满分**，两端降到 `EDGE_SCORE`，
    区间外线性衰减，`cap` 之外归零。

    为什么区间内不一律给满分：实测「放量上涨」的 80 只里，有 64 只同时落在
    涨幅 3~10% 且量比 2~6 的理想区间，于是全部并列 100 分 —— 排名就失去意义了。
    改成余弦钟形后中心唯一最高，既能分辨「教科书级」和「刚够格」，
    又不改变「落在区间里都算不错」的语义。

    **`high` 必须严格小于 `cap`。** 两者相等时区间上界恰好也是衰减终点，
    值正好落在那里会走 `value >= cap` 分支拿 0 分 —— 而那恰恰是最优值。
    这个坑我踩过 6 次（如「收盘从未跌破 MA20」得 0 分），单方向的打分一律用
    `_ramp`，别用这个。
    """
    if value <= 0:
        return 0.0
    center = (low + high) / 2
    if low <= value <= high:
        return EDGE_SCORE + (1.0 - EDGE_SCORE) * math.cos(
            (value - center) / (high - low) * math.pi
        )
    if value < low:
        return EDGE_SCORE * (value / low)
    if value >= cap:
        return 0.0
    return EDGE_SCORE * (cap - value) / (cap - high)


def _ma_series(values: np.ndarray, window: int) -> np.ndarray | None:
    """滑动均值序列，长度 = len(values) - window + 1。数据不足返回 None。"""
    if values.size < window:
        return None
    cumulative = np.concatenate([[0.0], np.cumsum(values)])
    return (cumulative[window:] - cumulative[:-window]) / window


def _align(series: np.ndarray | None, window: int, length: int) -> np.ndarray:
    """把均线序列对齐到 K 线的索引，前面不足的部分填 NaN。

    对齐后 `result[i]` 就是第 i 根 K 线的均线值，省得每个形态各写一遍索引换算。
    """
    out = np.full(length, np.nan)
    if series is None:
        return out
    usable = min(series.size, length - window + 1)
    if usable > 0:
        out[window - 1 : window - 1 + usable] = series[:usable]
    return out


def _rising(series: np.ndarray, lookback: int) -> bool:
    return series.size > lookback and series[-1] > series[-1 - lookback]


def _trailing_run(flags: np.ndarray) -> int:
    """从尾部往前数，连续的 True 有多少个。"""
    count = 0
    for flag in flags[::-1]:
        if not flag:
            break
        count += 1
    return count


def _safe_mean(values: np.ndarray) -> float:
    finite = values[np.isfinite(values)]
    return float(finite.mean()) if finite.size else 0.0


def _volume_ratio(bars: Bars, window: int) -> float:
    """当日成交量 / 前 window 日均量。"""
    if len(bars) < window + 1:
        return 0.0
    base = _safe_mean(bars.volume[-window - 1 : -1])
    return float(bars.volume[-1] / base) if base > 0 else 0.0


# ---------------------------------------------------------------- 形态实现


def _ma_bull(bars: Bars) -> Signal | None:
    """均线多头排列：MA5>MA10>MA20>MA60，且四条均线近期都在向上。"""
    if len(bars) < MIN_BARS:
        return None

    series = [_ma_series(bars.close, period) for period in MA_PERIODS]
    if any(item is None for item in series):
        return None

    # 四条均线起止日期不同，取公共长度后再比大小
    shared = min(item.size for item in series if item is not None)
    if shared <= MA_SLOPE_LOOKBACK:
        return None
    m5, m10, m20, m60 = (item[-shared:] for item in series if item is not None)

    stacked = (m5 > m10) & (m10 > m20) & (m20 > m60)
    if not stacked[-1]:
        return None
    if not all(_rising(item, MA_SLOPE_LOOKBACK) for item in (m5, m10, m20, m60)):
        return None

    days = _trailing_run(stacked)
    spread = float((m5[-1] - m60[-1]) / m60[-1])
    gap = float((bars.close[-1] - m20[-1]) / m20[-1])

    score = min(days, MA_STACK_FULL_DAYS) / MA_STACK_FULL_DAYS * 40
    score += _band_score(spread, *MA_SPREAD_IDEAL, MA_SPREAD_CAP) * 30
    score += _band_score(gap, *MA_GAP_IDEAL, MA_GAP_CAP) * 30

    return Signal(
        "ma_bull",
        score,
        {"support": float(m20[-1]), "ma5": float(m5[-1]), "ma10": float(m10[-1])},
        {"stack_days": days, "spread": round(spread, 4), "gap": round(gap, 4)},
    )


def _ma_pullback(bars: Bars) -> Signal | None:
    """回踩不破：先站上 MA20，再回踩到 MA20 附近缩量收回，且没跌破。"""
    if len(bars) < MIN_BARS + PULLBACK_LOOKBACK:
        return None

    ma20 = _align(_ma_series(bars.close, 20), 20, len(bars))
    window = slice(len(bars) - PULLBACK_LOOKBACK, len(bars))
    above = bars.close[window] > ma20[window] * (1 + PULLBACK_ABOVE_GAP)
    if not above.any():
        return None

    # 回踩只可能发生在「最后一次站上 MA20」之后
    last_above = len(bars) - PULLBACK_LOOKBACK + int(np.flatnonzero(above)[-1])
    if last_above >= len(bars) - 1:
        # 最后一天还在上方，压根没回踩
        return None

    dip = slice(last_above + 1, len(bars))
    dip_days = len(bars) - last_above - 1
    if dip_days < PULLBACK_MIN_DIP_DAYS:
        return None
    if ma20[dip].size == 0 or not np.isfinite(ma20[dip]).all() or (ma20[dip] <= 0).any():
        return None

    distance = np.abs(bars.low[dip] - ma20[dip]) / ma20[dip]
    touched = float(distance.min())
    if touched > PULLBACK_TOLERANCE:
        return None
    # 下影线扎破均线算回踩，**收盘**站不回去就是破位 —— 这两件事必须分开判
    deepest_break = float(((ma20[dip] - bars.close[dip]) / ma20[dip]).max())
    if deepest_break > PULLBACK_MAX_CLOSE_BREAK:
        return None
    if bars.close[-1] <= ma20[-1]:
        # 收在 MA20 下方，那就是破了，不是「回踩不破」
        return None

    up_volume = _safe_mean(bars.volume[window][above])
    dip_volume = _safe_mean(bars.volume[dip])
    drag = dip_volume / up_volume if up_volume > 0 else 1.0
    if drag > PULLBACK_MAX_VOL_DRAG:
        return None

    # 缩量越狠越好、踩得越浅越好、收盘守得越稳越好、收回力度适中
    score = _gate_score(drag, PULLBACK_MAX_VOL_DRAG, PULLBACK_IDEAL_VOL_DRAG) * 40
    score += _gate_score(touched, PULLBACK_TOLERANCE, 0.005) * 20
    score += _gate_score(max(deepest_break, 0.0), PULLBACK_MAX_CLOSE_BREAK, 0.0) * 20
    reclaim = float((bars.close[-1] - ma20[-1]) / ma20[-1])
    score += _gate_score(reclaim, 0.002, 0.05) * 20

    return Signal(
        "ma_pullback",
        score,
        {"support": float(ma20[-1]), "dip_low": float(bars.low[dip].min())},
        {
            "dip_low_vs_ma20": round(touched, 4),
            "close_break": round(deepest_break, 4),
            "vol_drag": round(drag, 3),
            "dip_days": dip_days,
        },
    )


def _new_high(bars: Bars) -> Signal | None:
    """创 N 日新高：收盘价高于前 N 根 K 线的最高收盘价，取命中的最长周期。"""
    for window in NEW_HIGH_WINDOWS:
        usable = min(window, len(bars) - 1)
        if usable < NEW_HIGH_MIN_WINDOW:
            continue
        prior = float(bars.close[-usable - 1 : -1].max())
        if bars.close[-1] <= prior:
            continue

        excess = float(bars.close[-1] / prior - 1)
        ratio = _volume_ratio(bars, 5)
        score = NEW_HIGH_BASE[window]
        score += _band_score(excess, 0.005, 0.03, 0.10) * 4
        # 量能按连续量给分，而不是「过 1.2 倍就 +3」的开关 ——
        # 后者会让所有放量的新高票挤在同一个分数上，榜首失去区分度
        score += min(ratio / 3.0, 1.0) * 4
        return Signal(
            "new_high",
            min(score, 100.0),
            {"breakout": prior},
            {"window": usable, "excess": round(excess, 4), "vol_ratio": round(ratio, 2)},
        )
    return None


def _platform_breakout(bars: Bars) -> Signal | None:
    """平台突破：前期窄幅横盘，今天放量收在平台上沿之上。"""
    need = PLATFORM_DAYS + 5
    if len(bars) < need:
        return None

    box = slice(len(bars) - 1 - PLATFORM_DAYS, len(bars) - 1)
    top = float(bars.high[box].max())
    bottom = float(bars.low[box].min())
    if bottom <= 0:
        return None
    width = (top - bottom) / bottom
    if width >= PLATFORM_MAX_RANGE:
        return None
    if bars.close[-1] <= top:
        return None

    ratio = _volume_ratio(bars, 5)
    if ratio < PLATFORM_VOL_MULT:
        return None

    excess = float(bars.close[-1] / top - 1)
    if excess > PLATFORM_MAX_EXCESS:
        return None

    # 平台越窄越好、放量越足越好、突破幅度适中
    score = _band_score(PLATFORM_MAX_RANGE - width, 0.02, 0.12, 0.15) * 40
    score += _band_score(ratio, PLATFORM_VOL_MULT, 4.0, 8.0) * 35
    score += _band_score(excess, 0.005, 0.04, 0.10) * 25

    return Signal(
        "platform_breakout",
        score,
        {"breakout": top, "support": bottom},
        {"platform_width": round(width, 4), "vol_ratio": round(ratio, 2),
         "excess": round(excess, 4)},
    )


def _volume_breakout(bars: Bars) -> Signal | None:
    """放量突破前高：突破 60 日前高，且当日量显著放大。"""
    if len(bars) < PRIOR_HIGH_DAYS + PRIOR_HIGH_VOL_WINDOW:
        return None

    prior = float(bars.close[-PRIOR_HIGH_DAYS - 1 : -1].max())
    if bars.close[-1] <= prior:
        return None

    ratio = _volume_ratio(bars, PRIOR_HIGH_VOL_WINDOW)
    if ratio < PRIOR_HIGH_VOL_MULT:
        return None

    excess = float(bars.close[-1] / prior - 1)
    score = _band_score(ratio, PRIOR_HIGH_VOL_MULT, 5.0, 10.0) * 60
    score += _band_score(excess, 0.005, 0.04, 0.10) * 40

    return Signal(
        "volume_breakout",
        score,
        {"breakout": prior},
        {"vol_ratio": round(ratio, 2), "excess": round(excess, 4)},
    )


def _volume_surge(bars: Bars) -> Signal | None:
    """放量上涨：当日大涨且量比显著高于 5 日均量。"""
    if len(bars) < 6:
        return None

    pct = float(bars.pct_chg[-1])
    if pct < SURGE_MIN_PCT:
        return None
    ratio = _volume_ratio(bars, 5)
    if ratio < SURGE_MIN_VOL_RATIO:
        return None

    score = _band_score(pct, *SURGE_PCT_IDEAL, SURGE_PCT_CAP) * 50
    score += _band_score(ratio, *SURGE_RATIO_IDEAL, SURGE_RATIO_CAP) * 50

    return Signal(
        "volume_surge",
        score,
        {},
        {"pct_chg": round(pct, 2), "vol_ratio": round(ratio, 2)},
    )


def _dry_pullback(bars: Bars) -> Signal | None:
    """缩量回踩：高位回调但量能明显萎缩，且没跌破 MA20。"""
    if len(bars) < MA_PERIODS[-1] + DRY_PULLBACK_UP_WINDOW + DRY_PULLBACK_MAX_DAYS:
        return None

    ma20 = _align(_ma_series(bars.close, 20), 20, len(bars))
    if not np.isfinite(ma20[-1]) or ma20[-1] <= 0:
        return None
    if bars.close[-1] <= ma20[-1]:
        return None

    best: Signal | None = None
    for days in range(DRY_PULLBACK_MIN_DAYS, DRY_PULLBACK_MAX_DAYS + 1):
        seg = slice(len(bars) - days, len(bars))
        peak = float(bars.high[seg].max())
        if peak <= 0:
            continue
        drop = (peak - float(bars.close[-1])) / peak
        if not (DRY_PULLBACK_DROP[0] <= drop <= DRY_PULLBACK_DROP[1]):
            continue

        prior = slice(len(bars) - days - DRY_PULLBACK_UP_WINDOW, len(bars) - days)
        up_volume = _safe_mean(bars.volume[prior])
        if up_volume <= 0:
            continue
        ratio = _safe_mean(bars.volume[seg]) / up_volume
        if ratio > DRY_PULLBACK_MAX_VOL_RATIO:
            continue

        gap = float((bars.close[-1] - ma20[-1]) / ma20[-1])
        score = _gate_score(ratio, DRY_PULLBACK_MAX_VOL_RATIO, DRY_PULLBACK_IDEAL_VOL_RATIO) * 45
        # 回调幅度取区间中段最好：太浅没洗够，太深就成破位了。这个是双向的，用 _band_score
        mid = sum(DRY_PULLBACK_DROP) / 2
        span = (DRY_PULLBACK_DROP[1] - DRY_PULLBACK_DROP[0]) / 2
        score += _band_score(span - abs(drop - mid), 0.005, span * 0.8, span * 1.2) * 30
        score += _gate_score(gap, 0.002, 0.05) * 25

        candidate = Signal(
            "dry_pullback",
            score,
            {"support": float(ma20[-1]), "peak": peak},
            {"pullback_days": days, "drop": round(drop, 4), "vol_ratio": round(ratio, 3)},
        )
        if best is None or candidate.score > best.score:
            best = candidate
    return best


# ---------------------------------------------------------------- 摆动点骨架


def swing_threshold(bars: Bars) -> float:
    """摆动点的最小反向幅度，按该股近一年的日均振幅自适应。

    **这是几何形态唯一的主观旋钮**，也是它们最容易出问题的地方：阈值太小会把
    日常噪声当成一个「摆动」，于是满地都是杯柄和 W 底；太大则把真实形态整个吞掉。

    固定百分比做不到两头兼顾 —— 日均振幅 1.5% 的银行股和 6% 的题材股，
    同一个 5% 阈值前者是「趋势反转」、后者是「周二」。
    """
    window = min(len(bars), SWING_AMP_WINDOW)
    if window < 20:
        return SWING_MIN
    amplitude = (bars.high[-window:] - bars.low[-window:]) / bars.close[-window:]
    median = float(np.median(amplitude))
    return float(min(max(median * SWING_AMP_MULT, SWING_MIN), SWING_MAX))


def zigzag(high: np.ndarray, low: np.ndarray, threshold: float) -> list[tuple[int, float, int]]:
    """ZigZag 摆动点，返回 `[(索引, 价格, 方向)]`，方向 +1 为高点、-1 为低点。

    严格交替。做法是：顺着当前趋势跟踪极值，当反向幅度超过 `threshold` 时确认
    上一个极值、翻转方向。首个极值以第一根 K 线的高点起算 —— 起点选谁不影响
    形态判定，因为所有形态都只看相对关系。

    末尾那个**尚未被反向确认**的极值也会带上（假设趋势延续）。不加的话，
    「正在突破的路上」这类形态全都看不到 —— 而它们恰恰是最该被看到的。
    """
    n = int(high.size)
    if n < 3:
        return []

    pivots: list[tuple[int, float, int]] = []
    trend = 1  # 一开始假设在走高，从第一根的高点起算
    last_idx, last_price = 0, float(high[0])

    for index in range(1, n):
        if trend > 0:
            if high[index] > last_price:
                last_idx, last_price = index, float(high[index])
            elif low[index] <= last_price * (1 - threshold):
                pivots.append((last_idx, last_price, 1))
                trend = -1
                last_idx, last_price = index, float(low[index])
        else:
            if low[index] < last_price:
                last_idx, last_price = index, float(low[index])
            elif high[index] >= last_price * (1 + threshold):
                pivots.append((last_idx, last_price, -1))
                trend = 1
                last_idx, last_price = index, float(high[index])

    pivots.append((last_idx, last_price, trend))
    return pivots


def _recent_pivots(bars: Bars, window: int) -> list[tuple[int, float, int]]:
    pivots = zigzag(bars.high, bars.low, swing_threshold(bars))
    return [item for item in pivots if item[0] >= len(bars) - window]


# ---------------------------------------------------------------- 几何形态


def _cup_handle(bars: Bars) -> Signal | None:
    """杯柄：左杯沿 → 回调 15~35% → 右杯沿回到前高附近 → 柄部小幅缩量回踩 → 突破柄部上沿。

    不用摆动点，直接按「杯底把窗口切成左右两半」来切 —— 杯子的形状是平滑的圆弧，
    用摆动点反而会被杯内的小波动切碎。
    """
    if len(bars) < CUP_WINDOW:
        return None

    highs = bars.high[-CUP_WINDOW:]
    lows = bars.low[-CUP_WINDOW:]
    size = int(highs.size)

    left_end = int(size * CUP_LEFT_END)
    right_start = int(size * CUP_RIGHT_START)
    left_rel = int(np.argmax(highs[:left_end]))
    left_rim = float(highs[left_rel])

    if right_start <= left_rel + 2:
        return None
    trough_rel = int(np.argmin(lows[left_rel + 1 : right_start])) + left_rel + 1
    trough = float(lows[trough_rel])

    depth = (left_rim - trough) / left_rim
    if not (CUP_DEPTH[0] <= depth <= CUP_DEPTH[1]):
        return None

    if trough_rel + 2 >= size - 1:
        return None
    right_rel = int(np.argmax(highs[trough_rel + 1 :])) + trough_rel + 1
    right_rim = float(highs[right_rel])
    cup_height = left_rim - trough
    if cup_height <= 0:
        return None
    recover = (right_rim - trough) / cup_height
    if recover < CUP_RECOVER:
        return None

    # 柄部：右杯沿之后、**不含今天**的那些 K 线（今天是突破日）
    handle = slice(right_rel + 1, size - 1)
    handle_days = handle.stop - handle.start
    if handle_days < CUP_HANDLE_MIN_DAYS or handle_days > CUP_HANDLE_MAX_DAYS:
        return None

    handle_top = float(highs[handle].max())
    # 用**收盘**的最低点衡量柄部深度，不用最低价：单根下影线扎一下就把整个
    # 结构判死没有意义，柄部的「回撤」应该是实打实跌下去多少
    handle_low = float(bars.close[-CUP_WINDOW:][handle].min())
    handle_ratio = (right_rim - handle_low) / cup_height
    if handle_ratio > CUP_HANDLE_DEPTH_RATIO:
        return None
    if bars.close[-1] <= handle_top:
        return None

    rise = slice(trough_rel, right_rel + 1)
    rise_volume = _safe_mean(bars.volume[-CUP_WINDOW:][rise])
    handle_volume = _safe_mean(bars.volume[-CUP_WINDOW:][handle])
    ratio = handle_volume / rise_volume if rise_volume > 0 else 1.0
    if ratio > CUP_HANDLE_VOL_RATIO:
        return None

    excess = float(bars.close[-1] / handle_top - 1)
    mid_depth = sum(CUP_DEPTH) / 2
    half = (CUP_DEPTH[1] - CUP_DEPTH[0]) / 2
    # 杯深取区间中段最好（太浅是回调不是杯，太深是反转不是整理）—— 唯一双向的一项
    score = _band_score(half - abs(depth - mid_depth), 0.01, half * 0.8, half * 1.2) * 30
    score += _gate_score(recover, CUP_RECOVER, 1.0) * 20
    score += _gate_score(handle_ratio, CUP_HANDLE_DEPTH_RATIO, CUP_HANDLE_IDEAL_RATIO) * 25
    score += _gate_score(ratio, CUP_HANDLE_VOL_RATIO, CUP_HANDLE_IDEAL_VOL_RATIO) * 15
    score += _band_score(excess, 0.005, 0.03, 0.08) * 10

    return Signal(
        "cup_handle",
        score,
        {"breakout": handle_top, "support": handle_low, "rim": left_rim},
        {
            "cup_depth": round(depth, 4),
            "recover": round(recover, 3),
            "handle_days": handle_days,
            "handle_ratio": round(handle_ratio, 3),
            "handle_vol_ratio": round(ratio, 3),
            "excess": round(excess, 4),
        },
    )


def _double_bottom(bars: Bars) -> Signal | None:
    """W 底：两个相近的低点，中间反弹出颈线，今天收在颈线上方。"""
    if len(bars) < DOUBLE_BOTTOM_MIN_BARS:
        return None

    recent = _recent_pivots(bars, DOUBLE_BOTTOM_WINDOW)
    lows = [item for item in recent if item[2] < 0]
    if len(lows) < 2:
        return None

    first, second = lows[-2], lows[-1]
    gap = second[0] - first[0]
    if not (DOUBLE_BOTTOM_GAP[0] <= gap <= DOUBLE_BOTTOM_GAP[1]):
        return None

    shallow = max(first[1], second[1])
    tolerance = abs(first[1] - second[1]) / shallow
    if tolerance > DOUBLE_BOTTOM_TOLERANCE:
        return None

    between = [item for item in recent if first[0] < item[0] < second[0] and item[2] > 0]
    if not between:
        return None
    neck = max(between, key=lambda item: item[1])
    neckline = neck[1]
    rebound = (neckline - shallow) / shallow
    if rebound < DOUBLE_BOTTOM_REBOUND:
        return None

    if bars.close[-1] <= neckline:
        return None
    excess = float(bars.close[-1] / neckline - 1)
    if excess > DOUBLE_BOTTOM_MAX_EXCESS:
        return None

    score = _gate_score(tolerance, DOUBLE_BOTTOM_TOLERANCE, 0.0) * 35
    score += _band_score(rebound, DOUBLE_BOTTOM_REBOUND, 0.30, 0.50) * 30
    score += _band_score(excess, 0.003, 0.025, 0.06) * 35

    return Signal(
        "double_bottom",
        score,
        {"breakout": neckline, "support": shallow},
        {
            # 刻意叫 bottom_gap 而不是 gap：`gap` 在均线多头排列里是「距 MA20 的比率」，
            # 同名不同义会让前端把它当比率乘 100，显示成 2700%（27 天 × 100）
            "bottom_gap": gap,
            "tolerance": round(tolerance, 4),
            "rebound": round(rebound, 4),
            "excess": round(excess, 4),
        },
    )


def _triangle(bars: Bars) -> Signal | None:
    """三角收敛：高点递降 + 低点递升，末端振幅收敛到起点的一半以内。

    这类形态本身不是买点，而是**待突破**的观察名单 —— 所以不打「突破」的分，
    改打「收敛得有多成熟」的分，越接近顶点越高。
    """
    if len(bars) < TRIANGLE_WINDOW:
        return None

    recent = _recent_pivots(bars, TRIANGLE_WINDOW)
    highs = [item for item in recent if item[2] > 0]
    lows = [item for item in recent if item[2] < 0]
    if len(highs) < TRIANGLE_MIN_PIVOTS or len(lows) < TRIANGLE_MIN_PIVOTS:
        return None

    last_highs = [item[1] for item in highs[-TRIANGLE_MIN_PIVOTS:]]
    last_lows = [item[1] for item in lows[-TRIANGLE_MIN_PIVOTS:]]
    if not (last_highs[0] > last_highs[1] > last_highs[2]):
        return None
    if not (last_lows[0] < last_lows[1] < last_lows[2]):
        return None

    early = highs[0][1] - lows[0][1]
    late = last_highs[-1] - last_lows[-1]
    if early <= 0 or late <= 0:
        return None
    ratio = late / early
    if ratio > TRIANGLE_CONVERGE:
        return None

    upper, lower = last_highs[-1], last_lows[-1]
    # 当前价必须还在上下边界之间；已经突破出去的不该留在「收敛中」的名单里
    if not (lower <= bars.close[-1] <= upper * 1.02):
        return None
    position = (bars.close[-1] - lower) / (upper - lower)

    score = _band_score(TRIANGLE_CONVERGE - ratio, 0.05, 0.35, 0.50) * 45
    # 贴着上边界说明快要选方向了，比趴在中间更值得盯
    score += _band_score(position, 0.4, 0.95, 1.05) * 35
    score += _band_score(len(highs) + len(lows), 6, 12, 18) * 20

    return Signal(
        "triangle",
        score,
        {"breakout": upper, "support": lower},
        {
            "converge": round(ratio, 3),
            "position": round(position, 3),
            "pivots": len(highs) + len(lows),
        },
    )


def _head_shoulders(bars: Bars) -> Signal | None:
    """头肩底：左肩 → 头（更低的低点）→ 右肩，两肩等高、颈线大致水平，今天收在颈线上方。

    与 W 底的区别在**结构**而不是阈值：W 底是两个底加一次反弹，头肩底是三个底、
    中间那个最深。所以这里必须点名「三个低点」，不能拿 W 底的条件拼出来 ——
    头肩底的两个肩在 W 底眼里正好是「两个相近的低点」。

    同一段走势同时命中两个形态是允许的（本来就重叠），但观察点不同：
    W 底盯的是两个底的连线，头肩底盯的是颈线。**别为了去重而把结构判松**，
    那会连「像不像」都一起丢掉。
    """
    if len(bars) < HS_MIN_BARS:
        return None

    recent = _recent_pivots(bars, HS_WINDOW)
    lows = [item for item in recent if item[2] < 0]
    if len(lows) < 3:
        return None

    left, head, right = lows[-3], lows[-2], lows[-1]
    # 头必须是三个低点里最低的那个，否则就不是头肩
    if not (head[1] < left[1] and head[1] < right[1]):
        return None

    shoulder_diff = abs(left[1] - right[1]) / min(left[1], right[1])
    if shoulder_diff > HS_SHOULDER_TOLERANCE:
        return None

    # 两个颈线高点：左肩~头之间一个、头~右肩之间一个
    neck_left = [item for item in recent if left[0] < item[0] < head[0] and item[2] > 0]
    neck_right = [item for item in recent if head[0] < item[0] < right[0] and item[2] > 0]
    if not neck_left or not neck_right:
        return None
    neck_a = max(neck_left, key=lambda item: item[1])
    neck_b = max(neck_right, key=lambda item: item[1])
    # 突破线取两者中**较低**的那个：保守一些，宁可要求多涨一点才算过关
    neckline = min(neck_a[1], neck_b[1])

    neck_diff = abs(neck_a[1] - neck_b[1]) / max(neck_a[1], neck_b[1])
    if neck_diff > HS_NECK_TOLERANCE:
        return None

    depth = (neckline - head[1]) / neckline
    if depth < HS_MIN_DEPTH:
        return None

    tail = len(bars) - 1 - right[0]
    if tail > HS_MAX_TAIL:
        return None

    if bars.close[-1] <= neckline:
        return None
    excess = float(bars.close[-1] / neckline - 1)
    if excess > HS_MAX_EXCESS:
        return None

    score = _gate_score(shoulder_diff, HS_SHOULDER_TOLERANCE, 0.0) * 25
    score += _gate_score(neck_diff, HS_NECK_TOLERANCE, 0.0) * 20
    score += _band_score(depth, HS_MIN_DEPTH, 0.20, 0.35) * 25
    score += _band_score(excess, 0.003, 0.025, 0.06) * 30

    return Signal(
        "head_shoulders",
        score,
        {"breakout": neckline, "support": max(left[1], right[1])},
        {
            "shoulder_diff": round(shoulder_diff, 4),
            "neck_diff": round(neck_diff, 4),
            "head_depth": round(depth, 4),
            "excess": round(excess, 4),
            "tail": tail,
        },
    )


def _flag(bars: Bars) -> Signal | None:
    """旗形：一段急涨（旗杆）之后窄幅缩量整理（旗面），今天贴着旗面上沿。

    和三角收敛一样属于**待突破**的观察位，所以不打「已经突破」的分，
    打的是「旗杆够不够强、整理得够不够紧」。

    旗面不用摆动点切，而是**从最高点之后算起**：整理期的波动本来就小，
    摆动点在这么窄的区间里会被噪声切出一堆假极值。这也顺便给了「旗面多长」
    一个自然的定义 —— 急涨顶到哪儿，旗面就从哪儿开始。
    """
    total = FLAG_POLE_DAYS + FLAG_MAX_DAYS
    if len(bars) < total + 10:
        return None

    highs = bars.high[-total:]
    lows = bars.low[-total:]

    peak = int(np.argmax(highs))
    flag_days = int(highs.size) - 1 - peak
    if not (FLAG_MIN_DAYS <= flag_days <= FLAG_MAX_DAYS):
        return None
    # 旗杆得留得下：顶之前至少还有 FLAG_POLE_DAYS 根可看
    if peak < FLAG_POLE_DAYS:
        return None

    pole_start = peak - FLAG_POLE_DAYS
    pole_low = float(lows[pole_start : peak + 1].min())
    if pole_low <= 0:
        return None
    pole_gain = float(highs[peak]) / pole_low - 1
    if pole_gain < FLAG_MIN_POLE:
        return None

    flag_high = float(highs[peak + 1 :].max())
    flag_low = float(lows[peak + 1 :].min())
    if flag_high <= flag_low:
        return None
    flag_range = (flag_high - flag_low) / flag_high
    if flag_range > FLAG_MAX_RANGE:
        return None

    retrace = (float(highs[peak]) - flag_low) / (float(highs[peak]) - pole_low)
    if retrace > FLAG_MAX_RETRACE:
        return None

    pole_volume = _safe_mean(bars.volume[-total:][pole_start : peak + 1])
    flag_volume = _safe_mean(bars.volume[-total:][peak + 1 :])
    vol_ratio = flag_volume / pole_volume if pole_volume > 0 else 1.0
    if vol_ratio > FLAG_MAX_VOL_RATIO:
        return None

    position = (float(bars.close[-1]) - flag_low) / (flag_high - flag_low)
    if position < FLAG_MIN_POSITION:
        return None
    excess = float(bars.close[-1] / flag_high - 1)
    if excess > FLAG_MAX_EXCESS:
        return None

    score = _band_score(pole_gain, FLAG_MIN_POLE, 0.35, 0.60) * 30
    score += _gate_score(flag_range, FLAG_MAX_RANGE, 0.04) * 20
    score += _gate_score(retrace, FLAG_MAX_RETRACE, 0.15) * 20
    score += _gate_score(vol_ratio, FLAG_MAX_VOL_RATIO, 0.50) * 20
    score += _band_score(position, FLAG_MIN_POSITION, 0.85, 1.05) * 10

    return Signal(
        "flag",
        score,
        {"breakout": flag_high, "support": flag_low},
        {
            "pole_gain": round(pole_gain, 4),
            "flag_days": flag_days,
            "flag_range": round(flag_range, 4),
            "retrace": round(retrace, 4),
            "vol_drop": round(vol_ratio, 4),
            "position": round(position, 4),
            "excess": round(excess, 4),
        },
    )


def _three_stage(bars: Bars) -> Signal | None:
    """三段式突破：缓涨 → 急涨 → 缩量整理 → 今天突破整理段上沿。

    这是**唯一一个不是从交易书里抄来的形态** —— 它来自用户举的一只真实票
    （强达电路 301628，2026-08 那段），阈值则是在全市场回测里选出来的
    （过程与局限见 `THREE_SLOW_DAYS` 上面那段注释，别跳过）。

    与「旗形」的关键区别：旗形只看急涨之后的整理，这里还要求**急涨之前有一段
    缓涨** —— 资金是逐步进场的，不是一根线拉起来的。代价是它严得多，
    A 股全市场平均每周才出一个。

    整理段与急涨段的长度都未知，两个都枚举一遍，取**评分最高**的组合：
    判定用的是「是不是」，但页面上要排序，所以必须给分而不是给是非。
    """
    close, high, low, volume, open_ = (
        bars.close,
        bars.high,
        bars.low,
        bars.volume,
        bars.open,
    )
    size = len(bars)
    best: Signal | None = None

    for flat_days in range(THREE_FLAT_DAYS[0], THREE_FLAT_DAYS[1] + 1):
        # 整理段是「不含今天」的那几天 —— 今天是突破日
        flat_start = size - 1 - flat_days
        if flat_start < 30:
            continue
        flat_high = float(high[flat_start : size - 1].max())
        flat_low = float(low[flat_start : size - 1].min())
        flat_mean = float(close[flat_start : size - 1].mean())
        if flat_mean <= 0:
            continue
        flat_range = (flat_high - flat_low) / flat_mean
        flat_vol = float(volume[flat_start : size - 1].mean())
        if flat_vol <= 0:
            continue

        for surge_days in range(THREE_SURGE_DAYS[0], THREE_SURGE_DAYS[1] + 1):
            surge_end = flat_start - 1
            surge_start = surge_end - surge_days + 1
            if surge_start < THREE_SLOW_DAYS + 5:
                continue
            base = float(close[surge_start - 1])
            if base <= 0:
                continue
            surge_gain = float(close[surge_end]) / base - 1
            if surge_gain < THREE_SURGE_GAIN:
                continue
            prev_vol = float(volume[surge_start - 5 : surge_start].mean())
            if prev_vol <= 0:
                continue
            surge_ratio = float(volume[surge_start : surge_end + 1].mean()) / prev_vol
            if surge_ratio < THREE_SURGE_VOL:
                continue
            if flat_range > THREE_FLAT_RANGE:
                continue

            # 整理段要守住急涨的成果
            span_high = float(high[surge_start - 1 : surge_end + 1].max())
            span_low = float(low[surge_start - 1 : surge_end + 1].min())
            span = span_high - span_low
            if span <= 0:
                continue
            if (span_high - flat_low) / span > THREE_FLAT_DROP:
                continue
            shrink = flat_vol / (float(volume[surge_start : surge_end + 1].mean()))
            if shrink > THREE_FLAT_VOL:
                continue

            # 缓涨段：急涨之前的这些天，涨得不多但阳线多
            slow_start = max(0, surge_start - THREE_SLOW_DAYS)
            slow_close = close[slow_start:surge_start]
            if slow_close.size < 8:
                continue
            slow_base = float(slow_close[0])
            if slow_base <= 0:
                continue
            slow_gain = float(slow_close[-1]) / slow_base - 1
            if not (THREE_SLOW_GAIN[0] <= slow_gain <= THREE_SLOW_GAIN[1]):
                continue
            bull = float(np.mean(slow_close > open_[slow_start:surge_start]))
            if bull < THREE_SLOW_BULL:
                continue

            # 今天必须真的突破了整理段上沿
            if float(close[-1]) <= flat_high:
                continue
            break_ratio = float(volume[-1]) / flat_vol
            if break_ratio < THREE_BREAK_VOL:
                continue
            excess = float(close[-1] / flat_high - 1)

            score = _band_score(surge_gain, THREE_SURGE_GAIN, 0.25, 0.45) * 20
            score += _band_score(surge_ratio, THREE_SURGE_VOL, 3.0, 5.0) * 15
            score += _gate_score(flat_range, THREE_FLAT_RANGE, 0.05) * 20
            score += _gate_score(shrink, THREE_FLAT_VOL, 0.35) * 20
            score += _gate_score(break_ratio, THREE_BREAK_VOL, 2.5) * 10
            score += _gate_score(bull, THREE_SLOW_BULL, 0.75) * 15

            if best is None or score > best.score:
                best = Signal(
                    "three_stage",
                    score,
                    {"breakout": flat_high, "support": flat_low},
                    {
                        "slow_gain": round(slow_gain, 4),
                        "slow_bull": round(bull, 3),
                        "surge_gain": round(surge_gain, 4),
                        "surge_days": surge_days,
                        "surge_vol": round(surge_ratio, 3),
                        "flat_days": flat_days,
                        "flat_range": round(flat_range, 4),
                        "shrink": round(shrink, 3),
                        "break_vol": round(break_ratio, 3),
                        "excess": round(excess, 4),
                    },
                )
    return best


def _limit_surge_flat(bars: Bars) -> Signal | None:
    """涨停爆量横盘：涨停爆量启动 → 突破前期平台 → 缩量横盘，今天仍在横盘段里。

    与其它形态最大的不同：它找的是**已经走完一段、正在歇脚**的票，买点在
    「横盘上沿被再次放量突破」那一刻 —— 所以它**不要求今天突破**，反而要求
    今天仍在区间内。这也意味着同一只票在横盘期里会**连续多天**命中，这是对的。

    「涨停日」在最近 `LS_LOOKBACK` 天里枚举（一只票可能连着好几个涨停），
    横盘长度也枚举，取评分最高的组合 —— 判定是「是不是」，但页面上要排序。
    """
    size = len(bars)
    # 涨停日之后至少要留出「上冲 1 天 + 横盘 LS_FLAT_DAYS[0] 天」
    last_t0 = size - 1 - LS_FLAT_DAYS[0]
    first_t0 = max(LS_PLATFORM_DAYS + 5, size - 1 - LS_LOOKBACK)
    if last_t0 < first_t0:
        return None

    close, high, low, volume, pct = (
        bars.close,
        bars.high,
        bars.low,
        bars.volume,
        bars.pct_chg,
    )
    best: Signal | None = None

    for t0 in range(first_t0, last_t0 + 1):
        if float(pct[t0]) < LS_LIMIT_PCT:
            continue
        base_vol = _safe_mean(volume[t0 - 5 : t0])
        if base_vol <= 0:
            continue
        limit_vol = float(volume[t0]) / base_vol
        if limit_vol < LS_SURGE_VOL:
            continue

        # 前期平台（不含涨停日，否则自己就是自己的平台）
        platform = float(high[t0 - LS_PLATFORM_DAYS : t0].max())
        prev_close = float(close[t0 - 1])
        if platform <= 0 or prev_close <= 0:
            continue

        for flat_days in range(LS_FLAT_DAYS[0], LS_FLAT_DAYS[1] + 1):
            flat_start = size - flat_days
            if flat_start <= t0:
                continue
            run_end = flat_start - 1

            # ---- 上冲段：涨停日起到横盘开始之前 ----
            seg_high = float(high[t0 : run_end + 1].max())
            run_gain = seg_high / prev_close - 1
            if run_gain < LS_MIN_RUN:
                continue
            # 突破必须**在涨停后 LS_BREAK_DAYS 天内**发生，且是收盘站上平台。
            # 只问「这段里有没有收盘在平台之上」不够 —— 涨了一大段之后的收盘
            # 当然也在上面，那就等于没检查突破发生的时间
            break_to = min(t0 + LS_BREAK_DAYS, run_end)
            if float(close[t0 : break_to + 1].max()) <= platform:
                continue

            # ---- 横盘段 ----
            flat_high = float(high[flat_start:].max())
            flat_low = float(low[flat_start:].min())
            flat_mean = float(close[flat_start:].mean())
            if flat_mean <= 0 or flat_high <= flat_low:
                continue
            flat_range = (flat_high - flat_low) / flat_mean
            if flat_range > LS_FLAT_RANGE:
                continue
            # 横盘低点守住多少成果。这一条比「振幅够窄」更要紧：振幅窄但一路
            # 阴跌回平台上的「横盘」，是出货不是整理
            keep = (flat_low - platform) / (seg_high - platform)
            if keep < LS_FLAT_KEEP:
                continue

            run_vol = _safe_mean(volume[t0 : run_end + 1])
            flat_vol = _safe_mean(volume[flat_start:])
            if run_vol <= 0:
                continue
            shrink = flat_vol / run_vol
            if shrink > LS_FLAT_VOL:
                continue

            score = _band_score(limit_vol, LS_SURGE_VOL, 5.0, 12.0) * 20
            score += _band_score(run_gain, LS_MIN_RUN, 0.30, 0.60) * 20
            score += _gate_score(flat_range, LS_FLAT_RANGE, 0.04) * 20
            score += _gate_score(shrink, LS_FLAT_VOL, 0.40) * 20
            score += _gate_score(keep, LS_FLAT_KEEP, 0.85) * 10
            score += _band_score(float(flat_days), LS_FLAT_DAYS[0], 6, LS_FLAT_DAYS[1] + 1) * 10

            if best is None or score > best.score:
                best = Signal(
                    "limit_surge_flat",
                    score,
                    {"breakout": flat_high, "support": flat_low},
                    {
                        "limit_vol": round(limit_vol, 3),
                        "surge_gain": round(run_gain, 4),
                        "flat_days": flat_days,
                        "flat_range": round(flat_range, 4),
                        "shrink": round(shrink, 3),
                        "keep": round(keep, 4),
                        "excess": round(float(close[-1]) / platform - 1, 4),
                    },
                )
    return best


def _breakout_flat(bars: Bars) -> Signal | None:
    """突破后横盘：放量突破 60 日新高 → 缩量横着、不跌回突破位 → 今天仍在区间里。

    与其它形态最不一样的一点：判定日落在**横盘期内**，给的是**观察位**而不是买点 ——
    真正的买点是「它再次放量突破横盘上沿」，那是之后的事。所以它**不要求今天突破**，
    反而要求今天还在区间内。

    突破日在最近 `BF_MAX_GAP` 天里枚举（一只票可能不止一根），取评分最高的那根。
    """
    size = len(bars)
    if size < BF_PRIOR_HIGH + BF_MIN_FLAT + 5:
        return None

    close, high, low, volume = bars.close, bars.high, bars.low, bars.volume
    today = float(close[-1])
    best: Signal | None = None

    # gap = 今天距突破日的天数。横盘段是「突破日之后、今天之前」
    for gap in range(BF_MIN_FLAT + 1, BF_MAX_GAP + 1):
        b_idx = size - 1 - gap
        if b_idx < BF_PRIOR_HIGH:
            continue
        prev_close = float(close[b_idx - 1])
        if prev_close <= 0:
            continue
        b_close = float(close[b_idx])
        gain = b_close / prev_close - 1
        if gain < BF_MIN_GAIN:
            continue
        prev_vol = _safe_mean(volume[b_idx - 5 : b_idx])
        if prev_vol <= 0:
            continue
        b_ratio = float(volume[b_idx]) / prev_vol
        if b_ratio < BF_MIN_VOL:
            continue
        # 突破要创 N 日新高，否则只是「涨了一天」
        if b_close <= float(close[b_idx - BF_PRIOR_HIGH : b_idx].max()):
            continue

        flat = slice(b_idx + 1, size - 1)
        flat_days = flat.stop - flat.start
        if flat_days < BF_MIN_FLAT:
            continue
        flat_high = float(high[flat].max())
        flat_low = float(low[flat].min())
        flat_mean = float(close[flat].mean())
        if flat_mean <= 0 or flat_high <= flat_low:
            continue
        flat_range = (flat_high - flat_low) / flat_mean
        if flat_range > BF_MAX_RANGE:
            continue
        # 不跌回突破位 —— 这条是形态的关键：涨上去又跌回来就不算强势
        giveback = (b_close - flat_low) / b_close
        if giveback > BF_MAX_GIVEBACK:
            continue
        flat_vol = _safe_mean(volume[flat])
        if flat_vol <= 0:
            continue
        shrink = flat_vol / float(volume[b_idx])
        if shrink > BF_MAX_SHRINK:
            continue
        # 今天还得在区间里：既没跌破下沿，也没有已经冲出去
        if today > flat_high * (1 + BF_EDGE_TOLERANCE):
            continue
        if today < flat_low * (1 - BF_EDGE_TOLERANCE):
            continue

        position = (today - flat_low) / (flat_high - flat_low)

        score = _band_score(gain, BF_MIN_GAIN, 0.15, 0.25) * 20
        score += _band_score(b_ratio, BF_MIN_VOL, 5.0, 10.0) * 20
        score += _gate_score(flat_range, BF_MAX_RANGE, 0.05) * 20
        score += _gate_score(giveback, BF_MAX_GIVEBACK, 0.0) * 10
        score += _gate_score(shrink, BF_MAX_SHRINK, 0.35) * 10
        score += _band_score(float(flat_days), BF_MIN_FLAT, 6, BF_MAX_GAP + 1) * 10
        score += _band_score(position, 0.4, 0.85, 1.05) * 10

        if best is None or score > best.score:
            best = Signal(
                "breakout_flat",
                score,
                {"breakout": flat_high, "support": flat_low},
                {
                    "break_gain": round(gain, 4),
                    "vol_ratio": round(b_ratio, 2),
                    "flat_days": flat_days,
                    "flat_range": round(flat_range, 4),
                    "giveback": round(giveback, 4),
                    "shrink": round(shrink, 3),
                    "position": round(position, 4),
                },
            )
    return best


def _n_shape(bars: Bars) -> Signal | None:
    """N 字选股：放量大涨 → 缩量回调到起涨点附近。

    起涨点 = 大涨那天的起点（开盘价与前一日收盘取低的，含跳空）。

    判定时点落在**回调期内**，所以同一只票会连着几天命中 —— 这是对的：
    它连着几天都处在「已经回踩到起点」的状态里，不是重复计数。
    """
    size = len(bars)
    if size < 30:
        return None

    close, high, low, open_ = bars.close, bars.high, bars.low, bars.open
    volume, pct = bars.volume, bars.pct_chg
    best: Signal | None = None

    for pull_days in range(NS_PULL_DAYS[0], NS_PULL_DAYS[1] + 1):
        t0 = size - 1 - pull_days  # 放量大涨日
        if t0 < 10:
            continue
        if float(pct[t0]) < NS_SURGE_PCT:
            continue
        base_vol = _safe_mean(volume[t0 - 5 : t0])
        if base_vol <= 0:
            continue
        ratio = float(volume[t0]) / base_vol
        if ratio < NS_SURGE_VOL:
            continue

        start_point = min(float(open_[t0]), float(close[t0 - 1]))
        if start_point <= 0:
            continue
        pull_low = float(low[t0 + 1 :].min())
        if not (start_point * NS_LOW_BAND[0] <= pull_low <= start_point * NS_LOW_BAND[1]):
            continue
        today = float(close[-1])
        if not (start_point * NS_NOW_BAND[0] <= today <= start_point * NS_NOW_BAND[1]):
            continue
        pull_vol = _safe_mean(volume[t0 + 1 :])
        if pull_vol <= 0:
            continue
        shrink = pull_vol / float(volume[t0])
        if shrink > NS_MAX_SHRINK:
            continue

        gap = today / start_point - 1
        # 「踩得准」占最大权重：离起涨点越近，越像教科书里的那一折
        score = (1 - min(abs(gap), 0.12) / 0.12) * 40
        score += _band_score(ratio, NS_SURGE_VOL, 5.0, 10.0) * 25
        score += _gate_score(shrink, NS_MAX_SHRINK, 0.40) * 20
        score += _band_score(float(pull_days), float(NS_PULL_DAYS[0]), 3.0, 6.0) * 15

        if best is None or score > best.score:
            best = Signal(
                "n_shape",
                score,
                {"breakout": start_point, "support": pull_low},
                {
                    "surge_gain": round(float(pct[t0]) / 100, 4),
                    "vol_ratio": round(ratio, 2),
                    "pullback_days": pull_days,
                    "start_gap": round(gap, 4),
                    "shrink": round(shrink, 3),
                },
            )
    return best


def _burst_shrink_pullback(bars: Bars) -> Signal | None:
    """爆量后缩量回踩：某天成交额放量到前一日 2 倍以上（爆量日），之后某天缩量到
    爆量日的一半以下（缩量日）—— **要选的是缩量日**，不是爆量日。

    判定时点落在缩量日，且是「爆量日之后**第一个**跌破一半的日子」：一只票一次爆量
    只出一个信号，不会在缩量期里连续好几天刷屏（缩量一旦到位往往会在低位趴几天，
    逐日命中会把榜单塞满同一只票）。

    爆量日只看成交额（≥ 前一日 2 倍）+ 必须阳线（收 > 开）；缩量日只看成交额。
    """
    size = len(bars)
    if size < BURST_SHRINK_LOOKBACK + 2:
        return None

    amount = bars.amount
    today = size - 1
    today_amt = float(amount[today])
    if today_amt <= 0:
        return None

    # 从最近往前找爆量日：最近的那一个才是当前这轮缩量的参照 —— 更早的爆量日，
    # 它的缩量早在新爆量日之前就走完了。找到最近的爆量日后，只看它之后
    # **第一个**跌破一半的日子是不是今天；不是就说明这轮信号已经过去/还没到。
    for e in range(today - 1, today - 1 - BURST_SHRINK_LOOKBACK, -1):
        if e < 1:
            break
        prev = float(amount[e - 1])
        cur = float(amount[e])
        if prev <= 0 or cur <= 0 or cur < BURST_SHRINK_MULT * prev:
            continue
        # 爆量日必须是阳线：放量下跌的「爆量」是出货，不是「爆量后缩量回踩」。
        # open/close 是前复权价，但同一根 K 线内部同乘一个 ratio，收>开的关系不变
        if float(bars.close[e]) <= float(bars.open[e]):
            continue

        threshold = cur * BURST_SHRINK_RATIO
        for t in range(e + 1, today + 1):
            a = float(amount[t])
            if a <= 0:
                continue  # 缺成交额的行不算「缩量日」
            if a <= threshold:
                if t != today:
                    return None  # 第一个缩量日已经过去，这轮信号不是今天
                break
        else:
            return None  # 到今天都还没跌破一半，缩量没到位

        mult = cur / prev
        shrink = today_amt / cur
        gap = today - e
        # 分数结构：过了两道硬门槛（爆量 ≥2×、缩量 ≤0.5×）就有保底分 —— 这是「是不是」，
        # 函数前段已经把这些前提 return 掉了。叠加上去的才是「像不像」，只在命中之间排序。
        # 爆量倍数只往一个方向打分（越猛越好）：它没有「太大就假」的上限，别用 _band_score。
        score = 55.0
        score += _gate_score(mult, BURST_SHRINK_MULT, BURST_SHRINK_MULT_IDEAL) * 20
        score += _gate_score(shrink, BURST_SHRINK_RATIO, BURST_SHRINK_IDEAL_RATIO) * 20
        score += _band_score(float(gap), *BURST_SHRINK_GAP_IDEAL, BURST_SHRINK_GAP_CAP) * 5

        return Signal(
            "burst_shrink_pullback",
            score,
            {},
            {
                "burst_mult": round(mult, 2),
                "shrink": round(shrink, 4),
                "burst_gap": gap,
            },
        )
    return None


def _oneil_breakout(bars: Bars) -> Signal | None:
    """欧奈尔突破：基底整理 → 放量站上平台上沿 → 且这个平台贴着一年新高。

    四条技术面约束对应 O'Neil 的说法：贴新高（N）、基底整理、放量确认（S）、
    RS 评级（L）。**RS 用的不是绝对涨幅，而是全市场横截面百分位**（见 `compute_rs`）。

    只在**突破当天**出信号（他说的「枢轴点买入」），所以频率低、时效短 ——
    今天没跟上，明天再买就是追高了（`ON_MAX_EXCESS` 会把追高的挡掉）。

    与已有的「平台突破」的区别：那个只看「30 日窄幅 + 今天放量收上去」，
    不管这个平台在什么位置；这里多两道 O'Neil 的硬约束 ——
    **平台上沿必须贴近一年最高价**（否则只是反弹到半山腰）、
    以及**价格必须站在 50/150 日均线上方**（中期趋势向上）。
    """
    size = len(bars)
    if size < 160:  # 150 日均线 + 一点余量
        return None

    close, high, low, volume = bars.close, bars.high, bars.low, bars.volume
    today_close = float(close[-1])
    if today_close <= 0:
        return None

    ma50 = _safe_mean(close[-50:])
    ma150 = _safe_mean(close[-150:])
    if today_close < ma50 or today_close < ma150:
        return None
    gain120 = today_close / float(close[-121]) - 1 if size > 121 else 0.0
    if gain120 < ON_MIN_GAIN:
        return None
    # 日均振幅过滤：把低波动的红利股挡在清单外（局限见 ON_MIN_AMPLITUDE 的注释）
    recent_close = close[-60:]
    if np.any(recent_close <= 0):
        return None
    if _safe_mean((high[-60:] - low[-60:]) / recent_close) < ON_MIN_AMPLITUDE:
        return None
    # RS 评级要在门槛之上。默认的 0 表示这笔数据没参与过横截面排名
    # （个股页只取一只、回测切片），一并挡掉 —— 它不该被当成「最弱」放行
    if bars.rs < ON_MIN_RS:
        return None

    year_high = float(high[-250:].max())
    if year_high <= 0:
        return None
    # 突破日量能：O'Neil 看的是「比日均量高 40% 以上」，所以基准取 50 日均量
    avg_vol = _safe_mean(volume[-51:-1])
    if avg_vol <= 0:
        return None
    vol_ratio = float(volume[-1]) / avg_vol
    if vol_ratio < ON_BREAK_VOL:
        return None

    best: Signal | None = None
    for base_days in ON_BASE_LENGTHS:
        base_start = size - 1 - base_days
        if base_start < 20:
            continue
        # 基底 = 今天之前的那一段（今天不算，它是突破日）
        base_high = float(high[base_start : size - 1].max())
        base_low = float(low[base_start : size - 1].min())
        if base_high <= 0 or base_low <= 0:
            continue
        drop = (base_high - base_low) / base_high
        if drop > ON_BASE_MAX_DROP:
            continue
        # 平台上沿要贴近一年新高：否则那是「反弹到半山腰」而不是「突破新高」
        from_high = base_high / year_high - 1
        if from_high < -ON_NEAR_HIGH:
            continue
        if today_close <= base_high:
            continue
        excess = today_close / base_high - 1
        if excess > ON_MAX_EXCESS:
            continue

        score = _band_score(vol_ratio, ON_BREAK_VOL, 2.5, 5.0) * 30
        score += _gate_score(-from_high, ON_NEAR_HIGH, 0.0) * 25
        score += _gate_score(drop, ON_BASE_MAX_DROP, 0.08) * 20
        score += _band_score(excess, 0.005, 0.025, 0.05) * 15
        score += _gate_score(float(base_days), float(ON_BASE_LENGTHS[0]), 90.0) * 10

        if best is None or score > best.score:
            best = Signal(
                "oneil_breakout",
                score,
                {"breakout": base_high, "support": base_low},
                {
                    "vol_ratio": round(vol_ratio, 2),
                    "flat_days": base_days,
                    "flat_range": round(drop, 4),
                    "from_high": round(from_high, 4),
                    "excess": round(excess, 4),
                },
            )
    return best


def _wudao_day_geom(bars: Bars, i: int) -> tuple[float, float, float, float]:
    """返回 (vol_ratio, pct, close_pos, upper_shadow)。"""
    base = _safe_mean(bars.volume[i - 20 : i]) if i >= 20 else 0.0
    vol = float(bars.volume[i] / base) if base > 0 else 0.0
    pct = float(bars.pct_chg[i])
    high = float(bars.high[i])
    low = float(bars.low[i])
    open_ = float(bars.open[i])
    close = float(bars.close[i])
    span = max(high - low, 1e-9)
    close_pos = (close - low) / span
    upper = (high - max(open_, close)) / span
    return vol, pct, close_pos, upper


def _wudao_is_diverge_or_dump(bars: Bars, i: int) -> bool:
    """与悟道 classify 一致：吵起来了 / 像出货 压过「明天盯」。"""
    if i < 20:
        return False
    vol, pct, close_pos, upper = _wudao_day_geom(bars, i)
    if vol >= WUDAO_DIVERGE_VOL and (upper >= 0.25 or close_pos <= 0.7):
        return True
    if vol >= 1.4 and pct < 2 and upper >= 0.3:
        return True
    return False


def _wudao_is_sample(bars: Bars, i: int) -> bool:
    """与悟道之路 `is_sample_day` 对齐：冲高约 ≥7%、量比≥1.3、收盘离开最高。"""
    if i < 1 or i >= len(bars):
        return False
    if i < 20:
        return False
    vol, pct, close_pos, upper = _wudao_day_geom(bars, i)
    if vol < WUDAO_SAMPLE_VOL:
        return False

    prev_close = float(bars.close[i - 1])
    if prev_close <= 0:
        return False
    high = float(bars.high[i])
    high_pct = (high / prev_close - 1.0) * 100.0
    if high_pct < WUDAO_SAMPLE_HIGH_PCT:
        return False
    if pct >= 12 or high_pct >= 18:
        return False

    if close_pos >= 0.92 and upper < 0.08 and pct >= 9.5:
        return False
    leave_high = (high - float(bars.close[i])) / max(high, 1e-9)
    if leave_high < 0.028 and upper < 0.15:
        return False
    if close_pos >= 0.88 and leave_high < 0.04:
        return False
    if pct < -3:
        return False
    return True


def _wudao_sample(bars: Bars) -> Signal | None:
    """明天盯：今日是样板日 —— 冲高回落有量，记下今高，今天不买。"""
    if len(bars) < WUDAO_MIN_BARS:
        return None
    i = len(bars) - 1
    if _wudao_is_diverge_or_dump(bars, i):
        return None
    if not _wudao_is_sample(bars, i):
        return None

    prev_close = float(bars.close[i - 1])
    high = float(bars.high[i])
    close = float(bars.close[i])
    high_pct = (high / prev_close - 1.0) * 100.0
    vol = _volume_ratio(bars, 20)
    leave = (high - close) / max(high, 1e-9)

    score = _band_score(high_pct, WUDAO_SAMPLE_HIGH_PCT, 12.0, 18.0) * 40
    score += _band_score(vol, WUDAO_SAMPLE_VOL, 2.5, 6.0) * 35
    score += _band_score(leave, 0.028, 0.08, 0.20) * 25
    score = max(score, 55.0)

    return Signal(
        "wudao_sample",
        score,
        {"watch_high": high},
        {
            "high_pct": round(high_pct, 2),
            "vol_ratio_20": round(vol, 2),
            "leave_high": round(leave, 4),
            "pct_chg": round(float(bars.pct_chg[i]), 2),
        },
    )


def _wudao_start(bars: Bars) -> Signal | None:
    """今天可买：昨是样板，今放量过昨高；买点是刚过昨高。"""
    if len(bars) < WUDAO_MIN_BARS + 1:
        return None
    i = len(bars) - 1
    y = i - 1
    if not _wudao_is_sample(bars, y):
        return None

    sample_high = float(bars.high[y])
    today_high = float(bars.high[i])
    today_close = float(bars.close[i])
    pct = float(bars.pct_chg[i])
    vol = _volume_ratio(bars, 20)

    broke = today_high > sample_high and today_close >= sample_high * 0.98
    if not (broke and vol >= WUDAO_START_VOL and pct > 0):
        return None

    excess = today_close / sample_high - 1.0
    score = _band_score(vol, WUDAO_START_VOL, 3.0, 8.0) * 45
    score += _band_score(pct, 0.5, 8.0, 16.0) * 30
    # 刚过线更好；收盘远离昨高太多偏追高，略降分但仍可进
    if excess <= 0.04:
        score += 25
    else:
        score += _band_score(excess, 0.0, 0.04, 0.12) * 25
    score = max(score, 60.0)

    return Signal(
        "wudao_start",
        score,
        {"breakout": sample_high, "watch_high": sample_high},
        {
            "vol_ratio_20": round(vol, 2),
            "pct_chg": round(pct, 2),
            "excess": round(excess, 4),
            "sample_high": round(sample_high, 2),
        },
    )


# ------------------------------------------------- 新增形态实现（2026-09-25）


def _linfit(values: np.ndarray) -> tuple[float, float]:
    """最小二乘直线拟合，返回 `(斜率, 截距)`，x 取 0..n-1。

    趋势线一律用拟合，而不是「窗口两端的极值连线」：后者对单根毛刺极度敏感 ——
    一根长上影就能把整条轨拉歪；最小二乘会把毛刺摊到整条线上。
    """
    if values.size < 2:
        return 0.0, 0.0
    slope, intercept = np.polyfit(np.arange(values.size, dtype=float), values, 1)
    return float(slope), float(intercept)


def _weekly_closes(bars: Bars) -> np.ndarray:
    """日线按自然周聚合成周线收盘序列（升序）。

    最后一周可能不足 5 个交易日，照样算一根 —— 与真实周线一致（未收盘的周线也是 K 线）。
    """
    out: list[float] = []
    current: tuple[int, int] | None = None
    for index, day in enumerate(bars.dates):
        iso = day.isocalendar()
        key = (iso[0], iso[1])
        if key != current:
            out.append(float(bars.close[index]))
            current = key
        else:
            out[-1] = float(bars.close[index])
    return np.array(out, dtype=float)


def _touches(values: np.ndarray, level: float, tol: float) -> int:
    """序列里「碰到 level」的次数（相对容差）。

    用来确认箱体上下沿是真被反复验证过，而不是取最大/最小时偶然扫到的一根毛刺。
    """
    if level <= 0:
        return 0
    return int(np.sum(np.abs(values / level - 1.0) <= tol))


def _prior_drop(bars: Bars, days: int = CANDLE_PRIOR_DAYS) -> float:
    """前置跌幅：`days` 个交易日前的收盘到**昨收**的跌幅（正数表示下跌）。

    反转类蜡烛（锤子、吞没、启明星…）必须在跌势末端出现才有意义 ——
    同样的形状出现在上涨半途，多半是中继或者干脆是噪声。
    """
    if len(bars) < days + 2:
        return 0.0
    base = float(bars.close[-1 - days])
    if base <= 0:
        return 0.0
    return float(base / bars.close[-2] - 1)


def _candle(
    bars: Bars, index: int = -1
) -> tuple[float, float, float, float, float, float, float, float]:
    """一根 K 线的 `(开, 高, 低, 收, 实体, 上影, 下影, 振幅)`。

    振幅可能是 0（一字板），调用方必须自己判 —— 除零会让整只票的扫描抛异常。
    """
    open_, high, low, close = (
        float(bars.open[index]),
        float(bars.high[index]),
        float(bars.low[index]),
        float(bars.close[index]),
    )
    return (
        open_,
        high,
        low,
        close,
        abs(close - open_),
        high - max(open_, close),
        min(open_, close) - low,
        high - low,
    )


def _ma_squeeze(bars: Bars) -> Signal | None:
    """均线粘合：MA5/10/20/60 挤在极窄区间里横住 —— 方向未定的蓄势。

    与「均线多头排列」正好互补：那个要四线**已发散且向上**（趋势中段），
    这个要四线**挤在一起且没在拉**（趋势起点），两者很少同时命中。
    """
    if len(bars) < MA_PERIODS[-1] + SQUEEZE_DAYS:
        return None
    series = [_align(_ma_series(bars.close, period), period, len(bars)) for period in MA_PERIODS]
    if any(not np.isfinite(item[-1]) for item in series):
        return None

    worst = 0.0
    for offset in range(SQUEEZE_DAYS):
        point = -1 - offset
        values = [float(item[point]) for item in series]
        lowest = min(values)
        if lowest <= 0:
            return None
        worst = max(worst, max(values) / lowest - 1)
    if worst > SQUEEZE_MAX_SPREAD:
        return None

    ma20 = series[2]
    base = float(ma20[-1 - SQUEEZE_DAYS])
    if base <= 0:
        return None
    slope = float(ma20[-1] / base - 1)
    if slope > SQUEEZE_SLOPE_MAX:
        return None  # 均线已经在拉了，那不是粘合

    score = _gate_score(worst, SQUEEZE_MAX_SPREAD, SQUEEZE_IDEAL_SPREAD) * 70
    # 横得越平越像蓄势；向上拉起来或向下破位都扣分
    score += _gate_score(abs(slope), SQUEEZE_SLOPE_MAX, 0.0) * 30

    return Signal(
        "ma_squeeze",
        score,
        {
            "breakout": float(bars.high[-SQUEEZE_DAYS:].max()),
            "support": float(bars.low[-SQUEEZE_DAYS:].min()),
        },
        {"squeeze_width": round(worst, 4), "days": SQUEEZE_DAYS, "ma20_rise": round(slope, 4)},
    )


def _ascending_channel(bars: Bars) -> Signal | None:
    """上升通道：近 40 天的最低价拟合出一条向上、且与上轨大致平行的下轨，
    今天收盘落在通道下半部（贴着下轨 = 低吸位）。

    上下轨分别对 low / high 序列做最小二乘，不用两端极值连线（理由见 `_linfit`）。
    """
    if len(bars) < CHANNEL_DAYS:
        return None
    window_high = bars.high[-CHANNEL_DAYS:]
    window_low = bars.low[-CHANNEL_DAYS:]
    mean_close = float(np.mean(bars.close[-CHANNEL_DAYS:]))
    if mean_close <= 0:
        return None

    high_slope, high_intercept = _linfit(window_high)
    low_slope, low_intercept = _linfit(window_low)
    high_daily = high_slope / mean_close
    low_daily = low_slope / mean_close
    if low_daily < CHANNEL_MIN_DAILY or high_daily <= 0:
        return None
    if abs(high_daily - low_daily) / high_daily > CHANNEL_PARALLEL_TOL:
        return None  # 两轨不平行，那是喇叭形

    last = CHANNEL_DAYS - 1
    upper = float(high_intercept + high_slope * last)
    lower = float(low_intercept + low_slope * last)
    if upper <= lower or lower <= 0:
        return None
    close = float(bars.close[-1])
    position = (close - lower) / (upper - lower)
    if not (-0.03 <= position <= CHANNEL_POSITION_MAX):
        return None  # 跌穿下轨，或已经冲到通道上部（那就不是"回踩到位"了）

    index = np.arange(CHANNEL_DAYS, dtype=float)
    upper_series = high_intercept + high_slope * index
    lower_series = low_intercept + low_slope * index
    upper_touch = int(np.sum(np.abs(window_high / upper_series - 1.0) <= CHANNEL_TOUCH_TOL))
    lower_touch = int(np.sum(np.abs(window_low / lower_series - 1.0) <= CHANNEL_TOUCH_TOL))
    # 两条轨各要真被碰到过：只要求「上下轨正好夹住价格」的话，随便一段趋势
    # 都能拟合出两条线来 —— 实测那样全市场有 7% 的票同时"在上升通道里"
    if upper_touch < CHANNEL_MIN_TOUCHES or lower_touch < CHANNEL_MIN_TOUCHES:
        return None
    touches = upper_touch + lower_touch

    score = _band_score(position, *CHANNEL_POSITION_IDEAL, 1.05) * 55
    score += min((low_daily - CHANNEL_MIN_DAILY) / 0.003, 1.0) * 25
    score += min(touches / CHANNEL_TOUCH_FULL, 1.0) * 20

    return Signal(
        "ascending_channel",
        min(score, 100.0),
        {"support": lower, "breakout": upper},
        {
            "daily_slope": round(low_daily, 5),
            "position": round(position, 3),
            "days": CHANNEL_DAYS,
            "touches": touches,
        },
    )


def _ma_golden_cross(bars: Bars) -> Signal | None:
    """金叉共振：MA5 最近 3 天内上穿 MA10，同时 MA10 向上、MA20 不走弱。

    「共振」指的是两个时间尺度同时转好：短期均线刚金叉（动能刚切），
    中期均线也没在向下（趋势没坏）。只有金叉、而 MA20 还在下行的那种，
    一半是真突破一半是反抽，所以拿 MA20 的五日斜率当否决条件。
    """
    if len(bars) < MA_PERIODS[-1] + CANDLE_PRIOR_DAYS + 1:
        return None
    ma5 = _align(_ma_series(bars.close, 5), 5, len(bars))
    ma10 = _align(_ma_series(bars.close, 10), 10, len(bars))
    ma20 = _align(_ma_series(bars.close, 20), 20, len(bars))
    if not (np.isfinite(ma5[-1]) and np.isfinite(ma10[-1]) and np.isfinite(ma20[-1])):
        return None
    if ma5[-1] <= ma10[-1]:
        return None

    cross_ago = 0
    for offset in range(1, CROSS_LOOKBACK + 1):
        above = float(ma5[-1 - offset])
        below = float(ma10[-1 - offset])
        if not (np.isfinite(above) and np.isfinite(below)):
            return None
        if above <= below:
            cross_ago = offset
            break
    if cross_ago == 0:
        return None  # 这几天一直压着，不是「刚金叉」

    look = CANDLE_PRIOR_DAYS
    base10, base20 = float(ma10[-1 - look]), float(ma20[-1 - look])
    if base10 <= 0 or base20 <= 0 or not np.isfinite(base10) or not np.isfinite(base20):
        return None
    rise10 = float(ma10[-1] / base10 - 1)
    rise20 = float(ma20[-1] / base20 - 1)
    if rise10 <= CROSS_MIN_MA10_RISE or rise20 < CROSS_MIN_MA20_RISE:
        return None

    ratio = _volume_ratio(bars, 5)
    score = (CROSS_LOOKBACK + 1 - cross_ago) / (CROSS_LOOKBACK + 1) * 40
    score += min(rise10 / 0.03, 1.0) * 30
    score += min(max(rise20, 0.0) / 0.02, 1.0) * 20
    score += min(ratio / 2.0, 1.0) * 10

    return Signal(
        "ma_golden_cross",
        min(score, 100.0),
        {"support": float(ma10[-1])},
        {
            "cross_days_ago": cross_ago,
            "ma10_rise": round(rise10, 4),
            "ma20_rise": round(rise20, 4),
            "vol_ratio": round(ratio, 2),
        },
    )


def _weekly_bull(bars: Bars) -> Signal | None:
    """周线多头：日线聚合成周线后，周 MA5 > MA10 > MA20 且都在向上。

    为什么值得单独看周线：日线多头排列在震荡里天天出信号，周线多头要求
    **连续 20 周**的结构，频率低得多，是「大方向向上」的粗筛。
    库里的日线约 250 根（≈1 年 ≈ 50 周），做不出 20 周以上的斜率，
    所以不足 `WEEKLY_BULL_MIN_BARS` 根时直接跳过。
    """
    if len(bars) < WEEKLY_BULL_MIN_BARS:
        return None
    weekly = _weekly_closes(bars)
    if weekly.size < 20 + WEEKLY_BULL_SLOPE_WEEKS:
        return None
    ma5 = _ma_series(weekly, 5)
    ma10 = _ma_series(weekly, 10)
    ma20 = _ma_series(weekly, 20)
    if ma5 is None or ma10 is None or ma20 is None:
        return None
    if not (ma5[-1] > ma10[-1] > ma20[-1]):
        return None

    look = WEEKLY_BULL_SLOPE_WEEKS
    base5, base10 = float(ma5[-1 - look]), float(ma10[-1 - look])
    if base5 <= 0 or base10 <= 0:
        return None
    rise5 = float(ma5[-1] / base5 - 1)
    rise10 = float(ma10[-1] / base10 - 1)
    if rise5 <= 0 or rise10 <= 0:
        return None
    gap = float(weekly[-1] / ma5[-1] - 1) if ma5[-1] > 0 else 0.0

    score = _band_score(rise10, *WEEKLY_BULL_RISE_IDEAL, WEEKLY_BULL_RISE_CAP) * 60
    score += _band_score(gap, *WEEKLY_BULL_GAP_IDEAL, 0.15) * 40

    return Signal(
        "weekly_bull",
        min(score, 100.0),
        {"support": float(ma5[-1])},
        {
            "weeks": int(weekly.size),
            "ma10_rise": round(rise10, 4),
            "ma5_gap": round(gap, 4),
        },
    )


def _downtrend_breakout(bars: Bars) -> Signal | None:
    """下降趋势线突破：用近 60 天的**摆动高点**连出一条下降线，今天收盘站上它。

    为什么只认摆动高点（zigzag）而不是窗口最高价：后者连出来是水平线，
    那就把「下降趋势突破」偷换成了「平台突破」，两者含义完全不同。
    """
    if len(bars) < TRENDLINE_DAYS:
        return None
    highs = [
        (index, price)
        for index, price, direction in _recent_pivots(bars, TRENDLINE_DAYS)
        if direction > 0
    ]
    if len(highs) < TRENDLINE_MIN_PIVOTS:
        return None

    use = highs[-(TRENDLINE_MIN_PIVOTS + 1) :]
    prices = np.array([price for _, price in use], dtype=float)
    if np.any(np.diff(prices) >= 0):
        return None  # 高点没有逐个降低，那就不叫下降趋势

    slope, intercept = np.polyfit(np.array([index for index, _ in use], dtype=float), prices, 1)
    line_now = float(intercept + slope * (len(bars) - 1))
    if line_now <= 0:
        return None
    close = float(bars.close[-1])
    if close <= line_now:
        return None
    excess = close / line_now - 1
    if excess > TRENDLINE_MAX_EXCESS:
        return None

    ratio = _volume_ratio(bars, 5)
    score = _band_score(excess, 0.002, 0.03, TRENDLINE_MAX_EXCESS) * 45
    score += _band_score(ratio, 1.0, 2.5, 6.0) * 35
    score += min(len(use) / 3.0, 1.0) * 20

    return Signal(
        "downtrend_breakout",
        min(score, 100.0),
        {"breakout": line_now},
        {"excess": round(excess, 4), "vol_ratio": round(ratio, 2), "pivots": len(use)},
    )


def _gap_up_breakout(bars: Bars) -> Signal | None:
    """向上跳空缺口突破：今天最低价高于昨天最高价（区间不重叠的真缺口），
    且当天收阳、收盘同时创 20 日新高。

    「真缺口」用**今低 > 昨高**判，不用「今开 > 昨收」—— 后者会把开盘价恰好
    等于昨收的那种漏掉，而前者只要两天成交区间不重叠就成立。
    """
    if len(bars) < GAP_BREAK_HIGH_DAYS + 2:
        return None
    prev_high = float(bars.high[-2])
    today_low = float(bars.low[-1])
    if prev_high <= 0 or today_low <= prev_high:
        return None
    gap = today_low / prev_high - 1
    if gap < GAP_MIN:
        return None
    if float(bars.close[-1]) <= float(bars.open[-1]):
        return None  # 跳空后收阴 = 冲高回落

    prior = float(bars.close[-GAP_BREAK_HIGH_DAYS - 1 : -1].max())
    close = float(bars.close[-1])
    if close <= prior:
        return None
    excess = close / prior - 1
    ratio = _volume_ratio(bars, 5)

    score = _band_score(gap, *GAP_IDEAL, GAP_CAP) * 35
    score += _band_score(ratio, 1.2, 3.0, 7.0) * 35
    score += _band_score(excess, 0.002, 0.03, 0.09) * 30

    return Signal(
        "gap_up_breakout",
        min(score, 100.0),
        {"breakout": prior, "support": prev_high},
        {"gap": round(gap, 4), "vol_ratio": round(ratio, 2), "excess": round(excess, 4)},
    )


def _gap_fill(bars: Bars) -> Signal | None:
    """缺口回补：近期向上跳空留下的缺口，今天回踩进缺口里、但收在缺口下沿之上。

    为什么回补缺口反而更值得看：向上跳空是当时抢筹留下的空白区，回踩到这里
    不破，说明缺口下沿（跳空前的最高价）真成了支撑 —— 比追在缺口上方安全。
    """
    if len(bars) < GAP_FILL_LOOKBACK + 3:
        return None
    total = len(bars)
    for start in range(total - 2, total - 2 - GAP_FILL_LOOKBACK, -1):
        if start < 1:
            break
        base = float(bars.high[start - 1])  # 缺口下沿
        top = float(bars.low[start])  # 缺口上沿
        if base <= 0 or top <= base * (1 + GAP_MIN):
            continue
        # 缺口之后到昨天为止都没被回补过（最低价没碰到下沿）
        if start + 1 <= total - 2 and bool((bars.low[start + 1 : total - 1] <= base).any()):
            continue
        width = top - base
        if width <= 0:
            continue

        today_low = float(bars.low[-1])
        today_close = float(bars.close[-1])
        fill = (top - today_low) / width  # 扎进缺口的深度（占缺口宽度）
        if fill < GAP_FILL_MIN_FILL or fill > GAP_FILL_MAX_FILL:
            continue
        if today_close < base:
            continue  # 收盘丢了缺口下沿 = 破位，不是回踩

        since_high = float(bars.close[start:total].max())
        rise = since_high / base - 1 if base > 0 else 0.0
        ratio = _volume_ratio(bars, 5)
        score = _band_score(fill, 0.2, 0.6, GAP_FILL_MAX_FILL + 0.05) * 45
        score += _band_score(rise, 0.005, 0.08, 0.25) * 30
        # 回踩要缩量：带量砸回缺口的，支撑多半守不住
        score += _gate_score(ratio, 1.5, 0.5) * 25

        return Signal(
            "gap_fill",
            min(score, 100.0),
            {"support": base, "breakout": top},
            {
                "gap": round(top / base - 1, 4),
                "fill": round(fill, 3),
                "rise": round(rise, 4),
                "vol_ratio": round(ratio, 2),
                "days_ago": total - 1 - start,
            },
        )
    return None


def _box_breakout(bars: Bars) -> Signal | None:
    """箱体突破：60 天的箱体、上下沿各被触碰 ≥2 次，今天放量收在箱体上沿之上。

    与「平台突破」的区别：平台看的是 30 天**窄幅**（≤18%），抓的是「横久了的启动」；
    箱体反过来要求**宽度够大**（≥8%）且上下沿被反复验证（各 ≥2 次触碰）——
    一个宽幅震荡了三个月的箱子被向上突破，与一段窄平台被突破，含义不一样。
    """
    if len(bars) < BOX_DAYS + 2:
        return None
    box = slice(len(bars) - 1 - BOX_DAYS, len(bars) - 1)
    top = float(bars.high[box].max())
    bottom = float(bars.low[box].min())
    if bottom <= 0:
        return None
    width = (top - bottom) / bottom
    if width < BOX_MIN_RANGE or width > BOX_MAX_RANGE:
        return None

    touches_top = _touches(bars.high[box], top, BOX_TOUCH_TOL)
    touches_bottom = _touches(bars.low[box], bottom, BOX_TOUCH_TOL)
    if touches_top < BOX_MIN_TOUCHES or touches_bottom < BOX_MIN_TOUCHES:
        return None

    close = float(bars.close[-1])
    if close <= top:
        return None
    excess = close / top - 1
    if excess > BOX_MAX_EXCESS:
        return None
    ratio = _volume_ratio(bars, 5)
    if ratio < BOX_VOL_MULT:
        return None

    score = min((touches_top + touches_bottom) / 6.0, 1.0) * 25
    score += _band_score(ratio, BOX_VOL_MULT, 3.0, 7.0) * 30
    score += _band_score(excess, 0.002, 0.03, BOX_MAX_EXCESS) * 25
    score += _gate_score(width, BOX_MAX_RANGE, BOX_MIN_RANGE) * 20  # 箱子越紧实越好

    return Signal(
        "box_breakout",
        min(score, 100.0),
        {"breakout": top, "support": bottom},
        {
            "box_range": round(width, 4),
            "touches_top": touches_top,
            "touches_bottom": touches_bottom,
            "vol_ratio": round(ratio, 2),
            "excess": round(excess, 4),
        },
    )


def _volume_dry_bottom(bars: Bars) -> Signal | None:
    """地量见底：成交量缩到 60 日最低，同时价格处在低位（距 60 日最高收盘回撤 ≥20%）。

    「地量」与「低位」必须**同时**成立：只有地量可能是没人交易的僵尸股；
    只有低位可能是放量下跌的中继。
    """
    if len(bars) < DRY_BOTTOM_DAYS + 1:
        return None
    window_vol = bars.volume[-DRY_BOTTOM_DAYS:]
    lowest = float(window_vol.min())
    average = _safe_mean(window_vol)
    if lowest <= 0 or average <= 0:
        return None
    today_vol = float(bars.volume[-1])
    if today_vol > lowest * DRY_BOTTOM_VOL_MAX:
        return None
    shrink = today_vol / average

    peak = float(bars.close[-DRY_BOTTOM_DAYS:].max())
    if peak <= 0:
        return None
    drawdown = 1 - float(bars.close[-1]) / peak
    if drawdown < DRY_BOTTOM_MIN_DRAWDOWN:
        return None
    pct = float(bars.pct_chg[-1])
    if pct < -2.0:
        return None  # 还在放量下跌的不算「见底」

    score = _gate_score(shrink, 0.6, 0.2) * 45
    score += _band_score(drawdown, DRY_BOTTOM_MIN_DRAWDOWN, 0.45, 0.7) * 30
    score += _gate_score(pct, -2.0, 2.0) * 25

    return Signal(
        "volume_dry_bottom",
        min(score, 100.0),
        {"support": float(bars.low[-DRY_BOTTOM_DAYS:].min())},
        {
            "vol_shrink": round(shrink, 3),
            "drawdown": round(drawdown, 4),
            "pct_chg": round(pct, 2),
        },
    )


def _vp_divergence(bars: Bars) -> Signal | None:
    """量价底背离：价格贴着 40 日新低，但这一段下跌的均量比上一段低点萎缩 ≥30%。

    口径说明：这里取「**缩量新低** = 抛压衰竭」这个读法（价创新低、量不再创新低）。
    另一种常见读法是「放量新低 = 恐慌盘出清」，两种都说得通，本引擎取前者；
    要换口径就把 `VP_DIVERGE_VOL_DROP` 的比较方向改掉。
    """
    if len(bars) < VP_DIVERGE_DAYS + 5:
        return None
    window = bars.close[-VP_DIVERGE_DAYS:]
    lowest = float(window.min())
    if lowest <= 0:
        return None
    close = float(bars.close[-1])
    if close > lowest * (1 + VP_DIVERGE_NEW_LOW):
        return None  # 还没到新低附近

    half = len(window) // 2
    prev_index = len(bars) - VP_DIVERGE_DAYS + int(np.argmin(window[:half]))
    prev_vol = _safe_mean(bars.volume[max(prev_index - 2, 0) : prev_index + 3])
    now_vol = _safe_mean(bars.volume[-5:])
    if prev_vol <= 0 or now_vol <= 0:
        return None
    ratio = now_vol / prev_vol
    if ratio > VP_DIVERGE_VOL_DROP:
        return None

    peak = float(bars.close[-DRY_BOTTOM_DAYS:].max()) if len(bars) >= DRY_BOTTOM_DAYS else float(
        bars.close.max()
    )
    drawdown = 1 - close / peak if peak > 0 else 0.0
    pct = float(bars.pct_chg[-1])

    score = _gate_score(ratio, VP_DIVERGE_VOL_DROP, 0.35) * 45
    score += _band_score(drawdown, DRY_BOTTOM_MIN_DRAWDOWN, 0.45, 0.7) * 25
    score += _gate_score(pct, -2.0, 2.0) * 30

    return Signal(
        "vp_divergence",
        min(score, 100.0),
        {"support": lowest},
        {
            "vol_ratio": round(ratio, 3),
            "new_low": round(close / lowest - 1, 4),
            "drawdown": round(drawdown, 4),
            "pct_chg": round(pct, 2),
        },
    )


def _shrink_limit_up(bars: Bars) -> Signal | None:
    """缩量涨停：涨停（沿用 `LS_LIMIT_PCT` 的 9.5% 口径）却明显没量 —— 惜售。

    与「放量上涨」正好相反：那个要量能配合，这个要**没量**。缩量涨停说明
    卖盘极少，次日容易接着封；但也常常是「一字板买不进」，所以给的是
    观察价值，别当成可执行的买点。
    """
    if len(bars) < 6:
        return None
    pct = float(bars.pct_chg[-1])
    if pct < LS_LIMIT_PCT:
        return None
    ratio = _volume_ratio(bars, 5)
    if ratio <= 0 or ratio > SHRINK_LIMIT_VOL_MAX:
        return None
    if float(bars.close[-1]) <= float(bars.open[-1]):
        return None

    score = _gate_score(ratio, SHRINK_LIMIT_VOL_MAX, 0.25) * 60
    score += _gate_score(pct, LS_LIMIT_PCT, 12.0) * 40

    return Signal(
        "shrink_limit_up",
        min(score, 100.0),
        {"support": float(bars.low[-1])},
        {"vol_ratio": round(ratio, 3), "pct_chg": round(pct, 2)},
    )


def _volume_stall(bars: Bars) -> Signal | None:
    """放量滞涨：放量（量比 ≥1.8）但价格没动（-1.5%~+2.5%）、还留了长上影。

    ⚠️ 偏**警示类**：高位放量滞涨多半是派发，低位才可能是吸筹。位置只写进 detail
    与打分，不做否决（理由见阈值块里那段）。位置看 detail 的「距高点回撤」。
    """
    if len(bars) < DRY_BOTTOM_DAYS + 1:
        return None
    ratio = _volume_ratio(bars, 5)
    if ratio < STALL_VOL_MULT:
        return None
    pct = float(bars.pct_chg[-1])
    if not STALL_PCT_BAND[0] <= pct <= STALL_PCT_BAND[1]:
        return None
    _, _, _, close, _, upper, _, span = _candle(bars)
    if span <= 0:
        return None
    shadow = upper / span
    if shadow < STALL_SHADOW_MIN:
        return None
    peak = float(bars.close[-DRY_BOTTOM_DAYS:].max())
    if peak <= 0:
        return None
    drawdown = 1 - close / peak

    score = _band_score(ratio, STALL_VOL_MULT, 4.0, 8.0) * 35
    score += _band_score(shadow, STALL_SHADOW_MIN, 0.6, 0.95) * 30
    # 低位才给这 35 分：高位放量滞涨同样会入库，但分数明显更低、排不到前面
    score += _band_score(drawdown, STALL_MIN_DRAWDOWN, 0.45, 0.7) * 35

    return Signal(
        "volume_stall",
        min(score, 100.0),
        {"support": float(bars.low[-1])},
        {
            "vol_ratio": round(ratio, 2),
            "pct_chg": round(pct, 2),
            "upper_shadow": round(shadow, 3),
            "drawdown": round(drawdown, 4),
        },
    )


def _volume_pile(bars: Bars) -> Signal | None:
    """量堆：最近 5 天里至少 4 天量比 >1.5，且 5 日均量 / 20 日均量 ≥1.5、价格重心上移。

    看的是**连续性**而不是单日爆量：一根天量可能是消息刺激（一次性的），
    连续四五天的堆量才像资金持续进场。
    """
    if len(bars) < 20 + PILE_DAYS:
        return None
    counts = 0
    for offset in range(PILE_DAYS):
        index = len(bars) - PILE_DAYS + offset
        base = _safe_mean(bars.volume[index - 5 : index])
        if base > 0 and float(bars.volume[index]) / base >= PILE_VOL_MULT:
            counts += 1
    if counts < PILE_MIN_DAYS:
        return None

    average = _safe_mean(bars.volume[-20:])
    if average <= 0:
        return None
    ratio = _safe_mean(bars.volume[-PILE_DAYS:]) / average
    if ratio < PILE_RATIO_5_20:
        return None

    start = float(bars.close[-PILE_DAYS - 1])
    close = float(bars.close[-1])
    if start <= 0:
        return None
    gain = close / start - 1
    if gain <= 0:
        return None  # 价格重心没上移，那就只是放量不是量堆

    score = min(counts / PILE_DAYS, 1.0) * 35
    score += _band_score(ratio, PILE_RATIO_5_20, 3.0, 6.0) * 35
    score += _band_score(gain, 0.005, 0.06, 0.15) * 30

    return Signal(
        "volume_pile",
        min(score, 100.0),
        {"support": float(bars.low[-PILE_DAYS:].min())},
        {
            "pile_days": counts,
            "vol_ratio_5_20": round(ratio, 3),
            "gain": round(gain, 4),
        },
    )


def _v_bottom(bars: Bars) -> Signal | None:
    """V 型底：急跌 → 快速收复，左右两段基本对称，现在已从低点反弹到左侧跌幅的 60% 以上。

    「对称」是它和「圆弧底」的分界：V 型的下跌段与上涨段**天数接近**（快跌快涨），
    圆弧底是慢慢磨出来的（幅度小、时间长）。
    """
    if len(bars) < V_BOTTOM_DAYS:
        return None
    pivots = _recent_pivots(bars, V_BOTTOM_DAYS)
    lows = [(index, price) for index, price, direction in pivots if direction < 0]
    if not lows:
        return None
    low_index, low_price = min(lows, key=lambda item: item[1])
    if low_price <= 0:
        return None

    left = [(index, price) for index, price, direction in pivots if direction > 0 and index < low_index]
    if not left:
        return None
    peak_index, peak_price = max(left, key=lambda item: item[1])
    if peak_price <= low_price:
        return None
    drop = 1 - low_price / peak_price
    if drop < V_BOTTOM_MIN_DROP:
        return None

    close = float(bars.close[-1])
    recovered = close / low_price - 1
    if recovered / drop < V_BOTTOM_MIN_REBOUND:
        return None

    down_days = low_index - peak_index
    up_days = len(bars) - 1 - low_index
    if down_days <= 0 or up_days <= 0:
        return None
    symmetry = max(down_days, up_days) / min(down_days, up_days)
    if symmetry > V_BOTTOM_SYMMETRY:
        return None

    left_vol = _safe_mean(bars.volume[peak_index : low_index + 1])
    right_vol = _safe_mean(bars.volume[low_index + 1 :])
    boost = right_vol / left_vol if left_vol > 0 else 0.0

    score = _gate_score(recovered / drop, V_BOTTOM_MIN_REBOUND, 1.0) * 45
    score += _gate_score(symmetry, V_BOTTOM_SYMMETRY, 1.0) * 30
    score += min(boost / 1.5, 1.0) * 25

    return Signal(
        "v_bottom",
        min(score, 100.0),
        {"support": low_price, "breakout": peak_price},
        {
            "drop": round(drop, 4),
            "recover_pct": round(recovered, 4),
            "down_days": down_days,
            "up_days": up_days,
            "vol_boost": round(boost, 2),
        },
    )


def _round_bottom(bars: Bars) -> Signal | None:
    """圆弧底：90 天窗口走出一条平缓的凹形曲线，现在已从底部抬起 60% 以上。

    开口方向用二次拟合判（二次项 >0 = 先跌后升），但**打分不看向量大小** ——
    二次项系数的量纲随价格与索引刻度放大，同一个形状在 10 元股和 100 元股上
    能差两个数量级。真正的判据是「振幅够小 + 最低点在窗口中段 + 现在已经抬起来」。
    """
    if len(bars) < ROUND_MIN_DAYS:
        return None
    days = min(ROUND_DAYS, len(bars))
    window = bars.close[-days:]
    lowest = float(window.min())
    highest = float(window.max())
    if lowest <= 0 or highest <= lowest:
        return None
    amplitude = highest / lowest - 1
    if amplitude > ROUND_MAX_AMPLITUDE:
        return None

    quad = float(np.polyfit(np.arange(days, dtype=float), window, 2)[0])
    if quad <= 0:
        return None  # 开口向下 = 圆弧顶

    close = float(bars.close[-1])
    recover = (close - lowest) / (highest - lowest)
    if recover < ROUND_MIN_RECOVER:
        return None
    low_position = float(np.argmin(window)) / (days - 1) if days > 1 else 0.5

    score = _gate_score(recover, ROUND_MIN_RECOVER, 1.0) * 45
    score += _band_score(amplitude, 0.08, 0.25, ROUND_MAX_AMPLITUDE) * 30
    score += _band_score(low_position, 0.3, 0.7, 1.0) * 25

    return Signal(
        "round_bottom",
        min(score, 100.0),
        {"support": lowest, "breakout": highest},
        {
            "days": days,
            "amplitude": round(amplitude, 4),
            "recover_pct": round(recover, 3),
            "low_position": round(low_position, 3),
        },
    )


def _triple_bottom(bars: Bars) -> Signal | None:
    """三重底：三次探底位置彼此接近，且当前已站上两个反弹高点连成的颈线。

    颈线取两个反弹高点里**较低**的那个（更严格）：取高的那个会让「刚过其中一个
    高点」也算突破，而那时价格其实还在另一个高点之下。
    """
    if len(bars) < TRIPLE_DAYS:
        return None
    pivots = _recent_pivots(bars, TRIPLE_DAYS)
    lows = [(index, price) for index, price, direction in pivots if direction < 0]
    if len(lows) < 3:
        return None
    use = lows[-3:]
    prices = [price for _, price in use]
    lowest = min(prices)
    highest = max(prices)
    if lowest <= 0:
        return None
    spread = highest / lowest - 1
    if spread > TRIPLE_LOW_TOL:
        return None

    between = [
        price
        for index, price, direction in pivots
        if direction > 0 and use[0][0] < index < use[-1][0]
    ]
    if not between:
        return None
    neckline = min(between)
    close = float(bars.close[-1])
    if close <= neckline:
        return None
    excess = close / neckline - 1
    if excess > TRIPLE_MAX_EXCESS:
        return None
    ratio = _volume_ratio(bars, 5)

    score = _gate_score(spread, TRIPLE_LOW_TOL, 0.01) * 35
    score += _band_score(excess, 0.002, 0.03, TRIPLE_MAX_EXCESS) * 35
    score += _band_score(ratio, 1.0, 2.5, 6.0) * 30

    return Signal(
        "triple_bottom",
        min(score, 100.0),
        {"breakout": neckline, "support": lowest},
        {
            "low_spread": round(spread, 4),
            "excess": round(excess, 4),
            "vol_ratio": round(ratio, 2),
        },
    )


def _rectangle_box(bars: Bars) -> Signal | None:
    """矩形箱体：80 天的宽幅矩形、上下沿反复验证，今天收在箱体上部但**尚未突破**。

    与「箱体突破」的分工：那个要求已经收在上沿之上（右侧交易），这个要求贴着上沿
    待突破（左侧埋伏）。买点不同，所以两个都留着。
    """
    if len(bars) < RECT_DAYS + 2:
        return None
    box = slice(len(bars) - 1 - RECT_DAYS, len(bars) - 1)
    top = float(bars.high[box].max())
    bottom = float(bars.low[box].min())
    if bottom <= 0:
        return None
    width = (top - bottom) / bottom
    if width < RECT_MIN_RANGE or width > RECT_MAX_RANGE:
        return None
    touches_top = _touches(bars.high[box], top, BOX_TOUCH_TOL)
    touches_bottom = _touches(bars.low[box], bottom, BOX_TOUCH_TOL)
    if touches_top < BOX_MIN_TOUCHES or touches_bottom < BOX_MIN_TOUCHES:
        return None

    close = float(bars.close[-1])
    position = (close - bottom) / (top - bottom)
    if not RECT_POSITION_MIN <= position <= 1.0:
        return None  # 必须在上部，且还没突破上沿

    score = min((touches_top + touches_bottom) / 8.0, 1.0) * 35
    score += _band_score(position, RECT_POSITION_MIN, 0.95, 1.05) * 40
    score += _gate_score(width, RECT_MAX_RANGE, RECT_MIN_RANGE) * 25

    return Signal(
        "rectangle_box",
        min(score, 100.0),
        {"breakout": top, "support": bottom},
        {
            "box_range": round(width, 4),
            "position": round(position, 3),
            "touches_top": touches_top,
            "touches_bottom": touches_bottom,
        },
    )


def _wedge_shape(bars: Bars) -> tuple[float, float, float, float] | None:
    """楔形几何：返回 `(上轨日斜率, 下轨日斜率, 收敛比例, 末端间距/均价)`，不成立则 None。

    上下轨分别对 high / low 序列做最小二乘（理由见 `_linfit`）。要求两轨**同向**
    且间距明显收敛 —— 一上一下那种是「三角收敛」，已有专门的形态，不在这里重复收。
    """
    if len(bars) < WEDGE_MIN_DAYS:
        return None
    days = min(WEDGE_DAYS, len(bars))
    mean_close = float(np.mean(bars.close[-days:]))
    if mean_close <= 0:
        return None
    high_slope, high_intercept = _linfit(bars.high[-days:])
    low_slope, low_intercept = _linfit(bars.low[-days:])
    high_daily = high_slope / mean_close
    low_daily = low_slope / mean_close
    if abs(high_daily) < WEDGE_MIN_SLOPE or abs(low_daily) < WEDGE_MIN_SLOPE:
        return None  # 太平，那是箱体
    if high_daily * low_daily <= 0:
        return None  # 一上一下 = 三角收敛，不是楔形

    end = days - 1
    gap_start = float(high_intercept - low_intercept)
    gap_end = float(
        high_intercept + high_slope * end - (low_intercept + low_slope * end)
    )
    if gap_start <= 0 or gap_end <= 0 or gap_end >= gap_start:
        return None  # 没有收敛
    converge = (gap_start - gap_end) / gap_start
    if converge < WEDGE_MIN_CONVERGE:
        return None

    # 同向两轨要收敛，必然是「下轨比上轨陡」：上升楔形下轨涨得快，
    # 下降楔形上轨跌得快 —— 两种情况都归到这一个判据上
    if high_daily >= low_daily:
        return None

    # 两条轨都要真被碰到过。光要求「同向 + 收敛」太松：实测那样全市场有 8.6%
    # 的票"在下降楔形里"，清单会长到没法看
    index = np.arange(days, dtype=float)
    upper_series = high_intercept + high_slope * index
    lower_series = low_intercept + low_slope * index
    high_touch = int(np.sum(np.abs(bars.high[-days:] / upper_series - 1.0) <= WEDGE_TOUCH_TOL))
    low_touch = int(np.sum(np.abs(bars.low[-days:] / lower_series - 1.0) <= WEDGE_TOUCH_TOL))
    if high_touch < WEDGE_MIN_TOUCHES or low_touch < WEDGE_MIN_TOUCHES:
        return None

    return high_daily, low_daily, converge, gap_end / mean_close


def _rising_wedge(bars: Bars) -> Signal | None:
    """上升楔形：两轨都向上但逐渐收敛 —— **偏空**形态（涨势在衰竭）。

    ⚠️ 与其它形态不同，它给的是**风险提示**而不是买点：上升楔形向上突破的
    成功率偏低，多数以下破告终。照样收录的理由是「持仓时看到它」比「选票时
    看到它」更重要 —— 分数越高表示形态越成熟（越接近变盘）。
    """
    shape = _wedge_shape(bars)
    if shape is None:
        return None
    high_daily, low_daily, converge, gap_ratio = shape
    if high_daily <= 0 or low_daily <= 0:
        return None

    score = _gate_score(converge, WEDGE_MIN_CONVERGE, 0.75) * 45
    score += min(low_daily / 0.006, 1.0) * 25  # 涨得越陡越危险
    score += _gate_score(gap_ratio, 0.12, 0.02) * 30  # 末端越挤越接近变盘

    return Signal(
        "rising_wedge",
        min(score, 100.0),
        {"support": float(bars.low[-1]), "breakout": float(bars.high[-1])},
        {
            "converge": round(converge, 3),
            "upper_slope": round(high_daily, 5),
            "lower_slope": round(low_daily, 5),
            "gap_ratio": round(gap_ratio, 4),
        },
    )


def _falling_wedge(bars: Bars) -> Signal | None:
    """下降楔形：两轨都向下但逐渐收敛 —— **偏多**（跌势衰竭，向上突破概率大）。"""
    shape = _wedge_shape(bars)
    if shape is None:
        return None
    high_daily, low_daily, converge, gap_ratio = shape
    if high_daily >= 0 or low_daily >= 0:
        return None

    close = float(bars.close[-1])
    high_slope, high_intercept = _linfit(bars.high[-min(WEDGE_DAYS, len(bars)) :])
    low_slope, low_intercept = _linfit(bars.low[-min(WEDGE_DAYS, len(bars)) :])
    end = min(WEDGE_DAYS, len(bars)) - 1
    upper = float(high_intercept + high_slope * end)
    lower = float(low_intercept + low_slope * end)
    position = (close - lower) / (upper - lower) if upper > lower else 0.5
    if position < WEDGE_FALL_POSITION_MIN:
        return None  # 还在楔形下部 = 跌势没走完，别急着当"向上突破"看
    if gap_ratio > WEDGE_FALL_GAP_MAX:
        return None  # 两轨还没汇合，说明楔形还没走到底

    score = _gate_score(converge, WEDGE_MIN_CONVERGE, 0.75) * 40
    score += _band_score(position, 0.4, 0.95, 1.05) * 30
    score += min(_volume_ratio(bars, 5) / 2.0, 1.0) * 30

    return Signal(
        "falling_wedge",
        min(score, 100.0),
        {"support": lower, "breakout": upper},
        {
            "converge": round(converge, 3),
            "upper_slope": round(high_daily, 5),
            "lower_slope": round(low_daily, 5),
            "position": round(position, 3),
        },
    )


def _diamond(bars: Bars) -> Signal | None:
    """菱形（钻石形）：先扩张后收敛 —— 前两段振幅放大、后两段收窄。

    判法把窗口四等分比各段振幅，这是「情绪先发散、后回归」的可计算版本。
    菱形多出现在转折处，本身不指示方向，所以打分只看形状成熟度与当前位置。
    """
    if len(bars) < DIAMOND_MIN_DAYS:
        return None
    days = min(DIAMOND_DAYS, len(bars))
    quarter = days // 4
    if quarter < 8:
        return None
    ranges = []
    for part in range(4):
        start = len(bars) - days + part * quarter
        segment = slice(start, start + quarter)
        ranges.append(float(bars.high[segment].max() - bars.low[segment].min()))
    if min(ranges) <= 0:
        return None

    expand = ranges[1] / ranges[0] - 1
    contract = (ranges[2] - ranges[3]) / ranges[2]
    if expand < DIAMOND_MIN_EXPAND or contract < DIAMOND_MIN_CONTRACT:
        return None
    # 中段必须明显比两端宽 —— 只看「前段扩张、后段收敛」的话，一段普通波动
    # 也能凑出来（实测全市场 10.7% 的票"是菱形"）
    if min(ranges[1], ranges[2]) < max(ranges[0], ranges[3]) * DIAMOND_MIDDLE_EDGE:
        return None

    tail = slice(len(bars) - quarter, len(bars))
    top = float(bars.high[tail].max())
    bottom = float(bars.low[tail].min())
    position = (float(bars.close[-1]) - bottom) / (top - bottom) if top > bottom else 0.5

    score = _gate_score(expand, DIAMOND_MIN_EXPAND, 0.6) * 35
    score += _gate_score(contract, DIAMOND_MIN_CONTRACT, 0.7) * 35
    score += _band_score(position, 0.4, 0.95, 1.05) * 30

    return Signal(
        "diamond",
        min(score, 100.0),
        {"breakout": top, "support": bottom},
        {
            "days": days,
            "expand": round(expand, 3),
            "contract": round(contract, 3),
            "position": round(position, 3),
        },
    )


def _engulf_shape(bars: Bars) -> tuple[float, float] | None:
    """吞没形态的两根 K 判据，返回 `(今日实体 / 昨日实体, 今日收盘在全日中的位置)`。

    「阳包阴」与「看涨吞没」的形状**完全相同**，差别只在要不要下跌背景
    （见各自实现），所以形状判据只写这一遍。
    """
    if len(bars) < 3:
        return None
    prev_open, _, _, prev_close, prev_body, _, _, prev_span = _candle(bars, -2)
    open_, _, low, close, body, _, _, span = _candle(bars)
    if prev_close >= prev_open:
        return None  # 昨天必须是阴线
    if close <= open_:
        return None  # 今天必须是阳线
    if prev_body <= 0 or prev_span <= 0 or span <= 0:
        return None
    if not (open_ <= prev_close and close >= prev_open):
        return None  # 今日实体没包住昨日实体
    ratio = body / prev_body
    if ratio < ENGULF_MIN_BODY:
        return None
    close_pos = (close - low) / span
    if close_pos < ENGULF_MIN_CLOSE_POS:
        return None  # 包住了但收在下半部 = 冲高回落，不是吞没该有的样子
    return ratio, close_pos


def _yang_wrap_yin(bars: Bars) -> Signal | None:
    """阳包阴：今天的阳线实体完全包住昨天的阴线实体 —— **纯形态，不要求趋势背景**。

    与「看涨吞没」的分工（两个名字在 A 股里常被混用，这里按「要不要下跌背景」分）：
    这个只认两根 K 的包含关系；「看涨吞没」额外要求出现在回调末端。
    """
    shape = _engulf_shape(bars)
    if shape is None:
        return None
    ratio, close_pos = shape
    ratio_vol = _volume_ratio(bars, 5)
    score = _gate_score(ratio, ENGULF_MIN_BODY, 3.0) * 45
    score += _gate_score(close_pos, 0.5, 0.95) * 30
    score += min(ratio_vol / 2.0, 1.0) * 25

    return Signal(
        "yang_wrap_yin",
        min(score, 100.0),
        {"support": float(bars.low[-1])},
        {
            "body_ratio": round(ratio, 2),
            "close_pos": round(close_pos, 3),
            "vol_ratio": round(ratio_vol, 2),
        },
    )


def _bullish_engulfing(bars: Bars) -> Signal | None:
    """看涨吞没：与「阳包阴」同一套包含关系，但**额外要求出现在回调末端**。

    为什么要区分：同样的吞没形态，出现在跌势末端是反转信号，出现在上涨中段
    往往只是中继（甚至是最后一冲）。加一条前置跌幅门槛，就能把两者分开，
    代价是同一个形状会在两只票上分别命中不同的形态名 —— 这是有意的。
    """
    if len(bars) < CANDLE_PRIOR_DAYS + 3:
        return None
    drop = _prior_drop(bars)
    if drop < CANDLE_PRIOR_DROP:
        return None
    shape = _engulf_shape(bars)
    if shape is None:
        return None
    ratio, close_pos = shape
    ratio_vol = _volume_ratio(bars, 5)
    score = _gate_score(ratio, ENGULF_MIN_BODY, 3.0) * 40
    score += _gate_score(close_pos, 0.5, 0.95) * 25
    score += _gate_score(drop, CANDLE_PRIOR_DROP, 0.15) * 35

    return Signal(
        "bullish_engulfing",
        min(score, 100.0),
        {"support": float(bars.low[-1])},
        {
            "body_ratio": round(ratio, 2),
            "close_pos": round(close_pos, 3),
            "prior_drop": round(drop, 4),
            "vol_ratio": round(ratio_vol, 2),
        },
    )


def _hammer(bars: Bars) -> Signal | None:
    """锤子线：长下影（≥2 倍实体）、上影极短、小实体，且出现在下跌之后。

    下影线的含义是「盘中砸下去又被买回来」；没有前置下跌的话，同样的形状
    出现在上涨中段只是普通回踩，不具备反转含义 —— 所以前置跌幅是否决条件。
    实体为 0（十字星）单独排除：那是另一种含义，不按锤子线算。
    """
    if len(bars) < CANDLE_PRIOR_DAYS + 2:
        return None
    _, _, _, _, body, upper, lower, span = _candle(bars)
    if span <= 0 or body <= 0:
        return None
    if body > span * CANDLE_BODY_MAX or upper > span * CANDLE_SHORT_SHADOW:
        return None
    if lower < body * CANDLE_SHADOW_MULT:
        return None
    drop = _prior_drop(bars)
    if drop < CANDLE_PRIOR_DROP:
        return None

    score = _gate_score(lower / body, CANDLE_SHADOW_MULT, 4.0) * 40
    score += _gate_score(body / span, CANDLE_BODY_MAX, 0.05) * 30
    score += _gate_score(drop, CANDLE_PRIOR_DROP, 0.15) * 30

    return Signal(
        "hammer",
        min(score, 100.0),
        {"support": float(bars.low[-1])},
        {
            "shadow_ratio": round(lower / body, 2),
            "body_ratio": round(body / span, 3),
            "prior_drop": round(drop, 4),
        },
    )


def _inverted_hammer(bars: Bars) -> Signal | None:
    """倒锤子线：长上影（≥2 倍实体）、下影极短、小实体，且出现在下跌之后。

    形状与锤子线相反（冲高被打回来），但在跌势末端两者含义接近 ——
    都表示「这个位置的抛压已经衰竭」，所以判据只把上下影对调。
    """
    if len(bars) < CANDLE_PRIOR_DAYS + 2:
        return None
    _, _, _, _, body, upper, lower, span = _candle(bars)
    if span <= 0 or body <= 0:
        return None
    if body > span * CANDLE_BODY_MAX or lower > span * CANDLE_SHORT_SHADOW:
        return None
    if upper < body * CANDLE_SHADOW_MULT:
        return None
    drop = _prior_drop(bars)
    if drop < CANDLE_PRIOR_DROP:
        return None

    score = _gate_score(upper / body, CANDLE_SHADOW_MULT, 4.0) * 40
    score += _gate_score(body / span, CANDLE_BODY_MAX, 0.05) * 30
    score += _gate_score(drop, CANDLE_PRIOR_DROP, 0.15) * 30

    return Signal(
        "inverted_hammer",
        min(score, 100.0),
        {"support": float(bars.low[-1])},
        {
            "shadow_ratio": round(upper / body, 2),
            "body_ratio": round(body / span, 3),
            "prior_drop": round(drop, 4),
        },
    )


def _morning_star(bars: Bars) -> Signal | None:
    """启明星：大阴 → 小实体（探底）→ 大阳，且右侧收盘要收复左侧实体一半以上。

    中间那根不要求跳空（A 股跳空缺口少见，硬要求跳空会把一大半形态挡掉），
    但要求它**实体很小** —— 小实体本身就是「多空在这个位置僵住」的表现。
    """
    if len(bars) < CANDLE_PRIOR_DAYS + 4:
        return None
    open1, _, _, close1, body1, _, _, span1 = _candle(bars, -3)
    _, _, _, _, body2, _, _, span2 = _candle(bars, -2)
    open3, _, _, close3, body3, _, _, span3 = _candle(bars)
    if close1 >= open1 or body1 <= 0 or span1 <= 0:
        return None  # 第一根必须是大阴
    if body1 / close1 < STAR_BIG_BODY:
        return None
    if span2 <= 0 or body2 > body1 * STAR_SMALL_BODY:
        return None  # 第二根必须是小实体
    if close3 <= open3 or body3 <= 0 or span3 <= 0:
        return None  # 第三根必须是大阳
    if body3 / close3 < STAR_BIG_BODY * 0.6:
        return None

    midpoint = (open1 + close1) / 2
    if close3 < midpoint:
        return None
    recover = (close3 - midpoint) / (open1 - close1)  # 收复左侧实体过半之后又多收复了多少
    ratio_vol = _volume_ratio(bars, 5)
    score = _gate_score(recover, 0.0, 0.4) * 35
    score += _gate_score(body3 / body1, 0.5, 1.5) * 35
    score += min(ratio_vol / 2.0, 1.0) * 30

    return Signal(
        "morning_star",
        min(score, 100.0),
        {"support": float(bars.low[-2])},
        {
            "star_recover": round(recover, 3),
            "body_ratio": round(body3 / body1, 2),
            "vol_ratio": round(ratio_vol, 2),
        },
    )


def _three_white_soldiers(bars: Bars) -> Signal | None:
    """红三兵：连续三根阳线、实体扎实、收盘逐日抬高、上影短，三日累计涨幅 ≥3%。

    上影短是它的关键特征（每根都被买到收盘），所以把上影占比也当否决条件 ——
    三根都带长上影的「阳线」其实是三根冲高回落，含义正好相反。
    """
    if len(bars) < 4:
        return None
    closes = [float(bars.close[index]) for index in (-3, -2, -1)]
    if not (closes[0] < closes[1] < closes[2]):
        return None

    bodies: list[float] = []
    for index in (-3, -2, -1):
        open_, _, _, close, body, upper, _, span = _candle(bars, index)
        if span <= 0 or close <= open_:
            return None
        if body < span * SOLDIER_MIN_BODY or upper > span * SOLDIER_MAX_SHADOW:
            return None
        bodies.append(body)

    gain = closes[2] / closes[0] - 1
    if gain < SOLDIER_MIN_GAIN:
        return None
    steady = bodies[2] / bodies[0] if bodies[0] > 0 else 0.0
    ratio_vol = _volume_ratio(bars, 5)

    score = _band_score(gain, SOLDIER_MIN_GAIN, 0.08, 0.2) * 45
    score += min(steady, 1.0) * 25  # 第三根实体不缩水（不缩水=1）
    score += min(ratio_vol / 2.0, 1.0) * 30

    return Signal(
        "three_white_soldiers",
        min(score, 100.0),
        {"support": float(bars.low[-3])},
        {
            "gain": round(gain, 4),
            "steady": round(steady, 2),
            "vol_ratio": round(ratio_vol, 2),
        },
    )


# ---------------------------------------------------------------- 注册表

# 蜡烛形态的分组名。抽成常量有两个原因：一是它比其它组名长得多
# （前端筛选条那个标签列要跟着加宽，见 Patterns.tsx 的 `w-24`），
# 二是以后改名只动这里。
CANDLE_GROUP = "单 K 蜡烛形态"

PATTERNS: tuple[Pattern, ...] = (
    Pattern("wudao_start", "今天可买", "致富", _wudao_start),
    Pattern("wudao_sample", "明天盯", "致富", _wudao_sample),
    Pattern("ma_bull", "均线多头排列", "趋势", _ma_bull),
    Pattern("ma_pullback", "回踩不破", "趋势", _ma_pullback),
    Pattern("new_high", "创 N 日新高", "突破", _new_high),
    Pattern("platform_breakout", "平台突破", "突破", _platform_breakout),
    Pattern("volume_breakout", "放量突破前高", "突破", _volume_breakout),
    Pattern("volume_surge", "放量上涨", "量价", _volume_surge),
    Pattern("dry_pullback", "缩量回踩", "量价", _dry_pullback),
    Pattern("cup_handle", "杯柄", "几何", _cup_handle),
    Pattern("double_bottom", "W 底", "几何", _double_bottom),
    Pattern("triangle", "三角收敛", "几何", _triangle),
    Pattern("head_shoulders", "头肩底", "几何", _head_shoulders),
    Pattern("flag", "旗形", "几何", _flag),
    Pattern("three_stage", "三段式突破", "几何", _three_stage),
    Pattern("limit_surge_flat", "涨停爆量横盘", "量价", _limit_surge_flat),
    Pattern("breakout_flat", "突破后横盘", "量价", _breakout_flat),
    Pattern("n_shape", "N 字选股", "量价", _n_shape),
    Pattern("oneil_breakout", "欧奈尔突破", "突破", _oneil_breakout),
    Pattern("burst_shrink_pullback", "爆量后缩量回踩", "量价", _burst_shrink_pullback),

    # ---- 以下为 2026-09-25 按用户给的形态清单补齐（分组名与清单一致）----
    #
    # 顺序说明：**追加在末尾**而不是插进各自分组里 —— 注册表顺序决定了
    # `/summary` 与筛选条里同组形态的排列，插进去会把用户已经熟悉的顺序打乱。
    #
    # ⚠️ 这一批是形状判定、**没有回测支持**（见文件上方阈值块开头那段说明）：
    # 定位是「按这个形状捞一批票自己看」，不是高胜率信号。
    Pattern("ma_squeeze", "均线粘合", "趋势", _ma_squeeze),
    Pattern("ascending_channel", "上升通道", "趋势", _ascending_channel),
    Pattern("ma_golden_cross", "金叉共振", "趋势", _ma_golden_cross),
    Pattern("weekly_bull", "周线多头", "趋势", _weekly_bull),
    Pattern("downtrend_breakout", "下降趋势线突破", "突破", _downtrend_breakout),
    Pattern("gap_up_breakout", "向上跳空缺口突破", "突破", _gap_up_breakout),
    Pattern("gap_fill", "缺口回补", "突破", _gap_fill),
    Pattern("box_breakout", "箱体突破", "突破", _box_breakout),
    Pattern("volume_dry_bottom", "地量见底", "量价", _volume_dry_bottom),
    Pattern("vp_divergence", "量价底背离", "量价", _vp_divergence),
    Pattern("shrink_limit_up", "缩量涨停", "量价", _shrink_limit_up),
    Pattern("volume_stall", "放量滞涨", "量价", _volume_stall),
    Pattern("volume_pile", "量堆", "量价", _volume_pile),
    Pattern("v_bottom", "V 型底", "几何", _v_bottom),
    Pattern("round_bottom", "圆弧底", "几何", _round_bottom),
    Pattern("triple_bottom", "三重底", "几何", _triple_bottom),
    Pattern("rectangle_box", "矩形箱体", "几何", _rectangle_box),
    Pattern("rising_wedge", "上升楔形", "几何", _rising_wedge),
    Pattern("falling_wedge", "下降楔形", "几何", _falling_wedge),
    Pattern("diamond", "菱形", "几何", _diamond),
    Pattern("morning_star", "启明星", CANDLE_GROUP, _morning_star),
    Pattern("bullish_engulfing", "看涨吞没", CANDLE_GROUP, _bullish_engulfing),
    Pattern("hammer", "锤子线", CANDLE_GROUP, _hammer),
    Pattern("inverted_hammer", "倒锤子线", CANDLE_GROUP, _inverted_hammer),
    Pattern("yang_wrap_yin", "阳包阴", CANDLE_GROUP, _yang_wrap_yin),
    Pattern("three_white_soldiers", "红三兵", CANDLE_GROUP, _three_white_soldiers),
)

PATTERN_NAMES = {pattern.key: pattern.name for pattern in PATTERNS}


def evaluate(bars: Bars, *, min_score: float = MIN_SCORE) -> list[Signal]:
    """跑完所有形态，返回达标的结果（按分数降序）。

    单个形态抛异常只记日志、不影响其余 —— 一个写错的阈值不该让整只票的
    扫描结果全空，那会让人以为是「今天没形态」而不是「引擎坏了」。
    """
    signals: list[Signal] = []
    for pattern in PATTERNS:
        try:
            signal = pattern.detect(bars)
        except Exception:  # noqa: BLE001 - 见 docstring
            logger.warning("形态 %s 计算失败（%s 只）", pattern.key, len(bars), exc_info=True)
            continue
        if signal is not None and signal.score >= min_score:
            signals.append(signal)
    signals.sort(key=lambda item: item.score, reverse=True)
    return signals
