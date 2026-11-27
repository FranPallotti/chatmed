# ChatMed — session-scoped agent over tenant DB + medical PDFs

An agent answers questions combining a per-tenant relational database with PDFs uploaded into a
session. Document content lives only in memory for the session and is wiped at the end.
Same PDFs + same question → same facts and values.

## Start here
- `CLAUDE.md` — rules and workflow for the coding agent (and for humans).
- `docs/requirements.md` — what and why.
- `docs/architecture.md` — how.
- `docs/decisions.md` — every design decision with its reason.
- `docs/milestones.md` — build plan with acceptance criteria.
- `docs/open-questions.md` — pending answers and the defaults used meanwhile.

## Local setup
Prerequisites: Python 3.12 with `uv`, Docker, and the Azure CLI with the Bicep CLI
(`az bicep install`: the infra tests compile `infra/*.bicep`). Tesseract 5 for OCR (D10).

1. `uv sync`
2. Copy `db/.env.example` to `db/.env` and set the passwords. Then `docker compose up -d` — starts
   PostgreSQL with two synthetic tenants (`tenant_acme`, `tenant_globex`), each with its own read-only role.
   Reset with `docker compose down -v && docker compose up -d`.
3. Copy `.env.example` to `.env` and fill the Azure values. Run `az login` (no API keys are used).
4. `uv run python scripts/check.py`
5. Run the app with both env files (it reads the tenant DB passwords from `db/.env` at startup
   and refuses to start without them):
   `uv run --env-file .env --env-file db/.env uvicorn chatmed.api:app --reload`
   (or the whole stack in containers: `docker compose up -d --build`, `docs/deploy.md`).
   Azure: `docs/deploy.md`.

## Working with the coding agent
1. `uv sync`
2. In Claude Code: `/milestone M0`, review the plan, approve, let it implement.
3. `/review` before committing. Then `uv run python scripts/check.py` must pass.
4. Next milestone.

## Status
- Seeded: `src/chatmed/extraction.py` + tests (schema-driven extraction, grounding, merge rules).
- Next: M0 (skeleton, config, metadata-only logging, guardrail scaffolding).
