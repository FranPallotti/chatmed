Implement milestone $ARGUMENTS from docs/milestones.md.

1. Read CLAUDE.md, docs/architecture.md, docs/decisions.md and the milestone's acceptance criteria.
2. Write a short plan: files to create/change, tests per acceptance criterion, and how each hard
   rule in CLAUDE.md is respected. Show it to me and wait for approval before coding.
3. Implement with tests. Keep interfaces as documented; don't add dependencies without saying why.
4. Run `uv run python scripts/check.py` until it passes. Never skip or weaken tests/rules to pass.
5. Report: what changed, each AC → test that proves it, open questions, anything that needs a
   decision (do not decide it yourself if it is not in docs/decisions.md).
