"""Central logging that can only emit allow-listed metadata (hard rule 2).

Why a filter and not just discipline: one careless call that passes page text as `extra`,
formats it into the message, or logs an exception whose message embeds document text,
would leak patient data into App Insights. So the handler cleans every record it handles:

- messages must be fixed event names (`doc_parsed`, `extraction.window_done`); anything else,
  including f-strings and third-party prose, is replaced by `UNSTRUCTURED_EVENT`;
  `args` are dropped, so `"%s" % text` never renders;
- `extra` attributes not in `ALLOWED_FIELDS` are removed; allowed ones must be small scalars;
- exception messages are dropped; only the exception type and code locations are kept.

Limits: the filter only sees records that reach this handler. Loggers with their own
handlers and `propagate=False` (uvicorn's, for one) bypass it, and the filter mutates the
record, so other handlers on the same logger only see a clean record if they run after this
one. Any extra handler (e.g. OpenTelemetry) must get its own `MetadataOnlyFilter`.

Named `logconfig` (not `logging`) so it never shadows the standard library module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sys
import threading
import traceback
from datetime import UTC, datetime
from typing import Any

# Metadata that is safe to log. Add a key only if its values can never be document content.
ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "session_id",
        "tenant_id",
        "doc_sha256",
        "doc_count",
        "page_count",
        "parser_id",
        "prompt_version",
        "duration_ms",
        "tokens_in",
        "tokens_out",
        "status",
        "attempt",
        "http_status",
        "error_type",
        "error_where",
        "doc_type",  # tenant metadata code (e.g. "cioms"), not document content
        "tool_name",  # one of the fixed agent tool names
        "iteration",
        "output_chars",  # length of a rejected model answer, never the answer itself
        "reason",  # fixed codes only: why a session closed (client | ttl | shutdown)
        "session_count",  # how many sessions a sweep or shutdown closed
        "method",  # HTTP method of an `http.request` event
        "route",  # route *template* of an `http.request` event (never the raw path) or unmatched
    }
)
# Loggers that come with their own handlers (uvicorn's default config); M3 AC7 routes them
# through the root handler so the filter sees their records too.
ROUTED_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi", "fastapi")
# Third-party loggers held at WARNING whatever LOG_LEVEL is. Their INFO records are prose
# (so only `unstructured_message` would reach the log) per HTTP call or credential attempt:
# noise that buries the lifecycle events. uvicorn.access logs the raw path (session ids) and
# the client address; `api.RequestLogMiddleware` logs `http.request` in its place.
QUIET_LOGGERS = (
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity",
    "httpx2",
    "uvicorn.access",
)
MAX_STR_LEN = 128
EVENT_NAME = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
UNSTRUCTURED_EVENT = "unstructured_message"

# Attributes every LogRecord has; anything else on a record came from `extra=`.
_STANDARD_ATTRS: frozenset[str] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


def _is_safe_value(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)  # NaN/inf would make the JSON line invalid
    if isinstance(value, bool | int) or value is None:
        return True
    return isinstance(value, str) and len(value) <= MAX_STR_LEN


def _error_where(exc_info: Any) -> str:
    """Code locations of the traceback (file:line:function), never the message."""
    tb = exc_info[2] if exc_info else None
    frames = traceback.extract_tb(tb) if tb else []
    return " < ".join(f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}" for f in frames[-3:])


class MetadataOnlyFilter(logging.Filter):
    """Strips a record down to its event name plus allow-listed scalar metadata."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and record.exc_info[0] is not None:
            record.error_type = record.exc_info[0].__name__
            record.error_where = _error_where(record.exc_info)[:MAX_STR_LEN]
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        msg = record.msg if isinstance(record.msg, str) else ""
        record.msg = msg if EVENT_NAME.fullmatch(msg) else UNSTRUCTURED_EVENT
        record.args = None
        for key in [k for k in record.__dict__ if k not in _STANDARD_ATTRS]:
            if key not in ALLOWED_FIELDS or not _is_safe_value(record.__dict__[key]):
                del record.__dict__[key]
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, event and the allowed metadata."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": str(record.msg),
        }
        for key in sorted(ALLOWED_FIELDS):
            if key in record.__dict__:
                out[key] = record.__dict__[key]
        return json.dumps(out, ensure_ascii=False)


def make_handler(stream: Any = None) -> logging.Handler:
    """A handler with the filter attached (handler filters also see propagated records)."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.addFilter(MetadataOnlyFilter())
    handler.setFormatter(JsonFormatter())
    return handler


def configure_logging(level: str = "INFO") -> None:
    """Replace all root handlers with the metadata-only handler. Call once at startup.

    Also strips the handlers uvicorn installs on its own loggers and lets their records
    propagate to the root handler, and installs the uncaught-exception hooks (M3 AC7).
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(make_handler())
    root.setLevel(level)
    for name in ROUTED_LOGGERS:
        logger = logging.getLogger(name)
        for existing in list(logger.handlers):
            logger.removeHandler(existing)
        logger.propagate = True
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    install_exception_hooks()


_hook_log = logging.getLogger("chatmed.uncaught")


def _log_uncaught(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
    """sys.excepthook: the default hook prints the exception message to stderr."""
    _hook_log.critical("uncaught_exception", exc_info=(exc_type, exc, tb))


def _log_thread_exception(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is not None:
        _log_uncaught(args.exc_type, args.exc_value or args.exc_type(), args.exc_traceback)


def asyncio_exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """For `loop.set_exception_handler`: the default one logs `context['message']` and repr()s."""
    exc = context.get("exception")
    if isinstance(exc, BaseException):
        _hook_log.error("asyncio_exception", exc_info=(type(exc), exc, exc.__traceback__))
    else:
        _hook_log.error("asyncio_exception")


def install_exception_hooks() -> None:
    """Route uncaught exceptions (main thread, other threads) through the filtered logger."""
    sys.excepthook = _log_uncaught
    threading.excepthook = _log_thread_exception
