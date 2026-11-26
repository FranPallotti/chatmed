"""HTTP API (M3 AC6): create a session, upload a PDF, ask (NDJSON stream), close.

Privacy choices (rules 1, 2):
- Uploads are read into memory and the multipart body is parsed with the standard library
  `email` parser. Starlette's `UploadFile` is not used: it spools files above 1 MB to a
  temporary file on disk.
- No request or response bodies are logged; error responses carry fixed codes only.
- Uncaught exceptions are logged through the metadata-only filter (type and code location,
  never the message); the client gets `{"detail": "internal_error"}`.

The tenant of a session is given at creation and checked against `TENANTS` (D14); in M5 it
comes from the caller's Entra ID token instead.

Run: `uv run uvicorn chatmed.api:app` (uvicorn's own log handlers are replaced at startup) or
`uv run python -m chatmed.api` (starts uvicorn with `log_config=None`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from chatmed.agent import AgentError, ToolContext, run_agent
from chatmed.config import Settings, get_settings
from chatmed.extraction import ExtractionError, extract_for_tenant, load_facts, make_client
from chatmed.logconfig import asyncio_exception_handler, configure_logging
from chatmed.parsing import DocumentParser, ParserError, build_parser
from chatmed.session import (
    DuplicateDocument,
    InMemorySessionStore,
    Session,
    SessionFull,
    SessionNotFound,
    SessionStore,
    StoredDocument,
)
from chatmed.tenant_db import (
    PostgresTenantRepository,
    TenantNotAllowed,
    TenantRepository,
    UnknownDocumentType,
    tenant_dsn,
)

log = logging.getLogger(__name__)

MB = 1024 * 1024
MULTIPART_OVERHEAD = 64 * 1024  # boundaries and part headers on top of the file itself
DOC_TYPE = re.compile(r"^[a-z0-9_]{1,64}$")
NDJSON = "application/x-ndjson"


@dataclass
class Services:
    """Everything the endpoints need; built once at startup (or injected by tests)."""

    settings: Settings
    store: SessionStore
    parser: DocumentParser
    model_client: Any  # AsyncAzureOpenAI or a fake with the same `chat.completions.create`
    repo_factory: Callable[[str], TenantRepository]  # tenant id -> repository bound to it


async def build_services(settings: Settings) -> Services:
    return Services(
        settings=settings,
        store=InMemorySessionStore(
            max_docs=settings.max_docs_per_session,
            duckdb_memory_limit=settings.duckdb_memory_limit,
        ),
        parser=await build_parser(settings),
        model_client=make_client(settings.azure_openai_endpoint, settings.azure_openai_api_version),
        repo_factory=lambda tenant: PostgresTenantRepository(settings, tenant),
    )


class CreateSession(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64)


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def _session(services: Services, session_id: str) -> Session:
    try:
        return services.store.get(session_id)
    except SessionNotFound:
        raise HTTPException(404, "session_not_found") from None


async def _read_body(request: Request, limit: int) -> bytes:
    """The request body, in memory, refusing anything over `limit` bytes (rule 1, AC6).

    1. A declared Content-Length over the limit is refused before any byte is read.
    2. The body is then streamed with a byte counter, because Content-Length can be absent
       (chunked transfer) or wrong: the first chunk past the limit ends the read with 413,
       so at most `limit` bytes plus one chunk are ever held.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, "upload_too_large")
    received = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise HTTPException(413, "upload_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def parse_multipart(content_type: str, body: bytes) -> tuple[bytes, str]:
    """(file bytes, doc_type) from a multipart/form-data body, parsed in memory."""
    if not content_type.lower().startswith("multipart/form-data"):
        raise HTTPException(415, "multipart_required")
    head = f"Content-Type: {content_type}\r\n\r\n".encode("latin-1", "replace")
    message = BytesParser(policy=policy.HTTP).parsebytes(head + body)
    file: bytes | None = None
    doc_type: str | None = None
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        if name == "file":
            file = payload
        elif name == "doc_type":
            doc_type = payload.decode("utf-8", "replace").strip()
    if file is None or doc_type is None:
        raise HTTPException(422, "file_and_doc_type_required")
    if not DOC_TYPE.fullmatch(doc_type):
        raise HTTPException(422, "invalid_doc_type")
    return file, doc_type


def _line(event: dict[str, Any]) -> bytes:
    return (json.dumps(event, sort_keys=True, ensure_ascii=False, default=str) + "\n").encode()


def create_app(*, services: Services | None = None, configure: bool = True) -> FastAPI:
    """The FastAPI app. Tests pass `services` (fakes) and `configure=False`."""
    settings = services.settings if services else get_settings()
    if configure:
        configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        asyncio.get_running_loop().set_exception_handler(asyncio_exception_handler)
        if not hasattr(app.state, "services"):
            app.state.services = await build_services(settings)
        yield

    app = FastAPI(title="ChatMed", lifespan=lifespan)
    if services is not None:
        app.state.services = services

    def svc(request: Request) -> Services:
        services: Services = request.app.state.services
        return services

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception) -> JSONResponse:
        log.error("api.unhandled_error", exc_info=(type(exc), exc, exc.__traceback__))
        return JSONResponse({"detail": "internal_error"}, status_code=500)

    @app.post("/sessions", status_code=201)
    async def create_session(body: CreateSession, request: Request) -> dict[str, str]:
        services = svc(request)
        try:
            tenant_dsn(services.settings, body.tenant_id)  # allow-list + pattern (D13a, D14)
        except TenantNotAllowed:
            raise HTTPException(422, "tenant_not_allowed") from None
        return {"session_id": services.store.create(body.tenant_id).id}

    @app.post("/sessions/{session_id}/documents")
    async def upload(session_id: str, request: Request) -> dict[str, Any]:
        services = svc(request)
        s = services.settings
        session = _session(services, session_id)
        async with session.lock:
            if len(services.store.get_documents(session_id)) >= s.max_docs_per_session:
                raise HTTPException(409, "too_many_documents")
            body = await _read_body(request, s.max_upload_mb * MB + MULTIPART_OVERHEAD)
            pdf, doc_type = parse_multipart(request.headers.get("content-type", ""), body)
            if len(pdf) > s.max_upload_mb * MB:
                raise HTTPException(413, "upload_too_large")
            if not pdf.startswith(b"%PDF-"):
                raise HTTPException(415, "pdf_required")
            return await _ingest(services, session, pdf, doc_type)

    @app.post("/sessions/{session_id}/ask")
    async def ask(session_id: str, body: AskBody, request: Request) -> StreamingResponse:
        services = svc(request)
        session = _session(services, session_id)
        return StreamingResponse(
            _answer_stream(services, session, body.question), media_type=NDJSON
        )

    @app.delete("/sessions/{session_id}", status_code=204)
    async def close_session(session_id: str, request: Request) -> Response:
        svc(request).store.close(session_id)  # idempotent wipe
        return Response(status_code=204)

    return app


