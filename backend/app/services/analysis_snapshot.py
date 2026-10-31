"""查票结论快照：保存 / 列表 / 删除（FEAT-ANALYSIS-SNAPSHOT，2026-10-31）。

只读写本地表 `analysis_snapshot`，**不重算结论、不出外网** —— 快照的意义就是
「我当时看到的是什么」，重新跑一遍 `classify_phase` 得到的是今天的结论，不是那天的。

同一只票只留最近 `MAX_PER_CODE` 条（需求里的「可选：同一代码仅保留最近 N 条」）：
查票页是随手用的，同一只票来回查十几次会把列表淹掉，而更早的结论参考价值很低。
"""

import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import AnalysisSnapshot

logger = logging.getLogger(__name__)

#: 每个用户、每只票保留的快照条数上限
MAX_PER_CODE = 20


def save(
    session: Session,
    user_id: int,
    code: str,
    name: str | None,
    summary: str,
    payload: dict | None,
) -> AnalysisSnapshot:
    """存一条快照，并把同票超出的旧记录删掉。"""
    row = AnalysisSnapshot(
        user_id=user_id,
        code=code,
        name=name,
        summary=summary,
        payload=payload,
    )
    session.add(row)
    session.flush()

    stale = (
        select(AnalysisSnapshot.id)
        .where(
            AnalysisSnapshot.user_id == user_id,
            AnalysisSnapshot.code == code,
        )
        .order_by(AnalysisSnapshot.id.desc())
        .offset(MAX_PER_CODE)
    )
    session.execute(delete(AnalysisSnapshot).where(AnalysisSnapshot.id.in_(stale)))
    return row


def rows(
    session: Session, user_id: int, code: str | None = None, limit: int = 50
) -> list[AnalysisSnapshot]:
    """当前用户的快照，**新的在前**；可按代码过滤。"""
    statement = select(AnalysisSnapshot).where(AnalysisSnapshot.user_id == user_id)
    if code:
        statement = statement.where(AnalysisSnapshot.code == code)
    return list(
        session.scalars(statement.order_by(AnalysisSnapshot.id.desc()).limit(limit))
    )


def get(session: Session, user_id: int, snap_id: int) -> AnalysisSnapshot | None:
    """按 id 取一条，**必须属于当前用户**（否则返回 None，不泄露别人存了什么）。"""
    return session.scalar(
        select(AnalysisSnapshot).where(
            AnalysisSnapshot.id == snap_id,
            AnalysisSnapshot.user_id == user_id,
        )
    )


def remove(session: Session, user_id: int, snap_id: int) -> bool:
    row = get(session, user_id, snap_id)
    if row is None:
        return False
    session.delete(row)
    return True
