"""Read-only access to one tenant's database (D8, hard rule 5).

A repository is bound to one tenant when it is built, from the session's tenant, never from
model output. The tenant is checked against the `TENANTS` allow-list and a strict pattern
before it is put into `TENANT_DB_DSN_TEMPLATE` (D13a), so a malformed value cannot change the
connection string. Queries are fixed and parameterized; connections are read-only on top of
the read-only role.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

from chatmed.config import TENANT_ID, TENANT_PLACEHOLDER, Settings
from chatmed.extraction import DocTypeSchema

log = logging.getLogger(__name__)

FIELD_DEFINITIONS_SQL = """
SELECT fd.key, fd.label, fd.description, fd.data_type, fd.required, fd.multiple,
       fd.allowed_values, fd.unit, fd.join_target, fd.checkbox,
       fd.box_labels
FROM field_definitions fd
JOIN document_types dt ON dt.id = fd.document_type_id
WHERE dt.code = %(doc_type)s AND dt.active AND fd.active
ORDER BY fd.key
"""
SUBJECT_IDS_SQL = "SELECT subjid FROM adsl ORDER BY subjid"


class TenantNotAllowed(ValueError):
    """The tenant is not in TENANTS or not a plain identifier."""


class UnknownDocumentType(LookupError):
    """The tenant has no active document type with this code (or it has no active fields)."""


class TenantRepository(Protocol):
    """What extraction needs from the tenant DB. Bound to one tenant; no tenant parameter."""

    tenant_id: str

    async def field_schema(self, doc_type: str) -> DocTypeSchema: ...

    async def subject_ids(self) -> list[str]: ...


def tenant_dsn(settings: Settings, tenant_id: str) -> str:
    """Connection string for `tenant_id`, after the allow-list and pattern checks (D13a)."""
    if not TENANT_ID.fullmatch(tenant_id) or tenant_id not in settings.tenant_ids:
        raise TenantNotAllowed("tenant is not allowed")  # value not echoed: it may be anything
    return settings.tenant_db_dsn_template.replace(TENANT_PLACEHOLDER, tenant_id)


class PostgresTenantRepository:
    """Async psycopg, one short read-only connection per call (pooling can come in M3)."""

    def __init__(self, settings: Settings, tenant_id: str) -> None:
        self._dsn = tenant_dsn(settings, tenant_id)
        self.tenant_id = tenant_id

    async def field_schema(self, doc_type: str) -> DocTypeSchema:
        rows = await self._fetch("field_definitions", FIELD_DEFINITIONS_SQL, {"doc_type": doc_type})
        if not rows:
            raise UnknownDocumentType("no active fields for this document type")
        return DocTypeSchema.from_rows(self.tenant_id, doc_type, rows)

    async def subject_ids(self) -> list[str]:
        rows = await self._fetch("subject_ids", SUBJECT_IDS_SQL, {})
        return [row["subjid"] for row in rows]

    async def _fetch(self, query: str, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        import psycopg
        from psycopg.rows import dict_row

        start = time.perf_counter()
        status = "error"
        try:
            async with await psycopg.AsyncConnection.connect(
                self._dsn, row_factory=dict_row
            ) as conn:
                await conn.set_read_only(True)
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    rows = await cur.fetchall()
            status = "ok"
            return rows
        finally:
            log.info(
                f"tenant_db.{query}",
                extra={
                    "tenant_id": self.tenant_id,
                    "status": status,
                    "duration_ms": round((time.perf_counter() - start) * 1000),
                },
            )
