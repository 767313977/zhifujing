"""用 iFinD 的选股接口回补涨停 / 跌停 / 炸板三池。

## 为什么需要

三池的正常来源是 akshare 的东财 push2ex（`stock_zt_pool_em` 等），而那三个接口
**只保留最近约 15 个交易日**：实测 09-01 有数据、08-28 起返回空表、08-20 直接抛错。
空表和「那天真的没有涨停」在调用方看来完全一样 —— 所以超过窗口的数据一旦漏采就
永久丢了，而且没有任何提示。这个模块就是那条「丢了以后能捞回来」的路。

回补走 iFinD 的**选股**接口：它按自然语言条件筛股，也认历史日期。实测
「2026年3月10日涨停的A股股票…」拿回 67 只，且字段与东财能对上 ——
连板数 = `连续涨停天数`、封板资金 = `涨停封单额`、首封/末封时间、炸板次数 =
`涨停开板次数`、跌停侧有 `跌停封单额` / `连续跌停天数` / `跌停开板次数`。

## 五个实测坑（改这个文件之前先读完）

1. **每个指标都要带日期限定**，否则返回的是**最新一天**的值；而且不带日期的指标
   还会被当成「当日条件」，把结果集整个毁掉：`…涨停的A股股票…的收盘价、…、涨停封单额`
   的 matched 从 67 掉到 2。
2. **行情类指标与涨停专属指标要分两组写，且每组前面都重写一遍日期**：
   `{D}的收盘价、涨跌幅、…、所属同花顺行业、{D}的涨停封单额、连续涨停天数`。
   第二组前面不带日期的话就会退回最新日期（即第 1 条）。
3. **一个不支持的指标会让整条查询静默返回 0 行**，不报错。实测 `所属东财行业`
   和单独的 `开板次数` 都会毁掉整句，而 `跌停开板次数` 是支持的。所以这里的指标
   清单一个字都不能随手改，改完必须用 `--verify` 重新对账。
4. **同一张回答里可能同时出现 `[目标日]` 与 `[最新日]` 两版同名列**（引擎把条件与
   取值各算了一遍）。按关键词裸匹配会取到最新那列，整列数据都是错的 ——
   取值一律走 `_pick(..., day)`，它同时按日期过滤。
5. **要验「不是查坏了」**：一条查询坏掉的表现就是 0 行，和「那天没有涨停」无法区分。
   所以每天采完会做一次自洽校验（涨停集合必须落在「触及涨停」集合里），
   0 行且 0 跌停 0 炸板的一律判为失败、不写库。
6. **「触及涨停」这条要问 首封时间 / 开板次数** —— 一开始我以为这是个坑：引擎会把
   「该指标非空」当成隐含条件，于是「触及涨停但从未封住」的票被整批剔掉
   （实测 2026-09-10 从 66 只掉到 61 只）。查了东财的数据才明白它**恰好等于东财
   的口径**：东财炸板池里每一行的「炸板次数」都 ≥ 1（17 天 407 行，分布 1~25，
   一个 0 都没有）—— 没封住过就谈不上开板，所以它的炸板 = 「封住过又打开」，
   不是「碰过涨停价」。问这两个指标等于让 iFinD 替我们做同一件事；
   **不问反而会多出 1~3 只/天**（实测四天共多 9 只）。

## 与东财那条路的口径差异

- **ST / *ST 全部剔除**：东财三池不收 ST（17 天 1623 行里一个都没有），
  iFinD 会老实地列出来（2026-09-15 跌停池就多 6 只，全是 ST）。不剔的话
  「涨停数 / 跌停数」在历史段与最近 15 天之间会有系统性偏差，曲线在交界处跳一下。
- `industry` 用的是**同花顺行业**（`所属同花顺行业`）：东财口径的行业问不出来
  （`所属东财行业` 会让整条查询返回 0 行）。同花顺给的是「房地产-房地产-住宅开发」
  这种三级路径，与东财的单级名称不同名，所以**历史段与最近 15 天的这一列不是同一套分类**。
- `涨速` 没有对应指标。
- 其余字段一一对应，时间统一成 `HH:MM:SS`（东财也是这个格式）。

## 实测对账（2026-09-10 / 15 / 17 / 18 四天、逐只比）

| 池 | 结果 |
| --- | --- |
| 涨停 up | 四天**逐只全等**（35/35、32/32、47/47、78/78） |
| 跌停 down | 四天**逐只全等**（11/11、27/27、1/1、0/0） |
| 炸板 broken | 91 只里对上 88 只（**-3.3%**） |

炸板那 3 只差在「东财算炸板、而 iFinD 那边首封时间/开板次数为空」的票上。
根因是**两家对涨停价的取整不一致**：实测 002631 德尔未来（前收 12.03，
12.03×1.1 = 13.233）iFinD 取 13.24、东财按 13.23 判 —— 差的就是那一位小数。
只有价格恰好落在半分位时才出现，所以每天 0~3 只，且只影响炸板这个计数
（涨停数、跌停数是逐只全等的）。

改动这个模块之后，必须重跑 `--verify` 对账 —— 上面每一条都是靠它才发现的。
"""

