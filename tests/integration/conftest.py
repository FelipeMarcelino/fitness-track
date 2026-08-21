"""Ephemeral Postgres for schema tests.

Two connection fixtures, deliberately:

- `owner_conn` connects as the container superuser, which bypasses RLS. Used
  for setup and for inspecting the catalog.
- `conn` connects as fittrack_app -- NOSUPERUSER, NOBYPASSRLS -- with
  app.tenant_id set, which is how production connects. Only this fixture can
  prove isolation; a superuser suite would pass no matter what the policies say.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from testcontainers.postgres import PostgresContainer

APP_PASSWORD = "app-password"  # ephemeral container, never real


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
    command.upgrade(Config("alembic.ini"), "head")
    yield postgres_dsn
    os.environ.pop("ALEMBIC_DSN", None)


@pytest.fixture(scope="session")
def app_dsn(migrated: str) -> str:
    """The migration creates fittrack_app NOLOGIN; give it a password so the
    tests can connect as the role the application actually uses."""
    import asyncio

    async def _grant() -> None:
        engine = create_async_engine(migrated, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            await conn.execute(text(f"ALTER ROLE fittrack_app LOGIN PASSWORD '{APP_PASSWORD}'"))
        await engine.dispose()

    asyncio.run(_grant())
    scheme, _, rest = migrated.partition("://")
    _, _, hostpart = rest.partition("@")
    return f"{scheme}://fittrack_app:{APP_PASSWORD}@{hostpart}"


@pytest_asyncio.fixture
async def owner_conn(migrated: str) -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(migrated)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        yield connection
        await transaction.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def conn(app_dsn: str) -> AsyncIterator[AsyncConnection]:
    """Connects as the application role. RLS is live here."""
    engine = create_async_engine(app_dsn)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        yield connection
        await transaction.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def as_tenant(conn: AsyncConnection):
    """Sets app.tenant_id for the transaction, the way the worker does."""

    async def _set(tenant_id: int) -> AsyncConnection:
        await conn.execute(text(f"SET LOCAL app.tenant_id = '{tenant_id}'"))
        return conn

    return _set
