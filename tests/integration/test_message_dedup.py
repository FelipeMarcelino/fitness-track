"""Deduplication of inbound messages (spec 17.4), and the scope that bounds it.

On Telegram a message id is unique only inside its chat, so the identity is part
of the key. Two people can legitimately produce the same `channel_message_id`;
one person producing it twice is a redelivery, and both channels redeliver
anything that did not get a fast 200.
"""

from __future__ import annotations

import secrets

import asyncpg
import pytest


def unique(prefix: str) -> bytes:
    """A marker no earlier run can have used.

    The database persists between runs — `make test-in-worker` after a host run
    is the normal case — and `ux_channel_identity_active` is unique across the
    whole table, not per test.
    """
    return f"{prefix}-{secrets.token_hex(8)}".encode()


async def make_tenant(owner: asyncpg.Connection, name: str) -> int:
    tenant_id: int = await owner.fetchval(
        "INSERT INTO tenant (display_name) VALUES ($1) RETURNING id", name
    )
    return tenant_id


async def make_identity(
    owner: asyncpg.Connection, tenant_id: int, marker: bytes, *, channel: str = "telegram"
) -> int:
    identity_id: int = await owner.fetchval(
        """
        INSERT INTO channel_identity (tenant_id, channel, external_id, external_id_hash)
        VALUES ($1, $2::channel_kind, $3, $4) RETURNING id
        """,
        tenant_id,
        channel,
        marker,
        marker,
    )
    return identity_id


async def insert_message(
    owner: asyncpg.Connection,
    tenant_id: int,
    identity_id: int,
    message_id: str,
    *,
    channel: str = "telegram",
) -> None:
    await owner.execute(
        """
        INSERT INTO raw_message
            (tenant_id, identity_id, channel, channel_message_id, direction, msg_type, payload)
        VALUES ($1, $2, $3::channel_kind, $4, 'inbound', 'text', $5)
        """,
        tenant_id,
        identity_id,
        channel,
        message_id,
        b"ciphertext",
    )


async def test_the_same_message_id_from_two_identities_is_not_a_collision(
    owner: asyncpg.Connection,
) -> None:
    first = await make_tenant(owner, "first")
    second = await make_tenant(owner, "second")
    one = await make_identity(owner, first, unique("identity-one"))
    two = await make_identity(owner, second, unique("identity-two"))

    await insert_message(owner, first, one, "42")
    await insert_message(owner, second, two, "42")  # must not raise

    # Scoped to this test's tenants: the database persists between runs, so a
    # global count would grow every time.
    assert (
        await owner.fetchval(
            """
            SELECT count(*) FROM raw_message
             WHERE channel_message_id = '42' AND tenant_id = ANY($1::bigint[])
            """,
            [first, second],
        )
        == 2
    )


async def test_a_repeat_from_the_same_identity_is_rejected(
    owner: asyncpg.Connection,
) -> None:
    tenant = await make_tenant(owner, "repeat")
    identity = await make_identity(owner, tenant, unique("identity-repeat"))

    await insert_message(owner, tenant, identity, "99")
    with pytest.raises(asyncpg.UniqueViolationError):
        await insert_message(owner, tenant, identity, "99")


async def test_a_message_cannot_claim_an_identity_of_another_tenant(
    owner: asyncpg.Connection,
) -> None:
    """The composite FK carries tenant and channel, not just the identity id."""
    owner_tenant = await make_tenant(owner, "owner")
    other_tenant = await make_tenant(owner, "other")
    identity = await make_identity(owner, owner_tenant, unique("identity-scope"))

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await insert_message(owner, other_tenant, identity, "1")


async def test_a_message_cannot_claim_the_wrong_channel(
    owner: asyncpg.Connection,
) -> None:
    tenant = await make_tenant(owner, "channel-scope")
    identity = await make_identity(owner, tenant, unique("identity-channel"))

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await insert_message(owner, tenant, identity, "1", channel="whatsapp")


async def test_deleting_the_tenant_takes_its_messages_with_it(
    owner: asyncpg.Connection,
) -> None:
    """CASCADE, not SET NULL: the payload is the user's text (spec 19.5)."""
    tenant = await make_tenant(owner, "erasure")
    identity = await make_identity(owner, tenant, unique("identity-erasure"))
    await insert_message(owner, tenant, identity, "1")

    await owner.execute("DELETE FROM tenant WHERE id = $1", tenant)
    assert (
        await owner.fetchval("SELECT count(*) FROM raw_message WHERE tenant_id = $1", tenant) == 0
    )


