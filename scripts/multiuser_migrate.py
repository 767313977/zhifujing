"""把一个「单用户」的库改成多用户（会员）结构。

设计见设计文档 §8.69。**一次性脚本：本地库与云端库各跑一次**，重复跑是安全的
（幂等：已经改好的表会跳过，管理员账号已存在就复用、不改密码）。

它做三件事：

1. **先备份数据库文件**（用 sqlite3 的在线备份 API，理由见 `backup_database`）
2. 建 3 张会员表，并确保管理员账号存在
3. 把 `watchlist` / `review_note` 改成复合主键 `(user_id, ...)`，现有数据
   **全部挂到管理员名下**（本来就是一个人用的）

## 本机与云端都要跑

    python scripts/multiuser_migrate.py --admin-user ming     # 密码自动生成并打印
    python scripts/multiuser_migrate.py --admin-user ming --admin-password 'xxx'

⚠️ **云端跑之前先停服务**（`sudo systemctl stop fupan`，跑完再 start）：重建那一下要拿
写锁，撞上正在跑的采集就是 `database is locked`。本机不用管（定时任务本来就是关的）。

## 安全网有哪几层（没有 --dry-run，它是**故意**没有的）

- 动库之前**先备份**（备份失败/写不进去就直接退出，不往下走）
- 重建在**一个事务**里：建新表 → 搬数据 → 删旧表 → 改名，任何一步出错整体回滚
- 搬完比对**行数**（少一行都不接受）
- 再比对**列集合与模型**、以及主键里到底有没有 `user_id`
- 上面任何一条不过 → 回滚，库保持原样，退出码非 0

⚠️ 曾经写过一个 `--dry-run`，但它为了「预览」会在事务外先建表、先建管理员账号 ——
也就是**「只报告」其实改了库**。那种「以为没动、其实动了」比没有这个参数危险得多，
所以删掉了；想看它要做什么，备份完那份日志里的逐表说明就够了。
"""

import argparse
import secrets
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.schema import CreateTable  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import engine, session_scope  # noqa: E402
from app.models import (  # noqa: E402
    AppUser,
    Base,
    InviteCode,
    ReviewNote,
    UserSession,
    Watchlist,
)
from fastapi import HTTPException  # noqa: E402

from app.services.auth import hash_password, validate_password  # noqa: E402

# 要从单列主键改成复合主键的两张表。
#
# ⚠️ 这里存的是 **Table 对象**（`__table__`）而不是模型类，别改回去：
# `Watchlist.name` 是**那个列**（ORM 属性），不是类名 —— 传类进来后 `model_table.name`
# 拿到的是列的 InstrumentedAttribute，拿它去查表名会报
# `Error binding parameter 1: type 'InstrumentedAttribute' is not supported`。
# 顺带还有个更隐蔽的后果：`verify_schema` 里靠 `in COMPOSITE_PK_TABLES` 判断
# 「要不要查主键」，混着类型传的话那个判断永远为假，主键检查会被**静默跳过**。
COMPOSITE_PK_TABLES = (Watchlist.__table__, ReviewNote.__table__)


def log(message: str) -> None:
    print(f"==> {message}", flush=True)


