"""The pre-tenant boundary (spec 19.1).

Resolving before tenancy and revoking a dead destination cannot use an ordinary
runtime table write. Narrow `SECURITY DEFINER` functions are the entire surface
of that exception, so the tests here are as much about what the application
still **cannot** do as about what it can.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from fittrack.db.engine import split_ssl_arguments
from fittrack.db.migrations.versions._0002_row_level_security import IDENTITY_BOUNDARY
from fittrack.db.sql import split_statements
from fittrack.repositories.base import tenant_transaction
from fittrack.security.crypto import ColumnCipher, Keyring, identity_aad
from fittrack.security.identity_hash import identity_hash
from fittrack.services.identity import IdentityService, ResolvedIdentity
from tests.conftest import CA_FILE

PEPPER = b"a-bootstrap-pepper-of-sufficient-length!"


@pytest.fixture
def cipher() -> ColumnCipher:
    return ColumnCipher(Keyring(keys={1: b"\x44" * 32}, active_version=1))


@pytest.fixture
async def app_session(app_dsn: str, migrated: None) -> AsyncIterator[AsyncSession]:
    """A SQLAlchemy session as the unprivileged principal.

    The service under test is the one thing the application runs before it has a
    tenant, so it has to be exercised through the principal that will run it.
    """
    url, ssl_args = split_ssl_arguments(
        app_dsn.replace("postgresql://", "postgresql+asyncpg://")
        + f"?sslmode=verify-full&sslrootcert={CA_FILE}"
    )
    engine = create_async_engine(url, connect_args=ssl_args)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def service(app_session: AsyncSession, cipher: ColumnCipher) -> IdentityService:
    return IdentityService(app_session, cipher, PEPPER)


# --------------------------------------------------------------------------- #
# Identity resolution and creation
# --------------------------------------------------------------------------- #


async def test_first_contact_creates_a_tenant_and_its_identity(
    service: IdentityService, app_session: AsyncSession, owner: asyncpg.Connection
) -> None:
    external_id = f"chat-{secrets.token_hex(6)}"
    resolved = await service.resolve_or_create("telegram", external_id)
    await app_session.commit()

    assert resolved.created
    identity = await owner.fetchrow(
        "SELECT tenant_id, channel, key_version FROM channel_identity WHERE external_id_hash = $1",
        service.hash_of("telegram", external_id),
    )
    assert identity["tenant_id"] == resolved.tenant_id
    assert identity["channel"] == "telegram"


async def test_the_stored_identifier_is_encrypted_and_readable(
    service: IdentityService,
    app_session: AsyncSession,
    owner: asyncpg.Connection,
    cipher: ColumnCipher,
) -> None:
    external_id = f"chat-{secrets.token_hex(6)}"
    await service.resolve_or_create("telegram", external_id)
    await app_session.commit()

    digest = service.hash_of("telegram", external_id)
    stored = await owner.fetchval(
        "SELECT external_id FROM channel_identity WHERE external_id_hash = $1", digest
    )
    assert external_id.encode() not in bytes(stored)
    assert (
        cipher.decrypt(
            bytes(stored), identity_aad(channel="telegram", external_id_hash=digest)
        ).decode()
        == external_id
    )


async def test_a_second_message_resolves_rather_than_creating(
    service: IdentityService, app_session: AsyncSession
) -> None:
    """Otherwise one person's history fragments across two tenants (spec 1.3)."""
    external_id = f"chat-{secrets.token_hex(6)}"
    first = await service.resolve_or_create("telegram", external_id)
    await app_session.commit()

    second = await service.resolve_or_create("telegram", external_id)
    await app_session.commit()

    assert second.tenant_id == first.tenant_id
    assert not second.created


async def test_an_unknown_account_resolves_to_nothing(service: IdentityService) -> None:
    assert await service.resolve("telegram", f"absent-{secrets.token_hex(6)}") is None


async def test_a_revoked_identity_does_not_resolve(
    service: IdentityService, app_session: AsyncSession, owner: asyncpg.Connection
) -> None:
    """Revoked means the account no longer maps here — a re-link is a new decision."""
    external_id = f"chat-{secrets.token_hex(6)}"
    await service.resolve_or_create("telegram", external_id)
    await app_session.commit()

    await owner.execute(
        "UPDATE channel_identity SET revoked_at = now(), is_primary = false"
        " WHERE external_id_hash = $1",
        service.hash_of("telegram", external_id),
    )
    assert await service.resolve("telegram", external_id) is None


