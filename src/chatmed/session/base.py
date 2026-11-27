"""Session state: everything derived from a session's documents lives here, in memory (rule 1).

A `Session` owns the tenant (fixed at creation, D14), the parsed documents, the in-memory
DuckDB with the verified facts, and the chat history. `SessionStore` is the interface the API
uses; implementations are chosen by config (`SESSION_STORE`), never per request.

Lifecycle (D15): a session is closed by the client (`DELETE`), by the TTL sweeper when it has
been idle for `SESSION_TTL_SECONDS`, or at shutdown. Closing is immediate: it cancels the
session's in-flight requests, closes its DuckDB and drops its documents and history. After
that, the id answers `SessionClosed` and the store refuses every write for it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from chatmed.models import ParsedDocument

if TYPE_CHECKING:
    import duckdb

CloseReason = Literal["client", "ttl", "shutdown"]


class SessionNotFound(LookupError):
    """No open session with this id (never created, or closed long ago and forgotten)."""

    code = "session_not_found"


class SessionClosed(SessionNotFound):
    """The session was closed (client, TTL or shutdown); its data is gone (D15).

    A subclass of SessionNotFound, so code that only needs "not usable" can catch either."""

    code = "session_closed"


class CapacityExceeded(RuntimeError):
    """MAX_SESSIONS sessions are already open (M4 AC3)."""


class SessionFull(RuntimeError):
    """The session already holds MAX_DOCS_PER_SESSION documents."""


class DuplicateDocument(RuntimeError):
    """A document with the same sha256 is already in the session."""


@dataclass(frozen=True)
class StoredDocument:
    """A parsed document plus what it was extracted as. `fingerprint` has no content (D6)."""

    doc: ParsedDocument
    doc_type: str
    fingerprint: str


@dataclass(eq=False)
class Session:
    """One user session. `history` holds prior questions and verified answer texts only.

    `inflight` holds the tasks doing work for this session (an upload or a question), so that
    closing the session can cancel them before they write anything (D15). `closed` is set
    once, by the store, and never cleared.
    """

    id: str
    tenant_id: str
    history: list[dict[str, Any]] = field(default_factory=list)
    # Serializes questions and uploads within a session, so history and facts never interleave.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    inflight: set[asyncio.Future[Any]] = field(default_factory=set)
    closed: bool = False


class SessionStore(Protocol):
    """`get` exists so the API can find the session's tenant and history by id (D14).

    `expire_idle` and `close_all` exist so the app can run the TTL sweeper and wipe every
    session at shutdown without knowing how sessions are stored (D15).
    """

    def create(self, tenant_id: str) -> Session: ...

    def get(self, session_id: str) -> Session: ...

    def put_document(self, session_id: str, document: StoredDocument) -> None: ...

    def get_documents(self, session_id: str) -> list[StoredDocument]: ...

    def facts_db(self, session_id: str) -> duckdb.DuckDBPyConnection: ...

    def close(self, session_id: str, reason: CloseReason = "client") -> None: ...

    def expire_idle(self) -> int: ...

    def close_all(self, reason: CloseReason = "shutdown") -> int: ...
