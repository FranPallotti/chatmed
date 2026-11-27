# Deploy and clean up

M5a covers the local container stack. The Azure deployment (Bicep, Container Apps, Key Vault,
PostgreSQL, budget alert, log checks) is added in M5b.

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

## Clean up

Local stack (containers, the database volume and the images):

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
