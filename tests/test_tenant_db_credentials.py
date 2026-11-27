"""M5b AC9: each tenant's read-only role connects with its own password (D17).

In Azure the passwords come from Key Vault as `PGPASSWORD_<TENANT>`; locally the shared
`PGPASSWORD` keeps working. A missing password stops the app at startup, and no password
ever shows up in an error, a repr or the DSN. No database is needed: the connect call is
replaced by one that records its arguments.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from chatmed import pipeline
from chatmed.tenant_db import MissingTenantPassword, PostgresTenantRepository, tenant_db_passwords
from chatmed.tenant_db.credentials import password_var
from tests.test_extraction_m2 import make_settings

ACME_PW = "acme-secret-Value-1"
GLOBEX_PW = "globex-secret-Value-2"
SHARED_PW = "shared-local-Value-3"


class _Stop(Exception):
    """Raised by the fake connect once it has seen its arguments."""


def _connect_kwargs(monkeypatch: pytest.MonkeyPatch, repo: PostgresTenantRepository) -> dict:
    """Arguments the repository passes to psycopg's connect for one query."""
    import psycopg

    seen: dict[str, Any] = {}

    async def fake_connect(dsn: str, **kwargs: Any) -> Any:
        seen["dsn"] = dsn
        seen.update(kwargs)
        raise _Stop

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", fake_connect)
    with pytest.raises(_Stop):
        asyncio.run(repo.subject_ids())
    return seen


def test_password_variable_is_named_after_the_tenant() -> None:
    assert password_var("acme") == "PGPASSWORD_ACME"
    assert password_var("tenant_2") == "PGPASSWORD_TENANT_2"


def test_each_tenant_gets_its_own_password() -> None:
    env = {"PGPASSWORD_ACME": ACME_PW, "PGPASSWORD_GLOBEX": GLOBEX_PW, "PGPASSWORD": SHARED_PW}
    passwords = tenant_db_passwords(make_settings(), env)
    assert {t: p.get_secret_value() for t, p in passwords.items()} == {
        "acme": ACME_PW,
        "globex": GLOBEX_PW,
    }


def test_shared_pgpassword_still_works_locally() -> None:
    passwords = tenant_db_passwords(make_settings(), {"PGPASSWORD": SHARED_PW})
    assert {p.get_secret_value() for p in passwords.values()} == {SHARED_PW}
    mixed = tenant_db_passwords(
        make_settings(), {"PGPASSWORD_ACME": ACME_PW, "PGPASSWORD": SHARED_PW}
    )
    assert mixed["acme"].get_secret_value() == ACME_PW
    assert mixed["globex"].get_secret_value() == SHARED_PW


@pytest.mark.parametrize("env", [{}, {"PGPASSWORD_ACME": ACME_PW}, {"PGPASSWORD_GLOBEX": ""}])
def test_missing_tenant_password_names_the_variable_and_no_value(env: dict[str, str]) -> None:
    with pytest.raises(MissingTenantPassword) as info:
        tenant_db_passwords(make_settings(), env)
    message = str(info.value)
    assert "PGPASSWORD_GLOBEX" in message
    assert ACME_PW not in message


def test_missing_tenant_password_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_services is what the app's lifespan runs before serving anything."""
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.setenv("PGPASSWORD_ACME", ACME_PW)
    monkeypatch.delenv("PGPASSWORD_GLOBEX", raising=False)
    with pytest.raises(MissingTenantPassword, match="PGPASSWORD_GLOBEX"):
        asyncio.run(pipeline.build_services(make_settings()))


def test_startup_wires_each_tenant_password_into_its_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.setenv("PGPASSWORD_ACME", ACME_PW)
    monkeypatch.setenv("PGPASSWORD_GLOBEX", GLOBEX_PW)

    async def no_parser(_settings: Any) -> object:
        return object()

    monkeypatch.setattr(pipeline, "build_parser", no_parser)
    monkeypatch.setattr(pipeline, "make_client", lambda *_args: object())
    services = asyncio.run(pipeline.build_services(make_settings()))
    for tenant, password in (("acme", ACME_PW), ("globex", GLOBEX_PW)):
        repo = services.repo_factory(tenant)
        assert isinstance(repo, PostgresTenantRepository)
        seen = _connect_kwargs(monkeypatch, repo)
        assert seen["password"] == password
        assert password not in seen["dsn"]  # a keyword argument, never in the DSN
        assert f"tenant_{tenant}" in seen["dsn"]


def test_without_a_password_libpq_decides(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local tests build repositories without one; libpq then reads PGPASSWORD."""
    seen = _connect_kwargs(monkeypatch, PostgresTenantRepository(make_settings(), "acme"))
    assert "password" not in seen


def test_passwords_never_show_in_a_repr() -> None:
    env = {"PGPASSWORD_ACME": ACME_PW, "PGPASSWORD_GLOBEX": GLOBEX_PW}
    passwords = tenant_db_passwords(make_settings(), env)
    repo = PostgresTenantRepository(make_settings(), "acme", passwords["acme"])
    for text in (repr(passwords), str(passwords), repr(vars(repo))):
        assert ACME_PW not in text and GLOBEX_PW not in text
