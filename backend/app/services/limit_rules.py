"""涨跌幅限制（涨停 / 跌停）的口径。

**为什么不能用一个固定阈值。** 各板块的限制不一样，一刀切 9.5% 会两头出错：
把创业板 / 科创板（20cm）的 +10% 大阳线**误判成涨停**，又会漏掉主板 ST（5cm）的涨停。

| 类别 | 代码前缀 | 限制 |
| --- | --- | --- |
| 沪深主板 / 中小板 | 600 / 601 / 603 / 605、000 / 001 / 002 / 003 | 10% |
| 创业板 | 300 / 301 / 302 | 20% |
| 科创板 | 688 / 689 | 20% |
| 北交所 | 43 / 83 / 87 / 88、920 | 30% |
| 主板 ST / *ST | 主板前缀 + 名称含 ST | 5% |

⚠️ 创业板 / 科创板的 ST **仍是 20%**（2020 年改革后没有 5cm 那一档），所以 ST 只改变
主板的判定 —— 这两类直接返回 20%，不看名称。

## 判定：收盘价 = 交易所的板价，一分不差

1. **收盘价就是当天交易所的板价**：`板价 = 前收 × (1 ± 限幅)`，再按该板块的规则取整到分
2. **收盘价 = 当日最高价**（跌停则是最低价）—— 封板的推论，留着是为了让判据自身可读、
   并防住「收盘 = 最高但没封板」的脏数据

**跌停完全对称**（`is_limit_down`），只是方向相反。

### 板价怎么取整（2026-09-26 实测）

| 板块 | 规则 | 实测依据 |
| --- | --- | --- |
| 沪深主板 / 创业板 / 科创板 | **四舍五入** | 12281 个「近板且收在最高价」的日子里 12089 与四舍五入逐位吻合，落在 ±1 分的只有个位数 |
| 北交所 | **截断**（向下取整到分） | 38 个近板日 **38/38** 吻合截断；其中「比四舍五入价低 1 分」的 17 天全在截断价上，且落在涨停池里的 5 个也都是截断价 |

⚠️ 半分钱的边界**必须用整数 / Decimal 做四舍五入**：4.85×1.1 = 5.335 要进到 5.34，
浮点 `round()` 会给出 5.33（实测 002453 2026-09-18 恰好卡在这里）。
下面的 `board_price` 走 `Decimal.quantize(ROUND_HALF_UP)`，就是为了这个。

### 前收：优先上一根收盘价，只有除权日才反推

`limit_price` 里的「前收」是**交易所当天用的那个基准**：

- 平常就是上一根**不复权**收盘价 —— 两位小数、干净，半分钱的边界要靠它才判得准
- **除权日**例外：交易所用的是除权参考价，只能由官方涨跌幅反推（`close / (1 + pct/100)`）。
  判法：拿上一根收盘价算出的涨跌幅与官方值比，**差 > 0.1pp** 就是除权或数据异常，改用反推值。
  反推值带浮点尾巴，在半分钱边界上可能判反，所以能不用就不用

板价是**原始价**上的概念，所以判定必须在 `_adjusted` **之前**做 —— 前复权整段乘一个系数，
在复权价上「取整到分」没有意义（个股接口就是按这个顺序排的）。

### 为什么不再用「涨跌幅 ≥ 限幅 − 余量」

2026-09-26 之前是 `pct >= 限幅 − 0.5`（固定百分比）+「收盘 = 最高」。两个问题：

1. **固定余量与价格无关**：板价取整的偏差在价格空间里不到一分钱，折成百分比却随价格反向走
   （3.33 元 0.15%、6 元 0.08%、50 元 0.01%）。0.5 在 6 元的票上等于 3 分钱的误差。
2. **余量再小也漏「差 1 分」的日子**：实测 002084 2026-09-10（前收 6.12 → 跌停价 5.51，
   实收 5.52 = 当日最低、全天没碰过 5.51）、002470 2026-09-10、000545 2025-11-21 都是这类误标。
   改成「收盘 = 板价」之后全库这类（涨停 16 / 跌停 29 天）清零。

顺带修掉两个同源问题：新股上市不限幅那几天不再误标（603448 2026-09-08 收 −16.1%，
与 −10% 的板价差得远），涨停侧的同类误标也一起没了。

⚠️ 形态引擎里的 `patterns.LS_LIMIT_PCT = 9.5` 与本模块**不是一套口径**（那个是「全市场一律
9.5%」，用途是筛形态、不吃板块差异），别拿一个去校另一个。

### 名称只用来认 ST，且对「名称口径错了」留了补救

`limit_pct` 靠**当前名称**判 ST（主板 5cm），而历史 ST 变更还原不了。所以某天的涨跌幅
**明显超过**名称口径（> 限幅 + 0.5pp）时，说明那天不是这一档，主板按 10cm 再判一次 ——
否则 ST海王 / *ST康佳A 这类「名称带 ST、当时不是」的日子会被整片误删
（实测 232 涨停 / 131 跌停）。

反方向（当时是 ST、现在摘帽）补救不了：那种日子与「收盘恰好等于 5cm 板价的普通上涨日」
从日线 OHLC 上**无法区分**（强行补会多出 664 个误标），所以只能漏 —— 这是本模块唯一
已知的系统性漏标。
"""

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

CHINEXT_PREFIXES = ("300", "301", "302")
STAR_PREFIXES = ("688", "689")
BSE_PREFIXES = ("43", "83", "87", "88", "920")

# 「收盘 = 最高 / 最低」的容差（元）。两个值来自同一个数据源、封板时逐位相同，
# 留半个分只是防浮点噪声
_CLOSE_IS_HIGH_TOL = 0.005

