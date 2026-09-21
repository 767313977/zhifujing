"""资金面数据采集：两融、沪深股通成交额、ETF 份额、龙虎榜机构席位。

为什么单独一个模块而不是并进 `collect_daily`：这四块都是「每日一个快照」的
市场级数据，彼此独立、失败互不影响；而且数据源分两类（iFinD EDB 与 akshare），
分开能让「哪块坏了」在采集日志里一眼看到。

配额成本：两融 1 次 + 北向 1 次 = **每天 2 次 iFinD 调用**。
EDB 按区间返回，但区间不能开太大（见 EDB_CHUNK_DAYS），
所以回补一年的历史是 3 段 × 2 项 = 6 次，仍然很便宜。
"""

import logging
from datetime import date, timedelta

from app.db import session_scope, upsert, upsert_fill, upsert_many
from app.models import EtfCategory, EtfShare, HsgtDaily, LhbInstitution, MarginDaily
from app.services.etf_category import classify
from app.sources.akshare_source import AkshareSource
from app.sources.ifind import IfindClient
from app.sources.markdown_table import to_float, to_int

logger = logging.getLogger(__name__)

# 每次采集回看的日历天数。覆盖周末 + 最长节假日（8 天）后仍有大量余量 ——
# 这样某天采集失败，下一次会自然把缺的补上，不必单独写回补逻辑。
EDB_LOOKBACK_DAYS = 30

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


def _day(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _value(row: dict, prefix: str) -> float | None:
    """按列名**前缀**取 EDB 的值。

    列名带单位后缀（`上交所:融资买入额（单位：亿元）`），而单位偶尔会变
    （亿元 / 万元 / 百万元）。写死全名会在单位调整后**静默取不到值** ——
    那种错很难发现，界面上只是空着。
    """
    for key, raw in row.items():
        if str(key).startswith(prefix):
            return to_float(raw)
    return None


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
        name for name in indicators if not any(_value(row, name) is not None for row in rows)
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
            values = (_value(row, balance), _value(row, buy), _value(row, securities))
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
            value = _value(row, name)
            if value is None:
                continue
            records.append({"trade_date": day, "channel": channel, "turnover": value})
    if not records:
        logger.warning("沪深股通成交额取到 0 行（%s 往前 %d 天）", end, days)
        return 0
    with session_scope() as session:
        return upsert_fill(session, HsgtDaily, records)


def collect_etf(ak: AkshareSource) -> int:
    """ETF 份额与行情快照。全市场约 1600 只，**不占 iFinD 配额**。

    **落库日期取数据源自带的「数据日期」，不接受调用方指定。** ETF 份额只有
    当日快照，而这份快照是哪一天的数据由行情源决定：盘前、凌晨、周末、节假日
    取到的都是**上一个交易日**的收盘快照，份额本身也常在 T+1 才更新。
    若按「今天」落库，凌晨采集就会把昨天的份额写到今天，凭空造出一个
    净申赎为 0 的假数据点，更要命的是后一天与它对比时会**把两天算成一天**。
    （实测踩过：09-21 凌晨采集，1621 行被写在 09-21 上，而份额是 09-18 的。）

    顺带刷新 `etf_category`：分类来自名称关键词（见 services/etf_category），
    每次采集都重算一遍，词典改了第二天自然生效。
    """
    rows = ak.etf_spot()
    records: list[dict] = []
    categories: list[dict] = []
    unknown_date = 0
    for row in rows:
        code = str(row.get("代码") or "").strip()
        if not code:
            continue
        code = code.zfill(6)
        name = row.get("名称")
        # 分类与日期无关，先写好 —— 数据日期缺失不该连带把分类一起丢掉
        categories.append({"code": code, "name": name, "category": classify(name)})

        day = _day(row.get("数据日期"))
        if day is None:
            unknown_date += 1
            continue
        records.append(
            {
                "trade_date": day,
                "code": code,
                "name": name,
                # 份额缺失也**不跳过**：行情部分（涨跌幅、成交额）本身也有用，
                # 少了份额只是算不出这只的申赎，不该连它的行情一起丢
                "shares": to_float(row.get("最新份额")),
                "close": to_float(row.get("最新价")),
                "pct_chg": to_float(row.get("涨跌幅")),
                "amount": to_float(row.get("成交额")),
            }
        )
    if unknown_date:
        logger.warning("ETF 快照有 %d 行没有「数据日期」，已跳过（不猜日期）", unknown_date)
    if not records:
        # 宁可不写也不写错日期：猜一个日期出来就会污染份额序列
        logger.warning("ETF 快照没取到任何带数据日期的行，本次不写份额")
        return 0
    with session_scope() as session:
        # 分类用普通 upsert：它必须能被新词典**覆盖**（upsert_fill 只在
        # 新值为空时保留旧值，而分类永不为空，所以两者在这里等价，写 upsert 更直白）
        upsert(session, EtfCategory, categories)
        return upsert_fill(session, EtfShare, records)


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
