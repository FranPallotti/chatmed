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

The work itself (sessions, upload, ask, lifecycle) is in `chatmed.pipeline`; this module maps
its fixed error codes to HTTP and runs the TTL sweeper while the app is up (M4, D15).

Run: `uv run uvicorn chatmed.api:app` (uvicorn's own log handlers are replaced at startup) or
`uv run python -m chatmed.api` (starts uvicorn with `log_config=None`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from email import policy
from email.parser import BytesParser
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from chatmed.agent import AgentError
from chatmed.config import get_settings
from chatmed.logconfig import asyncio_exception_handler, configure_logging
from chatmed.pipeline import (
    PipelineError,
    Services,
    answer_question,
    build_services,
    close_all_sessions,
    create_session,
    ingest_document,
    run_sweeper,
)
from chatmed.session import Session, SessionNotFound

__all__ = ["Services", "create_app", "parse_multipart"]

log = logging.getLogger(__name__)

MB = 1024 * 1024
MULTIPART_OVERHEAD = 64 * 1024  # boundaries and part headers on top of the file itself
DOC_TYPE = re.compile(r"^[a-z0-9_]{1,64}$")
NDJSON = "application/x-ndjson"


class CreateSession(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64)


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def _http(exc: PipelineError | SessionNotFound) -> HTTPException:
    """Fixed codes only: `session_closed` / `session_not_found` are 404 (D15)."""
    if isinstance(exc, PipelineError):
        return HTTPException(exc.http_status, exc.code)
    return HTTPException(404, exc.code)


def _session(services: Services, session_id: str) -> Session:
    try:
        return services.store.get(session_id)
    except SessionNotFound as exc:  # SessionClosed included
        raise _http(exc) from None


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
        """Startup: build services, start the TTL sweeper. Shutdown: stop it, wipe every
        session (M4 AC1, AC7)."""
        asyncio.get_running_loop().set_exception_handler(asyncio_exception_handler)
        if not hasattr(app.state, "services"):
            app.state.services = await build_services(settings)
        store = app.state.services.store
        sweeper = asyncio.create_task(
            run_sweeper(store, settings.session_sweep_interval_seconds), name="ttl-sweeper"
        )
        app.state.sweeper = sweeper
        try:
            yield
        finally:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper
            close_all_sessions(store)

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
    async def create(body: CreateSession, request: Request) -> dict[str, str]:
        try:
            return {"session_id": create_session(svc(request), body.tenant_id).id}
        except PipelineError as exc:
            raise _http(exc) from None

    @app.post("/sessions/{session_id}/documents")
    async def upload(session_id: str, request: Request) -> dict[str, Any]:
        services = svc(request)
        s = services.settings

        async def read_upload() -> tuple[bytes, str]:
            """Runs under the session lock (one upload body per session at a time)."""
            body = await _read_body(request, s.max_upload_mb * MB + MULTIPART_OVERHEAD)
            pdf, doc_type = parse_multipart(request.headers.get("content-type", ""), body)
            if len(pdf) > s.max_upload_mb * MB:
                raise HTTPException(413, "upload_too_large")
            if not pdf.startswith(b"%PDF-"):
                raise HTTPException(415, "pdf_required")
            return pdf, doc_type

        try:
            return await ingest_document(services, session_id, read_upload)
        except (PipelineError, SessionNotFound) as exc:
            raise _http(exc) from None

    @app.post("/sessions/{session_id}/ask")
    async def ask(session_id: str, body: AskBody, request: Request) -> StreamingResponse:
        services = svc(request)
        session = _session(services, session_id)
        return StreamingResponse(
            _answer_stream(services, session.id, body.question), media_type=NDJSON
        )

    @app.delete("/sessions/{session_id}", status_code=204)
    async def close_session(session_id: str, request: Request) -> Response:
        svc(request).store.close(session_id, "client")  # idempotent wipe
        return Response(status_code=204)

    return app


async def _answer_stream(
    services: Services, session_id: str, question: str
) -> AsyncIterator[bytes]:
    """NDJSON: `started`, metadata-only `tool` events, then one `answer` (or `error`) (D14)."""
    yield _line({"event": "started"})
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def produce() -> None:
        try:
            answer = await answer_question(services, session_id, question, on_event=queue.put)
            await queue.put(
                {
                    "event": "answer",
                    "outcome": answer.outcome,  # verified | fallback (D14)
                    "reason": answer.reason,  # fallback only: a fixed code
                    "text": answer.text,
                    "facts_used": answer.facts_used,
                    "fingerprint": answer.fingerprint,
                }
            )
        except AgentError as exc:
            await queue.put({"event": "error", "code": exc.code})
        except SessionNotFound as exc:  # SessionClosed included (D15)
            await queue.put({"event": "error", "code": exc.code})
        except Exception:
            log.error("api.ask_failed", exc_info=True, extra={"session_id": session_id})
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
