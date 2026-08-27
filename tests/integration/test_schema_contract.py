"""The schema of spec 5.2, checked against the database that migration built.

Written as a parametrised contract rather than prose so that a table, a
constraint or an index that quietly disappears fails by name. The entries that
carry real weight and would be easy to lose in a refactor are called out where
they appear.
"""

from __future__ import annotations

import asyncpg
import pytest

# Every table of section 5.2. `tenant` is the isolation root; the rest below it
# are tenant-scoped and every one of them needs RLS (section 19.1).
TENANT_SCOPED = [
    "channel_identity",
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
ALL_TABLES = ["tenant", *TENANT_SCOPED]

# Section 22.2: these are encrypted by the application and the database sees
# only bytes. They are BYTEA from the first migration precisely so that no
# conversion migration ever has to exist.
ENCRYPTED_COLUMNS = [
    ("channel_identity", "external_id"),
    ("channel_identity", "external_id_hash"),
    ("athlete_profile", "injuries"),
    ("session_summary", "narrative"),
    ("body_metric", "value"),
    ("health_report", "verbatim"),
    ("raw_message", "payload"),
    ("raw_message", "transcript"),
    ("processing_batch", "combined_text"),
    ("outbound_queue", "payload"),
]

ENUMS = {
    "plan_tier": {"free", "pro", "trial"},
    "tenant_state": {"onboarding", "active", "suspended", "deleted"},
    "channel_kind": {"telegram", "whatsapp"},
    "consent_kind": {"terms", "workout_data", "health_data", "proactive_msg", "model_training"},
    "session_status": {"open", "closed_auto", "closed_explicit", "discarded"},
    "set_type": {"strength", "cardio", "isometric", "interval"},
    "set_status": {"complete", "incomplete"},
    "program_status": {"draft", "active", "completed", "abandoned"},
    "movement_pattern": {
        "empurrar_horizontal",
        "empurrar_vertical",
        "puxar_horizontal",
        "puxar_vertical",
        "agachamento",
        "dobradica_quadril",
        "avanco",
        "core",
        "isolado",
        "locomocao",
        "outro",
    },
}


@pytest.mark.parametrize("table", ALL_TABLES)
async def test_every_table_exists(owner: asyncpg.Connection, table: str) -> None:
    assert await owner.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{table}"), table


@pytest.mark.parametrize("table", TENANT_SCOPED)
async def test_every_tenant_scoped_table_carries_tenant_id(
    owner: asyncpg.Connection, table: str
) -> None:
    """RLS is per table and does not follow a foreign key.

    Without the column on the child, a direct query on `program_phase` would
    read every tenant's phases (spec 19.1).
    """
    column = await owner.fetchrow(
        """
        SELECT is_nullable FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = $1 AND column_name = 'tenant_id'
        """,
        table,
    )
    assert column is not None, f"{table} has no tenant_id"


@pytest.mark.parametrize(("table", "column"), ENCRYPTED_COLUMNS)
async def test_encrypted_columns_are_bytea_from_the_first_migration(
    owner: asyncpg.Connection, table: str, column: str
) -> None:
    data_type = await owner.fetchval(
        """
        SELECT data_type FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = $1 AND column_name = $2
        """,
        table,
        column,
    )
    assert data_type == "bytea", f"{table}.{column} is {data_type}"


@pytest.mark.parametrize(("table", "column"), ENCRYPTED_COLUMNS)
async def test_no_index_covers_an_encrypted_column(
    owner: asyncpg.Connection, table: str, column: str
) -> None:
    """A randomised ciphertext is not searchable; an index on it is dead weight."""
    if column == "external_id_hash":
        return  # deterministic HMAC, indexed on purpose (spec 22.2)
    # A word boundary, not a substring: `external_id` is a prefix of
    # `external_id_hash`, which is indexed on purpose.
    indexed = await owner.fetch(
        """
        SELECT indexname FROM pg_indexes
         WHERE schemaname = 'public' AND tablename = $1
           AND indexdef ~ ('\\m' || $2 || '\\M')
        """,
        table,
        column,
    )
    assert not indexed, f"{table}.{column} is indexed: {[r['indexname'] for r in indexed]}"


@pytest.mark.parametrize(("name", "labels"), sorted(ENUMS.items()))
async def test_every_enum_has_its_labels(
    owner: asyncpg.Connection, name: str, labels: set[str]
) -> None:
    rows = await owner.fetch(
        """
        SELECT e.enumlabel FROM pg_enum e
          JOIN pg_type t ON t.oid = e.enumtypid
         WHERE t.typname = $1
        """,
        name,
    )
    assert {row["enumlabel"] for row in rows} == labels


@pytest.mark.parametrize("extension", ["pg_trgm", "unaccent"])
async def test_required_extensions_are_installed(owner: asyncpg.Connection, extension: str) -> None:
    """`gin_trgm_ops` does not exist without pg_trgm, and the alias index needs it."""
    assert await owner.fetchval("SELECT 1 FROM pg_extension WHERE extname = $1", extension)


@pytest.mark.parametrize(
    "index",
    [
        "ux_channel_identity_active",
        "ux_channel_identity_primary",
        "ux_exercise_slug_global",
        "ux_exercise_slug_tenant",
        "ux_session_one_open",
        "ux_set_idempotency",
        "ux_subscription_active",
        "ux_program_one_active",
        "ix_exercise_name_trgm",
        "ix_alias_norm_trgm",
        "ix_outbound_pending",
        "ix_health_active",
        "ix_set_incomplete",
    ],
)
async def test_the_indexes_that_carry_a_rule_exist(owner: asyncpg.Connection, index: str) -> None:
    assert await owner.fetchval(
        "SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = $1", index
    ), index


async def test_the_idempotency_index_treats_nulls_as_equal(
    owner: asyncpg.Connection,
) -> None:
    """NULLS NOT DISTINCT is the part that matters (spec 17.4).

    Without it, sets with a null `source_message_id` would escape the uniqueness
    and a batch retry would inflate the workout's volume silently.
    """
    definition = await owner.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ux_set_idempotency'"
    )
    assert "NULLS NOT DISTINCT" in definition


