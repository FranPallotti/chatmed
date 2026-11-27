"""M4 AC1-AC3, AC7: session lifecycle through the pipeline and the API (D15).

- AC1: after close, every call with the id is 404 `session_closed`; an expired session is
  refused on access before any sweep; the sweeper runs with the app and wipes an untouched
  session at most TTL + sweep interval after its last activity; parsed documents and the
  DuckDB connection are released (weakref) after a real upload and question.
- AC2: closing (DELETE or TTL) while an upload or a question waits on the model ends that
  request with `session_closed` and nothing is written after the wipe; both orders.
- AC3: `MAX_SESSIONS` → 503 `capacity`.
- AC7: created / closed (client | ttl | shutdown) / rejected events; shutdown wipes all.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import logging
import time
import weakref
from typing import Any

import duckdb
import httpx2 as httpx  # the project's test HTTP client (pyproject dev deps)
import pytest
from fastapi.testclient import TestClient

from chatmed.api import create_app
from chatmed.pipeline import (
    PipelineError,
    answer_question,
    create_session,
    ingest_document,
    run_sweeper,
)
from chatmed.session import InMemorySessionStore, SessionClosed
from tests.conftest import fixture_bytes
from tests.fakes import FakeModelClient, ScriptedModelClient
from tests.pipeline_support import DEMO, FIXTURE, SHA, GatedClient, make_services, make_store

QUESTION = "Was the subject hospitalised?"


@pytest.fixture(scope="module")
def recorded() -> FakeModelClient:
    return FakeModelClient.from_recordings()


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class SpyStore(InMemorySessionStore):
    """Records every data access made after the session was closed (there must be none)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.after_close: list[str] = []
        self.closed_ids: set[str] = set()

    def close(self, session_id: str, reason: Any = "client") -> None:
        self.closed_ids.add(session_id)
        super().close(session_id, reason)

    def put_document(self, session_id: str, document: Any) -> None:
        if session_id in self.closed_ids:
            self.after_close.append("put_document")
        super().put_document(session_id, document)

    def facts_db(self, session_id: str) -> Any:
        if session_id in self.closed_ids:
            self.after_close.append("facts_db")
        return super().facts_db(session_id)


async def _read_fixture() -> tuple[bytes, str]:
    return fixture_bytes(FIXTURE), "cioms"


def _close(store: InMemorySessionStore, clock: Clock, session_id: str, how: str) -> None:
    if how == "client":
        store.close(session_id, "client")
    else:  # TTL: the session goes idle past the TTL and the sweeper runs
        clock.now += 10_000
        assert store.expire_idle() == 1


# ---------------------------------------------------------------- AC1


async def test_documents_and_duckdb_are_released_after_a_real_session(
    recorded: FakeModelClient,
) -> None:
    """Parse, extract, ask, close: then nothing keeps the parsed document or the connection."""
    services = make_services(recorded, DEMO[QUESTION](SHA))
    session = create_session(services, "acme")
    sid = session.id
    await ingest_document(services, sid, _read_fixture)
    answer = await answer_question(services, sid, QUESTION)
    assert answer.verified
    stored = services.store.get_documents(sid)[0]
    refs = [weakref.ref(stored.doc), weakref.ref(services.store.facts_db(sid))]
    refs.append(weakref.ref(session))
    del stored, session, answer
    services.store.close(sid)
    gc.collect()
    assert [r() for r in refs] == [None, None, None]


def test_every_call_after_close_is_404_session_closed(recorded: FakeModelClient) -> None:
    with TestClient(create_app(services=make_services(recorded), configure=False)) as api:
        sid = api.post("/sessions", json={"tenant_id": "acme"}).json()["session_id"]
        files = {"file": (FIXTURE, fixture_bytes(FIXTURE), "application/pdf")}
        assert api.post(f"/sessions/{sid}/documents", files=files, data={"doc_type": "cioms"})
        assert api.delete(f"/sessions/{sid}").status_code == 204
        upload = api.post(f"/sessions/{sid}/documents", files=files, data={"doc_type": "cioms"})
        ask = api.post(f"/sessions/{sid}/ask", json={"question": QUESTION})
        assert [(r.status_code, r.json()["detail"]) for r in (upload, ask)] == [
            (404, "session_closed"),
            (404, "session_closed"),
        ]
        assert api.delete(f"/sessions/{sid}").status_code == 204  # still idempotent
        unknown = api.post(f"/sessions/{'0' * 32}/ask", json={"question": QUESTION})
        assert (unknown.status_code, unknown.json()["detail"]) == (404, "session_not_found")


