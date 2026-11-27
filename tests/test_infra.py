"""M5b AC6-AC12: what the Azure templates and deploy scripts promise, checked without Azure.

The Bicep files are compiled with the local Bicep CLI (installed by `az bicep install`) and
the ARM JSON is checked: resources and tiers, the identity's exact roles, no registry admin,
one always-on replica, HTTPS-only ingress behind an IP allow-list, TLS on Postgres, Key Vault
references for the tenant passwords, no secret in any output or parameter default. Without the
Bicep CLI those tests skip with a reason (check.py prints it). The PowerShell scripts are
checked as text for the secret-handling rules of AC9-AC11. Nothing here calls Azure.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
INFRA = ROOT / "infra"

ROLE_ACR_PULL = "7f951dda-4ed3-4680-a7ca-43fe172d538d"
ROLE_KV_SECRETS_USER = "4633458b-17de-408a-b874-0445c86b69e6"
ROLE_KV_SECRETS_OFFICER = "b86a8fe4-44ce-4948-aee5-eccb2c155cd7"
ROLE_OPENAI_USER = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
SECRET_WORDS = re.compile(r"password|secret|sharedkey|apikey|connectionstring|token", re.I)


def _bicep() -> str | None:
    found = shutil.which("bicep")
    if found:
        return found
    for name in ("bicep.exe", "bicep"):
        candidate = Path.home() / ".azure" / "bin" / name
        if candidate.exists():
            return str(candidate)
    return None


@cache
def compiled(name: str) -> dict[str, Any]:
    """ARM JSON of infra/<name>.bicep (modules inlined as nested deployments)."""
    bicep = _bicep()
    if bicep is None:
        pytest.skip("Bicep CLI not installed (az bicep install)")
    result = subprocess.run(
        [bicep, "build", str(INFRA / f"{name}.bicep"), "--stdout"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    template: dict[str, Any] = json.loads(result.stdout)
    return template


def resources(template: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every resource, including those of nested (module) deployments."""
    for res in template["resources"]:
        yield res
        if res["type"] == "Microsoft.Resources/deployments":
            yield from resources(res["properties"]["template"])


def one(template: dict[str, Any], rtype: str) -> dict[str, Any]:
    found = [r for r in resources(template) if r["type"] == rtype]
    assert len(found) == 1, f"{rtype}: {len(found)}"
    return found[0]


