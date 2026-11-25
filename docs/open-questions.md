# Open questions

| # | Question | Owner | Status | Default until answered |
|---|----------|-------|--------|------------------------|
| 1 | Is temporary retention by BAA-covered Azure services acceptable (Azure OpenAI abuse monitoring, Document Intelligence ≤24 h)? | Legal / stakeholders | Open | Azure OpenAI yes (required); Document Intelligence off |
| 2 | Is encrypted temporary storage wiped at session end allowed, or memory only? | Legal / stakeholders | Open | Memory only |
| 3 | May we store keyed hashes (no content) of documents/questions/answers to verify reproducibility? | Legal | Open | Not stored; reproducibility checked only in tests |
| 4 | Sample documents: digital or scanned? Typical size and count per session? | Data provider | Open | Synthetic fixtures; ≤5 docs, ≤30 pages |
| 5 | Exact shape of the tenant field metadata? | Data provider | Open | `fixtures/tenant_schema.sql` |
