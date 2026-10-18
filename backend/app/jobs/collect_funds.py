"""资金面数据采集：两融、沪深股通成交额、ETF 份额、龙虎榜机构席位。

为什么单独一个模块而不是并进 `collect_daily`：这四块都是「每日一个快照」的
市场级数据，彼此独立、失败互不影响；而且数据源分两类（iFinD EDB 与 akshare），
分开能让「哪块坏了」在采集日志里一眼看到。

配额成本：两融 1 次 + 北向 1 次 = **每天约 2 次 iFinD 调用**。
（ETF 2026-10-18 起换成交易所官方份额 + 通达信盘后包，**零 iFinD 配额**；
在那之前它要 59 次/天，是整条链路里最贵的一项 —— 原委见
`collect_etf` / `_collect_etf_ifind`。）
EDB 按区间返回，但区间不能开太大（见 EDB_CHUNK_DAYS），
所以回补一年的历史是 3 段 × 2 项 = 6 次，仍然很便宜。
"""

import logging
import re
from collections import Counter
from datetime import date, timedelta

from sqlalchemy import func, select

from app.db import session_scope, upsert, upsert_fill, upsert_many
from app.models import (
    EtfCategory,
    EtfShare,
    HsgtDaily,
    LhbInstitution,
    MarginDaily,
    TradeCalendar,
)
from app.services.etf_category import classify
from app.sources import etf_exchange, tdx
from app.sources.akshare_source import AkshareSource
from app.sources.ifind import (
    QUOTA_EXHAUSTED_HINTS as _QUOTA_EXHAUSTED_HINTS,
    IfindClient,
    normalize_code,
)
from app.sources.markdown_table import pick_float, pick_text, to_float, to_int

logger = logging.getLogger(__name__)

# iFinD 基金工具的**单批只数上限**。2026-10-14 实测：**30 只正常返回 30 行、
# 31 只整批被拒**（同一批代码做前缀二分，31/32/33/34 全拒、30 通过）。
#
# ⚠️ 这里原来写 80，注释还写着「实测 80 只一次问能全给」—— 那是 2026-09-22 的量法，
# 来源后来把上限收到 30 了。之所以一直没被发现，是因为**来源的拒绝提示被丢掉了**：
# 它不说谎，回的是「查询问句中包含证券主体数量过多，请适当拆分后发起多次调用」，
# 但 `_nl` 只把 markdown 表行返回给调用方，那句话没人看 —— 于是 80 只那一批
# 表现为「静默返回空」，被拆半逻辑 80→40→20 兜了一整天，1745 只烧掉 **154 次**
# 调用（模型里按 20 次算）。所以这个数**必须配一次实测**，失败时必须看
# `answer` 说了什么（见 `_fetch_etf_quotes`）。
ETF_BATCH = 30

# 来源拒绝「一批问太多只」时的原话（2026-10-14 实测两条都出现过）。命中它才拆半重试。
_TOO_MANY_HINTS = ("证券主体数量过多", "问句过长")

# 来源在**配额耗尽**时不报错，而是把这句话写进回答、且不带任何数据 —— 与「这批基金
# 本来就没有数据」长得一模一样。必须单独识别：否则整批被当成「无数据」跳过，`etf`
# 步骤照样记 `ok`（rows=0）。2026-10-16 就是这么静默丢掉 10-14 ~ 10-16 三天 ETF
# 份额的：7 天日志里 41 次「当前账户MCP请求用量已耗尽」，数据管理页却一路绿灯。
#
# 2026-10-18：识别已下沉到 `sources/ifind.call()`（所有 iFinD 调用统一抛
# `IfindQuotaExhausted`），这里保留同一份提示词做**第二道识别** —— 万一将来某条
# 路径绕过 `call()` 直接解析文本，也不至于又静默成「无数据」。

# 一批里问的指标。**四个是实测的上限**：再多问两个就只剩一张表、份额整列消失
# （见 `IfindClient.fund_profile` 的说明）。
ETF_METRICS = "基金份额、收盘价、成交额、涨跌幅"

# 每次采集回看的日历天数。覆盖周末 + 最长节假日（8 天）后仍有大量余量 ——
# 这样某天采集失败，下一次会自然把缺的补上，不必单独写回补逻辑。
EDB_LOOKBACK_DAYS = 30

# ETF 一次采集至少要写进这么多比例的行，否则整批丢弃。
#
# **宁可不写也不写半份快照。** 份额表是按「当日全市场」配对算净申赎的，
# 半份快照（比如某一批 iFinD 被拒、只回了一半）落库后，第二天拿它当基准日
# 只能配上那一半的代码，算出来的净申赎会明显偏小 —— 但**看起来是个正常数字**，
# 比缺一天更危险。正常一次是 1700/1745 ≈ 97%，留到 60% 只挡真正的灾难。
ETF_MIN_WRITE_RATIO = 0.6