@pytest.mark.parametrize(
    ("table", "constraint"),
    [
        ("exercise_set", "ck_set_payload"),
        ("exercise_set", "ck_rpe_range"),
        ("workout_session", "ck_session_dates"),
        ("training_program", "ck_program_horizon"),
        ("workout_plan", "ck_plan_phase_needs_program"),
    ],
)
async def test_the_check_constraints_exist(
    owner: asyncpg.Connection, table: str, constraint: str
) -> None:
    assert await owner.fetchval(
        """
        SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
         WHERE t.relname = $1 AND c.conname = $2 AND c.contype = 'c'
        """,
        table,
        constraint,
    ), f"{table}.{constraint}"


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        # A set of tenant A must not be able to point at a session of tenant B:
        # deleting B's session would then cascade away A's set. RLS does not
        # cover this — it validates the new row, not referential integrity.
        ("exercise_set", ["session_id", "tenant_id"]),
        ("program_phase", ["program_id", "tenant_id"]),
        ("program_milestone", ["program_id", "tenant_id"]),
        ("workout_plan", ["program_id", "tenant_id"]),
        ("plan_item", ["plan_id", "tenant_id"]),
        ("raw_message", ["identity_id", "tenant_id", "channel"]),
    ],
)
async def test_child_tables_use_tenant_qualified_foreign_keys(
    owner: asyncpg.Connection, table: str, columns: list[str]
) -> None:
    rows = await owner.fetch(
        """
        SELECT pg_get_constraintdef(c.oid) AS definition
          FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
         WHERE t.relname = $1 AND c.contype = 'f'
        """,
        table,
    )
    wanted = ", ".join(columns)
    assert any(f"({wanted})" in row["definition"] for row in rows), (
        f"{table} has no composite FK on ({wanted}): {[r['definition'] for r in rows]}"
    )


async def test_the_volume_view_excludes_what_must_not_be_counted(
    owner: asyncpg.Connection,
) -> None:
    """Warm-up sets, deleted sets and incomplete sets never enter a calculation."""
    definition = await owner.fetchval("SELECT pg_get_viewdef('v_set_volume'::regclass, true)")
    assert "is_warmup = false" in definition
    assert "deleted_at IS NULL" in definition
    assert "'complete'" in definition


async def test_the_low_confidence_flag_is_generated(owner: asyncpg.Connection) -> None:
    generated = await owner.fetchval(
        """
        SELECT is_generated FROM information_schema.columns
         WHERE table_name = 'exercise_set' AND column_name = 'low_confidence'
        """
    )
    assert generated == "ALWAYS"