import logging
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.db import session_scope, upsert_many
from app.models import CollectLog, LimitPool, TradeCalendar
from app.sources.ifind import IfindClient, IfindError, from_ths_symbol
from app.sources.markdown_table import to_float, to_int

logger = logging.getLogger(__name__)

# iFinD 的时间戳是 epoch 毫秒。转时分秒固定按 +8，不依赖机器时区 ——
# 服务器设了 Asia/Shanghai，但本地开发机不一定，那种偏差很难发现
_BEIJING = timezone(timedelta(hours=8))

# 三池的公共行情列。顺序无所谓，但**每组的日期前缀不能省**（见模块说明第 2 条）
_COMMON = "收盘价、涨跌幅、换手率、成交额、总市值、流通市值、振幅、所属同花顺行业"
_UP_ONLY = "涨停封单额、首次涨停时间、最终涨停时间、涨停开板次数、连续涨停天数"
_DOWN_ONLY = "跌停封单额、连续跌停天数、跌停开板次数"
_TOUCH_ONLY = "涨停价、最高价"
_TOUCH_EXTRA = "首次涨停时间、涨停开板次数"

# 三池的采集日志任务名，与当日采集（collect_daily）用同一套 ——
# `collect_sentiment` 判定「某池当天真的 0 家」还是「取不到数」靠的就是它们
POOL_TASKS = {"up": "pool_up", "down": "pool_down", "broken": "pool_broken"}


def _cn(day: date) -> str:
    """选股问句只认 `2026年3月10日` 这种写法。"""
    return f"{day.year}年{day.month}月{day.day}日"


def queries(day: date) -> dict[str, str]:
    """一个交易日的三条查询：涨停 / 跌停 / 触及涨停。"""
    d = _cn(day)
    return {
        "up": f"{d}涨停的A股股票{d}的{_COMMON}、{d}的{_UP_ONLY}",
        "down": f"{d}跌停的A股股票{d}的{_COMMON}、{d}的{_DOWN_ONLY}",
        "touch": f"{d}最高价等于涨停价的A股股票{d}的{_COMMON}、{_TOUCH_ONLY}、{d}的{_TOUCH_EXTRA}",
    }


def _pick(record: dict, keyword: str, day: date | None = None) -> str | None:
    """按「关键词（+ 目标日期）」取值。

    iFinD 的列名形如 `收盘价:不复权[20260310]`。**必须带日期过滤**：同一张回答里
    可能同时给出 `[目标日]` 和 `[最新日]` 两版同名列，裸匹配关键词会取到最新那天
    （见模块说明第 4 条）。`day=None` 用于本来就没有日期后缀的列（如所属同花顺行业）。
    """
    stamp = day.strftime("%Y%m%d") if day else None
    for column, value in record.items():
        if keyword in column and (stamp is None or stamp in column):
            return value
    return None


def _text(record: dict, keyword: str, day: date | None = None) -> str | None:
    value = _pick(record, keyword, day)
    text = str(value).strip() if value is not None else ""
    return text or None