def backup_database(db_path: Path) -> Path:
    """用 sqlite3 的**在线备份 API** 复制一份，返回副本路径。

    ⚠️ **不能直接 `shutil.copy` 那个 .db 文件**：库跑在 WAL 模式，最近的写入
    可能还在 `-wal` 里、没并进主库文件。只拷 `.db` 会得到一份「看着正常、
    其实少了最近数据」的备份 —— 那比没有备份更危险，因为出事时你以为救得回来。
    `Connection.backup()` 是按页复制的，会把 WAL 里的内容一起读进来。

    也不用 `mode=ro` 打开：WAL 库的只读打开要能访问 `-shm`，条件不满足时反而会
    直接打不开。这里就在同一台机器上，普通连接即可。

    ⚠️ 副本名写成 `<库名>-bak-<时间戳>`（即 `fupan.db-bak-...`）是**故意的**：
    `.gitignore` 里有 `backend/data/*.db-*`，正好盖住它。写成常见的
    `fupan.db.bak-...` 两边都匹配不上 —— 一个 405 MB 的文件就会出现在 `git status`
    里等人误提交。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = db_path.parent / f"{db_path.name}-bak-{stamp}"
    source = sqlite3.connect(db_path)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return dest


def table_columns(conn: sqlite3.Connection, table: str) -> dict[str, int]:
    """`{列名: 主键里的序号}`（0 表示不是主键）。表不存在时返回空 dict。"""
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {row[1]: row[5] for row in rows}


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def create_sql(table, new_name: str) -> str:
    """从**模型**生成建表语句（表名换成 `new_name`）。见模块说明里那条理由。"""
    ddl = str(CreateTable(table).compile(engine))
    marker = f"CREATE TABLE {table.name} "
    if marker not in ddl:
        raise RuntimeError(f"没能从模型生成建表语句，实际得到：{ddl}")
    return ddl.replace(marker, f"CREATE TABLE {new_name} ", 1)


def rebuild_table(conn: sqlite3.Connection, table, admin_id: int) -> int:
    """把一张表改成复合主键，返回搬完的行数。已经有 `user_id` 列就原样返回。"""
    name = table.name
    if not table_exists(conn, name):
        log(f"{name}：表不存在（新库会由 create_all 直接按新结构建），跳过")
        return 0

    old_columns = table_columns(conn, name)
    before = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    if "user_id" in old_columns:
        log(f"{name}：已经是多用户结构（{before} 行），跳过")
        return before

    # 只搬两边都有的列：老库可能少一列（比如 watchlist.name 是后来才加的）
    new_columns = [column.name for column in table.columns]
    moved = [c for c in new_columns if c in old_columns and c != "user_id"]

    tmp = f"{name}__new"
    conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')
    conn.execute(create_sql(table, tmp))
    columns_sql = ", ".join(f'"{c}"' for c in moved)
    conn.execute(
        f'INSERT INTO "{tmp}" (user_id, {columns_sql}) '
        f'SELECT ?, {columns_sql} FROM "{name}"',
        (admin_id,),
    )
    conn.execute(f'DROP TABLE "{name}"')
    conn.execute(f'ALTER TABLE "{tmp}" RENAME TO "{name}"')

    after = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    if after != before:
        # 抛出去让事务回滚 —— 少一行都不接受
        raise RuntimeError(f"{name} 搬数据前后行数不一致：{before} → {after}")
    log(f"{name}：已重建，{after} 行全部归到管理员 id={admin_id} 名下（搬了 {moved}）")
    return after


def verify_schema(conn: sqlite3.Connection) -> list[str]:
    """搬完之后逐表比对「库里的列」与「模型里的列」，返回不一致的说明。"""
    problems: list[str] = []
    tables = (
        *COMPOSITE_PK_TABLES,
        AppUser.__table__,
        InviteCode.__table__,
        UserSession.__table__,
    )
    for model_table in tables:
        name = model_table.name
        if not table_exists(conn, name):
            problems.append(f"{name}：表不存在")
            continue
        actual = table_columns(conn, name)
        expected = [column.name for column in model_table.columns]
        missing = [c for c in expected if c not in actual]
        extra = [c for c in actual if c not in expected]
        if missing:
            problems.append(f"{name}：缺列 {missing}")
        if extra:
            problems.append(f"{name}：多出模型里没有的列 {extra}")
        # 复合主键必须真的生效 —— 这是这次迁移的全部目的
        if model_table in COMPOSITE_PK_TABLES:
            pk = sorted([c for c, order in actual.items() if order], key=actual.get)
            if "user_id" not in pk:
                problems.append(f"{name}：主键里没有 user_id（实际主键 {pk}）")
    return problems


def ensure_admin(username: str, password: str) -> tuple[int, str | None]:
    """确保管理员存在，返回 (id, 明文密码或 None)。已存在则不改密码。"""
    with session_scope() as db:
        user = db.scalar(select(AppUser).where(AppUser.username == username))
        if user is not None:
            if not user.is_admin:
                user.is_admin = True
                log(f"账号 {username} 已存在，已把它提升为管理员")
            else:
                log(f"管理员 {username} 已存在（id={user.id}），密码保持不变")
            return user.id, None
        if not password:
            password = secrets.token_urlsafe(12)
        user = AppUser(
            username=username, password_hash=hash_password(password), is_admin=True
        )
        db.add(user)
        db.flush()
        return user.id, password


def main() -> int:
    parser = argparse.ArgumentParser(description="把库改成多用户（会员）结构")
    parser.add_argument("--admin-user", default="admin", help="管理员用户名")
    parser.add_argument(
        "--admin-password", default="", help="管理员密码（不给就随机生成并打印一次）"
    )
    args = parser.parse_args()

    db_path = Path(get_settings().db_path)
    if not db_path.exists():
        log(f"库不存在：{db_path}（先在别处跑一次采集把库建起来）")
        return 1
    log(f"库：{db_path}（{db_path.stat().st_size / 1e6:.1f} MB）")

    dest = backup_database(db_path)
    log(f"备份：{dest.name}（{dest.stat().st_size / 1e6:.1f} MB）")

    # 建缺的表：新库会直接按新结构建；老库上已存在的表不受影响
    # （补列/改主键由下面那个事务负责，不走 init_db 的自动补列）
    Base.metadata.create_all(engine)

    if args.admin_password:
        # 命令行传进来的密码也要过一遍长度校验（2026-10-10 加）：不走这道闸就能建出
        # 一个短密码的管理员 —— 而管理员能触发采集、白烧 iFinD 配额，恰恰是最不该
        # 被爆破的那个账号。随机生成的密码由 MIN_PASSWORD_LENGTH 兜底，不查。
        try:
            validate_password(args.admin_password)
        except HTTPException as exc:
            log(f"管理员密码不合规：{exc.detail}")
            return 1

    admin_id, plain_password = ensure_admin(args.admin_user, args.admin_password)

    # ⚠️ `PRAGMA foreign_keys` 在事务内是**空操作**，必须在 BEGIN 之前设。
    # 而 sqlite3 连接默认会自己开事务，所以先把它设成 None（自己管事务），再置 pragma。
    #
    # ⚠️ 用**原生 sqlite3 连接**而不是 app 的 SQLAlchemy engine，是因为这个 pragma 是
    # **连接级**的：在 engine 的连接上关掉它，那条连接会带着「不检查外键」的状态回到
    # 连接池，之后被应用随机取去用 —— 那等于全站静默失去外键校验。别把它「简化」成
    # engine.connect()。
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        try:
            for model_table in COMPOSITE_PK_TABLES:
                rebuild_table(conn, model_table, admin_id)
            problems = verify_schema(conn)
            if problems:
                conn.execute("ROLLBACK")
                log("校验没通过，已回滚（库没有被改动）：")
                for item in problems:
                    print(f"    - {item}")
                return 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    log("完成。")
    print(f"    管理员：{args.admin_user}（id={admin_id}）")
    if plain_password:
        print(f"    初始密码：{plain_password}")
        print("    ⚠️ 这串只显示这一次，先记下来（忘了可以让管理员重新生成，或在网页里改）。")
    print("    下一步：登录后去「数据管理」页生成邀请码，发给要用的人。")
    print("    ⚠️ 云端那份库要**单独再跑一次**这个脚本（本地改了云端不会自动变）。")
    print(f"    备份留在 {dest}，确认没问题之后可以删。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
