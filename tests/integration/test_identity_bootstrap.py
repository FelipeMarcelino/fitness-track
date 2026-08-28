"""The pre-tenant boundary (spec 19.1).

The one operation that cannot run inside `SET LOCAL app.tenant_id`: at the
moment a webhook arrives there is a channel and an opaque identifier, and
finding the tenant *is* the operation. Two `SECURITY DEFINER` functions are the
entire surface of that exception, so the tests here are as much about what the
application still **cannot** do as about what it can.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator

import asyncpg
import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from fittrack.db.engine import split_ssl_arguments
from fittrack.db.migrations.versions._0002_row_level_security import IDENTITY_BOUNDARY
from fittrack.db.sql import split_statements
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
# The two operations
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
    for name in ("resolve_tenant_for_identity", "create_tenant_with_identity"):
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
    for name in ("resolve_tenant_for_identity", "create_tenant_with_identity"):
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
    }, f"unexpected grants: {sorted(grants)}"


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
    service: IdentityService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a uniqueness conflict means "someone got here first".

    A bare `except Exception` answered a dropped connection or a bad key
    version with whatever `resolve` happened to return next — or swallowed the
    fault and re-raised something unrelated to the actual cause.
    """
    boom = OperationalError("SELECT create_tenant_with_identity(...)", {}, Exception("gone"))

    async def fail(*_: object, **__: object) -> None:
        raise boom

    monkeypatch.setattr(service._session, "scalar", fail)

    with pytest.raises(OperationalError):
        await service.resolve_or_create("telegram", f"boom-{secrets.token_hex(6)}")


async def test_the_migration_normalises_a_role_that_already_exists(
    owner: asyncpg.Connection,
) -> None:
    """`IF NOT EXISTS` accepts whatever attributes it finds, which is the bug.

    A `fittrack_identity` left over from an older run — or made by hand — can
    carry LOGIN and a password. That turns a boundary reachable only through
    two functions into a principal anyone with the password can connect as, and
    one that bypasses RLS at that.
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
