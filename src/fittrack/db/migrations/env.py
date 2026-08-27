"""Alembic environment.

The URL comes from settings, never from `alembic.ini`: one place for a
connection string, and no file that could be committed with a password in it.

Migrations connect as the **owner**, not as the application principal. The
application must not be able to alter the schema, and the owner must not be what
the application connects as (spec 19.1).
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The schema is raw SQL transcribed from spec 5.2, not a declarative model, so
# there is nothing for autogenerate to compare against. That is deliberate:
# partial unique indexes, NULLS NOT DISTINCT, composite tenant-qualified foreign
# keys, generated columns and a view do not survive a round trip through
# SQLAlchemy metadata, and the spec is the reference the schema is checked
# against by tests/integration/test_schema_contract.py.
target_metadata = None

MIGRATION_URL_ENV = "MIGRATION_DATABASE_URL"


def _url() -> str:
    """The owner DSN, from the environment or from settings.

    An explicit environment value is validated the same way settings validate
    theirs. Skipping that check was how a URL with no `sslmode` reached asyncpg,
    which then connected on its non-verifying default — an unverified hop the
    topology has no room for (spec 22.1).
    """
    explicit = os.environ.get(MIGRATION_URL_ENV)
    if explicit:
        from pydantic import SecretStr

        from fittrack.settings import require_verified_postgres

        require_verified_postgres(MIGRATION_URL_ENV, SecretStr(explicit))
        return explicit

    from fittrack.settings import get_settings

    dsn = get_settings().migration_database_url
    if dsn is None:
        raise RuntimeError(
            f"{MIGRATION_URL_ENV} is unset. Migrations run as the schema owner, which is a "
            "different principal from the one the application uses (spec 19.1)."
        )
    return dsn.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    from fittrack.db.engine import split_ssl_arguments

    # Same translation the application does: the DSN carries libpq's names and
    # asyncpg takes an SSLContext (see fittrack.db.engine).
    url, ssl_args = split_ssl_arguments(_url())
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = url
    engine = async_engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool, connect_args=ssl_args
    )
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
