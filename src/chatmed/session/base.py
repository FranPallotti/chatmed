"""Session state: everything derived from a session's documents lives here, in memory (rule 1).

A `Session` owns the tenant (fixed at creation, D14), the parsed documents, the in-memory
DuckDB with the verified facts, and the chat history. `SessionStore` is the interface the API
uses; implementations are chosen by config (`SESSION_STORE`), never per request.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from chatmed.models import ParsedDocument

if TYPE_CHECKING:
    import duckdb


class SessionNotFound(LookupError):
    """No open session with this id (never created, closed or expired)."""


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


@dataclass
class Session:
    """One user session. `history` holds prior questions and verified answer texts only."""

    id: str
    tenant_id: str
    history: list[dict[str, Any]] = field(default_factory=list)
    # Serializes questions within a session, so history and tool results never interleave.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionStore(Protocol):
    """`get` exists so the API can find the session's tenant and history by id (D14)."""

    def create(self, tenant_id: str) -> Session: ...

    def get(self, session_id: str) -> Session: ...

    def put_document(self, session_id: str, document: StoredDocument) -> None: ...

    def get_documents(self, session_id: str) -> list[StoredDocument]: ...

    def facts_db(self, session_id: str) -> duckdb.DuckDBPyConnection: ...

    def close(self, session_id: str) -> None: ...
