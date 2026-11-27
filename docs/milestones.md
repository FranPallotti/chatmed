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
- AC2 `DocumentParser` protocol, `PdfiumParser`, `TesseractOcrParser`, `RoutingParser` (D10).
  Replace the `pymupdf` dependency with `pypdfium2` (+ the Tesseract binary, D11); config `PARSER=routing|pdfium`,
  new settings `OCR_DPI` (default 300), `OCR_LANG` (default `eng`), `TESSERACT_CMD` (optional path),
  all listed in `.env.example`.
- AC2b `parser_id` includes the pypdfium2 version, and for OCR pages the Tesseract version, DPI and
  language, so any change shows up in the reproducibility fingerprint.
- AC2c If the Tesseract binary is missing, scanned pages fail with a clear error at startup or on
  first use (never silently return empty text). Tests that need Tesseract are skipped with a reason
  when it isn't installed; everything else runs.
- AC3 Same bytes → identical `ParsedDocument` across runs (contract test for every parser).
- AC4 Pages ordered and 1-based; `sha256` computed on the original bytes; `parser_id` set.
- AC5 `DocIntelParser` implemented behind config, sends bytes directly, deletes the analyze
  result after reading it; unit-tested with a mocked client (no network in tests).

## M1b — Clinical domain fixtures (D12)
The tenant databases now hold synthetic CDISC-style ADSL/ADAE data and CIOMS I field metadata
(`db/seed/`). Fixtures must describe the same synthetic case so M2–M4 can join documents to rows.
- AC1 `scripts/make_fixtures.py` also generates synthetic CIOMS I-style forms (invented data, layout
  following the public CIOMS I sections) for acme subject `01001-004`:
  - `cioms_initial.pdf` (digital): country Argentina, age 47, sex F, reaction onset 2026-06-12,
    reaction "acute liver injury, ALT 8x ULN", hospitalisation checked (others unchecked),
    suspect drug "Zentrobal" 20 mg daily, oral, indication "rheumatoid arthritis", therapy start
    2026-03-10, dechallenge yes, rechallenge na, concomitant drugs "Metformin 850 mg" and
    "Atorvastatin 20 mg", manufacturer control no. "ACM-2026-0042", date received 2026-06-15,
    report type initial. The subject id appears as "Subject 01001-004".
  - `cioms_initial_scanned.pdf`: the same form as an image-only PDF (no text layer).
  - `cioms_followup.pdf`: two pages, report type followup; page 2 states a corrected onset date
    2026-06-11, so extraction must report a `conflict` for `reaction_onset_date`.
- AC2 `fixtures/README.md` lists every value above and points to `db/seed/tenant_acme.sql`;
  a test checks that the subject id, onset date and drug in the fixtures exist in the acme seed.
- AC3 Parser contract tests run on the CIOMS fixtures; OCR of the scanned form recovers the
  subject id, onset date and suspect drug (substring asserts; skipped if Tesseract is missing).
- AC4 `tests/guardrails/test_no_persistence.py` uses the CIOMS fixture (doc type `cioms`, markers:
  subject id, drug name, control number) instead of the lab report.
- AC5 Linux portability (the M5 Docker image is Linux). Found when running the suite on Linux
  with Tesseract 4.1.1:
  - tessdata discovery fails ("cannot find the tesseract language data directory"): honor
    `TESSDATA_PREFIX`, then ask the binary (`tesseract --list-langs` prints the path), then the
    standard Debian/Ubuntu paths; works with Tesseract 4 and 5.
  - `test_fixture_generation_is_reproducible_and_committed` compares PDF bytes, which differ across
    platforms for the rendered scanned fixture. Make the check platform-independent (e.g. compare the
    digital fixtures byte-for-byte and the scanned ones by extracted text / page image hash at fixed
    settings), and document why.

## M2 — Extraction (seed code provided in `src/chatmed/extraction.py`)
- AC1 Seed tests pass; code adapted to the package and config (deployment, seed, limits).
- AC2 Field metadata loaded from the tenant DB per document type (repository function + test
  with a test database or fake repository).
