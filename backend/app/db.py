"""SQLite 连接与会话管理。"""

import logging
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
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
                if not column.nullable and column.default is None and column.server_default is None:
                    # SQLite 的 ADD COLUMN 不接受「无默认值的 NOT NULL」，跳过并告警，
                    # 而不是让整个启动失败
                    logger.warning(
                        "表 %s 新增了非空列 %s 且无默认值，需要手工迁移",
                        table.name,
                        column.name,
                    )
                    continue
                column_type = column.type.compile(engine.dialect)
                connection.execute(
                    text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}')
                )
                added.append(f"{table.name}.{column.name}")
    if added:
        logger.info("已为老库补上新增列：%s", "、".join(added))
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