# 单次 EDB 查询的日历跨度上限。**必须分段**：iFinD 的自然语言 EDB 接口在
# 区间过大时会**抽样**返回 —— 实测一次要 250 天只给回 106 天，而且缺口是
# 隔天出现的（不是截断掉末尾），画成曲线就是一条锯齿，很难看出来。
# 实测 120 天（86 个交易日）完整，150 天就开始丢行，取 90 天留足余量。
EDB_CHUNK_DAYS = 90

# 两融：市场 → (融资余额, 融资买入额, 融券余额) 的同花顺指标名。
# **必须写全名**：实测问「融资余额」会被模糊匹配成工商银行的个股融资余额。
MARGIN_INDICATORS = {
    "sh": ("上交所:融资余额", "上交所:融资买入额", "上交所:融券余额"),
    "sz": ("深交所:融资余额", "深交所:融资买入额", "深交所:融券余额"),
}

# 沪深股通（北向）成交金额。**只有成交总额、没有净流入** —— 详见 models.HsgtDaily。
# 实测一年里有 4 天取不到值（2026-04-03 / 04-07 / 05-25 / 07-01）：那几天
# A 股正常交易（两融有数）、但沪股通与深股通**同时**为空，只可能是港股休市、
# 北向通道关闭。这种日子留空而不是补 0 —— 补 0 会把「没开市」画成
# 「北向成交为零」，在图上看起来像资金突然撤离。
HSGT_INDICATORS = {"sh": "沪股通:当日成交金额", "sz": "深股通:当日成交金额"}

# 1 个「单位」等于多少元 —— 用来把 EDB 列名里的单位归一。
# **只列能确定的**：认不出的单位一律拒绝写入（见 `_value`），不猜。
_UNIT_TO_YUAN = {
    "元": 1.0,
    "万元": 1e4,
    "十万元": 1e5,
    "百万元": 1e6,
    "千万元": 1e7,
    "亿元": 1e8,
    "十亿元": 1e9,
}

# 库里各表的单位（**不能改成元**：`api/funds.py` 固定按 ×1e8 / ×1e6 换算成元展示，
# 改这里会让读端差几个数量级）：
# - margin_daily 存**亿元**（见 models.MarginDaily）
# - hsgt_daily 存**百万元**（见 models.HsgtDaily）
_YUAN_PER_YI = 1e8
_YUAN_PER_MILLION = 1e6

# 从列名里抠单位，形如 `上交所:融资买入额（单位：亿元）`。半角/全角冒号都认。
_UNIT_RE = re.compile(r"单位[:：]\s*([^）)\s]+)")


