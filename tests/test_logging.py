"""M0 AC3: the central log handler emits only allow-listed metadata, never content."""

from __future__ import annotations

import io
import json
import logging
import sys
import threading
from collections.abc import Iterator

import pytest

from chatmed.logconfig import (
    MAX_STR_LEN,
    ROUTED_LOGGERS,
    UNSTRUCTURED_EVENT,
    configure_logging,
    make_handler,
)

SECRET = "Hemoglobin 13.4 g/dL for MRN 00123"  # synthetic stand-in for document content


@pytest.fixture
def capture() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    """A logger tree whose parent has the real handler, isolated from the root logger."""
    stream = io.StringIO()
    parent = logging.getLogger("chatmed_test_parent")
    parent.handlers = [make_handler(stream)]
    parent.setLevel(logging.DEBUG)
    old_propagate = parent.propagate
    parent.propagate = False
    yield parent, stream
    parent.handlers = []
    parent.propagate = old_propagate


def lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_arbitrary_extra_payload_is_dropped(capture: tuple[logging.Logger, io.StringIO]) -> None:
    log, stream = capture
    log.info(
        "doc_parsed",
        extra={"text": SECRET, "quote": SECRET, "session_id": "s-1", "page_count": 3},
    )
    out = stream.getvalue()
    assert "13.4" not in out and "Hemoglobin" not in out
    [record] = lines(stream)
    assert record["event"] == "doc_parsed"
    assert record["session_id"] == "s-1" and record["page_count"] == 3
    assert "text" not in record and "quote" not in record


def test_format_args_are_never_interpolated(capture: tuple[logging.Logger, io.StringIO]) -> None:
    log, stream = capture
    log.warning("value_logged %s", SECRET)
    assert "13.4" not in stream.getvalue()
    assert lines(stream)[0]["event"] == UNSTRUCTURED_EVENT  # not an event name either


@pytest.mark.parametrize(
    "message",
    [
        f"extracted {SECRET}",  # f-string: content already inside the message
        SECRET,
        {"quote": SECRET},  # non-string message object
        "a" * 65,  # too long to be an event name
    ],
)
def test_free_text_messages_are_replaced(
    capture: tuple[logging.Logger, io.StringIO], message: object
) -> None:
    log, stream = capture
    log.info(message)
    assert "13.4" not in stream.getvalue()
    assert lines(stream)[0]["event"] == UNSTRUCTURED_EVENT


def test_event_names_are_kept(capture: tuple[logging.Logger, io.StringIO]) -> None:
    log, stream = capture
    log.info("extraction.window_done")
    assert lines(stream)[0]["event"] == "extraction.window_done"


def test_exception_message_is_not_logged(capture: tuple[logging.Logger, io.StringIO]) -> None:
    log, stream = capture
    try:
        raise ValueError(SECRET)
    except ValueError:
        log.exception("extraction_failed", extra={"session_id": "s-1"})
    assert "13.4" not in stream.getvalue()
    [record] = lines(stream)
    assert record["error_type"] == "ValueError"
    assert "test_logging.py" in str(record["error_where"])


def test_allowed_key_with_unsafe_value_is_dropped(
    capture: tuple[logging.Logger, io.StringIO],
) -> None:
    log, stream = capture
    log.info(
        "x",
        extra={
            "status": {"text": SECRET},
            "tenant_id": "a" * (MAX_STR_LEN + 1),
            "duration_ms": float("nan"),
            "attempt": 2,
        },
    )
    [record] = lines(stream)  # json.loads would accept NaN, so also check the key is gone
    assert "status" not in record and "tenant_id" not in record and "duration_ms" not in record
    assert record["attempt"] == 2


def test_records_from_child_loggers_are_filtered(
    capture: tuple[logging.Logger, io.StringIO],
) -> None:
    _, stream = capture
    child = logging.getLogger("chatmed_test_parent.extraction")
    child.info("window_done", extra={"page_text": SECRET, "duration_ms": 12.5})
    assert "13.4" not in stream.getvalue()
    assert lines(stream)[0]["duration_ms"] == 12.5


def test_configure_logging_installs_only_the_metadata_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The central entry point replaces any existing root handler and sets the level."""
    # configure_logging also installs exception hooks and reroutes uvicorn (M3 AC7): undo it.
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    for name in ROUTED_LOGGERS:
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "propagate", logger.propagate)
        monkeypatch.setattr(logger, "handlers", list(logger.handlers))
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        root.addHandler(logging.StreamHandler(io.StringIO()))  # an unsafe pre-existing handler
        configure_logging("WARNING")
        [handler] = root.handlers
        assert any(type(f).__name__ == "MetadataOnlyFilter" for f in handler.filters)
        assert root.level == logging.WARNING
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
