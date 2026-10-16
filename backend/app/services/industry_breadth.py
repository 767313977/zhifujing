"""行业宽度（当日横截面）的**计算**部分。

与其它指标的分工相同：本模块只做纯计算，**不碰数据库**。原始值是 `stock_daily`
的个股收盘价与 `stock_basic.industry`（同花顺三级行业路径），按要求**读取时现算**，
不建新表。

口径：
- 行业取 `stock_basic.industry` 路径的**第一段**（如「房地产-房地产-住宅开发」→
  「房地产」），实测 31 个一级行业；
- 「站上 MA20」= 当日收盘 ≥ 最近 20 根日线的收盘均值（含当日）；**不满 20 根 K 线的
  票不进该行业的分母**（新股上市第 5 天就算「站上 MA20」是假信号）—— 与
  `breadth_daily` 的 `above_ma*` 同一套约定；
- 行业占比 = 站上家数 ÷ 该行业有效家数 × 100（%）。

本模块只服务**当日**横截面：逐日算 250 天的行业占比要好几秒（按票的 20 日窗口
函数），会把情绪周期整页卡住，所以端点只取目标日一天（原委见
`api/market.industry_breadth`）。
"""


def aggregate(rows) -> list[dict]:
    """把 `(industry 全路径, 有效家数, 站上家数)` 聚到**一级行业**，按占比降序。

    返回 `[{industry, total, above, ratio}]`；`ratio` 是百分数，分母为 0 时记 None。
    排序把 None 排在最后（占比不可比的行业不该占前排）。
    """
    buckets: dict[str, list[int]] = {}
    for industry, total, above in rows:
        lv1 = str(industry).split("-")[0].strip()
        if not lv1:
            continue
        acc = buckets.setdefault(lv1, [0, 0])
        acc[0] += int(total)
        acc[1] += int(above)
    items = [
        {
            "industry": name,
            "total": total,
            "above": above,
            "ratio": round(above / total * 100, 1) if total else None,
        }
        for name, (total, above) in buckets.items()
    ]
    items.sort(key=lambda item: (item["ratio"] is None, -(item["ratio"] or 0.0)))
    return items


def median_ratio(items: list[dict]) -> float | None:
    """各行业占比的**中位数**（%）。

    用中位数而不是均值：一级行业的成分股数差得远（机械设备 600+、美容护理 20+），
    等权的中位数才代表「一个典型行业宽不宽」，否则会被大行业拖走。
    """
    values = sorted(item["ratio"] for item in items if item["ratio"] is not None)
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return round(values[mid], 1)
    return round((values[mid - 1] + values[mid]) / 2, 1)
