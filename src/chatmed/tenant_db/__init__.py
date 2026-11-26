"""Tenant database access: read-only, bound to the session's tenant (D8, D13a, rule 5)."""

from chatmed.tenant_db.repository import (
    PostgresTenantRepository,
    TenantNotAllowed,
    TenantRepository,
    UnknownDocumentType,
    tenant_dsn,
)

__all__ = [
    "PostgresTenantRepository",
    "TenantNotAllowed",
    "TenantRepository",
    "UnknownDocumentType",
    "tenant_dsn",
]
