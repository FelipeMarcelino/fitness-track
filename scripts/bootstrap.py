#!/usr/bin/env python3
"""Bring a database to a state the application can run against.

Two steps, both idempotent, because a bootstrap that can only be run once is a
bootstrap nobody dares run:

1. **Migrations**, to `head`. Alembic is already idempotent — an
   already-migrated database is a no-op.
2. **LangGraph's own tables**, created by the library's `setup()` rather than by
   a migration. Putting them in Alembic would fork their schema from the
   library's, and the library upgrades them itself (spec 5.3).
3. **Withdrawing the grants step 2 hands out by accident.** See
   `LANGGRAPH_TABLES` below: this one exists because of how the other two
   interact, and it is the only step here that is a fix rather than a setup.

Deliberately *not* here: any Telegram call, and any seed. `setWebhook` belongs
to the sprint that builds the adapter, and the exercise catalogue to the one
that needs it — a bootstrap that reaches out to a third party is one that cannot
run in CI.

    python -m scripts.bootstrap            # migrate and set up
    python -m scripts.bootstrap --check    # report, change nothing
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class BootstrapError(RuntimeError):
    """The database could not be brought to a usable state."""


def migrate(check_only: bool = False) -> str:
    """Run migrations to head, or report the current revision."""
    command = ["current"] if check_only else ["upgrade", "head"]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *command],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # stderr, not stdout: alembic puts the DSN nowhere, but a driver error
        # can, and this is the one place that would print it.
        raise BootstrapError(f"alembic {' '.join(command)} failed:\n{result.stderr}")

    reported = result.stdout.strip()
    if check_only and not reported:
        # `alembic current` exits 0 and prints nothing when there is no
        # `alembic_version` table at all. Reading that as "ok" answered "is this
        # environment ready?" with yes, on a database with no schema on it.
        raise BootstrapError(
            "no migrations have been applied to this database. "
            "Run `make bootstrap` (without --check) to create the schema."
        )
    return reported or "ok"


async def setup_langgraph_tables(dsn: str) -> str:
    """Create the checkpointer and store tables, if the dependency is installed.

    `setup()` is idempotent by design — the library uses it as its own migration
    mechanism, and calling it on an existing schema upgrades or does nothing.
    """
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from langgraph.store.postgres.aio import AsyncPostgresStore
    except ImportError as error:
        # Not swallowed as "not installed": psycopg imports happily without a
        # libpq binding and only fails here, so a generic message would report a
        # broken deployment as a skipped optional step.
        raise BootstrapError(
            f"the LangGraph Postgres backend is installed but unusable: {error}"
        ) from None

    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        await saver.setup()
    async with AsyncPostgresStore.from_conn_string(dsn) as store:
        await store.setup()
    return "checkpointer and store ready"


# Created by `setup()` rather than by a migration, which is exactly what makes
# them a problem. `ALTER DEFAULT PRIVILEGES ... GRANT SELECT, INSERT, UPDATE,
# DELETE ... TO fittrack_app` (spec 19.1) applies to tables the owner creates
# *in the future*, and bootstrap creates these as the owner -- so the
# application came out of bootstrap holding full DML over every tenant's graph
# state, on tables with no row-level security. Nothing in the migration says
# so, and `test_every_table_with_a_tenant_column_has_a_policy` cannot see it:
# the tenant lives inside `thread_id`, not in a `tenant_id` column.
#
# The grant is withdrawn rather than made safe. Making it safe means a policy
# keyed on the thread id, which belongs to the sprint that wires the
# checkpointer and gets to decide what a thread key is. Until then nothing in
# the application touches these tables, so taking the privilege away costs
# nothing -- and leaves that sprint having to grant deliberately, with a policy
# next to it, instead of inheriting one it never asked for.
LANGGRAPH_TABLES = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
    "store",
    "store_migrations",
)

# The two that hold the state itself, as opposed to the library's bookkeeping.
# Their absence after `setup()` means the revoke looked in the wrong place, not
# that there was nothing to revoke.
REQUIRED_TABLES = frozenset({"checkpoints", "store"})


async def revoke_application_grants(dsn: str) -> str:
    """Take back the DML that `ALTER DEFAULT PRIVILEGES` handed out silently.

    Idempotent: revoking a privilege nobody holds is not an error. Tables that
    are absent are skipped, so a LangGraph version creating a different set does
    not turn this into a failure — `test_bootstrap_knows_every_table_langgraph_
    creates` is what catches that case, by diffing what `setup()` actually
    created against this tuple.
    """
    from psycopg import AsyncConnection

    async with (
        await AsyncConnection.connect(dsn, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        await cursor.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname = ANY(%s)",
            (list(LANGGRAPH_TABLES),),
        )
        present = sorted(row[0] for row in await cursor.fetchall())

        # `setup()` ran moments ago, so finding nothing does not mean "nothing
        # to do" — it means the tables are not where this looked, and the only
        # visible result would be a cheerful "revoked on 0 table(s)" while the
        # application kept full DML on every tenant's graph state. The one
        # failure mode of a function like this must not be silence.
        missing = sorted(REQUIRED_TABLES - set(present))
        if missing:
            raise BootstrapError(
                f"LangGraph setup() ran but {', '.join(missing)} is not in the public schema. "
                "The application's inherited DML on the graph-state tables has NOT been "
                "revoked. Check search_path and the LangGraph version before using this "
                "database."
            )

        for table in present:
            # Schema-qualified: the lookup above is scoped to `public`, and an
            # unqualified REVOKE resolves through `search_path` instead — which
            # could revoke on a different table of the same name, and report
            # success either way. An identifier cannot be a bind parameter, and
            # these are literals from the tuple above filtered through pg_class,
            # not input.
            await cursor.execute(f'REVOKE ALL ON TABLE public."{table}" FROM fittrack_app')

    return f"application grants revoked on {len(present)} table(s)"


def _owner_dsn(env_file: str | None = ".env") -> str:
    """The migration principal's DSN, in the spelling the drivers want.

    LangGraph's Postgres backends use psycopg, which speaks libpq — so unlike
    the application engine, this one wants the URL exactly as written.

    `env_file=None` reads only the process environment, which is what a test
    wants: with the file in play, unsetting the variable proves nothing.
    """
    from fittrack.settings import Settings

    settings = Settings(_env_file=env_file)
    if settings.migration_database_url is None:
        raise BootstrapError(
            "MIGRATION_DATABASE_URL is unset. Bootstrap runs as the schema owner, "
            "which is a different principal from the one the application uses (spec 19.1)."
        )
    return settings.migration_database_url.get_secret_value().replace("+asyncpg", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="report the current state, change nothing"
    )
    args = parser.parse_args(argv)

    try:
        dsn = _owner_dsn()
        print(f"migrations: {migrate(check_only=args.check)}")
        if args.check:
            print("langgraph:  not checked (--check changes nothing)")
        else:
            print(f"langgraph:  {asyncio.run(setup_langgraph_tables(dsn))}")
            print(f"grants:     {asyncio.run(revoke_application_grants(dsn))}")
    except BootstrapError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
