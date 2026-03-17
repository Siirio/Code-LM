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
