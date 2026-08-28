"""Cross-tenant leakage, walked table by table (spec 19.1).

Parametrised over the **whole** list, not a sample. A table missing a policy is
a silent leak that needs only one repository to forget its predicate, so the
list here and the one in migration 0002 are checked against each other — a new
tenant-scoped table with no policy fails this file.

Everything runs as `fittrack_runtime`. As the owner, or as anything with
`BYPASSRLS`, every assertion below would pass without a single policy being
evaluated (spec 19.1) — which is why the connection is asserted first.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator

import asyncpg
import pytest

from fittrack.db.migrations.versions._0002_row_level_security import (
    GLOBAL_READABLE,
    TENANT_SCOPED,
)

# Needed because this file sits at the suite root, where the directory marker of
# `tests/integration/` does not reach it (spec 23 puts it here).
pytestmark = pytest.mark.integration

# One row per table, keyed by the columns the schema makes mandatory. The values
# are irrelevant; what matters is that the row exists and belongs to a tenant.
SEEDS: dict[str, str] = {
    "channel_identity": (
        "INSERT INTO channel_identity (tenant_id, channel, external_id, external_id_hash)"
        " VALUES ($1, 'telegram', $2, $2)"
    ),
    "athlete_profile": "INSERT INTO athlete_profile (tenant_id, goal) VALUES ($1, 'forca')",
    "consent": (
        "INSERT INTO consent (tenant_id, kind, granted, text_hash, version)"
        " VALUES ($1, 'terms', true, 'h', 'v1')"
    ),
    "subscription": "INSERT INTO subscription (tenant_id, status) VALUES ($1, 'active')",
    "exercise": (
        "INSERT INTO exercise (tenant_id, slug, name, modality)"
        " VALUES ($1, encode($2, 'hex'), 'x', 'forca')"
    ),
    "workout_session": (
        "INSERT INTO workout_session (tenant_id, local_date) VALUES ($1, CURRENT_DATE)"
    ),
    "body_metric": (
        "INSERT INTO body_metric (tenant_id, local_date, kind, value, unit)"
        " VALUES ($1, CURRENT_DATE, 'peso', $2, 'kg')"
    ),
    "health_report": (
        "INSERT INTO health_report (tenant_id, category, verbatim) VALUES ($1, 'dor', $2)"
    ),
    "training_program": (
        "INSERT INTO training_program (tenant_id, name, goal, horizon_weeks, rationale)"
        " VALUES ($1, 'p', 'forca', 8, 'r')"
    ),
    "workout_plan": "INSERT INTO workout_plan (tenant_id, name) VALUES ($1, 'plano')",
    "processing_batch": (
        "INSERT INTO processing_batch (tenant_id, message_ids, combined_text)"
        " VALUES ($1, ARRAY['m'], $2)"
    ),
    "usage_ledger": (
        "INSERT INTO usage_ledger (tenant_id, agent, provider, model) VALUES ($1, 'a', 'groq', 'm')"
    ),
}


# The nine tables below need a parent row, so one statement is not enough to
# seed them. They were absent from `SEEDS` and therefore walked by every
# parametrised test above while proving nothing -- including `exercise_set`,
# `raw_message` and `outbound_queue`, which hold the data actually worth
# leaking. The acceptance criterion says every tenant-scoped table, not a
# sample, and `test_the_suite_seeds_every_tenant_scoped_table` now enforces it.
PARENTED = (
    "exercise_alias",
    "exercise_set",
    "session_summary",
    "raw_message",
    "outbound_queue",
    "conversation_window",
    "program_phase",
    "program_milestone",
    "plan_item",
)


@pytest.fixture
async def app_session(
    app_dsn: str, trusting_ssl_context: object, migrated: None
) -> AsyncIterator[asyncpg.Connection]:
    """A connection as the unprivileged principal, with no tenant context yet."""
    connection = await asyncpg.connect(app_dsn, ssl=trusting_ssl_context)
    try:
        yield connection
    finally:
        await connection.close()


async def set_tenant(connection: asyncpg.Connection, tenant_id: int | None) -> None:
    await connection.execute(
        "SELECT set_config('app.tenant_id', $1, false)", "" if tenant_id is None else str(tenant_id)
    )


ALL_SEEDABLE = tuple(sorted(set(SEEDS) | set(PARENTED)))

# Tables the application cannot write at all, policy or no policy. Linking and
# revoking an account goes through the two boundary functions, so migration
# 0002 revokes INSERT/UPDATE/DELETE on `channel_identity` outright. The walks
# below expect a privilege error there rather than the usual "matched no rows":
# not a weaker result, a stronger one -- the statement never runs.
WRITE_DENIED = frozenset({"channel_identity"})


async def seeded_tenant(owner: asyncpg.Connection, table: str) -> int:
    """A tenant owning one row in `table`, written as the owner.

    Raises for a table it cannot seed. Doing nothing quietly was how nine
    tables sat in the parametrisation and proved nothing about themselves.
    """
    tenant: int = await owner.fetchval(
        "INSERT INTO tenant (display_name) VALUES ('seed') RETURNING id"
    )
    if table == "tenant":
        return tenant
    if table in SEEDS:
        await owner.execute(*seed_args(table, tenant))
        return tenant
    if table in PARENTED:
        await seed_parented(owner, table, tenant)
        return tenant
    raise AssertionError(f"no seed for {table}: the isolation walk would skip it silently")


def seed_args(table: str, tenant: int) -> tuple[object, ...]:
    """The insert and its parameters. Only some rows need a unique marker."""
    statement = SEEDS[table]
    if "$2" in statement:
        return (statement, tenant, secrets.token_bytes(16))
    return (statement, tenant)


async def seed_parented(owner: asyncpg.Connection, table: str, tenant: int) -> tuple[object, ...]:
    """One row in a table that needs a parent, plus whatever it hangs from.

    Written as the owner, so no policy is consulted while setting up; the tests
    then read it back as `fittrack_runtime`, which is the principal under test.

    Returns the child insert and its parameters, so the `WITH CHECK` test can
    replay it as the wrong tenant. Postgres evaluates the RLS check before it
    touches the indexes, so replaying an insert that would also collide on a
    unique constraint still fails as a privilege error, not a duplicate key.
    """
    marker = secrets.token_bytes(16)
    unique = secrets.token_hex(8)
    child: tuple[object, ...]

    if table == "exercise_alias":
        exercise = await owner.fetchval(
            "INSERT INTO exercise (tenant_id, slug, name, modality)"
            " VALUES ($1, $2, 'x', 'forca') RETURNING id",
            tenant,
            unique,
        )
        child = (
            "INSERT INTO exercise_alias (exercise_id, alias, normalized, tenant_id)"
            " VALUES ($1, $2, $2, $3)",
            exercise,
            unique,
            tenant,
        )
    elif table in ("exercise_set", "session_summary"):
        session = await owner.fetchval(
            "INSERT INTO workout_session (tenant_id, local_date)"
            " VALUES ($1, CURRENT_DATE) RETURNING id",
            tenant,
        )
        if table == "exercise_set":
            exercise = await owner.fetchval(
                "INSERT INTO exercise (tenant_id, slug, name, modality)"
                " VALUES ($1, $2, 'x', 'forca') RETURNING id",
                tenant,
                unique,
            )
            child = (
                "INSERT INTO exercise_set (tenant_id, session_id, exercise_id,"
                " exercise_tenant_id, set_index, reps, load_kg)"
                " VALUES ($1, $2, $3, $1, 1, 8, 80.0)",
                tenant,
                session,
                exercise,
            )
        else:
            child = (
                "INSERT INTO session_summary (session_id, tenant_id, narrative)"
                " VALUES ($1, $2, $3)",
                session,
                tenant,
                marker,
            )
    elif table in ("raw_message", "outbound_queue", "conversation_window"):
        identity = await owner.fetchval(
            "INSERT INTO channel_identity (tenant_id, channel, external_id, external_id_hash)"
            " VALUES ($1, 'telegram', $2, $2) RETURNING id",
            tenant,
            marker,
        )
        if table == "raw_message":
            child = (
                "INSERT INTO raw_message (tenant_id, identity_id, channel,"
                " channel_message_id, direction, msg_type, payload)"
                " VALUES ($1, $2, 'telegram', $3, 'inbound', 'text', $4)",
                tenant,
                identity,
                unique,
                marker,
            )
        elif table == "outbound_queue":
            child = (
                "INSERT INTO outbound_queue (tenant_id, identity_id, channel, kind,"
                " payload, group_id)"
                " VALUES ($1, $2, 'telegram', 'text', $3, gen_random_uuid())",
                tenant,
                identity,
                marker,
            )
        else:
            child = (
                "INSERT INTO conversation_window (identity_id, tenant_id, last_inbound_at)"
                " VALUES ($1, $2, now())",
                identity,
                tenant,
            )
    elif table in ("program_phase", "program_milestone"):
        program = await owner.fetchval(
            "INSERT INTO training_program (tenant_id, name, goal, horizon_weeks, rationale)"
            " VALUES ($1, 'p', 'forca', 8, 'r') RETURNING id",
            tenant,
        )
        if table == "program_phase":
            child = (
                "INSERT INTO program_phase (tenant_id, program_id, phase_order, name, weeks)"
                " VALUES ($1, $2, 1, 'base', 4)",
                tenant,
                program,
            )
        else:
            child = (
                "INSERT INTO program_milestone (tenant_id, program_id, description,"
                " metric, target_value)"
                " VALUES ($1, $2, 'd', 'e1rm', 100)",
                tenant,
                program,
            )
    elif table == "plan_item":
        plan = await owner.fetchval(
            "INSERT INTO workout_plan (tenant_id, name) VALUES ($1, 'plano') RETURNING id",
            tenant,
        )
        exercise = await owner.fetchval(
            "INSERT INTO exercise (tenant_id, slug, name, modality)"
            " VALUES ($1, $2, 'x', 'forca') RETURNING id",
            tenant,
            unique,
        )
        child = (
            "INSERT INTO plan_item (tenant_id, plan_id, day_label, day_order, item_order,"
            " exercise_id, exercise_tenant_id)"
            " VALUES ($1, $2, 'A', 1, 1, $3, $1)",
            tenant,
            plan,
            exercise,
        )
    else:  # pragma: no cover - guarded by seeded_tenant
        raise AssertionError(f"seed_parented does not know {table}")

    await owner.execute(*child)
    return child


# --------------------------------------------------------------------------- #
# The connection itself
# --------------------------------------------------------------------------- #


async def test_the_suite_connects_as_the_unprivileged_principal(
    app_session: asyncpg.Connection,
) -> None:
    """Otherwise every assertion below passes with no policy evaluated."""
    role = await app_session.fetchrow(
        """
        SELECT current_user AS name, rolsuper, rolbypassrls
          FROM pg_roles WHERE rolname = current_user
        """
    )
    assert role["name"] == "fittrack_runtime"
    assert not role["rolsuper"]
    assert not role["rolbypassrls"]


# --------------------------------------------------------------------------- #
# Every table, both directions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("table", sorted(TENANT_SCOPED))
async def test_row_level_security_is_enabled_and_forced(
    owner: asyncpg.Connection, table: str
) -> None:
    """`FORCE` matters: without it the table's owner ignores every policy."""
    flags = await owner.fetchrow(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = $1", table
    )
    assert flags["relrowsecurity"], f"{table} has RLS disabled"
    assert flags["relforcerowsecurity"], f"{table} does not FORCE RLS"


