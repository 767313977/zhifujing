"""知行B1 盘中预览（2026-10-19 加）。

14:50 拉一次全市场当天快照，在内存里拼成「历史 + 当日一根」的 K 线序列，只算
**知行B1 那 7 个形态 + 放量阳包阴**，结果直接推飞书。

## 为什么不写库

盘中预览**不落任何库**：

- **不写 `stock_daily`** —— 盘中那一根是半根 K 线（14:50 时还差 10 分钟），
  写进去会让个股页 / 形态页显示未收盘的价；
- **不写 `pattern_hit`** —— `scan_patterns.scan` 是「先删后插」，盘中跑会把当天
  正式榜单换成半根 K 线算出来的结果。15:05 的正式采集照常按收盘口径重扫
  （放量阳包阴已注册进形态库「量价」组，正式榜单里也会有它）。

## 成本与口径

- iFinD：14 次调用（与正式日线采集同一套前缀分段，全市场约 5,200 只）。
- 本地计算：只跑 B1 那 7 个形态（不是全部 47 个），约 10~20 秒。
- 量能是盘中的（差最后约 10 分钟），所以「缩量」类信号会比收盘口径略多。
  推送里写明了这一点，别把它当收盘名单用。

配额紧张时直接跳过：这是可选增强，不该和当天的主线采集抢额度。
"""

import logging
import time
from datetime import date, datetime

from app.config import Settings, get_settings
from app.db import session_scope
from app.jobs.collect_kline import _snapshot_query, _snapshot_row
from app.jobs.collect_universe import crawl_prefixes, load_codes
from app.jobs.push_brief import already_pushed, send_elements
from app.jobs.scan_patterns import (
    PATTERN_MAX_TOTAL_MV,
    _load_bars,
    _over_cap_codes,
    _signal_allowed,
)
from app.models import CollectLog
from app.services.coverage import coverage_ok
from app.services.patterns import (
    ENGULF_MIN_VOLUME_RATIO,
    MIN_SCORE,
    PATTERNS,
    _volume_engulf as _detect_volume_engulf,
    build_bars,
    is_st,
)
from app.services.usage import QuotaLevel, level_label, quota_level
from app.services.trading_days import is_trade_day
from app.sources.ifind import get_shared_client

logger = logging.getLogger(__name__)

#: `collect_log` 里的任务名
TASK = "b1_intraday"

#: 只推这 7 个（知行超级B1 的子信号），顺序与形态页一致
B1_KEYS = (
    "zgnb_turn",
    "zgnb_shrink",
    "zgnb_origin",
    "zgnb_super_shrink",
    "zgnb_pull_white",
    "zgnb_pull_super",
    "zgnb_pull_yellow",
)
NAMES = {pattern.key: pattern.name for pattern in PATTERNS}

#: 只保留 B1 的 7 个形态对象。不调 `patterns.evaluate`：那会把全部 47 个形态都算一遍
#: （约 65 秒），而这里只需要 7 个。
B1_PATTERNS = tuple(pattern for pattern in PATTERNS if pattern.key in set(B1_KEYS))

#: 每个形态最多列几只。推送是给人扫一眼去下单的，列几十只反而没法用
MAX_PER_PATTERN = 10

#: 卡片总表的行数上限。飞书卡片最多挂 **5 张表**、每张 `page_size` 最多 20 行 ——
#: 7 个形态各一张表会直接报 `card table number over limit`（2026-10-19 实测），
#: 所以只做一张总表：按评分全局排序，形态放第一列。
TABLE_ROWS = 20

#: 「放量阳包阴」最多列几只（与 B1 同口径，扫一眼下单用）
MAX_ENGULF_ROWS = 20


def _record(day: date, status: str, message: str) -> None:
    with session_scope() as session:
        session.add(
            CollectLog(trade_date=day, task=TASK, status=status, message=message)
        )


