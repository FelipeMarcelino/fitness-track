"""Row level security and the pre-tenant identity boundary (spec 19.1).

The second barrier. The first is that every domain repository carries a
`tenant_id` and offers no method without one; this is what holds when a
repository forgets, which is the failure mode the spec says to plan for.

`FORCE` is not optional — a table's owner ignores RLS without it — and neither
is the principal separation from S01-T04: a superuser, or any `BYPASSRLS` role,
ignores RLS even with `FORCE`. If `DATABASE_URL` pointed at the owner the
policies below would exist and never be evaluated, which is a silent failure
rather than an error.

The one thing that cannot go through a policy is the ingress lookup. At that
moment the only facts are a channel and a hash: there is no `app.tenant_id` to
filter on, because finding the tenant *is* the operation. That gets two
`SECURITY DEFINER` functions with a fixed `search_path`, owned by a role that
can do nothing else, and `EXECUTE` revoked from `PUBLIC`.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from fittrack.db.sql import split_statements

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Every table with a `tenant_id`, from spec 19.1. The list is exhaustive on
# purpose: a table missing from it is a silent leak, needing only one repository
# to forget its predicate. `tests/test_tenant_isolation.py` walks the same list
# from the other side, so a new table without a policy fails there.
TENANT_SCOPED = (
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
)

# Rows with `tenant_id IS NULL` are the global catalogue. The resolver (spec 10)
# queries `tenant_id IS NULL OR tenant_id = :t`, so without a read policy the
# catalogue becomes invisible the moment `app.tenant_id` is set. Read only:
# writing to it is an administrative operation, not something a tenant does.
GLOBAL_READABLE = ("exercise", "exercise_alias", "workout_plan", "plan_item")

POLICIES = f"""
-- `tenant` is the root of the isolation and keys on `id`, not `tenant_id`.
ALTER TABLE tenant ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_self ON tenant
  USING (id = NULLIF(current_setting('app.tenant_id', true), '')::bigint)
  WITH CHECK (id = NULLIF(current_setting('app.tenant_id', true), '')::bigint);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[{", ".join(f"'{name}'" for name in TENANT_SCOPED)}] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    -- `current_setting(..., true)` returns NULL rather than raising when the
    -- transaction forgot to set it, and NULL compares to nothing — so a
    -- forgotten `SET LOCAL` reads zero rows instead of every row.
    --
    -- The WITH CHECK demands a non-null tenant, which is what stops a write
    -- from creating a global row: comparison with NULL is never true, so the
    -- catalogue is unreachable for INSERT, UPDATE and DELETE.
    EXECUTE format($f$
      CREATE POLICY tenant_isolation ON %I
        USING (tenant_id =
               NULLIF(current_setting('app.tenant_id', true), '')::bigint)
        WITH CHECK (tenant_id IS NOT NULL AND tenant_id =
               NULLIF(current_setting('app.tenant_id', true), '')::bigint)
    $f$, t);
  END LOOP;
END $$;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[{", ".join(f"'{name}'" for name in GLOBAL_READABLE)}] LOOP
    EXECUTE format($f$
      CREATE POLICY global_rows_readable ON %I
        FOR SELECT USING (tenant_id IS NULL)
    $f$, t);
  END LOOP;
END $$;
"""

# The pre-tenant boundary. Owned by a role that exists only to own it: NOLOGIN,
# so nothing can connect as it, and BYPASSRLS, so the two functions can see
# `channel_identity` before there is an `app.tenant_id` to see it with.
#
# `search_path` is pinned on each function. Without that, a caller could put a
# schema of their own in front and have `SECURITY DEFINER` execute it with the
# owner's privileges — the classic escalation.
IDENTITY_BOUNDARY = """
DO $$
DECLARE
  v_super boolean;
BEGIN
  SELECT rolsuper INTO v_super FROM pg_roles WHERE rolname = 'fittrack_identity';
  IF v_super IS NULL THEN
    CREATE ROLE fittrack_identity NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
  ELSIF v_super THEN
    RAISE EXCEPTION 'role fittrack_identity already exists as a superuser: the two '
                    'SECURITY DEFINER functions would run with superuser rights. '
                    'Drop or demote it before migrating.';
  ELSE
    -- The role predates this migration, or an operator made it by hand. A bare
    -- `IF NOT EXISTS` would accept whatever attributes it happens to carry, and
    -- LOGIN with a password is the one that matters: it turns a boundary that
    -- is only supposed to be reachable through two functions into a principal
    -- anyone with the password can connect as -- one that bypasses RLS.
    ALTER ROLE fittrack_identity
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS PASSWORD NULL;
  END IF;
