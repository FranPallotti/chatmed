# Architecture

## Flow

```
User/UI ──upload PDF──► Parser (local / OCR / Doc Intelligence)
                            │ ParsedDocument (in memory)
                            ▼
                     Field extractor ── reads field metadata ──► Tenant DB (read-only)
                     (LLM, strict schema, temp 0, seed)
                            │ verified Facts
                            ▼
                     Session DuckDB (:memory:)
                            ▲
User/UI ──question──► Agent loop ──tools──► facts query / tenant DB tools / restricted SQL
                            │
                            └── stateless calls ──► Azure OpenAI (Foundry deployment)

Everything inside the session lives in process memory and is wiped on close/TTL.
```

## Components

### Parsing (`src/chatmed/parsing/`)
- `DocumentParser` interface → `ParsedDocument(sha256, pages, parser_id)`.
- `PdfiumParser`: digital PDFs via `pypdfium2`, local, deterministic.
- `TesseractOcrParser`: pages without a text layer: render with pypdfium2 at a fixed DPI, OCR with
  Tesseract (fixed language and config), local, deterministic.
- `DocIntelParser` (optional): sends bytes directly (no blob upload), fetches the result,
  calls delete-analyze-result immediately.
- `RoutingParser`: per page, text layer → pdfium, otherwise → configured OCR backend.
  A page has a text layer if pdfium returns any non-whitespace text. Only image-only pages go to
  OCR, copied into an in-memory sub-PDF; `parser_id` names the OCR backend only when it was used.
- Tesseract runs over stdin/stdout (D11); pdfium calls hold a process-wide lock (not thread-safe).
  At most `OCR_MAX_CONCURRENCY` pages (default 2) are rendered and OCRed at once across all
  sessions (a semaphore in the single parser instance, D16); pages of one document stay in
  page order.
- `build_parser(settings)` picks the implementation at startup and checks Tesseract there.
- `parser_id` records backend + version; it is part of the reproducibility fingerprint.
  For Tesseract it also records the Leptonica version and a hash of the `.traineddata` files used
  (same Tesseract version can ship different models), e.g.
  `tesseract-5.4.0-lept1.84.1-dpi300-eng-psm3-data<sha12>`.
  The `.traineddata` directory is the first one holding every configured language among:
  `TESSDATA_PREFIX` (or its `tessdata/`), the path printed by `tesseract --list-langs`
  (Tesseract 5 only), then the package directory of the same major version
  (`/usr/share/tesseract-ocr/5/tessdata` or `.../4.00/tessdata`) and `/usr/share/tessdata`.
  Works with Tesseract 4 and 5. The M5 image should set `TESSDATA_PREFIX` explicitly.
- Document Intelligence: if deleting the analyze result fails, `docintel_delete_failed` is logged
  (doc sha256 only), the original error is kept, and a successful parse is turned into an error.
  The backend is fixed per deployment/tenant config, never per request.

### Extraction (`src/chatmed/extraction.py`, seeded)
- Tenant `field_definitions` → strict JSON schema (sorted keys, schema hash).
- Each field returns `{value, evidence{page, quote}}` or nulls. Multiple fields return a list;
  single fields also return `other_statements` (other places stating a *different* value, e.g.
  a correction on a later page), so a conflict inside one window is not lost.
- Code validates types (Decimal/ISO date parsed in code) and grounds quotes against page text.
  A grounded quote is stored as the full page line(s) it sits on, so the evidence does not
  depend on how much of a line the model chose to quote (D13c).
- Statuses: `ok`, `missing`, `unverified`, `invalid`, `conflict`, `undeterminable` (checkbox
  fields, D13/D13a/D13b: `ok` only from a grounded quote with words beyond the field's own
  label/description/options that does not contain all words of one of its printed
  `box_labels`; anything else is `undeterminable`, never `missing`).
- Subject join (code, D13/D13a), only for `join_target = adsl.subjid`: normalized exact or
  suffix match → `joined` / `unmatched` / `ambiguous`; only an `ok` subject id is joined
  (`conflict` → `ambiguous`, otherwise `not_attempted`). Stored as `join_status`/`join_value`.
- Large documents: deterministic page windows; merge rules: candidates in a total order (page,
  value, quote), one fact per distinct value (the first in that order); single-value → that
  value, or each distinct value as `conflict`; multiple → all distinct values.
- Model calls: our own retries (SDK `max_retries=0`) on 429/5xx/timeouts/connection errors,
  exponential backoff with full jitter, per-call timeout (`LLM_*` settings). Unusable output
  (truncated, refused, not JSON) fails with `ExtractionError`, which never carries content.
  Logged per call: `llm_call` with attempt, http status, duration and token counts only.
