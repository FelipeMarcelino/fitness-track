"""Initial schema (spec 5.2) plus the roles of section 19.1.

Written as raw SQL rather than SQLAlchemy operations, deliberately. The schema
leans on partial unique indexes, `NULLS NOT DISTINCT`, composite
tenant-qualified foreign keys, a generated column and a view — none of which
survive a round trip through SQLAlchemy metadata without losing the property
that made them worth writing. The spec's SQL is the reference, and
`tests/integration/test_schema_contract.py` checks the database against it.

The whole of section 5.2 lands in one migration on purpose (spec 24): the
encrypted columns are `BYTEA` from the first `CREATE TABLE`, so no conversion
migration ever has to exist. Converting later would mean reading, encrypting and
rewriting every row with the service stopped.

Row level security policies are **not** here. They land with the repositories
that have to satisfy them (S01-T06), so that the test proving isolation and the
policy enforcing it arrive together.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from fittrack.db.sql import split_statements

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SCHEMA = """
-- Required by the schema itself: `gin_trgm_ops` does not exist without pg_trgm,
-- and alias normalisation (section 10) needs unaccent.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE TYPE plan_tier    AS ENUM ('free', 'pro', 'trial');
CREATE TYPE tenant_state AS ENUM ('onboarding', 'active', 'suspended', 'deleted');
CREATE TYPE channel_kind AS ENUM ('telegram', 'whatsapp');

-- The tenant is the USER, not the messenger account. It carries no channel
-- identifier: that lives in channel_identity, so the same person can arrive by
-- Telegram and link WhatsApp later without fragmenting their history.
CREATE TABLE tenant (
    id              BIGSERIAL PRIMARY KEY,
    display_name    TEXT,
    locale          TEXT NOT NULL DEFAULT 'pt-BR',
    timezone        TEXT NOT NULL DEFAULT 'America/Sao_Paulo',
    state           tenant_state NOT NULL DEFAULT 'onboarding',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
);

CREATE TABLE channel_identity (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    channel           channel_kind NOT NULL,
    external_id       BYTEA NOT NULL,
    -- Deterministic HMAC with a pepper, for the ingress lookup: an AES-GCM
    -- ciphertext has a random nonce and is therefore not searchable. Without
    -- this column, resolving a webhook would mean scanning the table and
    -- decrypting row by row (spec 22.2).
    external_id_hash  BYTEA NOT NULL,
    key_version       SMALLINT NOT NULL DEFAULT 1,
    is_primary        BOOLEAN NOT NULL DEFAULT true,
    linked_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ,
    revoked_at        TIMESTAMPTZ
);
-- Uniqueness among living identities only: UNIQUE on the column would stop
-- someone re-registering the same account after erasure (LGPD, section 19.5).
CREATE UNIQUE INDEX ux_channel_identity_active
    ON channel_identity(channel, external_id_hash) WHERE revoked_at IS NULL;
CREATE UNIQUE INDEX ux_channel_identity_primary
    ON channel_identity(tenant_id) WHERE is_primary AND revoked_at IS NULL;
CREATE INDEX ix_channel_identity_tenant ON channel_identity(tenant_id);
ALTER TABLE channel_identity
    ADD CONSTRAINT uq_channel_identity_scope UNIQUE (id, tenant_id, channel);
-- Target for children that need identity + tenant but not the channel.
ALTER TABLE channel_identity
    ADD CONSTRAINT uq_channel_identity_tenant UNIQUE (id, tenant_id);

CREATE TYPE consent_kind AS ENUM (
    'terms', 'workout_data', 'health_data', 'proactive_msg', 'model_training'
);

CREATE TABLE consent (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    kind        consent_kind NOT NULL,
    granted     BOOLEAN NOT NULL,
    text_hash   TEXT NOT NULL,
    version     TEXT NOT NULL,
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ
);
CREATE INDEX ix_consent_tenant_kind ON consent(tenant_id, kind, granted_at DESC);

