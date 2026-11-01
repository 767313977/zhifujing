"""让 `pytest` 能直接 ``from app...``，并把全站的**全局库**指到临时文件。

⚠️ 顺序很重要：`app.db` 在 import 时就会按 `settings.db_path` 建 engine，所以
`DB_PATH` 必须在 import app **之前**设好。测试里凡是走全局 `session_scope()` /
`SessionLocal` 的路径（例如 `stock_phase._trade_day_index`、`api/patterns.hits` 里的
阶段判定）都会落到这个库上 —— 不隔离的话：

- 本机能过、CI 空库报 `no such table: trade_calendar`（2026-10-23、11-01 的 CI #234 /
  #240 都是这一类：测试打了个真库）；
- 本机跑测试时，任何走 `session_scope()` 的写操作会**写进本机的 `backend/data/fupan.db`**。

隔离之后两边跑的是同一个空库（表已由 `init_db()` 建好），上面两个问题一起消失。
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_TEST_DB_DIR: Path | None = None


def _point_db_at_temp() -> None:
    """把 `DB_PATH` 指到本进程专属的临时库（调用方显式设了就不覆盖）。"""
    global _TEST_DB_DIR
    if os.environ.get("DB_PATH"):
        return
    _TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="fupan-test-db-"))
    os.environ["DB_PATH"] = str(_TEST_DB_DIR / "test.db")


_point_db_at_temp()

from app.db import init_db  # noqa: E402  （必须在设置 DB_PATH 之后）

init_db()


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """跑完把临时库删掉（Windows 上 WAL 文件可能还占着，删不掉就忽略）。"""
    if _TEST_DB_DIR is not None:
        shutil.rmtree(_TEST_DB_DIR, ignore_errors=True)