def _fetch_snapshot(day: date, settings: Settings) -> tuple[dict[str, dict], int]:
    """拉全市场当天快照，**只返回内存行，不写库**。返回 `({代码: 行}, 调用次数)`。

    ⚠️ 用**进程内共享的** iFinD 客户端（2026-10-22 体检）：原来这里 `IfindClient(settings)`
    新建一个实例 —— 它自带独立令牌桶、且要重新握手一次 MCP 会话。14:50 这 13 次调用
    与用户触发的同步 / DDE 现取（都持着 `collect_guard`）并发时，两边各按自己的
    4 req/s 走，实际 QPS 翻倍 → 直接触发 iFinD 429。共享实例后至少限速是同一把。
    """
    client = get_shared_client(settings)
    raw, calls = crawl_prefixes(
        client, lambda prefix: _snapshot_query(prefix, day), f"{day} 盘中"
    )
    rows: dict[str, dict] = {}
    for code, raw_row in raw.items():
        row = _snapshot_row(raw_row, code, day)
        if row is not None:
            rows[code] = row
    return rows, calls


def _as_record(row: dict) -> dict:
    """快照行 → 与 `_load_bars` 同口径的一根 K 线（键是 `date` 不是 `trade_date`）。"""
    return {
        "date": row["trade_date"],
        "name": row.get("name"),
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "volume": row.get("volume"),
        "amount": row.get("amount"),
        "pct_chg": row["pct_chg"],
    }


def _scan(
    history: dict[str, list[dict]],
    snapshot: dict[str, dict],
    *,
    dropped: set[str] | None = None,
) -> tuple[dict[str, list[dict]], list[dict]]:
    """把当天快照拼在历史后面，算 B1 与「放量阳包阴」。

    返回 `({pattern: [命中]}, [放量阳包阴])`。`dropped` 是外部已判定要剔除的代码
    （大市值票），与 `scan_patterns.scan` 同口径。纯函数，不发请求、不碰数据库。

    「放量阳包阴」直接用形态库里的 `patterns._volume_engulf`（「量价」组）——
    与站点、每日形态扫描同一份判定，不另写一遍。盘中只是**不落库**：结果只进
    推送，`pattern_hit` 仍由 15:05 的正式扫描按收盘口径写。
    """
    dropped = dropped or set()
    hits: dict[str, list[dict]] = {key: [] for key in B1_KEYS}
    engulfs: list[dict] = []
    for code, row in snapshot.items():
        records = history.get(code)
        if not records or code in dropped:
            continue
        record = _as_record(row)
        if is_st(record.get("name")):
            continue

        bars = build_bars([*records, record], code)
        for pattern in B1_PATTERNS:
            try:
                signal = pattern.detect(bars)
            except Exception:  # noqa: BLE001 - 单个形态出错不影响其余
                logger.warning(
                    "形态 %s 计算失败（%s）", pattern.key, code, exc_info=True
                )
                continue
            if signal is None or signal.score < MIN_SCORE:
                continue
            if not _signal_allowed(signal.pattern, code, set(), set()):
                continue
            hits[signal.pattern].append(
                {
                    "code": code,
                    "name": record.get("name") or code,
                    "score": round(signal.score, 1),
                    "close": record["close"],
                    "pct_chg": record["pct_chg"],
                }
            )

        try:
            signal = _detect_volume_engulf(bars)
        except Exception:  # noqa: BLE001 - 同上
            logger.warning("阳包阴判定失败（%s）", code, exc_info=True)
            signal = None
        if signal is None:
            continue
        if not _signal_allowed(signal.pattern, code, set(), set()):
            continue
        engulfs.append(
            {
                "code": code,
                "name": record.get("name") or code,
                "close": record["close"],
                "pct_chg": record["pct_chg"],
                "volume_ratio": float(signal.detail.get("volume_ratio") or 0.0),
            }
        )

    for items in hits.values():
        items.sort(key=lambda item: item["score"], reverse=True)
    engulfs.sort(key=lambda item: item["volume_ratio"], reverse=True)
    return hits, engulfs


def _title(day: date, clock: str, calls: int) -> str:
    """卡片首行。标题必须含「收盘复盘」（群机器人关键词校验）。"""
    return (
        f"**收盘复盘 · 知行B1 盘中预览** · {day.isoformat()} {clock}\n"
        f"盘中快照（当天量能未走完，尾盘可能变化），{calls} 次调用。"
    )


