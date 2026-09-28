"""数据管理接口。**整个 `/api/admin` 都是管理员专属**（见下面 router 的 dependencies）。"""

import logging
import secrets
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.jobs.collect_daily import CollectionBusy, DailyCollector, collect_guard
from app.jobs.scheduler import get_scheduler
from app.models import (
    AppUser,
    CollectLog,
    IndexDaily,
    InviteCode,
    Lhb,
    LimitPool,
    MarketSentiment,
    SectorDaily,
    StockConcept,
    StockDaily,
)
from app.schemas import (
    AdminStatus,
    CollectLogOut,
    DisabledIn,
    IfindQuota,
    InviteIn,
    InviteOut,
    MemberOut,
    ResetPasswordIn,
    SchedulerStatus,
    TableCoverage,
)
from app.services import auth
from app.services.auth import require_admin
from app.services.usage import quota_status
from app.sources.ifind import IfindError

logger = logging.getLogger(__name__)

# ⚠️ `dependencies=[Depends(require_admin)]` 挂在**路由级**，而不是逐个接口挂：
# 这里每一条都能触发采集（烧 iFinD 配额），漏挂一条就是一个洞，
# 而且将来往这个文件加接口的人很容易忘了 —— 挂在 router 上就忘不掉。
router = APIRouter(
    prefix="/api/admin", tags=["数据管理"], dependencies=[Depends(require_admin)]
)


# 邀请码用的字母表：**去掉了 0 O 1 l I** 这些手抄/口述时容易认错的。
# 邀请码是要发给人、可能被念出来的东西，可读性比多几个比特重要。
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
# 8 位 ≈ 32^8 ≈ 1.1e12 种，配合登录/注册限流足够；再短就开始担心被猜
_CODE_LENGTH = 8
# 一次最多生成几个：够发给一个群，又不至于让清单变成一坨
_CODE_MAX_BATCH = 20


def _build_collector() -> DailyCollector:
    try:
        return DailyCollector()
    except IfindError as exc:
        # 密钥缺失属于配置问题，给 400 而不是 500
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _coverage(session: Session, model, label: str) -> TableCoverage:
    """按「不同交易日数」统计覆盖，而不是行数 —— 指数表一天有 5 行。"""
    return TableCoverage(
        label=label,
        days=session.scalar(
            select(func.count(func.distinct(model.trade_date)))
        ) or 0,
        latest=session.scalar(select(func.max(model.trade_date))),
    )


@router.get("/status", response_model=AdminStatus)
def status(session: Session = Depends(get_db)) -> AdminStatus:
    """采集状态：各表覆盖、定时任务、最近采集日志。"""
    logs = list(
        session.scalars(select(CollectLog).order_by(CollectLog.id.desc()).limit(30))
    )
    scheduler = get_scheduler()
    scheduler_status = (
        scheduler.status()
        if scheduler is not None
        else {
            "enabled": False,
            "running": False,
            "collect_time": "—",
            "catchup_on_start": False,
            "next_run_time": None,
            "last_run": None,
            "last_result": None,
        }
    )
    return AdminStatus(
        latest_sentiment_date=session.scalar(select(func.max(MarketSentiment.trade_date))),
        latest_limit_pool_date=session.scalar(select(func.max(LimitPool.trade_date))),
        latest_lhb_date=session.scalar(select(func.max(Lhb.trade_date))),
        latest_index_date=session.scalar(select(func.max(IndexDaily.trade_date))),
        data_days=session.scalar(select(func.count()).select_from(MarketSentiment)) or 0,
        coverage=[
            _coverage(session, IndexDaily, "指数日线"),
            _coverage(session, LimitPool, "涨停三池"),
            _coverage(session, Lhb, "龙虎榜"),
            _coverage(session, SectorDaily, "板块行情"),
            _coverage(session, StockConcept, "涨停题材"),
            _coverage(session, StockDaily, "个股日线"),
            _coverage(session, MarketSentiment, "情绪指标"),
        ],
        scheduler=SchedulerStatus.model_validate(scheduler_status),
        recent_logs=[CollectLogOut.model_validate(log) for log in logs],
        ifind_quota=IfindQuota.model_validate(quota_status()),
    )


