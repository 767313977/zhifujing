"""量价因子打分：给**同一个候选池内**的票排序。

## 只用「池内分位」，不跨池比

横截面 IC（见 `docs/回测_量价因子IC_20261024.md`）说明的是「排序有信息」，
不是「分数 90 的票一定比分数 60 的票能赚」—— 所以这里把三个因子各自在**这一批候选
内部**算分位再加权，分数只对同一天的同一批票有意义。

## 三个因子与方向（2026-10-24 的 5 年 IC 实验）

| 因子 | 含义 | 方向 | 5 日 IC / t |
| --- | --- | --- | --- |
| `rev20` | 20 日复利涨幅取负 | 跌得多 → 分高 | +0.062 / 13.5 |
| `illiq20` | 20 日平均 |涨跌幅| ÷ 成交额（亿） | 越不流动 → 分高 | +0.058 / 12.3 |
| `amp20` | 20 日平均振幅 (高−低)/收 | 越安静 → 分高 | −0.079 / −12.7 |

权重按 |IC| 归一化成 3:3:4（振幅最强），写成常量；换样本期要重跑 IC 再调。

⚠️ 这三个都是**短期反转 / 低波动**类因子，与「首板打板」的追强逻辑天然相反 ——
它的用途是「同一批候选里先看哪只」，不是「用它去选涨停股」。
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import StockDaily, TradeCalendar
from app.services import rps
from app.sources.base import chunked

logger = logging.getLogger(__name__)

#: 一次 IN 查询最多带多少个代码。老 SQLite 单条 SQL 的变量上限是 999
#: （与 `api/patterns._IN_BATCH` 同一个数，给上限留余量）。
#:
#: 形态页 `min_score=0` 时候选可到 5000+ 只，一次 `code IN (...)` 会直接
#: OperationalError（CI #234 那类错误）。分批后各批代码不重叠、结果在内存里合并，
#: 与一次查全等价。
_IN_BATCH = 900

#: 因子窗口（交易日）
WINDOW = 20

#: 权重：来自 2026-10-24 的 5 年 IC（|IC| 0.062 / 0.058 / 0.079 → 3:3:4）
WEIGHTS = {
    "rev20": 0.30,
    "illiq20": 0.30,
    "amp20": 0.40,
}

#: 窗口内至少要有这么多比例的行，否则该因子记 None（与 RPS 同口径）
MIN_COVERAGE = 0.9


@dataclass(frozen=True, slots=True)
class FactorScore:
    code: str
    rev20: float | None
    illiq20: float | None
    amp20: float | None
    #: 0~100，池内分位加权；因子全部缺失时是 0
    score: float


def _window_days(session: Session, day: date) -> list[date]:
    days = list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= day)
            .order_by(TradeCalendar.trade_date.desc())
            .limit(WINDOW)
        )
    )
    return sorted(days)


def score_series(series: dict[str, list[tuple]]) -> dict[str, FactorScore]:
    """把「每只票窗口内的日线序列」折算成因子分 —— **纯函数，不碰数据库**。

    `series[code]` 是 `(交易日, 涨跌幅, 最高, 最低, 收盘, 成交额)` 的升序列表
    （通常 20 根）。分位是**池内**的：传进来的这批代码一起比较，所以页面与回测
    都必须按「同一天的同一批候选」调用，不能跨池比分数。

    抽出来的原因（2026-11-01）：① `scores()` 只管取数，公式只有一份；
    ② 回测脚本用内存里已装好的 bars 直接算，不必查库（`history.db` 没有交易日历）。
    """
    minimum = int(WINDOW * MIN_COVERAGE)
    raw: dict[str, dict[str, float | None]] = {}
    for code, items in series.items():
        pcts = [(when, float(pct)) for when, pct, _h, _l, _c, _a in items if pct is not None]
        # 20 日复利涨幅（取负：跌得多反而分高）
        ret = rps.ret([value for _when, value in pcts], WINDOW)
        rev = None if ret is None else -ret

        amplitudes = [
            (high - low) / close
            for _when, _pct, high, low, close, _amount in items
            if high is not None and low is not None and close
        ]
        amp = (
            sum(amplitudes) / len(amplitudes)
            if len(amplitudes) >= minimum
            else None
        )

        liquidities = [
            abs(pct) / (amount / 1e8)
            for _when, pct, _h, _l, _c, amount in items
            if pct is not None and amount
        ]
        illiq = (
            sum(liquidities) / len(liquidities)
            if len(liquidities) >= minimum
            else None
        )
        raw[code] = {"rev20": rev, "illiq20": illiq, "amp20": amp}

    # 池内分位：缺失的按 0 分位参与（等于排最后），不把整只票丢掉
    ordered = list(raw)
    percentiles: dict[str, dict[str, float]] = {code: {} for code in ordered}
    for key in ("rev20", "illiq20", "amp20"):
        # 振幅是「越低越好」，取负后与其他因子同向
        signal = [
            None
            if raw[code][key] is None
            else (-raw[code][key] if key == "amp20" else raw[code][key])
            for code in ordered
        ]
        ranked = rps.percentiles([value for value in signal if value is not None])
        cursor = 0
        for index, code in enumerate(ordered):
            if signal[index] is None:
                percentiles[code][key] = 0.0
            else:
                percentiles[code][key] = ranked[cursor]
                cursor += 1

    out: dict[str, FactorScore] = {}
    for code, computed in raw.items():
        score = (
            percentiles[code]["rev20"] * WEIGHTS["rev20"]
            + percentiles[code]["illiq20"] * WEIGHTS["illiq20"]
            + percentiles[code]["amp20"] * WEIGHTS["amp20"]
        )
        out[code] = FactorScore(
            code=code,
            rev20=computed["rev20"],
            illiq20=computed["illiq20"],
            amp20=computed["amp20"],
            score=round(score, 1),
        )
    return out


def scores(session: Session, codes: list[str], day: date) -> dict[str, FactorScore]:
    """给 `codes` 在这一天算因子分（键 = 代码；算不出来的不出现）。

    只负责取数：窗口取 `<= day` 的最近 `WINDOW` 个交易日，按 `_IN_BATCH` 分批查
    （形态页候选可能到 5000+），再交给 `score_series` 打分。
    """
    if not codes:
        return {}
    days = _window_days(session, day)
    if not days:
        return {}

    series: dict[str, list[tuple]] = defaultdict(list)
    # 分批前先排序去重：排序让分批稳定可复现，去重保证「一只票只落在一批里」——
    # 否则同一只票会被查两次、`series` 里出现重复的 K 线，涨跌幅复利直接算错。
    for batch in chunked(sorted(set(codes)), _IN_BATCH):
        for code, when, pct, high, low, close, amount in session.execute(
            select(
                StockDaily.code,
                StockDaily.trade_date,
                StockDaily.pct_chg,
                StockDaily.high,
                StockDaily.low,
                StockDaily.close,
                StockDaily.amount,
            )
            .where(
                StockDaily.trade_date.in_(days),
                StockDaily.code.in_(batch),
            )
            .order_by(StockDaily.code, StockDaily.trade_date)
        ):
            series[code].append((when, pct, high, low, close, amount))
    return score_series(series)
