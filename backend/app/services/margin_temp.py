"""两融杠杆温度的**计算**部分。

与恐贪 / 接力的分工相同：本模块只做纯计算，**不碰数据库**。原始值是
`margin_daily` 的沪 + 深两市「融资余额 + 融券余额」合计（亿元），读取时现算，
不建新表。

口径：
- **余额**：按 `trade_date` 汇总 sh + sz 的 `financing_balance + securities_balance`（亿元）；
- **变化率**：`(余额[i] − 余额[i−CHG_DAYS]) / 余额[i−CHG_DAYS] × 100`（%），
  按**序列位置**回溯 `CHG_DAYS` 个观测（不是自然日）；
- **温度**：变化率在滚动 `WINDOW` 个观测窗口里的中位秩（0–100，复用恐贪那套
  `percentile_score`）—— 越高 = 最近加杠杆越猛。

为什么温度用「20 日变化率的分位」而不是「余额水平的分位」：两融余额是长期上行的
慢变量，绝对水平的分位几乎恒在 100 附近（余额不断创历史新高），没有信息量；变化率
才回答「最近是在加杠杆还是在去杠杆」。

为什么窗口取 120 而不是恐贪的 250：站内 `margin_daily` 目前只有约 263 个交易日，
250 日窗口几乎等于全历史、出不了几个分位点。窗口是纯读时参数，改口径不用回补数据。

null 的传递：余额缺失的日期记 None，变化率与温度随之 None（图上断开），**不补 0** ——
0% 是「余额纹丝不动」，那是个真实结论。
"""

CHG_DAYS = 20
WINDOW = 120


def change_rate(balances: list[float | None]) -> list[float | None]:
    """逐日的 `CHG_DAYS` 日变化率（%，升序），与输入等长。

    前 `CHG_DAYS` 个观测没有基准值、基准余额缺失或为 0 时，都记 None。
    """
    result: list[float | None] = []
    for i, value in enumerate(balances):
        if i < CHG_DAYS or value is None:
            result.append(None)
            continue
        base = balances[i - CHG_DAYS]
        if base is None or base == 0:
            result.append(None)
            continue
        result.append(round((value - base) / base * 100, 2))
    return result
