"""SQLite 连接与会话管理。"""

import logging
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

engine = create_engine(
    f"sqlite:///{settings.db_path}",
    # FastAPI 请求线程与采集线程共用 engine
    connect_args={"check_same_thread": False},
    future=True,
)


@event.listens_for(engine, "connect")
def _enable_sqlite_pragmas(dbapi_conn, _record) -> None:
    """开启 WAL：采集在写的同时接口仍可读，避免 database is locked。"""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    from app import models  # noqa: F401  触发模型注册
    from app.models import Base

    Base.metadata.create_all(engine)
    _add_missing_columns(Base)
    _add_missing_indexes(Base)


def _add_missing_columns(base) -> list[str]:
    """给**已存在**的表补上模型里新增的列。

    `create_all` 只建缺的表，**不会给已有的表加列**：模型里新加一个字段
    （比如 `market_sentiment.up5_count`），老库上不会出现，接口一读就 500。
    项目没引入 Alembic，也不想为一张个人自用的库上那一整套，所以这里做最小
    可用的迁移：**只 ADD COLUMN，且只加可空列**。

    为什么不自动处理删列/改类型：那两件事有数据丢失风险，值得人工看一眼
    （而且极少发生）。加列是纯增量、幂等的，才适合自动化。

    返回补上的列名列表，只用于开日志。
    """
    from sqlalchemy import inspect, text

    added: list[str] = []
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table in base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue  # create_all 刚建的（或这张表还没建），列肯定是全的
            existing = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                default_sql = _column_default_sql(column)
                if not column.nullable and default_sql is None:
                    # SQLite 的 ADD COLUMN 不带 DEFAULT 时老行取 NULL，而模型声明的可能是
                    # 非空列（如 `Mapped[datetime]`）—— 造出与模型矛盾的 NULL 行，比跳过更糟。
                    # 走到这里的两种情形：① 列根本没默认值；② 默认值是 `default=_now` 这类
                    # **可调用**对象 —— SQLite 的 DEFAULT 只收**常量**，函数默认值写不进去。
                    # 都跳过并告警，让启动继续，留人工迁移。
                    logger.warning(
                        "表 %s 新增了非空列 %s，但没有可用的常量默认值（SQLite 的 "
                        "ADD COLUMN 只接受常量 DEFAULT，`default=_now` 这类函数默认值不行），"
                        "需要手工迁移",
                        table.name,
                        column.name,
                    )
                    continue
                column_type = column.type.compile(engine.dialect)
                # 有可用常量默认值时**显式带上 DEFAULT**：不带的话老行一律是 NULL
                #（对非空列不可接受，对可空列也会让「缺省值」语义丢失）。2026-10-10 修。
                clause = f" DEFAULT {default_sql}" if default_sql is not None else ""
                connection.execute(
                    text(
                        f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" '
                        f"{column_type}{clause}"
                    )
                )
                added.append(f"{table.name}.{column.name}")
    if added:
        logger.info("已为老库补上新增列：%s", "、".join(added))
    return added


def _column_default_sql(column) -> str | None:
    """能写进 `ADD COLUMN ... DEFAULT ?` 的**常量**默认值文本；拿不到常量就返回 None。

    SQLite 的 `ADD COLUMN` 只接受常量默认值（不接受函数 / 子查询），所以：
    - `server_default`（`text()` 或字符串）：直接用它的 SQL 文本
    - Python 侧 `default` 且是**标量常量**（int / float / str / bool）：渲染成字面量
    - 可调用对象（`default=_now`）：无法当常量 → None，由调用方决定跳过并告警

    只认这几种常见类型，其余（`default=func.now()` 之类）一律 None，交人工处理 ——
    自动迁移宁可不做，也不要写出一条 SQLite 执行不了的 DDL 让整个启动失败。
    """
    if column.server_default is not None:
        return str(column.server_default.arg)
    default = column.default
    if default is None:
        return None
    arg = getattr(default, "arg", None)
    if arg is None or callable(arg):
        return None
    if isinstance(arg, bool):  # 必须在 int 之前判：bool 是 int 的子类
        return "1" if arg else "0"
    if isinstance(arg, (int, float)):
        return repr(arg)
    if isinstance(arg, str):
        return "'" + arg.replace("'", "''") + "'"
    return None


