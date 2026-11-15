"""请求级研究数据写入边界（S6.12 P0-1）。

个人站是 SQLite 单库，研究数据与用户数据在同一个文件里，**没有表级账号权限**。
这里用最小的代码边界实现等效隔离：

- 后台采集 / CLI / 定时任务不在 HTTP 请求上下文里 → 研究数据写入照常；
- 普通 HTTP 请求默认**禁止**写研究数据（`stock_daily` / `sector_member` /
  `ifind_usage` / `stock_event_cache` / `stock_financial` 等）；
- `/api/admin/*` 是管理员显式操作，保留写权限（仍受登录与管理员校验）。

⚠️ 这不是数据库权限，不能替代 OS/库级隔离；它只约束本进程内的调用路径，
并由负向测试（`tests/test_s6_12_write_boundary.py`）守住。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

#: 当前是否在 HTTP 请求内（后台线程 / 调度器 / CLI 为 False）
_in_http_request: ContextVar[bool] = ContextVar("fupan_in_http_request", default=False)
#: 当前请求是否被允许写研究数据（仅管理员路径）
_research_write_allowed: ContextVar[bool] = ContextVar(
    "fupan_research_write_allowed", default=True
)


@contextmanager
def http_request_scope(*, allow_research_write: bool = False) -> Iterator[None]:
    """包住一次 HTTP 请求；默认禁止研究数据写入。"""
    request_token = _in_http_request.set(True)
    write_token = _research_write_allowed.set(bool(allow_research_write))
    try:
        yield
    finally:
        _research_write_allowed.reset(write_token)
        _in_http_request.reset(request_token)


def in_http_request() -> bool:
    return _in_http_request.get()


def research_write_allowed() -> bool:
    """后台（非请求）恒为 True；请求内只有管理员路径为 True。"""
    if not _in_http_request.get():
        return True
    return _research_write_allowed.get()


def user_research_write_blocked() -> bool:
    return not research_write_allowed()