def _num(record: dict, keyword: str, day: date | None = None) -> float | None:
    return to_float(_pick(record, keyword, day))


def _int(record: dict, keyword: str, day: date | None = None) -> int | None:
    return to_int(_pick(record, keyword, day))


def _clock(record: dict, keyword: str, day: date) -> str | None:
    """epoch 毫秒 → `09:27:39`。转换失败返回 None（宁可空着也别写个假时间）。"""
    millis = _num(record, keyword, day)
    if millis is None:
        return None
    try:
        stamp = datetime.fromtimestamp(millis / 1000, _BEIJING)
    except (OverflowError, OSError, ValueError):
        return None
    return stamp.strftime("%H:%M:%S")


def _code(record: dict) -> str | None:
    raw = _text(record, "股票代码") or _text(record, "证券代码")
    if not raw:
        return None
    code = from_ths_symbol(raw)
    return code if len(code) == 6 else None


def _base(record: dict, day: date, pool_type: str) -> dict:
    """三池公共字段。列名取 `a股市值(不含限售股)` 当流通市值 —— 它是 iFinD 对
    「流通市值」问法的实际返回列（实测问「流通市值」就给这一列）。"""
    return {
        "trade_date": day,
        "code": _code(record),
        "pool_type": pool_type,
        # 代码/名称/行业这三列**没有日期后缀**，别给它们传 day，否则永远取不到
        "name": _text(record, "股票简称"),
        "pct_chg": _num(record, "涨跌幅", day),
        "price": _num(record, "收盘价", day),
        "amount": _num(record, "成交额", day),
        "float_mv": _num(record, "a股市值", day),
        "total_mv": _num(record, "总市值", day),
        "turnover": _num(record, "换手率", day),
        "industry": _text(record, "所属同花顺行业"),
        # 以下几个按池类型填，这里先占位，保证三池的列集合一致
        # （upsert_many 要求一批里的每一行列相同）
        "seal_amount": None,
        "first_seal_time": None,
        "last_seal_time": None,
        "open_times": None,
        "consecutive": None,
    }


def _is_st(record: dict) -> bool:
    """东财三池**不收 ST**（*ST 也算）。

    证据：库里 17 天共 1623 行东财数据里，名称含 ST 的有 **0 行**；而 iFinD 会
    老实地把 ST 涨停/跌停都列出来（2026-09-15 跌停池就多 6 只，全是 ST）。
    不剔掉的话，历史段与最近 15 天的「涨停数 / 跌停数」会有一档系统性偏差 ——
    情绪曲线在交界处会跳一下，而那种跳变最容易被当成「市场变了」。
    """
    return "ST" in (_text(record, "股票简称") or "")