CREATE TABLE athlete_profile (
    tenant_id            BIGINT PRIMARY KEY REFERENCES tenant(id) ON DELETE CASCADE,
    goal                 TEXT,
    experience_level     TEXT,
    training_days_week   SMALLINT,
    session_minutes      SMALLINT,
    equipment_access     TEXT[],
    injuries             BYTEA,
    injuries_key_version SMALLINT NOT NULL DEFAULT 1,
    preferences          JSONB DEFAULT '{}'::jsonb,
    persona_style        TEXT DEFAULT 'parceiro',
    onboarded_at         TIMESTAMPTZ,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE subscription (
    id                  BIGSERIAL PRIMARY KEY,
    tenant_id           BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    tier                plan_tier NOT NULL DEFAULT 'free',
    provider            TEXT,
    provider_sub_id     TEXT,
    status              TEXT NOT NULL,
    current_period_end  TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_subscription_active
    ON subscription(tenant_id) WHERE status = 'active';

CREATE TYPE movement_pattern AS ENUM (
    'empurrar_horizontal','empurrar_vertical','puxar_horizontal','puxar_vertical',
    'agachamento','dobradica_quadril','avanco','core','isolado','locomocao','outro'
);

CREATE TABLE exercise (
    id                  BIGSERIAL PRIMARY KEY,
    slug                TEXT NOT NULL,
    name                TEXT NOT NULL,
    tenant_id           BIGINT REFERENCES tenant(id) ON DELETE CASCADE,
    modality            TEXT NOT NULL,
    primary_muscles     TEXT[] NOT NULL DEFAULT '{}',
    secondary_muscles   TEXT[] NOT NULL DEFAULT '{}',
    equipment           TEXT,
    pattern             movement_pattern,
    unilateral          BOOLEAN NOT NULL DEFAULT false,
    default_set_type    TEXT NOT NULL DEFAULT 'strength',
    execution_notes     TEXT,
    substitutes         BIGINT[] DEFAULT '{}',
    status              TEXT NOT NULL DEFAULT 'active',
    merged_into         BIGINT REFERENCES exercise(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_exercise_slug_global
    ON exercise(slug) WHERE tenant_id IS NULL;
CREATE UNIQUE INDEX ux_exercise_slug_tenant
    ON exercise(tenant_id, slug) WHERE tenant_id IS NOT NULL;
CREATE INDEX ix_exercise_name_trgm ON exercise USING gin (name gin_trgm_ops);
-- Candidate key for children that must prove the exercise's scope. `id` is
-- already unique, so this adds no restriction — only a target.
ALTER TABLE exercise ADD CONSTRAINT uq_exercise_scope UNIQUE (id, tenant_id);

CREATE TABLE exercise_alias (
    id          BIGSERIAL PRIMARY KEY,
    exercise_id BIGINT NOT NULL REFERENCES exercise(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL,
    normalized  TEXT NOT NULL,
    tenant_id   BIGINT REFERENCES tenant(id) ON DELETE CASCADE,
    source      TEXT NOT NULL DEFAULT 'curated',
    hits        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_alias_normalized ON exercise_alias(normalized);
CREATE INDEX ix_alias_norm_trgm  ON exercise_alias USING gin (normalized gin_trgm_ops);

CREATE TYPE session_status AS ENUM ('open', 'closed_auto', 'closed_explicit', 'discarded');

CREATE TABLE workout_session (
    id               BIGSERIAL PRIMARY KEY,
    tenant_id        BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    status           session_status NOT NULL DEFAULT 'open',
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at         TIMESTAMPTZ,
    local_date       DATE NOT NULL,
    label            TEXT,
    location         TEXT,
    notes            TEXT,
    plan_item_id     BIGINT,
    CONSTRAINT ck_session_dates CHECK (ended_at IS NULL OR ended_at >= started_at)
);
ALTER TABLE workout_session ADD CONSTRAINT uq_session_tenant UNIQUE (id, tenant_id);
CREATE UNIQUE INDEX ux_session_one_open
    ON workout_session(tenant_id) WHERE status = 'open';
CREATE INDEX ix_session_tenant_date ON workout_session(tenant_id, local_date DESC);
CREATE INDEX ix_session_open_activity
    ON workout_session(last_activity_at) WHERE status = 'open';

CREATE TYPE set_type   AS ENUM ('strength', 'cardio', 'isometric', 'interval');
CREATE TYPE set_status AS ENUM ('complete', 'incomplete');

CREATE TABLE exercise_set (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    -- Tenant-qualified FK: without it a set of tenant A could point at a
    -- session of tenant B, and deleting B's session would cascade away A's set.
    -- RLS does not cover this — it validates the new row, not the referential
    -- integrity with the parent.
    session_id      BIGINT NOT NULL,
    exercise_id     BIGINT NOT NULL REFERENCES exercise(id),
    -- The scope of the referenced exercise: NULL for a global one, the owning
    -- tenant for a private one. Denormalised on write like `is_bodyweight`,
    -- because a CHECK cannot consult another table — and without it a set could
    -- name another tenant's private exercise, which RLS does not prevent (it
    -- checks the set's tenant, not the exercise's).
    exercise_tenant_id BIGINT,
    set_type        set_type NOT NULL DEFAULT 'strength',
    set_index       SMALLINT NOT NULL,
    exercise_order  SMALLINT,

    load_kg         NUMERIC(6,2),
    reps            SMALLINT,
    rpe             NUMERIC(3,1),
    rir             SMALLINT,
    side            TEXT,

    distance_m      NUMERIC(10,2),
    duration_s      INTEGER,
    elevation_m     NUMERIC(7,2),
    avg_hr          SMALLINT,

    hold_s          INTEGER,
    rounds          SMALLINT,

    rest_s          INTEGER,
    tempo           TEXT,
    is_warmup       BOOLEAN NOT NULL DEFAULT false,
    is_failure      BOOLEAN NOT NULL DEFAULT false,
    technique       TEXT,

    status          set_status NOT NULL DEFAULT 'complete',
    -- Copied from exercise.equipment on write. Denormalised because a CHECK
    -- cannot consult another table, and the rule below depends on it.
    is_bodyweight   BOOLEAN NOT NULL DEFAULT false,
    inferred        BOOLEAN NOT NULL DEFAULT false,
    confidence      NUMERIC(3,2) NOT NULL DEFAULT 1.00,
    low_confidence  BOOLEAN GENERATED ALWAYS AS (confidence < 0.75) STORED,
    source_text     TEXT,
    source_message_id TEXT,
    corrected_from  BIGINT REFERENCES exercise_set(id),
    deleted_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The CHECK applies only to COMPLETE rows. A set whose clarification
    -- expired lands as 'incomplete' and stays out of every analysis — the
    -- user's input is never discarded, and never contaminates a calculation.
    CONSTRAINT ck_set_payload CHECK (
        status = 'incomplete'
     OR (set_type = 'strength'  AND reps IS NOT NULL
                                AND (is_bodyweight OR load_kg IS NOT NULL))
     OR (set_type = 'cardio'    AND duration_s IS NOT NULL)
     OR (set_type = 'isometric' AND hold_s IS NOT NULL)
     OR (set_type = 'interval'  AND rounds IS NOT NULL)
    ),
    CONSTRAINT ck_rpe_range CHECK (rpe IS NULL OR (rpe >= 0 AND rpe <= 10)),
    -- A private exercise must belong to the tenant recording the set.
    CONSTRAINT ck_set_exercise_scope
        CHECK (exercise_tenant_id IS NULL OR exercise_tenant_id = tenant_id),
    -- NUMERIC(3,2) accepts up to 9.99, and `low_confidence` is generated as
    -- `confidence < 0.75` — so an out-of-range value reads as *high* confidence
    -- and takes the silent-ack path instead of forcing review (spec 13.2).
    CONSTRAINT ck_confidence_range CHECK (confidence >= 0 AND confidence <= 1),
    FOREIGN KEY (session_id, tenant_id)
        REFERENCES workout_session(id, tenant_id) ON DELETE CASCADE,
    -- MATCH SIMPLE: skipped when exercise_tenant_id is NULL, which is the global
    -- case the plain FK above already covers. For a private exercise it is
    -- enforced, and the CHECK ties that scope to this row's tenant.
    FOREIGN KEY (exercise_id, exercise_tenant_id)
        REFERENCES exercise(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX ix_set_tenant_created ON exercise_set(tenant_id, created_at DESC)
    WHERE deleted_at IS NULL;
CREATE INDEX ix_set_session ON exercise_set(session_id) WHERE deleted_at IS NULL;
CREATE INDEX ix_set_tenant_exercise ON exercise_set(tenant_id, exercise_id, created_at DESC)
    WHERE deleted_at IS NULL;
CREATE INDEX ix_set_incomplete ON exercise_set(tenant_id, created_at DESC)
    WHERE status = 'incomplete' AND deleted_at IS NULL;

-- NULLS NOT DISTINCT is the part that matters (spec 17.4): without it, sets
-- with a null source_message_id would escape the uniqueness and a batch retry
-- would inflate the workout's volume silently.
CREATE UNIQUE INDEX ux_set_idempotency
    ON exercise_set (session_id, exercise_id, set_index, source_message_id)
    NULLS NOT DISTINCT
    WHERE deleted_at IS NULL;

-- security_invoker is not optional here. By default a view reads its base
-- tables as the view's *owner*, and the owner is the migration principal, which
-- bypasses row level security even under FORCE. Without this, a runtime query
-- against v_set_volume would return every tenant's sets while
-- `SET LOCAL app.tenant_id` did nothing (spec 19.1).
CREATE VIEW v_set_volume WITH (security_invoker = true) AS
SELECT s.*,
       (s.load_kg * s.reps) AS volume_kg,
       CASE WHEN s.reps BETWEEN 1 AND 12 AND s.load_kg > 0
            THEN s.load_kg * (1 + s.reps::numeric / 30) END AS e1rm_epley
FROM exercise_set s
WHERE s.deleted_at IS NULL
  AND s.is_warmup = false
  AND s.status = 'complete';

-- The session FK is tenant-qualified, like exercise_set's. Checked
-- independently, tenant A could summarise a session of tenant B: RLS would hide
-- A's row from B while it still held B's session_id as the primary key, so B
-- could never write its own — and deleting B's session would cascade into A.
CREATE TABLE session_summary (
    session_id      BIGINT NOT NULL,
    tenant_id       BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    narrative       BYTEA NOT NULL,
    key_version     SMALLINT NOT NULL DEFAULT 1,
    total_volume_kg NUMERIC(10,2),
    total_sets      SMALLINT,
    duration_min    SMALLINT,
    muscle_groups   TEXT[],
    prs             JSONB DEFAULT '[]'::jsonb,
    avg_rpe         NUMERIC(3,1),
    indexed_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (session_id),
    FOREIGN KEY (session_id, tenant_id)
        REFERENCES workout_session(id, tenant_id) ON DELETE CASCADE
);

CREATE TABLE body_metric (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    measured_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    local_date   DATE NOT NULL,
    kind         TEXT NOT NULL,
    value        BYTEA NOT NULL,
    key_version  SMALLINT NOT NULL DEFAULT 1,
    unit         TEXT NOT NULL,
    note         TEXT,
    source_text  TEXT
);
CREATE INDEX ix_body_metric ON body_metric(tenant_id, kind, local_date DESC);

CREATE TABLE health_report (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    reported_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    region         TEXT,
    severity       TEXT,
    category       TEXT NOT NULL,
    verbatim       BYTEA NOT NULL,
    key_version    SMALLINT NOT NULL DEFAULT 1,
    guidance_given TEXT,
    resolved_at    TIMESTAMPTZ
);
CREATE INDEX ix_health_active ON health_report(tenant_id) WHERE resolved_at IS NULL;

CREATE TYPE program_status AS ENUM ('draft', 'active', 'completed', 'abandoned');

CREATE TABLE training_program (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    goal            TEXT NOT NULL,
    base_template   TEXT,
    template_source TEXT,
    horizon_weeks   SMALLINT NOT NULL,
    rationale       TEXT NOT NULL,
    status          program_status NOT NULL DEFAULT 'draft',
    started_at      TIMESTAMPTZ,
    ends_at         TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_program_horizon CHECK (horizon_weeks BETWEEN 4 AND 16),
    -- Alternate key: lets the children carry a composite FK, which is what
    -- guarantees referrer and referent belong to the SAME tenant.
    UNIQUE (id, tenant_id)
);
CREATE UNIQUE INDEX ux_program_one_active
    ON training_program(tenant_id) WHERE status = 'active';

-- tenant_id is mandatory on the children. RLS is per table and does NOT follow
-- a foreign key: without this column, a direct query on program_phase would
-- read every tenant's phases.
CREATE TABLE program_phase (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    program_id        BIGINT NOT NULL,
    phase_order       SMALLINT NOT NULL,
    name              TEXT NOT NULL,
    weeks             SMALLINT NOT NULL,
    weekly_sets_min   SMALLINT,
    weekly_sets_max   SMALLINT,
    rpe_min           NUMERIC(3,1),
    rpe_max           NUMERIC(3,1),
    intensity_note    TEXT,
    is_deload         BOOLEAN NOT NULL DEFAULT false,
    UNIQUE (program_id, phase_order),
    UNIQUE (id, program_id),
    FOREIGN KEY (program_id, tenant_id)
        REFERENCES training_program(id, tenant_id) ON DELETE CASCADE
);

CREATE TABLE program_milestone (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    program_id     BIGINT NOT NULL,
    description    TEXT NOT NULL,
    metric         TEXT NOT NULL,
    exercise_id    BIGINT REFERENCES exercise(id),
    target_value   NUMERIC(10,2) NOT NULL,
    target_date    DATE,
    achieved_at    TIMESTAMPTZ,
    achieved_value NUMERIC(10,2),
    FOREIGN KEY (program_id, tenant_id)
        REFERENCES training_program(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX ix_milestone_open ON program_milestone(program_id) WHERE achieved_at IS NULL;

CREATE TABLE workout_plan (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    BIGINT REFERENCES tenant(id) ON DELETE CASCADE,
    program_id   BIGINT,
    phase_id     BIGINT,
    week_number  SMALLINT,
    name         TEXT NOT NULL,
    goal         TEXT,
    level        TEXT,
    days_week    SMALLINT,
    split_type   TEXT,
    rationale    TEXT,
    source       TEXT NOT NULL DEFAULT 'generated',
    active       BOOLEAN NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (program_id, tenant_id)
        REFERENCES training_program(id, tenant_id) ON DELETE CASCADE,
    FOREIGN KEY (phase_id, program_id)
        REFERENCES program_phase(id, program_id),
    CONSTRAINT ck_plan_phase_needs_program
        CHECK (phase_id IS NULL OR program_id IS NOT NULL),
    UNIQUE (id, tenant_id)
);

-- tenant_id exists for the same reason as on program_phase. It is NULL on the
-- items of a global plan, mirroring workout_plan.tenant_id — with MATCH SIMPLE
-- the composite FK is not checked when any column is NULL, which is what lets a
-- global item exist at all.
CREATE TABLE plan_item (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT REFERENCES tenant(id) ON DELETE CASCADE,
    plan_id         BIGINT NOT NULL,
    day_label       TEXT NOT NULL,
    day_order       SMALLINT NOT NULL,
    item_order      SMALLINT NOT NULL,
    exercise_id     BIGINT NOT NULL REFERENCES exercise(id),
    -- Same scope column, same reason, as exercise_set: without it a private
    -- plan could prescribe another tenant's private exercise, and the FK would
    -- then block that tenant from deleting it.
    exercise_tenant_id BIGINT,
    target_sets     SMALLINT,
    target_reps_min SMALLINT,
    target_reps_max SMALLINT,
    target_rpe      NUMERIC(3,1),
    rest_s          INTEGER,
    note            TEXT,
    CONSTRAINT ck_plan_item_exercise_scope
        CHECK (exercise_tenant_id IS NULL OR exercise_tenant_id = tenant_id),
    FOREIGN KEY (exercise_id, exercise_tenant_id)
        REFERENCES exercise(id, tenant_id) ON DELETE CASCADE,
    -- Two FKs on purpose. MATCH SIMPLE skips the composite one whenever a
    -- column is NULL, which is exactly the global case — leaving a global item
    -- free to name no plan at all, and surviving its plan's deletion as a
    -- readable orphan. The plain FK keeps existence and cascade for those; the
    -- composite one keeps the tenant tied for private plans.
    FOREIGN KEY (plan_id) REFERENCES workout_plan(id) ON DELETE CASCADE,
    FOREIGN KEY (plan_id, tenant_id)
        REFERENCES workout_plan(id, tenant_id) ON DELETE CASCADE
);

-- CASCADE, not SET NULL: the payload carries the user's text and audio
-- transcripts. With SET NULL the row would survive account deletion without the
-- tenant_id needed to find it, breaking erasure (section 19.5).
CREATE TABLE raw_message (
    id                 BIGSERIAL PRIMARY KEY,
    tenant_id          BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    identity_id        BIGINT NOT NULL,
    channel            channel_kind NOT NULL,
    -- On Telegram the message id is unique only within the chat, which is why
    -- the identity is part of the dedup key.
    channel_message_id TEXT NOT NULL,
    direction          TEXT NOT NULL,
    msg_type           TEXT NOT NULL,
    payload            BYTEA NOT NULL,
    transcript         BYTEA,
    key_version        SMALLINT NOT NULL DEFAULT 1,
    received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at       TIMESTAMPTZ,

    UNIQUE (identity_id, channel_message_id),
    FOREIGN KEY (identity_id, tenant_id, channel)
        REFERENCES channel_identity(id, tenant_id, channel) ON DELETE CASCADE
);
CREATE INDEX ix_raw_tenant_time ON raw_message(tenant_id, received_at DESC);

CREATE TABLE processing_batch (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    message_ids   TEXT[] NOT NULL,
    combined_text BYTEA NOT NULL,
    key_version   SMALLINT NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'pending',
    attempts      SMALLINT NOT NULL DEFAULT 0,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);

CREATE TABLE usage_ledger (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent          TEXT NOT NULL,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cached_tokens  INTEGER NOT NULL DEFAULT 0,
    audio_seconds  NUMERIC(8,2),
    cost_usd       NUMERIC(10,6) NOT NULL DEFAULT 0,
    trace_id       TEXT,
    was_fallback   BOOLEAN NOT NULL DEFAULT false
);
-- date_trunc('month', timestamptz) is STABLE, not IMMUTABLE, and an index
-- expression requires IMMUTABLE — the CREATE INDEX would fail. A range index
-- answers the same monthly quota queries.
CREATE INDEX ix_usage_tenant_time ON usage_ledger(tenant_id, occurred_at DESC);

CREATE TABLE outbound_queue (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    -- The identity, not just the channel: a tenant may have had two of the same
    -- channel over time (revoked and re-linked), and a retry has to know which
    -- one was the destination when the bubble was queued.
    identity_id   BIGINT NOT NULL,
    channel       channel_kind NOT NULL,
    kind          TEXT NOT NULL,
    payload       BYTEA NOT NULL,
    key_version   SMALLINT NOT NULL DEFAULT 1,

    -- Bubbles of one answer share group_id and go out in seq order. Without
    -- this, a worker restart would not know which had already been sent, and
    -- the retry would resend the prefix or lose the suffix (spec 13.6).
    group_id      UUID NOT NULL,
    seq           SMALLINT NOT NULL DEFAULT 0,

    -- scheduled_at is when it MAY first go out; next_retry_at is when it may be
    -- TRIED again after a failure. Eligible when both have passed.
    scheduled_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    next_retry_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at       TIMESTAMPTZ,
    attempts      SMALLINT NOT NULL DEFAULT 0,
    error_code    TEXT,
    retryable     BOOLEAN,
    last_error    TEXT,
    dead_at       TIMESTAMPTZ,

    UNIQUE (group_id, seq),
    -- Tenant- and channel-qualified, like raw_message. With a single-column FK,
    -- tenant A could enqueue a row whose identity belongs to tenant B: RLS would
    -- let A read and write it, because its tenant_id is A, while delivery went
    -- to B's account.
    FOREIGN KEY (identity_id, tenant_id, channel)
        REFERENCES channel_identity(id, tenant_id, channel) ON DELETE CASCADE
);
CREATE INDEX ix_outbound_pending
    ON outbound_queue(scheduled_at, next_retry_at, group_id, seq)
    WHERE sent_at IS NULL AND dead_at IS NULL;

-- Keyed by identity, not by tenant: the 24h window is a property of the
-- conversation on one channel, and a tenant with two channels can have one
-- window closed and the other non-existent at the same time.
-- `timestamptz + interval` is STABLE and a generated column requires IMMUTABLE,
-- so expiry is computed in the query — the only place it matters.
CREATE TABLE conversation_window (
    identity_id     BIGINT NOT NULL,
    tenant_id       BIGINT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    last_inbound_at TIMESTAMPTZ NOT NULL,

    -- Tenant-qualified for the same reason as session_summary: otherwise tenant
    -- A could claim the window of tenant B's identity, and B's inbound upsert
    -- would then collide with a row it cannot see.
    PRIMARY KEY (identity_id),
    FOREIGN KEY (identity_id, tenant_id)
        REFERENCES channel_identity(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX ix_window_tenant ON conversation_window(tenant_id);
"""

# Section 19.1. The application role must not be a superuser and must not have
# BYPASSRLS: either one ignores row level security even with FORCE, which would
# leave the policies in place and never evaluated — a silent failure, not an
# error. Passwords are never set here; `fittrack_runtime` gets its own from a
# secret outside the migration.
RUNTIME_REQUIRED = """
-- `fittrack_runtime` is NOT created here, and the difference matters. A
-- migration must not set a password (it is committed), and a LOGIN role without
-- one is worse than none: on an upgrade where the Postgres volume already
-- exists, initdb is skipped, so nothing would ever assign the password the
-- freshly generated DATABASE_URL is about to authenticate with — and every
-- service would fail to connect for a reason that looks like a network fault.
--
-- So the migration requires the principal to exist and says how to make it.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fittrack_runtime') THEN
    RAISE EXCEPTION USING
      MESSAGE = 'role fittrack_runtime does not exist',
      HINT = 'Provision it with a password before migrating. On a fresh volume '
             'docker/postgres/initdb/00-roles.sh does it. On an existing one, '
             'both roles: CREATE ROLE fittrack_app NOLOGIN NOSUPERUSER '
             'NOBYPASSRLS; CREATE ROLE fittrack_runtime LOGIN NOSUPERUSER '
             'NOBYPASSRLS IN ROLE fittrack_app PASSWORD ''<secret>''; — this '
             'revision is transactional, so the fittrack_app created moments '
             'ago is rolled back with this error.';
  END IF;
END $$;
"""

ROLES = """
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fittrack_app') THEN
    CREATE ROLE fittrack_app NOLOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
END $$;

-- A pre-existing `fittrack_app` may be anything at all, and the runtime
-- principal is about to become a member of it — so a privileged one restores
-- by `SET ROLE` the very bypass the separation exists to prevent. Checked
-- rather than assumed, memberships included.
DO $$
DECLARE inherited text[];
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'fittrack_app' AND (rolsuper OR rolbypassrls)
  ) THEN
    RAISE EXCEPTION 'fittrack_app must be NOSUPERUSER NOBYPASSRLS: either ignores '
                    'row level security even under FORCE (spec 19.1)';
  END IF;

  SELECT array_agg(g.rolname) INTO inherited
    FROM pg_auth_members m
    JOIN pg_roles r ON r.oid = m.member
    JOIN pg_roles g ON g.oid = m.roleid
   WHERE r.rolname = 'fittrack_app';
  IF inherited IS NOT NULL THEN
    RAISE EXCEPTION 'fittrack_app must inherit nothing, but is a member of: %', inherited;
  END IF;
END $$;

{runtime_guard}

-- Belt and braces: whatever created it, it must not be able to ignore RLS.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_roles
     WHERE rolname = 'fittrack_runtime' AND (rolsuper OR rolbypassrls)
  ) THEN
    RAISE EXCEPTION 'fittrack_runtime must be NOSUPERUSER NOBYPASSRLS: either '
                    'ignores row level security even under FORCE (spec 19.1)';
  END IF;
END $$;

-- The grants below go to fittrack_app alone, so a runtime principal that is not
-- a member of it migrates cleanly and then fails every query with insufficient
-- privileges. Membership is established here rather than assumed; anything
-- *extra* is refused, because "inherits only fittrack_app" is the boundary.
GRANT fittrack_app TO fittrack_runtime;

DO $$
DECLARE extra text[];
BEGIN
  SELECT array_agg(g.rolname) INTO extra
    FROM pg_auth_members m
    JOIN pg_roles r ON r.oid = m.member
    JOIN pg_roles g ON g.oid = m.roleid
   WHERE r.rolname = 'fittrack_runtime' AND g.rolname <> 'fittrack_app';
  IF extra IS NOT NULL THEN
    RAISE EXCEPTION 'fittrack_runtime must inherit only fittrack_app, but also has: %', extra;
  END IF;
END $$;

-- CONNECT is revoked from PUBLIC on this database by the initdb scripts, so
-- the privilege role has to be granted it explicitly. Without this the runtime
-- principal authenticates and is then refused the database itself — which reads
-- as a credential problem and is not one.
DO $$
BEGIN
  EXECUTE format(
    'GRANT CONNECT, TEMPORARY ON DATABASE %I TO fittrack_app', current_database()
  );
END $$;

-- PostgreSQL 15 dropped the default `CREATE` on `public` for `PUBLIC`, but a
-- database created before that keeps it — and `GRANT USAGE` does not remove it.
-- The runtime principal would still be able to create tables, against an
-- owns-nothing boundary that the test suite would report as intact, because a
-- fresh 16 cluster simply starts without the grant.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

GRANT USAGE ON SCHEMA public TO fittrack_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO fittrack_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO fittrack_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO fittrack_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO fittrack_app;

-- Alembic creates alembic_version before this revision runs, so the blanket
-- grant above reached it too. A runtime bug — or a compromised application —
-- could then rewrite the migration head and make the next deployment skip
-- revisions or try to recreate an existing schema.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE alembic_version FROM fittrack_app;
"""

DROP = """
DROP VIEW IF EXISTS v_set_volume;
DROP TABLE IF EXISTS conversation_window, outbound_queue, usage_ledger,
    processing_batch, raw_message, plan_item, workout_plan, program_milestone,
    program_phase, training_program, health_report, body_metric, session_summary,
    exercise_set, workout_session, exercise_alias, exercise, subscription,
    athlete_profile, consent, channel_identity, tenant CASCADE;
DROP TYPE IF EXISTS program_status, set_status, set_type, session_status,
    movement_pattern, consent_kind, channel_kind, tenant_state, plan_tier;
"""


def _run(script: str) -> None:
    """asyncpg prepares every statement, and a prepared statement holds one."""
    for statement in split_statements(script):
        op.execute(sa.text(statement))


def upgrade() -> None:
    _run(SCHEMA)
    _run(ROLES.format(runtime_guard=RUNTIME_REQUIRED))


def downgrade() -> None:
    # Only ever run against a disposable test database. The roles are left in
    # place: they may be shared with another database on the same cluster, and
    # dropping a role that still owns a grant elsewhere fails anyway.
    _run(DROP)
