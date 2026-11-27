"""The session pipeline: create → upload → ask → close, without HTTP (M4, D15).

The API (`chatmed.api`) is a thin transport over these functions, and the guardrail tests run
`run_full_session` over the same code. So "no files, no content in logs, same facts in two
sessions" is proven on the code that serves requests, not on a copy of it.

Lifecycle and concurrency (D15): every upload and question runs as its own task registered
on the session (`run_in_session`). Closing a session (client, TTL or shutdown) cancels those
tasks, so the request ends with `session_closed` and nothing is written after the wipe. The
store also refuses writes for a closed session, so a write cannot slip through either way.

Errors are `PipelineError` with a fixed code (and the HTTP status the API should use), never
with document text (rule 2).
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
from azure.core.exceptions import ClientAuthenticationError

from chatmed.agent import AgentError, Answer, ToolContext, run_agent
from chatmed.config import Settings
from chatmed.extraction import (
    ExtractionError,
    Fact,
    extract_for_tenant,
    load_facts,
    make_client,
)
from chatmed.parsing import DocumentParser, ParserError, build_parser
from chatmed.session import (
    CapacityExceeded,
    InMemorySessionStore,
    Session,
    SessionClosed,
    SessionStore,
    StoredDocument,
)
from chatmed.tenant_db import (
    PostgresTenantRepository,
    TenantNotAllowed,
    TenantRepository,
    UnknownDocumentType,
    tenant_db_passwords,
    tenant_dsn,
)

log = logging.getLogger(__name__)

ReadUpload = Callable[[], Awaitable[tuple[bytes, str]]]  # -> (pdf bytes, doc_type)
ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class Services:
    """Everything the pipeline needs; built once at startup (or injected by tests)."""

    settings: Settings
    store: SessionStore
    parser: DocumentParser
    model_client: Any  # AsyncAzureOpenAI or a fake with the same `chat.completions.create`
    repo_factory: Callable[[str], TenantRepository]  # tenant id -> repository bound to it


async def build_services(settings: Settings) -> Services:
    """The production services. Reads every tenant's DB password first, so a deployment
    missing one fails at startup, not at that tenant's first question (M5b AC9)."""
    passwords = tenant_db_passwords(settings)
    return Services(
        settings=settings,
        store=InMemorySessionStore(
            max_docs=settings.max_docs_per_session,
            duckdb_memory_limit=settings.duckdb_memory_limit,
            max_sessions=settings.max_sessions,
            ttl_seconds=settings.session_ttl_seconds,
        ),
        parser=await build_parser(settings),
        model_client=make_client(settings.azure_openai_endpoint, settings.azure_openai_api_version),
        repo_factory=lambda tenant: PostgresTenantRepository(
            settings, tenant, passwords.get(tenant)
        ),
    )


class PipelineError(Exception):
    """A request that cannot be served. Carries a fixed code and an HTTP status only."""

    def __init__(self, code: str, http_status: int) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


# ---------------------------------------------------------------- lifecycle


def create_session(services: Services, tenant_id: str) -> Session:
    """A new session for an allowed tenant (D13a, D14), if there is capacity (M4 AC3)."""
    try:
        tenant_dsn(services.settings, tenant_id)  # allow-list + pattern, before anything else
    except TenantNotAllowed:
        raise PipelineError("tenant_not_allowed", 422) from None
    try:
        return services.store.create(tenant_id)
    except CapacityExceeded:
        raise PipelineError("capacity", 503) from None


async def run_in_session[T](store: SessionStore, session: Session, work: Awaitable[T]) -> T:
    """Run `work` as a task the session's close can cancel (D15, M4 AC2).

    If the session is closed while `work` runs, `close()` cancels the task at its next await,
    before it can write anything, and this raises `SessionClosed`. If the caller itself is
    cancelled (e.g. the client went away), that cancellation propagates unchanged.
    Both the start and the end of the request count as use of the session (idle TTL).
    """
    task = asyncio.ensure_future(work)
    session.inflight.add(task)
    try:
        result = await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        caller_cancelled = current is not None and current.cancelling() > 0
        if session.closed and task.cancelled() and not caller_cancelled:
            raise SessionClosed("session closed") from None
        raise
    finally:
        session.inflight.discard(task)
    store.get(session.id)  # end of request: counts as use; SessionClosed if closed meanwhile
    return result


