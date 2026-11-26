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
  `close`; `get` added in M3, D14). POC: `InMemorySessionStore` (dict, explicit idempotent wipe;
  the TTL sweeper comes in M4).
- Each session owns: tenant id (fixed at creation, D14), documents (ordered by sha256), a DuckDB
  `:memory:` connection, chat history (questions and verified answer texts), and a lock that
  serializes uploads and questions within the session.
- DuckDB is opened with `temp_directory=''`, `memory_limit`, then
  `SET enable_external_access=false` (no `COPY TO`, `ATTACH` or file reads, and the two settings
  above can no longer be changed on that connection).
- DuckDB settings per connection: in-memory only, no spilling to disk
  (set `temp_directory` to an empty value and verify with the no-persistence guardrail test;
  keep `memory_limit` within container RAM and fail clearly if exceeded).
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
  `facts_used`, and rejects text that cites a row with an honesty status (`undeterminable`,
  `conflict`, `unverified`, `missing`, `invalid`, `unmatched`, `ambiguous`, `not_attempted`,
  `not_comparable`) without naming it. Output that is not exactly one JSON object (the real
  model once sent two) is also a rejection. One retry, then a deterministic fallback listing
  the retrieved rows with their statuses. A cited `recon:` row brings in the whole
  reconciliation of its document, the compared `adae` record and the document's joined
  `subject_id` fact, which `reconcile_case` registers itself (D14). Each answer carries a
  fingerprint (agent prompt version, rules version, deployment, seed, document fingerprints).

### API (`src/chatmed/api.py`)
- `POST /sessions {"tenant_id"}` → session id (tenant checked against `TENANTS`, D14)
- `POST /sessions/{id}/documents` (multipart `file` + `doc_type`) → sha256, page count,
  parser id, fingerprint, counts per status, join status (no values)
- `POST /sessions/{id}/ask {"question"}` → NDJSON: `started`, metadata-only `tool` events,
  then one `answer` event (`verified`, `reason`, `text`, `facts_used`, `fingerprint`) or an
  `error` code (D14)
- `DELETE /sessions/{id}` → idempotent wipe (204)
- The multipart body is streamed into memory with a byte counter (limit `MAX_UPLOAD_MB` +
  64 KiB): an oversized Content-Length is refused before reading, and the first chunk past the
  limit ends the read. It is parsed with the stdlib `email` parser: Starlette's `UploadFile`
  spools files above 1 MB to disk.
- Limits: 413 `upload_too_large`, 422 `too_many_pages`, 409 `too_many_documents` /
  `duplicate_document`, 415 `pdf_required`. Errors are fixed codes. No body is ever logged.
- Run with `uv run uvicorn chatmed.api:app` or `uv run python -m chatmed.api`.

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
  logs unhandled route errors as `api.unhandled_error`. OpenTelemetry handlers (M5) must get
  their own filter.

## Reproducibility

Fingerprint = sha256(doc) + parser_id + tenant + doc_type + schema_hash + deployment +
PROMPT_VERSION + RULES_VERSION + seed. Contains no document content.
`schema_hash` covers every field attribute (including `checkbox` and `box_labels`);
`RULES_VERSION` versions the code that turns model output into facts, and a guardrail test
fails if that code changes without a bump (D6).
Optional drift check: store only HMAC(secret, question) and HMAC(secret, answer facts) with the
fingerprint (keyed hash because short answers are guessable). Requires legal OK.

## Deployment (POC)

One Azure Container App (single replica) · Azure OpenAI deployment in a Foundry project ·
PostgreSQL with synthetic tenants · Managed Identity · Key Vault for the HMAC secret ·
Application Insights (metadata only).
