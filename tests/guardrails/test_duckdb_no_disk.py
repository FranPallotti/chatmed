"""M4 AC4: the session DuckDB stays in memory (rule 1, D15).

Spilling is disabled and `memory_limit` is set per connection; every setting is locked; a
query that would exceed the limit fails (`session_memory_limit` at the API and agent level,
from real DuckDB out-of-memory errors) and writes no file, also not to DuckDB's default temp
directory (`.tmp` in the working dir for an in-memory database). A control test shows that
a plain connection does spill there under the same query, so the "no file" assertion can
fail.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest

from chatmed.agent import AgentError
from chatmed.extraction import Fact
from chatmed.pipeline import PipelineError, answer_question, create_session, ingest_document
from chatmed.session import connect_facts_db
from tests.conftest import fixture_bytes
from tests.fakes import FakeModelClient, final, tool_call
from tests.pipeline_support import FIXTURE, SHA, make_services, make_store

LIMIT = "48MB"
TINY_LIMIT = "16MB"  # the session limit in the real out-of-memory tests
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


@pytest.fixture
def empty_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """An empty working dir (DuckDB's default spill dir is `.tmp` in it) and an empty system
    temp dir (`TMP`/`TEMP`/`TMPDIR`), both checked after the run."""
    work, systemp = tmp_path / "work", tmp_path / "systemp"
    work.mkdir()
    systemp.mkdir()
    monkeypatch.chdir(work)
    for var in ("TMP", "TEMP", "TMPDIR"):
        monkeypatch.setenv(var, str(systemp))
    monkeypatch.setattr(tempfile, "tempdir", None)
    return [work, systemp]


async def _read_fixture() -> tuple[bytes, str]:
    return fixture_bytes(FIXTURE), "cioms"


async def test_real_oom_on_upload_is_session_memory_limit(
    recorded: FakeModelClient, monkeypatch: pytest.MonkeyPatch, empty_dirs: list[Path]
) -> None:
    """A real DuckDB out-of-memory error during the facts insert (no fake exception).

    The first upload's extraction returns 40 synthetic 1 MB filler values, which cannot fit
    in a 16 MB session. Documented state afterwards (D15): the insert is rolled back, no
    document is kept, the session stays open and usable, and the next upload works."""
    import chatmed.pipeline as pipeline

    real_extract = pipeline.extract_for_tenant
    calls = 0

    async def filler_then_real(client: Any, repo: Any, doc: Any, doc_type: str, **kw: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls > 1:
            return await real_extract(client, repo, doc, doc_type, **kw)
        filler = "x" * 1_000_000  # synthetic, no document content
        facts = [
            Fact(doc.sha256, doc_type, f"filler_{i}", filler, None, None, None, None, 1, None, "ok")
            for i in range(40)
        ]
        return SimpleNamespace(facts=facts, fingerprint="0" * 64)

    monkeypatch.setattr(pipeline, "extract_for_tenant", filler_then_real)
    services = make_services(recorded, store=make_store(duckdb_memory_limit=TINY_LIMIT))
    sid = create_session(services, "acme").id

    with pytest.raises(PipelineError) as exc:
        await ingest_document(services, sid, _read_fixture)
    assert (exc.value.code, exc.value.http_status) == ("session_memory_limit", 503)
    assert [f for d in empty_dirs for f in _files(d)] == []  # no spill, no `.tmp`, no temp file

    store = services.store
    assert store.get(sid).closed is False  # still open
    assert store.get_documents(sid) == []
    assert store.facts_db(sid).execute("SELECT count(*) FROM facts").fetchone() == (0,)
    summary = await ingest_document(services, sid, _read_fixture)  # usable: the retry works
    assert summary["sha256"] == SHA
    assert [d.doc.sha256 for d in store.get_documents(sid)] == [SHA]
    assert [f for d in empty_dirs for f in _files(d)] == []


async def test_real_oom_in_a_tool_ends_the_question(
    recorded: FakeModelClient, monkeypatch: pytest.MonkeyPatch, empty_dirs: list[Path]
) -> None:
    """A real out-of-memory error from a query on the session connection, inside a tool: the
    question ends with `session_memory_limit` (not `tool_failed`), nothing reaches the disk,
    and the session is as before the question (open, facts intact, no history entry)."""
    import chatmed.agent.tools as tools

    async def query_too_big(ctx: Any, *_args: Any) -> Any:
        return ctx.facts_db.execute(BIG_SORT).fetchall()  # needs far more than 16 MB

    monkeypatch.setattr(tools, "query_document_facts", query_too_big)
    steps = [
        tool_call("query_document_facts", {"doc_sha": None, "field_keys": ["age"]}),
        final("unused", [f"fact:{SHA[:12]}:age:1"]),
    ]
    store = make_store(duckdb_memory_limit=TINY_LIMIT)
    services = make_services(recorded, steps, store=store)
    session = create_session(services, "acme")
    await ingest_document(services, session.id, _read_fixture)
    facts_before = store.facts_db(session.id).execute("SELECT count(*) FROM facts").fetchone()

    with pytest.raises(AgentError) as exc:
        await answer_question(services, session.id, "How old is the patient?")
    assert exc.value.code == "session_memory_limit"
    assert [f for d in empty_dirs for f in _files(d)] == []
    assert session.history == []
    assert store.get(session.id).closed is False
    facts_after = store.facts_db(session.id).execute("SELECT count(*) FROM facts").fetchone()
    assert facts_after == facts_before