async def _ingest(
    services: Services, session: Session, pdf: bytes, doc_type: str
) -> dict[str, Any]:
    """Parse, check limits, extract, load facts into the session DuckDB, keep the document."""
    s = services.settings
    try:
        doc = await services.parser.parse(pdf)
    except ParserError:
        raise HTTPException(422, "unreadable_pdf") from None
    if len(doc.pages) > s.max_pages_per_doc:
        raise HTTPException(422, "too_many_pages")
    if any(d.doc.sha256 == doc.sha256 for d in services.store.get_documents(session.id)):
        raise HTTPException(409, "duplicate_document")
    repo = services.repo_factory(session.tenant_id)  # bound to the session's tenant (rule 5)
    try:
        result = await extract_for_tenant(services.model_client, repo, doc, doc_type, settings=s)
    except UnknownDocumentType:
        raise HTTPException(422, "unknown_document_type") from None
    except ExtractionError:
        raise HTTPException(502, "extraction_failed") from None
    try:
        services.store.put_document(session.id, StoredDocument(doc, doc_type, result.fingerprint))
    except DuplicateDocument:
        raise HTTPException(409, "duplicate_document") from None
    except SessionFull:
        raise HTTPException(409, "too_many_documents") from None
    load_facts(services.store.facts_db(session.id), result.facts)
    log.info(
        "api.document_added",
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


async def _answer_stream(
    services: Services, session: Session, question: str
) -> AsyncIterator[bytes]:
    """NDJSON: `started`, metadata-only `tool` events, then one `answer` (or `error`) (D14)."""
    yield _line({"event": "started"})
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def produce() -> None:
        try:
            async with session.lock:
                s = services.settings
                ctx = ToolContext(
                    facts_db=services.store.facts_db(session.id),
                    documents=services.store.get_documents(session.id),
                    repo=services.repo_factory(session.tenant_id),
                    fulltext_max_chars=s.agent_fulltext_max_chars,
                    bm25_top_k=s.agent_bm25_top_k,
                )
                answer = await run_agent(
                    services.model_client, session, ctx, question, settings=s, on_event=queue.put
                )
            await queue.put(
                {
                    "event": "answer",
                    "verified": answer.verified,
                    "reason": answer.reason,
                    "text": answer.text,
                    "facts_used": answer.facts_used,
                    "fingerprint": answer.fingerprint,
                }
            )
        except AgentError as exc:
            await queue.put({"event": "error", "code": exc.code})
        except SessionNotFound:
            await queue.put({"event": "error", "code": "session_not_found"})
        except Exception:
            log.error("api.ask_failed", exc_info=True, extra={"session_id": session.id})
            await queue.put({"event": "error", "code": "internal_error"})
        finally:
            await queue.put(None)

    task = asyncio.create_task(produce())
    try:
        while (event := await queue.get()) is not None:
            yield _line(event)
    finally:
        if not task.done():
            task.cancel()


def __getattr__(name: str) -> Any:
    """`chatmed.api:app` for uvicorn, built on first access (so importing needs no env)."""
    if name == "app":
        return create_app()
    raise AttributeError(name)


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8000, log_config=None)  # noqa: S104


if __name__ == "__main__":
    main()
