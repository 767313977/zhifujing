"""全市场形态扫描：读日线 → 复权 → 逐形态判定 → 落 `pattern_hit`。

**这一步对「通用形态」不花 iFinD 配额** —— 形态全在本地算，实测 3032 只 0.7 秒。
辉宾对齐悟道时，会对**池外创业板候选**按需补近端日线（见 `_ensure_wudao_kline`），
那一小段才吃配额；候选已有 ≥22 根连续 K 线则 0 次调用。

## 为什么整段重算而不是增量

形态看的是「最近 N 根 K 线的形状」，而 N 最长 249。今天补上一根新 K 线，
昨天那些票的形态可能全都变了（也可能没变）。增量更新得知道「哪些票的形状
被新数据影响了」，而这个问题没有便宜的解。整段重算是 0.7 秒，不值得为它做增量。

## 为什么先删后插

同一天重复扫描必须幂等 —— 手工补扫、定时任务撞车都会发生。所以按
`trade_date` 整段替换，而不是 upsert：**形态是会消失的**。昨天命中「平台突破」
的票今天可能已经跌回平台里，upsert 会把旧命中永久留在表里，榜单越看越假。

## 致富（原辉宾）与悟道对齐

- **只扫同一小池**：东财强势/涨停/昨涨停 + 涨幅榜前 100（创业板）+ 悟道近端本地缓存，
  **不**对全市场流动性池里的创业板出 `wudao_*`。
- 池外缺 K 线时从悟道库 / 东财补近端（见 `_ensure_wudao_kline`）。
"""

import logging
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select

from app.config import Settings, get_settings
from app.db import session_scope, upsert_many
from app.jobs.collect_universe import load_codes
from app.models import CollectLog, PatternHit, StockBasic, StockDaily, TradeCalendar
from app.services.patterns import (
    MIN_SCORE,
    WUDAO_MIN_BARS,
    Bars,
    build_bars,
    compute_rs,
    evaluate,
)
from app.sources.ifind import IfindError

logger = logging.getLogger(__name__)

# 扫描时每只票取多少根 K 线。要够 249 日新高用（最长窗口 + 1），再留一点余量
SCAN_BARS = 260

# 采集日志里的一类任务名
PATTERN_TASK = "patterns"

# 致富形态 key；只对小池创业板落库，与悟道「默认只扫创业板」对齐
WUDAO_KEYS = frozenset({"wudao_sample", "wudao_start"})

# 与悟道 `/api/pattern/scan?limit=80&board=cyb` 同量级（UI 常用 50~80）
WUDAO_SCAN_LIMIT = 80

# 池外致富候选补多少交易日日线（日历日粗算 ×1.5 在 sync_stock 内）
_WUDAO_SYNC_DAYS = 60

# 悟道之路本地日线库
# __file__ = .../zhifujing/backend/app/jobs/scan_patterns.py → parents[4]=cursorzhb
_WUDAO_MARKET_DB = Path(__file__).resolve().parents[4] / "data" / "market.db"

# 与悟道 `_candidate_rows` 排序一致：涨幅 > 强势 > 昨涨停 > 本地 > 涨停
_WUDAO_SRC_PRIORITY = {"涨幅": 0, "强势": 1, "昨涨停": 2, "本地": 3, "涨停": 4}


class _NoBars(Exception):
    """这一级**没有这只票的数据** —— 与「这条源挂了」是两回事。

    退市股、还没上市的代码、以及腾讯那边根本没有的代码都属于这一类。实测（2026-09-24）
    300038 数知退、300060 都是这种：东财/腾讯都没有它们的近端日线，iFinD 却会给一行
    只有收盘价、其余全空的行。**「没有数据」不该记进熔断计数** —— 几个退市股就能凑够
    3 次、把一整条源整轮关掉，那才是真的亏。
    """


def is_chinext(code: str) -> bool:
    """创业板：300 / 301 / 302。"""
    c = str(code or "").strip().zfill(6)
    return c.startswith(("300", "301", "302"))


def already_scanned(trade_date: date) -> bool:
    """该交易日是否已经扫过。

    只用来在日志上留个记号并避免同一轮里重复打印，**不承担正确性职责** ——
    重扫是幂等的、只要 0.7 秒、还零配额，所以「多扫一次」没有代价。
    """
    return _scanned_marker(trade_date)