def _engulf_table(items: list[dict]) -> dict:
    """「放量阳包阴」表：名称 / 代码 / 现价 / 涨幅 / 倍量（今量 ÷ 昨量）。"""
    return {
        "tag": "table",
        "page_size": MAX_ENGULF_ROWS,
        "row_height": "low",
        "columns": [
            {"name": "name", "display_name": "名称", "data_type": "text", "width": "auto"},
            {"name": "code", "display_name": "代码", "data_type": "text"},
            {
                "name": "price",
                "display_name": "现价",
                "data_type": "text",
                "horizontal_align": "right",
            },
            {
                "name": "pct",
                "display_name": "涨幅",
                "data_type": "text",
                "horizontal_align": "right",
            },
            {
                "name": "ratio",
                "display_name": "倍量",
                "data_type": "text",
                "horizontal_align": "right",
            },
        ],
        "rows": [
            {
                "name": item["name"],
                "code": item["code"],
                "price": f"{item['close']:.2f}",
                "pct": f"{item['pct_chg']:+.2f}%",
                "ratio": f"{item['volume_ratio']:.2f}",
            }
            for item in items[:MAX_ENGULF_ROWS]
        ],
    }


def build_card(
    day: date,
    clock: str,
    hits: dict[str, list[dict]],
    engulfs: list[dict],
    calls: int,
) -> list[dict]:
    """飞书卡片元素：B1 总表 + 放量阳包阴表（最多两张表，飞书上限 5 张）。

    表格组件**只能挂在卡片根节点**（不能塞进 div 里），与 `jobs/scan_dde.py`
    的 `_board_table` 同一套写法。

    为什么 B1 只做一张表：7 个形态各一张表会直接报 `card table number over limit`
    （2026-10-19 实测）。改成总表后，形态放第一列，按评分全局排序。

    单元格放**格式化好的文本**：写 `+2.30%` 一眼能读，不用再配单位。
    """
    total = sum(len(items) for items in hits.values())
    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": _title(day, clock, calls)}}
    ]
    if total == 0 and not engulfs:
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": "今日暂无命中。"}}
        )
        return elements

    if total:
        counts = " · ".join(
            f"{NAMES.get(key, key)} {len(hits[key])}" for key in B1_KEYS if hits.get(key)
        )
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"共 **{total}** 条：{counts}"},
            }
        )

        merged = [
            {**item, "pattern": NAMES.get(key, key)}
            for key in B1_KEYS
            for item in hits.get(key) or []
        ]
        merged.sort(key=lambda item: item["score"], reverse=True)
        shown = merged[:TABLE_ROWS]

        elements.append(
            {
                "tag": "table",
                "page_size": TABLE_ROWS,
                "row_height": "low",
                "columns": [
                    {
                        "name": "pattern",
                        "display_name": "形态",
                        "data_type": "text",
                        "width": "auto",
                    },
                    {"name": "name", "display_name": "名称", "data_type": "text"},
                    {"name": "code", "display_name": "代码", "data_type": "text"},
                    {
                        "name": "price",
                        "display_name": "现价",
                        "data_type": "text",
                        "horizontal_align": "right",
                    },
                    {
                        "name": "pct",
                        "display_name": "涨幅",
                        "data_type": "text",
                        "horizontal_align": "right",
                    },
                    {
                        "name": "score",
                        "display_name": "评分",
                        "data_type": "text",
                        "horizontal_align": "right",
                    },
                ],
                "rows": [
                    {
                        "pattern": item["pattern"],
                        "name": item["name"],
                        "code": item["code"],
                        "price": f"{item['close']:.2f}",
                        "pct": f"{item['pct_chg']:+.2f}%",
                        "score": f"{item['score']:.0f}",
                    }
                    for item in shown
                ],
            }
        )
        if total > len(shown):
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"其余 {total - len(shown)} 条未列出（按评分取前 {TABLE_ROWS}）。",
                    },
                }
            )

    if engulfs:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**放量阳包阴** {len(engulfs)} 只"
                        f"（倍量 ≥ {ENGULF_MIN_VOLUME_RATIO:.1f}，按倍数排序）"
                    ),
                },
            }
        )
        elements.append(_engulf_table(engulfs))
        if len(engulfs) > MAX_ENGULF_ROWS:
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"其余 {len(engulfs) - MAX_ENGULF_ROWS} 只未列出。",
                    },
                }
            )
    return elements


