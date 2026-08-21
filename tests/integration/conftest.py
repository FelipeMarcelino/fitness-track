"""Ephemeral Postgres for schema tests.

The migration runs in a synchronous session-scoped fixture: Alembic's env.py
calls asyncio.run(), which cannot be nested inside the event loop an async
fixture already owns.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def migrated(postgres_dsn: str) -> Iterator[str]:
    """Runs the real migration, so tests exercise what production runs."""
    from alembic import command
    from alembic.config import Config

    os.environ["ALEMBIC_DSN"] = postgres_dsn
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    yield postgres_dsn
    os.environ.pop("ALEMBIC_DSN", None)


@pytest_asyncio.fixture
async def conn(migrated: str) -> AsyncIterator[AsyncConnection]:
    """Fresh transaction per test, rolled back, so tests cannot see each
    other's writes."""
    engine: AsyncEngine = create_async_engine(migrated)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        yield connection
        await transaction.rollback()
    await engine.dispose()