def script(name: str) -> str:
    return (INFRA / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- AC6 resources


def test_base_has_every_resource_with_the_chosen_tiers() -> None:
    base = compiled("base")
    registry = one(base, "Microsoft.ContainerRegistry/registries")
    assert registry["sku"]["name"] == "Basic"
    assert registry["properties"]["adminUserEnabled"] is False

    insights = one(base, "Microsoft.Insights/components")
    assert "workspaces" in insights["properties"]["WorkspaceResourceId"]  # workspace-based

    vault = one(base, "Microsoft.KeyVault/vaults")
    assert vault["properties"]["enableRbacAuthorization"] is True

    one(base, "Microsoft.ManagedIdentity/userAssignedIdentities")

    env = one(base, "Microsoft.App/managedEnvironments")
    assert "workloadProfiles" not in env["properties"]  # Consumption-only

    pg = one(base, "Microsoft.DBforPostgreSQL/flexibleServers")
    assert pg["sku"]["tier"] == "Burstable"
    assert base["parameters"]["postgresSkuName"]["defaultValue"] == "Standard_B1ms"
    assert base["parameters"]["postgresVersion"]["defaultValue"] == "17"


def test_app_is_at_most_one_replica_scaling_to_zero_by_default() -> None:
    template = compiled("app")
    app = one(template, "Microsoft.App/containerApps")
    scale = app["properties"]["template"]["scale"]
    assert scale == {"minReplicas": "[parameters('minReplicas')]", "maxReplicas": 1}  # D5
    min_replicas = template["parameters"]["minReplicas"]
    assert (min_replicas["defaultValue"], min_replicas["minValue"], min_replicas["maxValue"]) == (
        0,
        0,
        1,
    )
    (container,) = app["properties"]["template"]["containers"]
    assert container["image"] == "[parameters('image')]"
    assert app["identity"]["type"] == "UserAssigned"


def test_size_profiles_set_container_size_and_matching_limits() -> None:
    template = compiled("app")
    assert template["parameters"]["sizeProfile"]["defaultValue"] == "demo"
    assert template["parameters"]["sizeProfile"]["allowedValues"] == ["demo", "rated"]
    assert template["variables"]["profiles"] == {
        "demo": {"cpu": "1.0", "memory": "2Gi", "maxSessions": "3", "ocrMaxConcurrency": "1"},
        "rated": {"cpu": "2.0", "memory": "4Gi", "maxSessions": "8", "ocrMaxConcurrency": "2"},
    }
    (container,) = one(template, "Microsoft.App/containerApps")["properties"]["template"][
        "containers"
    ]
    assert container["resources"] == {
        "cpu": "[json(variables('size').cpu)]",
        "memory": "[variables('size').memory]",
    }
    env = container["env"]  # one concat() expression: the fixed entries + one per tenant
    assert "createObject('name', 'MAX_SESSIONS', 'value', variables('size').maxSessions)" in env
    assert (
        "createObject('name', 'OCR_MAX_CONCURRENCY', 'value', variables('size').ocrMaxConcurrency)"
        in env
    )


# ---------------------------------------------------------------- AC7 least privilege


def test_identity_gets_exactly_three_roles_and_the_developer_one() -> None:
    # Role ids are Bicep variables, resolved through the variables of each (module) template.
    assigned: list[tuple[str, str]] = []
    for template in _templates(compiled("base")):
        for res in template["resources"]:
            if res["type"].endswith("/roleAssignments"):
                role = _resolve_role(res["properties"]["roleDefinitionId"], template)
                assigned.append((role, res["properties"]["principalType"]))
    assert sorted(assigned) == sorted(
        [
            (ROLE_ACR_PULL, "ServicePrincipal"),
            (ROLE_KV_SECRETS_USER, "ServicePrincipal"),
            (ROLE_OPENAI_USER, "ServicePrincipal"),
            (ROLE_KV_SECRETS_OFFICER, "User"),
        ]
    )


def _templates(template: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield template
    for res in template["resources"]:
        if res["type"] == "Microsoft.Resources/deployments":
            yield from _templates(res["properties"]["template"])


def _resolve_role(expression: str, template: dict[str, Any]) -> str:
    match = re.search(r"variables\('(\w+)'\)", expression)
    value = template["variables"][match.group(1)] if match else expression
    found = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value)
    assert found, expression
    return found.group(0)


def test_no_registry_admin_and_no_registry_password_in_the_app() -> None:
    app = one(compiled("app"), "Microsoft.App/containerApps")
    (registry,) = app["properties"]["configuration"]["registries"]
    assert set(registry) == {"server", "identity"}  # no username / passwordSecretRef


# ---------------------------------------------------------------- AC8 database networking


def test_postgres_requires_tls_and_allows_only_azure_and_the_developer() -> None:
    base = compiled("base")
    rules = {
        r["name"]: r["properties"]
        for r in resources(base)
        if r["type"].endswith("flexibleServers/firewallRules")
    }
    names = {re.search(r"'([^']+)'\)\]$", name).group(1) for name in rules}  # type: ignore[union-attr]
    assert names == {"AllowAllAzureServicesAndResourcesWithinAzureIps", "developer"}
    ranges = {(p["startIpAddress"], p["endIpAddress"]) for p in rules.values()}
    assert ranges == {
        ("0.0.0.0", "0.0.0.0"),
        ("[parameters('developerIp')]", "[parameters('developerIp')]"),
    }
    (tls,) = [r for r in resources(base) if r["type"].endswith("flexibleServers/configurations")]
    assert "require_secure_transport" in tls["name"]
    assert tls["properties"]["value"] == "ON"


# ---------------------------------------------------------------- AC9 per-tenant passwords


def test_each_tenant_password_is_a_key_vault_reference_through_the_identity() -> None:
    app = compiled("app")
    secrets = app["variables"]["copy"]
    by_name = {c["name"]: c["input"] for c in secrets}
    reader = by_name["readerSecrets"]
    assert "pg-reader-" in reader["name"] and "secrets/pg-reader-" in reader["keyVaultUrl"]
    assert "userAssignedIdentities" in reader["identity"]
    assert "value" not in reader  # never the password itself
    env = by_name["readerEnv"]
    assert "PGPASSWORD_{0}" in env["name"] and "toUpper" in env["name"]
    assert "pg-reader-" in env["secretRef"]


def test_dsn_verifies_the_server_certificate_by_default() -> None:
    app = compiled("app")
    assert app["parameters"]["postgresSslMode"]["defaultValue"] == "verify-full"
    assert "sslrootcert=/etc/ssl/certs/ca-certificates.crt" in json.dumps(app["variables"])


# ---------------------------------------------------------------- AC12 access


def test_ingress_is_https_only_behind_the_developer_ip() -> None:
    app = one(compiled("app"), "Microsoft.App/containerApps")
    ingress = app["properties"]["configuration"]["ingress"]
    assert ingress["external"] is True
    assert ingress["allowInsecure"] is False
    (rule,) = ingress["ipSecurityRestrictions"]
    assert rule["action"] == "Allow"  # with an Allow rule, every other address is denied
    assert rule["ipAddressRange"] == "[format('{0}/32', parameters('developerIp'))]"


# ---------------------------------------------------------------- nothing secret anywhere


@pytest.mark.parametrize("name", ["base", "app", "budget", "modules/foundry-role"])
def test_no_secret_in_outputs_or_parameter_defaults(name: str) -> None:
    template = compiled(name)
    for key, output in template.get("outputs", {}).items():
        assert not SECRET_WORDS.search(key), key
        assert "listKeys" not in json.dumps(output) and "ConnectionString" not in json.dumps(
            output
        ), key
    for key, param in template["parameters"].items():
        if param["type"].lower() == "securestring":
            # Only a value generated at deploy time, never a literal.
            assert param.get("defaultValue") in (None, "[newGuid()]"), key
        else:
            assert not re.search(r"password|secret", key, re.I), f"{key} must be @secure()"


def test_admin_password_is_secure_and_goes_to_key_vault() -> None:
    base = compiled("base")
    param = base["parameters"]["postgresAdminPassword"]
    assert param["type"] == "securestring" and param["defaultValue"] == "[newGuid()]"
    secret = one(base, "Microsoft.KeyVault/vaults/secrets")
    assert secret["properties"]["value"] == "[parameters('postgresAdminPassword')]"


def test_budget_alerts_at_80_and_100_percent_of_25_usd() -> None:
    budget = one(compiled("budget"), "Microsoft.Consumption/budgets")
    assert compiled("budget")["parameters"]["amount"]["defaultValue"] == 25
    thresholds = {n["threshold"] for n in budget["properties"]["notifications"].values()}
    assert thresholds == {80, 100}


@pytest.mark.parametrize("name", ["base.bicepparam", "app.bicepparam"])
def test_params_files_hold_names_only(name: str) -> None:
    text = (INFRA / name).read_text(encoding="utf-8")
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text), "no IP addresses"
    assert "@" not in text.replace("@sha256", ""), "no e-mail addresses"
    for line in text.splitlines():
        if line.startswith("param ") and SECRET_WORDS.search(line.split("=")[0]):
            pytest.fail(f"secret-looking parameter in {name}: {line}")


