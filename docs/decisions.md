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
- **Amendment (M3 review): every output-deciding module is under exactly one guard.** A module
  "decides output" if, with the same inputs (PDF bytes, tenant data, question, model output),
  changing its code can change page text, facts, `facts_used`, statuses or which answer is
  returned. Each such module is hashed by exactly one guard; `test_guard_coverage.py` walks the
  `chatmed` package and fails on a module that is unclassified or listed twice.
  - **Rules version** (`RULES_VERSION`, since `rules-3`; `test_rules_version.py`), in order:
    `chatmed.extraction` (extraction prompt and schema, grounding, checkbox rules, merge,
    subject join), `chatmed.agent.reconcile` (adae record selection, item comparison),
    `chatmed.agent.answer` (answer checker, `facts_used` closure, fallback text),
    `chatmed.agent.tools` (tool rows and ids, printed-subject-id mapping, joined `subject_id`
    fact in the reconciliation unit), `chatmed.agent.retrieval` (whole text vs BM25, page
    order), `chatmed.agent.loop` (one retry, iteration and time limits, answer fingerprint),
    `chatmed.tenant_db.repository` (which tenant rows and columns the tools see),
    `chatmed.parsing.base`, `.pdfium`, `.tesseract_ocr`, `.docintel`, `.routing` (page text).
  - **Agent prompt version** (`AGENT_PROMPT_VERSION`, `agent-v3`;
    `test_agent_prompt_version.py`): `chatmed.agent.prompt`, which holds everything the agent
    model is shown (system prompt, session context, retry note, tool and answer schemas).
    The extraction prompt stays in `chatmed.extraction`, so it is under the rules guard (and
    `PROMPT_VERSION` in the extraction fingerprint).
  - **Not output-deciding** (listed with reasons in `test_guard_coverage.py`): package
    `__init__` re-exports, `chatmed.models` (dataclasses), `chatmed.config` (settings loading),
    `chatmed.logconfig` (logging), `chatmed.api` (transport), `chatmed.parsing` (picks the
    parser from config; the choice is in `parser_id`), `chatmed.session.base` / `.memory`
    (storage; every consumer sorts documents by sha256 itself).
  - **Settings (decided after the M3 review):** code is covered by the module guards; setting
    *values* are split in `chatmed.config` into `FINGERPRINT_SETTINGS` and
    `OPERATIONAL_SETTINGS`, and `tests/test_config.py` fails unless every `Settings` field is
    in exactly one. Fingerprint settings can change facts for the same inputs and go into
    both fingerprints by value (`name=value`, sorted by name; `config.fingerprint_settings`),
    the extraction fingerprint of each document and the answer fingerprint (rules-5): model
    endpoint, deployment, API version, seed,
    `EXTRACTION_MAX_CHARS` (extraction windows), `AGENT_FULLTEXT_MAX_CHARS`,
    `AGENT_BM25_TOP_K`, the parser settings (`PARSER`, `OCR_BACKEND`, `OCR_DPI`, `OCR_LANG`,
    also in `parser_id`) and `TENANT_DB_DSN_TEMPLATE` (which databases the data comes from).
    Operational settings only decide whether a verified answer is produced, or how the service
    runs, and stay out: LLM timeout/attempts/backoff, `AGENT_MAX_ITERATIONS`,
    `AGENT_QUESTION_TIMEOUT_SECONDS`, both `*_MAX_COMPLETION_TOKENS` (a truncated reply is an
    error, never partial facts), `DUCKDB_MEMORY_LIMIT` (no spill to disk: over the limit a
    query fails, it cannot return other rows), admission limits, `TENANTS`,
    `DOCINTEL_ENDPOINT` and `TESSERACT_CMD` (the model id and binary version are in
    `parser_id`), session settings, `LOG_LEVEL`. A run an operational setting cuts short ends
    with outcome `fallback` (or an error event), which claims no reproducibility (D14).

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

## D14. Agent loop, reconciliation, session tenant and answer streaming (M3)
Approved by the project owner with the M3 plan.
- **Session tenant:** `POST /sessions {"tenant_id": "acme"}`; the value must match
  `^[a-z0-9_]+$` and be in `TENANTS` (D13a), else 422. It is fixed for the session's life, the
  tenant repository is bound to it, and no agent tool has a tenant argument (rule 5).
  **M5:** with Entra ID auth the tenant comes from the caller's token (an app role or group
  claim mapped to one tenant), and the request field is removed or must equal it.
