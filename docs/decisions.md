# Decisions

Format: context → options → decision → why → consequences. Add new entries; don't rewrite old ones.

## D0. Interpretation of "no storage"
- **Decision:** strictest workable. Document content only in process memory. External processors
  limited to Azure services under the BAA (Azure OpenAI required; Document Intelligence optional).
- **Why:** safest default; relaxing later is a config change, tightening later would be a redesign.
- **Open:** legal confirmation (see `open-questions.md`).

## D1. PDF parsing
- Options: local library · Azure Document Intelligence · LLM reading page images.
- **Decision:** `DocumentParser` interface; local PyMuPDF for digital pages; scans go to a
  configurable OCR backend (local OCR default, Document Intelligence optional).
- **Why:** deterministic, no external retention by default, not blocked by pending legal answers
  or missing sample documents.
- **Consequence:** backend choice changes extracted text → `parser_id` is in the fingerprint and the
  backend is fixed per configuration.

## D2. How document content reaches the agent
- Options: whole document in context · in-memory vector index · schema-driven extraction.
- **Decision:** schema-driven extraction from tenant field metadata into session DuckDB;
  whole text (or in-memory BM25 for large docs) as fallback for free-text questions.
- **Why:** uses the metadata the tenant already has, turns unstructured data into verifiable
  facts, makes answers reproducible, no embeddings to store.

## D3. Tenant DB access
- **Decision:** parameterized tools for main paths; restricted read-only SQL for the rest
  (allow-list, enforced tenant filter, limits).
- **Why:** safety, testability, determinism; tenant isolation enforced in code.

## D4. Agent runtime
- **Decision:** code-first loop with stateless model calls (function calling).
- **Why:** Foundry Agent Service / Assistants store threads server-side, conflicting with D0.
  Foundry is still used for model deployments, evaluation and content safety.

## D5. Session state
- **Decision:** `SessionStore` interface; POC uses in-memory store, single replica, TTL + wipe.
- **Later:** encrypted blob store with crypto-shredding if scale/large files require it and legal allows.
- **Why:** simplest option that meets D0. Losing a session is cheap because results are reproducible.

## D6. Reproducibility
- **Decision:** canonical inputs (hash, stable ordering), pinned deployment, temperature 0 + seed,
  versioned prompt, strict structured outputs, code-side grounding and merge rules, facts from SQL,
  consistency test (N runs → identical facts). Optional HMAC-only drift log.
- **Why:** LLMs are not bit-for-bit deterministic even with a seed; determinism must come from
  the architecture and be measured.

## D7. Deployment
- **Decision:** one Azure Container App, Azure OpenAI in Foundry, PostgreSQL with synthetic tenants,
  Managed Identity, Key Vault, Application Insights with metadata only.
- **Why:** deployable within the week; no Kubernetes.

## D8. Tenant database layout
- Options: one database per tenant · one schema per tenant · shared tables with a tenant_id column.
- **Decision:** one database per tenant, same schema in each, selected by code from the session's
  tenant via `TENANT_DB_DSN_TEMPLATE`. Each tenant has its own read-only role
  (`chatmed_reader_{tenant}`) with CONNECT only on its own database; CONNECT is revoked from PUBLIC
  on every tenant database and on `postgres`.
- **Why:** matches the problem statement ("a database per tenant") and gives defense in depth:
  isolation is enforced by code (rule 5: the tenant comes from the session, never the model) **and**
  by Postgres (if code ever picked the wrong database, the connection is refused). Verified locally:
  `chatmed_reader_acme` gets "no CONNECT privilege" on `tenant_globex` and `postgres`.
- **Consequence:** one connection pool and one credential per tenant; schema changes must be applied
  to every tenant DB. In Azure, per-tenant roles map to per-tenant Entra identities (M5).
- **Local secrets:** passwords live in `db/.env` (gitignored), never in compose or SQL files.

## D9. Model and deployment
- Options: non-reasoning model (gpt-4.1-mini) · reasoning model (gpt-5.x-mini).
- **Decision:** gpt-4.1-mini, version 2025-04-14 (inference retirement 2027-04-14), deployment
  `gpt-4.1-mini-chatmed`, type GlobalStandard, 30K TPM, in Foundry resource `chatmed-ai-67800`
  (eastus2, project `chatmed`).
- **Why:** reasoning models do not accept `temperature`, which D6 relies on; gpt-4.1-mini supports
  temperature 0, seed and strict structured outputs, and is cheap and fast for extraction.
- **Limitation:** the subscription has 0 DataZoneStandard quota, so the POC uses GlobalStandard
  (requests may be processed in any Azure region). Acceptable for synthetic data only.
  Production with real patient data: DataZoneStandard or regional Standard deployment.
- **Consequence:** before 2027-04-14 the model must be replaced and re-validated with the
  reproducibility tests.
