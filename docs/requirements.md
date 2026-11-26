# Requirements

## Problem

An agent must answer questions combining (a) a per-tenant relational database and (b) PDF
documents uploaded during a session (similar to attaching a PDF to a chat). The database holds
the metadata of the fields each document type can contain, not the document values.

## Constraints

- Document data must not be stored after the session. Interpreted as **strictest workable**:
  document content only in process memory; the only external processors are Azure services
  covered by the BAA (Azure OpenAI; Document Intelligence optional), pending legal confirmation.
- One week to build and ideally deploy.
- Same PDFs + same question in different sessions → same answer, defined as **same facts and values**.

## Functional

1. A user belongs to a tenant and starts a session.
2. The user uploads one or more PDFs to the session and selects the document type.
3. The system parses each PDF and extracts the fields defined for that document type in the
   tenant's metadata.
4. The user asks questions in natural language; the agent combines tenant DB data and
   extracted document facts.
5. Answers cite their sources: document (sha256 prefix, page, quote) and/or DB table.
6. When the session is closed or its TTL expires, all document-derived data is destroyed.

## Non-functional

- **Privacy:** no document content on disk, in a DB, cache, logs, traces or exceptions.
- **Reproducibility:** identical inputs and configuration → identical facts; measured by a test.
- **Security:** read-only DB access, tenant injected by code, Entra ID auth, no secrets in code.
- **Latency (POC targets):** extraction < 60 s for a 30-page digital PDF; answers streamed as
  NDJSON events (D14): first (progress) event < 2 s, verified answer < 30 s. Answer text is not
  streamed token by token, because it must pass the code check first.
- **Scale (POC):** tens of users, one replica, ≤ 5 documents and ≤ 30 pages per document
  per session (configurable; reject above limits with a clear error).

## Out of scope for the POC

Multi-replica sessions, large-document storage, automatic document-type classification,
production auth flows, fine-tuning.
