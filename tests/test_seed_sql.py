"""M5b AC10: the Azure seeding SQL (db/azure/) against a real PostgreSQL 17 (D17).

A throwaway `postgres:17` container is started for this module and removed afterwards; the
developer's database is never touched. It runs the same SQL files `infra/seed-db.ps1` runs,
in the same order and with the same decision ("seeded" = the `seed_info` marker exists):

- a seeded tenant is a no-op on rerun: no new password, no changed rows, no second marker;
- a run that fails inside the seed leaves no marker and no tables, and the rerun seeds it;
- each reader role reads its own tenant database only;
- the reader password never shows up in psql's output, even when a statement fails.

Skipped with a reason when Docker is not running or `postgres:17` is not pulled (tests never
download images). Synthetic seed data only.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "postgres:17"
ADMIN = "chatmed_admin"
ADMIN_PW = secrets.token_hex(16)  # this container only
DOCKER = shutil.which("docker")


class SeedFailed(Exception):
    """seed_tenant.sql exited non-zero (carries psql's terse error, synthetic data only)."""


def _docker_ok() -> str | None:
    """Why the tests cannot run, or None."""
    if DOCKER is None:
        return "docker not installed"
    if subprocess.run([DOCKER, "info"], capture_output=True, check=False).returncode != 0:
        return "docker is not running"
    inspect = [DOCKER, "image", "inspect", IMAGE]
    if subprocess.run(inspect, capture_output=True, check=False).returncode != 0:
        return f"{IMAGE} not pulled (docker pull {IMAGE})"
    return None


@pytest.fixture(scope="module")
def pg() -> Iterator[str]:
    """Name of a fresh postgres:17 container with db/ mounted read-only at /db."""
    reason = _docker_ok()
    if reason:
        pytest.skip(reason)
    assert DOCKER is not None
    name = f"chatmed-seedtest-{uuid.uuid4().hex[:8]}"
    env = os.environ | {"POSTGRES_PASSWORD": ADMIN_PW}
    subprocess.run(
        [DOCKER, "run", "-d", "--rm", "--name", name, "-e", f"POSTGRES_USER={ADMIN}",
         "-e", "POSTGRES_PASSWORD", "-v", f"{ROOT / 'db'}:/db:ro", IMAGE],
        env=env, capture_output=True, check=True,
    )  # fmt: skip
    try:
        # Over TCP only once the final server is up (the init server listens on a socket).
        ready = [DOCKER, "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", ADMIN, "-q"]
        for _ in range(120):
            if subprocess.run(ready, capture_output=True, check=False).returncode == 0:
                break
            time.sleep(0.5)
        else:
            pytest.fail("postgres:17 did not become ready")
        yield name
    finally:
        subprocess.run([DOCKER, "rm", "-f", name], capture_output=True, check=False)


def psql(
    pg: str,
    db: str,
    *args: str,
    sql: str = "",
    user: str = ADMIN,
    password: str = ADMIN_PW,
    reader_pw: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """psql inside the container, with the flags seed-db.ps1 uses. Passwords travel as
    environment variables named (not valued) on the docker command line, as in the script."""
    assert DOCKER is not None
    env = os.environ | {"PGPASSWORD": password}
    passthrough = ["-e", "PGPASSWORD"]
    if reader_pw is not None:
        env["READER_PW"] = reader_pw
        passthrough += ["-e", "READER_PW"]
    cmd = [DOCKER, "exec", "-i", *passthrough, pg,
           "psql", "-X", "-q", "-h", "127.0.0.1", "-U", user, "-d", db,
           "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=terse", "-v", "SHOW_CONTEXT=never",
           *args]  # fmt: skip
    return subprocess.run(
        cmd, input=sql, capture_output=True, text=True, env=env, timeout=60, check=False
    )


def scalar(pg: str, db: str, sql: str) -> str:
    result = psql(pg, db, "-tA", sql=sql)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def is_seeded(pg: str, tenant: str) -> bool:
    """seed-db.ps1's check: the database exists and holds the seed_info marker."""
    db = f"tenant_{tenant}"
    exists = psql(pg, "postgres", "-tA", "-v", f"db={db}",
                  sql="SELECT 1 FROM pg_database WHERE datname = :'db';")  # fmt: skip
    if exists.stdout.strip() != "1":
        return False
    return scalar_file(pg, db, "/db/azure/is_seeded.sql") == "t"


def scalar_file(pg: str, db: str, path: str) -> str:
    result = psql(pg, db, "-tA", "-f", path)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def role_and_database(pg: str, tenant: str, password: str) -> subprocess.CompletedProcess[str]:
    return psql(pg, "postgres", "-v", f"db=tenant_{tenant}", "-v", f"role=chatmed_reader_{tenant}",
                "-f", "/db/azure/tenant_role_and_database.sql", reader_pw=password)  # fmt: skip


def seed(pg: str, tenant: str, seedfile: str | None = None) -> str | None:
    """seed-db.ps1's steps for one tenant. Returns the new reader password, or None when the
    tenant was already seeded (nothing done). Raises SeedFailed if step 2 fails."""
    if is_seeded(pg, tenant):
        return None
    password = secrets.token_hex(32)
    step1 = role_and_database(pg, tenant, password)
    assert step1.returncode == 0, step1.stderr
    assert password not in step1.stdout + step1.stderr
    step2 = psql(pg, f"tenant_{tenant}", "-v", f"reader=chatmed_reader_{tenant}",
                 "-v", f"seedfile={seedfile or f'/db/seed/tenant_{tenant}.sql'}",
                 "-f", "/db/azure/seed_tenant.sql")  # fmt: skip
    if step2.returncode != 0:
        assert password not in step2.stdout + step2.stderr
        raise SeedFailed(step2.stderr)
    return password


def public_tables(pg: str, db: str) -> int:
    return int(scalar(pg, db, "SELECT count(*) FROM pg_tables WHERE schemaname = 'public';"))


def role_password_hash(pg: str, tenant: str) -> str:
    sql = f"SELECT rolpassword FROM pg_authid WHERE rolname = 'chatmed_reader_{tenant}';"
    return scalar(pg, "postgres", sql)


# ---------------------------------------------------------------- tests


def test_seeding_then_rerun_is_a_no_op(pg: str) -> None:
    assert seed(pg, "acme") is not None
    assert is_seeded(pg, "acme")
    before = (
        scalar(pg, "tenant_acme", "SELECT count(*) FROM field_definitions;"),
        scalar(pg, "tenant_acme", "SELECT count(*) FROM adsl;"),
        scalar(pg, "tenant_acme", "SELECT count(*) FROM seed_info;"),
        role_password_hash(pg, "acme"),
    )
    assert int(before[0]) > 0 and before[2] == "1"

    assert seed(pg, "acme") is None  # the script's whole procedure: nothing generated

    # And step 2 on its own, run again on purpose, changes nothing either.
    again = psql(
        pg, "tenant_acme", "-v", "reader=chatmed_reader_acme",
        "-v", "seedfile=/db/seed/tenant_acme.sql", "-f", "/db/azure/seed_tenant.sql",
    )  # fmt: skip
    assert again.returncode == 0, again.stderr
    assert "already seeded" in again.stdout
    after = (
        scalar(pg, "tenant_acme", "SELECT count(*) FROM field_definitions;"),
        scalar(pg, "tenant_acme", "SELECT count(*) FROM adsl;"),
        scalar(pg, "tenant_acme", "SELECT count(*) FROM seed_info;"),
        role_password_hash(pg, "acme"),
    )
    assert after == before


def test_failed_seed_leaves_no_marker_and_the_rerun_seeds(pg: str) -> None:
    # Fails inside the transaction, after the schema: the seed file does not exist.
    with pytest.raises(SeedFailed, match="does_not_exist.sql: No such file"):
        seed(pg, "globex", seedfile="/db/seed/does_not_exist.sql")
    databases = "SELECT count(*) FROM pg_database WHERE datname = 'tenant_globex';"
    assert scalar(pg, "postgres", databases) == "1"  # the database stays, empty
    assert not is_seeded(pg, "globex")
    assert public_tables(pg, "tenant_globex") == 0  # the schema was rolled back too

    assert seed(pg, "globex") is not None  # the rerun retries instead of "already seeded"
    assert is_seeded(pg, "globex")
    assert int(scalar(pg, "tenant_globex", "SELECT count(*) FROM field_definitions;")) > 0


def test_each_reader_reads_only_its_own_tenant(pg: str) -> None:
    for tenant in ("acme", "globex"):
        seed(pg, tenant)
    password = secrets.token_hex(32)
    assert role_and_database(pg, "acme", password).returncode == 0  # step 1 is idempotent

    own = psql(pg, "tenant_acme", "-tA", user="chatmed_reader_acme", password=password,
               sql="SELECT count(*) FROM field_definitions;")  # fmt: skip
    assert own.returncode == 0, own.stderr
    assert int(own.stdout.strip()) > 0

    other = psql(pg, "tenant_globex", "-tA", user="chatmed_reader_acme", password=password,
                 sql="SELECT 1;")  # fmt: skip
    assert other.returncode != 0
    assert "permission denied for database" in other.stderr

    write = psql(pg, "tenant_acme", user="chatmed_reader_acme", password=password,
                 sql="DELETE FROM seed_info;")  # fmt: skip
    assert write.returncode != 0  # read-only role


def test_missing_reader_password_creates_nothing(pg: str) -> None:
    result = psql(pg, "postgres", "-v", "db=tenant_missing", "-v", "role=chatmed_reader_missing",
                  "-f", "/db/azure/tenant_role_and_database.sql")  # fmt: skip
    assert result.returncode != 0
    assert "READER_PW is not set" in result.stderr
    roles = "SELECT count(*) FROM pg_roles WHERE rolname = 'chatmed_reader_missing';"
    assert scalar(pg, "postgres", roles) == "0"
