"""自选股接口（最小集）。

完整的自选股页面属于 P5，这里先提供增删查，让选股结果能落下来 ——
否则选股器跑完就什么都没留下，没法跟进。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Watchlist
from app.schemas import WatchlistIn, WatchlistOut

router = APIRouter(prefix="/api/watchlist", tags=["自选股"])


def _normalize_code(raw: str) -> str:
    """把各种写法统一成 6 位代码。

    用户在选股结果、K 线页、手工输入等场景下会给出 `600519` / `600519.SH` /
    `sh600519` 等不同形态，统一抽数字后校验长度即可。
    """
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(digits) != 6:
        raise HTTPException(
            status_code=400, detail=f"股票代码应为 6 位数字，收到「{raw}」"
        )
    return digits


@router.get("", response_model=list[WatchlistOut])
def list_watchlist(session: Session = Depends(get_db)) -> list[WatchlistOut]:
    rows = list(session.scalars(select(Watchlist).order_by(Watchlist.added_at.desc())))
    return [WatchlistOut.model_validate(row) for row in rows]


@router.post("", response_model=WatchlistOut)
def add_watchlist(
    payload: WatchlistIn, session: Session = Depends(get_db)
) -> WatchlistOut:
    code = _normalize_code(payload.code)

    existing = session.get(Watchlist, code)
    if existing is not None:
        # 重复加入当成功处理，前端连点不会报错
        return WatchlistOut.model_validate(existing)

    row = Watchlist(code=code, name=payload.name, note=payload.note)
    session.add(row)
    session.commit()
    session.refresh(row)
    return WatchlistOut.model_validate(row)


@router.delete("/{code}")
def remove_watchlist(code: str, session: Session = Depends(get_db)) -> dict:
    row = session.get(Watchlist, _normalize_code(code))
    if row is None:
        raise HTTPException(status_code=404, detail="该股票不在自选里")
    session.delete(row)
    session.commit()
    return {"ok": True}