async def test_the_root_table_is_covered_too(owner: asyncpg.Connection) -> None:
    flags = await owner.fetchrow(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'tenant'"
    )
    assert flags["relrowsecurity"] and flags["relforcerowsecurity"]


@pytest.mark.parametrize("table", ALL_SEEDABLE)
async def test_a_tenant_reads_only_its_own_rows(
    owner: asyncpg.Connection, app_session: asyncpg.Connection, table: str
) -> None:
    mine = await seeded_tenant(owner, table)
    theirs = await seeded_tenant(owner, table)

    await set_tenant(app_session, mine)
    visible = await app_session.fetchval(
        f"SELECT count(*) FROM {table} WHERE tenant_id = $1",
        theirs,
    )
    assert visible == 0, f"{table} leaked rows of another tenant"

    assert (
        await app_session.fetchval(
            f"SELECT count(*) FROM {table} WHERE tenant_id = $1",
            mine,
        )
        >= 1
    )


@pytest.mark.parametrize("table", ALL_SEEDABLE)
async def test_a_tenant_cannot_update_another_tenants_rows(
    owner: asyncpg.Connection, app_session: asyncpg.Connection, table: str
) -> None:
    """An UPDATE that matches nothing is the policy working, not a no-op."""
    mine = await seeded_tenant(owner, table)
    theirs = await seeded_tenant(owner, table)

    await set_tenant(app_session, mine)
    statement = f"UPDATE {table} SET tenant_id = tenant_id WHERE tenant_id = $1"
    if table in WRITE_DENIED:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_session.execute(statement, theirs)
        return

    result = await app_session.execute(statement, theirs)
    assert result.endswith(" 0"), f"{table} allowed a cross-tenant update: {result}"


