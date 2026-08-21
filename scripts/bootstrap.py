"""Everything the database needs before a worker may start.

Two steps, both requiring owner privileges, both idempotent:

1. `alembic upgrade head` -- the application schema.
2. the LangGraph checkpoint tables.

The second is not an Alembic migration on purpose: those tables belong to
langgraph-checkpoint-postgres and it changes their shape between versions, so
pinning them in our own migration would break on the next upgrade. But it is
also not a runtime step, because the app role cannot create tables (§19.1) --
and finding that out on the first message is the wrong time.

Run with the owner DSN (MIGRATION_DATABASE_URL), the same credential Alembic
uses:

    python scripts/bootstrap.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, "src")

from alembic import command
from alembic.config import Config

from fittrack.graph.checkpoint import setup_checkpoint_tables

log = logging.getLogger("bootstrap")


def owner_dsn() -> str:
    """The owner credential, never the application one.

    Falling back to DATABASE_URL would appear to work right up to the CREATE,
    and then fail with a permission error that reads like a broken install
    rather than a missing variable.
    """
    dsn = os.environ.get("MIGRATION_DATABASE_URL")
    if not dsn:
        raise SystemExit(
            "MIGRATION_DATABASE_URL is not set. Bootstrap needs the owner "
            "credential; the application role cannot create tables."
        )
    return dsn


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    dsn = owner_dsn()

    log.info("applying migrations")
    command.upgrade(Config("alembic.ini"), "head")

    log.info("creating the checkpoint tables")
    await setup_checkpoint_tables(dsn)

    log.info("bootstrap complete")


if __name__ == "__main__":
    asyncio.run(main())
