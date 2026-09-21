"""数据管理接口。"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.jobs.collect_daily import CollectionBusy, DailyCollector, collect_guard
from app.jobs.scheduler import get_scheduler
from app.models import (
    CollectLog,
    IndexDaily,
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
    CollectResult,
    IfindQuota,
    SchedulerStatus,
    TableCoverage,
)
from app.services.usage import quota_status
from app.sources.ifind import IfindError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["数据管理"])


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


@router.post("/collect", response_model=CollectResult)
def collect(
    trade_date: date | None = Query(None, alias="date", description="缺省取最近交易日"),
) -> CollectResult:
    """执行一次完整采集（含指数、涨停三池、龙虎榜、情绪指标）。"""
    try:
        # 与定时任务互斥：两者同时跑会让实际请求速率翻倍并触发 iFinD 429
        with collect_guard("手动采集"):
            return CollectResult.model_validate(_build_collector().run(trade_date))
    except CollectionBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
