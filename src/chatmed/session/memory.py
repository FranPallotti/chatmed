"""Minimal in-memory session store (M3). M4 adds the TTL sweeper and the guardrail tests.

Why each DuckDB setting: `:memory:` keeps the facts in process memory (rule 1);
`temp_directory=''` stops DuckDB from spilling to disk when it runs low on memory (it fails
instead, which is what we want); `memory_limit` keeps it within the container; and
`enable_external_access=false` blocks `COPY ... TO`, `ATTACH` and file reads, and also locks
the two settings above so nothing can change them later on this connection.
"""

from __future__ import annotations

import logging
import secrets

import duckdb

from chatmed.extraction import FACTS_DDL
from chatmed.session.base import (
    DuplicateDocument,
    Session,
    SessionFull,
    SessionNotFound,
    StoredDocument,
)

log = logging.getLogger(__name__)


def connect_facts_db(memory_limit: str) -> duckdb.DuckDBPyConnection:
    """A fresh in-memory DuckDB with no way to write to disk, and an empty `facts` table."""
    con = duckdb.connect(":memory:", config={"temp_directory": "", "memory_limit": memory_limit})
    con.execute("SET enable_external_access = false")
    con.execute(FACTS_DDL)
    return con


class InMemorySessionStore:
    """Sessions in a dict. Single replica only (D5); a restart loses sessions, by design."""

    def __init__(self, *, max_docs: int, duckdb_memory_limit: str) -> None:
        self._max_docs = max_docs
        self._memory_limit = duckdb_memory_limit
        self._sessions: dict[str, Session] = {}
        self._docs: dict[str, dict[str, StoredDocument]] = {}
        self._dbs: dict[str, duckdb.DuckDBPyConnection] = {}

    def create(self, tenant_id: str) -> Session:
        """The caller has already checked the tenant against the allow-list (D14)."""
        session = Session(id=secrets.token_hex(16), tenant_id=tenant_id)
        self._sessions[session.id] = session
        self._docs[session.id] = {}
        self._dbs[session.id] = connect_facts_db(self._memory_limit)
        log.info("session.created", extra={"session_id": session.id, "tenant_id": tenant_id})
        return session

    def get(self, session_id: str) -> Session:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise SessionNotFound("no such session") from None

    def put_document(self, session_id: str, document: StoredDocument) -> None:
        self.get(session_id)
        docs = self._docs[session_id]
        if document.doc.sha256 in docs:
            raise DuplicateDocument("document already in session")
        if len(docs) >= self._max_docs:
            raise SessionFull("too many documents in session")
        docs[document.doc.sha256] = document

    def get_documents(self, session_id: str) -> list[StoredDocument]:
        """Ordered by sha256, so every consumer sees the same order (rule 3)."""
        self.get(session_id)
        return [self._docs[session_id][sha] for sha in sorted(self._docs[session_id])]

    def facts_db(self, session_id: str) -> duckdb.DuckDBPyConnection:
        self.get(session_id)
        return self._dbs[session_id]

    def close(self, session_id: str) -> None:
        """Idempotent wipe: closes the DuckDB connection and drops documents and history."""
        session = self._sessions.pop(session_id, None)
        self._docs.pop(session_id, None)
        con = self._dbs.pop(session_id, None)
        if con is not None:
            con.close()
        if session is not None:
            session.history.clear()
            log.info("session.closed", extra={"session_id": session_id})