END $$;

GRANT USAGE ON SCHEMA public TO fittrack_identity;
-- Exactly what the two functions need and nothing more. No UPDATE, no DELETE,
-- no other table: the blast radius of this role is the reason it exists.
GRANT SELECT, INSERT ON channel_identity TO fittrack_identity;
GRANT INSERT ON tenant TO fittrack_identity;
GRANT USAGE ON SEQUENCE tenant_id_seq TO fittrack_identity;
GRANT USAGE ON SEQUENCE channel_identity_id_seq TO fittrack_identity;

CREATE FUNCTION resolve_tenant_for_identity(
    p_channel channel_kind,
    p_external_id_hash bytea
) RETURNS bigint
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
STABLE
AS $$
  SELECT tenant_id FROM channel_identity
   WHERE channel = p_channel
     AND external_id_hash = p_external_id_hash
     AND revoked_at IS NULL;
$$;

-- Creates the tenant and its first identity together. Two statements from the
-- application could not be atomic across the RLS boundary — the tenant does not
-- exist yet, so no policy would admit the identity.
CREATE FUNCTION create_tenant_with_identity(
    p_channel channel_kind,
    p_external_id bytea,
    p_external_id_hash bytea,
    p_key_version smallint
) RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_tenant_id bigint;
BEGIN
  -- `nextval` rather than `INSERT ... RETURNING id`: RETURNING needs SELECT on
  -- the table, and this role is meant to hold exactly the grants section 19.1
  -- lists — INSERT on `tenant`, SELECT only on `channel_identity`. Reading the
  -- sequence keeps that true.
  v_tenant_id := nextval('tenant_id_seq');
  INSERT INTO tenant (id, state) VALUES (v_tenant_id, 'onboarding');
  INSERT INTO channel_identity
      (tenant_id, channel, external_id, external_id_hash, key_version)
  VALUES (v_tenant_id, p_channel, p_external_id, p_external_id_hash, p_key_version);
  RETURN v_tenant_id;
END;
$$;

ALTER FUNCTION resolve_tenant_for_identity(channel_kind, bytea) OWNER TO fittrack_identity;
ALTER FUNCTION create_tenant_with_identity(channel_kind, bytea, bytea, smallint)
    OWNER TO fittrack_identity;

-- PUBLIC gets EXECUTE on a new function by default, which would hand the
-- boundary to anyone who can connect.
REVOKE ALL ON FUNCTION resolve_tenant_for_identity(channel_kind, bytea) FROM PUBLIC;
REVOKE ALL ON FUNCTION create_tenant_with_identity(channel_kind, bytea, bytea, smallint)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_tenant_for_identity(channel_kind, bytea) TO fittrack_app;
GRANT EXECUTE ON FUNCTION create_tenant_with_identity(channel_kind, bytea, bytea, smallint)
    TO fittrack_app;
"""

DROP = f"""
DROP FUNCTION IF EXISTS create_tenant_with_identity(channel_kind, bytea, bytea, smallint);
DROP FUNCTION IF EXISTS resolve_tenant_for_identity(channel_kind, bytea);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[{", ".join(f"'{name}'" for name in TENANT_SCOPED)}] LOOP
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
    EXECUTE format('DROP POLICY IF EXISTS global_rows_readable ON %I', t);
    EXECUTE format('ALTER TABLE %I DISABLE ROW LEVEL SECURITY', t);
  END LOOP;
END $$;

DROP POLICY IF EXISTS tenant_self ON tenant;
ALTER TABLE tenant DISABLE ROW LEVEL SECURITY;
"""


def _run(script: str) -> None:
    for statement in split_statements(script):
        op.execute(sa.text(statement))


def upgrade() -> None:
    _run(POLICIES)
    _run(IDENTITY_BOUNDARY)


def downgrade() -> None:
    # The role survives: it may own objects in another database on the cluster,
    # and dropping a role that still holds a grant fails anyway.
    _run(DROP)