- **Matching `adae` record for `reconcile_case`** (tiered, deterministic, in code):
  1. exactly one of the subject's `adae` rows has `astdt` equal to one of the document's onset
     values (`ok`, or the values of a `conflict`) → that record; onset item `match`;
  2. otherwise, the unique serious (`aeser = 'Y'`) row whose `astdt` is nearest to an onset
     value, within `AE_MATCH_WINDOW_DAYS = 7` → that record; onset item `mismatch` with the day
     difference;
  3. otherwise (no onset value, no row in the window, or a tie) → every item `not_comparable`,
     reason `no_unique_ae_record`.
  An onset `conflict` is never compared: the onset item is `not_comparable` (reason `conflict`)
  even when its values selected the record. The window is a rule: `chatmed.agent.reconcile` is
  one of the modules hashed for `RULES_VERSION` (D6), which became `rules-2`.
- **Action taken / outcome:** the CIOMS field metadata has no such fields. These items are
  `not_comparable`, reason `no_document_field`, unless the tenant defines fields with keys
  `action_taken` / `outcome`. No seed fields are added.
- **Seriousness:** the tenant's `serious` field if it has one, else the four CIOMS boxes
  (`patient_died`, `hospitalisation`, `disability`, `life_threatening`): any `ok` true → `Y`;
  all `ok` false → `N`; otherwise `not_comparable` (reason: the statuses, e.g. `undeterminable`).
- **Answers:** the model ends the loop with a strict JSON answer `{text, cited_ids, quotes}`.
  Code builds `facts_used` from the ids of tool results it holds (values never come from the
  model), grounds quotes against page text, and rejects text whose numbers/dates are not in
  `facts_used`. One retry, then a deterministic fallback text built from `facts_used`.
  Output that is not exactly one JSON object (the real model once sent two back to back) is a
  rejected answer and gets the same one retry. Every model call, rejected answers included,
  counts toward `AGENT_MAX_ITERATIONS`. A rejected output is never logged: only its length
  (`output_chars`) and the reason code.
- **Checker details (M3 review):**
  - Dates are compared as dates in any written form (ISO, `12 June 2026`, `12-JUN-2026`,
    `June 12, 2026`, `12/06/2026` read both day- and month-first; month-name dates without a
    year match on month and day). A written date that is not a real date is rejected. Before,
    "June 6, 2026" passed against 2026-06-12 because 6 and 2026 occurred in it.
  - On the source side (`facts_used` values and quotes) a date supports only its one reading
    (rules-6). An ambiguous source such as `12/06/2026` (12 June or 6 December) supports no
    resolved date: the text may repeat it as written (`12/06/2026`, `12.06.2026`) but not
    state 2026-06-12 or 2026-12-06. The text's own numeric dates may still be read either way
    against unambiguous sources.
  - Identifiers are not number sources: the `doc` sha prefix and the citation `key`/`table`
    are skipped, so digits of a sha (`7652ea457661`) cannot support "7652".
  - A status counts as stated only as a whole word (plural/`-ing` allowed; a hyphen on either
    side makes it another word, so "conflict-free" and "non-conflicting" do not count) with
    no negation anywhere before it in the same clause (rules-6; before, only the 3 words
    before it were checked, so "no evidence of any conflict" passed). Negations: `no`, `not`,
    `never`, `without`, `none`, `nothing`, `absence`, `lack`, `free`, ..., and any word ending
    in `n't`. Clauses end at punctuation, dashes and `but`/`and`/`or`/`so`/`because`/`since`/
    `while`/`whereas`/`although`/`though`/`however`. The `not` of `not comparable` /
    `not attempted` is part of the status, not a negation. This errs towards rejecting (a
    retry) rather than towards hiding a status; it is a check on top of the prompt, which
    asks for the status names verbatim.
- **Question time limit:** `AGENT_QUESTION_TIMEOUT_SECONDS` (default 25 s, under the 30 s
  verified-answer target) covers the whole question: session context, every model call,
  retries with their backoff, and tools. When it runs out, the in-flight call is cancelled and
  the deterministic fallback is returned with reason `deadline` (logged as `agent.deadline`,
  metadata only). The per-call `LLM_TIMEOUT_SECONDS` still applies inside it.
