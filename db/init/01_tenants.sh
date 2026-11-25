#!/bin/bash
# One database per tenant, each with its own read-only role (D8).
# Isolation is enforced twice: by code (tenant comes from the session) and by Postgres
# (a tenant's role can only CONNECT to its own database; PUBLIC can connect to none).
set -euo pipefail
: "${CHATMED_READER_PASSWORD:?set CHATMED_READER_PASSWORD in db/.env}"

for tenant in acme globex; do
  db="tenant_${tenant}"
  role="chatmed_reader_${tenant}"
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres \
       -v db="$db" -v role="$role" -v pw="$CHATMED_READER_PASSWORD" <<'SQL'
CREATE DATABASE :"db";
REVOKE CONNECT, TEMPORARY ON DATABASE :"db" FROM PUBLIC;
CREATE ROLE :"role" LOGIN PASSWORD :'pw';
GRANT CONNECT ON DATABASE :"db" TO :"role";
SQL
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$db" -v reader="$role" -f /db/schema/tenant_schema.sql
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$db" -f "/db/seed/${db}.sql"
done

# The default 'postgres' database holds nothing for the app: readers can't connect to it either.
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -c "REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;"