@pytest.mark.parametrize("table", ALL_SEEDABLE)
async def test_a_tenant_cannot_delete_another_tenants_rows(
    owner: asyncpg.Connection, app_session: asyncpg.Connection, table: str
) -> None:
    mine = await seeded_tenant(owner, table)
    theirs = await seeded_tenant(owner, table)

    await set_tenant(app_session, mine)
    statement = f"DELETE FROM {table} WHERE tenant_id = $1"
    if table in WRITE_DENIED:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_session.execute(statement, theirs)
        return

    result = await app_session.execute(statement, theirs)
    assert result.endswith(" 0"), f"{table} allowed a cross-tenant delete: {result}"

    # And the row is still there, seen from the owner.
    assert (
        await owner.fetchval(
            f"SELECT count(*) FROM {table} WHERE tenant_id = $1",
            theirs,
        )
        >= 1
    )


# Only the single-statement seeds: this test executes the insert *as the app*,
# so it needs a statement it can run directly. The parented tables are covered
# for `WITH CHECK` by `test_the_with_check_half_covers_the_parented_tables`.
@pytest.mark.parametrize("table", sorted(SEEDS))
async def test_a_tenant_cannot_write_a_row_for_another(
    owner: asyncpg.Connection, app_session: asyncpg.Connection, table: str
) -> None:
    """The `WITH CHECK` half: reading is not the only way to cross the line."""
    mine = await seeded_tenant(owner, table)
    theirs = await seeded_tenant(owner, table)

    await set_tenant(app_session, mine)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_session.execute(*seed_args(table, theirs))


