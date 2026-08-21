"""Alembic environment.

The DSN comes from ALEMBIC_DSN when set (tests point it at an ephemeral
container), otherwise from the application settings.
"""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

config = context.config


def _dsn() -> str:
    """Migrations run as the owner; the application runs as fittrack_app.

    Using DATABASE_URL here would run DDL as a role that has no rights to
    create tables -- and, worse, would invite pointing the application at the
    owner, which silently bypasses row level security.
    """
    override = os.environ.get("ALEMBIC_DSN") or os.environ.get("MIGRATION_DATABASE_URL")
    if override:
        return override

    from fittrack.settings import get_settings

    return get_settings().database_url.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(url=_dsn(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: object) -> None:
    context.configure(connection=connection)  # type: ignore[arg-type]
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    config.set_main_option("sqlalchemy.url", _dsn())
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
