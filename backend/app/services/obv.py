"""量能潮 OBV（On Balance Volume）的**计算**部分。

与恐贪 / 接力的分工相同：本模块只做纯计算，**不碰数据库**。原始值是基准指数
（上证指数，与恐贪基准同源）的日线涨跌幅与成交额，直接读 `index_daily` ——
一天一行的小表，读取时现算，不建新表。

口径（经典 OBV，把「成交量」换成「成交额」——本站盯的是钱的进出）：
- 上涨日：累加当日成交额
- 下跌日：扣减当日成交额
- 平盘 / 涨跌幅缺失：不加不减

用法：OBV 与指数点位的**背离** —— 指数创新高而 OBV 不创新高 = 量能跟不上，
是存量博弈的警示；指数横盘而 OBV 持续爬升 = 有资金在悄悄进场。

null 的传递：成交额缺失的日子 OBV 记 None（图上断开），累加器保持不动 ——
与其它指标同一套「断线不补 0」约定。
"""


def build(pct_chgs: list[float | None], amounts: list[float | None]) -> list[float | None]:
    """由涨跌幅与成交额序列（**按交易日升序、两者等长**）算 OBV 累计净值。

    返回与输入等长的累计净额（元），从 0 开始累加。单日变动额（带正负）可用
    `daily_change` 单独拿。
    """
    result: list[float | None] = []
    acc = 0.0
    for pct, amount in zip(pct_chgs, amounts, strict=True):
        if amount is None:
            result.append(None)
            continue
        if pct is not None:
            if pct > 0:
                acc += amount
            elif pct < 0:
                acc -= amount
            # 平盘：不加不减
        result.append(acc)
    return result


def daily_change(pct_chgs: list[float | None], amounts: list[float | None]) -> list[float | None]:
    """每日的**变动额**（带正负，元），与输入等长。

    平盘 / 涨跌幅缺失记 0（不是 None：那天真实发生了交易，只是 OBV 不动）；
    成交额缺失记 None。
    """
    result: list[float | None] = []
    for pct, amount in zip(pct_chgs, amounts, strict=True):
        if amount is None:
            result.append(None)
        elif pct is None or pct == 0:
            result.append(0.0)
        elif pct > 0:
            result.append(amount)
        else:
            result.append(-amount)
    return result