- `extract_for_tenant(client, repo, doc, doc_type, settings)` loads the tenant's metadata and
  subject ids through the repository, then extracts.
- Facts go into the session DuckDB table `facts` (long format; joins to DB via `join_target`).

### Session (`src/chatmed/session/`)
- `SessionStore` interface (`create`, `get`, `put_document`, `get_documents`, `facts_db`,
  `close(id, reason)`, `expire_idle`, `close_all`; `get` added in M3 (D14), the last two and the
  close reason in M4 (D15)). POC: `InMemorySessionStore` (dict).
- Each session owns: tenant id (fixed at creation, D14), documents (ordered by sha256), a DuckDB
  `:memory:` connection, chat history (questions and verified answer texts), a lock that
  serializes uploads and questions within the session, and its in-flight tasks.
- Lifecycle (D15): idle TTL (`SESSION_TTL_SECONDS`; the start and end of a request count as
  activity, not the time in between). Once the TTL has passed, every access refuses the session
  (`session_closed`) and wipes it; untouched, it is wiped by an asyncio sweeper at most
  TTL + `SESSION_SWEEP_INTERVAL_SECONDS` after its last activity. `close` (client `DELETE`, `ttl`, `shutdown`) is idempotent
  and immediate: it cancels in-flight uploads/questions (they end with `session_closed`, nothing
  is written after the wipe), closes DuckDB and drops documents, facts and history. Closed ids
  (ids only, the latest 10 000) answer `session_closed`; unknown ids `session_not_found`.
  `MAX_SESSIONS` caps open sessions (503 `capacity`). Events: `session.created`,
  `session.closed` (with `reason`), `session.rejected`, `session.swept`, `session.shutdown`.
- DuckDB is opened with `temp_directory=''` (no spilling to disk: over `memory_limit` a query
  fails, `session_memory_limit`) and `memory_limit`, then `SET enable_external_access=false`
  (no `COPY TO`, `ATTACH` or file reads) and last `SET lock_configuration=true`, because
  `enable_external_access` alone does not stop `SET memory_limit` (D15). Proven by
  `tests/guardrails/test_duckdb_no_disk.py`, including DuckDB's default `.tmp` spill directory
  (a control test shows a plain connection does spill there).
