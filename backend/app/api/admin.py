"""数据管理接口。"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.jobs.collect_daily import DailyCollector
from app.models import CollectLog, Lhb, LimitPool, MarketSentiment
from app.schemas import AdminStatus, CollectLogOut, CollectResult
from app.sources.ifind import IfindError

router = APIRouter(prefix="/api/admin", tags=["数据管理"])


def _build_collector() -> DailyCollector:
    try:
        return DailyCollector()
    except IfindError as exc:
        # 密钥缺失属于配置问题，给 400 而不是 500
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/status", response_model=AdminStatus)
def status(session: Session = Depends(get_db)) -> AdminStatus:
    """采集状态：各表最新日期、已有天数、最近采集日志。"""
    logs = list(
        session.scalars(select(CollectLog).order_by(CollectLog.id.desc()).limit(20))
    )
    return AdminStatus(
        latest_sentiment_date=session.scalar(select(func.max(MarketSentiment.trade_date))),
        latest_limit_pool_date=session.scalar(select(func.max(LimitPool.trade_date))),
        latest_lhb_date=session.scalar(select(func.max(Lhb.trade_date))),
        data_days=session.scalar(select(func.count()).select_from(MarketSentiment)) or 0,
        recent_logs=[CollectLogOut.model_validate(log) for log in logs],
    )


@router.post("/collect", response_model=CollectResult)
def collect(
    trade_date: date | None = Query(None, alias="date", description="缺省取最近交易日"),
) -> CollectResult:
    """执行一次完整采集（含指数、涨停三池、龙虎榜、情绪指标）。"""
    return CollectResult.model_validate(_build_collector().run(trade_date))


@router.post("/backfill")
def backfill(
    start: date = Query(..., description="起始日期"),
    end: date | None = Query(None, description="结束日期，缺省取最近交易日"),
) -> dict:
    """历史回补：指数行情 + 涨停三池 + 龙虎榜 + 情绪指标。

    指数走 iFinD 日频接口按日期回补。涨跌家数与打板效应因数据源只提供当日值
    而留空，详见设计文档 4.1。
    """
    result = _build_collector().backfill(start, end)
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