# 这里原来有个 `POST /api/admin/collect`（手动跑一次完整采集）。**2026-09-28 删掉了**，
# 用户要求「删掉手动采集按钮，禁止手动采集」。
#
# 起因：当天有个自动化代理在测顶栏时误点了首页工具栏上的「采集」，于是白跑一轮
# （约 50 次 iFinD 调用）—— 采集是这一站唯一「点一下就花钱」的操作，而它本来就不需要
# 手动跑（每天收盘后自动跑 + 错过时刻的启动补采）。
#
# ⚠️ 真需要强制重采时的替代办法（都不需要这个接口）：
#   1. 重启服务 —— `start()` 里的「启动补采」会在当天数据缺失或有失败步骤时补跑
#      （`sudo systemctl restart fupan`）
#   2. 要补历史某几天：`POST /api/admin/backfill`（那个还在，它是「补数」不是「采集」）
# 判断依据见 `collect_daily.has_collected`：情绪表有当天数据**且**当天没有 failed 步骤。


@router.post("/kline/recent")
def kline_recent(
    days: int = Query(60, ge=20, le=120, description="补最近多少个交易日"),
    trade_date: date | None = Query(None, alias="date"),
) -> dict:
    """补齐最近 N 个交易日的全市场日线（辉宾/形态需要连续近端 K 线）。"""
    from app.jobs.collect_kline import KlineCollector

    try:
        with collect_guard("近端日线"):
            return KlineCollector().collect_recent(trade_date, days=days)
    except CollectionBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IfindError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/patterns/scan")
def patterns_scan(
    trade_date: date | None = Query(None, alias="date"),
) -> dict:
    """手动跑一遍形态扫描（含辉宾「明天盯 / 今天可买」）。"""
    from app.jobs.scan_patterns import scan

    try:
        with collect_guard("形态扫描"):
            return scan(trade_date)
    except CollectionBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IfindError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/backfill")
def backfill(
    start: date = Query(..., description="起始日期"),
    end: date | None = Query(None, description="结束日期，缺省取最近交易日"),
) -> dict:
    """历史回补：指数行情 + 涨停三池 + 龙虎榜 + 情绪指标。

    指数走 iFinD 日频接口按日期回补；三池受数据源窗口限制只能覆盖最近
    15 个交易日，更早的日期对应指标记 null。详见设计文档 4.1。
    """
    try:
        with collect_guard("手动回补"):
            result = _build_collector().backfill(start, end)
    except CollectionBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    days = result["days"]
    steps = [result["index_history"], result["lhb_history"]]
    for day in days:
        steps.extend(value for value in day.values() if isinstance(value, dict))
    failed = [step for step in steps if step.get("status") != "ok"]

    return {
        "days": len(days),
        "index_history": result["index_history"],
        "lhb_history": result["lhb_history"],
        "total_steps": len(steps),
        "failed_steps": len(failed),
        "failures": [
            {"trade_date": day["trade_date"], "task": name, **step}
            for day in days
            for name, step in day.items()
            if isinstance(step, dict) and step.get("status") != "ok"
        ],
        "detail": days,
    }


# ------------------------------------------------------------ 会员与邀请码
#
# 见设计文档 §8.69。这几条也在 `/api/admin/*` 下，所以自动受上面的 require_admin 保护。


def _new_invite_code(session: Session) -> str:
    """生成一个没被占用的邀请码。

    **不用 uuid**：那串 32 个十六进制字符既没法口头念给人、也不好手抄。
    这里用去掉了易混字母的 32 个字符表，随机 8 位。
    """
    for _ in range(20):
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
        if session.get(InviteCode, code) is None:
            return code
    # 撞 20 次同一批已存在的码：不是运气问题就是哪里坏了，别静默继续
    raise HTTPException(status_code=500, detail="生成邀请码失败，请重试")


def _invite_out(row: InviteCode, names: dict[int, str]) -> InviteOut:
    return InviteOut(
        code=row.code,
        note=row.note,
        created_at=row.created_at,
        used_by=row.used_by,
        # 列表里直接显示「被谁用了」，省得自己拿 id 去对
        used_by_name=names.get(row.used_by) if row.used_by else None,
        used_at=row.used_at,
        disabled_at=row.disabled_at,
    )