async def test_the_same_identifier_on_two_channels_is_two_tenants(
    service: IdentityService, app_session: AsyncSession
) -> None:
    """A Telegram chat.id and a WhatsApp bsuid are different namespaces (spec 1.3)."""
    external_id = f"same-{secrets.token_hex(6)}"
    telegram = await service.resolve_or_create("telegram", external_id)
    await app_session.commit()
    whatsapp = await service.resolve_or_create("whatsapp", external_id)
    await app_session.commit()

    assert telegram.tenant_id != whatsapp.tenant_id


async def test_a_concurrent_first_contact_resolves_to_one_tenant(
    app_dsn: str, cipher: ColumnCipher, owner: asyncpg.Connection, migrated: None
) -> None:
    """The race the unique index is there to lose gracefully.

    Two webhooks for an unknown account can both find nothing and both try to
    create. One wins; the other must resolve to the winner, not raise and not
    make a second tenant.
    """
    import asyncio

    external_id = f"race-{secrets.token_hex(6)}"

    async def contact() -> int:
        url, ssl_args = split_ssl_arguments(
            app_dsn.replace("postgresql://", "postgresql+asyncpg://")
            + f"?sslmode=verify-full&sslrootcert={CA_FILE}"
        )
        engine = create_async_engine(url, connect_args=ssl_args)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                result = await IdentityService(session, cipher, PEPPER).resolve_or_create(
                    "telegram", external_id
                )
                await session.commit()
                return result.tenant_id
        finally:
            await engine.dispose()

    first, second = await asyncio.gather(contact(), contact())
    assert first == second

    assert (
        await owner.fetchval(
            "SELECT count(*) FROM channel_identity WHERE external_id_hash = $1",
            identity_hash("telegram", external_id, PEPPER),
        )
        == 1
    )


# --------------------------------------------------------------------------- #
# What the boundary does *not* open
# --------------------------------------------------------------------------- #


async def test_the_application_cannot_read_the_identity_table_directly(
    app_session: AsyncSession,
) -> None:
    """The functions are the exception, not a general escape hatch.

    Without a tenant context the policy admits nothing, so a direct query
    returns zero rows — which is the point: the only way through is the
    boundary.
    """
    from sqlalchemy import text

    count = await app_session.scalar(text("SELECT count(*) FROM channel_identity"))
    assert count == 0


async def test_the_functions_run_as_a_role_the_application_is_not(
    owner: asyncpg.Connection,
) -> None:
    for name in (
        "resolve_tenant_for_identity",
        "create_tenant_with_identity",
        "revoke_channel_identity",
    ):
        row = await owner.fetchrow(
            """
            SELECT p.prosecdef, r.rolname AS owner, p.proconfig
              FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner
             WHERE p.proname = $1
            """,
            name,
        )
        assert row["prosecdef"], f"{name} is not SECURITY DEFINER"
        assert row["owner"] == "fittrack_identity"
        # A caller could otherwise put their own schema in front and have the
        # definer execute it with the owner's privileges.
        assert "search_path=public, pg_temp" in (row["proconfig"] or [])


async def test_the_owning_role_cannot_log_in(owner: asyncpg.Connection) -> None:
    role = await owner.fetchrow(
        "SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles"
        " WHERE rolname = 'fittrack_identity'"
    )
    assert not role["rolcanlogin"], "the definer role must not be a principal"
    assert not role["rolsuper"]
    # BYPASSRLS is the whole reason it exists: the functions read
    # channel_identity before there is an app.tenant_id to read it with.
    assert role["rolbypassrls"]


async def test_public_cannot_execute_the_boundary(owner: asyncpg.Connection) -> None:
    """A new function grants EXECUTE to PUBLIC by default."""
    for name in (
        "resolve_tenant_for_identity",
        "create_tenant_with_identity",
        "revoke_channel_identity",
    ):
        assert not await owner.fetchval(
            "SELECT has_function_privilege('public', p.oid, 'EXECUTE')"
            "  FROM pg_proc p WHERE p.proname = $1",
            name,
        ), f"PUBLIC can execute {name}"
        assert await owner.fetchval(
            "SELECT has_function_privilege('fittrack_app', p.oid, 'EXECUTE')"
            "  FROM pg_proc p WHERE p.proname = $1",
            name,
        ), f"fittrack_app cannot execute {name}"


