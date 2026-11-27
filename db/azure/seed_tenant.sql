-- Azure seeding, step 2 of 2 (M5b AC10, D17): schema + seed of one tenant database, in one
-- transaction that ends by creating the `seed_info` marker.
-- Run by infra/seed-db.ps1 on the tenant database as the server admin:
--   psql -d tenant_acme -v reader=chatmed_reader_acme -v seedfile=/db/seed/tenant_acme.sql \
--        -f /db/azure/seed_tenant.sql
--
-- "Seeded" means the marker exists (db/azure/is_seeded.sql). Everything is one transaction,
-- so a failed run leaves no marker and no tables behind, and the next run seeds again; a run
-- on a seeded database changes nothing. The schema and seed files are the ones the local
-- database uses (db/schema, db/seed).
\set ON_ERROR_STOP on

SELECT to_regclass('public.seed_info') IS NOT NULL AS seeded \gset
\if :seeded
  \echo 'already seeded'
\else
  BEGIN;
  \i /db/schema/tenant_schema.sql
  \i :seedfile
  -- Last statement of the transaction: it exists only if everything before it succeeded.
  CREATE TABLE seed_info (
      seeded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      seed_file TEXT        NOT NULL
  );
  INSERT INTO seed_info (seed_file) VALUES (:'seedfile');
  COMMIT;
\endif
