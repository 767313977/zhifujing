"""选股器接口。

本站**不做「本地全市场结构化筛选」**：全市场日线不落库（那会触发东财频控，
详见设计文档 1.7），所以找票只有一条路径 —— iFinD 的自然语言选股。

这也正是当初选 iFinD 方案的主要理由：一句自然语言就能表达多指标组合条件，
比自建筛选器能覆盖的维度多得多。
"""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ScreenPreset
from app.schemas import PresetIn, PresetOut, ScreenRunOut
from app.sources.ifind import IfindClient, IfindError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/screener", tags=["选股器"])

# 预设条件里存放自然语言 query 的键名
QUERY_KEY = "query"


def _client() -> IfindClient:
    try:
        return IfindClient()
    except IfindError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/run", response_model=ScreenRunOut)
def run_screen(
    query: str = Query(..., min_length=2, description="自然语言选股条件"),
) -> ScreenRunOut:
    """执行一次自然语言选股。

    返回里 `matched` 是匹配总数、`returned` 是表格实际给的行数（上限 100）。
    两者不等说明结果被截断，前端必须显式提示。
    """
    client = _client()
    started = time.monotonic()
    try:
        result = client.search_stocks(query)
    except IfindError as exc:
        logger.warning("选股失败 query=%s: %s", query, exc)
        raise HTTPException(status_code=502, detail=f"iFinD 选股失败：{exc}") from exc

    return ScreenRunOut(
        query=query,
        cost_seconds=round(time.monotonic() - started, 2),
        **result,
    )


@router.get("/presets", response_model=list[PresetOut])
def list_presets(session: Session = Depends(get_db)) -> list[PresetOut]:
    rows = list(session.scalars(select(ScreenPreset).order_by(ScreenPreset.id.desc())))
    return [PresetOut.model_validate(row) for row in rows]


@router.post("/presets", response_model=PresetOut)
def create_preset(payload: PresetIn, session: Session = Depends(get_db)) -> PresetOut:
    name = payload.name.strip()
    query = payload.query.strip()
    if not name or not query:
        raise HTTPException(status_code=400, detail="名称与条件都不能为空")

    existing = session.scalars(
        select(ScreenPreset).where(ScreenPreset.name == name)
    ).first()
    if existing is not None:
        # 同名直接覆盖，避免反复保存同一条件时堆出一串重复项
        existing.conditions = {QUERY_KEY: query}
        session.commit()
        session.refresh(existing)
        return PresetOut.model_validate(existing)

    preset = ScreenPreset(name=name, kind="natural", conditions={QUERY_KEY: query})
    session.add(preset)
    session.commit()
    session.refresh(preset)
    return PresetOut.model_validate(preset)


@router.delete("/presets/{preset_id}")
def delete_preset(preset_id: int, session: Session = Depends(get_db)) -> dict:
    preset = session.get(ScreenPreset, preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="预设不存在")
    session.delete(preset)
    session.commit()
    return {"ok": True}