# ---------------------------------------------------------------- scripts (AC9-AC11)


def test_seed_script_never_puts_a_secret_on_a_command_line_or_screen() -> None:
    text = script("seed-db.ps1")
    # --value would put the password on a command line (only the docstring mentions it)
    assert not re.search(r"^\s*az keyvault secret set", text, re.M)
    assert "Invoke-RestMethod -Method Put" in text
    # docker copies these from the script's environment: named, never valued.
    for var in ("PGPASSWORD", "READER_PW"):
        assert f"'-e', '{var}'" in text
        assert not re.search(rf"-e\W+{var}=", text)
    assert r"\getenv pw READER_PW" in text
    assert "VERBOSITY=terse" in text  # psql errors without the statement text
    for line in text.splitlines():
        if re.match(r"\s*(Write-Host|Write-Output)", line):
            assert not re.search(r"READER_PW|PGPASSWORD|\$token|adminPassword", line), line


def test_seed_script_stops_for_an_already_seeded_tenant_and_reads_admin_from_key_vault() -> None:
    text = script("seed-db.ps1")
    assert "already seeded, nothing changed" in text
    assert "--name pg-admin" in text
    assert "'-1'" in text  # schema + seed in one transaction


def test_build_script_builds_committed_code_and_returns_a_digest() -> None:
    text = script("build-image.ps1")
    assert "status --porcelain" in text and "uncommitted changes" in text
    assert "archive --format=tar" in text  # only committed files reach the image
    assert "az acr build" in text
    assert "sha256:[0-9a-f]{64}" in text


def test_app_deploy_accepts_only_a_digest() -> None:
    text = script("deploy-app.ps1")
    assert r"chatmed@sha256:[0-9a-f]{64}$" in text
    assert "the app does not run the requested image digest" in text