def pool_rows(day: date, found: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """把三条查询的原始行整理成三池的 `LimitPool` 行。

    炸板 = **触及涨停 − 涨停**（按代码取差集），而不是「收盘价 < 涨停价」。
    后者看着等价，实际会在涨停价的取整边界上误判：实测 002631 德尔未来
    2026-09-18 收 13.23、前收 12.03（12.03×1.1=13.233），东财把它算**炸板**而
    iFinD 算它收在涨停价上 —— 差的就是 13.23 还是 13.24 那一位。
    改用差集就没这个问题：iFinD 的「涨停」标记与东财的涨停池实测完全一致
    （两天分别 32/32、78/78），拿它去减「触及涨停」得到的炸板自然也对得上。

    另外东财两池是**互斥**的（涨停池里也有开板 38 次的票，但两池没有同一天同一只），
    所以差集这个形式本身也与它的语义一致。
    """
    up: list[dict] = []
    for record in found["up"]:
        row = _base(record, day, "up")
        if not row["code"] or _is_st(record):
            continue
        up.append(
            row
            | {
                "seal_amount": _num(record, "涨停封单额", day),
                "first_seal_time": _clock(record, "首次涨停时间", day),
                "last_seal_time": _clock(record, "最终涨停时间", day),
                "open_times": _int(record, "涨停开板次数", day),
                "consecutive": _int(record, "连续涨停天数", day),
            }
        )

    down: list[dict] = []
    for record in found["down"]:
        row = _base(record, day, "down")
        if not row["code"] or _is_st(record):
            continue
        down.append(
            row
            | {
                "seal_amount": _num(record, "跌停封单额", day),
                "open_times": _int(record, "跌停开板次数", day),
                "consecutive": _int(record, "连续跌停天数", day),
            }
        )

    up_codes = {row["code"] for row in up}
    broken: list[dict] = []
    for record in found["touch"]:
        row = _base(record, day, "broken")
        if not row["code"] or row["code"] in up_codes or _is_st(record):
            continue
        broken.append(
            row
            | {
                "first_seal_time": _clock(record, "首次涨停时间", day),
                "open_times": _int(record, "涨停开板次数", day),
            }
        )
    return {"up": up, "down": down, "broken": broken}


def fetch_day(
    ifind: IfindClient, day: date
) -> tuple[dict[str, list[dict]], dict[str, int]]:
    """取一天的三池。返回 `(三池行, 各条查询返回的原始行数)`。

    原始行数是给自洽校验用的：它能区分「原始就 0 行（查询坏了）」和
    「有行但都被过滤掉了（列名对不上）」。
    """
    found: dict[str, list[dict]] = {}
    sizes: dict[str, int] = {}
    for name, query in queries(day).items():
        matched, records = ifind.search_stocks_full(query)
        if matched is None:
            raise IfindError(f"选股接口未返回 matched（{day} {name}），无法确认是否取全")
        if matched > len(records):
            raise IfindError(
                f"{day} {name}：matched={matched} 只取回 {len(records)} 行，结果集可能被截断"
            )
        found[name] = records
        sizes[name] = len(records)
    return pool_rows(day, found), sizes


def _sane(day: date, pools: dict[str, list[dict]], found_size: dict[str, int]) -> str | None:
    """自洽校验。返回一句话说明哪里不对，没问题就返回 None。

    三池全空在 A 股是不可能出现的（五千多只票，总会有涨停或跌停），
    所以全空只可能是**查询坏了**（见模块说明第 3 条）而不是「那天没数据」。
    """
    if not pools["up"] and not pools["down"] and not pools["broken"]:
        return (
            f"三池都是 0 行（原始返回 up={found_size.get('up')} / "
            f"down={found_size.get('down')} / touch={found_size.get('touch')}），"
            "多半是查询里的指标不被支持"
        )
    up_codes = {row["code"] for row in pools["up"]}
    touch_codes = {row["code"] for row in pools["broken"]} | up_codes
    if found_size.get("touch") and not touch_codes:
        return "触及涨停取回了行却一行都没用上，检查涨停价/最高价列名"
    logger.debug("%s 校验：涨停 %d 只、触及涨停 %d 只", day, len(up_codes), len(touch_codes))
    return None


def _log(day: date, task: str, status: str, rows: int, message: str, cost: float) -> None:
    with session_scope() as session:
        session.add(
            CollectLog(
                trade_date=day,
                task=task,
                status=status,
                rows=rows,
                message=message,
                cost_seconds=cost,
            )
        )


def has_rows(day: date) -> bool:
    """这一天库里有没有三池数据。有就跳过 —— 既省调用，也避免用 iFinD 覆盖东财的行
    （两者的 `industry` 不是同一套分类，覆盖会悄悄换掉口径）。"""
    with session_scope() as session:
        return bool(
            session.scalar(
                select(func.count()).select_from(LimitPool).where(LimitPool.trade_date == day)
            )
        )


def write_day(day: date, pools: dict[str, list[dict]]) -> int:
    """三池一次性写入（一个事务，避免出现「涨停写了、炸板没写」的半截天）。"""
    rows = pools["up"] + pools["down"] + pools["broken"]
    if not rows:
        return 0
    with session_scope() as session:
        return upsert_many(session, LimitPool, rows)


def trade_days(start: date, end: date) -> list[date]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date >= start, TradeCalendar.trade_date <= end)
                .order_by(TradeCalendar.trade_date)
            )
        )