- AC3 Retries with exponential backoff + jitter on 429/5xx, per-call timeout; content never logged.
- AC4 Recorded-response tests: a fake client returns fixed JSON → facts and statuses as expected.
- AC5 One opt-in integration test (marked `azure`, skipped by default) against a real deployment.
- AC6 (D13) Fixtures mirror the real form: the subject id is printed with a prefix
  (`Subject ID 101-01001-004`); checkboxes are drawn as graphics with an X (never as text) in both
  the digital and scanned CIOMS; the narrative states "The patient was hospitalised on 2026-06-13"
  so `hospitalisation` has a text source while `dechallenge`, `rechallenge` and `report_type` do not.
  Update `fixtures/README.md` accordingly.
- AC7 (D13) Subject join in code: normalization + exact-or-suffix match against `adsl.subjid`;
  statuses `joined` / `unmatched` / `ambiguous`; tests for prefix, changed separators, no match,
  two matches.
- AC8 (D13) Checkbox fields: new fact status `undeterminable`; a checkbox field is `ok` only with a
  grounded explicit text statement, otherwise `undeterminable` (never `missing`, never guessed).
  Field metadata loading includes `checkbox`. Tests: hospitalisation `ok` from the narrative;
  dechallenge `undeterminable` on both CIOMS fixtures.
- AC9 (D13) Extraction prompt mentions column interleaving and OCR noise; prompt version bumped.

## M3 — Agent loop and API
Use case for the demo: a safety reviewer uploads a CIOMS report and asks questions that combine it
with the study data, e.g. "Does this CIOMS match what ADAE has for this subject?" (SAE
reconciliation), "What treatment arm is the subject in?", "Was the subject hospitalised?".
- AC1 Tools (tenant injected from the session, never a tool argument):
  - `query_document_facts`: read-only over the session DuckDB `facts` table.
  - `get_field_metadata`: the tenant's fields for a document type.
  - `get_subject(subjid)`: the `adsl` row; only callable with a subject from a `joined` fact (D13).
  - `list_adverse_events(subjid, serious_only?)`: `adae` rows for that subject.
  - `reconcile_case(doc_sha)`: deterministic code comparing CIOMS facts with the matching `adae`
    record (onset date vs `astdt`, seriousness vs `aeser`, action taken vs `aeacn`, outcome);
    returns per item `match` / `mismatch` / `not_comparable` (e.g. `undeterminable` checkbox).
- AC2 Agent loop: function calling, max iterations, per-call timeout, stateless calls,
  temperature 0 + seed + pinned deployment, retries as in M2. Tool errors go back to the model as
  short codes, never with document text.
- AC3 Answers are built from tool results: the response carries a structured `facts_used` list
  (fact/row ids, values, citations: doc sha prefix + page + quote, or table + key) plus the text.
  The model never states a value that isn't in `facts_used`; a code check rejects answers whose
  numbers/dates don't appear in `facts_used` and retries once, else returns a safe fallback.
- AC4 Honesty statuses reach the user: `undeterminable`, `conflict`, `unverified`, `missing`, and
  join statuses `unmatched` / `ambiguous` / `not_attempted` are stated plainly, never papered over.
  If the subject isn't `joined`, no study data about any subject is returned.
- AC5 Fallback for questions not covered by fields: relevant page text (whole text for small
  docs; in-memory BM25 over pages for large ones, ties broken by page number), quotes still grounded.
- AC6 FastAPI endpoints from `docs/architecture.md` (create session, upload, ask, close) with a
  minimal in-memory session holder (M4 hardens it); upload size/page limits; request-body logging off.
- AC7 Every log path goes through the metadata-only filter: uvicorn's own loggers (run with
  `log_config=None` or route `uvicorn.error`/`uvicorn.access` to our handler), and uncaught
  exceptions (`sys.excepthook`, FastAPI exception handlers). A test raises an exception whose
  message contains document text and proves it doesn't reach any log output.
