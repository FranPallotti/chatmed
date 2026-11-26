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
- Read checkbox states (D13): Document Intelligence selection marks, or a local detector that finds
  the boxes on the rendered page and measures their fill.
- Encrypted blob session store with crypto-shredding (must pass the session store contract tests).
- Self-consistency extraction (k runs, per-field majority).
- Automatic document-type classification limited to the tenant's types.
