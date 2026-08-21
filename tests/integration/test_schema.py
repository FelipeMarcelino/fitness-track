"""The migration must apply cleanly and enforce what the spec promises.

Several of these exist because the spec reviews found the contradiction they
guard: a constraint that rejected the rows the clarification policy requires,
uniqueness that NULL slipped past, and RLS that stopped at one table.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

pytestmark = pytest.mark.integration

TENANT_SCOPED = [
    "athlete_profile",
    "consent",
    "subscription",
    "exercise",
    "exercise_alias",
    "workout_session",
    "exercise_set",
    "session_summary",
    "body_metric",
    "health_report",
    "workout_plan",
    "plan_item",
    "training_program",
    "program_phase",
    "program_milestone",
    "raw_message",
    "processing_batch",
    "usage_ledger",
    "outbound_queue",
    "conversation_window",
]


async def _tenant(owner_conn: AsyncConnection, bsuid: str) -> int:
    row = await owner_conn.execute(
        text("INSERT INTO tenant (bsuid) VALUES (:b) RETURNING id"), {"b": bsuid}
    )
    return int(row.scalar_one())


async def _session(owner_conn: AsyncConnection, tenant_id: int) -> int:
    row = await owner_conn.execute(
        text(
            "INSERT INTO workout_session (tenant_id, local_date) "
            "VALUES (:t, CURRENT_DATE) RETURNING id"
        ),
        {"t": tenant_id},
    )
    return int(row.scalar_one())


async def _exercise(owner_conn: AsyncConnection, slug: str = "supino_reto_barra") -> int:
    row = await owner_conn.execute(
        text("INSERT INTO exercise (slug, name, modality) VALUES (:s, :s, 'forca') RETURNING id"),
        {"s": slug},
    )
    return int(row.scalar_one())


async def test_extensions_are_installed(owner_conn: AsyncConnection) -> None:
    """gin_trgm_ops does not exist without pg_trgm, and the migration would
    stop at the first trigram index."""
    rows = await owner_conn.execute(
        text("SELECT extname FROM pg_extension WHERE extname IN ('pg_trgm','unaccent')")
    )
    assert {r[0] for r in rows} == {"pg_trgm", "unaccent"}


async def test_every_table_from_the_spec_exists(owner_conn: AsyncConnection) -> None:
    rows = await owner_conn.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
    )
    present = {r[0] for r in rows}
    expected = set(TENANT_SCOPED) | {"tenant"}
    assert not (expected - present), f"missing tables: {sorted(expected - present)}"


async def test_encrypted_columns_are_bytea(owner_conn: AsyncConnection) -> None:
    """§22.2 stores these as application-encrypted bytes. A text column here
    means someone wrote plaintext."""
    expected = {
        ("health_report", "verbatim"),
        ("body_metric", "value"),
        ("athlete_profile", "injuries"),
        ("raw_message", "payload"),
        ("raw_message", "transcript"),
        ("session_summary", "narrative"),
    }
    rows = await owner_conn.execute(
        text(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='public'"
        )
    )
    types = {(r[0], r[1]): r[2] for r in rows}
    for table, column in expected:
        assert types[(table, column)] == "bytea", f"{table}.{column} is not bytea"


async def test_complete_strength_set_requires_reps(owner_conn: AsyncConnection) -> None:
    tenant_id = await _tenant(owner_conn, "b1")
    session_id = await _session(owner_conn, tenant_id)
    exercise_id = await _exercise(owner_conn)

    with pytest.raises((IntegrityError, DBAPIError)):
        await owner_conn.execute(
            text(
                "INSERT INTO exercise_set "
                "(tenant_id, session_id, exercise_id, set_index, load_kg) "
                "VALUES (:t, :s, :e, 1, 80)"
            ),
            {"t": tenant_id, "s": session_id, "e": exercise_id},
        )


async def test_incomplete_set_is_allowed_to_persist(owner_conn: AsyncConnection) -> None:
    """The clarification timeout (§8.6) records what arrived. Without the
    status escape the most common path -- the user never answers -- would fail
    persistence instead of degrading."""
    tenant_id = await _tenant(owner_conn, "b2")
    session_id = await _session(owner_conn, tenant_id)
    exercise_id = await _exercise(owner_conn)

    await owner_conn.execute(
        text(
            "INSERT INTO exercise_set "
            "(tenant_id, session_id, exercise_id, set_index, load_kg, status) "
            "VALUES (:t, :s, :e, 1, 80, 'incomplete')"
        ),
        {"t": tenant_id, "s": session_id, "e": exercise_id},
    )


async def test_weighted_strength_set_requires_load(owner_conn: AsyncConnection) -> None:
    """Reps alone yields null volume and null e1RM (§9.7)."""
    tenant_id = await _tenant(owner_conn, "b3")
    session_id = await _session(owner_conn, tenant_id)
    exercise_id = await _exercise(owner_conn)

    with pytest.raises((IntegrityError, DBAPIError)):
        await owner_conn.execute(
            text(
                "INSERT INTO exercise_set "
                "(tenant_id, session_id, exercise_id, set_index, reps) "
                "VALUES (:t, :s, :e, 1, 8)"
            ),
            {"t": tenant_id, "s": session_id, "e": exercise_id},
        )


async def test_bodyweight_set_needs_no_load(owner_conn: AsyncConnection) -> None:
    """Asking the weight of a pull-up is friction with no information."""
    tenant_id = await _tenant(owner_conn, "b4")
    session_id = await _session(owner_conn, tenant_id)
    exercise_id = await _exercise(owner_conn, "barra_fixa")

    await owner_conn.execute(
        text(
            "INSERT INTO exercise_set "
            "(tenant_id, session_id, exercise_id, set_index, reps, is_bodyweight) "
            "VALUES (:t, :s, :e, 1, 8, true)"
        ),
        {"t": tenant_id, "s": session_id, "e": exercise_id},
    )


async def test_cardio_requires_duration_not_distance(owner_conn: AsyncConnection) -> None:
    tenant_id = await _tenant(owner_conn, "b5")
    session_id = await _session(owner_conn, tenant_id)
    exercise_id = await _exercise(owner_conn, "corrida")

    with pytest.raises((IntegrityError, DBAPIError)):
        await owner_conn.execute(
            text(
                "INSERT INTO exercise_set "
                "(tenant_id, session_id, exercise_id, set_index, set_type, distance_m) "
                "VALUES (:t, :s, :e, 1, 'cardio', 5000)"
            ),
            {"t": tenant_id, "s": session_id, "e": exercise_id},
        )


async def test_idempotency_holds_when_source_message_id_is_null(
    owner_conn: AsyncConnection,
) -> None:
    """NULLS NOT DISTINCT is the whole point: without it a retry of a batch
    whose sets carry no message id would silently double the workout volume."""
    tenant_id = await _tenant(owner_conn, "b6")
    session_id = await _session(owner_conn, tenant_id)
    exercise_id = await _exercise(owner_conn)
    insert = text(
        "INSERT INTO exercise_set "
        "(tenant_id, session_id, exercise_id, set_index, load_kg, reps) "
        "VALUES (:t, :s, :e, 1, 80, 8)"
    )
    params = {"t": tenant_id, "s": session_id, "e": exercise_id}

    await owner_conn.execute(insert, params)
    with pytest.raises((IntegrityError, DBAPIError)):
        await owner_conn.execute(insert, params)


async def test_bsuid_can_be_reused_after_deletion(owner_conn: AsyncConnection) -> None:
    """A column-level UNIQUE would lock the identifier forever, so a user who
    exercised erasure (§19.5) could never come back."""
    await owner_conn.execute(
        text("INSERT INTO tenant (bsuid, deleted_at) VALUES ('reused', now())")
    )
    await owner_conn.execute(text("INSERT INTO tenant (bsuid) VALUES ('reused')"))


async def test_plan_cannot_reference_another_tenants_program(
    owner_conn: AsyncConnection,
) -> None:
    """Independent foreign keys allowed this, and the CASCADE meant deleting
    one tenant's program could delete another tenant's plan."""
    owner = await _tenant(owner_conn, "owner")
    intruder = await _tenant(owner_conn, "intruder")
    program = await owner_conn.execute(
        text(
            "INSERT INTO training_program "
            "(tenant_id, name, goal, horizon_weeks, rationale) "
            "VALUES (:t, 'p', 'hipertrofia', 8, 'r') RETURNING id"
        ),
        {"t": owner},
    )
    program_id = int(program.scalar_one())

    with pytest.raises((IntegrityError, DBAPIError)):
        await owner_conn.execute(
            text(
                "INSERT INTO workout_plan (tenant_id, program_id, name) VALUES (:t, :p, 'stolen')"
            ),
            {"t": intruder, "p": program_id},
        )


@pytest.mark.parametrize("table", TENANT_SCOPED)
async def test_row_level_security_is_enabled_and_forced(
    owner_conn: AsyncConnection, table: str
) -> None:
    """Parametrised over the list so a new tenant-scoped table without a policy
    breaks this test instead of leaking quietly.

    FORCE matters: without it the table owner bypasses RLS, and the owner is
    who the application connects as.
    """
    row = await owner_conn.execute(
        text(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = :t AND relnamespace = 'public'::regnamespace"
        ),
        {"t": table},
    )
    enabled, forced = row.one()
    assert enabled, f"{table} has no row level security"
    assert forced, f"{table} does not FORCE row level security"


@pytest.mark.parametrize("table", TENANT_SCOPED)
async def test_every_tenant_scoped_table_has_a_policy(
    owner_conn: AsyncConnection, table: str
) -> None:
    row = await owner_conn.execute(
        text("SELECT count(*) FROM pg_policies WHERE tablename = :t"), {"t": table}
    )
    assert row.scalar_one() > 0, f"{table} has no policy"


async def test_analytics_view_excludes_incomplete_and_warmup(
    owner_conn: AsyncConnection,
) -> None:
    """v_set_volume is what every analytics tool reads. An incomplete set
    leaking into it would contaminate volume and e1RM silently."""
    tenant_id = await _tenant(owner_conn, "b7")
    session_id = await _session(owner_conn, tenant_id)
    exercise_id = await _exercise(owner_conn)
    base = {"t": tenant_id, "s": session_id, "e": exercise_id}

    await owner_conn.execute(
        text(
            "INSERT INTO exercise_set "
            "(tenant_id, session_id, exercise_id, set_index, load_kg, reps) "
            "VALUES (:t, :s, :e, 1, 80, 8)"
        ),
        base,
    )
    await owner_conn.execute(
        text(
            "INSERT INTO exercise_set "
            "(tenant_id, session_id, exercise_id, set_index, load_kg, status) "
            "VALUES (:t, :s, :e, 2, 80, 'incomplete')"
        ),
        base,
    )
    await owner_conn.execute(
        text(
            "INSERT INTO exercise_set "
            "(tenant_id, session_id, exercise_id, set_index, load_kg, reps, is_warmup) "
            "VALUES (:t, :s, :e, 3, 40, 10, true)"
        ),
        base,
    )

    rows = await owner_conn.execute(
        text("SELECT set_index, volume_kg FROM v_set_volume WHERE tenant_id = :t"),
        {"t": tenant_id},
    )
    visible = {int(r[0]): r[1] for r in rows}
    assert list(visible) == [1]
    assert float(visible[1]) == 640.0


async def test_only_one_open_session_per_tenant(owner_conn: AsyncConnection) -> None:
    tenant_id = await _tenant(owner_conn, "b8")
    await _session(owner_conn, tenant_id)

    with pytest.raises((IntegrityError, DBAPIError)):
        await _session(owner_conn, tenant_id)