- **Worst-case memory (M5a AC1, D16).** Container: 2 vCPU / 4 GiB (4096 MB, the Container
  Apps Consumption limit). Defaults: `MAX_SESSIONS=8`, `MAX_DOCS_PER_SESSION=5`,
  `MAX_UPLOAD_MB=20`, `DUCKDB_MEMORY_LIMIT=128MB`, `OCR_MAX_CONCURRENCY=2`, `OCR_DPI=300`.

  | Item | Bound | × | MB |
  |------|-------|---|----|
  | DuckDB per session (no spill, D15) | 128 MB | 8 | 1024 |
  | Parsed text per session (≤ 5 docs, text ≈ PDF size at most; typically < 1 MB) | 100 MB | 8 | 800 |
  | In-flight upload per session (one at a time, session lock): body + decoded file part + OCR sub-PDF | 3 × 20 MB | 8 | 480 |
  | OCR slots: Tesseract process (≈ 290 MB peak, D10) + one 300 DPI A4 grayscale page (≈ 9 MB) | 299 MB | 2 | 598 |
  | Python process (measured in the image: 92–102 MB after imports and startup; budgeted at 2×) | 200 MB | 1 | 200 |
  | **Total** | | | **3102** |

  Headroom: 4096 − 3102 ≈ 994 MB (≈ 0.97 GiB). The page images and Tesseract processes are
  bounded by `OCR_MAX_CONCURRENCY` across all sessions (a semaphore around render + OCR of
  each page; extra pages queue), not by `MAX_SESSIONS`; before M5a the same worst case was
  20 sessions each running OCR, ≈ 11 GB. Measured in the image (2 vCPU, 4 GiB limit): three
  concurrent scans of the CIOMS fixture, largest Tesseract child 219 MB, Python peak 219 MB,
  6.5 s. With typical documents (text and facts < 1 MB) the total is ≈ 1.2 GB.
  The sizing holds while the defaults hold: raising `MAX_SESSIONS`, `MAX_UPLOAD_MB`,
  `DUCKDB_MEMORY_LIMIT`, `OCR_MAX_CONCURRENCY` or `OCR_DPI` needs this table redone.

  **Measured under load (2026-11-27, `scripts/measure_memory.py`).** 8 sessions created and
  uploading `cioms_initial_scanned.pdf` at once over HTTP, in the test image with
  `--memory 4g --cpus 2`: real app, multipart parsing, routing parser, Tesseract 5.5 with
  `OCR_MAX_CONCURRENCY=2`, in-memory sessions and DuckDB; fake model replaying the recorded
  extraction, fake tenant repository. Two runs: `docker stats` peak **401 MiB / 422 MiB**
  (same as the cgroup's `max_usage_in_bytes`, which includes page cache); idle 121 MiB, after
  the uploads (8 sessions holding their facts) 244–268 MiB; all 8 uploads done in 17–18 s.
  That is far below the ~3 GiB threshold, so `MAX_SESSIONS` stays 8. The fixture is a
  1-page scan with little text, so this measures the OCR cap and the per-session overhead;
  the table above is still the bound for 20 MB uploads and full DuckDB limits.
- Later (only if time / needed): `EncryptedBlobSessionStore` with **crypto-shredding**:
  per-session AES-256 key held only in memory; dropping the key on close makes any
  remaining copy unreadable.

### Tenant DB (`src/chatmed/tenant_db/`)
- One read-only role per tenant (`chatmed_reader_{tenant}`), which can only connect to its own
  database (D8). Repository functions take `tenant_id` from the session, never from the model.
- `PostgresTenantRepository(settings, tenant_id)` is bound to one tenant at construction; the
  tenant must match `^[a-z0-9_]+$` and be in `TENANTS` before it fills the DSN template (D13a).
  Fixed parameterized queries, connections set read-only. Methods: `field_schema(doc_type)`,
  `subject_ids()`, `subject(subjid)`, `adverse_events(subjid, serious_only)`.
  psycopg's async driver needs a selector event loop on Windows.
- Agent tools are parameterized queries for main paths.
- Restricted SQL tool (optional): SELECT only, allow-listed tables/views, tenant filter enforced,
  row limit, timeout. Parsed/validated before execution (e.g. `sqlglot`).

### Agent (`src/chatmed/agent/`)
- `prompt.py`: everything the model is shown (system prompt, session context, retry note,
  tool and answer schemas), versioned by `AGENT_PROMPT_VERSION` and hashed as one module. All
  other agent modules are under `RULES_VERSION` (D6).
- `loop.py`: code-first loop with function calling over stateless chat completions (no Agent
  Service threads, no `store=True`). Every call: full message list, temperature 0, seed, pinned
  deployment, `AGENT_PROMPT_VERSION`, strict tools, strict answer schema,
  `parallel_tool_calls=False`. A second system message lists the session's documents (sha
  prefix, type, page count) and the tenant's field keys per document type: metadata only.
  Without it the real model guessed ids and keys ("hospitalization") and got empty results.
  `AGENT_MAX_ITERATIONS` model calls, then the fallback. Per-call
  timeout and the M2 retry policy (429/5xx/timeouts). Logs `agent.llm_call`, `agent.tool_call`,
  `agent.answer` with metadata only.
- `tools.py`: `query_document_facts`, `get_field_metadata`, `get_subject`,
  `list_adverse_events`, `reconcile_case`, `search_document_text`. No tool has a tenant argument;
  the repository is bound to the session's tenant. `get_subject`/`list_adverse_events` accept
  the `join_value` of a `joined` fact in the session, or that fact's subject id exactly as
  printed (mapped to its `join_value`; the real model passed the printed id). Anything else is
  refused (`subject_not_joined`) before touching the DB. Every returned row has a stable id (`fact:<sha12>:<field>:<n>`,
  `adsl:<subjid>`, `adae:<usubjid>:<aeseq>`, `recon:<sha12>:<item>`, `page:<sha12>:<n>`) and is
  kept in an in-memory evidence registry for the question. Tool errors reach the model as short
  codes (`unknown_document`, `subject_not_joined`, `tool_failed`, ...).
- `reconcile.py` (rule module, D14): tiered `adae` record selection and per-item comparison
  (onset, seriousness, action taken, outcome) → `match` / `mismatch` / `not_comparable`.
- `retrieval.py`: page text for questions no field covers: whole text up to
  `AGENT_FULLTEXT_MAX_CHARS`, else in-memory BM25 over pages (top `AGENT_BM25_TOP_K`, ties broken
  by sha256 then page).