def _format(
    day: date,
    clock: str,
    hits: dict[str, list[dict]],
    engulfs: list[dict],
    calls: int,
) -> str:
    """纯文字版：没配 webhook、走 lark-cli 时的兜底（那条通道只吃 markdown）。"""
    total = sum(len(items) for items in hits.values())
    lines = [_title(day, clock, calls)]
    if total == 0 and not engulfs:
        lines += ["", "今日暂无命中。"]
        return "\n".join(lines)

    if total:
        lines.append("")
        for key in B1_KEYS:
            items = hits.get(key) or []
            if not items:
                continue
            lines.append(f"**{NAMES.get(key, key)}** {len(items)} 只")
            for item in items[:MAX_PER_PATTERN]:
                lines.append(
                    f"- {item['name']} {item['code']}（{item['score']:.0f} 分）"
                    f" 现价 {item['close']:.2f}（{item['pct_chg']:+.2f}%）"
                )
            if len(items) > MAX_PER_PATTERN:
                # 不带 `-`：和上面的命中项混在同一个列表里会被误读成「又一只」
                lines.append(f"（其余 {len(items) - MAX_PER_PATTERN} 只未列出）")

    if engulfs:
        lines.append("")
        lines.append(
            f"**放量阳包阴** {len(engulfs)} 只"
            f"（倍量 ≥ {ENGULF_MIN_VOLUME_RATIO:.1f}，按倍数排序）"
        )
        for item in engulfs[:MAX_ENGULF_ROWS]:
            lines.append(
                f"- {item['name']} {item['code']} 现价 {item['close']:.2f}"
                f"（{item['pct_chg']:+.2f}%）· 倍量 {item['volume_ratio']:.2f}"
            )
        if len(engulfs) > MAX_ENGULF_ROWS:
            lines.append(f"（其余 {len(engulfs) - MAX_ENGULF_ROWS} 只未列出）")
    return "\n".join(lines)


def run(settings: Settings | None = None, *, now: datetime | None = None) -> dict:
    """跑一次盘中预览。失败只记录，不抛异常（定时任务不该把调度器带崩）。"""
    settings = settings or get_settings()
    moment = now or datetime.now()
    day = moment.date()

    if not settings.b1_intraday_enabled:
        return {"status": "skipped", "reason": "盘中预览已关闭"}
    if not is_trade_day(day):
        return {"status": "skipped", "reason": f"{day} 不是交易日"}
    if already_pushed(day, TASK):
        return {"status": "skipped", "reason": "今天已经推过"}

    level = quota_level(settings=settings)
    if level >= QuotaLevel.PAUSE_HIT_DDE:
        reason = f"配额让路，跳过盘中预览：{level_label(level)}"
        logger.info(reason)
        return {"status": "skipped", "reason": reason}

    started = time.monotonic()
    try:
        snapshot, calls = _fetch_snapshot(day, settings)
    except Exception as exc:  # noqa: BLE001 - 记一条失败就结束
        _record(day, "failed", f"盘中快照失败：{exc}")
        logger.warning("知行B1 盘中快照失败：%s", exc)
        return {"status": "failed", "error": str(exc)}

    pool = len(load_codes())
    if pool and not coverage_ok(len(snapshot), pool):
        message = f"只取到 {len(snapshot)}/{pool} 只（{len(snapshot) / pool:.0%}），数据不全，本次不推名单"
        _record(day, "failed", message)
        logger.warning("知行B1 盘中预览：%s", message)
        return {"status": "failed", "reason": message}

    history = _load_bars(day, set(load_codes()) | set(snapshot))
    dropped = _over_cap_codes(PATTERN_MAX_TOTAL_MV)
    hits, engulfs = _scan(history, snapshot, dropped=dropped)

    clock = moment.strftime("%H:%M")
    # 卡片表格优先（webhook）；没配 webhook 时 send_elements 会自动退回纯文字版
    result = send_elements(
        build_card(day, clock, hits, engulfs, calls),
        settings,
        fallback_markdown=_format(day, clock, hits, engulfs, calls),
    )
    cost = round(time.monotonic() - started, 2)
    total = sum(len(items) for items in hits.values())

    if result["ok"]:
        message = f"{total} 条命中 / 阳包阴 {len(engulfs)} 只 / {calls} 次调用 / 用时 {cost}s"
        _record(day, "ok", message)
        logger.info("知行B1 盘中预览已推送：%s", message)
        return {
            "status": "sent",
            "hits": total,
            "engulfs": len(engulfs),
            "calls": calls,
            "cost_seconds": cost,
        }

    error = result.get("error") or "推送失败"
    _record(day, "failed", error)
    logger.warning("知行B1 盘中预览推送失败：%s", error)
    return {"status": "failed", "error": error}