def _add_missing_indexes(base) -> list[str]:
    """给**已存在**的表补上模型里新增的索引。

    ⚠️ 为什么需要它：`create_all` 只给「还没有的表」建索引，已存在的表**连索引一起
    跳过** —— 模型里新加的复合索引（如 `stock_daily.code+trade_date`）在老库上不会
    自动出现，`EXPLAIN` 仍走全表扫描/临时 B 树，等于改了没用（2026-10-10 实测确认）。
    项目没上 Alembic，所以这里做和 `_add_missing_columns` 一样的最小迁移：**只建缺失的
    索引**（SQLite 索引名全局唯一，按名字判断），纯增量、幂等。

    返回补上的索引名列表，只用于开日志。删索引/改索引有意不自动做（同 `_add_missing_columns`
    的取舍：有风险的事留人工）。
    """
    from sqlalchemy import inspect
    from sqlalchemy.schema import CreateIndex

    added: list[str] = []
    inspector = inspect(engine)
    with engine.begin() as connection:
        existing = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        for table in base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue  # create_all 刚建的（或这张表还没建），索引肯定已随之建好
            for index in table.indexes:
                if index.name in existing:
                    continue
                connection.execute(CreateIndex(index))
                existing.add(index.name)
                added.append(index.name)
    if added:
        logger.info("已为老库补上新增索引：%s", "、".join(added))
    return added


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """写操作使用：自动提交，异常回滚。"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入使用。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def upsert(session: Session, model, rows: list[dict]) -> int:
    """按主键 upsert，保证重复采集幂等。

    放在这里而不是某个采集模块里：多个采集模块都要用，
    放在其中任何一个都会造成循环导入。

    只适合几十到几百行的小批量 —— 它逐行 `session.merge`，每行都要先 SELECT
    一次。上万行请用 `upsert_many`。
    """
    for row in rows:
        session.merge(model(**row))
    return len(rows)


def upsert_fill(session: Session, model, rows: list[dict]) -> int:
    """按主键 upsert，但**新数据里的 None 不覆盖库里已有的值**。

    给「数据源会偶发漏字段」的采集用。`upsert` 走 `session.merge`，null 会
    直接写进去 —— 上游一次不完整的响应就能把之前采到的数据抹掉。缺数据可以
    等下次采集自然补齐，抹掉的数据只能重新回补，代价完全不同。

    非空的新值照常覆盖（订正仍然生效），只有「新的空 vs 库里的非空」才保留旧值。
    """
    table = model.__table__
    pks = [column.name for column in table.primary_key.columns]
    for row in rows:
        key = row[pks[0]] if len(pks) == 1 else tuple(row[name] for name in pks)
        existing = session.get(model, key)
        if existing is None:
            session.add(model(**row))
            continue
        for name, value in row.items():
            if value is None and getattr(existing, name) is not None:
                continue
            setattr(existing, name, value)
    return len(rows)


# 单条 INSERT 里塞多少行。SQLite 的参数上限是 32766（3.32 起），
# 一行十几个列，500 行约 6000 个参数，留足余量
_UPSERT_CHUNK = 500


def upsert_many(session: Session, model, rows: list[dict]) -> int:
    """批量 upsert，走 SQLite 的 `ON CONFLICT DO UPDATE`。

    为什么需要它：`upsert` 逐行 merge，全市场日线一次几万行、首次建库上百万行，
    逐行 SELECT 会把采集拖到几十分钟。这里把几百行并成一条语句，
    同样是幂等的，但快两个数量级。

    要求 `rows` 的每个 dict 列必须一致 —— 多行 VALUES 不支持各行列不同。

    ⚠️ 与 `upsert_fill` 的差别（用之前要想清楚）：这一批里带了的列**一律照写**，
    `None` 也会把库里的非空值刷成 NULL。所以「来源这次没返回某列」时整批都是
    None，一次重采就能把已存好的值全抹掉 —— 调用方要么像 `collect_universe`
    那样整列全空时干脆不带这一列，要么改用 `upsert_fill`。**别把没校验过的
    上游数据直接喂进来。**

    行数多、又需要「不覆盖已有非空值」时用 `upsert_many_fill`（同样是批量语句）。
    """
    if not rows:
        return 0

    table = model.__table__
    primary_keys = [column.name for column in table.primary_key.columns]
    # 只更新这一批里真的带了的列，免得把没传的列刷成 NULL
    value_columns = [
        column.name
        for column in table.columns
        if column.name not in primary_keys and column.name in rows[0]
    ]

    for start in range(0, len(rows), _UPSERT_CHUNK):
        chunk = rows[start : start + _UPSERT_CHUNK]
        statement = sqlite_insert(table).values(chunk)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=primary_keys,
                set_={name: statement.excluded[name] for name in value_columns},
            )
        )
    return len(rows)


def upsert_many_fill(session: Session, model, rows: list[dict]) -> int:
    """`upsert_many` 的 **fill 版**：新数据里的 `None` **不覆盖**库里已有的非空值。

    为什么需要它（而不直接用 `upsert_fill`）：`upsert_fill` 逐行 `session.get`，
    只适合几十到几百行；全市场日线一次几万行、首次建库上百万行，用它会把采集拖到
    几十分钟（这也正是 `upsert_many` 存在的原因）。这里仍走一条批量 `ON CONFLICT`，
    只把 SET 换成 `COALESCE(excluded.列, 表.列)`：
    - 新的**非空**值照常覆盖（来源订正仍然生效）
    - 新的**空**值遇库里非空 → 保留旧值

    给「数据源会偶发漏字段、且一次几万行」的采集用（当前只有 `collect_kline`）。
    整列全空的情形 `collect_kline._fetch_day` 仍会先把那一列 pop 掉（那是另一层保护，
    防的是「一次重采把整列刷空」）；这里补的是「**个别行**漏字段」的那类静默覆盖。
    """
    if not rows:
        return 0

    table = model.__table__
    primary_keys = [column.name for column in table.primary_key.columns]
    value_columns = [
        column.name
        for column in table.columns
        if column.name not in primary_keys and column.name in rows[0]
    ]

    for start in range(0, len(rows), _UPSERT_CHUNK):
        chunk = rows[start : start + _UPSERT_CHUNK]
        statement = sqlite_insert(table).values(chunk)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=primary_keys,
                set_={
                    name: func.coalesce(statement.excluded[name], table.c[name])
                    for name in value_columns
                },
            )
        )
    return len(rows)
