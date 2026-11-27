"""Application Insights telemetry with metadata only (M5a AC5, hard rule 2).

Why the pieces are wired by hand instead of with the `azure-monitor-opentelemetry` distro:
its `configure_azure_monitor()` switches on every instrumentation it ships and installs global
providers, so any library could start emitting, and the FastAPI instrumentation records
exception messages and the raw URL on request spans. Here only two signals exist, both
behind an allow-list at the exporter boundary:

- **Logs:** `MetadataOnlyOtelHandler` on the root logger has its own `MetadataOnlyFilter`
  (the filter only cleans records that reach its handler, see `logconfig`) and then sends
  only the event name, the level and the `ALLOWED_FIELDS` values. No code paths, no
  exception text, no formatted args. Records of the Azure exporter's own loggers stay on the
  console: sent through the exporter they report on, a failing export would produce new
  records to export (a feedback loop while App Insights is unreachable).
- **Request spans:** FastAPI's instrumentation with our own (non-global) tracer provider,
  so nothing else can emit spans. `MetadataOnlySpanExporter` rebuilds every span with the
  method, route template and status code only: no events (that is where exceptions are
  recorded), no status description, no URL, query, client address or headers.

Nothing is enabled unless `APPLICATIONINSIGHTS_CONNECTION_STRING` is set. The Azure exporters
run with offline storage disabled (failed batches are dropped, never written to a local
retry folder) and with statsbeat (the SDK's own usage reporting) off.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass

from fastapi import FastAPI
from opentelemetry._logs import SeverityNumber
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Status

from chatmed.config import Settings
from chatmed.logconfig import ALLOWED_FIELDS, MetadataOnlyFilter

SERVICE_NAME = "chatmed"
# Span attributes that can only hold request metadata (old and new HTTP semantic conventions).
# `http.route` is the route template (`/sessions/{session_id}/ask`), never the raw path.
SPAN_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "http.method",
        "http.request.method",
        "http.route",
        "http.status_code",
        "http.response.status_code",
    }
)
_SEVERITY = {
    logging.DEBUG: SeverityNumber.DEBUG,
    logging.INFO: SeverityNumber.INFO,
    logging.WARNING: SeverityNumber.WARN,
    logging.ERROR: SeverityNumber.ERROR,
    logging.CRITICAL: SeverityNumber.FATAL,
}
_STATSBEAT_DISABLED = "APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL"
# Loggers whose records never go to the telemetry handler (console only).
CONSOLE_ONLY_LOGGERS = ("azure.monitor.opentelemetry.exporter",)


def metadata_only_span(span: ReadableSpan) -> ReadableSpan:
    """A copy of `span` holding only allow-listed request metadata.

    The name is rebuilt from method and route too: for a request no route matched, some
    instrumentation versions name the span after the raw path.
    """
    attributes = {k: v for k, v in (span.attributes or {}).items() if k in SPAN_ATTRIBUTES}
    method = attributes.get("http.request.method") or attributes.get("http.method")
    route = attributes.get("http.route")
    name = " ".join(str(part) for part in (method, route) if part) or "span"
    return ReadableSpan(
        name=name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=attributes,
        events=(),
        links=(),
        kind=span.kind,
        status=Status(span.status.status_code),  # code only: the description can hold text
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class MetadataOnlySpanExporter(SpanExporter):
    """Wraps the real exporter; it only ever sees `metadata_only_span` copies."""

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._inner.export([metadata_only_span(span) for span in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._inner.force_flush(timeout_millis)


def _not_console_only(record: logging.LogRecord) -> bool:
    """False for records of `CONSOLE_ONLY_LOGGERS` and their children."""
    return not any(
        record.name == name or record.name.startswith(name + ".") for name in CONSOLE_ONLY_LOGGERS
    )


class MetadataOnlyOtelHandler(logging.Handler):
    """Sends log records to OpenTelemetry as event name + allow-listed fields only."""

    def __init__(self, logger_provider: LoggerProvider) -> None:
        super().__init__()
        self.addFilter(_not_console_only)  # first: a dropped record is not touched
        self.addFilter(MetadataOnlyFilter())  # runs in handle(), before emit()
        self._provider = logger_provider

    def emit(self, record: logging.LogRecord) -> None:
        attributes = {k: record.__dict__[k] for k in sorted(ALLOWED_FIELDS) if k in record.__dict__}
        self._provider.get_logger(record.name).emit(
            timestamp=int(record.created * 1e9),
            severity_number=_SEVERITY.get(record.levelno, SeverityNumber.UNSPECIFIED),
            severity_text=record.levelname,
            body=str(record.msg),  # the filter has made this a fixed event name
            attributes=attributes,
        )


@dataclass
class Telemetry:
    """Handles to flush and remove telemetry at app shutdown."""

    tracer_provider: TracerProvider
    logger_provider: LoggerProvider
    handler: logging.Handler

    def shutdown(self) -> None:
        logging.getLogger().removeHandler(self.handler)
        self.tracer_provider.shutdown()
        self.logger_provider.shutdown()


def install_telemetry(
    app: FastAPI,
    span_exporter: SpanExporter,
    log_exporter: LogRecordExporter,
    *,
    batch: bool = True,
) -> Telemetry:
    """Wire both signals to the given exporters (tests pass in-memory ones, `batch=False`)."""
    resource = Resource.create({"service.name": SERVICE_NAME})
    tracer_provider = TracerProvider(resource=resource)
    spans = MetadataOnlySpanExporter(span_exporter)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(spans) if batch else SimpleSpanProcessor(spans)
    )
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(log_exporter) if batch else SimpleLogRecordProcessor(log_exporter)
    )
    handler = MetadataOnlyOtelHandler(logger_provider)
    logging.getLogger().addHandler(handler)
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,  # not global: no other library can emit spans
        excluded_urls="healthz",
        exclude_spans=["receive", "send"],  # per-message ASGI spans add nothing
        http_capture_headers_server_request=[],
        http_capture_headers_server_response=[],
    )
    return Telemetry(tracer_provider, logger_provider, handler)


def setup_telemetry(app: FastAPI, settings: Settings) -> Telemetry | None:
    """Application Insights if `APPLICATIONINSIGHTS_CONNECTION_STRING` is set, else nothing."""
    secret = settings.applicationinsights_connection_string
    connection_string = secret.get_secret_value().strip() if secret else ""
    if not connection_string:
        return None
    # Imported here: the exporter package is only needed when telemetry is on.
    from azure.monitor.opentelemetry.exporter import (
        AzureMonitorLogExporter,
        AzureMonitorTraceExporter,
    )

    os.environ.setdefault(_STATSBEAT_DISABLED, "true")
    options = {"connection_string": connection_string, "disable_offline_storage": True}
    return install_telemetry(
        app, AzureMonitorTraceExporter(**options), AzureMonitorLogExporter(**options)
    )
