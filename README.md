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
1. `uv sync`
2. `docker compose up -d` — starts PostgreSQL with two synthetic tenants (`tenant_acme`, `tenant_globex`).
   Reset with `docker compose down -v && docker compose up -d`.
3. Copy `.env.example` to `.env` and fill the Azure values. Run `az login` (no API keys are used).
4. `uv run python scripts/check.py`

## Working with the coding agent
1. `uv sync`
2. In Claude Code: `/milestone M0`, review the plan, approve, let it implement.
3. `/review` before committing. Then `uv run python scripts/check.py` must pass.
4. Next milestone.

## Status
- Seeded: `src/chatmed/extraction.py` + tests (schema-driven extraction, grounding, merge rules).
- Next: M0 (skeleton, config, metadata-only logging, guardrail scaffolding).