- **Reconciliation is one unit in `facts_used` (approved after AC9 runs, option A):** citing
  any `recon:<doc>:` row adds all reconciliation items of that document, the `adae` record
  they were compared with, and the document's **joined** `subject_id` fact. `reconcile_case`
  returns and registers that fact itself (same id as `query_document_facts` gives it), so it
  is added by code whether or not the model queried or cited it; an unjoined subject fact is
  not added. The real model cited varying subsets across runs, which changed `facts_used` and
  let `not_comparable` items drop out of the text; with the whole unit cited, the status check
  makes the answer state every `not_comparable`.
- **Session context (approved after AC9 runs):** a second system message lists the session's
  documents (sha prefix, type, page count; sorted by sha256) and the tenant's field keys per
  document type (sorted). Metadata only. It is part of the agent prompt: since `agent-v3`
  everything the model is shown lives in `chatmed.agent.prompt`, and
  `tests/guardrails/test_agent_prompt_version.py` hashes that whole module and fails unless
  the hash is recorded for the current `AGENT_PROMPT_VERSION` (D6, M3 review amendment). A
  recorded hash is never re-recorded: any change bumps the version.
- **Status names are checked verbatim (`agent-v2`):** the status check requires each status of
  a cited row by its name (underscore or space). With `agent-v1` the real model wrote
  "cannot be compared" for `not_comparable`, so every reconciliation answer was rejected once
  (`status_not_stated`) and only verified on the retry. The check is not loosened to accept
  paraphrases (that would weaken AC4); `agent-v2` tells the model the names are checked
  verbatim, with that example. Reconciliation questions are answered from `reconcile_case`
  only. AC9 also requires first-attempt verification.
- **Answer fingerprint and outcome (revised after the M3 reviews):** the fingerprint is the
  sha256 of `AGENT_PROMPT_VERSION`, `RULES_VERSION`, every `FINGERPRINT_SETTINGS` value
  (`name=value`, sorted by name), the session's tenant id, the sorted extraction
  fingerprints of the session's documents (D6), and since rules-6:
  - a sha256 of the session history sent with the question (earlier questions and answer
    texts), because earlier turns steer which tools the model calls. So the claim holds per
    conversation: the same question after the same earlier turns;
  - a sha256 per tenant DB read the tools made (`field_schema`, `subject`, `adverse_events`,
    keyed by call and arguments; for `reconcile_case`, all events of the subject it chose
    from, not only the selected record), because tenant study data can change between
    sessions while its schema hash stays the same. A tenant change the question does not
    read leaves the fingerprint unchanged.
  Only digests enter the fingerprint, never history text or tenant values. The extraction fingerprint holds the same settings values, next to the
  document sha256, `parser_id`, the tenant schema hash, `PROMPT_VERSION` and
  `RULES_VERSION`. Operational settings are not in it. The `answer` event carries
  `outcome: verified | fallback`; a fallback carries a fixed `reason` code (`max_iterations`,
  `deadline`, `value_not_in_facts_used`, ...) and no fingerprint. Only verified answers
  claim reproducibility: same fingerprint and question → same `facts_used`. That is what AC9
  measures, and the reproducibility tests compare verified answers only.
- **Printed subject id (approved after AC9 runs):** `get_subject` / `list_adverse_events`
  accept the `join_value` of a joined `subject_id` fact or that fact's id exactly as printed
  (`101-01001-004`), which maps to the same fact's `join_value` only. Nothing else matches;
  an unjoined or unknown id is refused before the tenant DB is called.
- **Streaming (`POST /sessions/{id}/ask`):** NDJSON. Progress events carry metadata only
  (`{"event": "tool", "name": ..., "iteration": ...}`); one final `answer` event carries
  `outcome`, `reason`, the text, `facts_used` and (verified only) `fingerprint`. Tokens are not streamed because text must pass the code check
  before anyone sees it. Targets (requirements): first event < 2 s, verified answer < 30 s.
- **Interface change:** `SessionStore.get(session_id)` was added: the API needs a session's
  tenant, history and lock by id. Starlette's `UploadFile` is not used (it spools uploads over
  1 MB to a temp file); multipart bodies are parsed in memory with the stdlib `email` parser.
  The upload body is read as a stream with a byte counter: a declared Content-Length over the
  limit (`MAX_UPLOAD_MB` + 64 KiB multipart overhead) is refused with 413 before any byte is
  read, and the first chunk past the limit ends the read with 413 (Content-Length may be absent
  or wrong). The file part itself is then checked against `MAX_UPLOAD_MB`.

