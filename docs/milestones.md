# Milestones

Give the agent one milestone at a time: "Implement M<n> from docs/milestones.md".
Each acceptance criterion (AC) needs at least one test.

## M0 — Project skeleton and guardrails
- AC1 `uv sync`, `uv run pytest`, ruff, mypy and `uv run python scripts/check.py` run green.
- AC2 `config.py` loads settings from env (pydantic-settings); `.env.example` lists all of them.
- AC3 Logging configured centrally with a filter/formatter that only emits whitelisted metadata
  fields; a test proves arbitrary `extra` payloads (e.g. `text=...`) are dropped.
- AC4 `tests/guardrails/` exists with the no-persistence and no-content-in-logs tests
  (skipped until the pipeline exists, then enabled in M4).

## M1 — Parsing + synthetic fixtures
- AC1 `scripts/make_fixtures.py` generates synthetic PDFs (fake patients): a digital lab report,
  a multi-page report with a repeated value (for conflict tests), and an image-only "scanned" PDF.
- AC2 `DocumentParser` protocol, `PyMuPdfParser`, `LocalOcrParser`, `RoutingParser`.
- AC3 Same bytes → identical `ParsedDocument` across runs (contract test for every parser).
- AC4 Pages ordered and 1-based; `sha256` computed on the original bytes; `parser_id` set.
- AC5 `DocIntelParser` implemented behind config, sends bytes directly, deletes the analyze
  result after reading it; unit-tested with a mocked client (no network in tests).

## M2 — Extraction (seed code provided in `src/chatmed/extraction.py`)
- AC1 Seed tests pass; code adapted to the package and config (deployment, seed, limits).
- AC2 Field metadata loaded from the tenant DB per document type (repository function + test
  with a test database or fake repository).
- AC3 Retries with exponential backoff + jitter on 429/5xx, per-call timeout; content never logged.
- AC4 Recorded-response tests: a fake client returns fixed JSON → facts and statuses as expected.
- AC5 One opt-in integration test (marked `azure`, skipped by default) against a real deployment.

## M3 — Agent loop and API
- AC1 Tools: `query_document_facts` (read-only over session DuckDB), `get_field_metadata`,
  2–3 parameterized tenant DB tools; tenant injected from the session.
- AC2 Agent loop: function calling, max iterations, timeouts, stateless calls, temperature 0 + seed.
- AC3 Answers include citations (doc sha prefix + page + quote, or table name).
- AC4 Fallback for questions not covered by fields: page text (BM25 for large docs).
- AC5 FastAPI endpoints from `docs/architecture.md`, upload limits enforced, request-body logging off.
- AC6 Tests with a fake model client: a scripted tool-call sequence produces the expected answer.
- AC7 Every log path goes through the metadata-only filter: uvicorn's own loggers (run with
  `log_config=None` or route `uvicorn.error`/`uvicorn.access` to our handler), and uncaught
  exceptions (`sys.excepthook`, FastAPI exception handlers). A test raises an exception whose
  message contains document text and proves it doesn't reach any log output.

## M4 — Session lifecycle and guardrails on
- AC1 `InMemorySessionStore`: TTL sweeper, idempotent `close()` releasing DuckDB and documents.
- AC2 DuckDB per session is `:memory:` with disk spilling disabled; test proves no file is created.
- AC3 Guardrail: full pipeline run (fake model) in an empty working dir → no files written
  anywhere in that dir or the system temp dir.
- AC4 Guardrail: capture all logs during a full run → no page text, quote or value appears.
- AC5 Reproducibility test: same PDFs + question, N=5 runs (fake model with recorded outputs and,
  opt-in, real model) → identical facts and identical cited values.
- AC6 Restricted SQL tool (optional): SELECT-only, allow-list, tenant filter, limit, timeout,
  validated with `sqlglot`; negative tests for injection and cross-tenant access.

## M5 — Deployment and demo
- AC1 Dockerfile (non-root, slim), runs Uvicorn; image builds locally.
- AC2 Infra as code (Bicep or Terraform) for Container App, ACR, Key Vault, App Insights,
  PostgreSQL; Managed Identity with least-privilege roles.
- AC3 Seed script for two synthetic tenants with different field metadata.
- AC4 Minimal UI (Streamlit or a static page) for upload / ask / close.
- AC5 `docs/demo.md`: demo script, including showing that a second session gives the same answer.

## Stretch (only if time)
- Encrypted blob session store with crypto-shredding (must pass the session store contract tests).
- Self-consistency extraction (k runs, per-field majority).
- Automatic document-type classification limited to the tenant's types.
