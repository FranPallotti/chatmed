"""In-memory session store: sessions in a dict, an idle TTL, a session cap (M4, D15).

Why each DuckDB setting: `:memory:` keeps the facts in process memory (rule 1);
`temp_directory=''` stops DuckDB from spilling to disk when it runs low on memory (a query
over `memory_limit` fails with an out-of-memory error instead, which is what we want);
`enable_external_access=false` blocks `COPY ... TO`, `ATTACH` and file reads; and
`lock_configuration=true` makes every setting read-only on that connection. The lock is
needed: `enable_external_access=false` alone blocks `SET temp_directory` but not
`SET memory_limit` (DuckDB 1.x/2.x), as tests/guardrails/test_duckdb_no_disk.py shows.

Lifecycle (D15):
- A session is idle when it has not been used (`get`) for `ttl_seconds`. `get` is called when
  a request starts and when it ends, so a request in progress keeps its session alive.
- Expiry is lazy as well as swept: an expired session that the sweeper has not reached yet
  already answers `SessionClosed`, so the TTL is exact whatever the sweep interval.
- Closing cancels the session's in-flight tasks, closes its DuckDB and drops every reference
  to documents, facts and history. Closed ids (random tokens, no content) are remembered, up
  to `CLOSED_IDS_KEPT`, so a late call gets `SessionClosed` rather than `SessionNotFound`.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable

import duckdb

from chatmed.extraction import FACTS_DDL
from chatmed.session.base import (
    CapacityExceeded,
    CloseReason,
    DuplicateDocument,
    Session,
    SessionClosed,
    SessionFull,
    SessionNotFound,
    StoredDocument,
)

log = logging.getLogger(__name__)

CLOSED_IDS_KEPT = 10_000  # ~10k * 32 hex chars: bounded memory for the `session_closed` answer


def connect_facts_db(memory_limit: str) -> duckdb.DuckDBPyConnection:
    """A fresh in-memory DuckDB with no way to write to disk, and an empty `facts` table."""
    con = duckdb.connect(":memory:", config={"temp_directory": "", "memory_limit": memory_limit})
    con.execute("SET enable_external_access = false")
    con.execute(FACTS_DDL)
    con.execute("SET lock_configuration = true")  # last: nothing can change the above now
    return con


class InMemorySessionStore:
    """Sessions in a dict. Single replica only (D5); a restart loses sessions, by design."""

    def __init__(
        self,
        *,
        max_docs: int,
        duckdb_memory_limit: str,
        max_sessions: int = 20,
        ttl_seconds: float = 1800,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_docs = max_docs
        self._memory_limit = duckdb_memory_limit
        self._max_sessions = max_sessions
        self._ttl = ttl_seconds
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._docs: dict[str, dict[str, StoredDocument]] = {}
        self._dbs: dict[str, duckdb.DuckDBPyConnection] = {}
        self._last_used: dict[str, float] = {}
        self._closed_ids: OrderedDict[str, None] = OrderedDict()

    # ------------------------------------------------------------ lifecycle

    def create(self, tenant_id: str) -> Session:
        """The caller has already checked the tenant against the allow-list (D14).

        Expired sessions are closed first, so they never count against `max_sessions`."""
        self.expire_idle()
        if len(self._sessions) >= self._max_sessions:
            log.warning("session.rejected", extra={"tenant_id": tenant_id, "status": "capacity"})
            raise CapacityExceeded("too many open sessions")
        session = Session(id=secrets.token_hex(16), tenant_id=tenant_id)
        self._sessions[session.id] = session
        self._docs[session.id] = {}
        self._dbs[session.id] = connect_facts_db(self._memory_limit)
        self._last_used[session.id] = self._clock()
        log.info("session.created", extra={"session_id": session.id, "tenant_id": tenant_id})
        return session

    def get(self, session_id: str) -> Session:
        """The open session; counts as use (pushes its expiry back)."""
        session = self._open(session_id)
        self._last_used[session_id] = self._clock()
        return session

    def close(self, session_id: str, reason: CloseReason = "client") -> None:
        """Idempotent wipe: cancels in-flight work, closes DuckDB, drops documents and history.

        Synchronous on purpose: it runs between two steps of any in-flight task, so no task
        can be half-way through a write when the data goes away (D15)."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        session.closed = True
        for task in list(session.inflight):
            task.cancel()
        session.inflight.clear()
        self._docs.pop(session_id, None)
        self._last_used.pop(session_id, None)
        con = self._dbs.pop(session_id, None)
        if con is not None:
            con.close()
        session.history.clear()
        self._remember_closed(session_id)
        log.info(
            "session.closed",
            extra={"session_id": session_id, "tenant_id": session.tenant_id, "reason": reason},
        )

    def expire_idle(self) -> int:
        """Close every session idle for longer than the TTL; returns how many were closed."""
        now = self._clock()
        expired = [sid for sid, used in self._last_used.items() if now - used > self._ttl]
        for sid in expired:
            self.close(sid, "ttl")
        return len(expired)

    def close_all(self, reason: CloseReason = "shutdown") -> int:
        """Close every open session (app shutdown); returns how many were closed."""
        ids = list(self._sessions)
        for sid in ids:
            self.close(sid, reason)
        return len(ids)

    # ------------------------------------------------------------ data

    def put_document(self, session_id: str, document: StoredDocument) -> None:
        self._open(session_id)
        docs = self._docs[session_id]
        if document.doc.sha256 in docs:
            raise DuplicateDocument("document already in session")
        if len(docs) >= self._max_docs:
            raise SessionFull("too many documents in session")
        docs[document.doc.sha256] = document

    def get_documents(self, session_id: str) -> list[StoredDocument]:
        """Ordered by sha256, so every consumer sees the same order (rule 3)."""
        self._open(session_id)
        return [self._docs[session_id][sha] for sha in sorted(self._docs[session_id])]

    def facts_db(self, session_id: str) -> duckdb.DuckDBPyConnection:
        self._open(session_id)
        return self._dbs[session_id]

    # ------------------------------------------------------------ helpers

    def _open(self, session_id: str) -> Session:
        """The session if it is open and not expired; never touches its expiry."""
        session = self._sessions.get(session_id)
        if session is None:
            if session_id in self._closed_ids:
                raise SessionClosed("session closed")
            raise SessionNotFound("no such session")
        if self._clock() - self._last_used[session_id] > self._ttl:
            self.close(session_id, "ttl")
            raise SessionClosed("session closed")
        return session

    def _remember_closed(self, session_id: str) -> None:
        self._closed_ids[session_id] = None
        while len(self._closed_ids) > CLOSED_IDS_KEPT:
            self._closed_ids.popitem(last=False)