def _scanned_marker(trade_date: date) -> bool:
    with session_scope() as session:
        return bool(
            session.scalar(
                select(CollectLog.trade_date)
                .where(
                    CollectLog.trade_date == trade_date,
                    CollectLog.task == PATTERN_TASK,
                    CollectLog.status == "ok",
                )
                .limit(1)
            )
        )


def _record(
    trade_date: date, status: str, rows: int, message: str | None, cost: float | None = None
) -> None:
    with session_scope() as session:
        session.add(
            CollectLog(
                trade_date=trade_date,
                task=PATTERN_TASK,
                status=status,
                rows=rows,
                message=message,
                cost_seconds=cost,
            )
        )


def _load_bars(
    trade_date: date, codes: list[str] | set[str], *, label: str = "股票池"
) -> dict[str, list[dict]]:
    """取出最近 `SCAN_BARS` 个交易日内指定代码的日线，按代码分组。

    一定要显式给代码名单，不能把 `stock_daily` 里的票全拿来扫：库里还有一批
    **池外的涨停股**（见 8.22.4，每天只给它们补当天那一根）。它们的序列是断的 ——
    连着两天涨停才有两行相邻，其余日子是空的 —— 而形态引擎会把**相邻的行**
    当成**相邻的交易日**，等于喂进去一条带空洞的 K 线，正是这套引擎最怕的输入。

    辉宾池外候选必须先经 `_ensure_wudao_kline` 补成连续近端，再放进这份名单。
    """
    codes = list(codes)
    if not codes:
        raise IfindError(f"{label}为空，先建池或补候选")

    with session_scope() as session:
        dates = list(
            session.scalars(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date <= trade_date)
                .order_by(TradeCalendar.trade_date.desc())
                .limit(SCAN_BARS)
            )
        )
        if not dates:
            raise IfindError(f"{trade_date} 之前没有交易日历数据")
        start = min(dates)

        rows = session.execute(
            select(
                StockDaily.code,
                StockDaily.name,
                StockDaily.trade_date,
                StockDaily.open,
                StockDaily.high,
                StockDaily.low,
                StockDaily.close,
                StockDaily.volume,
                StockDaily.amount,
                StockDaily.pct_chg,
            )
            .where(
                StockDaily.trade_date >= start,
                StockDaily.trade_date <= trade_date,
                StockDaily.code.in_(codes),
            )
            .order_by(StockDaily.code, StockDaily.trade_date)
        ).all()

    grouped: dict[str, list[dict]] = defaultdict(list)
    for code, name, day, open_, high, low, close, volume, amount, pct in rows:
        # 涨跌幅为空的行在采集时就已经滤掉了，这里再挡一道：
        # 少了它 build_bars 会把停牌日当成 0% 涨跌，前复权序列直接失真
        if close is None or pct is None:
            continue
        grouped[code].append(
            {
                "date": day,
                "name": name,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
                "pct_chg": pct,
            }
        )
    return grouped