# --------------------------------------------------------------------------- #
# No context at all
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("table", ALL_SEEDABLE)
async def test_a_connection_without_a_tenant_sees_nothing_private(
    owner: asyncpg.Connection, app_session: asyncpg.Connection, table: str
) -> None:
    """A forgotten `SET LOCAL` must read zero rows, never every row.

    `current_setting(..., true)` returns NULL instead of raising, and NULL
    compares to nothing — so the omission fails closed.
    """
    tenant = await seeded_tenant(owner, table)
    await set_tenant(app_session, None)
    assert (
        await app_session.fetchval(
            f"SELECT count(*) FROM {table} WHERE tenant_id = $1",
            tenant,
        )
        == 0
    )


async def test_a_connection_without_a_tenant_sees_no_tenants(
    owner: asyncpg.Connection, app_session: asyncpg.Connection
) -> None:
    await seeded_tenant(owner, "tenant")
    await set_tenant(app_session, None)
    assert await app_session.fetchval("SELECT count(*) FROM tenant") == 0


# --------------------------------------------------------------------------- #
# The global catalogue
# --------------------------------------------------------------------------- #


# The column carrying the marker, per catalogue table. The old test asserted
# `count(*) IS NOT NULL`, which is true of every count ever taken -- and for
# three of the four tables it inserted nothing at all, so it counted zero rows
# and passed. Seeding a row and looking for that row is the difference between
# testing the policy and testing that SQL works.
GLOBAL_MARKER = {
    "exercise": "slug",
    "exercise_alias": "normalized",
    "workout_plan": "name",
    "plan_item": "day_label",
}


