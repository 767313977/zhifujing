"""S6.2：Research DB / User DB 的表归属定义（单一事实来源）。

拆分原则：
- **Research DB（只读）**：行情、历史、涨停池、板块、情绪、竞价、交易日历、
  采集日志、研究结果、前瞻/C0 相关数据；
- **User DB（可写）**：账号、会话、邀请码、自选、复盘笔记、风控偏好、
  交易日志/做 T、交易计划、查票快照。

⚠️ 这是**表级**归属；模块级不能简单按名字归类（例如 `api/admin.py` 同时写
用户表与触发研究采集）。逐表清单与写路径见
`reports/s6/s62/db-table-ownership.csv` 与 `db-write-paths.json`。
"""

from __future__ import annotations

from sqlalchemy import Table

RESEARCH = "research"
USER = "user"

#: 10 张用户表（与 models.py 一一对应）
USER_MODEL_NAMES: tuple[str, ...] = (
    "AppUser",
    "InviteCode",
    "UserSession",
    "Watchlist",
    "ReviewNote",
    "UserRiskPref",
    "TradeJournal",
    "TradeT",
    "TradePlan",
    "AnalysisSnapshot",
)

#: 明确禁止在 User DB 出现的表（研究/采集结果）
RESEARCH_ONLY_HINTS: tuple[str, ...] = (
    "stock_daily",
    "limit_pool",
    "sector_",
    "market_sentiment",
    "auction_snapshot",
    "trade_calendar",
    "collect_log",
    "forward_",
    "pattern_hit",
)


def user_table_objects() -> list[Table]:
    """返回用户表的 SQLAlchemy Table 列表（导入 models 触发注册）。"""
    from app import models

    return [getattr(models, name).__table__ for name in USER_MODEL_NAMES]


def table_domain(table_name: str) -> str:
    """按表名返回所属域；未知表返回 `research`（默认更严格：不写用户库）。"""
    from app import models

    for name in USER_MODEL_NAMES:
        model = getattr(models, name)
        if model.__tablename__ == table_name:
            return USER
    return RESEARCH