def test_sweeper_runs_with_the_app_and_shutdown_wipes_the_rest(
    recorded: FakeModelClient, caplog: pytest.LogCaptureFixture
) -> None:
    """AC1 + AC7: the TTL sweeper is an asyncio task of the app; shutdown closes every session."""
    store = make_store(ttl_seconds=0.2)
    services = make_services(recorded, store=store, session_sweep_interval_seconds=0.05)
    app = create_app(services=services, configure=False)
    with caplog.at_level(logging.INFO, logger="chatmed"):
        with TestClient(app) as api:
            idle = api.post("/sessions", json={"tenant_id": "acme"}).json()["session_id"]
            deadline = time.monotonic() + 5
            while (
                not any(r.getMessage() == "session.swept" for r in caplog.records)
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            ask = api.post(f"/sessions/{idle}/ask", json={"question": QUESTION})
            assert (ask.status_code, ask.json()["detail"]) == (404, "session_closed")
            store._ttl = 3600  # the next session stays open until shutdown
            open_at_shutdown = api.post("/sessions", json={"tenant_id": "acme"}).json()[
                "session_id"
            ]
            sweeper = app.state.sweeper
            assert not sweeper.done()
        assert sweeper.cancelled()
    closed = [
        (r.session_id, r.reason)  # type: ignore[attr-defined]
        for r in caplog.records
        if r.getMessage() == "session.closed"
    ]
    assert closed == [(idle, "ttl"), (open_at_shutdown, "shutdown")]
    shutdown = [r for r in caplog.records if r.getMessage() == "session.shutdown"]
    assert [r.session_count for r in shutdown] == [1]  # type: ignore[attr-defined]


def test_expired_session_is_refused_on_access_before_any_sweep(
    recorded: FakeModelClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Lazy expiry (D15): once the TTL has passed since the last activity, the next access is
    404 `session_closed` and wipes the session, even though the sweeper has not run."""
    clock = Clock()
    store = make_store(ttl_seconds=100, clock=clock)
    services = make_services(recorded, store=store, session_sweep_interval_seconds=3600)
    files = {"file": (FIXTURE, fixture_bytes(FIXTURE), "application/pdf")}
    with caplog.at_level(logging.INFO, logger="chatmed"):
        with TestClient(create_app(services=services, configure=False)) as api:
            sid = api.post("/sessions", json={"tenant_id": "acme"}).json()["session_id"]
            clock.now = 99  # just inside the TTL: still open (and this upload is activity)
            upload = api.post(f"/sessions/{sid}/documents", files=files, data={"doc_type": "cioms"})
            assert upload.status_code == 200
            con = store.facts_db(sid)
            clock.now = 99 + 100.001  # TTL passed since the upload ended
            ask = api.post(f"/sessions/{sid}/ask", json={"question": QUESTION})
            again = api.post(f"/sessions/{sid}/documents", files=files, data={"doc_type": "cioms"})
            assert [(r.status_code, r.json()["detail"]) for r in (ask, again)] == [
                (404, "session_closed"),
                (404, "session_closed"),
            ]
            with pytest.raises(duckdb.ConnectionException):
                con.execute("SELECT 1")  # wiped on that access, not at the next sweep
    messages = [r.getMessage() for r in caplog.records]
    assert "session.swept" not in messages  # the sweeper never ran
    closed = [r for r in caplog.records if r.getMessage() == "session.closed"]
    assert [(r.session_id, r.reason) for r in closed] == [(sid, "ttl")]  # type: ignore[attr-defined]


@pytest.mark.parametrize("last_activity", [0.0, 1.0, 29.99, 30.0, 45.0])
async def test_sweeper_wipes_within_ttl_plus_interval(
    last_activity: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented bound (D15): an untouched session is wiped by the sweeper more than TTL
    and at most TTL + sweep interval after its last activity, whatever the activity's phase
    against the sweeps. `asyncio.sleep` is replaced by one that moves the store's clock, so
    this is exact and instant."""
    ttl, interval = 100.0, 30.0
    clock = Clock()
    store = make_store(ttl_seconds=ttl, clock=clock)
    session = store.create("acme")
    # The sweeper starts at t=0 and sweeps at t=30, 60, ...; the session's last activity is
    # at t=last_activity (set directly: the fake sleep would jump the clock past it).
    store._last_used[session.id] = last_activity
    wiped_at: list[float] = []
    close = store.close

    def recording_close(session_id: str, reason: Any = "client") -> None:
        wiped_at.append(clock.now)
        close(session_id, reason)

    monkeypatch.setattr(store, "close", recording_close)
    real_sleep = asyncio.sleep

    async def clock_sleep(seconds: float) -> None:
        clock.now += seconds
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", clock_sleep)
    sweeper = asyncio.create_task(run_sweeper(store, interval))
    while not session.closed:
        await real_sleep(0)
    sweeper.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sweeper
    assert len(wiped_at) == 1
    assert ttl < wiped_at[0] - last_activity <= ttl + interval


# ---------------------------------------------------------------- AC2: close while in flight


@pytest.mark.parametrize("how", ["client", "ttl"])
async def test_close_during_upload_ends_it_with_session_closed(
    recorded: FakeModelClient, how: str
) -> None:
    clock = Clock()
    store = SpyStore(max_docs=5, duckdb_memory_limit="256MB", clock=clock)
    gated = GatedClient(FakeModelClient(recorded.responses), "extraction")
    services = make_services(recorded, store=store, model_client=gated)
    sid = create_session(services, "acme").id

    upload = asyncio.create_task(ingest_document(services, sid, _read_fixture))
    await asyncio.wait_for(gated.entered.wait(), 5)  # the upload waits on the model
    _close(store, clock, sid, how)
    gated.release.set()

    with pytest.raises(SessionClosed):
        await asyncio.wait_for(upload, 5)
    assert gated.calls == 0  # the extraction call never went out
    assert store.after_close == []  # no facts or document written after the wipe
    with pytest.raises(SessionClosed):
        store.get_documents(sid)


@pytest.mark.parametrize("how", ["client", "ttl"])
async def test_close_during_a_question_ends_it_with_session_closed(
    recorded: FakeModelClient, how: str
) -> None:
    clock = Clock()
    store = SpyStore(max_docs=5, duckdb_memory_limit="256MB", clock=clock)
    scripted = ScriptedModelClient(
        DEMO[QUESTION](SHA), extraction=FakeModelClient(recorded.responses)
    )
    gated = GatedClient(scripted, "agent")
    services = make_services(recorded, store=store, model_client=gated)
    session = create_session(services, "acme")
    await ingest_document(services, session.id, _read_fixture)

    ask = asyncio.create_task(answer_question(services, session.id, QUESTION))
    await asyncio.wait_for(gated.entered.wait(), 5)  # the question waits on the model
    _close(store, clock, session.id, how)
    gated.release.set()

    with pytest.raises(SessionClosed):
        await asyncio.wait_for(ask, 5)
    assert scripted.requests == []  # no agent call went out after the gate
    assert session.history == []  # no question or answer written after the wipe
    assert store.after_close == []


async def test_upload_finished_before_close_is_wiped_by_it(recorded: FakeModelClient) -> None:
    """The other order: the request completes, then the close wipes what it wrote."""
    store = SpyStore(max_docs=5, duckdb_memory_limit="256MB")
    services = make_services(recorded, DEMO[QUESTION](SHA), store=store)
    session = create_session(services, "acme")
    summary = await ingest_document(services, session.id, _read_fixture)
    answer = await answer_question(services, session.id, QUESTION)
    assert summary["sha256"] == SHA and answer.verified and len(session.history) == 2
    con = store.facts_db(session.id)
    store.close(session.id)
    assert session.history == [] and store.after_close == []
    with pytest.raises(SessionClosed):
        store.get_documents(session.id)
    with pytest.raises(Exception, match="(?i)closed"):
        con.execute("SELECT count(*) FROM facts")


async def test_close_during_http_requests_returns_session_closed(
    recorded: FakeModelClient,
) -> None:
    """AC2 over HTTP: DELETE while an upload / an ask waits on the model."""
    gated = GatedClient(FakeModelClient(recorded.responses), "extraction")
    services = make_services(recorded, model_client=gated)
    app = create_app(services=services, configure=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as api:
        sid = (await api.post("/sessions", json={"tenant_id": "acme"})).json()["session_id"]
        files = {"file": (FIXTURE, fixture_bytes(FIXTURE), "application/pdf")}
        upload = asyncio.create_task(
            api.post(f"/sessions/{sid}/documents", files=files, data={"doc_type": "cioms"})
        )
        await asyncio.wait_for(gated.entered.wait(), 5)
        assert (await api.delete(f"/sessions/{sid}")).status_code == 204
        gated.release.set()
        resp = await asyncio.wait_for(upload, 5)
        assert (resp.status_code, resp.json()["detail"]) == (404, "session_closed")

    scripted = ScriptedModelClient(
        DEMO[QUESTION](SHA), extraction=FakeModelClient(recorded.responses)
    )
    gated = GatedClient(scripted, "agent")
    services = make_services(recorded, model_client=gated)
    transport = httpx.ASGITransport(app=create_app(services=services, configure=False))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as api:
        sid = (await api.post("/sessions", json={"tenant_id": "acme"})).json()["session_id"]
        files = {"file": (FIXTURE, fixture_bytes(FIXTURE), "application/pdf")}
        ok = await api.post(f"/sessions/{sid}/documents", files=files, data={"doc_type": "cioms"})
        assert ok.status_code == 200
        ask = asyncio.create_task(api.post(f"/sessions/{sid}/ask", json={"question": QUESTION}))
        await asyncio.wait_for(gated.entered.wait(), 5)
        assert (await api.delete(f"/sessions/{sid}")).status_code == 204
        gated.release.set()
        resp = await asyncio.wait_for(ask, 5)
        events = [json.loads(line) for line in resp.text.splitlines() if line]
        assert events == [{"event": "started"}, {"event": "error", "code": "session_closed"}]
        assert scripted.requests == []


async def test_caller_cancellation_is_not_reported_as_session_closed(
    recorded: FakeModelClient,
) -> None:
    """A client that goes away cancels its own request; the session stays open."""
    gated = GatedClient(FakeModelClient(recorded.responses), "extraction")
    services = make_services(recorded, model_client=gated)
    session = create_session(services, "acme")
    upload = asyncio.create_task(ingest_document(services, session.id, _read_fixture))
    await asyncio.wait_for(gated.entered.wait(), 5)
    upload.cancel()
    with pytest.raises(asyncio.CancelledError):
        await upload
    assert not session.closed and not session.inflight
    assert services.store.get(session.id) is session


# ---------------------------------------------------------------- AC3: capacity


def test_capacity_returns_503_and_is_logged(
    recorded: FakeModelClient, caplog: pytest.LogCaptureFixture
) -> None:
    services = make_services(recorded, max_sessions=2)
    with TestClient(create_app(services=services, configure=False)) as api:
        with caplog.at_level(logging.INFO, logger="chatmed"):
            first = api.post("/sessions", json={"tenant_id": "acme"}).json()["session_id"]
            assert api.post("/sessions", json={"tenant_id": "acme"}).status_code == 201
            full = api.post("/sessions", json={"tenant_id": "acme"})
            assert (full.status_code, full.json()["detail"]) == (503, "capacity")
            api.delete(f"/sessions/{first}")
            assert api.post("/sessions", json={"tenant_id": "acme"}).status_code == 201
    rejected = [r for r in caplog.records if r.getMessage() == "session.rejected"]
    assert [(r.tenant_id, r.status) for r in rejected] == [("acme", "capacity")]  # type: ignore[attr-defined]


def test_tenant_is_checked_before_capacity(recorded: FakeModelClient) -> None:
    services = make_services(recorded, max_sessions=1)
    create_session(services, "acme")
    with pytest.raises(PipelineError) as exc:
        create_session(services, "initech")
    assert (exc.value.code, exc.value.http_status) == ("tenant_not_allowed", 422)


# ---------------------------------------------------------------- AC7: events reach the log


def test_lifecycle_events_survive_the_metadata_filter() -> None:
    """`reason` and `session_count` are allow-listed, so a reviewer sees them in the output."""
    import io

    from chatmed.logconfig import make_handler

    stream = io.StringIO()
    handler = make_handler(stream)
    logger = logging.getLogger("chatmed.session.memory")
    logger.addHandler(handler)
    old = logger.level
    logger.setLevel(logging.INFO)
    try:
        store = make_store(max_sessions=1)
        sid = store.create("acme").id
        with pytest.raises(Exception, match="too many"):
            store.create("acme")
        store.close(sid, "client")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old)
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [(e["event"], e.get("reason"), e.get("status")) for e in lines] == [
        ("session.created", None, None),
        ("session.rejected", None, "capacity"),
        ("session.closed", "client", None),
    ]
    assert all(e.get("session_id") in (sid, None) for e in lines)
