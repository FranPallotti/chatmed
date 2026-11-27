"""Which password each tenant's read-only role uses (M5b AC9, D17).

In Azure every tenant reader has its own random password, kept in Key Vault and given to the
app as `PGPASSWORD_<TENANT>` (a Container Apps secret that references Key Vault through the
managed identity). Locally the shared `PGPASSWORD` from `db/.env` keeps working. The password
is picked by the session's tenant, the same way the role and the database are (rule 5).

The passwords of all allowed tenants are read once, at startup, so a deployment that lacks
one fails before it serves anyone instead of at that tenant's first question. Values are
`SecretStr` and are never logged or put in an error message; errors name the environment
variable only.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import SecretStr

from chatmed.config import Settings

SHARED_PASSWORD_VAR = "PGPASSWORD"  # local development: one password for every reader role


class MissingTenantPassword(RuntimeError):
    """A tenant in TENANTS has no reader password in the environment."""


def password_var(tenant_id: str) -> str:
    """Environment variable holding `tenant_id`'s reader password, e.g. PGPASSWORD_ACME."""
    return f"{SHARED_PASSWORD_VAR}_{tenant_id.upper()}"


def tenant_db_passwords(
    settings: Settings, environ: Mapping[str, str] | None = None
) -> dict[str, SecretStr]:
    """Reader password per allowed tenant: `PGPASSWORD_<TENANT>`, else the shared
    `PGPASSWORD`. Raises `MissingTenantPassword` naming every variable that is missing."""
    env = os.environ if environ is None else environ
    shared = env.get(SHARED_PASSWORD_VAR) or None
    passwords: dict[str, SecretStr] = {}
    missing: list[str] = []
    for tenant in sorted(settings.tenant_ids):
        value = env.get(password_var(tenant)) or shared
        if value is None:
            missing.append(password_var(tenant))
        else:
            passwords[tenant] = SecretStr(value)
    if missing:
        raise MissingTenantPassword(
            f"no tenant DB password: set {', '.join(missing)} "
            f"(or {SHARED_PASSWORD_VAR} for local development)"
        )
    return passwords