def _day(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _cell(row: dict, prefix: str) -> tuple[str, float | None] | None:
    """按列名**前缀**找到那一列，返回 `(列名, 原始数值)`。没找到返回 None。"""
    for key, raw in row.items():
        text = str(key)
        if text.startswith(prefix):
            return text, to_float(raw)
    return None


def _present(row: dict, prefix: str) -> bool:
    """该前缀的列是否存在且有值（**不做单位换算**）。

    给 `_edb_chunk` 的「整列是否缺失」判断用：即便单位认不出来、值没法归一，
    「这列在不在」也是另一回事，混用会让补问逻辑误判。
    """
    found = _cell(row, prefix)
    return found is not None and found[1] is not None


def _unit_factor(column: str) -> float | None:
    """从列名里解析单位，返回「1 单位 = 多少元」；认不出返回 None。

    先按标准写法 `（单位：亿元）` 抠；抠不到再退一步，直接在列名里找已知单位词
    （如 `上交所:融资余额（亿元）`）。**只认明确写出来的单位**，找不到就 None
    （调用方据此拒绝写入）—— 不猜。
    """
    match = _UNIT_RE.search(column)
    if match:
        return _UNIT_TO_YUAN.get(match.group(1))
    # 按长度降序匹配，免得「百万元」被「万元」抢先命中
    for token in sorted(_UNIT_TO_YUAN, key=len, reverse=True):
        if token in column:
            return _UNIT_TO_YUAN[token]
    return None


def _value(row: dict, prefix: str, *, target_yuan: float) -> float | None:
    """按列名**前缀**取 EDB 的值，并把上游单位**归一成库里的单位**。

    列名带单位后缀（`上交所:融资买入额（单位：亿元）`），而单位**会变**
    （亿元 / 万元 / 百万元）。写死全名会在单位调整后静默取不到值；而按固定系数
    换算、不解析单位，则会在单位变化时静默差 10²~10⁴ 倍，还会经 `upsert_fill`
    把 30 天回看窗口整段改写成新单位。所以：**从列名解析出单位再换算**；
    **认不出单位就拒绝写入**（返回 None 并记 warning）—— 不瞎猜。
    """
    found = _cell(row, prefix)
    if found is None:
        return None
    column, number = found
    if number is None:
        return None
    factor = _unit_factor(column)
    if factor is None:
        logger.warning(
            "EDB 列 %r 的单位无法识别（缺「单位：X」后缀或单位不在已知表内），"
            "本次拒绝写入该值，避免写错数量级",
            column,
        )
        return None
    return number * factor / target_yuan


def _merge_rows(base: list[dict], extra: list[dict]) -> list[dict]:
    """按日期合并两次 EDB 响应，同一天同一列取「先解析出非空值」的那个。

    丢的列每次未必相同，所以合并比二选一稳。
    """
    merged: dict[date, dict] = {}
    for row in base:
        day = _day(row.get("日期"))
        if day is not None:
            merged[day] = dict(row)
    for row in extra:
        day = _day(row.get("日期"))
        if day is None:
            continue
        target = merged.setdefault(day, {})
        for key, value in row.items():
            if to_float(target.get(key)) is None:
                target[key] = value
    return list(merged.values())


def _edb_chunk(
    ifind: IfindClient, indicators: list[str], start: date, end: date
) -> list[dict]:
    """取一段区间的 EDB 数据，整列缺失时重问一次。

    **必须校验整列是否存在。** 自然语言接口会偶发把某个指标整列丢掉 ——
    实测同一段区间（2026-07-10 ~ 09-18），云端那次没给「深交所:融资买入额」、
    本地那次给了，50 个交易日因此变成空白，而且**不报任何错**。
    判据用「整段区间一行都没有这个指标的值」：这几个市场级指标每天都在更新，
    90 天内一次都没有值，只可能是整列没回来。
    """
    query = (
        f"{'、'.join(indicators)} 的日度数据，"
        f"时间范围 {start.isoformat()} 至 {end.isoformat()}"
    )
    _, rows = ifind.edb_data(query)
    missing = [
        name for name in indicators if not any(_present(row, name) for row in rows)
    ]
    if not missing:
        return rows
    # 只补问缺的那几个指标，而**不是把原问句重发一遍**：一次问 6 个指标时
    # 匹配器会漏掉其中一个，单独问它则基本不会漏。
    logger.warning("EDB %s ~ %s 整列缺失 %s，单独补问", start, end, missing)
    _, alone = ifind.edb_data(
        f"{'、'.join(missing)} 的日度数据，时间范围 {start.isoformat()} 至 {end.isoformat()}"
    )
    return _merge_rows(rows, alone)


def _edb_rows(ifind: IfindClient, indicators: list[str], end: date, days: int) -> list[dict]:
    """分段取 EDB 日度数据并拼接。两个理由见 `EDB_CHUNK_DAYS` 与 `_edb_chunk`。

    返回的行是「一天一行、多个指标各占一列」的宽表，不是「一天一个指标一行」。
    """
    rows: list[dict] = []
    cursor = end - timedelta(days=days)
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=EDB_CHUNK_DAYS - 1), end)
        rows.extend(_edb_chunk(ifind, indicators, cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return rows


def collect_margin(ifind: IfindClient, end: date, days: int = EDB_LOOKBACK_DAYS) -> int:
    """两融（融资余额 / 融资买入额 / 融券余额，沪 + 深）。"""
    indicators = [name for names in MARGIN_INDICATORS.values() for name in names]
    rows = _edb_rows(ifind, indicators, end, days)

    records: list[dict] = []
    for row in rows:
        day = _day(row.get("日期"))
        if day is None:
            continue
        for market, (balance, buy, securities) in MARGIN_INDICATORS.items():
            values = (
                _value(row, balance, target_yuan=_YUAN_PER_YI),
                _value(row, buy, target_yuan=_YUAN_PER_YI),
                _value(row, securities, target_yuan=_YUAN_PER_YI),
            )
            if all(value is None for value in values):
                # 该所当天还没披露（实测深市比沪市晚一天）。**整行跳过，不写 0** ——
                # 写 0 会让「今天两融增加多少」在深市数据到达前后给出两个矛盾的答案
                continue
            records.append(
                {
                    "trade_date": day,
                    "market": market,
                    "financing_balance": values[0],
                    "financing_buy": values[1],
                    "securities_balance": values[2],
                }
            )
    if not records:
        logger.warning("两融取到 0 行（%s 往前 %d 天）", end, days)
        return 0
    with session_scope() as session:
        return upsert_fill(session, MarginDaily, records)


def collect_hsgt(ifind: IfindClient, end: date, days: int = EDB_LOOKBACK_DAYS) -> int:
    """沪深股通（北向）成交金额。**只有总额，没有净流入**（见 models.HsgtDaily）。"""
    rows = _edb_rows(ifind, list(HSGT_INDICATORS.values()), end, days)

    records: list[dict] = []
    for row in rows:
        day = _day(row.get("日期"))
        if day is None:
            continue
        for channel, name in HSGT_INDICATORS.items():
            value = _value(row, name, target_yuan=_YUAN_PER_MILLION)
            if value is None:
                continue
            records.append({"trade_date": day, "channel": channel, "turnover": value})
    if not records:
        logger.warning("沪深股通成交额取到 0 行（%s 往前 %d 天）", end, days)
        return 0
    with session_scope() as session:
        return upsert_fill(session, HsgtDaily, records)


def _batched(items: list[str], size: int) -> list[list[str]]:
    """把代码切成固定大小的批。"""
    return [items[index : index + size] for index in range(0, len(items), size)]


def _snippet(text: str | None, limit: int = 60) -> str:
    """把来源的回答压成一行短摘要（日志用）。"""
    return " ".join((text or "").split())[:limit]


def _fetch_etf_quotes(
    ifind: IfindClient, codes: list[str]
) -> tuple[dict[str, dict], int]:
    """分批取 ETF 的份额与行情，返回（`{6 位代码: 原始行}`, 调用次数）。

    批内一条都没回来时**先看来源说了什么**，再决定要不要拆半：

    - 命中 `_TOO_MANY_HINTS`（来源在说「这批问太多只了」）→ 拆半重试，那是真的超上限；
    - 没那句话 → 这批基金来源本来就没有数据（新上市 / 已清算 / 当天没成交），
      再拆下去只是把这一批一只只问一遍，纯白花钱。**日志里必须留下 `answer`**：
      2026-10-14 那次每批都空、却一行原因都没留，只能靠回溯代码才认定是超上限。

    ⚠️ 2026-10-14 之前是**无条件拆半**，而当时的 `ETF_BATCH = 80` 本身就已超上限，
    于是每一批都必然走 80→40→20 三次拆分 —— 上限问题被拆半逻辑「兜住了」，
    一天烧 154 次调用，谁也没发现。
    """
    found: dict[str, dict] = {}
    queue = _batched(codes, ETF_BATCH)
    calls = 0
    while queue:
        batch = queue.pop(0)
        answer, rows = ifind.fund_profile("、".join(batch) + f" 的 {ETF_METRICS}")
        calls += 1
        if rows:
            for row in rows:
                code = normalize_code(pick_text(row, "证券代码") or "")
                if code:
                    found[code] = row
            continue
        too_many = any(hint in (answer or "") for hint in _TOO_MANY_HINTS)
        # 「配额耗尽」优先判：它和「超上限」都不带数据，但处理方式相反 —— 超上限要
        # 拆半重试，配额耗尽再拆只是把剩下几十次调用窗口空转完。抛出去让 `_step`
        # 记 `failed`（而不是 ok+0 行），缺口才会在数据管理页露出来。
        # ⚠️ 也**不写已取到的那部分**：半天快照会被净申赎面板当成整日，
        # 算出一整天的假净流入（见 `collect_etf` 写入侧的覆盖率守卫，同一道理）。
        if any(hint in (answer or "") for hint in _QUOTA_EXHAUSTED_HINTS):
            raise RuntimeError(f"iFinD 配额已耗尽，ETF 采集中止：{_snippet(answer)}")
        if too_many and len(batch) > 1:
            half = len(batch) // 2
            logger.warning(
                "iFinD 基金批次 %d 只被来源拒绝，拆成 %d+%d 重试：%s",
                len(batch),
                half,
                len(batch) - half,
                _snippet(answer),
            )
            queue[0:0] = [batch[:half], batch[half:]]
            continue
        logger.warning(
            "iFinD 基金批次 %d 只没回数据（%s）：%s",
            len(batch),
            "已拆到单只仍被拒" if too_many else "来源没说是超上限，按无数据跳过",
            _snippet(answer) or "(空回答)",
        )
    return found, calls


# 交易所快照的完整性下限。正常一份是 1680 只左右（沪 920 + 深 760）；
# 单所只有 900 / 760，取 1000 能把「只拿到一所」挡在外面。
ETF_EXCHANGE_MIN_ROWS = 1000

# 已有快照里收盘价的覆盖率到这么多就不再重取。17:30 与 19:30 两趟都会调本函数，
# 用这个比例避免第二趟把两所几十页再翻一遍。行情没齐时（盘后包还没发布）会低于它，
# 下一趟自然会重试。
ETF_PRICED_RATIO = 0.95

# 交易所源的异常类型直接透给 `collect_daily`（它据此把步骤记成 skipped）
EtfNotReady = etf_exchange.EtfNotReady


def _etf_snapshot_complete(session, day: date) -> tuple[int, bool]:
    """该日快照的（行数, 是否完整）。完整 = 行数达标且收盘价覆盖 ≥ `ETF_PRICED_RATIO`。"""
    total = (
        session.scalar(
            select(func.count())
            .select_from(EtfShare)
            .where(EtfShare.trade_date == day)
        )
        or 0
    )
    priced = (
        session.scalar(
            select(func.count())
            .select_from(EtfShare)
            .where(EtfShare.trade_date == day, EtfShare.close.is_not(None))
        )
        or 0
    )
    return total, total >= ETF_EXCHANGE_MIN_ROWS and priced >= total * ETF_PRICED_RATIO


def _etf_quotes(day: date) -> dict[str, dict]:
    """从通达信盘后包取 ETF 的收盘价 / 涨跌幅 / 成交额。

    取不到（当天包还没发布 / 下载失败）时返回空 dict，**不抛** —— 份额本身是
    官方数据，比行情重要，不能因为行情缺就让整批不写。行情由下一趟或
    `_heal_etf_quotes` 补。
    """
    try:
        package = tdx.daily_package(day)
    except tdx.TdxUnavailable as exc:
        logger.info("ETF 行情暂缺（%s），本轮只写份额", exc)
        return {}
    except tdx.TdxError:
        logger.exception("通达信盘后包解析失败，本轮只写份额")
        return {}

    quotes: dict[str, dict] = {}
    for row in package:
        code = row["code"]
        # 只收场内 ETF 的代码段：沪 5 开头、深 1 开头。包里还有指数 / 债券 / 个股，
        # 但下面按 ETF 代码查表，多收进来也无害；这里过滤只是少几个键。
        if not (
            (row["market"] == "sh" and code.startswith("5"))
            or (row["market"] == "sz" and code.startswith("1"))
        ):
            continue
        prev = row["prev_close"]
        pct = (row["close"] / prev - 1) * 100 if prev and prev > 0 else None
        quotes[code] = {
            "close": row["close"],
            "pct_chg": round(pct, 2) if pct is not None else None,
            "amount": row["amount"],
        }
    return quotes


def _heal_etf_quotes(day: date, *, limit: int = 2) -> None:
    """给最近几天的快照补收盘价（盘后包比份额晚发布时用）。

    只查最近 5 个有快照的日期，挑「收盘价覆盖不足」的至多 `limit` 天去补。
    写入用 `upsert_fill`，所以只填空值，不会把已有的份额 / 名称改掉。
    """
    with session_scope() as session:
        recent = list(
            session.scalars(
                select(EtfShare.trade_date)
                .where(EtfShare.trade_date <= day)
                .group_by(EtfShare.trade_date)
                .order_by(EtfShare.trade_date.desc())
                .limit(5)
            )
        )
        need = [
            recent_day
            for recent_day in recent
            if not _etf_snapshot_complete(session, recent_day)[1]
        ]
        codes_by_day = {
            recent_day: list(
                session.scalars(
                    select(EtfShare.code).where(EtfShare.trade_date == recent_day)
                )
            )
            for recent_day in need[:limit]
        }

    for recent_day, codes in codes_by_day.items():
        quotes = _etf_quotes(recent_day)
        if not quotes:
            continue
        updates = [
            {"trade_date": recent_day, "code": code, **quotes[code]}
            for code in codes
            if code in quotes
        ]
        if not updates:
            continue
        with session_scope() as session:
            filled = upsert_fill(session, EtfShare, updates)
        logger.info("ETF 行情补齐 %s：%d 行", recent_day, filled)


def _collect_etf_exchange(target: date) -> int:
    """交易所官方份额 + 通达信盘后包行情（**零 iFinD 配额**）。"""
    # 先用深交所第一页确认「当前快照是哪一天」：如果库里这一天已经完整，就不必
    # 再把 38 页翻一遍（17:30 / 19:30 两趟都会调本函数）。
    snap_day, _pages, _total = etf_exchange.szse_snapshot()
    if snap_day > target:
        raise etf_exchange.EtfSourceError(f"深交所快照日期 {snap_day} 晚于目标日 {target}")
    with session_scope() as session:
        total, complete = _etf_snapshot_complete(session, snap_day)
    if complete:
        logger.info("ETF %s 的份额与行情已完整（%d 只），跳过", snap_day, total)
        _heal_etf_quotes(snap_day)
        return total

    rows = etf_exchange.fetch_shares(target)
    day = rows[0]["trade_date"]

    if len(rows) < ETF_EXCHANGE_MIN_ROWS:
        raise etf_exchange.EtfSourceError(
            f"交易所 ETF 快照只有 {len(rows)} 只（下限 {ETF_EXCHANGE_MIN_ROWS}），不写半份快照"
        )

    quotes = _etf_quotes(day)
    records: list[dict] = []
    categories: list[dict] = []
    for row in rows:
        quote = quotes.get(row["code"], {})
        categories.append(
            {"code": row["code"], "name": row["name"], "category": classify(row["name"])}
        )
        records.append(
            {
                "trade_date": day,
                "code": row["code"],
                "name": row["name"],
                "shares": row["shares"],
                "close": quote.get("close"),
                "pct_chg": quote.get("pct_chg"),
                "amount": quote.get("amount"),
            }
        )

    with session_scope() as session:
        upsert(session, EtfCategory, categories)
        written = upsert_fill(session, EtfShare, records)

    priced = sum(1 for record in records if record["close"] is not None)
    logger.info(
        "ETF（交易所）：%s 快照 %d 只 → 入库 %d 行；行情 %d 只",
        day,
        len(rows),
        written,
        priced,
    )
    if priced < len(records) * ETF_PRICED_RATIO:
        logger.warning(
            "ETF 行情只覆盖 %d/%d 只：通达信盘后包可能还没发布，下一趟 / 次日会补",
            priced,
            len(records),
        )
    _heal_etf_quotes(day)
    return written


def collect_etf(
    ak: AkshareSource, ifind: IfindClient, trade_date: date | None = None
) -> int:
    """ETF 份额与行情。**2026-10-18 起换成交易所官方 + 通达信盘后包，零 iFinD 配额。**

    分工：份额来自上交所（按日归档）+ 深交所（当前快照），行情（收盘价 / 涨跌幅 /
    成交额）来自通达信盘后包 —— 后者的数值与本站 iFinD 口径逐字段对拍一致
    （见 `sources/tdx` 顶部）。两所必须**同一天**取齐，理由见 `sources/etf_exchange`。

    降级顺序：
    1. 交易所**还没发布**（`EtfNotReady`）→ 原样抛给 `collect_daily`，步骤记 `skipped`，
       等 19:30 那趟或次日再取，**不退回 iFinD**（否则白烧 59 次调用）；
    2. 交易所源**坏了**（网络 / 结构变化）→ 退回 `_collect_etf_ifind`，保证数据不断。

    `trade_date` 只用来判断「深交所快照是不是早于目标日」，落库日期始终取快照自己的
    日期 —— 深交所只有当前快照，按请求日期写会把昨天的份额标成今天。
    """
    target = trade_date or date.today()
    try:
        return _collect_etf_exchange(target)
    except etf_exchange.EtfNotReady:
        raise
    except Exception:  # noqa: BLE001 - 交易所源真坏了才退回 iFinD
        logger.exception("交易所 ETF 源失败，回退 iFinD（约 59 次调用）")
        return _collect_etf_ifind(ak, ifind)


def _collect_etf_ifind(ak: AkshareSource, ifind: IfindClient) -> int:
    """**旧路径（iFinD）**：ETF 份额与行情。

    **为什么换源（2026-09-22）**：原来用 akshare 的东财 ETF 快照
    （`fund_etf_spot_em` → `push2delay.eastmoney.com`），而那一系从**本机与云端
    都连不上**（实测 HTTP 000、0.05 秒即断；同域名的 `push2ex` 正常）——
    于是这一项在云端一直**静默失效**：`collect_log` 里只有一行 `[failed]`，
    页面上只表现为 ETF 面板空白。换源之后不再依赖东财。

    分工的理由：**同花顺**能一次给全市场代码清单与「最新-交易日」（1725 行、
    零配额），但它是净值口径、**没有份额**；**iFinD** 有份额与行情，却一次只能
    问 30 只（`ETF_BATCH`，实测上限）、列不全 1745 只。所以清单与日期用同花顺，
    份额/收盘价/涨跌幅/成交额按批问 iFinD（30 只一批，约 59 次调用）。

    **落库日期取同花顺自带的「最新-交易日」，不接受调用方指定。** ETF 份额只有
    当日快照，而这份快照是哪一天的数据由行情源决定：盘前、凌晨、周末、节假日
    取到的都是**上一个交易日**的收盘快照，份额本身也常在 T+1 才更新。
    若按「今天」落库，凌晨采集就会把昨天的份额写到今天，凭空造出一个
    净申赎为 0 的假数据点，更要命的是后一天与它对比时会**把两天算成一天**。
    （实测踩过：09-21 凌晨采集，1621 行被写在 09-21 上，而份额是 09-18 的。）

    顺带刷新 `etf_category`：分类来自名称关键词（见 services/etf_category），
    每次采集都重算一遍，词典改了第二天自然生效。
    """
    meta: dict[str, dict] = {}
    for row in ak.etf_list_ths():
        code = normalize_code(pick_text(row, "基金代码") or "")
        if not code:
            continue
        meta[code] = {
            "name": pick_text(row, "基金名称"),
            "date": _day(pick_text(row, "最新-交易日")),
        }
    if not meta:
        logger.warning("同花顺 ETF 列表为空，本次不写份额")
        return 0

    quotes, calls = _fetch_etf_quotes(ifind, list(meta))

    # 落库日期必须落在交易日历里（见下方 `off_calendar`）。日历表在本链路第一步
    # 就采过（`collect_daily.run` 的 `calendar`），所以这里一定拿得到；
    # 拿不到（手工单独调用）时跳过这道校验，宁可少一道也不把整批数据丢光。
    with session_scope() as session:
        calendar = set(session.scalars(select(TradeCalendar.trade_date)))
    if not calendar:
        logger.warning("交易日历为空，本次跳过「落库日期须是交易日」的校验")

    records: list[dict] = []
    categories: list[dict] = []
    unknown_date = 0
    missing_quote = 0
    off_calendar = 0
    for code, info in meta.items():
        # 分类与日期、行情都无关，先写好 —— 别让缺数据连带把分类一起丢掉
        categories.append(
            {"code": code, "name": info["name"], "category": classify(info["name"])}
        )

        raw = quotes.get(code)
        if raw is None:
            # 份额与行情是**同一个来源**，它没回话说明这一只什么都拿不到：
            # 写一条全空的行只是噪音。（旧实现是「份额缺失也不跳过」，那是因为
            # 份额与行情分属两个来源、行情本身单独有用；同源之后这条不成立。）
            missing_quote += 1
            continue

        day = info["date"]
        if day is None:
            unknown_date += 1
            continue
        if calendar and day not in calendar:
            # 非交易日的快照一律不写（2026-10-16 修）：**货币基金的净值按自然日
            # 公布**，周末/假期取回同花顺的「最新-交易日」就是当天（实测 09-27 周日、
            # 10-07 国庆、10-11 周日各 14 行、全是货基）。这类行既不是本表口径
            # （本表是「当日全市场快照」），又会把「上一交易日 → 当天」的配对
            # 掐断 —— 份额表按日期配对算净申赎，中间插一个 14 行的假日期，
            # 下一个正常日的基准日就变成它，能配上的只剩那 14 只。
            off_calendar += 1
            continue
        records.append(
            {
                "trade_date": day,
                "code": code,
                "name": info["name"],
                # 能走到这里说明拿到了行，那么**少一个字段也不该丢掉整行** ——
                # 少了份额算不出申赎，但涨跌幅/成交额仍然有用
                "shares": pick_float(raw, "份额"),
                "close": pick_float(raw, "收盘价"),
                "pct_chg": pick_float(raw, "涨跌幅"),
                "amount": pick_float(raw, "成交额"),
            }
        )
    if unknown_date:
        logger.warning("ETF 列表有 %d 只没有「最新-交易日」，已跳过（不猜日期）", unknown_date)
    if missing_quote:
        logger.warning("iFinD 没回 %d 只 ETF 的份额与行情，已跳过", missing_quote)
    if off_calendar:
        logger.warning(
            "ETF 有 %d 只的「最新-交易日」不在交易日历里，已跳过（非交易日的净值快照）",
            off_calendar,
        )
    if not records:
        # 宁可不写也不写错日期：猜一个日期出来就会污染份额序列
        logger.warning("ETF 没取到任何带数据日期的行，本次不写份额")
        return 0

    # 覆盖率守卫（见 `ETF_MIN_WRITE_RATIO`）：整批太薄就整批不写。
    if len(records) < len(meta) * ETF_MIN_WRITE_RATIO:
        logger.warning(
            "ETF 本次只取到 %d/%d 只（不足 %.0f%%），整批不写 —— 半份快照会让下一次"
            "配对只算到这一半的代码，得出一个偏小但看起来正常的净申赎",
            len(records),
            len(meta),
            ETF_MIN_WRITE_RATIO * 100,
        )
        return 0

    # 同一次采集里**跨日期**是正常的：跨境/QDII 基金的同花顺「最新-交易日」常年
    # 比境内 ETF 晚一个交易日（实测 10-12 那 45 行与 10-13 的 1657 行**零交集**）。
    # 各自按自己的日期落库，不在本地改齐 —— 改齐等于把 QDII 的份额挪到它还
    # 没公布的那天，是编数据。读取端另有覆盖判据识别这种碎片日（见
    # `services/etf_flow.usable_days`）。
    spread = Counter(row["trade_date"] for row in records)
    if len(spread) > 1:
        main, main_count = spread.most_common(1)[0]
        logger.info(
            "ETF 本次写入跨 %d 个日期（主日期 %s %d 行；另 %s）—— 跨境/QDII 的"
            "「最新-交易日」本就滞后一天",
            len(spread),
            main,
            main_count,
            ", ".join(
                f"{day} {count} 行" for day, count in spread.most_common()[1:]
            ),
        )

    with session_scope() as session:
        # 分类用普通 upsert：它必须能被新词典**覆盖**（upsert_fill 只在
        # 新值为空时保留旧值，而分类永不为空，所以两者在这里等价，写 upsert 更直白）
        upsert(session, EtfCategory, categories)
        written = upsert_fill(session, EtfShare, records)
    logger.info(
        "ETF：清单 %d 只 → 入库 %d 行（%d 次 iFinD 调用）", len(meta), written, calls
    )
    return written


def collect_lhb_institutions(
    ak: AkshareSource, end: date, days: int = 0
) -> int:
    """龙虎榜机构席位统计（只含机构专用席位，全市场一次返回）。

    `days > 0` 时补 `[end - days, end]` 这一段 —— 接口原生支持区间，实测整年
    一次调用 22 秒，所以**补历史并不比补一天贵**，没必要逐日循环。
    `days == 0`（默认）就是单日，当日采集走这条。

    **按每行自带的「上榜日期」分日期写**，而不是按请求日期 —— 区间模式下后者
    只有一天，会把 250 天的数据全压到同一天上。

    **没有「哪家游资营业部买了哪只票」** —— 逐股营业部明细接口已下线，
    东财只保留机构口径的汇总，所以本表能回答「机构在买什么」，
    回答不了「章盟主今天买了什么」。
    """
    start = end - timedelta(days=days) if days > 0 else end
    rows = ak.lhb_institutions_range(start, end)

    # 用 (日期, 代码, 原因) 去重再写：这三列是主键，而同一条在区间返回里可能
    # 重复出现 —— 一条 INSERT 里带重复主键在 SQLite 上行为未定义
    # （Postgres 直接报 cannot affect row a second time）。
    records: dict[tuple, dict] = {}
    unplaced = 0
    for row in rows:
        day = _day(row.get("上榜日期"))
        code = str(row.get("代码") or "").strip()
        if day is None or not (start <= day <= end) or not code:
            unplaced += 1
            continue
        reason = str(row.get("上榜原因") or "未知").strip() or "未知"
        records[(day, code.zfill(6), reason)] = {
            "trade_date": day,
            "code": code.zfill(6),
            "name": row.get("名称"),
            "close": to_float(row.get("收盘价")),
            "pct_chg": to_float(row.get("涨跌幅")),
            "buy_count": to_int(row.get("买方机构数")),
            "sell_count": to_int(row.get("卖方机构数")),
            "buy_amount": to_float(row.get("机构买入总额")),
            "sell_amount": to_float(row.get("机构卖出总额")),
            "net_amount": to_float(row.get("机构买入净额")),
            "reason": reason,
        }
    if not records:
        if rows:
            # 有行却一行都没落库：多半是上游改了日期列的名字/格式。
            # 这种情况**必须出声** —— 否则采集日志上只是「0 行」，
            # 看着像「这些天没有机构上榜」，实际是取数坏了（8.22.4 的教训）。
            logger.warning(
                "机构席位 %s~%s 取回 %d 行却一行都没落库（日期列变了吗？）",
                start,
                end,
                len(rows),
            )
        # 这些天没有机构上榜是正常情况，不是错误 —— 不写日志、不报失败
        return 0
    if unplaced:
        logger.warning("机构席位 %s~%s 有 %d 行日期缺失或越界，已跳过", start, end, unplaced)
    with session_scope() as session:
        return upsert_many(session, LhbInstitution, list(records.values()))