async def seed_global(owner: asyncpg.Connection, table: str) -> str:
    """A catalogue row -- `tenant_id IS NULL` -- carrying a unique marker."""
    marker = f"global-{secrets.token_hex(6)}"

    if table == "exercise":
        await owner.execute(
            "INSERT INTO exercise (tenant_id, slug, name, modality)"
            " VALUES (NULL, $1, 'global', 'forca')",
            marker,
        )
    elif table == "exercise_alias":
        exercise = await owner.fetchval(
            "INSERT INTO exercise (tenant_id, slug, name, modality)"
            " VALUES (NULL, $1, 'global', 'forca') RETURNING id",
            f"parent-{marker}",
        )
        await owner.execute(
            "INSERT INTO exercise_alias (exercise_id, alias, normalized, tenant_id)"
            " VALUES ($1, $2, $2, NULL)",
            exercise,
            marker,
        )
    elif table == "workout_plan":
        await owner.execute("INSERT INTO workout_plan (tenant_id, name) VALUES (NULL, $1)", marker)
    elif table == "plan_item":
        plan = await owner.fetchval(
            "INSERT INTO workout_plan (tenant_id, name) VALUES (NULL, $1) RETURNING id",
            f"parent-{marker}",
        )
        exercise = await owner.fetchval(
            "INSERT INTO exercise (tenant_id, slug, name, modality)"
            " VALUES (NULL, $1, 'global', 'forca') RETURNING id",
            f"parent-{marker}",
        )
        await owner.execute(
            "INSERT INTO plan_item (tenant_id, plan_id, day_label, day_order, item_order,"
            " exercise_id, exercise_tenant_id)"
            " VALUES (NULL, $1, $2, 1, 1, $3, NULL)",
            plan,
            marker,
            exercise,
        )
    else:  # pragma: no cover - the parametrisation is GLOBAL_READABLE itself
        raise AssertionError(f"seed_global does not know {table}")

    return marker


@pytest.mark.parametrize("table", sorted(GLOBAL_READABLE))
async def test_global_rows_are_readable_by_any_tenant(
    owner: asyncpg.Connection, app_session: asyncpg.Connection, table: str
) -> None:
    """Without this policy the catalogue vanishes as soon as a tenant is set."""
    marker = await seed_global(owner, table)
    column = GLOBAL_MARKER[table]
    tenant = await seeded_tenant(owner, "tenant")
    await set_tenant(app_session, tenant)

    visible = await app_session.fetchval(
        f"SELECT count(*) FROM {table} WHERE {column} = $1 AND tenant_id IS NULL", marker
    )
    assert visible == 1


@pytest.mark.parametrize("table", sorted(GLOBAL_READABLE))
async def test_the_catalogue_policy_grants_reads_and_nothing_else(
    owner: asyncpg.Connection, app_session: asyncpg.Connection, table: str
) -> None:
    """`FOR SELECT` is the whole policy: a readable row is not a writable one.

    A tenant that could edit the shared catalogue would reach every other
    tenant through it, which is the leak the policy exists to avoid.
    """
    marker = await seed_global(owner, table)
    column = GLOBAL_MARKER[table]
    tenant = await seeded_tenant(owner, "tenant")
    await set_tenant(app_session, tenant)

    # No policy grants UPDATE or DELETE over `tenant_id IS NULL`, so the rows
    # are simply not there for either -- silently, as RLS filters rather than
    # raises. The row surviving as the owner sees it is the assertion.
    updated = await app_session.execute(
        f"UPDATE {table} SET {column} = 'hijacked' WHERE {column} = $1", marker
    )
    deleted = await app_session.execute(f"DELETE FROM {table} WHERE {column} = $1", marker)
    assert updated.endswith(" 0"), updated
    assert deleted.endswith(" 0"), deleted
    assert (await owner.fetchval(f"SELECT count(*) FROM {table} WHERE {column} = $1", marker)) == 1


async def test_a_tenant_cannot_write_to_the_global_catalogue(
    owner: asyncpg.Connection, app_session: asyncpg.Connection
) -> None:
    """Comparison with NULL is never true, and `WITH CHECK` demands non-null."""
    tenant = await seeded_tenant(owner, "tenant")
    await set_tenant(app_session, tenant)

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_session.execute(
            "INSERT INTO exercise (tenant_id, slug, name, modality)"
            " VALUES (NULL, $1, 'x', 'forca')",
            f"sneaky-{secrets.token_hex(6)}",
        )


