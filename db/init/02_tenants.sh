#!/bin/bash
# One database per tenant, each with the same schema and its own metadata + synthetic data.
set -euo pipefail
for tenant in acme globex; do
  db="tenant_${tenant}"
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE ${db};"
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${db}" -f /db/schema/tenant_schema.sql
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${db}" -f "/db/seed/${db}.sql"
done