def _wudao_candidate_codes(trade_date: date) -> set[str]:
    """致富候选：与悟道 `_candidate_rows` + `[:limit]` 同口径。

    来源：强势 / 涨停 / 昨涨停 + 涨幅榜前 100（4.5%~20.5%）+ 悟道近端本地。
    按悟道优先级排序后只取前 `WUDAO_SCAN_LIMIT` 只创业板。
    """
    # code → 最高优先级来源（数字越小越优先）
    best: dict[str, int] = {}

    def add(code: str, source: str) -> None:
        c = str(code or "").strip().zfill(6)
        if not is_chinext(c):
            return
        pri = _WUDAO_SRC_PRIORITY.get(source, 9)
        prev = best.get(c)
        if prev is None or pri < prev:
            best[c] = pri

    ymd = trade_date.strftime("%Y%m%d")

    db = _WUDAO_MARKET_DB
    if db.is_file() and db.stat().st_size > 0:
        import sqlite3

        cutoff = (trade_date - timedelta(days=14)).isoformat()
        try:
            with sqlite3.connect(str(db)) as conn:
                for (code,) in conn.execute(
                    "SELECT DISTINCT code FROM kline WHERE trade_date >= ?",
                    (cutoff,),
                ):
                    add(code, "本地")
        except Exception as exc:  # noqa: BLE001
            logger.warning("读悟道本地候选失败：%s", exc)

    try:
        import akshare as ak
    except Exception as exc:  # noqa: BLE001
        logger.warning("akshare 不可用，致富候选只靠悟道本地库：%s", exc)
        ranked = sorted(best.items(), key=lambda kv: (kv[1], kv[0]))
        return {c for c, _ in ranked[:WUDAO_SCAN_LIMIT]}

    _clear_proxies()

    loaders = (
        ("强势", lambda: ak.stock_zt_pool_strong_em(date=ymd)),
        ("涨停", lambda: ak.stock_zt_pool_em(date=ymd)),
        ("昨涨停", lambda: ak.stock_zt_pool_previous_em(date=ymd)),
    )
    for label, fn in loaders:
        try:
            df = fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("拉东财%s池失败：%s", label, exc)
            continue
        if df is None or getattr(df, "empty", True):
            continue
        col = "代码" if "代码" in df.columns else None
        if not col:
            continue
        for raw in df[col].tolist():
            add(raw, label)

    try:
        spot = ak.stock_zh_a_spot_em()
        if (
            spot is not None
            and not spot.empty
            and "代码" in spot.columns
            and "涨跌幅" in spot.columns
        ):
            rows: list[tuple[str, float]] = []
            for rec in spot.to_dict("records"):
                code = str(rec.get("代码") or "").strip().zfill(6)
                if not is_chinext(code):
                    continue
                try:
                    pct = float(rec.get("涨跌幅") or 0)
                except (TypeError, ValueError):
                    continue
                if 4.5 <= pct <= 20.5:
                    rows.append((code, pct))
            rows.sort(key=lambda x: x[1], reverse=True)
            for code, _ in rows[:100]:
                add(code, "涨幅")
    except Exception as exc:  # noqa: BLE001
        logger.warning("拉东财涨幅榜失败：%s", exc)

    ranked = sorted(best.items(), key=lambda kv: (kv[1], kv[0]))
    return {c for c, _ in ranked[:WUDAO_SCAN_LIMIT]}


def _bar_counts(codes: set[str], trade_date: date, *, lookback: int = 40) -> dict[str, int]:
    """近端 lookback 个交易日内，各代码已有多少根日线。"""
    if not codes:
        return {}
    with session_scope() as session:
        dates = list(
            session.scalars(
                select(TradeCalendar.trade_date)
                .where(TradeCalendar.trade_date <= trade_date)
                .order_by(TradeCalendar.trade_date.desc())
                .limit(lookback)
            )
        )
        if not dates:
            return {}
        start = min(dates)
        rows = session.execute(
            select(StockDaily.code, func.count())
            .where(
                StockDaily.code.in_(codes),
                StockDaily.trade_date >= start,
                StockDaily.trade_date <= trade_date,
            )
            .group_by(StockDaily.code)
        ).all()
    return {str(code).zfill(6): int(n) for code, n in rows}


def _clear_proxies() -> None:
    import os

    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        os.environ.pop(key, None)


def _em_secid(code: str) -> str:
    c = str(code).zfill(6)
    if c.startswith(("5", "6", "9")):
        return f"1.{c}"
    return f"0.{c}"


