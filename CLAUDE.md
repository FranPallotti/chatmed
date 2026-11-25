# CLAUDE.md — ChatMed: session-scoped document + database agent

Read this file fully before every task. Then read `docs/architecture.md`, `docs/decisions.md`
and the milestone you were given in `docs/milestones.md`.

## What we are building

A proof of concept (one week) where an agent answers questions using two sources:

1. **Tenant relational database** (one per tenant). Holds business data and the *metadata*
   of the fields that each document type can contain. It never holds values taken from
   patient documents.
2. **PDF documents uploaded into a session** (medical, unstructured). Their content may be used
   only during the session and must be gone when the session ends.

Same PDFs + same question in two different sessions must give the same facts and values.

## HARD RULES (never break these; stop and ask if a task seems to require it)

1. **No document content outside process memory.** No writing document text, extracted values,
   prompts or answers to disk, a database, a cache, a queue, temp files or blob storage.
   - DuckDB is always `duckdb.connect(":memory:")`, one connection per session, closed on wipe,
     configured so it does not spill to disk (see `docs/architecture.md`).
   - No `tempfile`, no `open(..., "w")` with document data, no pickle of session objects.
2. **No document content in logs, traces, metrics or exceptions.** Log only metadata:
   session id, tenant id, document sha256, page counts, timings, token counts, statuses.
   Never log request bodies, prompts, model outputs, page text, quotes or field values.
   Exception messages must not embed document text.
3. **Reproducibility settings are mandatory on every model call:** `temperature=0`,
   a fixed `seed`, the pinned deployment from config, a versioned prompt (`PROMPT_VERSION`),
   strict structured outputs (`response_format` json_schema, `strict: True`).
   Process pages and documents in a stable order (page number; documents by sha256).
4. **Facts are decided by code, not by the model.** Model output is validated (types, enums)
   and grounded (the evidence quote must be found on the claimed page). Ungrounded values
   are `unverified`, never `ok`.
5. **Tenant isolation.** Every database access goes through a tool/function that injects the
   tenant from the session. The model never chooses the tenant. Database user is read-only.
6. **Stateless model calls.** Do not use Foundry Agent Service threads, Assistants API, or
   `store=True`. Conversation state lives in the session object in memory.
7. **No secrets in code.** Auth with `DefaultAzureCredential` (Entra ID). Config from env vars.
8. **No real patient data** in the repo, tests or fixtures. Synthetic documents only.

## Stack

- Python 3.12, `uv` for dependencies, FastAPI + Uvicorn (async), Pydantic v2.
- Azure OpenAI (deployed in a Microsoft Foundry project) via `openai.AsyncAzureOpenAI`.
- DuckDB (in-memory, per session). PyMuPDF for digital PDFs. Local OCR for scans.
  Azure Document Intelligence as an optional parser backend (bytes in, delete result right after).
- Tenant DB: PostgreSQL, **one database per tenant** (`tenant_acme`, `tenant_globex`), read-only role
  `chatmed_reader`, async `psycopg`. Local: `docker compose up -d`. Schema in `db/schema/`.
- Deploy: one Azure Container App, single replica. Telemetry: Application Insights via OpenTelemetry.

## Commands

```bash
uv sync                      # install
uv run pytest -q             # all tests
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run python scripts/check.py   # all of the above + forbidden-pattern guard. Must pass before "done".
uv run uvicorn chatmed.api:app --reload   # once M3+ exists
```

## Layout

```
src/chatmed/
  models.py         # shared dataclasses (ParsedDocument, ParsedPage, ...)
  config.py         # settings from env (pydantic-settings)
  parsing/          # DocumentParser interface + pymupdf / local_ocr / docintel / routing
  extraction.py     # metadata -> JSON schema -> LLM -> verified facts -> DuckDB  (seeded, see M2)
  session/          # SessionStore interface + in-memory implementation (+ encrypted later)
  tenant_db/        # read-only repository + parameterized tools
  agent/            # tool definitions, agent loop, answer composition with citations
  api.py            # FastAPI app: sessions, upload, ask, close
tests/
  contracts/        # tests every implementation of an interface must pass
  guardrails/       # no-persistence, no-content-in-logs, reproducibility
fixtures/           # synthetic PDFs, recorded model responses
db/                 # tenant DB schema + synthetic seeds (docker compose up -d)
docs/               # architecture, decisions, milestones, open questions
```

## Interfaces (keep them small; do not add methods without a reason in docs/decisions.md)

- `DocumentParser.parse(pdf_bytes) -> ParsedDocument` (async)
- `SessionStore`: `create`, `put_document`, `get_documents`, `facts_db`, `close` (idempotent wipe)
- Implementations are chosen by config (`PARSER`, `OCR_BACKEND`, `SESSION_STORE`), never per request.

## How to work

1. Work on **one milestone at a time**. Do not start the next one unless asked.
2. Before coding, write a short plan (files to touch, tests to add) and check it against the
   hard rules.
3. Tests first or together with code. Every acceptance criterion in the milestone needs a test.
4. Run `uv run python scripts/check.py`. Fix everything. Do not disable or skip tests or rules to pass.
5. Finish with a summary: what changed, how each acceptance criterion is met, any open question.
   If a decision is needed that is not in `docs/decisions.md`, stop and ask; do not invent it.
6. Keep code readable for a human reviewer: small functions, type hints, docstrings on public
   functions explaining *why*. The author must be able to explain every line in a code review.

## Definition of done (every task)

- Acceptance criteria met and tested.
- `uv run python scripts/check.py` passes.
- No hard rule broken; guardrail tests still pass.
- Docs updated if behavior or a decision changed.