@router.get("/invites", response_model=list[InviteOut])
def list_invites(session: Session = Depends(get_db)) -> list[InviteOut]:
    """所有邀请码，新的在前。"""
    rows = list(
        session.scalars(select(InviteCode).order_by(InviteCode.created_at.desc()))
    )
    names = {user.id: user.username for user in session.scalars(select(AppUser))}
    return [_invite_out(row, names) for row in rows]


@router.post("/invites", response_model=list[InviteOut])
def create_invites(
    payload: InviteIn, session: Session = Depends(get_db)
) -> list[InviteOut]:
    """生成邀请码。`count` 可以一次多生成几个（最多 20）。"""
    count = max(1, min(payload.count, _CODE_MAX_BATCH))
    rows = []
    for _ in range(count):
        row = InviteCode(code=_new_invite_code(session), note=payload.note)
        session.add(row)
        rows.append(row)
    session.commit()
    for row in rows:
        session.refresh(row)
    names: dict[int, str] = {}
    return [_invite_out(row, names) for row in rows]


@router.delete("/invites/{code}")
def delete_invite(code: str, session: Session = Depends(get_db)) -> dict:
    """删掉一个**没用过**的邀请码。

    ⚠️ 用过的拒绝删除：删了就断了「这个账号是拿哪个码进来的」这条记录，
    而那正是发一次性码的意义（见 models.InviteCode 的说明）。想阻止它继续被用，
    停用账号即可 —— 反正它已经绑定了，用不了第二次。
    """
    row = session.get(InviteCode, code.strip().upper())
    if row is None:
        raise HTTPException(status_code=404, detail="没有这个邀请码")
    if row.used_by is not None:
        raise HTTPException(
            status_code=400,
            detail="这个邀请码已经被用过了，删掉会失去「谁用哪个码进来」的记录",
        )
    session.delete(row)
    session.commit()
    return {"ok": True}


@router.get("/users", response_model=list[MemberOut])
def list_users(session: Session = Depends(get_db)) -> list[MemberOut]:
    """所有会员。`invite_code` 是他注册时用的那个码（可追来源）。"""
    users = list(session.scalars(select(AppUser).order_by(AppUser.created_at)))
    invites = {
        row.used_by: row.code
        for row in session.scalars(select(InviteCode))
        if row.used_by
    }
    return [
        MemberOut(
            id=user.id,
            username=user.username,
            is_admin=user.is_admin,
            created_at=user.created_at,
            last_login_at=user.last_login_at,
            disabled_at=user.disabled_at,
            invite_code=invites.get(user.id),
        )
        for user in users
    ]


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    payload: ResetPasswordIn,
    me: AppUser = Depends(require_admin),
    session: Session = Depends(get_db),
) -> dict:
    """管理员替会员重置密码，并**踢掉他所有会话**。

    先小规模不做自助找回：忘了密码来找你重置，比引入邮箱验证那一整套便宜得多。
    改完必须踢会话 —— 密码重置通常就发生在「怀疑账号被别人用了」的时候。
    """
    auth.validate_password(payload.new_password)
    row = session.get(AppUser, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有这个账号")
    row.password_hash = auth.hash_password(payload.new_password)
    session.commit()
    kicked = auth.delete_user_sessions(session, user_id)
    logger.info("管理员 %s 重置了 %s 的密码，踢掉 %d 个会话", me.username, row.username, kicked)
    return {"ok": True, "kicked_sessions": kicked}


@router.post("/users/{user_id}/disabled")
def set_disabled(
    user_id: int,
    payload: DisabledIn,
    me: AppUser = Depends(require_admin),
    session: Session = Depends(get_db),
) -> dict:
    """停用 / 恢复会员。停用即踢掉他所有会话。"""
    # 不拦的话可以一键把自己锁在门外 —— 而这时唯一能救你的账号就是你自己
    if user_id == me.id:
        raise HTTPException(status_code=400, detail="不能停用自己")
    row = session.get(AppUser, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有这个账号")
    row.disabled_at = datetime.now() if payload.disabled else None
    session.commit()
    kicked = auth.delete_user_sessions(session, user_id) if payload.disabled else 0
    logger.info(
        "%s 把 %s %s（踢掉 %d 个会话）",
        me.username,
        row.username,
        "停用了" if payload.disabled else "恢复了",
        kicked,
    )
    return {"ok": True, "kicked_sessions": kicked}
