"""M4 AC4: the session DuckDB stays in memory (rule 1, D15).

Spilling is disabled and `memory_limit` is set per connection; every setting is locked; a
query that would exceed the limit fails (`session_memory_limit` at the API and agent level)
and writes no file, also not to DuckDB's default temp directory (`.tmp` in the working dir
for an in-memory database). A control test shows that a plain connection does spill there
under the same query, so the "no file" assertion can fail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from chatmed.agent import AgentError
from chatmed.pipeline import PipelineError, answer_question, create_session, ingest_document
from chatmed.session import connect_facts_db
from tests.conftest import fixture_bytes
from tests.fakes import FakeModelClient, final, tool_call
from tests.pipeline_support import FIXTURE, SHA, make_services

LIMIT = "48MB"
# Sorting 1M md5 strings needs well over 48 MB: with spilling it completes, without it fails.
BIG_SORT = "SELECT count(*) FROM (SELECT md5(range::VARCHAR) AS h FROM range(1000000) ORDER BY h)"


@pytest.fixture(scope="module")
def recorded() -> FakeModelClient:
    return FakeModelClient.from_recordings()


def _files(root: Path) -> list[Path]:
    return sorted(root.rglob("*"))


def test_session_connection_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    con = connect_facts_db(LIMIT)
    settings = dict(
        con.execute(
            "SELECT name, value FROM duckdb_settings() WHERE name IN "
            "('temp_directory', 'enable_external_access', 'lock_configuration', 'memory_limit')"
        ).fetchall()
    )
    assert settings["temp_directory"] == ""
    assert settings["enable_external_access"] == "false"
    assert settings["lock_configuration"] == "true"
    assert settings["memory_limit"].endswith("MiB")  # 48MB as DuckDB reports it
    con.close()


@pytest.mark.parametrize(
    "statement",
    [
        "SET temp_directory = 'spill'",
        "SET memory_limit = '8GB'",  # without lock_configuration this one was allowed
        "RESET memory_limit",
        "SET enable_external_access = true",
        "SET lock_configuration = false",
        "COPY (SELECT 1) TO 'out.csv'",
        "ATTACH 'other.duckdb'",
        "SELECT * FROM read_csv('anything.csv')",
    ],
)
def test_nothing_can_reopen_the_disk(
    statement: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    con = connect_facts_db(LIMIT)
    with pytest.raises(duckdb.Error):
        con.execute(statement)
    con.close()
    assert _files(tmp_path) == []


def test_over_the_limit_a_query_fails_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    con = connect_facts_db(LIMIT)
    with pytest.raises(duckdb.OutOfMemoryException):
        con.execute(BIG_SORT).fetchall()
    assert _files(tmp_path) == []  # no `.tmp` spill directory, no file at all
    assert con.execute("SELECT count(*) FROM facts").fetchone() == (0,)  # still usable
    con.close()
    assert _files(tmp_path) == []


def test_control_a_plain_connection_does_spill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without our settings DuckDB spills the same query to `.tmp` in the working dir."""
    monkeypatch.chdir(tmp_path)
    con = duckdb.connect(":memory:", config={"memory_limit": LIMIT, "threads": 1})
    con.execute("SET enable_progress_bar = false")
    assert con.execute(BIG_SORT).fetchone() == (1_000_000,)
    assert (tmp_path / ".tmp").exists()  # removed again when the connection closes
    con.close()


async def test_upload_over_the_limit_is_session_memory_limit(
    recorded: FakeModelClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The facts insert fails half-way: rolled back, no document kept, code 503."""
    import chatmed.pipeline as pipeline

    real_load = pipeline.load_facts

    def load_then_fail(con: duckdb.DuckDBPyConnection, facts: list[Any]) -> None:
        real_load(con, facts)  # rows are in, inside the transaction ...
        raise duckdb.OutOfMemoryException("Out of Memory Error")  # ... then the limit hits

    monkeypatch.setattr(pipeline, "load_facts", load_then_fail)
    services = make_services(recorded)
    sid = create_session(services, "acme").id

    async def read() -> tuple[bytes, str]:
        return fixture_bytes(FIXTURE), "cioms"

    with pytest.raises(PipelineError) as exc:
        await ingest_document(services, sid, read)
    assert (exc.value.code, exc.value.http_status) == ("session_memory_limit", 503)
    assert services.store.get_documents(sid) == []
    assert services.store.facts_db(sid).execute("SELECT count(*) FROM facts").fetchone() == (0,)


async def test_tool_over_the_limit_ends_the_question(
    recorded: FakeModelClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In the agent, the error ends the question with the code (not `tool_failed`)."""
    import chatmed.agent.tools as tools

    async def oom(*_args: Any) -> Any:
        raise duckdb.OutOfMemoryException("Out of Memory Error")

    monkeypatch.setattr(tools, "query_document_facts", oom)
    steps = [
        tool_call("query_document_facts", {"doc_sha": None, "field_keys": ["age"]}),
        final("unused", [f"fact:{SHA[:12]}:age:1"]),
    ]
    services = make_services(recorded, steps)
    session = create_session(services, "acme")

    async def read() -> tuple[bytes, str]:
        return fixture_bytes(FIXTURE), "cioms"

    await ingest_document(services, session.id, read)
    with pytest.raises(AgentError) as exc:
        await answer_question(services, session.id, "How old is the patient?")
    assert exc.value.code == "session_memory_limit"
    assert session.history == []  # the question left nothing behind
