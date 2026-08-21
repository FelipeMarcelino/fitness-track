"""Where the conversation state is persisted between turns (§8.1).

Postgres rather than memory, because the state has to survive a deploy. A bot
that forgets mid-conversation asks again for what the user already told it,
which is the most visible way for it to look broken.

The checkpointer uses psycopg, not the SQLAlchemy engine the rest of the
application uses: langgraph-checkpoint-postgres owns its own connection and
its own tables. Keeping them separate means a migration to the app schema
cannot break checkpointing and vice versa.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, sql

log = logging.getLogger(__name__)


def to_psycopg_dsn(dsn: str) -> str:
    """Turns the app's SQLAlchemy URL into one psycopg understands.

    `postgresql+asyncpg://` is a SQLAlchemy dialect string; psycopg rejects it.
    Converting here rather than carrying a second URL in the environment keeps
    one credential to rotate instead of two that can drift apart.
    """
    scheme, separator, rest = dsn.partition("://")
    if not separator:
        return dsn
    return f"{scheme.split('+')[0]}://{rest}"


# The tables langgraph-checkpoint-postgres owns. Listed explicitly so the
# grant below cannot quietly widen to cover application tables.
CHECKPOINT_TABLES: Final = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
)


async def setup_checkpoint_tables(owner_dsn: str) -> None:
    """Creates the checkpoint tables and grants the app role access.

    Run as the owner, not as fittrack_app: the app role is deliberately unable
    to create tables (§19.1), so calling `setup()` at runtime fails with
    "permission denied for schema public" -- on the first deploy, on the first
    message.

    Deliberately not an Alembic migration. These tables belong to the library
    and it changes their shape between versions; pinning that in our own
    migration would break on the next upgrade. `setup()` is idempotent, so
    this is a startup step instead.
    """
    psycopg_dsn = to_psycopg_dsn(owner_dsn)
    async with AsyncPostgresSaver.from_conn_string(psycopg_dsn) as saver:
        await saver.setup()

    # A separate connection rather than the saver's own: `from_conn_string`
    # types its connection as either a connection or a pool, and reaching in to
    # find out which is exactly the coupling that breaks on a library upgrade.
    async with await AsyncConnection.connect(psycopg_dsn, autocommit=True) as conn:
        for table in CHECKPOINT_TABLES:
            await conn.execute(
                sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO fittrack_app").format(
                    sql.Identifier(table)
                )
            )
    log.info("checkpoint tables are ready")


@asynccontextmanager
async def checkpointer(dsn: str) -> AsyncIterator[AsyncPostgresSaver]:
    """Opens a checkpointer for the running application.

    No `setup()` here: that needs privileges the app role does not have, and
    should not have. Call `setup_checkpoint_tables` with the owner DSN first.

    Note what these tables do not have: RLS. They are the library's, they carry
    no tenant_id, and the isolation is that `thread_id` is the BSUID and the
    application never asks for another one. That is enforcement in code rather
    than in the database, which is weaker than every other table here -- worth
    knowing before conversation state is used for anything but conversation.
    """
    async with AsyncPostgresSaver.from_conn_string(to_psycopg_dsn(dsn)) as saver:
        yield saver


def thread_config(bsuid: str) -> dict[str, Any]:
    """One thread per user.

    The BSUID is the thread id because conversation continuity is per person.
    Keying on anything narrower -- a batch, a session -- would start a fresh
    conversation every few minutes; anything wider would mix two people's
    histories, which is a privacy incident rather than a bug.
    """
    return {"configurable": {"thread_id": bsuid}}