def backfill(
    start: date,
    end: date,
    *,
    max_days: int = 0,
    settings: Settings | None = None,
) -> dict:
    """回补 `[start, end]` 里**库里还没有**的交易日。

    从旧到新走，`max_days` 卡「本轮补几天」（同 `collect_kline` 的分批语义：
    每轮往前推进一段，跑完报出还剩几天）。已入库的天直接跳过。
    """
    settings = settings or get_settings()
    ifind = IfindClient(settings)
    from app.jobs.collect_daily import DailyCollector  # 延迟导入，避免循环引用

    days = trade_days(start, end)
    pending = [day for day in days if not has_rows(day)]

    started = time.monotonic()
    written = 0
    done: list[str] = []
    failed: list[str] = []
    calls = 0
    for index, day in enumerate(pending):
        if max_days and len(done) >= max_days:
            logger.info("本轮补满 %d 天，窗口内还剩 %d 天待补", max_days, len(pending) - index)
            break
        try:
            pools, sizes = fetch_day(ifind, day)
        except Exception as exc:  # noqa: BLE001 - 单天失败不该拖垮整轮
            failed.append(f"{day} {type(exc).__name__}: {exc}")
            logger.warning("%s 回补失败：%s", day, exc)
            continue
        calls += 3
        problem = _sane(day, pools, sizes)
        if problem:
            failed.append(f"{day} {problem}")
            logger.warning("%s 回补结果不可信，已跳过：%s", day, problem)
            continue

        written += write_day(day, pools)
        for pool_type, task in POOL_TASKS.items():
            _log(
                day,
                task,
                "ok",
                len(pools[pool_type]),
                f"iFinD 选股回补（{len(pools[pool_type])} 只）",
                0.0,
            )
        # 情绪指标依赖三池的计数与日志状态，补完立刻重算 ——
        # history=True 不会去问涨跌家数/打板效应（那两个本来就补不了）
        DailyCollector(settings).collect_sentiment(day, history=True)
        done.append(day.isoformat())
        logger.info(
            "%s 回补完成：涨停 %d / 跌停 %d / 炸板 %d",
            day,
            len(pools["up"]),
            len(pools["down"]),
            len(pools["broken"]),
        )

    remaining = max(0, len(pending) - len(done))
    return {
        "status": "ok" if not failed else "partial",
        "days": len(days),
        "pending": len(pending),
        "done": done,
        "remaining": remaining,
        "calls": calls,
        "rows": written,
        "failed": failed,
        "cost_seconds": round(time.monotonic() - started, 2),
    }


def verify_day(day: date, settings: Settings | None = None) -> dict:
    """拿 iFinD 的结果与库里已有的**东财**数据对账（不写库）。

    这是确认「查询没写错、口径对得上」的唯一手段：字段清单、日期绑定、
    炸板推导任何一处出错，都会在这里露出来。**改动这个模块后必须重跑一次。**

    输出里「只有 iFinD」的名单带上名称，是有用的诊断线索：东财三池**不收 ST**
    （17 天 1623 行里一个 ST 都没有），而 iFinD 会给出它们 —— 一眼就能分别
    「口径差异」和「查询写错了」。
    """
    settings = settings or get_settings()
    pools, _ = fetch_day(IfindClient(settings), day)
    with session_scope() as session:
        existing: dict[str, set[str]] = {name: set() for name in POOL_TASKS}
        for code, pool_type in session.execute(
            select(LimitPool.code, LimitPool.pool_type).where(LimitPool.trade_date == day)
        ):
            existing.setdefault(pool_type, set()).add(code)

    report = {}
    for name in POOL_TASKS:
        mine = {row["code"]: row["name"] or "" for row in pools[name]}
        theirs = existing.get(name, set())
        extra = {code: name_ for code, name_ in mine.items() if code not in theirs}
        report[name] = {
            "ifind": len(mine),
            "东财": len(theirs),
            "两边都有": len(set(mine) & theirs),
            "只有 iFinD": [f"{code} {name_}" for code, name_ in sorted(extra.items())][:12],
            "只有 iFinD 里含 ST": sum(1 for name_ in extra.values() if "ST" in name_),
            "只有东财": sorted(theirs - set(mine))[:10],
        }
    return report
