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

M5a: `GET /healthz` (liveness, no tenant data), the static UI at `/` (AC4) and optional
Application Insights telemetry with metadata only (AC5, `chatmed.telemetry`). Every response
carries `Cache-Control: no-store`, so answers and facts never land in a browser or proxy cache
on disk (rule 1).

Run: `uv run uvicorn chatmed.api:app` (uvicorn's own log handlers are replaced at startup) or
`uv run python -m chatmed.api` (starts uvicorn with `log_config=None`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from email import policy
from email.parser import BytesParser
from functools import cache
from importlib import resources
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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
from chatmed.telemetry import Telemetry, setup_telemetry

__all__ = ["Services", "create_app", "parse_multipart"]

log = logging.getLogger("chatmed.api")  # not __name__: "__main__" under `python -m`

MB = 1024 * 1024
MULTIPART_OVERHEAD = 64 * 1024  # boundaries and part headers on top of the file itself
DOC_TYPE = re.compile(r"^[a-z0-9_]{1,64}$")
NDJSON = "application/x-ndjson"
# The UI (AC4): one page, its script and stylesheet, all from this origin; no inline code.
UI_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}
UI_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class CreateSession(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64)


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


@cache
def _ui_file(name: str) -> bytes:
    return (resources.files("chatmed") / "static" / name).read_bytes()


class NoStoreMiddleware:
    """Adds `Cache-Control: no-store` to every response (pure ASGI, so streaming and
    cancellation pass through untouched)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k != b"cache-control"]
                message["headers"] = [*headers, (b"cache-control", b"no-store")]
            await send(message)

        await self.app(scope, receive, send_no_store)


UNMATCHED_ROUTE = "unmatched"
# Methods logged by name; any other is logged as OTHER (the method is client input).
LOGGED_METHODS = frozenset({"GET", "POST", "DELETE"})


class RequestLogMiddleware:
    """One `http.request` event per request: method, route template, status, duration.

    Replaces uvicorn's access log, which prints the raw path (session ids) and the client
    address. The route is the template FastAPI matched (`/sessions/{session_id}/ask`), read
    after routing from `scope["route"]`; a request no route matched is logged as `unmatched`,
    never with its path. The method is one of `LOGGED_METHODS` or `OTHER`, so a client cannot
    put text of its own into the log. Pure ASGI, so the duration of a streamed answer covers
    the stream.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = 500  # if the app raises before starting a response

        async def send_and_record(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_and_record)
        finally:
            route = getattr(scope.get("route"), "path", None)
            method = scope.get("method", "")
            log.info(
                "http.request",
                extra={
                    "method": method if method in LOGGED_METHODS else "OTHER",
                    "route": route if isinstance(route, str) else UNMATCHED_ROUTE,
                    "http_status": status,
                    "duration_ms": round((time.perf_counter() - start) * 1000),
                },
            )


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
    telemetry: Telemetry | None = None

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
            if telemetry is not None:
                telemetry.shutdown()  # flushes the last lifecycle events

    app = FastAPI(title="ChatMed", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(NoStoreMiddleware)
    app.add_middleware(RequestLogMiddleware)  # added last = outermost: sees the final status
    if services is not None:
        app.state.services = services
    if configure:
        telemetry = setup_telemetry(app, settings)

    def svc(request: Request) -> Services:
        services: Services = request.app.state.services
        return services

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception) -> JSONResponse:
        log.error("api.unhandled_error", exc_info=(type(exc), exc, exc.__traceback__))
        return JSONResponse({"detail": "internal_error"}, status_code=500)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness only: no tenant data, no database or model call."""
        return {"status": "ok"}

    def ui_route(path: str, name: str, media_type: str) -> None:
        async def serve() -> Response:
            return Response(_ui_file(name), media_type=media_type, headers=UI_HEADERS)

        app.add_api_route(path, serve, methods=["GET"], include_in_schema=False)

    for path, (name, media_type) in UI_FILES.items():
        ui_route(path, name, media_type)

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

    # access_log=False: RequestLogMiddleware logs `http.request` instead (no raw paths).
    uvicorn.run(  # noqa: S104
        create_app(), host="0.0.0.0", port=8000, log_config=None, access_log=False
    )


if __name__ == "__main__":
    main()
