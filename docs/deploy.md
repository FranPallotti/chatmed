# Deploy and clean up

- [Local container (M5a)](#local-container-m5a)
- [Azure (M5b)](#azure-m5b): prerequisites, deploy, update the image, update the IP, verify,
  before and after a demo, cost, delete
- [Clean up](#clean-up): local stack and the local service principal

Synthetic data only, everywhere. Decisions and weak points: D16 (local), D17 (Azure).

## Local container (M5a)

The container cannot use your host's `az login`, so it authenticates to Azure OpenAI as a
service principal with a single role on the Foundry resource and a short-lived secret (D16).

1. Create the service principal (once). It gets only `Cognitive Services OpenAI User`, scoped
   to the Foundry resource, and a secret that expires in about two weeks:

   ```powershell
   $scope = az cognitiveservices account show -n <foundry-resource> -g <resource-group> --query id -o tsv
   $appId = az ad sp create-for-rbac --name chatmed-local --role "Cognitive Services OpenAI User" --scopes $scope --query appId -o tsv
   az ad app credential reset --id $appId --end-date (Get-Date).AddDays(14).ToString("yyyy-MM-dd") --query "{tenant:tenant, appId:appId, password:password}"
   ```

   `credential reset` replaces the password `create-for-rbac` made, so the only valid secret
   is the 14-day one. Put the values in your `.env` (gitignored) as `AZURE_TENANT_ID`,
   `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` (see `.env.example`). Never commit or paste them.

2. Start the stack and run the smoke test:

   ```powershell
   docker compose up -d --build
   ```

   ```powershell
   uv run python scripts/smoke_demo.py --tenant acme
   ```

   The UI is at <http://localhost:8000>. `docker build --target test -t chatmed:test .` runs
   the OCR and parser tests inside the image.

3. Check that the logs hold metadata only (event names, ids, counts, statuses):

   ```powershell
   docker logs chatmed-app
   ```

## Azure (M5b)

Everything goes into `rg-chatmed-dev` (eastus2). The templates and scripts are in `infra/`.
Names are in `infra/base.bicepparam` and `infra/app.bicepparam`: the registry, Key Vault and
Postgres names are global, so if one is taken, change its suffix in both files and pass the
same names to the scripts. You run every command with your own `az login`.

### Prerequisites

- Azure CLI with Bicep (`az bicep install`), Docker Desktop (for psql), git, Windows
  PowerShell 5.1 or PowerShell 7.
- `az login` to the subscription of the Foundry resource. You need Owner (or User Access
  Administrator + Contributor) on `rg-chatmed-dev` and on the Foundry resource, because the
  templates create role assignments.
- The Foundry resource name and its resource group, and the model endpoint and deployment
  (the same values as `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT` in your `.env`).
- Your public IPv4 address. It goes into the app's allow-list and the Postgres firewall, for
  example:

  ```powershell
  $ip = (Invoke-RestMethod https://api.ipify.org)
  ```

### Deploy

Order: base → seed → image → app → verify (D17). Run from the repository root.

1. Base resources (5–10 minutes). `-BudgetEmail` only the first time: a budget's start date
   cannot move afterwards.

   ```powershell
   .\infra\deploy-base.ps1 -DeveloperIp $ip -FoundryResourceName <foundry-resource> -FoundryResourceGroup <foundry-rg> -BudgetEmail <your-email>
   ```

   The Postgres admin password is generated inside the deployment and stored in Key Vault as
   `pg-admin`. **Every redeploy of the base rotates it** (server and Key Vault together),
   which is harmless: only the seed script uses it.

2. Seed the tenant databases. New role assignments can take a few minutes to apply; if the
   Key Vault write is refused, wait and rerun.

   ```powershell
   .\infra\seed-db.ps1
   ```

   For each tenant it prints `seeded; chatmed_reader_<t> reads N field definitions over
   sslmode=verify-full`, or `already seeded, nothing changed`. No password is printed. If a
   run fails, run it again: a tenant without the `seed_info` marker is seeded again
   ([Reseed a tenant](#reseed-a-tenant)).
   If `verify-full` fails, the psql error is printed: rerun with `-SslMode require`, pass
   `postgresSslMode=require` to the app (edit `infra/app.bicepparam`) and record the error
   in D17.

3. Build the image from the committed code (commit first; the script refuses a dirty tree):

   ```powershell
   $image = .\infra\build-image.ps1
   ```

   It prints the commit and the image by digest (`…/chatmed@sha256:…`).

4. Create the app with that digest:

   ```powershell
   .\infra\deploy-app.ps1 -Image $image -DeveloperIp $ip -AzureOpenAiEndpoint <endpoint> -AzureOpenAiDeployment <deployment>
   ```

   It prints the app URL and the image digest the app is running.

### Reseed a tenant

A tenant counts as seeded once its database holds the `seed_info` marker, which the seed
writes in the same transaction as the data. So a run that failed is simply retried by running
`seed-db.ps1` again; there is nothing to clean up. To seed a tenant again on purpose (for
example after changing `db/seed/tenant_acme.sql`), drop its database, then seed it:

```powershell
$fqdn = az postgres flexible-server show -g rg-chatmed-dev -n psql-chatmed-dev-01 --query fullyQualifiedDomainName -o tsv
$env:PGPASSWORD = az keyvault secret show --vault-name kv-chatmed-dev-01 --name pg-admin --query value -o tsv
docker run --rm -e PGPASSWORD chatmed-psql:17 psql "host=$fqdn user=chatmed_admin dbname=postgres sslmode=verify-full sslrootcert=/etc/ssl/certs/ca-certificates.crt" -c "DROP DATABASE tenant_acme WITH (FORCE);"
Remove-Item Env:PGPASSWORD
```

```powershell
.\infra\seed-db.ps1 -Tenants acme
```

Seeding gives the reader role a new password (in Key Vault and in Postgres), and the running
app still holds the old one. Restart its revision so it reads the new one:

```powershell
az containerapp revision restart -g rg-chatmed-dev -n ca-chatmed --revision (az containerapp show -g rg-chatmed-dev -n ca-chatmed --query properties.latestRevisionName -o tsv)
```

If that tenant's questions still fail with `tenant_db.* status=error` in the logs, the Key
Vault reference has not refreshed yet (Container Apps refreshes them periodically): wait a few
minutes and restart again. The same applies after a retried failed seed while the app runs.

### Update (new image)

Commit, then repeat steps 3 and 4. A new revision replaces the old one, and the sessions open
in the old revision are wiped with its process.

### Update the IP

When your public IP changes, this one command updates the app's allow-list:

```powershell
az containerapp ingress access-restriction set -g rg-chatmed-dev -n ca-chatmed --rule-name developer --ip-address "$ip/32" --action Allow --description "Only the developer IP"
```

Only for seeding again do you also need the Postgres firewall:

```powershell
az postgres flexible-server firewall-rule update -g rg-chatmed-dev -n psql-chatmed-dev-01 --rule-name developer --start-ip-address $ip --end-ip-address $ip
```

Pass the new IP to the deploy scripts as well; a redeploy with an old one sets it back.

### Verify (AC13)

1. Set `--min-replicas 1` first ([Before and after a demo](#before-and-after-a-demo)), so
   the run does not include a cold start. The demo flow against the app must pass with a
   verified answer to every question:

   ```powershell
   $url = az containerapp show -g rg-chatmed-dev -n ca-chatmed --query "properties.configuration.ingress.fqdn" -o tsv
   uv run python scripts/smoke_demo.py --base-url "https://$url" --tenant acme --require-verified
   ```

   From any other IP, the same URL answers 403 before the app.

2. The logs contain only metadata events. In the Log Analytics workspace `log-chatmed-dev`
   (portal → Logs), the telemetry lands in `AppTraces` (event name = `Message`, allow-listed
   fields = `Properties`) and the console output in `ContainerAppConsoleLogs_CL`:

   ```kusto
   // Every event name and every field name the app sent: compare with the event names in the
   // code and with logconfig.ALLOWED_FIELDS. No free text, no values beyond ids and counts.
   AppTraces
   | where TimeGenerated > ago(1d)
   | mv-expand field = Properties
   | extend field_name = tostring(bag_keys(field)[0])
   | summarize records = count() by event = Message, field_name
   | order by event asc, field_name asc
   ```

   ```kusto
   // Request telemetry: method + route template + status only (no URL, no client address).
   AppRequests
   | where TimeGenerated > ago(1d)
   | summarize count() by Name, ResultCode, Url, ClientIP
   ```

   ```kusto
   // Console lines: each one a JSON object with an event name, or uvicorn/platform startup.
   ContainerAppConsoleLogs_CL
   | where TimeGenerated > ago(1d) and ContainerAppName_s == "ca-chatmed"
   | project TimeGenerated, Log_s
   | order by TimeGenerated desc
   ```

   Expected: event names such as `session.created`, `pipeline.document_added`, `agent.answer`,
   `http.request`; field names only from `ALLOWED_FIELDS`; `Url` empty and `ClientIP`
   `0.0.0.0` (or empty); never page text, quotes, prompts, questions or answers.

3. The app writes no files. Container Apps cannot mount the root filesystem read-only (D17),
   so check it after the smoke run:

   ```powershell
   az containerapp exec -g rg-chatmed-dev -n ca-chatmed --command "find / -xdev -newer /app -type f"
   ```

   It lists the files changed after the image was built, on the container's own filesystem.
   Expected: nothing written by the app (no PDFs, no temp files, no DuckDB files, no
   `__pycache__`). Record the output with the verification; anything the platform itself
   writes at start (if any) is listed there with its reason.

### Before and after a demo

The app scales to zero by default (`minReplicas` 0, D17): after about 5 minutes without
requests its only replica stops, which wipes every open session, and the next request waits
for a cold start. Before a demo, keep one replica running:

```powershell
az containerapp update -g rg-chatmed-dev -n ca-chatmed --min-replicas 1
```

After the demo, set it back with `--min-replicas 0`. Each change starts a new revision, which
also wipes open sessions. A later `deploy-app.ps1` uses its `-MinReplicas` (default 0).

Start Postgres before the demo, and stop it after (compute is billed only while it runs;
storage always):

```powershell
az postgres flexible-server start -g rg-chatmed-dev -n psql-chatmed-dev-01
```

```powershell
az postgres flexible-server stop -g rg-chatmed-dev -n psql-chatmed-dev-01
```

Azure starts a stopped server again by itself after 7 days, so stop it again each week if the
pause is longer.

The container size is chosen at deploy time with `-SizeProfile`. `demo` (default) is 1 vCPU /
2 GiB with `MAX_SESSIONS=3` and `OCR_MAX_CONCURRENCY=1`. `rated` is 2 vCPU / 4 GiB with 8 and
2, the configuration the measured worst case covers (architecture.md, D16):

```powershell
.\infra\deploy-app.ps1 -Image $image -DeveloperIp $ip -AzureOpenAiEndpoint <endpoint> -AzureOpenAiDeployment <deployment> -SizeProfile rated -MinReplicas 1
```

### Cost and budget

The budget is USD 25/month, with e-mail alerts at 80 % ($20) and 100 % of actual cost. It is
an alert, not a cap.

Estimate per month: Azure list prices for eastus2 from the Azure Retail Prices API (the
calculator's price source), read 2026-11-27. Unit prices and assumptions are in D17.

| Resource | Demo month, `demo` | Demo month, `rated` | Always on, `demo` | Always on, `rated` |
|---|---|---|---|---|
| Container app | $0 (free grant) | $0 (free grant) | ≈ $22.03 | ≈ $45.68 |
| PostgreSQL compute | ≈ $0.41 | ≈ $0.41 | $12.41 | $12.41 |
| PostgreSQL storage 32 GB | $3.68 | $3.68 | $3.68 | $3.68 |
| Container Registry Basic | ≈ $5.07 | ≈ $5.07 | ≈ $5.07 | ≈ $5.07 |
| Key Vault, Log Analytics, requests | < $0.10 | < $0.10 | < $0.10 | < $0.10 |
| **Total** (plus backup storage, at most ≈ $3) | **≈ $9.2** | **≈ $9.2** | **≈ $43.2** | **≈ $66.8** |

*Demo month* means scale to zero, with one replica for about 20 demo hours and Postgres
started for about 24 hours. *Always on* means `minReplicas` 1 and Postgres running all month.
The free grant covers 50 running hours of `demo` or 25 of `rated`; after that, a running hour
costs about $0.11 (`demo`) or $0.22 (`rated`). Leaving Postgres running while the app scales
to zero costs ≈ $21 and fires the 80 % alert.

### Delete

1. Remove the identity's role on the Foundry resource. It lives in the Foundry's resource
   group, so deleting `rg-chatmed-dev` would leave it behind, orphaned:

   ```powershell
   $principal = az identity show -g rg-chatmed-dev -n id-chatmed-app --query principalId -o tsv
   az role assignment delete --assignee $principal --scope (az cognitiveservices account show -g <foundry-rg> -n <foundry-resource> --query id -o tsv)
   ```

2. Delete the resource group (app, registry, Postgres, Key Vault, logs, budget, identity):

   ```powershell
   az group delete -n rg-chatmed-dev --yes
   ```

3. Purge the soft-deleted Key Vault (otherwise it is kept 7 days and its name stays taken):

   ```powershell
   az keyvault purge -n kv-chatmed-dev-01 -l eastus2
   ```

4. Remove the local psql helper image:

   ```powershell
   docker image rm chatmed-psql:17
   ```

5. Delete the local service principal too (next section), if not done yet.

## Clean up

Azure: see [Delete](#delete). Local stack (containers, the database volume and the images):

```powershell
docker compose down -v --rmi local
```

Delete the local service principal and its app registration as soon as the local demo is no
longer needed (do not wait for the secret to expire):

```powershell
az ad app delete --id <appId>
```

Deleting the app registration also deletes its service principal and its role assignment.
Check that nothing is left:

```powershell
az ad sp list --display-name chatmed-local --query "[].appId" -o tsv
```

Then remove the `AZURE_*` lines from your `.env`.