async def test_a_tenant_cannot_delete_from_the_global_catalogue(
    owner: asyncpg.Connection, app_session: asyncpg.Connection
) -> None:
    slug = f"global-{secrets.token_hex(6)}"
    await owner.execute(
        "INSERT INTO exercise (tenant_id, slug, name, modality)"
        " VALUES (NULL, $1, 'global', 'forca')",
        slug,
    )
    tenant = await seeded_tenant(owner, "tenant")
    await set_tenant(app_session, tenant)

    result = await app_session.execute("DELETE FROM exercise WHERE slug = $1", slug)
    assert result.endswith(" 0")
    assert await owner.fetchval("SELECT count(*) FROM exercise WHERE slug = $1", slug) == 1


# --------------------------------------------------------------------------- #
# The list itself
# --------------------------------------------------------------------------- #


async def test_every_table_with_a_tenant_column_has_a_policy(
    owner: asyncpg.Connection,
) -> None:
    """The check that makes a *new* table fail rather than leak quietly."""
    with_column = {
        row["table_name"]
        for row in await owner.fetch(
            """
            SELECT c.table_name FROM information_schema.columns c
              JOIN information_schema.tables t
                ON t.table_schema = c.table_schema AND t.table_name = c.table_name
             WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'
               AND t.table_type = 'BASE TABLE'
            """
        )
    }
    missing = sorted(with_column - set(TENANT_SCOPED))
    assert not missing, f"tables with tenant_id and no policy: {missing}"


@pytest.mark.parametrize("table", PARENTED)
async def test_the_with_check_half_covers_the_parented_tables(
    owner: asyncpg.Connection, app_session: asyncpg.Connection, table: str
) -> None:
    """`WITH CHECK`, for the tables the single-statement walk cannot reach.

    The parents are created as the owner and belong to the other tenant; the
    child insert is then replayed as ours, naming theirs. A policy that only
    filtered reads would let this through.
    """
    mine = await seeded_tenant(owner, table)
    theirs: int = await owner.fetchval(
        "INSERT INTO tenant (display_name) VALUES ('seed') RETURNING id"
    )
    child = await seed_parented(owner, table, theirs)

    await set_tenant(app_session, mine)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_session.execute(*child)


def test_the_suite_seeds_every_tenant_scoped_table() -> None:
    """The criterion is every tenant-scoped table, not a sample.

    Nine tables sat in `TENANT_SCOPED` with no seed, so the parametrised walks
    ran against a tenant that owned no row in them and passed without asserting
    anything -- `exercise_set`, `raw_message` and `outbound_queue` among them.
    """
    unseeded = sorted(set(TENANT_SCOPED) - set(ALL_SEEDABLE))
    assert not unseeded, f"tenant-scoped tables the isolation walk would skip: {unseeded}"


# --------------------------------------------------------------------------- #
# What a tenant may point at
# --------------------------------------------------------------------------- #
#
# RLS governs which rows a tenant can see, not which rows it can reference:
# foreign key checks run with row security bypassed by design. Migration 0003
# puts the tenant inside the key itself so the reference cannot skip it.


async def exercise_owned_by(owner: asyncpg.Connection, tenant: int | None) -> int:
    slug = f"scoped-{secrets.token_hex(6)}"
    exercise: int = await owner.fetchval(
        "INSERT INTO exercise (tenant_id, slug, name, modality)"
        " VALUES ($1, $2, 'x', 'forca') RETURNING id",
        tenant,
        slug,
    )
    return exercise


