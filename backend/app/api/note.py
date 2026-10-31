"""复盘笔记接口。

每天一条：市场观点 + 次日计划。数据落在 `review_note` 表，
**按用户隔离**（2026-09-28 起，见设计文档 §8.69）：主键是 `(user_id, trade_date)`，
所以「取某天的笔记」必须同时给用户 id。
"""

from collections import Counter
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, ReviewNote
from app.schemas import NoteIn, NoteOut, NoteSummaryOut, NoteTagCount
from app.services.auth import current_user
from app.timeutil import today_cn

router = APIRouter(prefix="/api/note", tags=["复盘笔记"])

# 笔记日期的合理区间。下限 2020-01-01（本站在此之前没有数据可复盘）；
# 上限是「今天 + 7 天」——留出一周是因为前端允许提前写次日 / 下周计划。
# ⚠️ 上限跟着「今天」走，**不能写成固定常量，也就不能用 `Path(le=...)`** ——
# 那样要么写死一个未来日期、要么直接禁掉明天，所以用手写校验（见 _check_date）。
NOTE_MIN_DATE = date(2020, 1, 1)
NOTE_FUTURE_DAYS = 7


def _check_date(trade_date: date) -> None:
    """把笔记日期限在合理区间，越界抛 400（两个端点都要过）。

    不卡范围的话，一个登录用户能把 `0001-01-01` 到 `9999-12-31` 每天都写一条、
    每条最多 4 万字 ×2 段 —— 光靠一个人就能把库刷满（路径参数 `date` 本身不设界）。
    """
    latest = today_cn() + timedelta(days=NOTE_FUTURE_DAYS)
    if trade_date < NOTE_MIN_DATE or trade_date > latest:
        raise HTTPException(
            status_code=400,
            detail=(
                f"日期只能填 {NOTE_MIN_DATE} 到 {latest}（今天 + {NOTE_FUTURE_DAYS} 天）之间，"
                f"收到「{trade_date}」"
            ),
        )


def _clean_tags(values: list[str] | None) -> list[str]:
    """标签清洗：去空白、去重、单条 ≤20 字、最多 20 个（防刷库）。"""
    result: list[str] = []
    for raw in values or []:
        tag = str(raw).strip()[:20]
        if tag and tag not in result:
            result.append(tag)
        if len(result) >= 20:
            break
    return result


def _fill(row: ReviewNote, payload: NoteIn) -> None:
    row.market_view = payload.market_view
    row.next_plan = payload.next_plan
    row.mainlines = _clean_tags(payload.mainlines)
    row.mistake_tags = _clean_tags(payload.mistake_tags)
    row.good_tags = _clean_tags(payload.good_tags)
    row.watch_points = (payload.watch_points or "").strip() or None
    row.plan_ids = [int(item) for item in payload.plan_ids][:50]


@router.get("/summary", response_model=NoteSummaryOut)
def note_summary(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$", description="YYYY-MM"),
    user: AppUser = Depends(current_user),
    session: Session = Depends(get_db),
) -> NoteSummaryOut:
    """某月的笔记标签统计（FEAT-JOURNAL-STRUCT）：主线 / 做错 / 做对各出现多少次。

    只统计**当前用户**；观察点取最近 10 条非空文本，供月末回看。
    """
    try:
        start = datetime.strptime(month, "%Y-%m").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"月份格式应为 YYYY-MM：{month}") from exc
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    rows = list(
        session.scalars(
            select(ReviewNote)
            .where(
                ReviewNote.user_id == user.id,
                ReviewNote.trade_date >= start,
                ReviewNote.trade_date <= end,
            )
            .order_by(ReviewNote.trade_date.desc())
        )
    )

    def counts(field: str) -> list[NoteTagCount]:
        counter: Counter[str] = Counter()
        for row in rows:
            for tag in getattr(row, field) or []:
                if tag:
                    counter[str(tag)] += 1
        return [
            NoteTagCount(tag=tag, count=count)
            for tag, count in counter.most_common()
        ]

    watch_points = [
        (row.watch_points or "").strip()
        for row in rows
        if (row.watch_points or "").strip()
    ][:10]

    return NoteSummaryOut(
        month=month,
        notes=len(rows),
        mainlines=counts("mainlines"),
        mistakes=counts("mistake_tags"),
        goods=counts("good_tags"),
        watch_points=watch_points,
        plan_links=sum(1 for row in rows if row.plan_ids),
    )


@router.get("/{trade_date}", response_model=NoteOut)
def get_note(
    trade_date: date,
    user: AppUser = Depends(current_user),
    session: Session = Depends(get_db),
) -> NoteOut:
    """取某天的笔记。没写过就返回空模板，前端不必区分「没写过」与「写空了」。"""
    _check_date(trade_date)
    row = session.get(ReviewNote, (user.id, trade_date))
    if row is None:
        return NoteOut(
            trade_date=trade_date, market_view=None, next_plan=None, updated_at=None
        )
    return NoteOut.model_validate(row)


@router.put("/{trade_date}", response_model=NoteOut)
def save_note(
    trade_date: date,
    payload: NoteIn,
    user: AppUser = Depends(current_user),
    session: Session = Depends(get_db),
) -> NoteOut:
    _check_date(trade_date)
    row = session.get(ReviewNote, (user.id, trade_date))
    if row is None:
        row = ReviewNote(user_id=user.id, trade_date=trade_date)
        session.add(row)
    _fill(row, payload)
    try:
        session.commit()
    except IntegrityError:
        # 并发保存同一天笔记（双击 / 两个标签页）：两个 PUT 都在上面「查不到」→ 都
        # INSERT，后者撞主键 `(user_id, trade_date)`。结果与串行一致（行已存在），
        # 重新取回来按「更新」再写一次，不冒 500（2026-10-22 加，与次日计划同处理）。
        session.rollback()
        row = session.get(ReviewNote, (user.id, trade_date))
        if row is None:
            # 理论上不会走到：主键冲突说明这一行已经在库里
            raise
        _fill(row, payload)
        session.commit()
    session.refresh(row)
    return NoteOut.model_validate(row)
