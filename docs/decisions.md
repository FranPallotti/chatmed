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
- **Amendment (M2 review): rules version in the fingerprint.** The same model output can give
  different facts under different code rules (D13a, D13b, D13c each did). So the fingerprint also
  contains `RULES_VERSION` (`chatmed.extraction`). `tests/guardrails/test_rules_version.py`
  hashes `ast.dump()` of the modules holding the rules (today only `chatmed.extraction`:
  grounding, checkbox and box-label rules, merge, subject join) and fails unless the hash matches
  the one recorded for the current `RULES_VERSION`; a change means bump the version and record
  its new hash, never edit an old one. Comments, formatting and docstrings are ignored; any other
  code change in those modules needs a bump, even one that is not a rule (cheap, and safer than
  listing functions by hand). `ast.dump()` differs between Python minor versions, so the test
  requires the pinned 3.12. The fingerprint also contains `schema_hash`, which covers every field
  attribute, `checkbox` and `box_labels` included (tested).

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

## D13. What OCR on the real CIOMS showed, and how extraction handles it
- **Evidence:** the developer ran `scripts/ocr_check.py` locally on the data provider's real scanned
  CIOMS (D12: content not shared). Text was recovered well and deterministically, with three issues:
  1. The subject id on the form carries a prefix before the SUBJID (pattern `NNN-SSSSS-SSS`, where
     the SUBJID is `SSSSS-SSS`).
  2. Checkbox states are lost: tick marks come out as nothing or as noise characters, e.g. a box
     marked with an X was not detected at all.
  3. Two-column boxes are interleaved line by line, so a label and its value can be split by text
     from the neighbouring box.
- **Decision:**
  1. **Subject join in code, not by the model.** `subject_id` is extracted as written. Code
     normalizes separators (any run of non-alphanumerics → `-`) and matches it against
     `adsl.subjid` as an exact value or as a suffix after a separator. Exactly one match → joined;
     none → `unmatched`; more than one → `ambiguous`. No fuzzy matching.
  2. **Checkbox fields** (`field_definitions.checkbox = TRUE`) are filled only from an explicit text
     statement that passes grounding (e.g. "patient was hospitalised"). Tick marks are never
     inferred from OCR text; otherwise the status is `undeterminable` and the agent says the form
     must be checked visually. Reading tick marks is a separate capability: Document Intelligence
     selection marks (optional backend, needs D12 clearance for real data) or a local checkbox
     detector (stretch).
  3. **OCR-aware prompt:** the extraction prompt states that text may be interleaved across columns
     and contain OCR noise, and that quotes must be short verbatim fragments from one place.
- **Why:** a wrong "hospitalised: no" on a safety report is worse than "not determinable"; the
  join decides which subject's data the agent combines, so it must be deterministic and testable.

## D13a. Amendment to D13: checkbox sources, join statuses, tenant allow-list
Approved by the project owner with the M2 plan.
- **Checkbox source rule (amends D13.2):** a checkbox field's quote must contain words beyond the
  field's own label/description. Code normalizes the quote (case, accents, punctuation, OCR noise
  such as stray single characters), splits it into words and drops stop words ("or", "the",
  "of", ...). If every remaining word appears in the field's `label` or `description`, the quote
  is the printed box label, not a statement, and the field is `undeterminable`. A written
  negation ("not hospitalised") is a valid statement (`ok`, value false).
  Implementation details found on the synthetic scanned form (M2): an enum field's allowed values
  count as label words (the form prints the options "yes no NA", "initial followup"); tokens of
  one or two characters that are not all digits count as OCR noise (`X]`, `XK]`, `LJ` around the
  drawn boxes); hyphenated words also match their joined form ("follow-up" = "followup").
- **Join only from a verified subject id (amends D13.1):** the join runs only when `subject_id`
  is `ok`. `conflict` (different ids on different pages) → join status `ambiguous`;
  `unverified`, `invalid` or `missing` → join status `not_attempted`, with the subject_id status
  as the reason. The agent then says the subject could not be identified reliably; it never
  falls back to a guess. Why: linking the wrong patient's study data is the worst error this
  system can make.
- **Tenant allow-list (rule 5):** before `{tenant}` is filled into `TENANT_DB_DSN_TEMPLATE`, the
  tenant must match `^[a-z0-9_]+$` and be listed in the `TENANTS` setting (e.g. `acme,globex`),
  so a malformed tenant value can never change the connection string.

## D13b. Amendment to D13: printed box labels as metadata
Approved by the project owner after the first M2 run against the real model.
- **Why:** with the real Azure deployment, one run returned `patient_died = true` quoting the
  heading "8-12. Check all appropriate to adverse reaction: patient died". The heading has words
  beyond the field's label/description, so the D13a word rule accepted it; the next run said
  null. The same document gave different facts (breaks D6).
- **Metadata:** `field_definitions.box_labels TEXT[] NOT NULL DEFAULT '{}'` holds the text
  printed next to the field's box(es) on the form, copied from the form layout (e.g.
  `patient died`; `serious` in globex has the four seriousness labels).
  `CHECK (NOT checkbox OR cardinality(box_labels) > 0)`: every checkbox field has at least one
  label. The column is `NOT NULL` because a CHECK passes when it evaluates to NULL, so a NULL
  array would slip through.
- **Rule:** a checkbox quote is the printed label, and the field is `undeterminable`, if it
  contains all meaningful words of any one of the field's `box_labels` (same normalization as
  D13a: case, accents, punctuation, OCR noise, stop words; word order and extra words do not
  matter). The D13a rule still applies as well: a quote with no words beyond the label,
  description and options is a label quote too.
- **Known limitation:** a narrative statement that repeats a printed label word for word (e.g.
  "the patient died on 2026-06-20" for `patient died`) is also rejected and the field is
  `undeterminable`. We accept this: a missed `ok` is safe (the agent says it cannot tell from
  the document), a tick read from a printed label is not.
- **Tests:** the real-model test runs the same form at least 3 times and asserts identical facts.
- **Also found in that run (metadata only, no new rule):** in 1 of 9 real-model extractions
  `suspect_drug` also listed a concomitant drug, quoting box 22. The quote is on the page, so
  grounding cannot catch it. The `suspect_drug` description in both seeds now says
  "Box 14 only: ...; never the concomitant drugs of box 22". If it happens again, the next step
  is a code rule between fields (needs its own decision).

## D13c. Amendment to D13: the evidence quote is the full page line
Approved by the project owner after repeated real-model runs.
- **Why:** with values and statuses identical across 6 runs, the model still chose how much of
  a line to quote ("2a. Age: 47 years" in one run, "2a. Age: 47 years 3. Sex: F" in the
  others), so the stored facts differed (breaks D6).
- **Rule:** after grounding, code stores as `quote` the full page line(s) that hold the first
  occurrence of the model's quote (whitespace collapsed, case kept), not the model's
  fragment. A quote spanning lines stores all of them. Matching uses the same normalization as
  before (NFKC, case, whitespace). An ungrounded quote is stored as the model gave it, with
  status `unverified`.
- **Checkbox fields:** the label rule (D13a, D13b) is applied to both the model's quote and the
  stored line. A label fragment on a line shared with other text (OCR columns) is still a label
  quote, even though the line has extra words.
- **Trade-off:** a citation can be longer than needed (a whole form line, or OCR text from
  another column on the same line).
