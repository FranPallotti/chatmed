-- Azure seeding, step 1 of 2 (M5b AC10, D17): one tenant's read-only role and database.
-- Run by infra/seed-db.ps1 on the `postgres` database as the server admin:
--   psql -v db=tenant_acme -v role=chatmed_reader_acme -f /db/azure/tenant_role_and_database.sql
-- with the reader password in the environment variable READER_PW (never on a command line).
--
-- Idempotent, so a failed run can simply be run again: the role is created, or its password
-- reset to READER_PW (the value just written to Key Vault); the database is created only if
-- it is missing. CREATE DATABASE cannot run inside a transaction, which is why this is a step
-- of its own and why "seeded" is decided by the marker of step 2, not by the database.
\set ON_ERROR_STOP on

\getenv pw READER_PW
\if :{?pw}
\else
  DO $$ BEGIN RAISE EXCEPTION 'READER_PW is not set'; END $$;
\endif

-- \gexec runs the generated statement; with VERBOSITY=terse an error never echoes it.
SELECT format(CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role')
                   THEN 'ALTER ROLE %I LOGIN PASSWORD %L'
                   ELSE 'CREATE ROLE %I LOGIN PASSWORD %L' END, :'role', :'pw') \gexec
\unset pw

SELECT format('CREATE DATABASE %I', :'db')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db') \gexec

-- Only this tenant's role may connect to this tenant's database (D8).
REVOKE CONNECT, TEMPORARY ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO :"role";