async def test_the_definer_role_has_only_the_grants_it_needs(
    owner: asyncpg.Connection,
) -> None:
    """Its blast radius is the reason it exists, so the grants are asserted exactly."""
    grants = {
        (row["table_name"], row["privilege_type"])
        for row in await owner.fetch(
            """
            SELECT table_name, privilege_type FROM information_schema.role_table_grants
             WHERE grantee = 'fittrack_identity'
            """
        )
    }
    assert grants == {
        ("channel_identity", "SELECT"),
        ("channel_identity", "INSERT"),
        ("tenant", "INSERT"),
    }, f"unexpected table grants: {sorted(grants)}"

    column_grants = {
        (row["table_name"], row["column_name"], row["privilege_type"])
        for row in await owner.fetch(
            """
            SELECT table_name, column_name, privilege_type
              FROM information_schema.column_privileges
             WHERE grantee = 'fittrack_identity' AND privilege_type = 'UPDATE'
            """
        )
    }
    assert column_grants == {
        ("channel_identity", "is_primary", "UPDATE"),
        ("channel_identity", "revoked_at", "UPDATE"),
    }, f"unexpected column grants: {sorted(column_grants)}"

    # `role_table_grants` only reports relations, so on its own the assertion
    # above is blind to sequences, schemas and functions — which is most of the
    # ways a role's reach can grow. Asserting the blast radius means asserting
    # all of it.
    usage = {
        (row["object_type"], row["object_name"], row["privilege_type"])
        for row in await owner.fetch(
            """
            SELECT object_type, object_name, privilege_type
              FROM information_schema.role_usage_grants
             WHERE grantee = 'fittrack_identity'
            """
        )
    }
    assert usage == {
        ("SEQUENCE", "tenant_id_seq", "USAGE"),
        ("SEQUENCE", "channel_identity_id_seq", "USAGE"),
    }, f"unexpected usage grants: {sorted(usage)}"

    routines = {
        row["routine_name"]
        for row in await owner.fetch(
            """
            SELECT routine_name FROM information_schema.role_routine_grants
             WHERE grantee = 'fittrack_identity'
            """
        )
    }
    # The three it owns, and nothing else. Ownership reports as a grant here,
    # so this is the boundary itself rather than an extra privilege.
    assert routines == {
        "resolve_tenant_for_identity",
        "create_tenant_with_identity",
        "revoke_channel_identity",
    }, f"unexpected routine grants: {sorted(routines)}"

    schemas = {
        row["nspname"]
        for row in await owner.fetch(
            "SELECT nspname FROM pg_namespace"
            " WHERE has_schema_privilege('fittrack_identity', nspname, 'USAGE')"
            "   AND nspname NOT LIKE 'pg_%'"
        )
    }
    assert schemas == {"public", "information_schema"}, f"unexpected schemas: {sorted(schemas)}"


async def _commit_identity_elsewhere(app_dsn: str, cipher: ColumnCipher, external_id: str) -> int:
    """Another connection wins the race and commits, as a real racer would."""
    url, ssl_args = split_ssl_arguments(
        app_dsn.replace("postgresql://", "postgresql+asyncpg://")
        + f"?sslmode=verify-full&sslrootcert={CA_FILE}"
    )
    engine = create_async_engine(url, connect_args=ssl_args)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as other:
            winner = await IdentityService(other, cipher, PEPPER).resolve_or_create(
                "telegram", external_id
            )
            await other.commit()
            return winner.tenant_id
    finally:
        await engine.dispose()


