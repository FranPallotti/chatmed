"""Tenant database access: read-only, bound to the session's tenant (D8, D13a, rule 5)."""

from chatmed.tenant_db.credentials import MissingTenantPassword, tenant_db_passwords
from chatmed.tenant_db.repository import (
    PostgresTenantRepository,
    TenantNotAllowed,
    TenantRepository,
    UnknownDocumentType,
    tenant_dsn,
)

__all__ = [
    "MissingTenantPassword",
    "PostgresTenantRepository",
    "TenantNotAllowed",
    "TenantRepository",
    "UnknownDocumentType",
    "tenant_db_passwords",
    "tenant_dsn",
]