def _sync_stock_eastmoney(code: str, *, days: int = _WUDAO_SYNC_DAYS) -> int:
    """池外致富候选的日线兜底：东财不复权 K 线 → `stock_daily`。

    iFinD 401 / 配额紧张时走这条。只写近端 `days` 个日历日，够量比窗口即可。
    OHLC 用不复权（fqt=0），与全市场采集口径一致，交给 `build_bars` 用涨跌幅复权。
    """
    import requests

    _clear_proxies()
    end = date.today()
    beg = (end - timedelta(days=int(days * 1.5) + 5)).strftime("%Y%m%d")
    end_s = end.strftime("%Y%m%d")
    last_err: Exception | None = None
    payload = None
    session = requests.Session()
    session.trust_env = False
    for _ in range(3):
        try:
            resp = session.get(
                "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                params={
                    "fields1": "f1,f2,f3,f4,f5,f6",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                    "ut": "fa5fd1943c7b386f172d6893dbfba10b",
                    "klt": "101",
                    "fqt": "0",
                    "secid": _em_secid(code),
                    "beg": beg,
                    "end": end_s,
                },
                timeout=20,
                proxies={"http": None, "https": None},
                headers={"Referer": "https://finance.eastmoney.com/", "User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            payload = resp.json()
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    if payload is None:
        raise RuntimeError(f"东财直连失败：{last_err}")

    data = payload.get("data") or {}
    name = (data.get("name") or code).strip()
    klines = data.get("klines") or []
    if not klines:
        raise _NoBars("东财无数据")

    rows: list[dict] = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 11:
            continue
        try:
            day = date.fromisoformat(parts[0][:10])
        except ValueError:
            continue
        vol_hands = float(parts[5] or 0)
        rows.append(
            {
                "trade_date": day,
                "code": str(code).zfill(6),
                "name": name,
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": vol_hands * 100.0,
                "amount": float(parts[6] or 0),
                "pct_chg": float(parts[8] or 0),
            }
        )
    if not rows:
        raise _NoBars("东财无数据")
    with session_scope() as session:
        return upsert_many(session, StockDaily, rows)


# 腾讯行情接口单次超时。单独给一个值：它挂在形态扫描的链条里，
# 卡住就等于整条链路卡住（akshare 默认不设超时，会一直等下去）
_TX_TIMEOUT = 15.0


def _tx_symbol(code: str) -> str:
    """腾讯行情的代码写法：沪市 `sh`、深市 `sz`。"""
    c = str(code).zfill(6)
    return ("sh" if c.startswith(("5", "6", "9")) else "sz") + c


def _sync_stock_tencent(code: str, *, days: int = _WUDAO_SYNC_DAYS) -> int:
    """池外候选补日线（第二级兜底）：腾讯 K 线 → `stock_daily`。**不占 iFinD 配额**。

    为什么加这一级：云端连不上东财直连（8.51.3），那一级必然失败，于是每天几十只候选
    全落到 iFinD 兜底（实测 2026-09-24：48 只 = 48 次调用）。腾讯这条线**云端可达**
    （2026-09-24 在服务器上实测过），把它插进链条就把那几十次配额全省下来。

    **口径对过账**（本机 + 云端各跑一遍 `scripts/probe_akshare_tx.py`）：腾讯「不复权」的
    开高低收 / 成交量(股) / 成交额(元) 与库里 iFinD 写的值**逐位相同**（成交量有 14 股的
    舍入差，腾讯是整百股）；唯一的单位差是 `turnover` —— 腾讯给**比例** 0.0015、
    本站库里是**百分数** 0.15，故 ×100。

    **涨跌幅必须由前复权序列算，不能用不复权收盘价比**：除权日不复权价会跳空，
    比出来是一个假的暴跌。腾讯的 `qfq` 相邻收盘之比才是真实收益率（与 iFinD 的
    `涨跌幅` 对账：48 行、差异 >0.02 的 0 行）。所以这里**拉两份** ——
    价格存不复权、`pct_chg` 由前复权推。
    """
    import akshare as ak
    import pandas as pd

    def _num(value: object) -> float | None:
        """pandas 的 NaN/NaT → None，其余转 float（腾讯的空字段给的是 NaN）。"""
        if value is None or pd.isna(value):
            return None
        return float(value)

    end = date.today()
    # 多留两周：前复权序列的**第一行**没有前值、算不出涨跌幅，那一行会被丢掉，
    # 多取的这段保证丢完之后仍在 `days` 个交易日以上
    start = end - timedelta(days=int(days * 1.5) + 14)
    symbol = _tx_symbol(code)

    def _fetch(adjust: str) -> pd.DataFrame:
        try:
            return ak.stock_zh_a_hist_tx(
                symbol=symbol,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust=adjust,
                timeout=_TX_TIMEOUT,
            )
        except (IndexError, KeyError) as exc:
            # ⚠️ 对「没有这只票」（退市 / 未上市），akshare 不是给空表，而是在解析里炸掉 ——
            # 实测 300060 报 `list index out of range`。那是**没有数据**，不是这条源挂了，
            # 不能记进熔断计数（见 `_NoBars`），否则几个退市股就能把腾讯整轮关掉。
            raise _NoBars(f"腾讯无 {code} 数据：{exc}") from exc

    plain = _fetch("")
    if plain is None or plain.empty:
        raise _NoBars(f"腾讯无 {code} 日线")
    qfq = _fetch("qfq")
    if qfq is None or qfq.empty:
        raise _NoBars(f"腾讯无 {code} 前复权日线")

    qfq_days = [str(v)[:10] for v in qfq["date"].tolist()]
    qfq_close = {day: _num(v) for day, v in zip(qfq_days, qfq["close"].tolist())}
    # 逐日的真实涨跌幅：**前复权比前复权**（拿不复权收盘当分子会在除权日算错）
    pct_by_day: dict[str, float] = {}
    for i, day in enumerate(qfq_days):
        if not i:
            continue
        cur = qfq_close.get(day)
        prev = qfq_close.get(qfq_days[i - 1])
        if cur is None or not prev:
            continue
        pct_by_day[day] = (cur / prev - 1) * 100

    with session_scope() as session:
        name = session.scalar(
            select(StockBasic.name).where(StockBasic.code == str(code).zfill(6))
        )

    rows: list[dict] = []
    for rec in plain.to_dict("records"):
        day = str(rec.get("date"))[:10]
        close = _num(rec.get("close"))
        pct = pct_by_day.get(day)
        if close is None or pct is None:
            continue  # 首行，或前复权缺这一行：算不出真实涨跌幅，丢掉
        turnover = _num(rec.get("turnover"))
        rows.append(
            {
                "trade_date": date.fromisoformat(day),
                "code": str(code).zfill(6),
                "name": name or str(code).zfill(6),
                "open": _num(rec.get("open")),
                "high": _num(rec.get("high")),
                "low": _num(rec.get("low")),
                "close": close,
                "volume": _num(rec.get("volume")) or 0.0,
                "amount": _num(rec.get("amount")) or 0.0,
                "turnover": turnover * 100 if turnover is not None else None,
                "pct_chg": pct,
            }
        )
    if not rows:
        raise _NoBars(f"腾讯 {code} 近端无效")
    with session_scope() as session:
        return upsert_many(session, StockDaily, rows)


def _sync_stock_from_wudao(code: str, *, days: int = _WUDAO_SYNC_DAYS) -> int:
    """从本机悟道之路 `data/market.db` 抄近端日线进致富经 `stock_daily`。"""
    import sqlite3

    db = _WUDAO_MARKET_DB
    if not db.is_file() or db.stat().st_size == 0:
        raise RuntimeError(f"悟道日线库不可用：{db}")
    cutoff = (date.today() - timedelta(days=int(days * 1.5) + 5)).isoformat()
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        name_row = conn.execute(
            "SELECT name FROM stocks WHERE code = ? LIMIT 1", (code,)
        ).fetchone()
        name = (name_row["name"] if name_row else None) or code
        src = conn.execute(
            """
            SELECT trade_date, open, high, low, close, volume, amount, pct_chg
            FROM kline
            WHERE code = ? AND trade_date >= ?
            ORDER BY trade_date
            """,
            (code, cutoff),
        ).fetchall()
    if not src:
        raise RuntimeError(f"悟道库无 {code} 近端日线")
    rows = []
    for r in src:
        if r["close"] is None or r["pct_chg"] is None:
            continue
        rows.append(
            {
                "trade_date": date.fromisoformat(str(r["trade_date"])[:10]),
                "code": str(code).zfill(6),
                "name": name,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"] or 0),
                "amount": float(r["amount"] or 0) if r["amount"] is not None else None,
                "pct_chg": float(r["pct_chg"]),
            }
        )
    if not rows:
        raise RuntimeError(f"悟道库 {code} 近端无效")
    with session_scope() as session:
        return upsert_many(session, StockDaily, rows)


def _sync_note(sync_info: dict) -> str:
    """采集日志里「补日线用了哪几级」那截说明；都没干活就不写。

    分来源列出来是为了**事后能看出哪一级在扛** —— 腾讯这一级的价值正是让 iFinD 归零，
    没有这个数就只能去翻 logger 才知道它到底生效没有。
    """
    parts = []
    if sync_info.get("via_wudao"):
        parts.append(f"悟道库 {sync_info['via_wudao']} 只")
    if sync_info.get("via_eastmoney"):
        parts.append(f"东财 {sync_info['via_eastmoney']} 只")
    if sync_info.get("via_tencent"):
        parts.append(f"腾讯 {sync_info['via_tencent']} 只")
    if sync_info.get("via_ifind"):
        parts.append(f"iFinD {sync_info['via_ifind']} 只")
    if sync_info.get("skipped_quota"):
        parts.append(f"因 iFinD 上限跳过 {sync_info['skipped_quota']} 只")
    return (" / 补日线 " + "、".join(parts)) if parts else ""


def _ensure_wudao_kline(
    codes: set[str], trade_date: date, settings: Settings | None = None
) -> dict:
    """池外辉宾候选补日线：悟道本地库 → 东财 → 腾讯 → iFinD。

    **为什么四级**：前两级是「免费又快」（本地库一份不多花；东财一次请求），第三级
    腾讯也是免费的，但没有历史库、要两次请求并自己推涨跌幅；iFinD 是唯一**吃配额**的
    一级，放在最后、且有上限。

    **三道闸门**（后两道 2026-09-24 加，为同时省时间与配额；阈值见 `Settings` 的注释）：

    - **东财熔断**：连续失败 `wudao_em_breaker_failures` 次就本轮不再试它。
      云端连不上东财直连（1.7 / 8.51.3 记过），而这里是**逐只**补 —— 不熔断的话每只都要
      等一次约 10 秒的超时。实测 09-24：48 只候选白等约 8 分钟，且最终全部落到 iFinD。
    - **腾讯熔断**：同一个道理 —— 这条线也有可能整轮不通（接口变动 / 该机器不可达），
      不熔断就是 48 只 × 两次请求 × 超时。
    - **iFinD 上限**：每轮最多补 `wudao_ifind_fallback_max` 只，超出的**本轮不补**
      （当天就没有这些候选的形态信号）。这是刻意的「配额 ↔ 覆盖」取舍，不静默：
      跳过的只数进 `skipped_quota`，日志与采集日志里都会写出来。
      **有了腾讯这一级，它其实很少再触发** —— 只有在腾讯也整轮不通时才轮得到它顶上来。
    """
    settings = settings or get_settings()
    counts = _bar_counts(codes, trade_date)
    need = {c for c in codes if counts.get(c, 0) < WUDAO_MIN_BARS}
    if not need:
        return {
            "synced": 0,
            "failed": 0,
            "needed": 0,
            "via_wudao": 0,
            "via_eastmoney": 0,
            "via_tencent": 0,
            "via_ifind": 0,
            "skipped_quota": 0,
        }

    from app.jobs.collect_daily import DailyCollector
    from app.sources.ifind import IfindError

    collector = DailyCollector()
    synced = 0
    failed = 0
    via_wudao = 0
    via_em = 0
    via_tencent = 0
    via_ifind = 0
    skipped_quota = 0
    skip_ifind = False
    em_failures = 0
    em_dead = False
    tx_failures = 0
    tx_dead = False
    for code in sorted(need):
        ok = False
        try:
            wrote = _sync_stock_from_wudao(code, days=_WUDAO_SYNC_DAYS)
            synced += 1
            via_wudao += 1
            ok = True
            logger.info("辉宾补日线(悟道库) %s → %d 行", code, wrote)
        except Exception as exc:  # noqa: BLE001
            logger.debug("辉宾补日线(悟道库) %s：%s", code, exc)
        if not ok and not em_dead:
            try:
                wrote = _sync_stock_eastmoney(code, days=_WUDAO_SYNC_DAYS)
                synced += 1
                via_em += 1
                ok = True
                logger.info("辉宾补日线(东财) %s → %d 行", code, wrote)
            except _NoBars as exc:
                # 这只票这一级没有 —— 换下一级，不算这条源挂了
                logger.info("辉宾补日线(东财) %s 无数据，换下一级：%s", code, exc)
            except Exception as exc:  # noqa: BLE001
                em_failures += 1
                if em_failures >= settings.wudao_em_breaker_failures:
                    # 熔断：后面几十只不再一只只等超时。只报一次，别刷屏
                    em_dead = True
                    logger.warning(
                        "辉宾补日线(东财) 连续失败 %d 次（最近一只 %s：%s），"
                        "本轮剩余候选不再试东财，改由腾讯 / iFinD 兜底",
                        em_failures,
                        code,
                        exc,
                    )
                else:
                    logger.warning("辉宾补日线(东财) %s 失败：%s", code, exc)
        if not ok and not tx_dead:
            try:
                wrote = _sync_stock_tencent(code, days=_WUDAO_SYNC_DAYS)
                synced += 1
                via_tencent += 1
                ok = True
                logger.info("辉宾补日线(腾讯) %s → %d 行", code, wrote)
            except _NoBars as exc:
                logger.info("辉宾补日线(腾讯) %s 无数据，换下一级：%s", code, exc)
            except Exception as exc:  # noqa: BLE001
                tx_failures += 1
                if tx_failures >= settings.wudao_tx_breaker_failures:
                    tx_dead = True
                    logger.warning(
                        "辉宾补日线(腾讯) 连续失败 %d 次（最近一只 %s：%s），"
                        "本轮剩余候选不再试腾讯，改由 iFinD 兜底（受上限约束）",
                        tx_failures,
                        code,
                        exc,
                    )
                else:
                    logger.warning("辉宾补日线(腾讯) %s 失败：%s", code, exc)
        if not ok and not skip_ifind:
            if via_ifind >= settings.wudao_ifind_fallback_max:
                # 到上限了：本轮不补它。不记 failed —— 这不是失败，是刻意的取舍
                skipped_quota += 1
                continue
            try:
                wrote = collector.sync_stock(code, days=_WUDAO_SYNC_DAYS)
                synced += 1
                via_ifind += 1
                ok = True
                logger.info("辉宾补日线(iFinD) %s → %d 行", code, wrote)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "401" in msg or isinstance(exc, IfindError):
                    skip_ifind = True
                    logger.warning("iFinD 不可用，本轮不再重试：%s", exc)
                else:
                    logger.warning("辉宾补日线(iFinD) %s 失败：%s", code, exc)
        if not ok:
            failed += 1
    if skipped_quota:
        logger.warning(
            "辉宾补日线：%d 只候选因 iFinD 兜底到上限（%d 只）本轮未补 —— "
            "它们当天没有形态信号；想让覆盖更全就调大 `WUDAO_IFIND_FALLBACK_MAX`",
            skipped_quota,
            settings.wudao_ifind_fallback_max,
        )
    return {
        "synced": synced,
        "failed": failed,
        "needed": len(need),
        "via_wudao": via_wudao,
        "via_eastmoney": via_em,
        "via_tencent": via_tencent,
        "via_ifind": via_ifind,
        "skipped_quota": skipped_quota,
    }


def scan(
    trade_date: date | None = None,
    settings: Settings | None = None,
    *,
    min_score: float = MIN_SCORE,
) -> dict:
    """扫描一个交易日的全市场形态，整段替换该日的命中记录。"""
    settings = settings or get_settings()
    started = time.monotonic()

    target = trade_date or _latest_trade_date()
    _require_bars(target)

    universe = load_codes()
    if not universe:
        raise IfindError("股票池为空，先建池（UniverseCollector.collect）")

    # 致富：只扫悟道同口径小池（强势/涨停/昨涨停/涨幅榜/本地），不对全流动性池出致富信号
    wudao_cands = _wudao_candidate_codes(target)
    extras = {c for c in wudao_cands if c not in set(universe)}
    sync_info = (
        _ensure_wudao_kline(wudao_cands, target, settings)
        if wudao_cands
        else {
            "synced": 0,
            "failed": 0,
            "needed": 0,
            "via_wudao": 0,
            "via_eastmoney": 0,
            "via_tencent": 0,
            "via_ifind": 0,
            "skipped_quota": 0,
        }
    )

    scan_codes = set(universe) | extras
    grouped = _load_bars(target, scan_codes)
    if not grouped:
        raise IfindError(f"{target} 没有日线数据，先跑 collect_kline")

    rows: list[dict] = []
    skipped = 0
    dropped_wudao = 0
    by_pattern: dict[str, int] = defaultdict(int)
    # 先把全市场的 K 线都建出来、再统一算 RS 评级 —— 它是**横截面排名**，
    # 单只票自己算不出来，必须等所有票都在手上（见 patterns.compute_rs）
    bars_map: dict[str, Bars] = {}
    for code, records in grouped.items():
        # 致富只要约 22 根；其它形态内部仍按各自 MIN_BARS 自行跳过
        if len(records) < WUDAO_MIN_BARS:
            skipped += 1
            continue
        bars_map[code] = build_bars(records)
    compute_rs(bars_map)

    for code, bars in bars_map.items():
        records = grouped[code]
        last = records[-1]
        for signal in evaluate(bars, min_score=min_score):
            # 致富只出小池创业板，与悟道名单对齐
            if signal.pattern in WUDAO_KEYS and (
                not is_chinext(code) or code not in wudao_cands
            ):
                dropped_wudao += 1
                continue
            by_pattern[signal.pattern] += 1
            rows.append(
                {
                    "trade_date": target,
                    "code": code,
                    "pattern": signal.pattern,
                    "name": last["name"],
                    "score": round(signal.score, 1),
                    "close": last["close"],
                    "pct_chg": last["pct_chg"],
                    "amount": last["amount"],
                    "key_levels": signal.key_levels,
                    "detail": signal.detail,
                }
            )

    with session_scope() as session:
        session.execute(delete(PatternHit).where(PatternHit.trade_date == target))
        written = upsert_many(session, PatternHit, rows)

    cost = round(time.monotonic() - started, 2)
    logger.info(
        "形态扫描完成：%s，%d 只票 → %d 条命中（跳过 %d / 致富剔池外 %d / 候选 %d / 补日线 %s），用时 %ss",
        target,
        len(grouped),
        written,
        skipped,
        dropped_wudao,
        len(wudao_cands),
        sync_info,
        cost,
    )
    _record(
        target,
        "ok",
        written,
        f"{len(grouped)} 只 / {written} 条命中 / 致富候选 {len(wudao_cands)}"
        + _sync_note(sync_info),
        cost,
    )
    return {
        "status": "ok",
        "trade_date": target.isoformat(),
        "codes": len(grouped),
        "skipped": skipped,
        "wudao_cands": len(wudao_cands),
        "wudao_extras": len(extras),
        "wudao_sync": sync_info,
        "wudao_dropped": dropped_wudao,
        "rows": written,
        "by_pattern": dict(by_pattern),
        "cost_seconds": cost,
    }


def _require_bars(trade_date: date) -> None:
    """确认库里真的有这一天的日线。

    少了这道校验，扫描会拿**昨天**的 K 线当今天用 —— 结果不是空的，而是「用
    昨天的数据打上今天的日期」，看起来完全正常。这种错误没有任何外部症状，
    只会在事后复盘时发现「那天的信号怎么是用前一天的价算的」。
    """
    with session_scope() as session:
        latest = session.scalar(select(func.max(StockDaily.trade_date)))
        count = (
            session.scalar(
                select(func.count())
                .select_from(StockDaily)
                .where(StockDaily.trade_date == trade_date)
            )
            or 0
        )
    if latest is None:
        raise IfindError("stock_daily 是空的，先跑 collect_kline")
    if latest < trade_date or count == 0:
        raise IfindError(f"{trade_date} 的日线还没采到（库里最新是 {latest}），先跑 collect_kline")


def _latest_trade_date() -> date:
    with session_scope() as session:
        found = session.scalar(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.trade_date <= date.today())
            .order_by(TradeCalendar.trade_date.desc())
            .limit(1)
        )
    if found is None:
        raise IfindError("交易日历为空，先采集交易日历")
    return found
