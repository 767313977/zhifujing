"""市场情绪指标计算。

涨停/跌停/炸板家数一律以 akshare 池子为准，**不用 iFinD 指数级数据**：
iFinD 的涨跌家数只对上证指数有效（深证A指、国证A指、中证全指均返回 null），
而深证成指仅含 500 只成分股，会严重低估深市涨停数。
涨跌家数则取乐咕乐股的全市宽度。
"""

from datetime import date


def seal_and_broken_rate(
    limit_up_count: int, broken_count: int
) -> tuple[float | None, float | None]:
    """封板率 / 炸板率。

    分母是当日所有触及涨停的尝试数（封住 + 炸开），而非全市场股票数。
    """
    attempts = limit_up_count + broken_count
    if attempts == 0:
        return None, None
    return (
        round(limit_up_count / attempts * 100, 2),
        round(broken_count / attempts * 100, 2),
    )


def build_sentiment(
    trade_date: date,
    *,
    limit_up_count: int,
    limit_down_count: int,
    broken_count: int,
    consecutive_list: list[int],
    up_count: int | None,
    down_count: int | None,
    total_amount: float | None,
    yesterday_limit_today_avg: float | None = None,
) -> dict:
    """汇总当日情绪指标，返回可直接写入 MarketSentiment 的字段字典。"""
    seal_rate, broken_rate = seal_and_broken_rate(limit_up_count, broken_count)
    return {
        "trade_date": trade_date,
        "limit_up_count": limit_up_count,
        "limit_down_count": limit_down_count,
        "broken_count": broken_count,
        "seal_rate": seal_rate,
        "broken_rate": broken_rate,
        # 连板高度只看涨停池，跌停池是「连续跌停」、炸板池没有该列
        "max_consecutive": max(consecutive_list) if consecutive_list else None,
        "up_count": up_count,
        "down_count": down_count,
        "total_amount": total_amount,
        "yesterday_limit_today_avg": yesterday_limit_today_avg,
    }
