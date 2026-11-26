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
- Each field returns `{value, evidence{page, quote}}` or nulls.
- Code validates types (Decimal/ISO date parsed in code) and grounds quotes against page text.
- Statuses: `ok`, `missing`, `unverified`, `invalid`, `conflict`, `undeterminable` (checkbox
  fields with no explicit text statement, D13). Subject join (code, D13): `joined` / `unmatched` / `ambiguous`.
- Large documents: deterministic page windows; merge rules: single-value → one verified value
  or `conflict`; multiple → union, dedupe, page order.
- Facts go into the session DuckDB table `facts` (long format; joins to DB via `join_target`).

### Session (`src/chatmed/session/`)
- `SessionStore` interface. POC: `InMemorySessionStore` (dict, TTL sweeper, explicit wipe).
- Each session owns: tenant id, documents, a DuckDB `:memory:` connection, chat history.
- DuckDB settings per connection: in-memory only, no spilling to disk
  (set `temp_directory` to an empty value and verify with the no-persistence guardrail test;
  keep `memory_limit` within container RAM and fail clearly if exceeded).
- Later (only if time / needed): `EncryptedBlobSessionStore` with **crypto-shredding**:
  per-session AES-256 key held only in memory; dropping the key on close makes any
  remaining copy unreadable.

### Tenant DB (`src/chatmed/tenant_db/`)
- One read-only role per tenant (`chatmed_reader_{tenant}`), which can only connect to its own
  database (D8). Repository functions take `tenant_id` from the session, never from the model.
- Agent tools are parameterized queries for main paths.
- Restricted SQL tool (optional): SELECT only, allow-listed tables/views, tenant filter enforced,
  row limit, timeout. Parsed/validated before execution (e.g. `sqlglot`).

### Agent (`src/chatmed/agent/`)
- Code-first loop with function calling over stateless chat completions
  (no Agent Service threads, no `store=True`).
- Tools: `query_document_facts`, `get_field_metadata`, tenant DB tools, optional `restricted_sql`.
- Fallback for free-text questions not covered by fields: pass relevant page text
  (small docs: whole text; large docs: in-memory BM25 over pages, ties broken by page number).
- Answers: values come from tool results; the model formats them; every claim carries a citation.
- Max iterations, per-call timeout, retries with exponential backoff + jitter on 429/5xx.

### API (`src/chatmed/api.py`)
- `POST /sessions` → session id
- `POST /sessions/{id}/documents` (multipart PDF + doc_type) → document sha256 + extraction summary
- `POST /sessions/{id}/ask` → streamed answer with citations
- `DELETE /sessions/{id}` → wipe
- Request-body logging disabled. Upload size and page limits enforced.

### Logging (`src/chatmed/logconfig.py`)
- `configure_logging()` installs one JSON handler on the root logger with `MetadataOnlyFilter`.
- Log calls use a fixed event name as the message (`log.info("doc_parsed", extra={...})`).
  Free-text messages (f-strings, third-party prose) are replaced by `unstructured_message`;
  `%s` args are never rendered; `extra` keys outside `ALLOWED_FIELDS` and non-scalar or long
  values are dropped; exceptions keep only their type and code locations.
- The filter only sees records that reach its handler. Loggers with their own handlers
  (uvicorn) and extra handlers (OpenTelemetry) must be routed through it or get their own filter
  (to do in M3/M5).

## Reproducibility

Fingerprint = sha256(doc) + parser_id + tenant + doc_type + schema_hash + deployment +
PROMPT_VERSION + seed. Contains no document content.
Optional drift check: store only HMAC(secret, question) and HMAC(secret, answer facts) with the
fingerprint (keyed hash because short answers are guessable). Requires legal OK.

## Deployment (POC)

One Azure Container App (single replica) · Azure OpenAI deployment in a Foundry project ·
PostgreSQL with synthetic tenants · Managed Identity · Key Vault for the HMAC secret ·
Application Insights (metadata only).
