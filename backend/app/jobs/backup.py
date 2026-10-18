"""数据库定时备份：在线备份 API + 本机保留 N 份 + 可选同步目录。

为什么不能直接 `shutil.copy` 那个 `.db`：库跑在 WAL 模式，最近的写入可能还在
`-wal` 里、没并进主库。只拷 `.db` 会得到一份「看着正常、其实少了最近数据」的
备份 —— 那比没有备份更危险，因为出事时你以为救得回来。`sqlite3.Connection.backup()`
是按页复制的，会把 WAL 里的内容一起读进来。

同样的函数原本只存在于 `scripts/multiuser_migrate.py`（一次性迁移脚本）。这里
独立实现一份给定时任务用，**不改那个脚本**（它是一次性工具，改它没有收益）。

备份文件命名 `<库名>-bak-<时间戳>`（如 `fupan.db-bak-20261018-231000`）：
`.gitignore` 里的 `backend/data/*.db-*` 正好盖住它，不会出现在 `git status` 里。
"""

import logging
import shutil
import sqlite3
from datetime import date, datetime
from pathlib import Path

from app.config import Settings, get_settings
from app.db import session_scope
from app.models import CollectLog

logger = logging.getLogger(__name__)

#: `collect_log` 里的任务名（数据管理页据此显示备份结果）
TASK = "db_backup"


def backup_database(db_path: Path) -> Path:
    """用在线备份 API 复制一份，返回副本路径。

    文件名带**微秒**：正式任务每天只跑一次，秒级就够；但手动重跑 / 测试时
    同一秒内连续备份会互相覆盖（`sqlite3.connect` 会直接复用同名文件），
    加微秒后每次都是独立副本，`prune` 的「保留 N 份」才有意义。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = db_path.parent / f"{db_path.name}-bak-{stamp}"
    source = sqlite3.connect(db_path)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return dest


def prune(db_path: Path, keep: int) -> list[Path]:
    """只保留最近 `keep` 份备份，返回被删掉的文件。"""
    if keep < 1:
        return []
    pattern = f"{db_path.name}-bak-*"
    files = sorted(
        db_path.parent.glob(pattern),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed: list[Path] = []
    for old in files[keep:]:
        try:
            old.unlink()
            removed.append(old)
        except OSError:
            logger.warning("删除旧备份失败：%s", old, exc_info=True)
    return removed


def _record(status: str, message: str) -> None:
    with session_scope() as session:
        session.add(
            CollectLog(
                trade_date=date.today(),
                task=TASK,
                status=status,
                message=message,
            )
        )


def run(settings: Settings | None = None) -> dict:
    """备份一次。失败时记一条 `failed` 并**抛出**（让调度日志也留下堆栈）。"""
    settings = settings or get_settings()
    db_path = settings.db_path
    if not db_path.is_file():
        raise FileNotFoundError(f"数据库文件不存在：{db_path}")

    try:
        dest = backup_database(db_path)
        removed = prune(db_path, settings.backup_keep)
        size_mb = dest.stat().st_size / 1e6

        synced_to: str | None = None
        sync_dir = settings.backup_sync_dir.strip()
        sync_note: str | None = None
        if sync_dir:
            # 同步失败不算备份失败：本机那一份已经成功了，同步只是额外一层。
            try:
                target_dir = Path(sync_dir)
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, target_dir / dest.name)
                synced_to = str(target_dir / dest.name)
                # 同步目录也按同一份 keep 清理，否则它会无限增长（本机删了它还留着）
                remote_files = sorted(
                    target_dir.glob(f"{db_path.name}-bak-*"),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
                for old in remote_files[settings.backup_keep :]:
                    old.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001 - 见上
                sync_note = "同步失败（详见日志）"
                logger.warning("备份同步到 %s 失败（本机备份已成功）：%s", sync_dir, exc)
        else:
            # 没配 BACKUP_SYNC_DIR 时备份与主库在同一块盘上 —— 盘坏了主库备份一起丢。
            # 在记录里写明白，告警（jobs/alerts.py）也会据此提醒。
            sync_note = "未配置异地同步（备份与主库同盘）"

        message = f"{dest.name}（{size_mb:.0f} MB），保留 {settings.backup_keep} 份"
        if removed:
            message += f"，清理 {len(removed)} 份"
        if synced_to:
            message += "，已同步"
        elif sync_note:
            message += f"，{sync_note}"
        _record("ok", message)
        logger.info("数据库备份完成：%s", message)
        return {
            "path": str(dest),
            "size_mb": round(size_mb, 1),
            "removed": len(removed),
            "synced_to": synced_to,
        }
    except Exception as exc:  # noqa: BLE001 - 记录后原样抛出
        try:
            _record("failed", f"备份失败：{exc}")
        except Exception:  # noqa: BLE001 - 日志表也写不进时只能打日志
            logger.exception("写备份失败记录失败")
        raise