async def run_sweeper(store: SessionStore, interval_seconds: float) -> None:
    """The TTL sweeper (M4 AC1): every `interval_seconds`, close the idle sessions.

    Runs until cancelled (app shutdown). A failing sweep is logged (type only) and retried
    at the next interval, so one error never stops expiry for good."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            closed = store.expire_idle()
        except Exception:
            log.error("session.sweep_failed", exc_info=True)
            continue
        if closed:
            log.info("session.swept", extra={"session_count": closed})


def close_all_sessions(store: SessionStore) -> None:
    """App shutdown: every open session is wiped (M4 AC7)."""
    closed = store.close_all("shutdown")
    log.info("session.shutdown", extra={"session_count": closed})


# ---------------------------------------------------------------- upload


async def ingest_document(
    services: Services, session_id: str, read_upload: ReadUpload
) -> dict[str, Any]:
    """Read, parse, extract and store one document; returns a metadata-only summary.

    `read_upload` runs under the session lock, so one session reads at most one upload body
    at a time (this bounds memory, see architecture.md)."""
    session = services.store.get(session_id)
    return await run_in_session(
        services.store, session, _ingest_locked(services, session, read_upload)
    )


async def _ingest_locked(
    services: Services, session: Session, read_upload: ReadUpload
) -> dict[str, Any]:
    s = services.settings
    async with session.lock:
        if len(services.store.get_documents(session.id)) >= s.max_docs_per_session:
            raise PipelineError("too_many_documents", 409)
        pdf, doc_type = await read_upload()
        try:
            doc = await services.parser.parse(pdf)
        except ParserError:
            raise PipelineError("unreadable_pdf", 422) from None
        if len(doc.pages) > s.max_pages_per_doc:
            raise PipelineError("too_many_pages", 422)
        # Checked here so a duplicate never pays for an extraction; `_store_document` checks
        # again just before writing, where it guarantees facts and document go in together.
        if any(d.doc.sha256 == doc.sha256 for d in services.store.get_documents(session.id)):
            raise PipelineError("duplicate_document", 409)
        repo = services.repo_factory(session.tenant_id)  # bound to the session's tenant (rule 5)
        try:
            result = await extract_for_tenant(
                services.model_client, repo, doc, doc_type, settings=s
            )
        except UnknownDocumentType:
            raise PipelineError("unknown_document_type", 422) from None
        except ExtractionError:
            raise PipelineError("extraction_failed", 502) from None
        except ClientAuthenticationError:
            _log_model_auth_failed(session)
            raise PipelineError("model_auth_failed", 503) from None
        stored = StoredDocument(doc, doc_type, result.fingerprint)
        _store_document(services, session.id, stored, result.facts)
    log.info(
        "pipeline.document_added",
        extra={
            "session_id": session.id,
            "tenant_id": session.tenant_id,
            "doc_sha256": doc.sha256,
            "doc_type": doc_type,
            "page_count": len(doc.pages),
        },
    )
    join = sorted({f.join_status for f in result.facts if f.join_status})
    return {
        "sha256": doc.sha256,
        "doc_type": doc_type,
        "page_count": len(doc.pages),
        "parser_id": doc.parser_id,
        "fingerprint": result.fingerprint,
        "statuses": dict(sorted(Counter(f.status for f in result.facts).items())),
        "join_status": join[0] if len(join) == 1 else None,
    }


def _store_document(
    services: Services, session_id: str, stored: StoredDocument, facts: list[Fact]
) -> None:
    """Facts and document together or not at all.

    Synchronous with no await, so a session close cannot land in the middle. The limits are
    checked first, so `put_document` cannot refuse after the facts are committed. The facts
    go in one transaction: over the DuckDB memory limit the insert fails, is rolled back, and
    the upload ends with `session_memory_limit` (M4 AC4); nothing spills to disk."""
    store = services.store
    docs = store.get_documents(session_id)  # SessionClosed if the session is gone
    if any(d.doc.sha256 == stored.doc.sha256 for d in docs):
        raise PipelineError("duplicate_document", 409)
    if len(docs) >= services.settings.max_docs_per_session:
        raise PipelineError("too_many_documents", 409)
    con = store.facts_db(session_id)
    con.execute("BEGIN TRANSACTION")
    try:
        load_facts(con, facts)
        con.execute("COMMIT")
    except duckdb.OutOfMemoryException:
        con.execute("ROLLBACK")
        log.warning("pipeline.memory_limit", extra={"session_id": session_id})
        raise PipelineError("session_memory_limit", 503) from None
    except BaseException:
        con.execute("ROLLBACK")
        raise
    store.put_document(session_id, stored)


def _log_model_auth_failed(session: Session) -> None:
    """No Entra token for the model (e.g. the local service principal's secret expired,
    D16). Logged as metadata only: the SDK's message is not ours to vouch for (rule 2)."""
    log.error(
        "pipeline.model_auth_failed",
        extra={
            "session_id": session.id,
            "tenant_id": session.tenant_id,
            "error_type": "ClientAuthenticationError",
        },
    )