## D15. Session lifecycle, concurrency, capacity and the pipeline module (M4)
Approved by the project owner with the M4 plan.
- **Idle TTL.** A session expires after `SESSION_TTL_SECONDS` without use. "Use" is
  `SessionStore.get`, which the pipeline calls when a request starts and when it ends, so a
  request in progress keeps its session alive; reading documents or facts does not count.
  Expiry is lazy as well as swept: an expired session already answers `session_closed` before
  the sweeper (an asyncio task of the app, every `SESSION_SWEEP_INTERVAL_SECONDS`) reaches it,
  so the TTL is exact whatever the sweep interval. Why idle and not absolute: D5 only said
  "TTL + wipe"; a reviewer working through a case should not lose the session mid-review.
- **Close is immediate and cancels in-flight work.** `close(id, reason)` (reason `client` |
  `ttl` | `shutdown`) marks the session closed, cancels its in-flight tasks, closes its DuckDB
  and drops documents, facts and history. Every upload and question runs as a task registered
  on the session (`pipeline.run_in_session`); the cancellation lands at the task's next await,
  before it can write, and the request ends with `session_closed` (404 / NDJSON `error`). A
  caller's own cancellation (client gone) propagates unchanged and leaves the session open.
  The store also refuses writes for a closed session, and the facts insert plus
  `put_document` run with no await between them, so a close can never land half-way.
  Why cancel rather than wait: a wipe that waits on a 25 s question is not a wipe; and without
  the cancellation a question carried on against the closed connection (seen in a mutation
  test: `tool_failed` from the model's tools).
  A cancelled OCR call kills and reaps its Tesseract process (it holds the page image), so
  nothing outlives the request (`rules-8`, found in the M4 review). Work already handed to a
  thread (`asyncio.to_thread`: pdfium text, page rendering) cannot be cancelled: it finishes
  its one call in memory and its result is dropped.
- **Closed ids.** The store remembers closed ids (random tokens, no content), the latest
  `CLOSED_IDS_KEPT = 10_000`, so a late call gets `session_closed`; an id never created (or
  forgotten) gets `session_not_found`. Both are 404.
- **Capacity.** `MAX_SESSIONS` (default 20, operational) caps open sessions; expired sessions
  are wiped before the check. Above it `POST /sessions` is 503 `capacity`, logged as
  `session.rejected`. `DUCKDB_MEMORY_LIMIT` default lowered from 1 GB to 128 MB (the facts
  table is tiny; 20 × 1 GB was not a bound). Worst case in `architecture.md`.
- **Memory limit.** No spilling, so over `memory_limit` DuckDB fails: on upload the facts
  insert (one transaction) is rolled back and the request is 503 `session_memory_limit`; in a
  tool the question ends with the `error` code `session_memory_limit` instead of reaching the
  model as `tool_failed` (`chatmed.agent.loop`, hence `rules-7`). Found while testing: on
  DuckDB 2.0 `enable_external_access=false` does not lock `memory_limit` (`SET memory_limit`
  still worked), so the connection now also runs `SET lock_configuration=true` last, which
  makes every setting read-only (tested).
- **Interface change.** `SessionStore` gains `expire_idle() -> int` and
  `close_all(reason) -> int` (the app runs the sweeper and the shutdown wipe without knowing
  how sessions are stored; the encrypted store will need both), and `close` takes the reason.
  `Session` gains `closed` and `inflight`.
- **Pipeline module.** `chatmed.pipeline` holds what `chatmed.api` did besides HTTP
  (`Services`, create, upload, ask, the sweeper, the shutdown wipe) plus
  `run_full_session` (create → upload → ask → close). The API is a thin transport over it, and
  the guardrail tests run the same code. It is classed as not output-deciding, like `api`
  (orchestration moved, not new rules; `test_guard_coverage.py`).
- **Lifecycle events** (metadata only): `session.created` (session, tenant),
  `session.closed` (session, tenant, `reason`), `session.rejected` (tenant, `status`
  `capacity`), `session.swept` and `session.shutdown` (`session_count`).
