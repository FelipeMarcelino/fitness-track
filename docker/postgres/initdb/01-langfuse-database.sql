-- Langfuse keeps its own schema. Giving it a separate database on the same
-- server keeps the compose file to one Postgres without letting tracing tables
-- share a namespace with the domain schema of section 5.2.
SELECT 'CREATE DATABASE langfuse'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'langfuse')\gexec