# ---------------------------------------------------------------- ask


async def _no_progress(_event: dict[str, Any]) -> None:
    return None


async def answer_question(
    services: Services,
    session_id: str,
    question: str,
    *,
    on_event: ProgressCallback = _no_progress,
) -> Answer:
    """Run the agent on one question in the session (D14). Raises AgentError, SessionClosed."""
    session = services.store.get(session_id)
    return await run_in_session(
        services.store, session, _answer_locked(services, session, question, on_event)
    )


async def _answer_locked(
    services: Services, session: Session, question: str, on_event: ProgressCallback
) -> Answer:
    s = services.settings
    async with session.lock:
        ctx = ToolContext(
            facts_db=services.store.facts_db(session.id),
            documents=services.store.get_documents(session.id),
            repo=services.repo_factory(session.tenant_id),
            fulltext_max_chars=s.agent_fulltext_max_chars,
            bm25_top_k=s.agent_bm25_top_k,
        )
        try:
            return await run_agent(
                services.model_client, session, ctx, question, settings=s, on_event=on_event
            )
        except ClientAuthenticationError:
            _log_model_auth_failed(session)
            raise AgentError("model_auth_failed") from None


# ---------------------------------------------------------------- whole session


@dataclass(frozen=True)
class SessionRun:
    """What one full session produced, held in memory by the caller (never written anywhere).

    `facts` are the rows of the session's `facts` table (sorted), read just before close."""

    session_id: str
    documents: list[dict[str, Any]]
    answers: list[Answer]
    facts: list[tuple[Any, ...]]


def _upload_of(pdf: bytes, doc_type: str) -> ReadUpload:
    """A `ReadUpload` that returns bytes already in memory (what the API reads from HTTP)."""

    async def read() -> tuple[bytes, str]:
        return pdf, doc_type

    return read


async def run_full_session(
    services: Services,
    tenant_id: str,
    pdf_paths: Sequence[Path],
    doc_type: str,
    questions: Sequence[str],
) -> SessionRun:
    """Create a session, upload each PDF, ask each question, then close it (always).

    The same steps the API runs for a client, in one call: the guardrail tests use it to show
    that a whole session writes no files and logs no content (M4 AC5)."""
    session = create_session(services, tenant_id)
    try:
        documents = []
        for path in pdf_paths:
            reader = _upload_of(Path(path).read_bytes(), doc_type)
            documents.append(await ingest_document(services, session.id, reader))
        answers = [await answer_question(services, session.id, q) for q in questions]
        facts = (
            services.store.facts_db(session.id)
            .execute("SELECT * FROM facts ORDER BY ALL")
            .fetchall()
        )
        return SessionRun(session.id, documents, answers, facts)
    finally:
        services.store.close(session.id, "client")
