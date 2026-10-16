"""北向活跃度（外资参与度）的**计算**部分。

与其它指标的分工相同：本模块只做纯计算，**不碰数据库**。原始值来自 `hsgt_daily`
（沪深股通**成交金额**，单位百万元）与 `market_sentiment.total_amount`
（两市成交额，单位元），读取时现算，不建新表。

⚠️ **只有成交额，没有净流入**：2024-08-19 起交易所不再按日披露买入 / 卖出金额，
「北向净流入」在数据源层面已经不存在（实测三条印证见 `models.HsgtDaily` 的注释）。
所以本指标是**活跃度 / 参与度，不含方向** —— 口径说明里绝不能出现「净」字。

口径：`北向活跃度 = 沪深股通成交额 ÷ 两市成交额 × 100（%）`

为什么用「占比」而不是成交额绝对值：绝对值随全市场量能潮起潮落，占比才是归一化
后的「外资参与度」。

null 的传递：任一侧缺失（通道关闭日、或两市成交额缺失）记 None，图上断开、**不补 0**
—— 0% 是「北向完全没交易」，那是错的结论；通道关闭只是没数据。
"""

# 沪深股通成交额原始单位是**百万元**，换成元要 ×1e6
MILLION = 1e6
# 展示用：百万元 → 亿元（1 亿元 = 100 百万元）
MILLION_TO_YI = 1 / 100


def ratio(
    turnovers: list[float | None], amounts: list[float | None]
) -> list[float | None]:
    """逐日「北向成交额 ÷ 两市成交额 × 100」（%），与输入等长（升序）。

    `turnovers` 单位百万元、`amounts` 单位元；任一侧为空、或两市成交额为 0 时记 None。
    """
    out: list[float | None] = []
    for turnover, amount in zip(turnovers, amounts, strict=True):
        if turnover is None or amount is None or amount <= 0:
            out.append(None)
            continue
        out.append(round(turnover * MILLION / amount * 100, 2))
    return out


def mean_tail(values: list[float | None], end: int, size: int) -> float | None:
    """窗口内**有数的**最近 `size` 项均值（`end` 为闭区间右端下标），找不齐就 None。

    跳过 None 而不是要求连续：通道关闭日会在序列上留断口，要求连续会让指标在
    休市周附近整段失效（与 `services.obv` 的分位窗口、`services.etf_flow.window_sum`
    同一套路）。窗口内有数的项少于**一半**时返回 None —— 样本太少不硬凑。
    """
    window = [v for v in values[max(0, end - size + 1) : end + 1] if v is not None]
    if len(window) < max(1, size // 2):
        return None
    return round(sum(window) / len(window), 2)
