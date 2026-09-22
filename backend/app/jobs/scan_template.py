"""样板池扫描：读本地日线 → 判定样板日 → 落 `template_pool`（次日「明天盯」清单）。

**零 iFinD 调用**：全在本地日线上算，取数直接复用形态扫描的 `scan_patterns.load_bars`
（同源、同窗口、同样只取股票池），实测一两秒。所以这一步**不挂配额守卫** ——
配额紧张时该停的是日线采集与 DDE 扫描，不是这种不花钱的本地计算。

## 为什么与形态扫描分表、分任务

样板日是**两日序列的第一天**：判定要拿「昨天那根 K 线」当基准（昨收、昨高），
而且它的产物是**给次日用的状态**（今高 = 明天的触发价），不是「今天像不像某个形态」。
塞进 `pattern_hit` 的话，它会被形态选股页当成第 11 个形态混进榜单里 ——
而它真正该出现的地方是「明天开盘前看一眼」的那张表。

## 为什么整段替换

与形态扫描同理：**样板是会消失的**（补采或订正日线之后，某只票可能不再合格），
upsert 会把旧命中永久留在表里。按 `trade_date` 整段替换，重扫幂等。
"""

import logging
import time
from datetime import date

from sqlalchemy import delete

from app.config import Settings, get_settings
from app.db import session_scope, upsert_many
from app.jobs.scan_patterns import latest_trade_date, load_bars, require_bars
from app.models import CollectLog, TemplatePool
from app.services.patterns import build_bars
from app.services.template_pool import TEMPLATE_MIN_BARS, evaluate
from app.sources.ifind import IfindError

logger = logging.getLogger(__name__)

# 采集日志里的一类任务名
TEMPLATE_TASK = "template"


def _record(
    trade_date: date, status: str, rows: int, message: str | None, cost: float | None = None
) -> None:
    with session_scope() as session:
        session.add(
            CollectLog(
                trade_date=trade_date,
                task=TEMPLATE_TASK,
                status=status,
                rows=rows,
                message=message,
                cost_seconds=cost,
            )
        )


def scan(trade_date: date | None = None, settings: Settings | None = None) -> dict:
    """扫描一个交易日的样板日，整段替换该日的记录。"""
    settings = settings or get_settings()
    started = time.monotonic()

    target = trade_date or latest_trade_date()
    require_bars(target)
    grouped = load_bars(target, settings)
    if not grouped:
        raise IfindError(f"{target} 没有日线数据，先跑 collect_kline")

    rows: list[dict] = []
    skipped = 0
    for code, records in grouped.items():
        if len(records) < TEMPLATE_MIN_BARS:
            # 次新股 / 长期停牌：凑不出 20 日均量，量比无从谈起
            skipped += 1
            continue
        hit = evaluate(build_bars(records))
        if hit is None:
            continue

        # 价格取**库里那根原始 K 线**，不取复权序列上的值。两者对最后一根是同一个数
        # （`build_bars` 锚在最新收盘）、但写代码时看不出这个前提，所以别改成
        # `bars.high[-1]` —— 复权序列一旦不再锚在最新价，触发价就会变成复权价，
        # 而那是不能拿去挂单的（见 TemplatePool 的模型说明）
        last = records[-1]
        rows.append(
            {
                "trade_date": target,
                "code": code,
                "name": last["name"],
                "high": last["high"],
                "close": last["close"],
                "pct_chg": last["pct_chg"],
                "amount": last["amount"],
                "surge": round(hit.surge, 4),
                "vol_ratio": round(hit.vol_ratio, 2),
                "close_pos": round(hit.close_pos, 3),
            }
        )

    with session_scope() as session:
        session.execute(delete(TemplatePool).where(TemplatePool.trade_date == target))
        written = upsert_many(session, TemplatePool, rows)

    cost = round(time.monotonic() - started, 2)
    logger.info(
        "样板池扫描完成：%s，%d 只票 → %d 只合格样板（%d 只因 K 线不足跳过），用时 %ss",
        target,
        len(grouped),
        written,
        skipped,
        cost,
    )
    _record(target, "ok", written, f"{len(grouped)} 只 / {written} 只样板", cost)
    return {
        "status": "ok",
        "trade_date": target.isoformat(),
        "codes": len(grouped),
        "skipped": skipped,
        "rows": written,
        "cost_seconds": cost,
    }
