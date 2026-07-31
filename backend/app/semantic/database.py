"""Database lifecycle for the independently deployed semantic service."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.semantic.models import SemanticBase


def ensure_sqlite_parent(database_url: str) -> None:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return
    raw = database_url[len(prefix) :]
    if raw and raw != ":memory:":
        Path(raw).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def create_semantic_engine(database_url: str) -> AsyncEngine:
    ensure_sqlite_parent(database_url)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    if database_url.startswith("sqlite+aiosqlite:"):

        @event.listens_for(engine.sync_engine, "connect")
        def _configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

    return engine


def create_semantic_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def initialize_semantic_database(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(SemanticBase.metadata.create_all)