_CENT = Decimal("0.01")

# 前收与上一根收盘价「对不上」的判据（pp）：超过它就认为那天是除权日 / 数据异常，
# 改用官方涨跌幅反推的基准（见模块说明）
_REF_MISMATCH_PCT = 0.1

# 「名称口径错了」的判据（pp）：涨跌幅比名称给的那一档还大这么多，就说明那天不是这一档
_LIMIT_NAME_SLACK = 0.5


def limit_pct(code: str, name: str | None = None) -> float:
    """这只票当日的涨跌幅限制（百分数，如 10.0 / 20.0 / 5.0）。

    名称只用来认 ST，且**只对主板生效** —— 创业板 / 科创板的 ST 也是 20%。
    """
    c = str(code or "").strip().zfill(6)
    if c.startswith(CHINEXT_PREFIXES) or c.startswith(STAR_PREFIXES):
        return 20.0
    if c.startswith(BSE_PREFIXES):
        return 30.0
    if name and "ST" in name.upper():
        return 5.0
    return 10.0


def is_bse(code: str) -> bool:
    """北交所。它的板价取整规则与沪深不同（截断，见模块说明）。"""
    return str(code or "").strip().zfill(6).startswith(BSE_PREFIXES)


def _ref_price(
    prev_close: float | None, close: float | None, pct_chg: float | None
) -> Decimal | None:
    """交易所当天用的「前收盘价」，**对齐到分**。缺数据给 None。

    优先用上一根不复权收盘价（干净；半分钱边界要靠它判准），只有它与官方涨跌幅对不上
    （除权日 / 数据异常）时才反推 —— 理由见模块说明。

    ⚠️ 反推值必须 `quantize` 到分再用：交易所的基准价本身就是两位小数（除权参考价也是），
    而浮点算出来会带尾巴（实测 `close/(1+10.010277/100)` 得 48.64999999999995），
    乘限幅之后正好落在半分钱下方、被 round 掉一分 —— 实测 000811 2026-06-25（除权日）
    与 603289 2026-09-21 都因此漏判成没封板。
    """
    if close is None or pct_chg is None or close <= 0 or pct_chg <= -100:
        return None
    if prev_close and prev_close > 0:
        raw_pct = (close / prev_close - 1) * 100
        if abs(raw_pct - pct_chg) <= _REF_MISMATCH_PCT:
            return Decimal(repr(prev_close)).quantize(_CENT, rounding=ROUND_HALF_UP)
    derived = close / (1 + pct_chg / 100.0)
    if derived <= 0:
        return None
    return Decimal(repr(derived)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _limit_candidates(code: str, name: str | None, pct_chg: float | None) -> tuple[float, ...]:
    """这天可能适用的限幅档位。名称只认得出「现在」的 ST，所以对明显超出名称口径的日子
    再补一档主板 10cm（见模块说明「名称只用来认 ST」）。
    """
    named = limit_pct(code, name)
    if pct_chg is not None and named == 5.0 and abs(pct_chg) > 5.0 + _LIMIT_NAME_SLACK:
        return (named, 10.0)
    return (named,)


def board_price(ref: Decimal, lim: float, code: str, *, up: bool) -> Decimal:
    """交易所的板价 = 前收 ×(1 ± 限幅)，按板块规则取整到分（沪深四舍五入、北交所截断）。"""
    ratio = Decimal(1) + (Decimal(str(lim)) if up else -Decimal(str(lim))) / Decimal(100)
    return (ref * ratio).quantize(
        _CENT, rounding=ROUND_DOWN if is_bse(code) else ROUND_HALF_UP
    )


def _sealed(
    pct_chg: float | None,
    close: float | None,
    extreme: float | None,
    code: str,
    name: str | None,
    prev_close: float | None,
    *,
    up: bool,
) -> bool:
    """当天是否**收盘封在板价上**（涨 / 跌两个方向共用）。缺数据（停牌等）一律 False。"""
    if pct_chg is None or close is None or extreme is None or close <= 0:
        return False
    ref = _ref_price(prev_close, close, pct_chg)
    if ref is None:
        return False
    px = Decimal(repr(close)).quantize(_CENT)
    # 两边都取整到分，所以直接比相等即可（不存在「差不多」）
    if not any(
        px == board_price(ref, lim, code, up=up)
        for lim in _limit_candidates(code, name, pct_chg)
    ):
        return False
    # 收盘封板 ⇒ 收盘价即当日最高 / 最低价（见模块说明第 2 条）
    return close >= extreme - _CLOSE_IS_HIGH_TOL if up else close <= extreme + _CLOSE_IS_HIGH_TOL


def is_limit_up(
    pct_chg: float | None,
    close: float | None,
    high: float | None,
    code: str,
    name: str | None = None,
    prev_close: float | None = None,
) -> bool:
    """这一天是不是收盘涨停（收盘价 = 涨停价）。

    `prev_close` 传**上一根不复权收盘价**（判板价要用，见模块说明）；拿不到时可省，
    这时会退回用官方涨跌幅反推的基准，代价是半分钱边界上可能判反。
    """
    return _sealed(pct_chg, close, high, code, name, prev_close, up=True)


def is_limit_down(
    pct_chg: float | None,
    close: float | None,
    low: float | None,
    code: str,
    name: str | None = None,
    prev_close: float | None = None,
) -> bool:
    """这一天是不是收盘跌停（收盘价 = 跌停价）。与 `is_limit_up` 完全对称。"""
    return _sealed(pct_chg, close, low, code, name, prev_close, up=False)
