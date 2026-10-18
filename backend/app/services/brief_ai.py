"""飞书简报的 AI 点评（OpenAI 兼容接口）。

**只喂库里已有的数字**：简报本身 + 恐贪 / 宽度 / ERP / 期指基差 / 涨跌停读数，
不让模型自由发挥。原因很直接 —— 它不知道当天的新闻，放开写只会编。

**失败一律返回 None**：超时、4xx/5xx、空回复都只记一条 warning，简报照发。
点评是增强项，不能把一次成功的推送拖成失败，也不能把尾部链标成 partial。

**日线没采全时不喂派生读数**：那些指标在覆盖率低的日子会算出偏差极大的值
（见 `scan_patterns._require_bars` 的注释），模型会把它们当真。此时 prompt 里
明确写「今日日线未采全」，只允许引用简报里已有的数字。
"""

import logging
from datetime import date

import requests
from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.db import session_scope
from app.models import (
    BreadthDaily,
    ErpDaily,
    FearGreedDaily,
    FuturesBasisDaily,
    MarketSentiment,
    StockDaily,
    StockUniverse,
)
from app.services.coverage import coverage_ok
from app.services.fear_greed import build_series
from app.services.fear_greed import latest as fear_latest

logger = logging.getLogger(__name__)

# 固定输出结构。不给买卖建议、不给目标价、不许引用未给出的信息。
_SYSTEM_PROMPT = """你是 A 股收盘复盘助手。只能根据用户给出的数字写点评，不得编造个股、新闻、政策、传闻或数据；不得给出买卖建议、目标价、仓位建议。
请写 3~5 句纯文本，严格按这个顺序：
1. 情绪处在什么阶段（必须引用给定数字，如恐贪分数、涨停数、宽度占比）；
2. 主线题材是否延续（只能提简报里已经出现的板块/题材名，不得新增）；
3. 明日最需要盯的一个风险（只依据给定数字，不得编造事件）。
要求：不用表格、不用标题、不用分点编号、不用 Markdown 加粗；总字数不超过 {limit} 字。"""


def _coverage_ok(trade_date: date) -> bool:
    """当天日线是否采全。口径与 `scan_patterns._require_bars` 一致（同一个纯函数）。"""
    with session_scope() as session:
        pool = session.scalar(select(func.count()).select_from(StockUniverse)) or 0
        count = (
            session.scalar(
                select(func.count())
                .select_from(StockDaily)
                .where(
                    StockDaily.trade_date == trade_date,
                    StockDaily.pct_chg.is_not(None),
                )
            )
            or 0
        )
    return coverage_ok(count, pool)


def _readings(trade_date: date) -> str:
    """把库里的派生读数写成几行纯文本。没有的项直接省略，不写「—」。"""
    lines: list[str] = []
    with session_scope() as session:
        senti = session.get(MarketSentiment, trade_date)
        if senti is not None:
            parts = []
            if senti.limit_up_count is not None:
                parts.append(f"涨停 {senti.limit_up_count} 家")
            if senti.limit_down_count is not None:
                parts.append(f"跌停 {senti.limit_down_count} 家")
            if senti.broken_rate is not None:
                parts.append(f"炸板率 {senti.broken_rate:.1f}%")
            if senti.max_consecutive is not None:
                parts.append(f"最高连板 {senti.max_consecutive} 板")
            if senti.yesterday_limit_today_avg is not None:
                parts.append(f"昨日涨停今日均涨 {senti.yesterday_limit_today_avg:+.2f}%")
            if parts:
                lines.append("涨跌停：" + "、".join(parts))

        breadth = session.get(BreadthDaily, trade_date)
        if breadth is not None:
            parts = []
            for label, value in (
                ("站上 MA20", breadth.above_ma20),
                ("站上 MA60", breadth.above_ma60),
                ("站上 MA250", breadth.above_ma250),
            ):
                if value is not None:
                    parts.append(f"{label} {value:.1f}%")
            if parts:
                lines.append("宽度：" + "、".join(parts))

        erp = session.get(ErpDaily, trade_date)
        if erp is not None and erp.erp is not None:
            extra = []
            if erp.pe_ttm is not None:
                extra.append(f"PE {erp.pe_ttm:.2f} 倍")
            if erp.bond_yield is not None:
                extra.append(f"10Y 国债 {erp.bond_yield:.2f}%")
            suffix = f"（{'、'.join(extra)}）" if extra else ""
            lines.append(f"ERP：{erp.erp:.2f} 个百分点{suffix}")

        basis = session.scalars(
            select(FuturesBasisDaily)
            .where(FuturesBasisDaily.trade_date == trade_date)
            .order_by(FuturesBasisDaily.code)
        ).all()
        parts = [
            f"{row.code} 年化 {row.annual_pct:+.2f}%"
            for row in basis
            if row.annual_pct is not None
        ]
        if parts:
            lines.append("期指基差：" + "、".join(parts))

        # 恐贪分数是滚动分位算出来的，需要最近约 250 个交易日的历史。
        # 必须限定 `<= trade_date`：补发历史某天的简报时，不能拿之后的行情
        # 去算那天的分数（`build_series` 只看传入的序列）。
        rows = session.scalars(
            select(FearGreedDaily)
            .where(FearGreedDaily.trade_date <= trade_date)
            .order_by(FearGreedDaily.trade_date.desc())
            .limit(260)
        ).all()
    rows.reverse()
    fear = fear_latest(build_series(rows))
    if fear is not None and fear.get("score") is not None:
        text = f"恐贪指数：{fear['score']:.1f}（{fear.get('level') or '—'}）"
        if fear.get("change") is not None:
            text += f"，较前值 {fear['change']:+.1f}"
        lines.append(text)

    return "\n".join(lines) if lines else "（库里没有可用的派生读数）"


def ai_comment(
    trade_date: date, brief_md: str, settings: Settings | None = None
) -> str | None:
    """生成一段点评；任何失败都返回 None，**不抛异常**。"""
    settings = settings or get_settings()
    if not settings.llm_brief_enabled:
        return None
    base = settings.llm_base_url.strip().rstrip("/")
    key = settings.llm_api_key.strip()
    model = settings.llm_model.strip()
    if not (base and key and model):
        logger.warning("AI 点评已开启，但 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 不全，跳过")
        return None

    if _coverage_ok(trade_date):
        readings = _readings(trade_date)
    else:
        readings = "今日日线未采全：禁止引用任何派生读数，只依据简报中明确出现的数字。"

    limit = max(1, settings.llm_max_chars)
    prompt = (
        f"日期：{trade_date.isoformat()}\n\n"
        f"【当日简报】\n{brief_md}\n\n"
        f"【派生读数】\n{readings}\n\n"
        "请按系统要求写点评。"
    )
    try:
        response = requests.post(
            f"{base}/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": _SYSTEM_PROMPT.format(limit=limit),
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 500,
            },
            timeout=settings.llm_timeout,
        )
        response.raise_for_status()
        payload = response.json()
        content = (
            (payload.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        )
        content = " ".join(str(content).split()).strip()
        if not content:
            logger.warning("AI 点评返回空内容，简报照发")
            return None
        if len(content) > limit:
            content = content[: limit - 1].rstrip() + "…"
        return content
    except Exception as exc:  # noqa: BLE001 —— 点评失败绝不能影响推送
        logger.warning("AI 点评失败（简报照发）：%s", exc)
        return None