# --------------------------------------------------------------------------- #
# Cross-tenant references the schema has to refuse
# --------------------------------------------------------------------------- #


async def make_session(owner: asyncpg.Connection, tenant_id: int) -> int:
    session_id: int = await owner.fetchval(
        """
        INSERT INTO workout_session (tenant_id, local_date)
        VALUES ($1, CURRENT_DATE) RETURNING id
        """,
        tenant_id,
    )
    return session_id


async def make_exercise(owner: asyncpg.Connection, tenant_id: int | None, slug: str) -> int:
    exercise_id: int = await owner.fetchval(
        """
        INSERT INTO exercise (slug, name, tenant_id, modality)
        VALUES ($1, $1, $2, 'forca') RETURNING id
        """,
        slug,
        tenant_id,
    )
    return exercise_id


async def test_a_set_cannot_use_another_tenants_private_exercise(
    owner: asyncpg.Connection,
) -> None:
    """RLS does not cover this: it checks the set's tenant, not the exercise's."""
    mine = await make_tenant(owner, "mine")
    theirs = await make_tenant(owner, "theirs")
    session = await make_session(owner, mine)
    private = await make_exercise(owner, theirs, f"private-{secrets.token_hex(4)}")

    with pytest.raises(asyncpg.IntegrityConstraintViolationError):
        await owner.execute(
            """
            INSERT INTO exercise_set
                (tenant_id, session_id, exercise_id, exercise_tenant_id, set_index, reps, load_kg)
            VALUES ($1, $2, $3, $4, 1, 8, 80.0)
            """,
            mine,
            session,
            private,
            theirs,
        )


async def test_a_set_may_use_a_global_exercise(owner: asyncpg.Connection) -> None:
    mine = await make_tenant(owner, "global-user")
    session = await make_session(owner, mine)
    catalogued = await make_exercise(owner, None, f"global-{secrets.token_hex(4)}")

    await owner.execute(
        """
        INSERT INTO exercise_set
            (tenant_id, session_id, exercise_id, exercise_tenant_id, set_index, reps, load_kg)
        VALUES ($1, $2, $3, NULL, 1, 8, 80.0)
        """,
        mine,
        session,
        catalogued,
    )


async def test_a_set_may_use_its_own_private_exercise(owner: asyncpg.Connection) -> None:
    mine = await make_tenant(owner, "own-private")
    session = await make_session(owner, mine)
    private = await make_exercise(owner, mine, f"own-{secrets.token_hex(4)}")

    await owner.execute(
        """
        INSERT INTO exercise_set
            (tenant_id, session_id, exercise_id, exercise_tenant_id, set_index, reps, load_kg)
        VALUES ($1, $2, $3, $1, 1, 8, 80.0)
        """,
        mine,
        session,
        private,
    )


async def test_a_global_plan_item_still_needs_its_plan(owner: asyncpg.Connection) -> None:
    """MATCH SIMPLE skips the composite FK when tenant_id is NULL."""
    catalogued = await make_exercise(owner, None, f"item-{secrets.token_hex(4)}")
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await owner.execute(
            """
            INSERT INTO plan_item
                (tenant_id, plan_id, day_label, day_order, item_order, exercise_id)
            VALUES (NULL, 999999999, 'A', 1, 1, $1)
            """,
            catalogued,
        )


async def test_the_public_schema_is_not_writable_by_everyone(
    owner: asyncpg.Connection,
) -> None:
    """A pre-15 database keeps CREATE on `public` for PUBLIC, and GRANT does not remove it."""
    assert not await owner.fetchval("SELECT has_schema_privilege('public', 'public', 'CREATE')")


async def test_a_plan_item_cannot_use_another_tenants_private_exercise(
    owner: asyncpg.Connection,
) -> None:
    """Same hole as `exercise_set`: the FK would also block B's own deletion."""
    mine = await make_tenant(owner, "plan-mine")
    theirs = await make_tenant(owner, "plan-theirs")
    private = await make_exercise(owner, theirs, f"plan-private-{secrets.token_hex(4)}")
    plan = await owner.fetchval(
        "INSERT INTO workout_plan (tenant_id, name) VALUES ($1, 'A') RETURNING id", mine
    )

    with pytest.raises(asyncpg.IntegrityConstraintViolationError):
        await owner.execute(
            """
            INSERT INTO plan_item
                (tenant_id, plan_id, day_label, day_order, item_order,
                 exercise_id, exercise_tenant_id)
            VALUES ($1, $2, 'A', 1, 1, $3, $4)
            """,
            mine,
            plan,
            private,
            theirs,
        )