def _force_stale_resolve(service: IdentityService, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the first lookup miss, which is what a real racer sees.

    The winner commits between the lookup and the insert, so the racer's
    `resolve` answered `None` a moment before the row existed.
    """
    real_resolve = service.resolve
    calls = {"n": 0}

    async def stale(channel: str, external_id: str) -> int | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await real_resolve(channel, external_id)

    monkeypatch.setattr(service, "resolve", stale)


def _fail_the_create(
    service: IdentityService, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Break only `create_tenant_with_identity`.

    Patching `scalar` outright breaks the `resolve` at the top of
    `resolve_or_create` too, so the exception never reaches the `except` clause
    under test — which is how the first version of this test passed against the
    very code it was written to reject.
    """
    real_scalar = service._session.scalar

    async def selective(statement: Any, *args: Any, **kwargs: Any) -> Any:
        if "create_tenant_with_identity" in str(statement):
            raise error
        return await real_scalar(statement, *args, **kwargs)

    monkeypatch.setattr(service._session, "scalar", selective)


async def test_a_lost_race_does_not_roll_back_the_caller(
    app_dsn: str,
    app_session: AsyncSession,
    service: IdentityService,
    cipher: ColumnCipher,
    owner: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing the race undoes one insert, not the whole transaction.

    The handler that calls this has usually already written in the same
    transaction — the raw message, at least. A blanket `session.rollback()`
    threw that away too, silently, on a path whose whole point is that nothing
    went wrong.

    The stale read is forced rather than raced: `resolve` is made to answer
    `None` once, which is exactly what a real racer sees when the winner
    commits between the lookup and the insert.
    """
    contested = f"lost-{secrets.token_hex(6)}"
    ours = f"ours-{secrets.token_hex(6)}"

    url, ssl_args = split_ssl_arguments(
        app_dsn.replace("postgresql://", "postgresql+asyncpg://")
        + f"?sslmode=verify-full&sslrootcert={CA_FILE}"
    )
    engine = create_async_engine(url, connect_args=ssl_args)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as other:
            winner = await IdentityService(other, cipher, PEPPER).resolve_or_create(
                "telegram", contested
            )
            await other.commit()
    finally:
        await engine.dispose()

    # Work the caller did before the contested lookup, still uncommitted.
    earlier = await service.resolve_or_create("telegram", ours)
    assert earlier.created

    real_resolve = service.resolve
    calls = {"n": 0}

    async def stale(channel: str, external_id: str) -> int | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await real_resolve(channel, external_id)

    monkeypatch.setattr(service, "resolve", stale)

    result = await service.resolve_or_create("telegram", contested)
    assert result == ResolvedIdentity(tenant_id=winner.tenant_id, created=False)

    await app_session.commit()

    assert (
        await owner.fetchval(
            "SELECT count(*) FROM channel_identity WHERE external_id_hash = $1",
            identity_hash("telegram", ours, PEPPER),
        )
        == 1
    ), "the caller's earlier insert was rolled back by someone else's lost race"


async def test_a_real_failure_is_not_mistaken_for_a_lost_race(
    app_dsn: str,
    service: IdentityService,
    cipher: ColumnCipher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a uniqueness conflict means "someone got here first".

    The failure has to coincide with a concurrent create, or the test proves
    nothing: with no winner to find, the follow-up `resolve` returns `None` and
    even a bare `except Exception` re-raises. It is when a winner *does* exist
    that the two differ — the broad catch answers with that tenant and buries
    the fault, the narrow one lets it through.
    """
    contested = f"real-{secrets.token_hex(6)}"
    await _commit_identity_elsewhere(app_dsn, cipher, contested)

    _force_stale_resolve(service, monkeypatch)
    boom = OperationalError("create_tenant_with_identity", {}, Exception("connection lost"))
    _fail_the_create(service, monkeypatch, boom)

    with pytest.raises(OperationalError):
        await service.resolve_or_create("telegram", contested)


async def test_a_constraint_that_is_not_a_race_is_not_swallowed(
    app_dsn: str,
    service: IdentityService,
    cipher: ColumnCipher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`IntegrityError` spans all of SQLSTATE 23, and only `23505` is a race.

    A foreign-key or check violation arriving while another worker legitimately
    created the same identity would otherwise be reported as a clean
    `created=False`.
    """
    contested = f"fk-{secrets.token_hex(6)}"
    await _commit_identity_elsewhere(app_dsn, cipher, contested)

    _force_stale_resolve(service, monkeypatch)

    class ForeignKeyViolationError(Exception):
        sqlstate = "23503"

    violation = IntegrityError("create_tenant_with_identity", {}, ForeignKeyViolationError())
    _fail_the_create(service, monkeypatch, violation)

    with pytest.raises(IntegrityError):
        await service.resolve_or_create("telegram", contested)


async def test_the_migration_normalises_a_role_that_already_exists(
    owner: asyncpg.Connection,
) -> None:
    """`IF NOT EXISTS` accepts whatever attributes it finds, which is the bug.

    A `fittrack_identity` left over from an older run — or made by hand — can
    carry LOGIN and a password. That turns a boundary reachable only through
    named functions into a principal anyone with the password can connect as,
    and one that bypasses RLS at that.
    """
    guard = split_statements(IDENTITY_BOUNDARY)[0]
    assert "fittrack_identity" in guard and guard.lstrip().startswith("DO")

    await owner.execute("ALTER ROLE fittrack_identity LOGIN PASSWORD 'let-me-in'")
    try:
        assert await owner.fetchval(
            "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'fittrack_identity'"
        )
        await owner.execute(guard)
    finally:
        await owner.execute(
            "ALTER ROLE fittrack_identity NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
            " BYPASSRLS PASSWORD NULL"
        )

    role = await owner.fetchrow(
        "SELECT r.rolcanlogin, r.rolsuper, r.rolbypassrls,"
        "       a.rolpassword IS NOT NULL AS has_password"
        "  FROM pg_roles r JOIN pg_authid a ON a.oid = r.oid"
        " WHERE r.rolname = 'fittrack_identity'"
    )
    assert not role["rolcanlogin"]
    assert not role["rolsuper"]
    assert role["rolbypassrls"]
    assert not role["has_password"], "NOLOGIN alone leaves the password in place"


async def test_the_boundary_role_has_no_members(owner: asyncpg.Connection) -> None:
    """NOLOGIN stops connecting *as* it, not reaching it from somewhere else.

    A `GRANT fittrack_identity TO fittrack_app` would let the runtime — which
    inherits `fittrack_app` — `SET ROLE` into BYPASSRLS, and every policy in
    this migration would stop being evaluated. Nothing errors when that
    happens; the queries just return more rows.
    """
    members = await owner.fetch(
        "SELECT m.rolname FROM pg_auth_members am"
        "  JOIN pg_roles m ON m.oid = am.member"
        "  JOIN pg_roles g ON g.oid = am.roleid"
        " WHERE g.rolname = 'fittrack_identity'"
    )
    assert not [row["rolname"] for row in members]

    for role in ("fittrack_app", "fittrack_runtime"):
        assert not await owner.fetchval(
            "SELECT pg_has_role($1, 'fittrack_identity', 'USAGE')", role
        ), f"{role} can SET ROLE into the boundary"


async def test_the_migration_revokes_a_leftover_membership(owner: asyncpg.Connection) -> None:
    """The guard has to fix what it finds, not just what it creates.

    A cluster migrated by an older revision — or an operator being helpful —
    can arrive with the membership already granted, and the role-attribute
    branch above would leave it in place.
    """
    guard = split_statements(IDENTITY_BOUNDARY)[1]
    assert "pg_auth_members" in guard, "the membership guard moved"

    await owner.execute("GRANT fittrack_identity TO fittrack_app")
    try:
        assert await owner.fetchval(
            "SELECT pg_has_role('fittrack_app', 'fittrack_identity', 'USAGE')"
        )
        await owner.execute(guard)
    finally:
        await owner.execute("REVOKE fittrack_identity FROM fittrack_app")

    assert not await owner.fetchval(
        "SELECT pg_has_role('fittrack_app', 'fittrack_identity', 'USAGE')"
    )


async def test_the_application_cannot_write_to_the_identity_table(
    app_session: AsyncSession, owner: asyncpg.Connection
) -> None:
    """Linking or revoking an account goes through the boundary or not at all.

    `ALTER DEFAULT PRIVILEGES` handed the application DML on every table, so it
    could add a second identity for itself, or set `revoked_at`, without ever
    calling a named boundary function.
    """
    tenant: int = await owner.fetchval(
        "INSERT INTO tenant (display_name) VALUES ('writes') RETURNING id"
    )
    marker = secrets.token_bytes(16)
    await owner.execute(
        "INSERT INTO channel_identity (tenant_id, channel, external_id, external_id_hash)"
        " VALUES ($1, 'telegram', $2, $2)",
        tenant,
        marker,
    )

    async with tenant_transaction(app_session, tenant):
        # Readable: RLS scopes it to this tenant, same as every other table.
        assert (
            await app_session.scalar(
                text("SELECT count(*) FROM channel_identity WHERE external_id_hash = :h"),
                {"h": marker},
            )
        ) == 1

        for statement in (
            "INSERT INTO channel_identity (tenant_id, channel, external_id, external_id_hash)"
            " VALUES (:t, 'telegram', :h, :h)",
            "UPDATE channel_identity SET revoked_at = now() WHERE external_id_hash = :h",
            "DELETE FROM channel_identity WHERE external_id_hash = :h",
        ):
            # The error has to leave the savepoint, or SQLAlchemy tries to
            # RELEASE one that Postgres has already put into a failed state.
            try:
                async with app_session.begin_nested():
                    await app_session.execute(
                        text(statement), {"t": tenant, "h": secrets.token_bytes(16)}
                    )
            except DBAPIError as error:
                # 42501, not "matched no rows": the privilege is gone, so the
                # statement never reaches the policy.
                assert getattr(error.orig, "sqlstate", None) == "42501", statement
            else:
                pytest.fail(f"the application was allowed to run: {statement}")
