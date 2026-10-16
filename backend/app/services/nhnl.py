"""NH-NL 新高新低扩散的**计算**部分。

与恐贪 / 接力的分工相同：本模块只做纯计算，**不碰数据库**。原始值（创 60/250 日
收盘新高新低家数）直接读 `breadth_daily`（宽度温度计的采集 job 落库的那张表，
`high60 / low60 / high250 / low250` 四列）—— 它本来就是「数据已在库里」的 A 档
指标，序列在 API 读取时现算，不建新表。

两个读法：
- `diff*`：当日「新高家数 − 新低家数」。正值 = 创新高的票多于创新低的票。
- `cum*`：diff 的**逐日累计净值**（类似涨跌家数 AD 线）。单日 diff 噪声大，
  累计线才是看顶底背离的那条：指数收盘创新高而净值线不创新高 = 顶背离，
  指数新低而净值线不再下探 = 底背离。

null 的传递：high250 / low250 在站内 K 线历史还不够 250 个交易日时整列为 NULL
（与宽度温度计同口径），对应 diff / 净值也是 None —— 前端断线展示，不补 0：
0 是「新高新低刚好打平」，是个真实结论，不能拿来填「不知道」。
"""


def diff(high: int | None, low: int | None) -> int | None:
    """单日「新高 − 新低」。任一侧缺数则 None（不外推、不补 0）。"""
    if high is None or low is None:
        return None
    return high - low


def cumulative(diffs: list[int | None]) -> list[float | None]:
    """diff 序列的累计净值（**输入按交易日升序**），与输入等长。

    从 0 开始累加；diff 为 None 的日子净值记 None（图上断开），累加器保持不动 ——
    之后第一个有效日在断点前的累计值上继续，而不是从 0 重来（历史不足导致的断口
    不是「情绪归零」）。
    """
    result: list[float | None] = []
    acc = 0.0
    for value in diffs:
        if value is None:
            result.append(None)
            continue
        acc += value
        result.append(acc)
    return result


def slice_window(series: list, days: int) -> list:
    """取序列尾巴的 `days` 天。与恐贪的 `slice_window` 同一语义，这里只是简单切片。"""
    return series[-days:] if days < len(series) else series