- `answer.py`: the model ends with `{text, cited_ids, quotes}`. Code builds `facts_used` from the
  registry (values never come from the model), grounds quotes against the page text
  (`ground_quote`, stored as full lines, D13c), rejects text with numbers/dates not in
  `facts_used` (dates compared as dates in any written form; an ambiguous source date such as
  12/06/2026 supports no resolved reading; sha prefixes and row keys are not number sources),
  and rejects text that cites a row with an honesty status (`undeterminable`,
  `conflict`, `unverified`, `missing`, `invalid`, `unmatched`, `ambiguous`, `not_attempted`,
  `not_comparable`) without naming it (whole words, "conflict-free" is not one; a mention
  with a negation earlier in its clause, such as "no evidence of any conflict", does not
  count). Output that is not exactly one JSON object (the real
  model once sent two) is also a rejection. One retry, then a deterministic fallback listing
  the retrieved rows with their statuses. A cited `recon:` row brings in the whole
  reconciliation of its document, the compared `adae` record and the document's joined
  `subject_id` fact, which `reconcile_case` registers itself (D14).
- **Reconciliation answers are rendered by code** (D14, agent-v7, rules-11). The model
  only routes the question: as soon as a `reconcile_case` call succeeds, the loop stops
  calling the model and `answer.render_reconciliation` writes the answer: a header, then
  one line per item in fixed order with its status name and reason code ("Action taken:
  not comparable (no document field)."), no values. It cites every item; `facts_used`
  (the whole unit, with the `adae` record and joined subject) carries the citations. The
  text still goes through `verify`, so a unit row with an unstated status gives the
  fallback. The last line always says the answer covers only the comparison with the
  study database, since the rest of a combined question is not answered (rules-12). A
  failed `reconcile_case` (error code) goes back to the model as usual.
- A verified answer carries
  a fingerprint (agent prompt version, rules version, `FINGERPRINT_SETTINGS` values, tenant,
  document fingerprints, and digests of the session history and of each tenant DB read); a fallback carries outcome `fallback` and a reason code instead.

### Pipeline (`src/chatmed/pipeline.py`) and API (`src/chatmed/api.py`)
- `chatmed.pipeline` does the work without HTTP (D15): `create_session` (tenant allow-list, then
  capacity), `ingest_document`, `answer_question` (each runs as a task the session's close can
  cancel, `run_in_session`), `run_sweeper`, `close_all_sessions`, and `run_full_session`
  (create → upload → ask → close) used by the guardrail tests. Errors are `PipelineError` with a
  fixed code and HTTP status. Facts go into DuckDB in one transaction together with the
  document, or not at all.
- `chatmed.api` is the HTTP transport over it; the app's lifespan starts the TTL sweeper and,
  at shutdown, stops it and closes every session (reason `shutdown`).
- `POST /sessions {"tenant_id"}` → session id (tenant checked against `TENANTS`, D14);
  503 `capacity` over `MAX_SESSIONS`
- `POST /sessions/{id}/documents` (multipart `file` + `doc_type`) → sha256, page count,
  parser id, fingerprint, counts per status, join status (no values)
- `POST /sessions/{id}/ask {"question"}` → NDJSON: `started`, metadata-only `tool` events,
  then one `answer` event (`outcome` verified|fallback, `reason`, `text`, `facts_used`,
  `fingerprint` for verified answers) or an `error` code (D14; `session_closed`,
  `session_memory_limit`, ...)
- `DELETE /sessions/{id}` → idempotent wipe (204)
- After close every call with the id is 404 `session_closed`; an unknown id is 404
  `session_not_found`.
- The multipart body is streamed into memory with a byte counter (limit `MAX_UPLOAD_MB` +
  64 KiB): an oversized Content-Length is refused before reading, and the first chunk past the
  limit ends the read. It is parsed with the stdlib `email` parser: Starlette's `UploadFile`
  spools files above 1 MB to disk.
- Limits: 413 `upload_too_large`, 422 `too_many_pages`, 409 `too_many_documents` /
  `duplicate_document`, 415 `pdf_required`, 503 `session_memory_limit`, 503
  `model_auth_failed` (no Entra token for the model; on `ask` the same code as an NDJSON
  `error` event, D16). Errors are fixed codes. No body is ever logged.
- `GET /healthz` → `{"status": "ok"}`: liveness only, no tenant data, no DB or model call.
- `GET /` (+ `/app.js`, `/app.css`): the demo UI (M5a AC4), static files from
  `chatmed/static`, no dependency. Strict CSP (`default-src 'none'`, scripts and styles from
  this origin only, no inline code); every server value is rendered with `textContent`; the
  page keeps content only in memory (no browser storage, no cookies). The tenant is a text
  field validated by the server (no tenant list endpoint, D16).
- Every response carries `Cache-Control: no-store` (pure ASGI middleware), so answers and
  facts never land in a browser or proxy disk cache (rule 1). OpenAPI docs pages are off.
- Run with `uv run uvicorn chatmed.api:app` or `uv run python -m chatmed.api`; in the
  container `python -m chatmed.api` (Dockerfile, `docker compose up -d --build`).

### Logging (`src/chatmed/logconfig.py`)
- `configure_logging()` installs one JSON handler on the root logger with `MetadataOnlyFilter`.
- Log calls use a fixed event name as the message (`log.info("doc_parsed", extra={...})`).
  Free-text messages (f-strings, third-party prose) are replaced by `unstructured_message`;
  `%s` args are never rendered; `extra` keys outside `ALLOWED_FIELDS` and non-scalar or long
  values are dropped; exceptions keep only their type and code locations.
- The filter only sees records that reach its handler. `configure_logging()` strips the
  handlers uvicorn installs on `uvicorn`, `uvicorn.error`, `uvicorn.access`, `uvicorn.asgi` and
  `fastapi` and lets them propagate to the root handler (M3). It also installs `sys.excepthook`
  and `threading.excepthook`; the API sets the asyncio loop exception handler at startup and
  logs unhandled route errors as `api.unhandled_error`. OpenTelemetry handlers must get
  their own filter.
- Access log (M5a, D16): uvicorn's access log is off (`access_log=False` in
  `chatmed.api.main`, and `uvicorn.access` held at WARNING for `uvicorn chatmed.api:app`)
  because it prints the raw path (session ids) and the client address. `RequestLogMiddleware`
  logs one `http.request` per request instead: `method` (GET, POST, DELETE or OTHER;
  client input, so never logged as sent), `route` (the matched route
  *template*, e.g. `/sessions/{session_id}/ask`, or `unmatched`), `http_status`, `duration_ms`
  (for a streamed answer, the whole stream).
