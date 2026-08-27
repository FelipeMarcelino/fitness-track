#!/bin/bash
# Langfuse owns its own database and nothing else.
#
# The bootstrap user the Postgres image creates is a cluster superuser, so
# reusing it would make the separate database decorative: a compromised Langfuse
# could read every other one on the server.
#
# The password lives here and not in a migration on purpose — a migration is
# committed, and a committed password is a leaked one.
set -euo pipefail

if [ -z "${LANGFUSE_DB_PASSWORD:-}" ]; then
  echo 'LANGFUSE_DB_PASSWORD is unset; refusing to create a passwordless login role.' >&2
  exit 1
fi

# The password goes in as a psql variable and is quoted by `:'name'`, never
# interpolated into the SQL text. An operator-supplied value containing a single
# quote would otherwise break initialisation outright — and could inject.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v password="$LANGFUSE_DB_PASSWORD" -v maindb="$POSTGRES_DB" \
     -v mainuser="$POSTGRES_USER" <<'SQL'
	SELECT format('CREATE ROLE langfuse LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
	              'NOBYPASSRLS PASSWORD %L', :'password')
	 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'langfuse')
	\gexec

	SELECT format('ALTER ROLE langfuse PASSWORD %L', :'password')
	 WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'langfuse')
	\gexec

	ALTER DATABASE langfuse OWNER TO langfuse;

	-- Revoking from `langfuse` alone confines nothing: PostgreSQL grants CONNECT
	-- and TEMPORARY on every database to PUBLIC, and every role inherits those.
	-- The boundary has to be drawn at PUBLIC and the access handed back only to
	-- the roles that should have it.
	-- Granted back to the bootstrap user, not to an application role: at this
	-- point in initdb the only roles that exist are the one the image created
	-- and `langfuse`. The application principals arrive with the schema
	-- migration, which grants itself what it needs (spec 19.1).
	REVOKE CONNECT, TEMPORARY ON DATABASE :"maindb" FROM PUBLIC;
	REVOKE ALL ON DATABASE :"maindb" FROM langfuse;
	GRANT CONNECT, TEMPORARY ON DATABASE :"maindb" TO :"mainuser";

	REVOKE CONNECT, TEMPORARY ON DATABASE langfuse FROM PUBLIC;
	GRANT CONNECT, TEMPORARY ON DATABASE langfuse TO langfuse;
SQL
