"""Contract every SessionStore must pass (M4 AC1, AC3, AC7; D15).

Today only `InMemorySessionStore`; the stretch encrypted store must pass the same tests. The
clock is injected, so TTL tests do not sleep.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import weakref
from collections.abc import Callable
from typing import Any

import duckdb
import pytest

from chatmed.models import ParsedDocument, ParsedPage
from chatmed.session import (
    CapacityExceeded,
    InMemorySessionStore,
    SessionClosed,
    SessionNotFound,
    StoredDocument,
)

TTL = 100.0


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


StoreFactory = Callable[..., Any]


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture(params=["memory"])
def make(request: pytest.FixtureRequest, clock: Clock) -> StoreFactory:
    """Builds a store of the parametrized kind with a fake clock."""

    def factory(**overrides: Any) -> Any:
        options: dict[str, Any] = {
            "max_docs": 3,
            "duckdb_memory_limit": "64MB",
            "max_sessions": 3,
            "ttl_seconds": TTL,
            "clock": clock,
        } | overrides
        return InMemorySessionStore(**options)

    return factory


def _doc(n: int = 1) -> StoredDocument:
    pages = (ParsedPage(1, f"synthetic page {n}"),)
    return StoredDocument(ParsedDocument(f"{n:064x}", pages, "test-parser"), "cioms", "fp")


def _events(caplog: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == name]


# ---------------------------------------------------------------- AC1: close


def test_close_is_idempotent_and_wipes_everything(make: StoreFactory) -> None:
    store = make()
    session = store.create("acme")
    store.put_document(session.id, _doc())
    con = store.facts_db(session.id)
    session.history.append({"role": "user", "content": "q"})

    store.close(session.id)
    store.close(session.id)  # second call: no error, no effect

    assert session.closed and session.history == []
    with pytest.raises(duckdb.ConnectionException):
        con.execute("SELECT 1")
    for call in (store.get, store.get_documents, store.facts_db):
        with pytest.raises(SessionClosed):
            call(session.id)
    with pytest.raises(SessionClosed):
        store.put_document(session.id, _doc(2))


def test_closed_and_unknown_ids_are_told_apart(make: StoreFactory) -> None:
    store = make()
    sid = store.create("acme").id
    store.close(sid)
    with pytest.raises(SessionClosed):
        store.get(sid)
    with pytest.raises(SessionNotFound) as unknown:
        store.get("0" * 32)
    assert not isinstance(unknown.value, SessionClosed)
    assert (SessionClosed.code, SessionNotFound.code) == ("session_closed", "session_not_found")


def test_close_releases_documents_and_duckdb(make: StoreFactory) -> None:
    """AC1: after close nothing in the store keeps the parsed document or the connection."""
    store = make()
    session = store.create("acme")
    stored = _doc()
    store.put_document(session.id, stored)
    refs = [
        weakref.ref(stored),
        weakref.ref(stored.doc),
        weakref.ref(store.facts_db(session.id)),
        weakref.ref(session),
    ]
    sid = session.id
    del stored, session
    gc.collect()
    assert all(r() is not None for r in refs)  # still held by the open session

    store.close(sid)
    gc.collect()
    assert [r() for r in refs] == [None] * len(refs)


def test_close_cancels_inflight_tasks(make: StoreFactory) -> None:
    async def scenario() -> None:
        store = make()
        session = store.create("acme")
        task = asyncio.ensure_future(asyncio.sleep(10))
        session.inflight.add(task)
        store.close(session.id)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not session.inflight

    asyncio.run(scenario())


def test_closed_ids_are_remembered_up_to_a_bound(
    make: StoreFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tombstones are ids only, and bounded (D15): the oldest become `session_not_found`."""
    monkeypatch.setattr("chatmed.session.memory.CLOSED_IDS_KEPT", 2)
    store = make(max_sessions=5)
    ids = [store.create("acme").id for _ in range(3)]
    for sid in ids:
        store.close(sid)
    with pytest.raises(SessionNotFound) as oldest:
        store.get(ids[0])
    assert not isinstance(oldest.value, SessionClosed)
    for sid in ids[1:]:
        with pytest.raises(SessionClosed):
            store.get(sid)


# ---------------------------------------------------------------- AC1: idle TTL


def test_idle_session_expires_and_use_pushes_expiry_back(make: StoreFactory, clock: Clock) -> None:
    store = make()
    active = store.create("acme")
    idle = store.create("acme")
    clock.now = TTL - 1
    store.get(active.id)  # used: expiry moves to 2*TTL - 1
    clock.now = TTL + 1
    assert store.expire_idle() == 1
    assert store.get(active.id) is active
    with pytest.raises(SessionClosed):
        store.get(idle.id)


def test_expiry_is_exact_without_a_sweep(make: StoreFactory, clock: Clock) -> None:
    """Lazy expiry: an expired session reads as closed before the sweeper runs."""
    store = make()
    session = store.create("acme")
    con = store.facts_db(session.id)
    clock.now = TTL + 0.001
    with pytest.raises(SessionClosed):
        store.get_documents(session.id)  # does not count as use, and wipes
    assert session.closed
    with pytest.raises(duckdb.ConnectionException):
        con.execute("SELECT 1")
    assert store.expire_idle() == 0


def test_data_access_does_not_count_as_use(make: StoreFactory, clock: Clock) -> None:
    """Only `get` (start and end of a request) moves expiry, so idle time is per request."""
    store = make()
    session = store.create("acme")
    clock.now = TTL - 1
    store.get_documents(session.id)
    store.facts_db(session.id)
    clock.now = TTL + 1
    assert store.expire_idle() == 1


# ---------------------------------------------------------------- AC3: capacity


def test_capacity_is_enforced_and_freed_by_close_and_expiry(
    make: StoreFactory, clock: Clock
) -> None:
    store = make(max_sessions=2)
    first = store.create("acme")
    store.create("acme")
    with pytest.raises(CapacityExceeded):
        store.create("acme")
    store.close(first.id)
    store.create("acme")  # a closed session frees its slot
    clock.now = TTL + 1
    store.create("acme")  # expired sessions are wiped before the cap is checked
    store.create("acme")
    with pytest.raises(CapacityExceeded):
        store.create("acme")


# ---------------------------------------------------------------- AC7: lifecycle events


def test_lifecycle_events_name_the_reason(
    make: StoreFactory, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    store = make(max_sessions=3)
    with caplog.at_level(logging.INFO, logger="chatmed.session"):
        by_client = store.create("acme")
        by_ttl = store.create("acme")
        at_shutdown = store.create("globex")
        with pytest.raises(CapacityExceeded):
            store.create("acme")
        store.close(by_client.id)
        store.close(by_client.id)  # idempotent: no second event
        clock.now = TTL - 1
        store.get(at_shutdown.id)
        clock.now = TTL + 1
        store.expire_idle()
        assert store.close_all() == 1

    created = [(r.session_id, r.tenant_id) for r in _events(caplog, "session.created")]  # type: ignore[attr-defined]
    assert created == [(by_client.id, "acme"), (by_ttl.id, "acme"), (at_shutdown.id, "globex")]
    closed = [(r.session_id, r.reason) for r in _events(caplog, "session.closed")]  # type: ignore[attr-defined]
    assert closed == [(by_client.id, "client"), (by_ttl.id, "ttl"), (at_shutdown.id, "shutdown")]
    rejected = _events(caplog, "session.rejected")
    assert [(r.tenant_id, r.status) for r in rejected] == [("acme", "capacity")]  # type: ignore[attr-defined]
