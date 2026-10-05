"""数据库连接与初始化。"""
from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

engine: Engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def wait_for_database(timeout_seconds: float = 30.0) -> None:
    """等待 PostgreSQL 就绪（Compose 启动顺序下的短暂窗口）。"""
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except OperationalError as exc:  # pragma: no cover - 启动时序相关
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"数据库在 {timeout_seconds}s 内不可用: {last_error}")


def initialize_schema() -> None:
    """幂等执行 DDL（init.sql 由 Postgres 首启与本函数共用同一份）。

    psycopg2 的 DBAPI 连接原生支持多语句脚本（含 DO $$ 块），因此直接获取
    原始连接执行整个文件，DDL 在 PostgreSQL 中本身为事务安全操作。
    """
    sql = Path(settings.schema_path).read_text(encoding="utf-8")
    # btree_gist 需要数据库级 CREATE 权限；应用账号即库 owner（见 compose）
    sa_conn = engine.connect()
    try:
        dbapi_conn = sa_conn.connection
        with dbapi_conn.cursor() as cur:
            cur.execute(sql)
        dbapi_conn.commit()
    finally:
        sa_conn.close()


def get_session() -> Session:
    """FastAPI 依赖：每个请求一个事务，正常结束提交，异常回滚。"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
