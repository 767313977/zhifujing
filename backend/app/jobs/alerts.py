"""收盘链路告警：20:00 检查「简报没发 / 尾部 partial / 配额让路」。

为什么需要它：这条链路的失败大多**不会让页面报错** —— 简报没发只是少一条消息、
尾部 partial 只是少几列数据、配额让路只是某些增强项悄悄停掉。2026-10-16 那次
配额撞墙、当天没有选股结果，就是事后复盘才发现的。这里把这三类状态在晚间
主动推一条飞书，**只推一次**（`collect_log` 的 `alert` 任务去重）。
"""

import logging
from datetime import date

from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import session_scope
from app.models import CollectLog, TradeCalendar
from app.services.usage import QuotaLevel, quota_status

logger = logging.getLogger(__name__)

#: `collect_log` 里的任务名
TASK = "alert"
#: 与 `jobs/scheduler.py` 的 `TAIL_TASK` 必须一致（那里不能反向 import 本模块）
TAIL_TASK = "daily_tail"
#: 与 `jobs/push_brief.py` 的 `PUSH_TASK` 必须一致
PUSH_TASK = "push_brief"
#: 与 `jobs/backup.py` 的 `TASK` 必须一致
BACKUP_TASK = "db_backup"


def _is_trading_day(day: date) -> bool:
    with session_scope() as session:
        return (
            session.scalar(
                select(TradeCalendar.trade_date).where(TradeCalendar.trade_date == day)
            )
            is not None
        )


def _log_statuses(day: date, task: str) -> set[str]:
    with session_scope() as session:
        return set(
            session.scalars(
                select(CollectLog.status).where(
                    CollectLog.trade_date == day, CollectLog.task == task
                )
            )
        )


def _latest_backup() -> tuple[date | None, set[str]]:
    """最近一次备份记录：`(记录日期, 那天的全部状态)`；从未跑过返回 `(None, set())`。

    告警 20:00 跑、备份 23:10 才跑，所以这里查的是**昨天及以前**的备份 ——
    语义上就是「最近一次」，不是「当天」。同一天重跑多次时把状态合并看。
    """
    with session_scope() as session:
        rows = session.execute(
            select(CollectLog.trade_date, CollectLog.status)
            .where(CollectLog.task == BACKUP_TASK)
            .order_by(CollectLog.trade_date.desc())
            .limit(5)
        ).all()
    if not rows:
        return None, set()
    latest = rows[0][0]
    return latest, {status for day, status in rows if day == latest}


def _record(day: date, status: str, message: str) -> None:
    with session_scope() as session:
        session.add(
            CollectLog(trade_date=day, task=TASK, status=status, message=message)
        )


def check(day: date | None = None, settings: Settings | None = None) -> dict:
    """检查当天收盘链路；有异常就推一条告警。**不抛异常**。"""
    settings = settings or get_settings()
    today = day or date.today()
    if not settings.alert_enabled:
        return {"status": "skipped", "reason": "告警已关闭（ALERT_ENABLED=false）"}
    if not settings.feishu_push_enabled:
        return {"status": "skipped", "reason": "推送已关闭"}
    if not settings.feishu_webhook_url.strip() and not settings.feishu_target.strip():
        return {"status": "skipped", "reason": "未配置飞书推送通道"}
    if not _is_trading_day(today):
        return {"status": "skipped", "reason": f"{today} 不是交易日"}
    if "sent" in _log_statuses(today, TASK):
        return {"status": "skipped", "reason": "今天已经发过告警"}

    issues: list[str] = []
    push_status = _log_statuses(today, PUSH_TASK)
    if "ok" not in push_status:
        detail = "、".join(sorted(push_status)) if push_status else "没有任何记录"
        issues.append(f"简报未推送成功（push_brief：{detail}）")

    tail_status = _log_statuses(today, TAIL_TASK)
    if "ok" not in tail_status:
        if "partial" in tail_status:
            # 失败步骤写在 message 里，这里只给状态；详情去「数据管理」页看
            issues.append("尾部链只留下 partial（有步骤失败，详情见数据管理页）")
        elif tail_status:
            issues.append(f"尾部链状态异常（{'、'.join(sorted(tail_status))}）")
        else:
            issues.append("尾部链没有运行（当天采集可能没跑完）")

    quota = quota_status(today, settings)
    level = QuotaLevel(quota["level"])
    if level != QuotaLevel.NORMAL:
        issues.append(
            f"配额已进入让路档：{quota['level_label']}"
            f"（剩余 {quota['cycle_remaining']} / {quota['monthly_quota']} 次，"
            f"今日 {quota['today_calls']} 次）"
        )

    # 备份：23:10 才跑，这里查「最近一次」。失败要报、从未跑也要报。
    #
    # ⚠️ 「未配异地同步（BACKUP_SYNC_DIR）」**故意不报**（2026-10-20 用户要求）：
    # 那是一句常驻的配置提醒，每天都会推、推到用户关掉为止 —— 常驻提醒会让真告警
    # （备份失败、链路断）也变成背景噪声。它只留在备份记录里（`backup.py` 的
    # `sync_note` 会写明「未配置异地同步（备份与主库同盘）」），想看就去数据管理页。
    backup_day, backup_statuses = _latest_backup()
    if backup_day is None:
        issues.append("数据库备份从未运行（23:10 任务无记录）")
    elif "ok" not in backup_statuses:
        issues.append(f"最近一次数据库备份失败（{backup_day}），主库当前无保护")

    if not issues:
        return {"status": "ok", "issues": []}

    from app.jobs.push_brief import send_markdown

    lines = [f"**收盘复盘 · 链路告警** · {today.isoformat()}"] + [
        f"- {item}" for item in issues
    ]
    lines.append("")
    lines.append("请到站内「数据管理」页查看采集日志；若当天简报确实没发，可在服务器上手动补跑。")
    markdown = "\n".join(lines)
    result = send_markdown(markdown, settings)
    if result["ok"]:
        _record(today, "sent", "；".join(issues))
        logger.warning("收盘链路告警已推送：%s", "；".join(issues))
        return {"status": "sent", "issues": issues}
    _record(today, "failed", result.get("error") or "推送失败")
    logger.warning("收盘链路告警推送失败：%s", result.get("error"))
    return {"status": "failed", "issues": issues, "error": result.get("error")}
