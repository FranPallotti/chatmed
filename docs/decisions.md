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
