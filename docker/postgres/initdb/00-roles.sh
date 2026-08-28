#!/bin/bash
# Create the two principals of spec 19.1 and give the runtime one its password.
#
# Numbered 00 because the Langfuse script grants CONNECT to `fittrack_app`, and
# initdb runs these in lexical order — the roles have to exist first.
#
# The password lives here and not in a migration on purpose: a migration is
# committed, and a committed password is a leaked one. The migration creates
# these roles too when they are absent, but never sets a secret — on a
# non-compose Postgres an operator does that step themselves.
set -euo pipefail

if [ -z "${FITTRACK_RUNTIME_PASSWORD:-}" ]; then
  echo 'FITTRACK_RUNTIME_PASSWORD is unset; refusing to create a passwordless login role.' >&2
  exit 1
fi

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v password="$FITTRACK_RUNTIME_PASSWORD" <<-'SQL'
	DO $$
	BEGIN
	  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fittrack_app') THEN
	    CREATE ROLE fittrack_app NOLOGIN NOSUPERUSER NOBYPASSRLS;
	  END IF;
	END $$;
SQL

# The password goes in as a psql variable and is quoted by `%L`, never
# interpolated into the SQL text. An operator-supplied value containing a single
# quote would otherwise break initialisation outright — and could inject.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v password="$FITTRACK_RUNTIME_PASSWORD" <<'SQL'
	SELECT format('CREATE ROLE fittrack_runtime LOGIN NOSUPERUSER NOBYPASSRLS '
	              'IN ROLE fittrack_app PASSWORD %L', :'password')
	 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fittrack_runtime')
	\gexec

	SELECT format('ALTER ROLE fittrack_runtime PASSWORD %L', :'password')
	 WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fittrack_runtime')
	\gexec
SQL
