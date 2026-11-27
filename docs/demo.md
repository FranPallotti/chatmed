# Demo

The full 10-minute script is written in M5c. Until then, this page names the tenants
(the UI does not list them, D16) and how to start the local demo.

## Tenants (synthetic)

| Tenant id | What it shows |
|-----------|---------------|
| `acme` | Synthetic CIOMS fixtures in `fixtures/` describe acme subject `01001-004` |
| `globex` | Different CIOMS field metadata; used to show tenant isolation |

## Local demo

1. `docker compose up -d --build` (setup: `docs/deploy.md`).
2. Open <http://localhost:8000>, type the tenant id `acme`, create a session.
3. Upload `fixtures/cioms_initial_scanned.pdf` with document type `cioms`.
4. Ask, for example, "Was the patient hospitalised?" and look at the statuses
   (`undeterminable` for checkbox fields) and citations.
5. Close the session: the server wipes documents, facts and history.
