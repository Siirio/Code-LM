"""PostgreSQL connection via SQLAlchemy async.

Stores: project metadata, architectural rules, module registry, ADRs,
        memory update proposals awaiting developer approval.
"""
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config import settings

_engine = None
_session_factory = None


class Base(DeclarativeBase):
    pass


async def init_postgres() -> None:
    """Create the engine and all tables. Call once at app startup.

    IMPORTANT: models must be imported before Base.metadata.create_all is
    called, otherwise SQLAlchemy's metadata registry is empty and no tables
    are created.  We import storage.models here (inside the function body)
    rather than at module top-level to avoid a circular import: models.py
    imports Base from this file, and this file must not import from models.py
    at module scope.
    """
    # Ensure all ORM models are registered with Base.metadata before create_all.
    import storage.models  # noqa: F401 — side-effect import

    global _engine, _session_factory
    _engine = create_async_engine(
        settings.postgres_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Idempotent column migrations — safe to run on every startup.
        # create_all above handles fresh installs; these ADD IF NOT EXISTS
        # handle existing databases that are missing newer columns.
        migrations = [
            "ALTER TABLE project_memory ADD COLUMN IF NOT EXISTS discovered_patterns TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE project_memory ADD COLUMN IF NOT EXISTS confidence_level VARCHAR(16) NOT NULL DEFAULT 'medium'",
            "ALTER TABLE project_memory ADD COLUMN IF NOT EXISTS memory_source VARCHAR(32) NOT NULL DEFAULT 'static_analysis'",
            # parser_discrepancies is created by create_all on fresh installs;
            # this block handles existing DBs that pre-date this table.
            """
            CREATE TABLE IF NOT EXISTS parser_discrepancies (
                id VARCHAR(128) PRIMARY KEY,
                project_id VARCHAR(128) NOT NULL,
                file_path TEXT NOT NULL,
                regex_classes TEXT NOT NULL DEFAULT '[]',
                ts_classes TEXT NOT NULL DEFAULT '[]',
                regex_count INTEGER NOT NULL DEFAULT 0,
                ts_count INTEGER NOT NULL DEFAULT 0,
                confidence VARCHAR(16) NOT NULL,
                parser_used VARCHAR(32) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_parser_discrepancies_project_id ON parser_discrepancies (project_id)",
            "CREATE INDEX IF NOT EXISTS ix_parser_discrepancies_file_path ON parser_discrepancies (file_path)",
            # Change tracking tables
            """
            CREATE TABLE IF NOT EXISTS chat_file_changes (
                id VARCHAR(128) PRIMARY KEY,
                session_id VARCHAR(128) NOT NULL,
                file_path TEXT NOT NULL,
                action VARCHAR(16) NOT NULL DEFAULT 'update',
                summary TEXT NOT NULL DEFAULT '',
                completed BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_chat_file_changes_session_id ON chat_file_changes (session_id)",
            """
            CREATE TABLE IF NOT EXISTS chat_todos (
                id VARCHAR(128) PRIMARY KEY,
                session_id VARCHAR(128) NOT NULL,
                text TEXT NOT NULL,
                completed BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_chat_todos_session_id ON chat_todos (session_id)",
        ]
        for ddl in migrations:
            await conn.exec_driver_sql(ddl)


async def close_postgres() -> None:
    global _engine
    if _engine:
        await _engine.dispose()
        _engine = None


@asynccontextmanager
async def get_pg_session() -> AsyncIterator[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("PostgreSQL not initialised — call init_postgres() first")
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
