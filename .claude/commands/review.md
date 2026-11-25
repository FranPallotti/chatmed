Review the current uncommitted changes (git diff) against the project rules.

Check, and report findings ordered by severity:
1. Hard rules in CLAUDE.md: any document content that could reach disk, logs, exceptions,
   caches or external services other than Azure OpenAI / configured parser; DuckDB not in memory;
   model calls without temperature 0 + seed + strict schema; tenant not injected by code.
2. Reproducibility: unstable ordering (sets, dict order from external input, async gather
   results used without sorting), time- or randomness-dependent behavior.
3. Acceptance criteria of the current milestone: which are covered by tests, which are not.
4. Readability: anything the author could not explain in a code review.
Do not change code in this command; only report.
