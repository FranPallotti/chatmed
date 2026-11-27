-- Prints t when this tenant database is seeded (the seed_info marker of seed_tenant.sql
-- exists), else f. Run by infra/seed-db.ps1 with psql -tA on the tenant database, before it
-- generates a password, so a rerun on a seeded tenant changes nothing at all.
SELECT to_regclass('public.seed_info') IS NOT NULL;
