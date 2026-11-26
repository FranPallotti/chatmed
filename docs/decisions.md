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

## D10. PDF text extraction and OCR
- Options: PyMuPDF · pypdfium2 · pdfplumber (text); Tesseract · RapidOCR · Document Intelligence (OCR).
- **Decision:** `pypdfium2` for the text layer and for rendering pages to images; Tesseract 5 via
  `pytesseract` for pages without a text layer; Document Intelligence stays an optional backend (D1).
- **Why (measured on a synthetic lab report, 2 vCPU / 8 GB):**
  - pypdfium2 extracted every line correctly and also renders pages, so one library covers both needs.
    License BSD-3/Apache-2.0. PyMuPDF is AGPL (strong obligations for a company product) and its
    current version pulls in onnxruntime.
  - Tesseract at 300 DPI: every line correct, ~2.2 s/page, 3 runs byte-identical, ~290 MB peak.
    License Apache-2.0.
  - RapidOCR: ~29 s/page and the process was killed for memory on the second page. Rejected.
- **Consequences:** Tesseract is a system binary: install it locally (Windows:
  `winget install -e --id UB-Mannheim.TesseractOCR`) and in the Docker image (`apt-get install
  tesseract-ocr`, M5). Its version, DPI and language go into `parser_id`. Tables come out as plain
  lines; if real samples need table structure, use the Document Intelligence backend.

## D11. Calling Tesseract without temp files (amends D10)
- Context: D10 named `pytesseract`, but `pytesseract.save()` writes each page image to a
  `NamedTemporaryFile` and Tesseract then writes its text to a temp output file: page images and
  extracted text on disk (hard rule 1).
- Options: `pytesseract` · `tesserocr` (C bindings, hard to install on Windows) · run the
  `tesseract` binary ourselves over stdin/stdout.
- **Decision:** run `tesseract stdin stdout -l <lang> --dpi <dpi> --psm 3` as a subprocess; the page
  is rendered by pypdfium2 to an 8-bit grayscale PGM in memory and written to stdin; text is read
  from stdout. No `pytesseract`, no Pillow at runtime. Approved by the project owner (M1 plan).
- **Why:** nothing touches disk, one dependency fewer, and the exact command line is explicit
  and goes into `parser_id`. Measured on the scanned fixture: every line correct, two runs
  byte-identical.
- **Consequences:** Tesseract's stderr is discarded and errors carry only the exit code (stderr
  could echo content). `TESSERACT_CMD` points to the binary when it is not on PATH. Per-page
  timeout of 120 s.

## D12. Clinical trial domain and handling of the real samples
- **Context:** the data provider's samples are a CIOMS I adverse-reaction report (scanned PDF) and
  CDISC ADaM datasets ADSL (subjects) and ADAE (adverse events), matched by subject id. Their README
  says it is real study data, to be kept internal until cleared for external sharing.
- **Decision (domain):** tenant DBs hold ADSL/ADAE-shaped tables; the document type is `cioms`, with
  per-tenant CIOMS field metadata; `subject_id` joins to `adsl.subjid`. Patient initials and date of
  birth are not extracted (data minimisation).
- **Decision (samples):** strictest reading until written clearance. Real samples stay in `samples/`
  (gitignored), are never committed, never shown to AI assistants, never sent to Azure OpenAI.
  Only fully local steps (pypdfium2, Tesseract) may be run on them, by the developer, outside tests.
  All development, tests and demos use synthetic data built from the public CIOMS I and ADaM structures.
- **Why:** the challenge is about handling regulated data correctly; relaxing later is a config and
  data swap, while a wrong "yes" can't be undone.
- **Enforced by:** hard rule 9 in `CLAUDE.md`, deny rules for `samples/**` in `.claude/settings.json`,
  `tests/guardrails/test_no_real_samples.py`, `.gitignore`.
- **If cleared:** drop the real files in `samples/`, add a local loader for the SAS datasets into the
  tenant DB, and run the same pipeline. No redesign.
