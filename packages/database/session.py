import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(database_url: str, *, echo: bool = False, null_pool: bool = False) -> AsyncEngine:
    """null_pool=True is for Celery workers: each task runs in its own asyncio.run() event
    loop, and a pooled asyncpg connection stays bound to the loop that opened it, so the
    next task would fail with "attached to a different loop"."""
    global _engine, _session_factory
    if null_pool:
        _engine = create_async_engine(database_url, echo=echo, poolclass=NullPool)
    else:
        _engine = create_async_engine(database_url, echo=echo, pool_pre_ping=True)
    _session_factory = async_sessionmaker(
        bind=_engine, expire_on_commit=False, autoflush=False
    )
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database engine not initialized. Call init_engine() first.")
    return _engine


async def get_session() -> AsyncIterator[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Database engine not initialized. Call init_engine() first.")
    async with _session_factory() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """For non-FastAPI callers (Celery tasks, scripts) that want a session
    without going through the `get_session` generator-dependency shape."""
    if _session_factory is None:
        raise RuntimeError("Database engine not initialized. Call init_engine() first.")
    async with _session_factory() as session:
        yield session


_SET_TENANT = text("SELECT set_config('app.tenant_id', :tenant_id, true)")


@event.listens_for(Session, "after_begin")
def _apply_tenant_context(session, transaction, connection) -> None:
    """Re-applies the tenant to every new transaction on a session that has
    one (set_config(..., is_local=true) only lasts until commit/rollback, and
    a request commits several times)."""
    tenant_id = session.info.get("tenant_id")
    if tenant_id:
        connection.execute(_SET_TENANT, {"tenant_id": tenant_id})


async def set_tenant_context(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Scope this session to one tenant for Postgres row-level security (see
    packages/database/rls.py): the current transaction now, and every later
    one on the same session."""
    session.sync_session.info["tenant_id"] = str(tenant_id)
    await session.execute(_SET_TENANT, {"tenant_id": str(tenant_id)})
