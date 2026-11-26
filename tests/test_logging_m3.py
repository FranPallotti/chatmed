"""M3 AC7: every log path goes through the metadata-only filter.

uvicorn's own loggers, FastAPI's exception handler, `sys.excepthook`, `threading.excepthook`
and the asyncio loop handler all get an exception whose message holds document text; none of
that text may reach stdout or stderr.
"""

from __future__ import annotations

import asyncio
import logging
import logging.config
import sys
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chatmed.api import Services, create_app
from chatmed.logconfig import ROUTED_LOGGERS, asyncio_exception_handler, configure_logging
from chatmed.parsing import PdfiumParser
from chatmed.session import InMemorySessionStore
from tests.fakes import FakeTenantRepository, ScriptedModelClient
from tests.test_extraction_m2 import make_settings

SECRET = "Zentrobal 01001-004 ACM-2026-0042 hospitalised"


class Leak(RuntimeError):
    pass


@pytest.fixture
def global_logging() -> Iterator[None]:
    """configure_logging changes process-wide state; put everything back afterwards."""
    root = logging.getLogger()
    saved_root = (list(root.handlers), root.level)
    saved = {
        n: (list(logging.getLogger(n).handlers), logging.getLogger(n).propagate)
        for n in ROUTED_LOGGERS
    }
    hooks = (sys.excepthook, threading.excepthook)
    yield
    root.handlers[:] = saved_root[0]
    root.setLevel(saved_root[1])
    for name, (handlers, propagate) in saved.items():
        logging.getLogger(name).handlers[:] = handlers
        logging.getLogger(name).propagate = propagate
    sys.excepthook, threading.excepthook = hooks


def _output(capsys: pytest.CaptureFixture[str]) -> str:
    out, err = capsys.readouterr()
    return out + err


def _app() -> Any:
    services = Services(
        settings=make_settings(),
        store=InMemorySessionStore(max_docs=5, duckdb_memory_limit="256MB"),
        parser=PdfiumParser(),
        model_client=ScriptedModelClient([]),
        repo_factory=FakeTenantRepository,
    )
    app = create_app(services=services, configure=True)

    @app.get("/boom")
    async def boom() -> None:
        raise Leak(SECRET)

    return app


def test_route_exception_is_logged_without_its_message(
    global_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with TestClient(_app(), raise_server_exceptions=False) as client:
        resp = client.get("/boom")
    assert resp.status_code == 500 and resp.json() == {"detail": "internal_error"}
    out = _output(capsys)
    assert '"event": "api.unhandled_error"' in out and '"error_type": "Leak"' in out
    assert SECRET not in out and "Zentrobal" not in out


def test_uvicorn_loggers_are_routed_through_the_filter(
    global_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    import uvicorn.config

    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)  # what `uvicorn ...` installs
    configure_logging()
    for name in ROUTED_LOGGERS:
        assert logging.getLogger(name).handlers == [] and logging.getLogger(name).propagate
    try:
        raise Leak(SECRET)
    except Leak:
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application\n")
    access = logging.getLogger("uvicorn.access")
    access.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4", "GET", f"/x?q={SECRET}", "1.1", 200)
    out = _output(capsys)
    assert "Zentrobal" not in out and "01001-004" not in out
    assert out.count('"event": "unstructured_message"') == 2


def test_sys_excepthook(global_logging: None, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    try:
        raise Leak(SECRET)
    except Leak:
        sys.excepthook(*sys.exc_info())  # type: ignore[arg-type]
    out = _output(capsys)
    assert '"event": "uncaught_exception"' in out and '"error_type": "Leak"' in out
    assert "Zentrobal" not in out


def test_thread_excepthook(global_logging: None, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()

    def work() -> None:
        raise Leak(SECRET)

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    out = _output(capsys)
    assert '"event": "uncaught_exception"' in out and "Zentrobal" not in out


def test_asyncio_exception_handler(
    global_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging()
    loop = asyncio.new_event_loop()
    try:
        loop.set_exception_handler(asyncio_exception_handler)
        loop.call_exception_handler({"message": SECRET, "exception": Leak(SECRET)})
        loop.call_exception_handler({"message": SECRET})
    finally:
        loop.close()
    out = _output(capsys)
    assert out.count('"event": "asyncio_exception"') == 2 and "Zentrobal" not in out
