"""M2 AC2: field metadata and subject ids come from the tenant DB, bound to the session's tenant.

Unit tests need nothing. The Postgres tests use the local tenant DBs (`docker compose up -d`)
and are skipped with a reason when they cannot connect. Locally the reader password comes from
the environment, e.g.: uv run --env-file .env --env-file db/.env pytest tests/test_tenant_db.py
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest
from pydantic import ValidationError

from chatmed.config import Settings
from chatmed.extraction import DocTypeSchema
from chatmed.tenant_db import (
    PostgresTenantRepository,
    TenantNotAllowed,
    UnknownDocumentType,
    tenant_dsn,
)
from tests.fakes import ACME_CIOMS_ROWS, GLOBEX_CIOMS_ROWS, SUBJIDS_BY_TENANT
from tests.test_extraction_m2 import make_settings

TEMPLATE = "postgresql://chatmed_reader_{tenant}@localhost:5432/tenant_{tenant}"


# ---------------------------------------------------------------- tenant allow-list (D13a)


def test_allowed_tenant_fills_the_template() -> None:
    settings = make_settings(tenant_db_dsn_template=TEMPLATE)
    assert tenant_dsn(settings, "acme") == (
        "postgresql://chatmed_reader_acme@localhost:5432/tenant_acme"
    )


@pytest.mark.parametrize(
    "tenant",
    ["acme dbname=postgres", "acme;drop", "ACME", "acme/../globex", "", "initech", "acme\n"],
)
def test_malformed_or_unlisted_tenant_is_rejected(tenant: str) -> None:
    settings = make_settings(tenant_db_dsn_template=TEMPLATE)
    with pytest.raises(TenantNotAllowed) as info:
        tenant_dsn(settings, tenant)
    assert tenant.strip() == "" or tenant.strip() not in str(info.value)  # value not echoed


def test_repository_rejects_tenant_before_connecting() -> None:
    with pytest.raises(TenantNotAllowed):
        PostgresTenantRepository(make_settings(), "acme dbname=postgres")


@pytest.mark.parametrize("tenants", ["acme,Bad Tenant", "acme,glo-bex", ",", ""])
def test_tenants_setting_must_be_plain_identifiers(tenants: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(tenants=tenants)


def test_tenants_setting_is_a_set() -> None:
    assert make_settings(tenants=" acme , globex ").tenant_ids == {"acme", "globex"}


# ---------------------------------------------------------------- real tenant databases


def run_db(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run on a selector loop: psycopg's async driver cannot use Windows' Proactor loop."""
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(scope="module")
def db_settings() -> Settings:
    import psycopg

    async def probe(settings: Settings) -> None:
        dsn = tenant_dsn(settings, "acme")
        async with await psycopg.AsyncConnection.connect(dsn, connect_timeout=3):
            pass

    try:
        settings = Settings()  # type: ignore[call-arg]  # local .env / environment
        run_db(probe(settings))
    except (ValidationError, psycopg.OperationalError, TenantNotAllowed) as exc:
        pytest.skip(f"local tenant DB not reachable ({type(exc).__name__})")
    return settings


def test_acme_field_metadata_matches_seed(db_settings: Settings) -> None:
    schema = run_db(PostgresTenantRepository(db_settings, "acme").field_schema("cioms"))
    assert schema == DocTypeSchema.from_rows("acme", "cioms", ACME_CIOMS_ROWS)
    assert {f.key for f in schema.fields if f.checkbox} == {
        "patient_died", "hospitalisation", "disability", "life_threatening",
        "dechallenge", "rechallenge", "report_type",
    }  # fmt: skip


def test_globex_has_its_own_fields(db_settings: Settings) -> None:
    schema = run_db(PostgresTenantRepository(db_settings, "globex").field_schema("cioms"))
    assert schema == DocTypeSchema.from_rows("globex", "cioms", GLOBEX_CIOMS_ROWS)


def test_subject_ids_come_from_the_tenants_adsl(db_settings: Settings) -> None:
    for tenant in ("acme", "globex"):
        repo = PostgresTenantRepository(db_settings, tenant)
        assert run_db(repo.subject_ids()) == SUBJIDS_BY_TENANT[tenant]


def test_unknown_document_type(db_settings: Settings) -> None:
    with pytest.raises(UnknownDocumentType):
        run_db(PostgresTenantRepository(db_settings, "acme").field_schema("lab_report"))


def test_connections_are_read_only(db_settings: Settings) -> None:
    import psycopg

    repo = PostgresTenantRepository(db_settings, "acme")
    with pytest.raises(psycopg.Error):
        run_db(repo._fetch("probe", "UPDATE adsl SET age = age WHERE false", {}))


def test_acme_role_cannot_open_globex_database(db_settings: Settings) -> None:
    import psycopg

    acme_role_globex_db = db_settings.tenant_db_dsn_template.replace("{tenant}", "acme", 1)
    acme_role_globex_db = acme_role_globex_db.replace("{tenant}", "globex", 1)

    async def connect() -> None:
        async with await psycopg.AsyncConnection.connect(acme_role_globex_db, connect_timeout=3):
            pass

    with pytest.raises(psycopg.OperationalError):
        run_db(connect())


def test_checkbox_fields_must_have_box_labels(db_settings: Settings) -> None:
    """D13b: the schema itself refuses a checkbox field without a printed label."""
    sql = (
        "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE conrelid = 'field_definitions'::regclass AND contype = 'c'"
    )
    for tenant in ("acme", "globex"):
        rows = run_db(PostgresTenantRepository(db_settings, tenant)._fetch("probe", sql, {}))
        assert any("box_labels" in r["def"] and "checkbox" in r["def"] for r in rows)
