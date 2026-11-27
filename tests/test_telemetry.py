"""M5a AC5: Application Insights telemetry carries metadata only.

The real Azure exporters are replaced by OpenTelemetry's in-memory exporters ("fake
exporters"), placed exactly where the Azure ones go. Every test sends document-like text
(`SECRET`) through the paths that could leak it: log `extra`, `%s` args, f-strings, exception
messages, request bodies, query strings and an exception raised in a route.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, LogExportResult
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from chatmed import telemetry as telemetry_module
from chatmed.api import Services, create_app
from chatmed.logconfig import ALLOWED_FIELDS, EVENT_NAME
from chatmed.parsing import PdfiumParser
from chatmed.session import InMemorySessionStore
from chatmed.telemetry import SPAN_ATTRIBUTES, Telemetry, install_telemetry, setup_telemetry
from tests.conftest import fixture_bytes
from tests.fakes import FakeModelClient, FakeTenantRepository, ScriptedModelClient
from tests.test_extraction_m2 import make_settings

SECRET = "Zentrobal 01001-004 ACM-2026-0042 hospitalised"
SECRET_WORDS = ("Zentrobal", "01001-004", "ACM-2026-0042", "hospitalised")


class Leak(RuntimeError):
    pass


def _leaks(blob: str) -> list[str]:
    return [w for w in SECRET_WORDS if w in blob]


def _app() -> Any:
    services = Services(
        settings=make_settings(),
        store=InMemorySessionStore(max_docs=5, duckdb_memory_limit="256MB"),
        parser=PdfiumParser(),
        model_client=ScriptedModelClient(
            [], extraction=FakeModelClient(FakeModelClient.from_recordings().responses)
        ),
        repo_factory=FakeTenantRepository,
    )
    app = create_app(services=services, configure=False)

    @app.get("/boom")
    async def boom() -> None:
        raise Leak(SECRET)  # document text in an exception message

    return app


@pytest.fixture
def wired() -> Iterator[tuple[Any, InMemorySpanExporter, InMemoryLogRecordExporter, Telemetry]]:
    root = logging.getLogger()
    level = root.level
    root.setLevel(logging.INFO)
    app = _app()
    spans, logs = InMemorySpanExporter(), InMemoryLogRecordExporter()
    handle = install_telemetry(app, spans, logs, batch=False)
    yield app, spans, logs, handle
    handle.shutdown()
    root.setLevel(level)


def _span_json(exporter: InMemorySpanExporter) -> list[dict[str, Any]]:
    return [json.loads(span.to_json()) for span in exporter.get_finished_spans()]


# ---------------------------------------------------------------- logs


def test_log_exporter_receives_only_allow_listed_fields(wired: Any) -> None:
    _app_, _spans, logs, _ = wired
    log = logging.getLogger("chatmed.test")
    log.info("doc_parsed", extra={"session_id": "s-1", "page_count": 2, "text": SECRET})
    log.warning("%s", SECRET)
    log.info(f"free text {SECRET}")
    try:
        raise Leak(SECRET)
    except Leak:
        log.error("extraction.failed", exc_info=True, extra={"doc_sha256": "ab" * 32})

    records = [r.log_record for r in logs.get_finished_logs()]
    assert len(records) == 4
    for record in records:
        assert set(record.attributes or {}) <= ALLOWED_FIELDS, record.attributes
        assert isinstance(record.body, str) and EVENT_NAME.fullmatch(record.body)
        assert not _leaks(json.dumps({"b": record.body, "a": dict(record.attributes or {})}))
    first, *_, failed = records
    assert first.body == "doc_parsed" and dict(first.attributes or {}) == {
        "session_id": "s-1",
        "page_count": 2,
    }
    assert failed.attributes is not None and failed.attributes["error_type"] == "Leak"
    assert "exception.message" not in failed.attributes  # what the stock handler would send


def test_lifecycle_events_reach_the_log_exporter(wired: Any) -> None:
    app, _spans, logs, _ = wired
    with TestClient(app) as client:
        sid = client.post("/sessions", json={"tenant_id": "acme"}).json()["session_id"]
        client.delete(f"/sessions/{sid}")
    events = {r.log_record.body for r in logs.get_finished_logs()}
    assert {"session.created", "session.closed"} <= events


# ---------------------------------------------------------------- spans


def test_exception_with_document_text_never_reaches_the_span_exporter(wired: Any) -> None:
    """Events (where exceptions are recorded), status description and URL are all dropped.
    A control exporter on the same provider, without our wrapper, shows the instrumentation
    really does record the text, so this test can fail."""
    app, spans, _logs, handle = wired
    raw = InMemorySpanExporter()
    handle.tracer_provider.add_span_processor(SimpleSpanProcessor(raw))
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/boom", params={"q": SECRET})
    assert resp.status_code == 500

    raw_blob = json.dumps(_span_json(raw))
    assert _leaks(raw_blob), "control: the unfiltered span should carry the exception"

    exported = _span_json(spans)
    assert exported and not _leaks(json.dumps(exported))
    (span,) = [s for s in exported if s["attributes"].get("http.route") == "/boom"]
    assert span["events"] == [] and span["links"] == []
    assert span["status"]["status_code"] == "ERROR"
    assert span["status"].get("description") is None
    assert not any("url" in k or "target" in k for k in span["attributes"])
    assert span["name"] == "GET /boom"


def test_request_bodies_and_paths_stay_out_of_spans(wired: Any) -> None:
    app, spans, _logs, _ = wired
    with TestClient(app) as client:
        client.post("/sessions", json={"tenant_id": SECRET})  # 422, body never exported
        sid = client.post("/sessions", json={"tenant_id": "acme"}).json()["session_id"]
        files = {"file": ("c.pdf", fixture_bytes("cioms_initial.pdf"), "application/pdf")}
        assert (
            client.post(
                f"/sessions/{sid}/documents", files=files, data={"doc_type": "cioms"}
            ).status_code
            == 200
        )
        client.get(f"/nothing/{SECRET}")  # unmatched route: raw path must not name the span
    exported = _span_json(spans)
    blob = json.dumps(exported)
    assert not _leaks(blob) and sid not in blob  # route templates only, no raw paths
    for span in exported:
        assert set(span["attributes"]) <= SPAN_ATTRIBUTES
        assert span["events"] == []
    routes = {s["attributes"].get("http.route") for s in exported}
    assert "/sessions/{session_id}/documents" in routes


def test_no_global_provider_so_no_other_library_can_emit(wired: Any) -> None:
    """OpenAI, azure-core, httpx etc. are not instrumented: our providers are not global,
    so no prompt or completion can be captured by any instrumentation."""
    assert isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider)


# ---------------------------------------------------------------- enabling


@pytest.mark.parametrize("value", [None, "", "   "])
def test_telemetry_is_off_without_a_connection_string(value: str | None) -> None:
    settings = make_settings(applicationinsights_connection_string=value)
    assert setup_telemetry(_app(), settings) is None


def test_connection_string_wires_azure_exporters_without_offline_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import azure.monitor.opentelemetry.exporter as azure_exporter

    seen: list[dict[str, Any]] = []

    def fake(kind: type) -> Any:
        def build(**kwargs: Any) -> Any:
            seen.append(kwargs)
            return kind()

        return build

    monkeypatch.setattr(azure_exporter, "AzureMonitorTraceExporter", fake(InMemorySpanExporter))
    monkeypatch.setattr(azure_exporter, "AzureMonitorLogExporter", fake(InMemoryLogRecordExporter))
    monkeypatch.delenv(telemetry_module._STATSBEAT_DISABLED, raising=False)
    conn = "InstrumentationKey=00000000-0000-0000-0000-000000000000"
    handle = setup_telemetry(_app(), make_settings(applicationinsights_connection_string=conn))
    try:
        assert handle is not None
        assert seen == [{"connection_string": conn, "disable_offline_storage": True}] * 2
        import os

        assert os.environ[telemetry_module._STATSBEAT_DISABLED] == "true"
    finally:
        assert handle is not None
        handle.shutdown()


def test_connection_string_is_not_in_the_settings_repr() -> None:
    conn = "InstrumentationKey=11111111-2222-3333-4444-555555555555"
    settings = make_settings(applicationinsights_connection_string=conn)
    assert "11111111" not in repr(settings) and "11111111" not in str(settings.model_dump())


class _FailingLogExporter(InMemoryLogRecordExporter):
    """Fails every export and logs about it the way the Azure exporter does."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def export(self, batch: Any) -> Any:
        self.calls += 1
        if self.calls <= 3:  # bounded, so a regression fails the test instead of recursing
            logging.getLogger("azure.monitor.opentelemetry.exporter.export._base").warning(
                "Retrying due to server request error: %s", "unreachable"
            )
        return LogExportResult.FAILURE


class _Names(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.names: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.names.append(record.name)


def test_a_failing_export_does_not_produce_a_new_export() -> None:
    """The exporter's own log records stay on the console (CONSOLE_ONLY_LOGGERS)."""
    root = logging.getLogger()
    level = root.level
    root.setLevel(logging.INFO)
    console = _Names()
    root.addHandler(console)
    logs = _FailingLogExporter()
    handle = install_telemetry(_app(), InMemorySpanExporter(), logs, batch=False)
    try:
        logging.getLogger("chatmed.session.memory").info("session.created")
    finally:
        handle.shutdown()
        root.removeHandler(console)
        root.setLevel(level)
    assert logs.calls == 1, "the exporter's warning was exported in turn"
    assert "azure.monitor.opentelemetry.exporter.export._base" in console.names
