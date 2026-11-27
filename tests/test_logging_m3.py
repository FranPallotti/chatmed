"""M3 AC7: every log path goes through the metadata-only filter.

uvicorn's own loggers, FastAPI's exception handler, `sys.excepthook`, `threading.excepthook`
and the asyncio loop handler all get an exception whose message holds document text; none of
that text may reach stdout or stderr.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.config
import sys
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chatmed.api import Services, create_app
from chatmed.logconfig import (
    QUIET_LOGGERS,
    ROUTED_LOGGERS,
    asyncio_exception_handler,
    configure_logging,
)
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
    levels = {n: logging.getLogger(n).level for n in QUIET_LOGGERS}
    hooks = (sys.excepthook, threading.excepthook)
    yield
    root.handlers[:] = saved_root[0]
    root.setLevel(saved_root[1])
    for name, (handlers, propagate) in saved.items():
        logging.getLogger(name).handlers[:] = handlers
        logging.getLogger(name).propagate = propagate
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)
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
    # WARNING: uvicorn.access is held at WARNING (QUIET_LOGGERS); routing must still hold.
    access.warning('%s - "%s %s HTTP/%s" %d', "1.2.3.4", "GET", f"/x?q={SECRET}", "1.1", 200)
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


# ---------------------------------------------------------------- http.request (M5a)


def _events(out: str, name: str) -> list[dict[str, Any]]:
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return [e for e in lines if e["event"] == name]


def test_http_request_event_logs_the_route_template_never_the_path(
    global_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with TestClient(_app()) as client:
        sid = client.post("/sessions", json={"tenant_id": "acme"}).json()["session_id"]
        assert client.delete(f"/sessions/{sid}").status_code == 204
        assert client.post(f"/sessions/{sid}/ask", json={"question": "q"}).status_code == 404
    out = _output(capsys)
    assert f"/{sid}" not in out, "the real path (with the session id) reached the log"
    requests = _events(out, "http.request")
    assert len(requests) == 3 and not [e for e in requests if sid in json.dumps(e)]
    assert [(e["method"], e["route"], e["http_status"]) for e in requests] == [
        ("POST", "/sessions", 201),
        ("DELETE", "/sessions/{session_id}", 204),
        ("POST", "/sessions/{session_id}/ask", 404),
    ]
    assert all(isinstance(e["duration_ms"], int) and e["duration_ms"] >= 0 for e in requests)
    assert not [
        e
        for e in requests
        if set(e)
        - {"ts", "level", "logger", "event", "method", "route", "http_status", "duration_ms"}
    ]


def test_unmatched_path_is_logged_as_unmatched(
    global_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with TestClient(_app()) as client:
        assert client.get(f"/nope/{SECRET}").status_code == 404
    out = _output(capsys)
    assert "Zentrobal" not in out and "/nope" not in out
    assert [(e["route"], e["http_status"]) for e in _events(out, "http.request")] == [
        ("unmatched", 404)
    ]


def test_unexpected_method_is_logged_as_other(
    global_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with TestClient(_app()) as client:
        assert client.request("PATCH", "/sessions").status_code == 405
        assert client.request("ZENTROBAL", "/sessions").status_code == 405
    out = _output(capsys)
    assert "PATCH" not in out and "ZENTROBAL" not in out
    assert [(e["method"], e["route"]) for e in _events(out, "http.request")] == [
        ("OTHER", "/sessions"),
        ("OTHER", "/sessions"),
    ]


def test_failed_request_is_logged_as_500(
    global_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with TestClient(_app(), raise_server_exceptions=False) as client:
        client.get("/boom")
    assert [(e["route"], e["http_status"]) for e in _events(_output(capsys), "http.request")] == [
        ("/boom", 500)
    ]


def test_noisy_third_party_loggers_are_held_at_warning(
    global_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("DEBUG")
    for name in QUIET_LOGGERS:
        logging.getLogger(name).info("Request URL: '/x'")
    assert _output(capsys) == ""
    for name in ("azure.core.pipeline.policies.http_logging_policy", "azure.identity", "httpx2"):
        assert name in QUIET_LOGGERS and logging.getLogger(name).level == logging.WARNING
    assert logging.getLogger("uvicorn.access").level == logging.WARNING


def test_main_turns_off_uvicorn_access_log(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    from chatmed import api

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda _app, **kw: calls.append(kw))
    monkeypatch.setattr(api, "create_app", lambda: object())
    api.main()
    assert calls and calls[0]["access_log"] is False and calls[0]["log_config"] is None