async def reference_exercise(
    connection: asyncpg.Connection,
    table: str,
    tenant: int,
    exercise: int,
    exercise_tenant: int | None,
    owner: asyncpg.Connection,
) -> None:
    """Write a row in `table` that points at `exercise`."""
    if table == "exercise_set":
        session = await owner.fetchval(
            "INSERT INTO workout_session (tenant_id, local_date)"
            " VALUES ($1, CURRENT_DATE) RETURNING id",
            tenant,
        )
        await connection.execute(
            "INSERT INTO exercise_set (tenant_id, session_id, exercise_id,"
            " exercise_tenant_id, set_index, reps, load_kg)"
            " VALUES ($1, $2, $3, $4, 1, 8, 80.0)",
            tenant,
            session,
            exercise,
            exercise_tenant,
        )
    elif table == "plan_item":
        plan = await owner.fetchval(
            "INSERT INTO workout_plan (tenant_id, name) VALUES ($1, 'plano') RETURNING id",
            tenant,
        )
        await connection.execute(
            "INSERT INTO plan_item (tenant_id, plan_id, day_label, day_order, item_order,"
            " exercise_id, exercise_tenant_id)"
            " VALUES ($1, $2, 'A', 1, 1, $3, $4)",
            tenant,
            plan,
            exercise,
            exercise_tenant,
        )
    else:
        program = await owner.fetchval(
            "INSERT INTO training_program (tenant_id, name, goal, horizon_weeks, rationale)"
            " VALUES ($1, 'p', 'forca', 8, 'r') RETURNING id",
            tenant,
        )
        await connection.execute(
            "INSERT INTO program_milestone (tenant_id, program_id, description, metric,"
            " target_value, exercise_id, exercise_tenant_id)"
            " VALUES ($1, $2, 'd', 'e1rm', 100, $3, $4)",
            tenant,
            program,
            exercise,
            exercise_tenant,
        )


REFERRING = ("exercise_set", "plan_item", "program_milestone")


@pytest.mark.parametrize("table", REFERRING)
async def test_a_tenant_cannot_point_at_another_tenants_exercise(
    owner: asyncpg.Connection, app_session: asyncpg.Connection, table: str
) -> None:
    """Claiming the exercise is global, when it is someone's private one.

    This was the open door: `MATCH SIMPLE` skips a composite key containing a
    NULL, so leaving the tenant column empty meant only the unscoped
    `exercise_id` key applied — and that one accepted any id in the table. The
    result was an existence oracle (violation means free, success means taken)
    and a way to block the owner from ever deleting the row.
    """
    mine = await seeded_tenant(owner, "tenant")
    theirs = await seeded_tenant(owner, "tenant")
    private = await exercise_owned_by(owner, theirs)

    await set_tenant(app_session, mine)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await reference_exercise(app_session, table, mine, private, None, owner)


@pytest.mark.parametrize("table", REFERRING)
async def test_a_tenant_cannot_name_another_tenant_as_the_scope(
    owner: asyncpg.Connection, app_session: asyncpg.Connection, table: str
) -> None:
    """The other way round: naming the scope honestly. The CHECK refuses it."""
    mine = await seeded_tenant(owner, "tenant")
    theirs = await seeded_tenant(owner, "tenant")
    private = await exercise_owned_by(owner, theirs)

    await set_tenant(app_session, mine)
    with pytest.raises(asyncpg.CheckViolationError):
        await reference_exercise(app_session, table, mine, private, theirs, owner)


@pytest.mark.parametrize("table", REFERRING)
async def test_a_tenant_can_still_point_at_the_shared_catalogue(
    owner: asyncpg.Connection, app_session: asyncpg.Connection, table: str
) -> None:
    """The fix has to leave the legitimate case alone.

    A scoped key that also rejected global exercises would make the catalogue
    unusable, which is a worse bug than the one being fixed and would not show
    up in a test that only checks that the leak is closed.
    """
    mine = await seeded_tenant(owner, "tenant")
    catalogue = await exercise_owned_by(owner, None)

    await set_tenant(app_session, mine)
    await reference_exercise(app_session, table, mine, catalogue, None, owner)


@pytest.mark.parametrize("table", REFERRING)
async def test_a_tenant_can_point_at_its_own_exercise(
    owner: asyncpg.Connection, app_session: asyncpg.Connection, table: str
) -> None:
    """And the other legitimate case: a private exercise, referenced by its owner."""
    mine = await seeded_tenant(owner, "tenant")
    private = await exercise_owned_by(owner, mine)

    await set_tenant(app_session, mine)
    await reference_exercise(app_session, table, mine, private, mine, owner)