- `QUIET_LOGGERS` (`azure.core.pipeline.policies.http_logging_policy`, `azure.identity`,
  `httpx2`, `uvicorn.access`) are held at WARNING whatever `LOG_LEVEL` is: their INFO records
  are per-call prose that only showed up as `unstructured_message` noise.

### Telemetry (`src/chatmed/telemetry.py`, M5a AC5, D16)
- Off unless `APPLICATIONINSIGHTS_CONNECTION_STRING` is set. Built from
  `azure-monitor-opentelemetry-exporter` and `opentelemetry-instrumentation-fastapi` only;
  the `azure-monitor-opentelemetry` distro is not used (it instruments every library it
  finds and installs global providers).
- Logs: `MetadataOnlyOtelHandler` on the root logger, with its own `MetadataOnlyFilter`,
  sends the event name, level and `ALLOWED_FIELDS` values only (no code paths, no exception
  attributes, unlike OpenTelemetry's stock handler). Records of the Azure exporter's own
  loggers (`CONSOLE_ONLY_LOGGERS`) stay on the console, so a failing export never produces
  a new export.
- Request spans: FastAPI instrumentation on a non-global tracer provider (no other library
  can emit spans, so no prompt/completion capture), no header capture, `/healthz` excluded.
  `MetadataOnlySpanExporter` rebuilds every span with method, route template and status
  code only: no events (exceptions are recorded there), no status description, no URL,
  query string, client address or user agent; the span name is rebuilt from method + route.
- Azure exporters run with `disable_offline_storage=True` (a failed batch is dropped, never
  written to a local retry folder) and statsbeat off.

## Reproducibility

Fingerprint = sha256(doc) + parser_id + tenant + doc_type + schema_hash + deployment +
PROMPT_VERSION + RULES_VERSION + seed. Contains no document content.
`schema_hash` covers every field attribute (including `checkbox` and `box_labels`);
`RULES_VERSION` versions the code that turns model output into facts, and a guardrail test
fails if that code changes without a bump (D6).
Optional drift check: store only HMAC(secret, question) and HMAC(secret, answer facts) with the
fingerprint (keyed hash because short answers are guessable). Requires legal OK.

## Deployment (POC)

Local (M5a): `docker compose up -d --build` runs Postgres and the app image (Debian 13 slim,
Python 3.12, Tesseract 5.5 from the distro, non-root, read-only filesystem, 2 CPUs / 4 GiB);
`.env` and `db/.env` are passed as environment at run time and never enter the image
(`.dockerignore` is an allow-list). `scripts/smoke_demo.py` runs the demo flow against it.
Azure (M5b): one Azure Container App (single replica) · Azure OpenAI deployment in a Foundry project ·
PostgreSQL with synthetic tenants · Managed Identity · Key Vault for the HMAC secret ·
Application Insights (metadata only).
