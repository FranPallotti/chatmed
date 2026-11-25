"""Central logging that can only emit allow-listed metadata (hard rule 2).

Why a filter and not just discipline: one careless call that passes page text as `extra`,
or that logs an exception whose message embeds document text, would leak patient data
into App Insights. So the handler cleans every record before any formatter or
exporter sees it:

- messages are treated as fixed event names: `args` are dropped, so `"%s" % text` never renders;
- `extra` attributes not in `ALLOWED_FIELDS` are removed; allowed ones must be small scalars;
- exception messages are dropped; only the exception type and code locations are kept.

Named `logconfig` (not `logging`) so it never shadows the standard library module.
"""

from __future__ import annotations

import json
import logging
import os
import sys
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
    }
)
MAX_STR_LEN = 128

# Attributes every LogRecord has; anything else on a record came from `extra=`.
_STANDARD_ATTRS: frozenset[str] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


def _is_safe_value(value: Any) -> bool:
    if isinstance(value, bool | int | float) or value is None:
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
        record.msg = str(record.msg)
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
    """Replace all root handlers with the metadata-only handler. Call once at startup."""
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(make_handler())
    root.setLevel(level)