- AC8 Tests with a fake model client: scripted tool-call sequences for the three demo questions on
  the synthetic CIOMS produce the expected `facts_used`; reconciliation reports the expected
  match/mismatch items (the follow-up fixture's onset `conflict` → `not_comparable`).
- AC9 Opt-in Azure test (synthetic data only): the three demo questions, 3 runs each → identical
  `facts_used` across runs (answer wording may differ; the facts may not).

## M4 — Session lifecycle and guardrails on
M3 already has a minimal `InMemorySessionStore` (create / get / get_documents / close). M4 makes the
session lifecycle trustworthy and turns on the guardrails that prove the "no storage" rule end to end.
- AC1 Lifecycle: a background TTL sweeper (asyncio task started/stopped with the app) closes idle
  sessions; `close()` is idempotent, closes the session's DuckDB connection and drops every
  reference to documents, facts and chat history. After close, any call with that session id gets
  404 `session_closed` (never another session's data). A test uses `weakref` to prove the parsed
  documents and the DuckDB connection object are released after close.
- AC2 Concurrency: closing (by DELETE or TTL) while an upload or ask is in flight ends that request
  cleanly with `session_closed`, without writing anything after the wipe. A per-session lock or
  generation counter makes this deterministic; tests cover both orders.
- AC3 Capacity: `MAX_SESSIONS` (operational setting) caps concurrent sessions; above it
  `POST /sessions` returns 503 `capacity`. Combined with the existing per-session limits this bounds
  memory. Document the worst-case estimate in `architecture.md`.
- AC4 DuckDB stays in memory: spilling disabled and `memory_limit` set per connection; a query that
  would exceed it fails with `session_memory_limit` instead of writing to disk. Test proves no file
  is created (including DuckDB's default temp directory).
- AC5 Guardrails on: add `chatmed.pipeline.run_full_session(...)` (create → upload → ask → close,
  fake model from recorded synthetic outputs) and enable `tests/guardrails/test_no_persistence.py`
  with the CIOMS fixture: no files written in the working dir or the system temp dir; no document
  text, quote or value in any log output.
- AC6 Cross-session reproducibility, the challenge requirement verbatim: two *separate* sessions
  (create → upload the same CIOMS → ask the same question → close, then again) give identical
  `facts_used` and the same answer fingerprint; verified outcome in both. Fake model: N=5 pairs.
  Opt-in Azure test: 3 pairs on the synthetic CIOMS.
- AC7 Lifecycle in the logs: session created / closed (reason: `client` | `ttl` | `shutdown`) /
  rejected (capacity) as metadata events, so a reviewer can see from the logs that every session
  was wiped. On app shutdown every open session is closed.

## M5 — Deployment and demo
Split in two so a working demo exists even if the cloud deploy hits problems: **M5a** runs the full
stack locally in containers; **M5b** deploys it to Azure.

### M5a — Container, UI, telemetry (local)
- AC1 Memory sizing (closes open question 8). Target container: 2 vCPU / 4 GiB (the Azure Container
  Apps limit in a Consumption-only environment). Add `OCR_MAX_CONCURRENCY` (operational, default 2):
  a semaphore around Tesseract; extra scans queue. Set `MAX_SESSIONS=8`. Recompute the worst case
  in `architecture.md`; it must fit 4 GiB with ≈1 GiB headroom, otherwise lower `MAX_SESSIONS`.
  Test: with concurrency 2, a third concurrent scan waits instead of starting a third process.
- AC2 Dockerfile: Python 3.12 slim, `uv sync --frozen --no-dev`, `tesseract-ocr` + `tesseract-ocr-eng`
  from the distro, non-root user, Uvicorn with our log config (`log_config=None`), a `/healthz` route
  with no tenant data. The OCR tests pass inside the image (Linux tessdata discovery from M1b).
- AC3 `docker compose up` runs app + Postgres together; `.env`/`db/.env` passed as env, never baked
  into the image. A smoke script runs the demo flow against the running container.
- AC4 Minimal UI: one static HTML/JS page served by FastAPI (no new dependency, no Streamlit, which
  would add server state and upload handling outside our control): pick tenant, upload PDF, ask,
  see progress events, the verified answer with `facts_used` and citations, statuses
  (`undeterminable`, `conflict`, `not_comparable`…) visibly marked, and a Close session button.
- AC5 Telemetry: Application Insights via `azure-monitor-opentelemetry-exporter` and
  `opentelemetry-instrumentation-fastapi` only (not the `azure-monitor-opentelemetry` distro,
  D16), enabled only when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set. The metadata-only filter
  applies to the exporter too (test: a fake exporter receives only whitelisted fields); no
  request/response body capture; no prompt/completion capture by any instrumentation; an
  exception containing document text never reaches the span exporter (events, status
  description, URL).

### M5b — Azure deployment
Same image as M5a, in `rg-chatmed-dev` (eastus2, next to the Foundry resource), built from Bicep in
`infra/`. Order: base resources → seed the database → build the image → create the app → verify.
The agent writes templates, scripts and docs; the developer runs the `az` commands (their login).
Record the decisions below as D17.
- AC6 Infra as code (Bicep, parameters for every name, nothing secret in templates or params files):
  Container Registry (Basic, admin user disabled); Log Analytics + workspace-based App Insights;
  Key Vault (RBAC mode); a **user-assigned** managed identity created before the app; Container
  Apps environment (Consumption-only) and the app with **min = max = 1 replica** (in-memory sessions,
  D5; always on so the demo never waits for a cold start), 2 vCPU / 4 GiB; Azure Database for
  PostgreSQL Flexible Server, smallest Burstable tier, version 17.
- AC7 Least privilege: the identity gets exactly `AcrPull` (registry), `Key Vault Secrets User`
  (vault) and `Cognitive Services OpenAI User` (Foundry resource). The developer gets
  `Key Vault Secrets Officer` on the vault, only to write the generated passwords. No registry
  admin, no API keys, no service principal secret in Azure.
- AC8 Database networking (POC): public endpoint, TLS required, firewall = Azure services + the
  developer's IP (parameter; needed for seeding). Per-tenant read-only roles as locally. D17 states
  the weak point (any Azure resource can attempt a connection; stopped by per-tenant roles and
  random passwords) and the production path (VNet-integrated environment + private access).
- AC9 Per-tenant passwords: the seed script generates one random password per tenant reader, writes
  it straight to Key Vault (`pg-reader-<tenant>`) and uses it, without printing it. The app picks the
  password of the session's tenant (`PGPASSWORD_<TENANT>` env vars, each a Container Apps secret
  referencing Key Vault through the identity), consistent with how it picks the role and database.
  Locally the shared `PGPASSWORD` keeps working. Tests: right password per tenant; a missing
  tenant password fails clearly at startup.
- AC10 Seeding from the developer's machine: the same `db/schema` and `db/seed` files, applied with
  `psql` from the `postgres:17` Docker image (nothing new to install on Windows) over TLS by a
  script (PowerShell) that reads the admin password from Key Vault and never
  echoes secrets. Idempotent or with a clear "already seeded" stop.
- AC11 Image: built in the cloud with `az acr build`, tagged with the git commit; the app references
  the image **digest**, so the deployed image is exactly the one that was tested. Record the digest
  in the deploy output.
- AC12 Access: ingress external, HTTPS only, IP allow-list with the developer's IP (parameter);
  everything else rejected before the app. `deploy.md` has the one command to update the IP. The
  tenant still comes from the request (D14); Entra login + tenant claim is the production step.
- AC13 Verification in Azure: `smoke_demo.py --base-url <app URL> --tenant acme --require-verified`
  passes; a KQL query in `deploy.md` shows the app's logs contain only metadata events; the
  file check (`find / -xdev -newer /app -type f`) via `az containerapp exec` shows no app writes.
- AC14 `docs/deploy.md`: prerequisites, deploy, update (new image), update IP, verify, and
  **delete** (`az group delete -n rg-chatmed-dev`, plus deleting the local service principal), the
  budget alert, and expected monthly cost of the chosen tiers (from the Azure pricing pages, dated).

### M5c — Demo and wrap-up
- AC10 `docs/demo.md`, a 10-minute script on synthetic data: create an acme session → upload the
  scanned CIOMS → ask the three demo questions (arm, hospitalised, reconciliation) → show
  `undeterminable` checkboxes and citations → close → second session, same questions, same
  `facts_used` and fingerprint → show the logs (lifecycle events, no content) → globex session to
  show different field metadata and tenant isolation.
- AC11 README rewrite for a reviewer: what it is, the three constraints and how each is met, run
  locally in five commands, architecture diagram, decision index, known limitations and what
  changes for production (Entra auth per tenant, DataZone deployment, crypto-shredding store,
  checkbox reading, real-sample clearance).

## Stretch (only if time)
- Restricted SQL tool (was M4 AC6): SELECT-only, allow-list, tenant filter, limit, timeout,
  validated with `sqlglot`; negative tests for injection and cross-tenant access.
- Read checkbox states (D13): Document Intelligence selection marks, or a local detector that finds
  the boxes on the rendered page and measures their fill.
- Encrypted blob session store with crypto-shredding (must pass the session store contract tests).
- Self-consistency extraction (k runs, per-field majority).
- Automatic document-type classification limited to the tenant's types.
